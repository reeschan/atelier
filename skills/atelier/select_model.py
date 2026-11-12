"""担当を決める。

  python select_model.py <facts.json> [<brief の Jev 応答.json>] [--today YYYY-MM-DD] [--tier haiku|sonnet|opus] [--skip opencode,agy,<model>] [--pbi N] [--routing <routing.json>] [--home DIR] [--now ISO]
  python select_model.py --all docs/pbi [--only PBI-003,PBI-004 [--tier haiku|sonnet|opus]] [--today ...] [--skip ...] [--routing ...] [--home ...] [--now ...]
  python select_model.py --override PBI-003 --route <runner>/<model>[@<effort>][,...] [--why TEXT] [--dir docs/pbi] [--tier ...]

facts.json: {"files_changed", "files_new", "files_deleted", "modules", "criteria", "sensitive"}
Jev 応答: ゲート brief（novelty / coupling / ambiguity）の API 応答。
--tier: 決定表を飛ばして段を指定する（tier up で route を出し直すとき）。--all では --only と一緒のときだけ受け付ける。
--skip: route から外す実行先（このセッションで上限・拒否が出た実行先、昇格で外す opencode）。agent は外せない。
        runner 名（opencode など）はその runner の手順を全部、モデル ID はその model の手順だけを外す。モデル ID は
        / を含む値（opencode/glm-5.3-flash-free など）か、routing・適用中の優先テンプレートの手順に出てくる値
        （codex の gpt-6-astra など）。どれでもない値は打ち間違いとして止める。
--pbi: 交互グループを回す数（PBI の番号）。省くと facts のファイル名の PBI-<番号> から取り、取れなければ 0。
--all: <dir>/PBI-*.facts.json と <dir>/jev/PBI-*.brief.json を全部判定し、<dir>/PBI-*.route.json に書いて、
       標準出力には 1 PBI 1 行の表だけを出す（オーケストレーターの文脈を細く保つため）。
       --only を付けると、その PBI だけを判定し直して route.json を書き直す（ほかの PBI の route.json は触らない）。
       --tier は --only と一緒のときだけ受け付け、指定した PBI の段を決定表ではなくその段にする。
       --all で --only を付けずに --tier を付けたときは、エラーで終了する。
       route.json に override (goal などで指定して固定した route) があれば書き直さず、表の後ろに
       「keep PBI-n: override」を出す。
--override: <dir>/PBI-n.route.json の route を --route の手順に差し替え、override {"route", "why"} を足して固定する
       （goal で worker を指定したとき）。--route は <runner>/<model>[@<effort>] のカンマ区切りで、model は / を含んでよい。
       --why は既定 goal。段・review・scores は今の route.json から（無ければ決定表で判定して）引き継ぐ。--dir は既定 docs/pbi。
--routing: 段ごとの route を書いたテンプレート（routing.default.json と同じ形。studio/ の画面で作れる。studio/serve.py なら画面から直接置ける）。
       指定が無ければ、--all のときは <dir>/routing.json、次に home（--home と同じ解決順。templates.resolve_home）の
       routing.json を探し、どちらも無ければ同梱の routing.default.json を読む。全リポジトリで共通の表は home に置く
       （版管理の配置ではスキルのルートなので、版を切り替えても引き継がれる）。
--home: 優先テンプレートと共通の routing.json を置く home（templates.resolve_home）。--now: 今の時刻（templates.parse_time）。
        --today を省いて --now があれば、今日は --now の日付。--all でも同じ引数が効く。
標準出力（1 件のとき）: {"tier", "route": [{"runner", "model", "effort"}, ...],
"dropped": [{"runner", "model", "effort", "reason"}, ...], "review", "scores", "reasons",
"template": {"name", "applied", "note"}}

route を組んだあと（振り分けテンプレートからでも、優先テンプレートからでも）、agents.json と CLI の有無で
各手順を照合する（agents.filter_steps）。使えない手順は dropped に理由つきで並べる。--skip で外した手順と
until を過ぎた手順も dropped に入れる。理由は agents.py の 3 つ（未使用 (agents.json)・見つからない・
未選択のモデル）と、--skip で外した・until を過ぎた。route の最後の agent の手順は外さない。
agents.json は 1 回だけ読み、CLI の場所は runner ごとに 1 回だけ求める（--all でも同じ）。

優先テンプレート（期限付きの上書き。appendix/priority-templates.md）は振り分けテンプレート（routing）より先に当たる。
写し (overrides.json) の適用が active で、優先テンプレートにその段の上書きがあり、sensitive でなければ、
route を「優先テンプレートの手順（--skip の runner・model と until を過ぎた手順は外す）＋ routing のその段の agent」に置き換える。
優先テンプレートが opus の段に当たったときは、template の note の後ろに opus の段を上書きした旨の注意を足す。
優先テンプレートの手順には交互グループ {"rotate": [手順, ...]} を書ける。PBI の番号 mod メンバー数 番目を先頭にして
元の並びを回し、残りはその PBI の控えとして後ろに続ける（expand_rotate）。展開してから skip と until で絞る。

route は上から順に試す実行先。runner は opencode（opencode CLI の OpenCode Go）/ codex（Codex CLI）/ agy（Antigravity
CLI の Claude）/ dsh（DeepSeek Harness の headless。dsh_run.py。優先テンプレートからだけ使う）/ agent（Claude Code の Agent ツール = Anthropic のサブスク）。
既定の振り分けは同梱の routing.default.json が正本で、全段 agent の 1 手順だけ。コードの中に表は持たない。
外部の実行先を使うときは自分の routing.json（docs/pbi か home）に書く。見本は examples/routing/multi-runner.json。
- Jev の応答に novelty・coupling・ambiguity のどれも無いときは facts だけで決める。sensitive → sonnet（review は deep、
  route は agent だけ）、files_changed >= 4 か modules >= 2 → sonnet、それ以外 → haiku。opus にはしない
  （設計判断は facts では分からない。必要なら設計者が段を指定する）。理由の先頭は「Jev なし: facts だけで判定」。
  一部のキーだけ欠けるときは、欠けたキーを 1 とする。
- Jev の応答があるとき: novelty=2 かつ ambiguity=2 の段を opus にする（agent の opus で実装する）。
- sensitive・coupling=2 は sonnet の段で実装し、review を "deep" にする（Opus のオーケストレーターが重点的に読む）。
  sensitive はリポジトリの外のサービスに出さないため、route を agent の sonnet だけにする。
"""
import datetime
import json
import re
import sys
import agents as agents_module
import common
import templates
from pathlib import Path

