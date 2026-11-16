"""Append run events and summarize observed counts."""
from __future__ import annotations

import json
import math
from pathlib import Path

from .template import should_read


def filter_parts(template, parts):
    return {key: value for key, value in parts.items() if should_read(template, key)}


def measure_read(parts):
    """Estimate tokens per part as characters divided by four, rounded up."""
    return {key: math.ceil(len(value) / 4) for key, value in parts.items()}


def append(record_path, event):
    """Append one validated event; the caller controls event identity and deduplication."""
    required = {
        'launch': {'launch_id', 'role', 'pbi', 'runner', 'model', 'effort', 'level', 'tokens', 'cost', 'read'},
        'check': {'pbi', 'rules', 'stage', 'model', 'level', 'result', 'read_launch_ids', 'first_pass'},
        'fix': {'rule', 'method'},
    }
    if not isinstance(event, dict) or event.get('type') not in required or not required[event['type']] <= event.keys():
        raise ValueError('invalid record event')
    kind = event['type']
    if kind == 'launch':
        tokens = event['tokens']
        valid_tokens = tokens == 'unknown' or (isinstance(tokens, dict) and set(tokens) == {'input', 'output', 'total'} and all(_count(x) or x == 'unknown' for x in tokens.values()))
        if not valid_tokens or not (_number(event['cost']) or event['cost'] == 'unknown') or not isinstance(event['read'], dict) or not all(isinstance(k, str) and _count(v) for k, v in event['read'].items()):
            raise ValueError('invalid launch usage')
    elif kind == 'check':
        if not all(isinstance(event[k], list) and all(isinstance(x, str) for x in event[k]) for k in ('rules', 'read_launch_ids')) or event['result'] not in ('pass', 'fail') or type(event['first_pass']) is not bool:
            raise ValueError('invalid check')
    elif event['method'] not in ('auto', 'asked') or not isinstance(event['rule'], str):
        raise ValueError('invalid fix')
    line = json.dumps(event, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n'
    path = Path(record_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8', newline='\n') as stream:
        stream.write(line)


def _count(value):
    return type(value) is int and value >= 0


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _usage():
    return {'launches': 0, 'tokens': {k: {'known': 0, 'unknown': 0} for k in ('input', 'output', 'total')}, 'cost': {'known': 0, 'unknown': 0}}


def _add_usage(target, event):
    target['launches'] += 1
    for key, total in target['tokens'].items():
        value = event['tokens'].get(key, 'unknown') if isinstance(event['tokens'], dict) else 'unknown'
        total['unknown' if value == 'unknown' else 'known'] += 1 if value == 'unknown' else value
    value = event['cost']
    target['cost']['unknown' if value == 'unknown' else 'known'] += 1 if value == 'unknown' else value


def _rule(rules, key):
    return rules.setdefault(key, {'tokens_read': 0, 'fired': 0, 'fixed': 0, 'violated_while_read': 0})


def report(record_path):
    """Return (Japanese Markdown, JSON-compatible aggregates)."""
    data = {'by_pbi': {}, 'by_role': {}, 'by_model': {}, 'rules': {}, 'first_pass': {}}
    path = Path(record_path)
    events = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()] if path.exists() else []
    launches = {event['launch_id']: event for event in events if event['type'] == 'launch'}
    for event in events:
        kind = event['type']
        if kind == 'launch':
            for field in ('pbi', 'role', 'model'):
                group = data['by_' + field]
                _add_usage(group.setdefault(event[field] or 'unknown', _usage()), event)
            for key, count in event['read'].items():
                _rule(data['rules'], key)['tokens_read'] += count
        elif kind == 'check':
            read = set().union(*(launches[x]['read'].keys() for x in event['read_launch_ids'] if x in launches))
            for key in set(event['rules']):
                rule = _rule(data['rules'], key)
                rule['fired'] += 1
                rule['violated_while_read'] += int(key in read)
            if event['first_pass']:
                group = data['first_pass'].setdefault(event['level'] or 'unknown', {}).setdefault(event['model'] or 'unknown', {'passed': 0, 'total': 0})
                group['total'] += 1
                group['passed'] += int(event['result'] == 'pass')
        elif kind == 'fix':
            _rule(data['rules'], event['rule'])['fixed'] += 1
    for models in data['first_pass'].values():
        for group in models.values():
            group['rate'] = group['passed'] / group['total']
    return _markdown(data), data


def _markdown(data):
    lines = ['# 記録の集計', '']
    for name, label in (('by_pbi', 'PBI 別'), ('by_role', '役別'), ('by_model', 'モデル別')):
        lines += [f'## {label}', '']
        for key, item in data[name].items():
            values = [f"{label}: {item['tokens'][field]['known']}（不明 {item['tokens'][field]['unknown']} 件）" for field, label in (('input', '入力'), ('output', '出力'), ('total', '合計'))]
            lines.append(f"- {key}: 起動 {item['launches']} 件、" + '、'.join(values) + f"、費用 {item['cost']['known']}（不明 {item['cost']['unknown']} 件）")
        lines.append('')
    lines += ['## 規則ごとの件数', '']
    for key, item in data['rules'].items():
        lines.append(f"- {key}: 読込 {item['tokens_read']} トークン、発火 {item['fired']} 件、直し {item['fixed']} 件、読込時の違反 {item['violated_while_read']} 件")
    lines += ['', '## 難度・モデル別の初回合格率', '']
    for level, models in data['first_pass'].items():
        for model, item in models.items():
            lines.append(f"- {level} / {model}: {item['passed']} / {item['total']} 件（{item['rate']:.1%}）")
    return '\n'.join(lines) + '\n'
