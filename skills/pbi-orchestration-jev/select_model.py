"""担当を決める: python select_model.py <facts.json> [<brief の Jev 応答.json>] [--today YYYY-MM-DD] [--tier haiku|sonnet|opus]

facts.json: {"files_changed", "files_new", "files_deleted", "modules", "criteria", "sensitive"}
Jev 応答: ゲート brief（novelty / coupling / ambiguity）の API 応答。無い・エラーのときは各 score を 1 とする。
--tier: 決定表を飛ばして段を指定する（9.1 の昇格で route を出し直すとき）。
標準出力: {"tier": "haiku|sonnet|opus", "route": [{"runner", "model", "effort"}, ...], "scores": {...}, "reasons": [...]}

route は上から順に試す実行先。runner は codex（Codex CLI）/ agy（Antigravity CLI の Claude）/ agent（Claude Code の
Agent ツール = Anthropic のサブスク）。CODEX_UNTIL の日まで（その日を含む）は haiku / sonnet の段の先頭に codex を置く。
"""
import datetime
import json
import sys

LOW_CONFIDENCE = 0.7
KEYS = ('novelty', 'coupling', 'ambiguity')
TIERS = ('haiku', 'sonnet', 'opus')
CODEX_UNTIL = datetime.date(2026, 10, 23)
CODEX_MODEL = 'gpt-6-astra'

# 段ごとの実行先。Codex の effort は high を上限とし、haiku 相当は medium に下げる。
# Antigravity には haiku が無いので、haiku 相当は Sonnet の low にする。
CODEX = {
    'haiku': {'runner': 'codex', 'model': CODEX_MODEL, 'effort': 'medium'},
    'sonnet': {'runner': 'codex', 'model': CODEX_MODEL, 'effort': 'high'},
}
AGY = {
    'haiku': {'runner': 'agy', 'model': 'claude-sonnet-5-5-low', 'effort': None},
    'sonnet': {'runner': 'agy', 'model': 'claude-sonnet-5-5-high', 'effort': None},
    'opus': {'runner': 'agy', 'model': 'claude-opus-5-5-high', 'effort': None},
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
    if facts.get('sensitive'):
        return 'opus', 'sensitive'
    if scores['coupling'] == 2:
        return 'opus', 'coupling=2'
    if scores['novelty'] == 2 and scores['ambiguity'] == 2:
        return 'opus', 'novelty=2 かつ ambiguity=2'
    if any(scores[k] >= 1 for k in KEYS):
        return 'sonnet', 'novelty・coupling・ambiguity のどれかが 1 以上'
    if facts.get('files_changed', 0) >= 4:
        return 'sonnet', f"files_changed={facts['files_changed']} >= 4"
    if facts.get('modules', 0) >= 2:
        return 'sonnet', f"modules={facts['modules']} >= 2"
    return 'haiku', 'どの条件にも当たらない'


def route(tier: str, today: datetime.date) -> list[dict]:
    steps = []
    if tier in CODEX and today <= CODEX_UNTIL:
        steps.append(CODEX[tier])
    steps += [AGY[tier], AGENT[tier]]
    return steps


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    args = sys.argv[1:]
    today = datetime.date.today()
    if '--today' in args:
        i = args.index('--today')
        today = datetime.date.fromisoformat(args[i + 1])
        del args[i:i + 2]
    forced = None
    if '--tier' in args:
        i = args.index('--tier')
        forced = args[i + 1]
        del args[i:i + 2]
        if forced not in TIERS:
            sys.exit(f'--tier は {"/".join(TIERS)} のどれか')

    facts = json.load(open(args[0], encoding='utf-8'))
    answers = {}
    if len(args) > 1:
        try:
            answers = json.load(open(args[1], encoding='utf-8')).get('answers', {})
        except (OSError, ValueError):
            answers = {}

    scores, reasons = {}, []
    for k in KEYS:
        scores[k], note = level(answers.get(k))
        reasons.append(f'{k}={scores[k]}（{note}）')

    if forced:
        tier, why = forced, f'--tier {forced} で指定'
    else:
        tier, why = claude_tier(facts, scores)
    steps = route(tier, today)
    if steps[0]['runner'] == 'codex':
        why += f'（{CODEX_UNTIL} までは codex を先に試す）'
    reasons.insert(0, f'決定: {tier}。{why}')
    print(json.dumps({'tier': tier, 'route': steps, 'scores': scores, 'reasons': reasons},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