LOW_CONFIDENCE = 0.7
KEYS = ('novelty', 'coupling', 'ambiguity')
TIERS = common.TIERS
RUNNERS = common.EXTERNAL_RUNNERS
# 手順を外す理由のうち、agents.py が持たないもの (route.json の dropped の reason に書く)
REASON_SKIP = '--skip で外した'
REASON_UNTIL = 'until を過ぎた'
# Jev が無いときの facts のしきい値。設計判断は facts では分からないので、ここから上は sonnet にする (opus にはしない)。
MANY_FILES = 4
MANY_MODULES = 2
DEFAULT_ROUTING = Path(__file__).resolve().parent / 'routing.default.json'


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


def facts_tier(facts: dict) -> tuple[str, str]:
    """Jev が無いときに facts だけで段を決める。sensitive を先に見て、opus にはしない。"""
    if facts.get('sensitive'):
        return 'sonnet', 'sensitive（Agent の sonnet だけ・重点レビュー）'
    if facts.get('files_changed', 0) >= MANY_FILES:
        return 'sonnet', f"files_changed={facts['files_changed']} >= {MANY_FILES}"
    if facts.get('modules', 0) >= MANY_MODULES:
        return 'sonnet', f"modules={facts['modules']} >= {MANY_MODULES}"
    return 'haiku', (f"files_changed={facts.get('files_changed', 0)} < {MANY_FILES} かつ "
                     f"modules={facts.get('modules', 0)} < {MANY_MODULES}")


def default_routing() -> dict:
    """同梱の routing.default.json を読んで返す。壊れていたら終了する。"""
    try:
        table = json.loads(DEFAULT_ROUTING.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        sys.exit(f'{DEFAULT_ROUTING}: {error}')
    err = routing_error(table)
    if err:
        sys.exit(f'{DEFAULT_ROUTING}: {err}')
    return table


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


def known_models(routing: dict, home: Path) -> set:
    """routing と適用中の優先テンプレートの手順に出てくるモデル ID（agent の手順は除く）。--skip の確かめに使う。"""
    found = set()

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get('model'), str) and node.get('runner') != 'agent':
                found.add(node['model'])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(routing)
    application = templates.load_overrides(home).get('application')
    name = application.get('template') if isinstance(application, dict) else None
    if isinstance(name, str):
        walk(templates.load_template(home, name))
    return found


