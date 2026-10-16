"""担当を決める。

  python select_model.py <facts.json> [<brief の Jev 応答.json>] [--today YYYY-MM-DD] [--tier haiku|sonnet|opus] [--skip opencode,agy,<model>] [--pbi N] [--routing <routing.json>] [--home DIR] [--now ISO]
  python select_model.py --all docs/pbi [--today ...] [--skip ...] [--routing ...] [--home ...] [--now ...]

facts.json: {"files_changed", "files_new", "files_deleted", "modules", "criteria", "sensitive"}
Jev 応答: ゲート brief（novelty / coupling / ambiguity）の API 応答。無い・エラーのときは各 score を 1 とする。
--tier: 決定表を飛ばして段を指定する（9.1 の昇格で route を出し直すとき）。
--skip: route から外す実行先（このセッションで上限・拒否が出た実行先、昇格で外す opencode）。agent は外せない。
        runner 名（opencode など）はその runner の手順を全部、モデル ID（/ を含む値。opencode/glm-5.3-flash-free など）は
        その model の手順だけを外す。
--pbi: 交互グループを回す数（PBI の番号）。省くと facts のファイル名の PBI-<番号> から取り、取れなければ 0。
--all: <dir>/PBI-*.facts.json と <dir>/jev/PBI-*.brief.json を全部判定し、<dir>/PBI-*.route.json に書いて、
       標準出力には 1 PBI 1 行の表だけを出す（オーケストレーターの文脈を細く保つため）。
--routing: 段ごとの route を書いたテンプレート（routing.default.json と同じ形。studio/ の画面で作れる。studio/serve.py なら画面から直接置ける）。
       指定が無ければ、--all のときは <dir>/routing.json、次にこのディレクトリの routing.json を探し、
       どちらも無ければ下の既定の表を使う。
--home: 優先テンプレートの home（templates.resolve_home）。--now: 今の時刻（templates.parse_time）。
        --today を省いて --now があれば、今日は --now の日付。--all でも同じ引数が効く。
標準出力（1 件のとき）: {"tier", "route": [{"runner", "model", "effort"}, ...], "review", "scores", "reasons",
"template": {"name", "applied", "note"}}

優先テンプレート（期限付きの上書き。appendix/priority-templates.md）は振り分けテンプレート（routing）より先に当たる。
写し (overrides.json) の適用が active で、優先テンプレートにその段の上書きがあり、sensitive でなければ、
route を「優先テンプレートの手順（--skip の runner・model と until を過ぎた手順は外す）＋ routing のその段の agent」に置き換える。
優先テンプレートの手順には交互グループ {"rotate": [手順, ...]} を書ける。PBI の番号 mod メンバー数 番目を先頭にして
元の並びを回し、残りはその PBI の控えとして後ろに続ける（expand_rotate）。展開してから skip と until で絞る。

route は上から順に試す実行先。runner は opencode（opencode CLI の OpenCode Go）/ codex（Codex CLI）/ agy（Antigravity
CLI の Claude）/ agent（Claude Code の Agent ツール = Anthropic のサブスク）。
- opus の段は設計判断が要る PBI（novelty=2 かつ ambiguity=2）だけ。agent の opus で実装する。
- sensitive・coupling=2 は sonnet の段で実装し、review を "deep" にする（Opus のオーケストレーターが重点的に読む）。
  sensitive はリポジトリの外のサービスに出さないため、route を agent の sonnet だけにする。
- haiku・sonnet の段は opencode（DeepSeek）を先頭に置く。CODEX_UNTIL の日まで（その日を含む）は codex を agy の前に置く。
"""
import datetime
import json
import re
import sys
import templates
from pathlib import Path

LOW_CONFIDENCE = 0.7
KEYS = ('novelty', 'coupling', 'ambiguity')
TIERS = ('haiku', 'sonnet', 'opus')
RUNNERS = ('opencode', 'codex', 'agy')
CODEX_UNTIL = datetime.date(2026, 10, 23)
CODEX_MODEL = 'gpt-6-astra'
DEEPSEEK = 'opencode-go/deepseek-v4.1-flash'

