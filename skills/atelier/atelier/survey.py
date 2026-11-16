"""Fact sheet validation and OMM cache context."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

FIELDS = ('modules', 'layers', 'likely_files', 'tests', 'conventions')


def validate_facts(obj: Any) -> list[str]:
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
    return errors


def _cache_script() -> Path | None:
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


def omm_context(root: str | Path, cache_dir: str | Path) -> dict[str, str | None]:
    root, cache = Path(root).resolve(), Path(cache_dir).resolve()
    none = {'source': 'none', 'folder': None}
    script, node = _cache_script(), shutil.which('node')
    if not script or not node or not shutil.which('omm') or not _external(cache, root):
        return none
    try:
        common = [node, str(script)]
        output = _run(common + ['lookup', '--at', 'HEAD', '--cache-dir', str(cache)], root)
        if output.startswith('hit '):
            folder = Path(output[4:])
            if _external(folder, root) and (folder / '.omm').is_dir():
                return {'source': 'cache-hit', 'folder': str(folder / '.omm')}
        elif output.startswith('near '):
            match = re.fullmatch(r'near (.+) (\S+) (\d+)', output)
            if not match or not _external(Path(match[1]), root):
                return none
            derived = _run(common + ['derive', '--from', match[1], '--at', 'HEAD', '--cache-dir', str(cache)], root)
            match = re.fullmatch(r'derived (.+) (\S+)\.\.(\S+)', derived)
            if match:
                folder = Path(match[1])
                if _external(folder, root) and (folder / '.omm').is_dir():
                    return {'source': 'cache-delta', 'folder': str(folder / '.omm')}
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        pass
    return none


def survey_prompt(root: str | Path, goal_path: str | Path, spec_path: str | Path | None, omm: dict[str, Any]) -> Path:
    root = Path(root).resolve()
    prompt = root / 'docs' / 'pbi' / 'prompts' / 'survey.md'
    result = root / 'docs' / 'pbi' / 'results' / 'survey.json'
    prompt.parent.mkdir(parents=True, exist_ok=True)
    result.parent.mkdir(parents=True, exist_ok=True)
    template = Path(__file__).resolve().parent.parent / 'prompts' / 'surveyor.md'
    context = (f'\nRepository: {root}\nGoal: {Path(goal_path).resolve()}\n'
               f'Spec: {Path(spec_path).resolve() if spec_path else "none"}\n'
               f'OMM source: {omm.get("source", "none")}\n'
               f'OMM folder: {omm.get("folder") or "none"}\nResult: {result}\n')
    prompt.write_text(template.read_text(encoding='utf-8') + context, encoding='utf-8')
    return prompt
