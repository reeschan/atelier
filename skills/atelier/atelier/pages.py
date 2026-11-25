"""Build Japanese approval drafts and render them with answer-me-with-html."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from . import stepcheck
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


def acceptance_toggle_text(enabled, reason):
    """入り or 切り (with the reason) for the acceptance_first part."""
    if enabled is None:
        return '未確認'
    return '入り' if enabled else '切り（' + _cell(reason) + '）'


KINDS = (('acceptance', '受け入れ'), ('stub', 'stub'), ('build', 'build'), ('e2e', 'E2E'))
NO_MODE = '—'


def _modes_by_pbi(scenarios):
    """PBI id -> modes of the requirements that have a case assigned to it, in order of appearance."""
    modes = {}
    for scenario in scenarios:
        for case in scenario.get('cases') or []:
            owner, mode = case.get('pbi'), scenario.get('mode')
            if owner and mode and mode not in modes.setdefault(owner, []):
                modes[owner].append(mode)
    return modes


def _case_value(values, key):
    """A case's value for the Examples table: a non-empty string as it is, any other value as JSON ("" and null
    included, so a boundary value is not shown as unknown), and NO_MODE for a column the case does not have."""
    if key not in values:
        return NO_MODE
    value = values[key]
    if isinstance(value, str) and value:
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _scenario_lines(scenarios):
    """One block per requirement: normal shows Gherkin and the Examples table; light modes list case ids and test names."""
    if not scenarios:
        return ['なし']
    lines = []
    for scenario in scenarios:
        mode = scenario.get('mode')
        cases = [c for c in scenario.get('cases') or [] if isinstance(c, dict)]
        lines += ['**' + _cell(scenario.get('id')) + ': ' + _cell(scenario.get('text')) + '（' + _cell(mode) + '）**']
        if mode == 'normal':
            lines += ['```gherkin', str(scenario.get('gherkin') or '').strip(), '```']
            columns = []
            for case in cases:
                columns += [k for k in (case.get('values') or {}) if k not in columns]
            lines += _table(['ケース', *columns], [[c.get('id'), *[_case_value(c.get('values') or {}, k) for k in columns]] for c in cases])
        else:
            lines += _table(['ケース', 'テスト名'], [(c.get('id'), c.get('test_name') or c.get('name') or scenario.get('text')) for c in cases])
        lines.append('')
    return lines[:-1]


def step_check_text(enabled, interfaces):
    """Where the step definition checks run, and the languages they skip as the interfaces' modules show them."""
    if not enabled:
        return '流さない（受け入れを先に置かない）'
    skipped = stepcheck.interface_languages([i for i in interfaces or [] if isinstance(i, dict)])
    checked = '・'.join(stepcheck.CHECKED_LANGUAGES)
    if not skipped:
        return checked + ' で流す（飛ばす言語なし）'
    return checked + ' で流す（飛ばす言語: ' + stepcheck.skipped_text(skipped) + '）'