# 段ごとの実行先。Codex の effort は high を上限とし、haiku 相当は medium に下げる。
# Antigravity には haiku が無いので、haiku 相当は Sonnet の low にする。
OPENCODE = {
    'haiku': {'runner': 'opencode', 'model': DEEPSEEK, 'effort': None},
    'sonnet': {'runner': 'opencode', 'model': DEEPSEEK, 'effort': None},
}
CODEX = {
    'haiku': {'runner': 'codex', 'model': CODEX_MODEL, 'effort': 'medium'},
    'sonnet': {'runner': 'codex', 'model': CODEX_MODEL, 'effort': 'high'},
}
# Antigravity はヘッドレス (-p) でコマンドの実行が拒否されることがある (2026-10-13 時点)。
# opus の段は使わない
AGY = {
    'haiku': {'runner': 'agy', 'model': 'claude-sonnet-5-5-low', 'effort': None},
    'sonnet': {'runner': 'agy', 'model': 'claude-sonnet-5-5-high', 'effort': None},
}
AGENT = {
    'haiku': {'runner': 'agent', 'model': 'haiku', 'effort': 'max'},
    'sonnet': {'runner': 'agent', 'model': 'sonnet', 'effort': 'high'},
    'opus': {'runner': 'agent', 'model': 'opus', 'effort': None},
}


def level(answer: dict | None) -> tuple[int, str]:
    if not answer or 'probabilities' not in answer:
        return 1, 'Jev の答えが無いため 1 とした'
    probs = answer['probabilities']
    lv = int(max(probs, key=lambda k: probs[k]))
    conf = answer.get('confidence', 0)
    if conf < LOW_CONFIDENCE and lv > 0:
        return lv - 1, f'confidence {conf:.2f} < {LOW_CONFIDENCE} のため {lv} を {lv - 1} に下げた'
    return lv, f'confidence {conf:.2f}'


def claude_tier(facts: dict, scores: dict) -> tuple[str, str]:
    if scores['novelty'] == 2 and scores['ambiguity'] == 2:
        return 'opus', 'novelty=2 かつ ambiguity=2（設計判断が要る）'
    if facts.get('sensitive'):
        return 'sonnet', 'sensitive（Agent の sonnet だけ・重点レビュー）'
    if scores['coupling'] == 2:
        return 'sonnet', 'coupling=2（重点レビュー）'
    if any(scores[k] >= 1 for k in KEYS):
        return 'sonnet', 'novelty・coupling・ambiguity のどれかが 1 以上'
    if facts.get('files_changed', 0) >= 4:
        return 'sonnet', f"files_changed={facts['files_changed']} >= 4"
    if facts.get('modules', 0) >= 2:
        return 'sonnet', f"modules={facts['modules']} >= 2"
    return 'haiku', 'どの条件にも当たらない'


def default_routing() -> dict:
    """テンプレートが無いときの表。routing.default.json と同じ中身。"""
    def until(step, day):
        return {**step, 'until': day.isoformat()}
    return {'name': 'default', 'tiers': {
        'haiku': [OPENCODE['haiku'], until(CODEX['haiku'], CODEX_UNTIL), AGY['haiku'], AGENT['haiku']],
        'sonnet': [OPENCODE['sonnet'], until(CODEX['sonnet'], CODEX_UNTIL), AGY['sonnet'], AGENT['sonnet']],
        'opus': [AGENT['opus']],
    }}


def nonempty_str(v) -> bool:
    return isinstance(v, str) and bool(v.strip())


def routing_error(table) -> str | None:
    """テンプレートの形を確かめ、問題があればその説明を返す。どの段も最後の 1 つだけが agent で、間は RUNNERS のどれか。"""
    tiers = table.get('tiers') if isinstance(table, dict) else None
    if not isinstance(tiers, dict):
        return 'tiers が無い'
    for tier in TIERS:
        steps = tiers.get(tier)
        if not isinstance(steps, list) or not steps or not all(isinstance(s, dict) for s in steps):
            return f'tiers.{tier} が無いか、形が違う'
        if steps[-1].get('runner') != 'agent':
            return f'tiers.{tier} の最後は agent の実行先にする'
        if not nonempty_str(steps[-1].get('model')):
            return f'tiers.{tier} の agent に model が無い'
        for s in steps[:-1]:
            if s.get('runner') not in RUNNERS:
                return f'tiers.{tier} の途中は {"/".join(RUNNERS)} のどれか（{s.get("runner")}）'
            if not nonempty_str(s.get('model')):
                return f'tiers.{tier} の {s["runner"]} に model が無い'
            if s.get('until'):
                try:
                    datetime.date.fromisoformat(s['until'])
                except (TypeError, ValueError):
                    return f'tiers.{tier} の until が日付でない（{s["until"]}）'
    return None


def load_routing(path) -> dict:
    table = json.load(open(path, encoding='utf-8'))
    err = routing_error(table)
    if err:
        sys.exit(f'{path}: {err}')
    return table


def find_routing(explicit: str | None, all_dir: str | None) -> tuple[dict, str]:
    candidates = [Path(explicit)] if explicit else []
    if not explicit:
        if all_dir:
            candidates.append(Path(all_dir) / 'routing.json')
        candidates.append(Path(__file__).resolve().parent / 'routing.json')
    for c in candidates:
        if c.exists():
            return load_routing(c), str(c)
    if explicit:
        sys.exit(f'--routing のファイルが無い: {explicit}')
    return default_routing(), ''


