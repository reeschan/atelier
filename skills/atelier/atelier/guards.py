"""Compare observable workspace and Git state around a role launch."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from pathlib import Path
import subprocess

LOCKS = {'package-lock.json', 'npm-shrinkwrap.json', 'yarn.lock', 'pnpm-lock.yaml', 'poetry.lock', 'Pipfile.lock', 'uv.lock', 'Cargo.lock', 'Gemfile.lock', 'composer.lock', 'go.sum'}
MANIFESTS = {'package.json', 'pyproject.toml', 'requirements.txt', 'Pipfile', 'Cargo.toml', 'Gemfile', 'composer.json', 'go.mod'}


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