def find_routing(explicit: str | None, all_dir: str | None, home: Path) -> tuple[dict, str]:
    """--routing → <all_dir>/routing.json → <home>/routing.json → 同梱の routing.default.json。home は優先テンプレートと同じ置き場。"""
    candidates = [Path(explicit)] if explicit else []
    if not explicit:
        if all_dir:
            candidates.append(Path(all_dir) / 'routing.json')
        candidates.append(Path(home) / 'routing.json')
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


def assemble(steps: list, today: datetime.date, skip: frozenset) -> tuple[list, list]:
    """--skip の runner・model と until を過ぎた手順を、理由つきで外して (残り, 外した手順) を返す。"""
    kept, dropped = [], []
    for s in steps:
        if skipped(s, skip):
            dropped.append({**strip(s), 'reason': REASON_SKIP})
        elif s.get('until') and today > datetime.date.fromisoformat(s['until']):
            dropped.append({**strip(s), 'reason': REASON_UNTIL})
        else:
            kept.append(strip(s))
    return kept, dropped


def route(tier: str, today: datetime.date, sensitive: bool = False, skip: frozenset = frozenset(),
          routing: dict | None = None) -> tuple[list, list]:
    """振り分けテンプレートから (route, 外した手順) を組む。

    sensitive はリポジトリの外のサービスに出さないため、その段の agent だけにする。
    --skip と until を過ぎた手順は理由つきで外す（最後の agent の手順は外さない）。
    """
    steps = (routing or default_routing())['tiers'][tier]
    if sensitive:
        return [strip(steps[-1])], []
    kept, dropped = assemble(steps[:-1], today, skip)
    kept.append(strip(steps[-1]))
    return kept, dropped


def strip(step: dict) -> dict:
    return {'runner': step['runner'], 'model': step['model'], 'effort': step.get('effort')}


def load_agent_table(home: Path) -> dict | None:
    """home の agents.json を読む (agents.load_agents)。"""
    return agents_module.load_agents(home)


def find_found(agents: dict | None) -> dict:
    """runner ごとの CLI の場所を 1 回ずつ求めて返す。"""
    return {runner: agents_module.find_cli(runner, agents) for runner in RUNNERS}