def skipped(step: dict, skip: frozenset) -> bool:
    """skip の runner 名か model ID に当たる手順。"""
    return step['runner'] in skip or step['model'] in skip


def pbi_number(name: str) -> int:
    """PBI-007.facts.json などから 7。取れなければ 0。"""
    m = re.search(r'PBI-(\d+)', name)
    return int(m.group(1)) if m else 0


def expand_rotate(steps: list, pbi: int) -> list[dict]:
    """交互グループを、pbi mod メンバー数 番目を先頭にして回した平らな手順の列に展開する。"""
    out = []
    for s in steps:
        if templates.ROTATE_KEY in s:
            members = s[templates.ROTATE_KEY]
            k = pbi % len(members)
            out.extend(members[k:] + members[:k])
        else:
            out.append(s)
    return out


def route(tier: str, today: datetime.date, sensitive: bool = False, skip: frozenset = frozenset(),
          routing: dict | None = None) -> list[dict]:
    """sensitive はリポジトリの外のサービスに出さないため、その段の agent だけにする。"""
    steps = (routing or default_routing())['tiers'][tier]
    if sensitive:
        return [strip(steps[-1])]
    out = []
    for s in steps[:-1]:
        if skipped(s, skip):
            continue
        if s.get('until') and today > datetime.date.fromisoformat(s['until']):
            continue
        out.append(strip(s))
    out.append(strip(steps[-1]))
    return out


def strip(step: dict) -> dict:
    return {'runner': step['runner'], 'model': step['model'], 'effort': step.get('effort')}


def apply_template(tier: str, sensitive: bool, skip: frozenset, today: datetime.date, routing: dict,
                   home: Path, now: datetime.datetime, pbi: int = 0) -> dict:
    """写しの適用から route（当てないときは None）と template の欄を組む。

    当てる条件は、適用の状態が active・優先テンプレートが読める・その段がある・sensitive でない、の全部。
    route は優先テンプレートの手順（交互グループは pbi で回して展開し、--skip の runner・model と
    until を過ぎた手順は外す）のあとに、routing のその段の agent（最後の手順）を付けたもの。
    """
    application = templates.load_overrides(home).get('application')
    if not isinstance(application, dict):
        return {'name': None, 'applied': False, 'note': '適用なし', 'route': None}
    raw_name = application.get('template')
    name = raw_name if isinstance(raw_name, str) else None
    try:
        state = templates.state_of(application, now)
    except ValueError:
        return {'name': name, 'applied': False, 'note': '適用の時刻が不正のため当てない', 'route': None}
    if state == 'expired_ask':
        return {'name': name, 'applied': False,
                'note': '期限切れ: 延長・やめる・期限なしの確認が要る', 'route': None}
    if state == 'expired_drop':
        return {'name': name, 'applied': False, 'note': '期限切れ (drop) のため当てない', 'route': None}
    if sensitive:
        return {'name': name, 'applied': False, 'note': 'sensitive のため当てない', 'route': None}
    template = templates.load_template(home, raw_name) if isinstance(raw_name, str) else None
    if template is None:
        return {'name': name, 'applied': False, 'note': 'テンプレートが見つからないか不正', 'route': None}
    tier_steps = template['tiers'].get(tier)
    if not tier_steps:
        return {'name': name, 'applied': False, 'note': 'この段の上書きなし', 'route': None}
    fallback = strip(routing['tiers'][tier][-1])
    steps = [strip(s) for s in expand_rotate(tier_steps, pbi)
             if not skipped(s, skip)
             and not (s.get('until') and today > datetime.date.fromisoformat(s['until']))]
    if not steps or steps[-1] != fallback:
        steps = steps + [fallback]
    until = application.get('until')
    note = f'{name} を期限なしで当てた' if until is None else f'{name} を当てた ({until} まで)'
    return {'name': name, 'applied': True, 'note': note, 'route': steps}


def load_answers(path) -> dict:
    if not path:
        return {}
    try:
        return json.load(open(path, encoding='utf-8')).get('answers', {})
    except (OSError, ValueError):
        return {}


