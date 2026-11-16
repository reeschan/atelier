"""Compare observable workspace and Git state around a role launch."""
from __future__ import annotations

from collections import Counter
import difflib
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import re

LOCKS = {'package-lock.json', 'npm-shrinkwrap.json', 'yarn.lock', 'pnpm-lock.yaml', 'poetry.lock', 'Pipfile.lock', 'uv.lock', 'Cargo.lock', 'Gemfile.lock', 'composer.lock', 'go.sum'}
MANIFESTS = {'package.json', 'pyproject.toml', 'requirements.txt', 'Pipfile', 'Cargo.toml', 'Gemfile', 'composer.json', 'go.mod'}
_TRIPLE_Q_RE = re.compile(r'\?{3,}')
_CJK_CHAR = r'[\u3040-\u309f\u30a0-\u30ff\u31f0-\u31ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af\uff66-\uff9f]'
_CJK_Q_CJK_RE = re.compile(rf'{_CJK_CHAR}\?{_CJK_CHAR}')


def _git(root, *args):
    p = subprocess.run(['git', '-C', str(root), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode:
        if args[:2] == ('rev-parse', '--verify'):
            return ''
        raise RuntimeError('git inspection failed: ' + p.stderr.decode('utf-8', 'replace').strip())
    return p.stdout.decode('utf-8', 'surrogateescape')


def _files(root):
    names = _git(root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').split('\0')
    files = {}
    for name in names:
        if not name:
            continue
        p = root / name
        try:
            if p.is_symlink():
                files[name] = 'link:' + os.readlink(p)
            elif p.is_file():
                files[name] = hashlib.sha256(p.read_bytes()).hexdigest()
            else:
                files[name] = None
        except OSError as exc:
            raise RuntimeError(f'cannot inspect {name}: {exc}') from exc
    return files


def snapshot(root, allowed_paths=None, report_path=None, child_pids=None):
    root = Path(root).resolve()
    return {'root': str(root), 'head': _git(root, 'rev-parse', '--verify', 'HEAD').strip(),
            'branch': _git(root, 'symbolic-ref', '--short', '-q', 'HEAD').strip() if _branch_exists(root) else '',
            'stash': _git(root, 'stash', 'list', '--format=%H'), 'files': _files(root),
            'allowed_paths': list(allowed_paths) if allowed_paths is not None else None,
            'report_path': str(report_path) if report_path else None, 'child_pids': list(child_pids or [])}


def _branch_exists(root):
    p = subprocess.run(['git', '-C', str(root), 'symbolic-ref', '-q', 'HEAD'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return p.returncode == 0


def _allowed(name, paths):
    return any(name == p.rstrip('/') or name.startswith(p.rstrip('/') + '/') or fnmatch.fnmatchcase(name, p) for p in paths)


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def compare(root, before):
    root = Path(root).resolve()
    if str(root) != before['root']:
        raise ValueError('snapshot belongs to another root')
    after = snapshot(root)
    violations = []
    for key in ('head', 'branch', 'stash'):
        if before[key] != after[key]:
            violations.append({'rule': 'G2', 'detail': key + ' changed'})
    changed = {p for p in before['files'].keys() | after['files'].keys() if before['files'].get(p) != after['files'].get(p)}
    for name in sorted(changed):
        if before['allowed_paths'] is not None and not _allowed(name, before['allowed_paths']):
            violations.append({'rule': 'G1', 'detail': 'changed outside allowed paths: ' + name})
        if Path(name).name in LOCKS | MANIFESTS:
            violations.append({'rule': 'G5', 'detail': 'dependency file changed: ' + name})
    for pid in before['child_pids']:
        if _alive(pid):
            violations.append({'rule': 'G14', 'detail': f'child process still running: {pid}'})
    if before['report_path']:
        path = Path(before['report_path'])
        path = path if path.is_absolute() else root / path
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(data, dict) or not data:
                raise ValueError('empty report')
        except (OSError, ValueError):
            violations.append({'rule': 'G14', 'detail': 'missing or unfinished report: ' + str(path)})
    return violations


def sends_approved(run, pbi, step):
    runner = step.get('runner') if isinstance(step, dict) else None
    if runner == 'agent':
        return True
    approvals = run.get('approvals', {})
    sends = approvals.get('plan_sends', {})
    identifier = pbi.get('id') if isinstance(pbi, dict) else pbi
    return bool(runner and approvals.get('plan') is True and isinstance(sends, dict)
                and isinstance(sends.get(identifier), list) and runner in sends[identifier])


def scan_secrets(text):
    """Return categories and line numbers without exposing matched values."""
    patterns = [
        ('private_key', r'-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----'),
        ('api_key', r'\b(?:sk-[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{30,}|AKIA[A-Z0-9]{16})\b'),
        ('token', r'\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|Bearer\s+[A-Za-z0-9._~+/-]{16,})'),
        ('secret_assignment', r'(?im)^\s*(?:export\s+)?(?=[A-Za-z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|PRIVATE_?KEY|CREDENTIAL))[A-Za-z_][A-Za-z0-9_]*\s*=\s*[^\s#]+'),
    ]
    findings = []
    for kind, pattern in patterns:
        for match in re.finditer(pattern, text):
            findings.append({'kind': kind, 'line': text.count('\n', 0, match.start()) + 1})
    return sorted(findings, key=lambda item: (item['line'], item['kind']))


def install_attempts(before, after):
    old, new = before['files'], after['files']
    return sorted(name for name in old.keys() | new.keys()
                  if Path(name).name in LOCKS | MANIFESTS
                  and (name not in old or name not in new or old[name] != new[name]))


def forbidden_args(component, argv):
    blocked = component.get('forbidden_args', [])
    if not isinstance(blocked, list) or not all(isinstance(x, str) and x for x in blocked):
        raise ValueError('forbidden_args must be a list of nonempty strings')
    return [option for option in blocked if any(arg == option or arg.startswith(option + '=') for arg in argv)]


def destructive_changes(root, base):
    """Inspect tracked and untracked working-tree changes against a commit."""
    root = Path(root).resolve()
    _git(root, 'cat-file', '-e', base + '^{commit}')
    fields = _git(root, 'diff', '--name-status', '-z', '--no-renames', base, '--').split('\0')
    changes = [(fields[i], fields[i + 1]) for i in range(0, len(fields) - 1, 2) if fields[i]]
    untracked = _git(root, 'ls-files', '--others', '--exclude-standard', '-z').split('\0')
    changes.extend(('?', name) for name in untracked if name)
    findings = []
    for status, name in changes:
        path = Path(name)
        if status == 'D':
            findings.append({'kind': 'deleted', 'path': name})
        if any(part.lower() in ('migration', 'migrations') for part in path.parts) or re.search(r'(^|[_.-])migration(s)?([_.-]|$)', path.name, re.I):
            findings.append({'kind': 'migration', 'path': name})
        schema_name = re.search(r'(^|[_.-])schema([_.-]|$)', path.name, re.I)
        schema_sql = False
        if path.suffix.lower() == '.sql' and status != 'D':
            try:
                content = (root / path).read_text(encoding='utf-8')
                schema_sql = bool(re.search(r'\b(?:CREATE|ALTER|DROP|TRUNCATE)\s+(?:TABLE|SCHEMA|DATABASE|INDEX|TYPE|VIEW)\b', content, re.I))
            except (OSError, UnicodeError):
                schema_sql = True
        if schema_name or schema_sql:
            findings.append({'kind': 'schema', 'path': name})
    return findings


def _is_fake_test_path(name):
    parts = PurePosixPath(name.replace('\\', '/').lstrip('./')).parts
    return any(parts[i:i + 2] == ('tests', 'fakes') for i in range(len(parts) - 1))


def _git_show_bytes(root, ref, name):
    if not ref:
        return b''
    proc = subprocess.run(['git', '-C', str(root), 'show', f'{ref}:{name}'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return proc.stdout if proc.returncode == 0 else b''


def garbled_changes(root, before, base=None):
    """Detect added garbled text (U+FFFD, new ???+, CJK?CJK) in changed non-binary files."""
    root = Path(root).resolve()
    after = snapshot(root)
    changed = sorted(
        p for p in before['files'].keys() | after['files'].keys()
        if before['files'].get(p) != after['files'].get(p)
        and after['files'].get(p) is not None
        and not str(after['files'].get(p)).startswith('link:')
    )
    base_ref = base or before.get('head') or ''
    findings = []
    for name in changed:
        if _is_fake_test_path(name):
            continue
        try:
            raw = (root / name).read_bytes()
        except OSError:
            continue
        if b'\x00' in raw:
            continue
        base_raw = _git_show_bytes(root, base_ref, name)
        if b'\x00' in base_raw:
            continue
        base_lines = base_raw.decode('utf-8', 'replace').splitlines()
        new_lines = raw.decode('utf-8', 'replace').splitlines()
        matcher = difflib.SequenceMatcher(None, base_lines, new_lines)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag not in ('replace', 'insert'):
                continue
            base_runs = Counter(m.group(0) for bl in base_lines[i1:i2] for m in _TRIPLE_Q_RE.finditer(bl))
            for offset, line in enumerate(new_lines[j1:j2]):
                reasons = []
                if '\ufffd' in line:
                    reasons.append('U+FFFD')
                for m in _TRIPLE_Q_RE.finditer(line):
                    run = m.group(0)
                    if base_runs[run] > 0:
                        base_runs[run] -= 1
                    else:
                        reasons.append('???')
                        break
                if _CJK_Q_CJK_RE.search(line):
                    reasons.append('CJK?CJK')
                if reasons:
                    findings.append({'path': name, 'line': j1 + offset + 1, 'text': line, 'reason': ', '.join(reasons)})
    return findings
