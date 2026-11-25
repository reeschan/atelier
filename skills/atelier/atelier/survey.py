"""Fact sheet validation and OMM cache context."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from .template import part_enabled

FIELDS = ('modules', 'layers', 'likely_files', 'tests', 'conventions', 'rule_docs', 'commands', 'review_skills')
COMMAND_KINDS = ('unit', 'type', 'lint', 'e2e', 'all', 'acceptance')
# Keys each item of these fields carries besides id, text and source.
EXTRA_KEYS = {'rule_docs': ('path',), 'commands': ('command', 'kind'), 'review_skills': ('name', 'path')}


def inside_repo(path: str, root: str | Path | None = None) -> bool:
    """True when path is relative, has no '..', and (with root) stays inside root."""
    text = path.strip().replace('\\', '/')
    pure = PurePosixPath(text)
    if not text or pure.is_absolute() or re.match(r'^[A-Za-z]:', text) or '..' in pure.parts:
        return False
    if root is None:
        return True
    base = Path(root).resolve()
    return (base / text).resolve().is_relative_to(base)


def _validate_extra(field: str, item: dict, label: str, root: str | Path | None, errors: list[str]) -> None:
    for key in EXTRA_KEYS.get(field, ()):
        if not isinstance(item.get(key), str) or not item[key].strip():
            errors.append(f'{label} の {key} は空でない文字列にしてください。')
    if field == 'commands' and item.get('kind') not in COMMAND_KINDS:
        errors.append(f'{label} の kind は {", ".join(COMMAND_KINDS)} のいずれかにしてください。')
    path = item.get('path')
    if field == 'rule_docs' and isinstance(path, str) and path.strip():
        if not inside_repo(path, root):
            errors.append(f'{label} の path はリポジトリの中の相対パスにしてください。')
        elif root is not None and not (Path(root) / path.strip()).is_file():
            errors.append(f'{label} の path のファイルがありません: {path}')


def validate_facts(obj: Any, root: str | Path | None = None) -> list[str]:
    if not isinstance(obj, dict):
        return ['事実の束は JSON オブジェクトにしてください。']
    errors = []
    if obj.get('schema') != 'atelier.facts/1':
        errors.append('schema は atelier.facts/1 にしてください。')
    for field in FIELDS:
        items = obj.get(field)
        if not isinstance(items, list):
            errors.append(f'{field} は配列にしてください。')
            continue
        for index, item in enumerate(items):
            label = f'{field}[{index}]'
            if not isinstance(item, dict):
                errors.append(f'{label} はオブジェクトにしてください。')
                continue
            for key in ('id', 'text', 'source'):
                if not isinstance(item.get(key), str) or not item[key].strip():
                    errors.append(f'{label} の {key} は空でない文字列にしてください。')
            source = item.get('source')
            if isinstance(source, str) and source.strip() and not re.fullmatch(r'(?:omm:.+|.+:[1-9][0-9]*)', source.strip()):
                errors.append(f'{label} の出典は path:行番号 または omm:要素パスにしてください。')
            _validate_extra(field, item, label, root, errors)
    return errors


def _field_values(facts: Any, field: str, key: str) -> list[str]:
    out: list[str] = []
    for item in (facts.get(field) if isinstance(facts, dict) else None) or []:
        value = item.get(key) if isinstance(item, dict) else None
        if isinstance(value, str) and value.strip() and value.strip() not in out:
            out.append(value.strip())
    return out


def acceptance_commands(facts: Any) -> list[str]:
    """Commands of kind acceptance in a fact sheet, in order, without duplicates."""
    out: list[str] = []
    for item in (facts.get('commands') if isinstance(facts, dict) else None) or []:
        if isinstance(item, dict) and item.get('kind') == 'acceptance':
            command = item.get('command')
            if isinstance(command, str) and command.strip() and command.strip() not in out:
                out.append(command.strip())
    return out


ACCEPTANCE_REASONS = {'template': 'テンプレートで切った', 'not_git': 'git のリポジトリでない', 'no_command': 'acceptance のコマンドが無い'}


def decide_acceptance(facts: Any, template: dict | None, is_git: bool) -> dict:
    """Decide after the survey whether acceptance goes first: {'enabled', 'reason', 'commands'}.

    It is off when the template turns it off, the work root is not a git repository, or the fact sheet has no acceptance command.
    """
    commands = acceptance_commands(facts)
    if not part_enabled(template, 'acceptance_first'):
        reason = ACCEPTANCE_REASONS['template']
    elif not is_git:
        reason = ACCEPTANCE_REASONS['not_git']
    elif not commands:
        reason = ACCEPTANCE_REASONS['no_command']
    else:
        return {'enabled': True, 'reason': None, 'commands': commands}
    return {'enabled': False, 'reason': reason, 'commands': []}


def rule_doc_paths(facts: Any) -> list[str]:
    """Paths of the rule documents in a fact sheet, in order, without duplicates."""
    return _field_values(facts, 'rule_docs', 'path')


def review_skill_names(facts: Any) -> list[str]:
    """Names of the repository's review skills in a fact sheet."""
    return _field_values(facts, 'review_skills', 'name')


