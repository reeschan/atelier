"""Load and select user-owned routing templates."""
from __future__ import annotations

import copy
import json
import math
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_TEMPLATE = Path(__file__).resolve().parent.parent / 'template.default.json'
LEVELS = tuple(f'L{i}' for i in range(1, 6))
ROLES = ('surveyor', 'interviewer', 'planner', 'reviewer')
# Launches started together at most; 3 matches the fix stage's old group limit and stays clear of most runners' rate limits.
DEFAULT_PARALLEL = 3


class TemplateError(ValueError):
    """A template, switch, or registration is invalid."""


def _read(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError, UnicodeError) as exc:
        raise TemplateError(f'{label}を読み込めません: {path}') from exc


def _fail(message: str) -> None:
    raise TemplateError(message)


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and '\n' not in value and '\r' not in value


def _runners(path: Path | str | None) -> set[str]:
    """Enabled runners. Without a registration file only Claude Code itself (agent) is available."""
    file = Path(path).expanduser() if path is not None else Path.home() / '.config/atelier/agents.json'
    if not file.exists():
        return {'agent'}
    data = _read(file, '実行先の登録')
    if not isinstance(data, dict) or not isinstance(data.get('runners'), dict):
        _fail('実行先の登録には runners が必要です')
    enabled = set()
    for name, entry in data['runners'].items():
        if not _text(name) or not isinstance(entry, dict) or not isinstance(entry.get('enabled'), bool):
            _fail('実行先の登録形式が不正です')
        if entry['enabled']:
            enabled.add(name)
    return enabled


def _step(value: Any, label: str, runners: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != {'runner', 'model', 'effort'}:
        _fail(f'{label}には runner、model、effort が必要です')
    if not _text(value['runner']) or not _text(value['model']) or (value['effort'] is not None and not _text(value['effort'])):
        _fail(f'{label}の実行先、モデルまたは effort が不正です')
    if value['runner'] not in runners:
        _fail(f'{label}の実行先は登録されていないか無効です')


def validate_template(value: Any, agents_path: Path | str | None = None) -> dict:
    if not isinstance(value, dict) or set(value) != {'name', 'routes', 'roles', 'limits', 'parts', 'reading'} or not _text(value.get('name')):
        _fail('テンプレートの名前または節が不正です')
    runners = _runners(agents_path)
    routes = value['routes']
    if not isinstance(routes, dict) or set(routes) != set(LEVELS):
        _fail('経路には L1 から L5 が必要です')
    for level, route in routes.items():
        if not isinstance(route, list) or not route:
            _fail(f'{level}の経路は空にできません')
        for step in route:
            _step(step, f'{level}の経路', runners)
    roles = value['roles']
    if not isinstance(roles, dict) or not set(ROLES) <= set(roles) or set(roles) - set(ROLES) - {'worker_fix'}:
        _fail('役の設定が不正です')
    for role, step in roles.items():
        _step(step, f'役 {role}', runners)
    limits = value['limits']
    if not isinstance(limits, dict) or set(limits) - {'auto_fix', 'e2e_rounds', 'budget_tokens', 'budget_usd', 'parallel'} or limits.get('auto_fix') != 1 or type(limits.get('auto_fix')) is not int:
        _fail('上限の設定が不正です')
    rounds = limits.get('e2e_rounds', 2)
    if type(rounds) is not int or rounds < 0:
        _fail('E2E の上限は 0 以上の整数です')
    parallel = limits.get('parallel', DEFAULT_PARALLEL)
    if type(parallel) is not int or parallel < 1:
        _fail('同時に動かす起動の上限 (parallel) は 1 以上の整数です')
    for key in ('budget_tokens', 'budget_usd'):
        number = limits.get(key)
        if number is not None and (type(number) not in (int, float) or not math.isfinite(number) or number < 0 or (key == 'budget_tokens' and type(number) is not int)):
            _fail('予算は 0 以上の数値または null です')
    for section in ('parts', 'reading'):
        mapping = value[section]
        if not isinstance(mapping, dict) or any(not _text(k) or type(v) is not bool for k, v in mapping.items()):
            _fail(f'{section} は真偽値の対応表です')
    result = copy.deepcopy(value)
    result['limits'].setdefault('e2e_rounds', 2)
    result['limits'].setdefault('budget_tokens', None)
    result['limits'].setdefault('budget_usd', None)
    result['limits'].setdefault('parallel', DEFAULT_PARALLEL)
    return result


def load_template(path: Path | str | None = None, agents_path: Path | str | None = None) -> dict:
    return validate_template(_read(Path(path).expanduser() if path is not None else DEFAULT_TEMPLATE, 'テンプレート'), agents_path)


def load_levels() -> list[dict]:
    return _read(Path(__file__).with_name('levels.json'), '難度定義')


def choose_route(template: dict, pbi: dict) -> dict:
    level = pbi.get('level')
    reason = pbi.get('level_reason')
    if level not in LEVELS or not _text(reason):
        _fail('PBI の難度と理由が必要です')
    return {'level': level, 'route': copy.deepcopy(template['routes'][level]), 'rule': f'routes.{level}', 'inputs': {'level': level, 'reason': reason}, 'template': template['name']}


def role_for(template: dict, role: str) -> dict:
    if role not in template['roles']:
        _fail('指定された役の設定がありません')
    return copy.deepcopy(template['roles'][role])


def should_read(template: dict, identifier: str) -> bool:
    return template['reading'].get(identifier, True)


def resolve_template(base_path: Path | str | None = None, switch: dict | None = None, *, agents_path: Path | str | None = None, now: datetime | None = None) -> dict:
    if switch is None:
        return {'template': load_template(base_path, agents_path), 'ask': False}
    if not isinstance(switch, dict) or set(switch) != {'template', 'until', 'on_expire', 'reason'} or not all(_text(switch.get(k)) for k in ('template', 'until', 'reason')) or switch['on_expire'] not in ('ask', 'drop'):
        _fail('テンプレート切り替えの設定が不正です')
    try:
        end = date.fromisoformat(switch['until'])
    except ValueError as exc:
        raise TemplateError('切り替え期限は YYYY-MM-DD で指定してください') from exc
    current = now or datetime.now(timezone.utc)
    if current.date() <= end:
        return {'template': load_template(switch['template'], agents_path), 'ask': False}
    if switch['on_expire'] == 'ask':
        return {'template': None, 'ask': True}
    return {'template': load_template(base_path, agents_path), 'ask': False}


def parallel_limit(run_or_limits: dict) -> int:
    """The most launches to start together for a run (or its limits); runs started before the limit existed get the default."""
    limits = run_or_limits.get('limits', run_or_limits) if isinstance(run_or_limits, dict) else {}
    value = (limits or {}).get('parallel', DEFAULT_PARALLEL)
    return value if type(value) is int and value >= 1 else DEFAULT_PARALLEL
