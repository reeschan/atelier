"""担当を決める。

  python select_model.py <facts.json> [<brief の Jev 応答.json>] [--today YYYY-MM-DD] [--tier haiku|sonnet|opus] [--skip opencode,agy]
  python select_model.py --all docs/pbi [--today ...] [--skip ...]

facts.json: {"files_changed", "files_new", "files_deleted", "modules", "criteria", "sensitive"}
Jev 応答: ゲート brief（novelty / coupling / ambiguity）の API 応答。無い・エラーのときは各 score を 1 とする。
--tier: 決定表を飛ばして段を指定する（9.1 の昇格で route を出し直すとき）。
--skip: route から外す実行先（このセッションで上限・拒否が出た実行先、昇格で外す opencode）。agent は外せない。
--all: <dir>/PBI-*.facts.json と <dir>/jev/PBI-*.brief.json を全部判定し、<dir>/PBI-*.route.json に書いて、
       標準出力には 1 PBI 1 行の表だけを出す（オーケストレーターの文脈を細く保つため）。
標準出力（1 件のとき）: {"tier", "route": [{"runner", "model", "effort"}, ...], "review", "scores", "reasons"}

route は上から順に試す実行先。runner は opencode（opencode CLI の OpenCode Go）/ codex（Codex CLI）/ agy（Antigravity
CLI の Claude）/ agent（Claude Code の Agent ツール = Anthropic のサブスク）。
- opus の段は設計判断が要る PBI（novelty=2 かつ ambiguity=2）だけ。agent の opus で実装する。
- sensitive・coupling=2 は sonnet の段で実装し、review を "deep" にする（Opus のオーケストレーターが重点的に読む）。
  sensitive はリポジトリの外のサービスに出さないため、route を agent の sonnet だけにする。
- haiku・sonnet の段は opencode（DeepSeek）を先頭に置く。CODEX_UNTIL の日まで（その日を含む）は codex を agy の前に置く。
"""
import datetime
import json
import sys
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


def route(tier: str, today: datetime.date, sensitive: bool = False, skip: frozenset = frozenset()) -> list[dict]:
    steps = []
    if not sensitive:
        if tier in OPENCODE:
            steps.append(OPENCODE[tier])
        if tier in CODEX and today <= CODEX_UNTIL:
            steps.append(CODEX[tier])
        if tier in AGY:
            steps.append(AGY[tier])
    steps = [s for s in steps if s['runner'] not in skip]
    steps.append(AGENT[tier])
    return steps


def load_answers(path) -> dict:
    if not path:
        return {}
    try:
        return json.load(open(path, encoding='utf-8')).get('answers', {})
    except (OSError, ValueError):
        return {}


def decide(facts: dict, answers: dict, today: datetime.date, forced: str | None = None,
           skip: frozenset = frozenset()) -> dict:
    scores, reasons = {}, []
    for k in KEYS:
        scores[k], note = level(answers.get(k))
        reasons.append(f'{k}={scores[k]}（{note}）')
    if forced:
        tier, why = forced, f'--tier {forced} で指定'
    else:
        tier, why = claude_tier(facts, scores)
    sensitive = bool(facts.get('sensitive'))
    steps = route(tier, today, sensitive, skip)
    review = 'deep' if sensitive or scores['coupling'] == 2 else 'normal'
    if steps[0]['runner'] == 'opencode':
        why += '（opencode の DeepSeek を先に試す）'
    if any(s['runner'] == 'codex' for s in steps):
        why += f'（{CODEX_UNTIL} までは codex を agy より先に試す）'
    if skip:
        why += f"（{','.join(sorted(skip))} を外した）"
    reasons.insert(0, f'決定: {tier}。{why}')
    return {'tier': tier, 'route': steps, 'review': review, 'scores': scores, 'reasons': reasons}


def label(step: dict) -> str:
    model = step['model'].split('/')[-1]
    return f"{step['runner']} {model}" + (f" {step['effort']}" if step['effort'] else '')


def run_all(base: Path, today: datetime.date, skip: frozenset) -> None:
    print('| PBI | 段 | route の先頭 | review | 理由 |')
    print('|---|---|---|---|---|')
    for fp in sorted(base.glob('PBI-*.facts.json')):
        pbi = fp.name.removesuffix('.facts.json')
        brief = base / 'jev' / f'{pbi}.brief.json'
        out = decide(json.load(open(fp, encoding='utf-8')), load_answers(brief if brief.exists() else None),
                     today, skip=skip)
        (base / f'{pbi}.route.json').write_text(json.dumps(out, ensure_ascii=False), encoding='utf-8')
        print(f"| {pbi} | {out['tier']} | {label(out['route'][0])} | {out['review']} | "
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
    today = datetime.date.fromisoformat(today_s) if today_s else datetime.date.today()
    forced = pop_opt(args, '--tier')
    if forced and forced not in TIERS:
        sys.exit(f'--tier は {"/".join(TIERS)} のどれか')
    skip_s = pop_opt(args, '--skip')
    skip = frozenset(s for s in (skip_s or '').split(',') if s)
    if skip - set(RUNNERS):
        sys.exit(f'--skip は {"/".join(RUNNERS)} のどれか（カンマ区切り）')
    all_dir = pop_opt(args, '--all')
    if all_dir:
        run_all(Path(all_dir), today, skip)
        return

    facts = json.load(open(args[0], encoding='utf-8'))
    answers = load_answers(args[1] if len(args) > 1 else None)
    print(json.dumps(decide(facts, answers, today, forced, skip), ensure_ascii=False))


if __name__ == '__main__':
    main()
