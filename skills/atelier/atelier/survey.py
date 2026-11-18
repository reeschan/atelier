"""Fact sheet validation and OMM cache context."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

FIELDS = ('modules', 'layers', 'likely_files', 'tests', 'conventions', 'rule_docs', 'commands', 'review_skills')
COMMAND_KINDS = ('unit', 'type', 'lint', 'e2e', 'all')
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


def omm_context(root: str | Path, cache_dir: str | Path | None = None) -> dict[str, str | None]:
    """Look up the OMM cache for HEAD.

    source is cache-hit or cache-delta (folder is the .omm to read), miss (no usable scan; reason is
    omm-pr-scan's), or none (reason: tools missing, cache inside the repository, or the lookup failed).
    Without cache_dir, omm-pr-scan picks the place ($OMM_CACHE_DIR, else beside the main clone).
    """
    root = Path(root).resolve()
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
    context = (f'\nRepository: {root}\nGoal: {Path(goal_path).resolve()}\n'
               f'Spec: {Path(spec_path).resolve() if spec_path else "none"}\n'
               f'OMM source: {source if source in ("cache-hit", "cache-delta") else "none"}\n'
               f'OMM folder: {omm.get("folder") or "none"}\nResult: {result}\n')
    prompt.write_text(template.read_text(encoding='utf-8') + context, encoding='utf-8')
    return prompt