def decide(facts: dict, answers: dict, today: datetime.date, forced: str | None = None,
           skip: frozenset = frozenset(), routing: dict | None = None, home: Path | None = None,
           now: datetime.datetime | None = None, pbi: int = 0) -> dict:
    scores, reasons = {}, []
    for k in KEYS:
        scores[k], note = level(answers.get(k))
        reasons.append(f'{k}={scores[k]}（{note}）')
    if forced:
        tier, why = forced, f'--tier {forced} で指定'
    else:
        tier, why = claude_tier(facts, scores)
    sensitive = bool(facts.get('sensitive'))
    routing = routing or default_routing()
    if now is None:
        now = datetime.datetime.now().astimezone()
    tpl = apply_template(tier, sensitive, skip, today, routing,
                         home if home is not None else templates.resolve_home(None), now, pbi)
    steps = tpl['route'] if tpl['applied'] else route(tier, today, sensitive, skip, routing)
    review = 'deep' if sensitive or scores['coupling'] == 2 else 'normal'
    if tpl['applied']:
        why += f"（優先テンプレート {tpl['name']} を当てた）"
    else:
        if steps[0]['runner'] == 'opencode':
            why += '（opencode の DeepSeek を先に試す）'
        if any(s['runner'] == 'codex' for s in steps):
            why += f'（{CODEX_UNTIL} までは codex を agy より先に試す）' if routing.get('name') == 'default' else ''
        if routing.get('name') != 'default':
            why += f"（テンプレート {routing.get('name', '?')}）"
    if skip:
        why += f"（{','.join(sorted(skip))} を外した）"
    reasons.insert(0, f'決定: {tier}。{why}')
    return {'tier': tier, 'route': steps, 'review': review, 'scores': scores, 'reasons': reasons,
            'template': {'name': tpl['name'], 'applied': tpl['applied'], 'note': tpl['note']}}


def label(step: dict) -> str:
    model = step['model'].split('/')[-1]
    return f"{step['runner']} {model}" + (f" {step['effort']}" if step['effort'] else '')


def run_all(base: Path, today: datetime.date, skip: frozenset, routing: dict | None = None,
            home: Path | None = None, now: datetime.datetime | None = None) -> None:
    print('| PBI | 段 | route の先頭 | review | 優先テンプレート | 理由 |')
    print('|---|---|---|---|---|---|')
    for fp in sorted(base.glob('PBI-*.facts.json')):
        pbi = fp.name.removesuffix('.facts.json')
        brief = base / 'jev' / f'{pbi}.brief.json'
        out = decide(json.load(open(fp, encoding='utf-8')), load_answers(brief if brief.exists() else None),
                     today, skip=skip, routing=routing, home=home, now=now, pbi=pbi_number(pbi))
        (base / f'{pbi}.route.json').write_text(json.dumps(out, ensure_ascii=False), encoding='utf-8')
        print(f"| {pbi} | {out['tier']} | {label(out['route'][0])} | {out['review']} | {out['template']['note']} | "
              f"{out['reasons'][0].removeprefix('決定: ')} |")


def pop_opt(args: list, name: str):
    if name not in args:
        return None
    i = args.index(name)
    value = args[i + 1]
    del args[i:i + 2]
    return value


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    args = sys.argv[1:]
    today_s = pop_opt(args, '--today')
    now_s = pop_opt(args, '--now')
    home_s = pop_opt(args, '--home')
    forced = pop_opt(args, '--tier')
    if forced and forced not in TIERS:
        sys.exit(f'--tier は {"/".join(TIERS)} のどれか')
    skip_s = pop_opt(args, '--skip')
    skip = frozenset(s for s in (skip_s or '').split(',') if s)
    if {s for s in skip if '/' not in s} - set(RUNNERS):
        sys.exit(f'--skip は {"/".join(RUNNERS)} のどれかかモデル ID（/ を含む値）（カンマ区切り）')
    pbi_s = pop_opt(args, '--pbi')
    if pbi_s is not None and not pbi_s.isdigit():
        sys.exit('--pbi は 0 以上の整数')
    try:
        now = templates.parse_time(now_s) if now_s else datetime.datetime.now().astimezone()
        if today_s:
            today = datetime.date.fromisoformat(today_s)
        else:
            today = now.date() if now_s else datetime.date.today()
    except ValueError as error:
        sys.exit(f'--today / --now が不正: {error}')
    home = templates.resolve_home(home_s)
    all_dir = pop_opt(args, '--all')
    routing, _ = find_routing(pop_opt(args, '--routing'), all_dir)
    if all_dir:
        run_all(Path(all_dir), today, skip, routing, home, now)
        return

    facts = json.load(open(args[0], encoding='utf-8'))
    answers = load_answers(args[1] if len(args) > 1 else None)
    pbi = int(pbi_s) if pbi_s is not None else pbi_number(Path(args[0]).name)
    print(json.dumps(decide(facts, answers, today, forced, skip, routing, home, now, pbi), ensure_ascii=False))


if __name__ == '__main__':
    main()