def _cache_script() -> Path | None:
    configured = os.environ.get('ATELIER_OMM_SCRIPT')
    if configured:
        return Path(configured) if Path(configured).is_file() else None
    base = Path.home() / '.claude' / 'skills' / 'omm-pr-scan'
    try:
        version = (base / 'ACTIVE').read_text(encoding='utf-8').strip()
        script = base / 'versions' / version / 'scripts' / 'cache.mjs'
        return script if version == 'v1.2' and script.is_file() else None
    except OSError:
        return None


def _run(command: list[str], root: Path) -> str:
    return subprocess.run(command, cwd=root, check=True, capture_output=True, text=True, encoding='utf-8').stdout.strip()


def _external(path: Path, root: Path) -> bool:
    return not path.resolve().is_relative_to(root.resolve())


def _git(args: list[str], root: Path, data: bytes | None = None) -> bytes:
    """Run git directly (not through _run, so a stubbed _run never reaches the committed-OMM path)."""
    return subprocess.run(['git', '-c', 'core.quotepath=off'] + args, cwd=root, check=True, capture_output=True,
                          input=data).stdout


def _has_committed_omm(root: Path) -> bool:
    try:
        return bool(_git(['ls-tree', 'HEAD', '--', '.omm'], root).strip())
    except (OSError, subprocess.SubprocessError):
        return False


def _blobs(root: Path, shas: list[str]) -> list[bytes]:
    """Contents of blobs, read from the object database in one cat-file --batch call."""
    out = _git(['cat-file', '--batch'], root, ''.join(f'{s}\n' for s in shas).encode('ascii'))
    blobs, pos = [], 0
    for sha in shas:
        end = out.index(b'\n', pos)
        head = out[pos:end].split()
        if len(head) != 3 or head[0].decode('ascii') != sha or head[1] != b'blob':
            raise ValueError(f'unexpected cat-file output for {sha}')
        size = int(head[2])
        blobs.append(out[end + 1:end + 1 + size])
        pos = end + 1 + size + 1
    return blobs


def _copy_omm(root: Path, commit: str) -> Path:
    """Copy .omm of commit into <git common dir>/atelier/omm/<12 digits>/.omm; reuse a finished copy."""
    common = Path(_git(['rev-parse', '--git-common-dir'], root).decode('utf-8').strip())
    common = (common if common.is_absolute() else root / common).resolve()
    final = common / 'atelier' / 'omm' / commit[:12]
    if (final / '.omm').is_dir():
        return final / '.omm'
    tree = _git(['ls-tree', '-r', '-z', commit, '--', '.omm'], root).decode('utf-8')
    entries = []
    for record in filter(None, tree.split('\0')):
        meta, path = record.split('\t', 1)
        mode, kind, sha = meta.split()
        if mode == '120000':
            raise ValueError(f'symbolic link in .omm: {path}')
        if kind == 'blob':
            entries.append((path, sha))
    contents = _blobs(root, [sha for _, sha in entries]) if entries else []
    work = final.parent / f'{final.name}.tmp-{os.getpid()}'
    try:
        if work.exists():
            shutil.rmtree(work)
        (work / '.omm').mkdir(parents=True)
        for (path, _), body in zip(entries, contents):
            target = work / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        try:
            os.rename(work, final)
        except OSError:
            if not (final / '.omm').is_dir():
                raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return final / '.omm'


def _shallow_boundary(root: Path) -> set[str]:
    """Commits a shallow clone cut its history at (none in a full clone)."""
    path = Path(_git(['rev-parse', '--git-path', 'shallow'], root).decode('utf-8').strip())
    path = path if path.is_absolute() else root / path
    return set(path.read_text(encoding='ascii').split()) if path.is_file() else set()


def _committed_omm(root: Path) -> dict[str, Any]:
    failed: dict[str, Any] = {'source': 'none', 'folder': None, 'reason': 'committed-failed'}
    sha = re.compile(r'[0-9a-f]{40,64}')
    try:
        head = _git(['rev-parse', '--verify', 'HEAD^{commit}'], root).decode('utf-8').strip()
        commit = _git(['rev-list', '-1', head, '--', '.omm'], root).decode('utf-8').strip()
        if not sha.fullmatch(head) or not sha.fullmatch(commit):
            return failed
        if commit in _shallow_boundary(root):
            return failed  # the history stops here, so a later commit may be taken for the one that changed .omm
        folder = _copy_omm(root, commit)
        diff = _git(['diff', '--name-status', '-M', commit, head, '--', '.', ':(exclude).omm'], root).decode('utf-8')
        lines = [line for line in diff.splitlines() if line.strip()]
        listing = root / 'docs' / 'pbi' / 'prompts' / 'omm-changes.txt'
        listing.parent.mkdir(parents=True, exist_ok=True)
        listing.write_text(''.join(f'{line}\n' for line in lines), encoding='utf-8', newline='\n')
        return {'source': 'committed', 'folder': str(folder), 'commit': commit, 'changes': str(listing), 'count': len(lines)}
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        return failed


