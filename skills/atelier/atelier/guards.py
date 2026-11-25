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

from . import signatures as _signatures

LOCKS = {'package-lock.json', 'npm-shrinkwrap.json', 'yarn.lock', 'pnpm-lock.yaml', 'poetry.lock', 'Pipfile.lock', 'uv.lock', 'Cargo.lock', 'Gemfile.lock', 'composer.lock', 'go.sum'}
MANIFESTS = {'package.json', 'pyproject.toml', 'requirements.txt', 'Pipfile', 'Cargo.toml', 'Gemfile', 'composer.json', 'go.mod'}
_TRIPLE_Q_RE = re.compile(r'\?{3,}')
_CJK_CHAR = r'[\u3040-\u309f\u30a0-\u30ff\u31f0-\u31ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af\uff66-\uff9f]'
_CJK_Q_CJK_RE = re.compile(rf'{_CJK_CHAR}\?{_CJK_CHAR}')
# The G1 detail for a locked file (an acceptance test file or the lock list) that differs from the lock commit.
LOCKED_DETAIL = 'locked file changed: '
# The G1 details for a locked public interface signature and a locked key of a file workers also touch.
SIGNATURE_DETAIL = 'locked signature changed: '
KEY_DETAIL = 'locked key changed: '


def _git(root, *args):
    p = subprocess.run(['git', '-C', str(root), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode:
        if args[:2] == ('rev-parse', '--verify'):
            return ''
        raise RuntimeError('git inspection failed: ' + p.stderr.decode('utf-8', 'replace').strip())
    return p.stdout.decode('utf-8', 'surrogateescape')


def _digest(p):
    if p.is_symlink():
        return 'link:' + os.readlink(p)
    if p.is_file():
        return hashlib.sha256(p.read_bytes()).hexdigest()
    return None


def _files(root):
    names = _git(root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').split('\0')
    files = {}
    for name in names:
        if not name:
            continue
        try:
            files[name] = _digest(root / name)
        except OSError as exc:
            raise RuntimeError(f'cannot inspect {name}: {exc}') from exc
    return files


def approved_edit(root, path):
    """Return the work-root-relative name and current digest of a file the user approved changing.

    compare() lets that file differ from the snapshot without a G1 violation while its content still matches the digest.
    """
    root = Path(root).resolve()
    p = Path(path)
    p = Path(os.path.normpath(p if p.is_absolute() else root / p))
    # Resolve only the parent so that a symlink keeps its own entry and link digest, as in snapshot().
    p = p.parent.resolve() / p.name
    try:
        name = p.relative_to(root).as_posix()
    except ValueError:
        raise ValueError(f'outside the work root: {path}') from None
    if not (p.is_symlink() or p.is_file()):
        raise ValueError(f'not a file: {path}')
    return {'path': name, 'digest': _digest(p)}


def snapshot(root, allowed_paths=None, report_path=None, child_pids=None, outside_edits=None, locked=None):
    """The work root before a launch.

    outside_edits lets compare() pass a change to an existing file outside allowed_paths. It is a dict:
    "blocked" lists the paths that stay closed (a PBI's do_not_touch), and "no_tests" closes every test
    file, inside allowed_paths too (a fix PBI must not rewrite the tests it is meant to make pass).
    locked is {"commit", "blobs": {path: blob id}, "signatures", "keys"} once the acceptance tests are locked:
    compare() stops a change to any of those files, inside allowed_paths too, by its git blob, and a change of a
    locked signature or key during the launch. The signatures and keys as they are before the launch are kept with
    it, so a difference that was there before the launch is not laid on it.
    """
    root = Path(root).resolve()
    if locked and (locked.get('signatures') or locked.get('keys')):
        locked = {**locked, 'signatures_now': _signatures.current(root, locked.get('signatures') or {}),
                  'keys_now': _signatures.key_values(root, locked.get('keys') or {})}
    return {'root': str(root), 'head': _git(root, 'rev-parse', '--verify', 'HEAD').strip(),
            'branch': _git(root, 'symbolic-ref', '--short', '-q', 'HEAD').strip() if _branch_exists(root) else '',
            'stash': _git(root, 'stash', 'list', '--format=%H'), 'files': _files(root),
            'allowed_paths': list(allowed_paths) if allowed_paths is not None else None,
            'outside_edits': dict(outside_edits) if outside_edits is not None else None,
            'locked': dict(locked) if locked else None,
            'report_path': str(report_path) if report_path else None, 'child_pids': list(child_pids or [])}


# Directories and file names that hold tests by the usual conventions of common languages.
_TEST_DIRS = {'test', 'tests', '__tests__', '__snapshots__', 'spec', 'e2e', 'testdata', 'test_data'}
_TEST_NAMES = ('test_*', 'conftest.py', '*_test.*', '*.test.*', '*.spec.*', '*_spec.*', '*.cy.*', '*Test.*', '*Tests.*', '*.snap')


def is_test_file(name):
    """True when a work-root-relative path is a test file: under a test directory or named like a test."""
    parts = PurePosixPath(name.replace('\\', '/').removeprefix('./')).parts
    if not parts:
        return False
    if any(part.lower() in _TEST_DIRS for part in parts[:-1]):
        return True
    return any(fnmatch.fnmatchcase(parts[-1], pattern) for pattern in _TEST_NAMES)


def _edit_of_existing(name, before, after):
    """An existing file whose content changed; adding, deleting or linking a file is not such an edit."""
    old, new = before['files'].get(name), after['files'].get(name)
    return (
        old is not None and new is not None and old != new
        and not str(old).startswith('link:') and not str(new).startswith('link:')
    )


def _locked_blobs(before):
    return (before.get('locked') or {}).get('blobs') or {}


def locked_changes(root, blobs, ignore=()):
    """Locked files whose git blob in the work root is not the locked one (missing files included).

    The blob is what git would store for the file (git hash-object, with the repository's line ending
    conversion), so a change that git converts away on add is not a change.
    """
    root = Path(root).resolve()
    names = [n for n in sorted(blobs) if not _allowed(n, ignore)]
    present = [n for n in names if (root / n).is_file() and not (root / n).is_symlink()]
    current = {}
    # The names go as arguments after --, in batches that keep the command line short, so a name with a newline
    # stays one path (--stdin-paths would split it). git still applies the repository's filters to each path.
    for batch in _name_batches(present):
        proc = subprocess.run(['git', '-C', str(root), 'hash-object', '--', *batch],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode:
            raise RuntimeError('git inspection failed: ' + proc.stderr.decode('utf-8', 'replace').strip())
        current.update(zip(batch, proc.stdout.decode('ascii').split()))
    return [n for n in names if current.get(n) != blobs[n]]


_BATCH_CHARS = 6000


def _name_batches(names):
    """The names in order, in lists whose total length stays under _BATCH_CHARS (at least one name in each)."""
    batch, size = [], 0
    for name in names:
        if batch and size + len(name) + 1 > _BATCH_CHARS:
            yield batch
            batch, size = [], 0
        batch.append(name)
        size += len(name) + 1
    if batch:
        yield batch


def _ignored(name, ignore, directory=False):
    """True when a path belongs to another launch of the batch (for a directory: a path inside it does)."""
    if _allowed(name, ignore):
        return True
    return directory and any(_allowed(p.rstrip('/'), [name]) for p in ignore if name not in ('', '.'))


def locked_signature_changes(root, locked, ignore=()):
    """G1 violations for the locked signatures that differ from the lock now and changed during the launch.

    A signature whose tool is not there now is not compared. The detail gives the locked and the current signature.
    """
    entries = (locked or {}).get('signatures') or {}
    if not entries:
        return []
    before = (locked or {}).get('signatures_now') or {}
    now = _signatures.current(root, entries)
    out = []
    for key in sorted(entries):
        entry = entries[key]
        if key not in now or _ignored(str(entry.get('file') or ''), ignore, entry.get('language') == _signatures.GO):
            continue
        sig = now[key].get('signature')
        if sig == entry.get('signature') or (key in before and before[key].get('signature') == sig):
            continue
        new = _signatures.brief(sig) if sig is not None else f"（{now[key].get('reason') or '取り出せない'}）"
        out.append({'rule': 'G1', 'module': entry['module'], 'name': entry['name'],
                    'detail': f"{SIGNATURE_DETAIL}{entry['module']} {entry['name']}"
                              f"（鍵: {_signatures.brief(entry.get('signature'))} / 今: {new}）"})
    return out


def locked_key_changes(root, locked, ignore=()):
    """G1 violations for the locked keys whose value differs from the lock now and changed during the launch."""
    keys = (locked or {}).get('keys') or {}
    if not keys:
        return []
    before = (locked or {}).get('keys_now') or {}
    now = _signatures.key_values(root, keys)
    out = []
    for path in sorted(keys):
        if _allowed(path, ignore):
            continue
        for key in sorted(keys[path]):
            got = (now.get(path) or {}).get(key) or {'error': '読めない'}
            if 'value' in got and _signatures.same(got['value'], keys[path][key]):
                continue
            was = (before.get(path) or {}).get(key)
            if was is not None and _signatures.same(was, got):
                continue
            new = _signatures.brief(got['value']) if 'value' in got else f"（{got['error']}）"
            out.append({'rule': 'G1', 'path': path, 'key': key,
                        'detail': f"{KEY_DETAIL}{path} {key}（鍵: {_signatures.brief(keys[path][key])} / 今: {new}）"})
    return out


def _open_outside(name, before, after):
    """True when the snapshot lets this change outside allowed_paths through."""
    rule = before.get('outside_edits')
    if rule is None or name in _locked_blobs(before) or not _edit_of_existing(name, before, after):
        return False
    if _allowed(name, rule.get('blocked') or []):
        return False
    return not (rule.get('no_tests') and is_test_file(name))


def outside_edits(root, before, after=None, ignore=()):
    """Existing files changed outside the snapshot's allowed paths that the snapshot let through.

    ignore names paths that belong to other launches of the same launch batch; their changes are theirs.
    """
    if before.get('outside_edits') is None or before['allowed_paths'] is None:
        return []
    after = after if after is not None else snapshot(Path(root))
    return sorted(
        name for name in before['files'].keys() & after['files'].keys()
        if not _allowed(name, before['allowed_paths']) and not _allowed(name, ignore) and _open_outside(name, before, after)
    )


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


def compare(root, before, approved_edits=None, launch_id=None, *, after=None, ignore=()):
    """Compare the work root with a snapshot taken before a launch.

    approved_edits lets a changed file outside the allowed paths pass while it still has the approved digest.
    An approval registered during launch_id is checked even if the file ends where it started, so a role
    that writes back the content it read before the approval does not undo the approved edit unseen.
    In a launch batch, after is the work root once every launch ended, and ignore names the paths that
    belong to the other launches of the batch: their changes are compared in those launches, not here.
    """
    root = Path(root).resolve()
    approved = {e['path']: e['digest'] for e in approved_edits or []}
    during = {e['path']: e['digest'] for e in approved_edits or [] if launch_id is not None and e.get('launch_id') == launch_id}
    if str(root) != before['root']:
        raise ValueError('snapshot belongs to another root')
    after = after if after is not None else snapshot(root)
    violations = []
    for key in ('head', 'branch', 'stash'):
        if before[key] != after[key]:
            violations.append({'rule': 'G2', 'detail': key + ' changed'})
    changed = {p for p in before['files'].keys() | after['files'].keys()
               if before['files'].get(p) != after['files'].get(p) and not _allowed(p, ignore)}
    locked = _locked_blobs(before)
    for name in sorted(changed):
        user_approved = name in approved and after['files'].get(name) == approved[name]
        rule = before.get('outside_edits') or {}
        if name in locked:
            pass  # A locked file is judged by its git blob below, whoever's paths it is in.
        elif rule.get('no_tests') and is_test_file(name) and not user_approved:
            violations.append({'rule': 'G1', 'detail': 'test file changed by a fix PBI: ' + name})
        elif _allowed(name, rule.get('blocked') or []) and not user_approved:
            # do_not_touch holds even when a target directory covers the file.
            violations.append({'rule': 'G1', 'detail': 'do_not_touch changed: ' + name})
        elif (before['allowed_paths'] is not None and not _allowed(name, before['allowed_paths'])
              and not user_approved and (name in approved or not _open_outside(name, before, after))):
            # A file with a registered approval keeps that content: the outside allowance does not reopen it.
            violations.append({'rule': 'G1', 'detail': 'changed outside allowed paths: ' + name})
        if Path(name).name in LOCKS | MANIFESTS:
            violations.append({'rule': 'G5', 'detail': 'dependency file changed: ' + name})
    violations.extend({'rule': 'G1', 'detail': LOCKED_DETAIL + name} for name in locked_changes(root, locked, ignore))
    violations.extend(locked_signature_changes(root, before.get('locked'), ignore))
    violations.extend(locked_key_changes(root, before.get('locked'), ignore))
    for name, digest in sorted(during.items()):
        outside = before['allowed_paths'] is not None and not _allowed(name, before['allowed_paths'])
        if outside and name not in changed and not _allowed(name, ignore) and after['files'].get(name) != digest:
            violations.append({'rule': 'G1', 'detail': 'approved edit overwritten: ' + name})
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


def covered(name, paths):
    """True when a work-root-relative name is one of paths, inside one of them, or matches one as a glob."""
    return _allowed(name, paths)