def apply_template(tier: str, sensitive: bool, skip: frozenset, today: datetime.date, routing: dict,
                   home: Path, now: datetime.datetime, pbi: int = 0) -> dict:
    """写しの適用から route（当てないときは None）と template の欄を組む。

    当てる条件は、適用の状態が active・優先テンプレートが読める・その段がある・sensitive でない、の全部。
    route は優先テンプレートの手順（交互グループは pbi で回して展開し、--skip の runner・model と
    until を過ぎた手順は外す）のあとに、routing のその段の agent（最後の手順）を付けたもの。
    opus の段に当てたときは、note の後ろに opus の段を上書きした旨の注意を足す。
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
    steps, dropped = assemble(expand_rotate(tier_steps, pbi), today, skip)
    if not steps or steps[-1] != fallback:
        steps = steps + [fallback]
    until = application.get('until')
    note = f'{name} を期限なしで当てた' if until is None else f'{name} を当てた ({until} まで)'
    if tier == 'opus':
        note += '（opus の段を上書き）'
    return {'name': name, 'applied': True, 'note': note, 'route': steps, 'dropped': dropped}


def load_answers(path) -> dict:
    if not path:
        return {}
    try:
        return json.load(open(path, encoding='utf-8')).get('answers', {})
    except (OSError, ValueError):
        return {}


def decide(facts: dict, answers: dict, today: datetime.date, forced: str | None = None,
           skip: frozenset = frozenset(), routing: dict | None = None, home: Path | None = None,
           now: datetime.datetime | None = None, pbi: int = 0, agents: dict | None = None,
           found: dict | None = None) -> dict:
    """1 件の PBI の段と route を決める。

    agents は agents.json の中身、found は runner → CLI の場所。省くと home と環境から求める。
    route を組んだあと agents.filter_steps で照合し、外した手順を dropped に並べる。
    """
    no_jev = not any(k in answers for k in KEYS)
    scores, reasons = {}, []
    for k in KEYS:
        scores[k], note = level(answers.get(k))
        reasons.append(f'{k}={scores[k]}（{note}）')
    if forced:
        tier, why = forced, f'--tier {forced} で指定'
        head = f'決定: {tier}。'
    elif no_jev:
        tier, why = facts_tier(facts)
        head = f'Jev なし: facts だけで判定。決定: {tier}。'
    else:
        tier, why = claude_tier(facts, scores)
        head = f'決定: {tier}。'
    sensitive = bool(facts.get('sensitive'))
    routing = routing or default_routing()
    if now is None:
        now = datetime.datetime.now().astimezone()
    if home is None:
        home = templates.resolve_home(None)
    if agents is None:
        agents = load_agent_table(home)
    if found is None:
        found = find_found(agents)
    tpl = apply_template(tier, sensitive, skip, today, routing, home, now, pbi)
    if tpl['applied']:
        steps, dropped = tpl['route'], tpl['dropped']
    else:
        steps, dropped = route(tier, today, sensitive, skip, routing)
    steps, extra = agents_module.filter_steps(steps, agents, found)
    dropped = dropped + extra
    review = 'deep' if sensitive or scores['coupling'] == 2 else 'normal'
    if tpl['applied']:
        why += f"（優先テンプレート {tpl['name']} を当てた）"
    elif routing.get('name') != 'default':
        why += f"（テンプレート {routing.get('name', '?')}）"
    if skip:
        why += f"（{','.join(sorted(skip))} を外した）"
    reasons.insert(0, head + why)
    return {'tier': tier, 'route': steps, 'dropped': dropped, 'review': review, 'scores': scores,
            'reasons': reasons, 'template': {'name': tpl['name'], 'applied': tpl['applied'],
                                             'note': tpl['note']}}


def label(step: dict) -> str:
    return common.step_label(step)


def run_all(base: Path, today: datetime.date, skip: frozenset, routing: dict | None = None,
            home: Path | None = None, now: datetime.datetime | None = None,
            only: list | None = None, tier: str | None = None) -> None:
    """<base> の PBI を判定して route.json に書く。

    only があれば、その PBI だけを判定し直す（ほかの route.json は触らない）。tier は only と
    一緒のときだけ使い、その段を決定表ではなく指定の段にする。agents.json は 1 回だけ読み、
    CLI の場所も runner ごとに 1 回だけ求める。
    """
    if home is None:
        home = templates.resolve_home(None)
    names = set(only or ())
    for name in sorted(names):
        if not (base / f'{name}.facts.json').is_file():
            sys.exit(f'--only の PBI の facts が無い: {name}')
    agents = load_agent_table(home) or {}
    found = find_found(agents)
    print('| PBI | 段 | route の先頭 | review | 優先テンプレート | 外した手順 | 理由 |')
    print('|---|---|---|---|---|---|---|')
    kept = []
    for fp in sorted(base.glob('PBI-*.facts.json')):
        pbi = fp.name.removesuffix('.facts.json')
        if names and pbi not in names:
            continue
        if pinned(base / f'{pbi}.route.json'):
            kept.append(pbi)
            continue
        brief = base / 'jev' / f'{pbi}.brief.json'
        out = decide(common.load_json(fp), load_answers(brief if brief.exists() else None),
                     today, forced=tier if names else None, skip=skip, routing=routing, home=home, now=now,
                     pbi=pbi_number(pbi), agents=agents, found=found)
        (base / f'{pbi}.route.json').write_text(json.dumps(out, ensure_ascii=False), encoding='utf-8')
        print(f"| {pbi} | {out['tier']} | {label(out['route'][0])} | {out['review']} | {out['template']['note']} | "
              f"{len(out['dropped'])} | {out['reasons'][0].removeprefix('決定: ')} |")
    for pbi in kept:
        print(f'keep {pbi}: override')


def pinned(path: Path) -> bool:
    """route.json に override (指定で固定した route) があるか。あれば --all は書き直さない。"""
    data = common.load_json(path) if path.is_file() else None
    return isinstance(data, dict) and isinstance(data.get('override'), dict)


def parse_route(text: str) -> list[dict]:
    """--route の値 (<runner>/<model>[@<effort>] のカンマ区切り) を route の手順にする。model は / を含んでよい。"""
    steps = []
    for item in (x.strip() for x in text.split(',')):
        runner, _, rest = item.partition('/')
        model, _, effort = rest.partition('@')
        if runner not in common.RUNNERS or not model:
            sys.exit(f'--route は <runner>/<model>[@<effort>] のカンマ区切り (runner は {"/".join(common.RUNNERS)}): {item}')
        steps.append({'runner': runner, 'model': model, 'effort': effort or None})
    return steps


def write_override(base: Path, pbi: str, steps: list, why: str, today: datetime.date, forced: str | None,
                   routing: dict, home: Path, now: datetime.datetime) -> dict:
    """PBI の route.json に、指定した route を override として書く。

    段・review・scores は今の route.json から (無ければ決定表で判定して) 引き継ぎ、route だけを差し替える。
    --tier があれば段をそれにする。
    """
    path = base / f'{pbi}.route.json'
    facts = common.load_json(base / f'{pbi}.facts.json', {})
    # sensitive はリポジトリの外のサービスに出さない (route と同じ決まり): override でも agent だけ
    if isinstance(facts, dict) and facts.get('sensitive') and any(step.get('runner') != 'agent' for step in steps):
        sys.exit(f'--override: {pbi} は sensitive なので agent の route だけにできる')
    out = common.load_json(path) if path.is_file() else None
    if not isinstance(out, dict) or not isinstance(out.get('tier'), str):
        facts_path = base / f'{pbi}.facts.json'
        if not facts_path.is_file():
            sys.exit(f'--override の PBI の route.json も facts も無い: {pbi}')
        brief = base / 'jev' / f'{pbi}.brief.json'
        out = decide(common.load_json(facts_path), load_answers(brief if brief.exists() else None), today,
                     forced, routing=routing, home=home, now=now, pbi=pbi_number(pbi))
    if forced:
        out['tier'] = forced
    reasons = [r for r in out.get('reasons') or [] if not str(r).startswith('決定: override')]
    out.update({'route': steps, 'dropped': [], 'override': {'route': steps, 'why': why},
                'reasons': [f'決定: override（{why}）', *reasons]})
    path.write_text(json.dumps(out, ensure_ascii=False), encoding='utf-8')
    return out


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
    override = pop_opt(args, '--override')
    if override:
        route_s = pop_opt(args, '--route')
        if not route_s:
            sys.exit('--override は --route と一緒に使う')
        why = pop_opt(args, '--why') or 'goal'
        base = Path(pop_opt(args, '--dir') or 'docs/pbi')
        routing, _ = find_routing(pop_opt(args, '--routing'), str(base), home)
        out = write_override(base, override, parse_route(route_s), why, today, forced, routing, home, now)
        print(f"override {override}: {' / '.join(label(s) for s in out['route'])} ({why})")
        return
    all_dir = pop_opt(args, '--all')
    only_s = pop_opt(args, '--only')
    only = [s for s in (only_s or '').split(',') if s]
    if only and not all_dir:
        sys.exit('--only は --all と一緒に使う')
    if all_dir and forced and not only:
        sys.exit('--tier は --all では --only と一緒に使う')
    routing, _ = find_routing(pop_opt(args, '--routing'), all_dir, home)
    unknown = {s for s in skip if s not in RUNNERS and '/' not in s and s not in known_models(routing, home)}
    if unknown:
        sys.exit(f'--skip は {"/".join(RUNNERS)} のどれかかモデル ID（カンマ区切り。agent は外せない）: '
                 f'{",".join(sorted(unknown))}')
    if all_dir:
        run_all(Path(all_dir), today, skip, routing, home, now, only=only, tier=forced)
        return

    facts = json.load(open(args[0], encoding='utf-8'))
    answers = load_answers(args[1] if len(args) > 1 else None)
    pbi = int(pbi_s) if pbi_s is not None else pbi_number(Path(args[0]).name)
    print(json.dumps(decide(facts, answers, today, forced, skip, routing, home, now, pbi), ensure_ascii=False))


if __name__ == '__main__':
    main()