def omm_context(root: str | Path, cache_dir: str | Path | None = None) -> dict[str, Any]:
    """Prepare the OMM for HEAD.

    A repository whose HEAD tree holds .omm gets source committed (git only, no cache lookup): folder is a
    copy of .omm from the last commit that changed it, kept outside the worktree; commit is that commit;
    changes is a file listing the files changed since then (git diff --name-status -M, .omm excluded) and
    count its lines. HEAD is read once for both. If git fails there, a shallow clone's history stops at that
    commit, or .omm holds a symbolic link, it is none (reason committed-failed).
    Otherwise source is cache-hit or cache-delta (folder is the .omm to read), miss (no usable scan; reason is
    omm-pr-scan's), or none (reason: tools missing, cache inside the repository, or the lookup failed).
    Without cache_dir, omm-pr-scan picks the place ($OMM_CACHE_DIR, else beside the main clone).
    """
    root = Path(root).resolve()
    if _has_committed_omm(root):
        return _committed_omm(root)
    cache = Path(cache_dir).resolve() if cache_dir else None
    script, node = _cache_script(), shutil.which('node')
    if not script or not node or not shutil.which('omm'):
        return {'source': 'none', 'folder': None, 'reason': 'tools'}
    if cache is not None and not _external(cache, root):
        return {'source': 'none', 'folder': None, 'reason': 'cache-inside-repo'}
    failed = {'source': 'none', 'folder': None, 'reason': 'lookup-failed'}
    try:
        common = [node, str(script)]
        where = ['--cache-dir', str(cache)] if cache is not None else []
        output = _run(common + ['lookup', '--at', 'HEAD'] + where, root)
        if output.startswith('hit '):
            folder = Path(output[4:])
            if _external(folder, root) and (folder / '.omm').is_dir():
                return {'source': 'cache-hit', 'folder': str(folder / '.omm')}
        elif output.startswith('near '):
            match = re.fullmatch(r'near (.+) (\S+) (\d+)', output)
            if not match or not _external(Path(match[1]), root):
                return failed
            derived = _run(common + ['derive', '--from', match[1], '--at', 'HEAD'] + where, root)
            match = re.fullmatch(r'derived (.+) (\S+)\.\.(\S+)', derived)
            if match:
                folder = Path(match[1])
                if _external(folder, root) and (folder / '.omm').is_dir():
                    return {'source': 'cache-delta', 'folder': str(folder / '.omm')}
        elif output.startswith('miss '):
            match = re.fullmatch(r'miss (.+) (\S+)', output)
            if match and _external(Path(match[1]), root):
                return {'source': 'miss', 'folder': None, 'reason': match[2]}
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        pass
    return failed


def omm_scan_steps(root: str | Path, cache_dir: str | Path | None) -> dict[str, Any]:
    """What the caller runs to scan the whole repository and store the scan in the OMM cache."""
    script = _cache_script()
    where = f' --cache-dir "{Path(cache_dir).resolve()}"' if cache_dir else ''
    return {
        'repository': str(Path(root).resolve()),
        'cache_dir': str(Path(cache_dir).resolve()) if cache_dir else None,
        'store_command': f'node "{script}" store --at HEAD --note "atelier survey"{where}' if script else None,
    }


def survey_prompt(root: str | Path, goal_path: str | Path, spec_path: str | Path | None, omm: dict[str, Any]) -> Path:
    root = Path(root).resolve()
    prompt = root / 'docs' / 'pbi' / 'prompts' / 'survey.md'
    result = root / 'docs' / 'pbi' / 'results' / 'survey.json'
    prompt.parent.mkdir(parents=True, exist_ok=True)
    result.parent.mkdir(parents=True, exist_ok=True)
    template = Path(__file__).resolve().parent.parent / 'prompts' / 'surveyor.md'
    source = omm.get('source', 'none')
    if source == 'committed':
        omm_lines = (f'OMM source: committed\nOMM folder: {omm.get("folder")}\nOMM commit: {omm.get("commit")}\n'
                     f'OMM changes: {omm.get("changes")} ({omm.get("count")} files)\n')
    else:
        omm_lines = (f'OMM source: {source if source in ("cache-hit", "cache-delta") else "none"}\n'
                     f'OMM folder: {omm.get("folder") or "none"}\n')
    context = (f'\nRepository: {root}\nGoal: {Path(goal_path).resolve()}\n'
               f'Spec: {Path(spec_path).resolve() if spec_path else "none"}\n'
               f'{omm_lines}Result: {result}\n')
    prompt.write_text(template.read_text(encoding='utf-8') + context, encoding='utf-8')
    return prompt
