"""Build Japanese approval drafts and render them with answer-me-with-html."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from . import template as routing

DEFAULT_SCRIPT = Path(os.environ.get('ATELIER_AM_SCRIPT') or Path.home() / '.claude/skills/answer-me-with-html/scripts/am.mjs')


class PageError(ValueError):
    """A draft or render cannot be presented."""


def _cell(value):
    if value is None or value == '':
        return '未確認'
    return str(value).replace('|', '／').replace('\r', ' ').replace('\n', ' ')


def _route(steps):
    if not steps:
        return '未確認'
    return ' → '.join(' / '.join(_cell(step.get(k)) for k in ('runner', 'model', 'effort') if step.get(k) is not None) for step in steps)


def _table(headers, rows):
    return ['| ' + ' | '.join(headers) + ' |', '| ' + ' | '.join('---' for _ in headers) + ' |'] + [
        '| ' + ' | '.join(_cell(value) for value in row) + ' |' for row in rows]


def _decisions(decisions):
    if not decisions:
        return ['なし']
    lines = []
    for decision in decisions:
        lines += ['```ask', _cell(decision.get('question'))]
        options = decision.get('options') or []
        for option in options:
            lines.append(('* ' if option.get('recommended') else '- ') + _cell(option.get('label')) + ' | ' + _cell(option.get('note')))
        lines += ['```', '']
    return lines[:-1]


def draft(kind, run):
    """Return {'text', 'summary'} from the caller's run and presentation data."""
    titles = {'plan': '計画の承認', 'spec': '仕様の承認', 'fix_list': '直す一覧の承認'}
    if kind not in titles:
        raise PageError('承認ページの種類が不正です。')
    conclusion = _cell(run.get('conclusion'))
    decisions = list(run.get('decisions') or [])
    decisions.append({'question': titles[kind] + 'を進めますか', 'options': [{'label': '承認する', 'note': 'このまま進める', 'recommended': True}, {'label': '修正を指定する', 'note': '変更点を伝えて直す'}]})
    lines = ['---', 'lang: ja', 'template: sheet', 'cols: 2', 'title: ' + titles[kind], '---',
             '## 結論 {span=2}', '```callout info 確認してください', conclusion, '```', '',
             '## 今の流れ', _cell(run.get('before')), '', '## この run のあとの流れ', _cell(run.get('after')), '',
             '## 決めること {span=2}'] + _decisions(decisions)
    summaries = ['結論: ' + conclusion]
    if kind == 'plan':
        template = run.get('template') or {}
        roles = template.get('roles') or {}
        pbis = run.get('pbis') or []
        lines += ['', '## PBI {span=2}']
        rows = []
        external = []
        for pbi in pbis:
            level = pbi.get('level')
            try:
                choice = routing.choose_route(template, pbi)
            except (ValueError, KeyError, TypeError):
                choice = {'rule': None, 'route': []}
            route = choice['route']
            row = [pbi.get('id'), pbi.get('title'), level, pbi.get('level_reason'), choice['rule'], _route(route)]
            rows.append(row)
            summaries.append(' | '.join(_cell(v) for v in row))
            if any(step.get('runner') != 'agent' for step in route):
                external.append((pbi.get('id'), pbi.get('title'), _route(route)))
            elif not route:
                external.append((pbi.get('id'), pbi.get('title'), '送信先は未確認'))
        lines += _table(['PBI', '内容', '難度', '理由', '規則', '経路'], rows)
        lines += ['', '## 慎重に扱う候補 {span=2}']
        candidates = [(p.get('id'), p.get('sensitive_reason')) for p in pbis if p.get('sensitive_reason')]
        lines += _table(['PBI', '理由'], candidates) if candidates else ['候補なし']
        lines += ['慎重に扱うと決めた部分は作業から外します。依存する作業は保留し、別の作業への引き継ぎを提案します。', '', '## 外部へ送る PBI {span=2}']
        lines += _table(['PBI', '内容', '送信先'], external) if external else ['なし']
        lines += ['', '## 役・モデル・テンプレート {span=2}', 'テンプレート: ' + _cell(template.get('name'))]
        labels = {'surveyor': '調べる役', 'interviewer': '仕様を詰める役', 'planner': '計画を決める役', 'reviewer': '見直す役', 'worker_fix': '修正する役'}
        lines += _table(['役', '実行先とモデル'], [(labels.get(k, k), _route([v])) for k, v in roles.items()])
        limits = template.get('limits') or {}
        lines += ['', '## 上限と見積もり {span=2}']
        lines += _table(['項目', '値'], [('自動修正の回数', limits.get('auto_fix')), ('E2E の回数', limits.get('e2e_rounds')), ('予算（トークン）', limits.get('budget_tokens') if limits.get('budget_tokens') is not None else '上限なし'), ('予算（米ドル）', limits.get('budget_usd') if limits.get('budget_usd') is not None else '上限なし'), ('見積もり', run.get('estimate'))])
        lines += ['', '難度の変更や計画の修正を指定できます。外部への送信と慎重に扱う候補も確認してください。']
    elif kind == 'spec':
        lines += ['', '## 仕様 {span=2}', str(run.get('spec') or '未確認'), '', '仕様を承認するか、修正を指定してください。']
    else:
        lines += ['', '## 直す一覧 {span=2}']
        fixes = run.get('fix_list') or []
        lines += _table(['項目', '内容', '理由'], [(f.get('id'), f.get('title'), f.get('reason')) for f in fixes]) if fixes else ['直す項目はありません。']
        lines += ['', '一覧を承認するか、変更を指定してください。']
    summaries.append('決めること: ' + ('、'.join(_cell(d.get('question')) for d in decisions) if decisions else '承認するか、修正するか'))
    return {'text': '\n'.join(lines) + '\n', 'summary': summaries}


def render(draft_path, *, script=DEFAULT_SCRIPT, node='node'):
    """Render a UTF-8 draft and return the page path, or None when answer-me-with-html is not installed.

    Without a page the caller shows the draft and the summary lines instead. A garbled draft is always refused (G10).
    """
    path = Path(draft_path)
    try:
        text = path.read_text(encoding='utf-8-sig')
        if '???' in text or '\ufffd' in text:
            raise PageError('文字化けした原稿です。原稿を書き直してください。')
        if not Path(script).is_file() or not shutil.which(str(node)):
            return None
        result = subprocess.run([str(node), str(script), 'render', str(path), '--no-open'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        raise PageError(f'ページを作成できません: {exc}') from exc
    if result.returncode:
        raise PageError('ページを作成できません: ' + (result.stderr.strip() or result.stdout.strip()))
    for line in result.stdout.splitlines():
        if line.startswith('✓ ') and line[2:].strip():
            return Path(line[2:].strip())
    raise PageError('ページの保存先を確認できません。')