def _estimate(run, pbis):
    counts = {}
    for pbi in pbis:
        counts[pbi.get('kind') or 'build'] = counts.get(pbi.get('kind') or 'build', 0) + 1
    text = '、'.join(label + ' ' + str(counts.get(kind, 0)) for kind, label in KINDS)
    base = run.get('estimate')
    return (str(base) + '（' + text + '）') if base else '（' + text + '）'


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
        scenarios = [sc for sc in (run.get('scenarios') or []) if isinstance(sc, dict)]
        modes = _modes_by_pbi(scenarios)
        for pbi in pbis:
            level = pbi.get('level')
            try:
                choice = routing.choose_route(template, pbi)
            except (ValueError, KeyError, TypeError):
                choice = {'rule': None, 'route': []}
            route = choice['route']
            row = [pbi.get('id'), pbi.get('title'), level, pbi.get('level_reason'), choice['rule'], _route(route), '、'.join(modes.get(pbi.get('id'), [])) or NO_MODE, pbi.get('kind') or 'build']
            rows.append(row)
            summaries.append(' | '.join(_cell(v) for v in row))
            if any(step.get('runner') != 'agent' for step in route):
                external.append((pbi.get('id'), pbi.get('title'), _route(route)))
            elif not route:
                external.append((pbi.get('id'), pbi.get('title'), '送信先は未確認'))
        lines += _table(['PBI', '内容', '難度', '理由', '規則', '経路', 'モード', '種類'], rows)
        if run.get('acceptance_first'):
            lines += ['', '## シナリオ {span=2}'] + _scenario_lines(scenarios)
        lines += ['', '## 慎重に扱う候補 {span=2}']
        candidates = [(p.get('id'), p.get('sensitive_reason')) for p in pbis if p.get('sensitive_reason')]
        lines += _table(['PBI', '理由'], candidates) if candidates else ['候補なし']
        lines += ['慎重に扱うと決めた部分は作業から外します。依存する作業は保留し、別の作業への引き継ぎを提案します。', '', '## 外部へ送る PBI {span=2}']
        lines += _table(['PBI', '内容', '送信先'], external) if external else ['なし']
        review = run.get('final_review')
        lines += ['', '## 検査と最後の見直し {span=2}']
        lines += _table(['項目', '値'], [
            ('波と直しのあとの検査', '、'.join(run.get('plan_checks') or []) or 'なし（PBI ごとの検査だけ）'),
            ('E2E', '、'.join(run.get('plan_e2e') or []) or 'なし（E2E を飛ばす）'),
            ('ステップ定義の機械の検査', step_check_text(run.get('acceptance_first'), run.get('interfaces'))),
            ('最後の見直し', (review.get('skill', '') + ' ' + review.get('args', '')).strip() + '（呼び出し側がスキルで流す）' if review else '見直し役'),
        ])
        needs = [(pbi.get('id'), need.get('source'), need.get('dest'), need.get('why')) for pbi in pbis for need in (pbi.get('data_needs') or [])]
        lines += ['', '## 外のデータ {span=2}']
        lines += _table(['PBI', '元（リポジトリの外）', '置き先（リポジトリの中）', '使い道'], needs) + ['', '承認すると、作る前に呼び出し側がこのファイルを置き先へ写します。'] if needs else ['なし']
        enabled = run.get('acceptance_first')
        lines += ['', '## 部品の入り切り {span=2}']
        lines += _table(['部品', '入り切り', '理由'], [('受け入れを先に置く (acceptance_first)', '未確認' if enabled is None else '入り' if enabled else '切り', '' if enabled is None else 'なし' if enabled else run.get('acceptance_reason'))])
        lines += ['', '## 役・モデル・テンプレート {span=2}', 'テンプレート: ' + _cell(template.get('name'))]
        labels = {'surveyor': '調べる役', 'interviewer': '仕様を詰める役', 'planner': '計画を決める役', 'reviewer': '見直す役', 'worker_fix': '修正する役'}
        lines += _table(['役', '実行先とモデル'], [(labels.get(k, k), _route([v])) for k, v in roles.items()])
        limits = template.get('limits') or {}
        lines += ['', '## 上限と見積もり {span=2}']
        lines += _table(['項目', '値'], [('同じ持ち越しの修正の PBI の回数', 2), ('E2E の回数', limits.get('e2e_rounds')), ('予算（トークン）', limits.get('budget_tokens') if limits.get('budget_tokens') is not None else '上限なし'), ('予算（米ドル）', limits.get('budget_usd') if limits.get('budget_usd') is not None else '上限なし'), ('見積もり', _estimate(run, pbis))])
        lines += ['', '難度の変更や計画の修正を指定できます。外部への送信と慎重に扱う候補も確認してください。']
    elif kind == 'spec':
        lines += ['', '## 仕様 {span=2}', str(run.get('spec') or '未確認'), '', '仕様を承認するか、修正を指定してください。']
    else:
        lines += ['', '## 直す一覧 {span=2}']
        fixes = run.get('fix_list') or []
        lines += _table(['項目', '内容', '理由'], [(f.get('id'), f.get('title'), f.get('reason')) for f in fixes]) if fixes else ['直す項目はありません。']
        tests = run.get('outside_tests') or []
        if tests:
            lines += ['', '## 対象の外で変えたテスト {span=2}']
            lines += _table(['PBI', 'ファイル', '理由'], [(e.get('by'), e.get('file'), e.get('why') or '（理由の記載なし）') for e in tests])
            lines += ['', 'テストの期待値の変更が仕様に合うかを確かめてください。']
        candidates = run.get('candidates') or []
        lines += ['', '## 見送りの候補 {span=2}']
        lines += _table(['項目', '種類', '内容', '理由'], [(c.get('id'), c.get('kind'), c.get('title'), c.get('reason')) for c in candidates]) + ['', '直したい項目があれば、ID を指定して直す一覧に足せます。'] if candidates else ['なし']
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

