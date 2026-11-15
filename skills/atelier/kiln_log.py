"""kiln (窯) の区切りで文脈の大きさを線と比べ、決めた手立てを compact-event の 1 行として足す (appendix/kiln.md)。

  python kiln_log.py <pbi-dir> --role designer|implementer --boundary start|phase|wave|attempt|milestone --phase NAME
                     (--tokens N | --probe-agent PREFIX | --probe-session ID) [--seq N] [--action ACTION]
                     [--state-path P] [--task-id ID] [--note TEXT]
                     [--host claude-code] [--regime EoCC] [--log FILE] [--run-id ID]
  python kiln_log.py tablet-check <path> [--pbi-dir DIR] [--max-chars N]

文脈の大きさは --tokens で渡すか、--probe-agent (description がその文字列で始まる最新のサブエージェント) か
--probe-session (そのセッションの会話記録) で、`agent_stats.py --ctx` を流して自分で測る。測れないとき
(agent_stats.py が context=unknown を出したとき) は、tokens_before を null、action を --action か noop にした行を書き、
`context=unknown -> <記録のパス>` を出して終了コード 3 で終わる (線と比べていないので events.jsonl には出さない)。
--seq は conductor の出番の番号。--boundary start は作業開始の文脈 (出番の読み込みを終えた時点) の 1 回の測り。

線は <pbi-dir>/kiln.json、無ければ同梱の workflows/jev-routed.json の kiln。"enabled" が false なら
`kiln off` を出して何も書かない。--action を省くと線から決める (handoff_at 以上 → handoff、peak 以上 → compact、
soft 以上 → suggest、設計者で aux_compress_at 以上 → aux_on、ほかは noop)。
run_id は <pbi-dir>/.kiln/run_id (無ければ作る)。--run-id を渡すと、その値をそこに書いて使う (resume で HANDOFF.md の
Run id: から戻すとき)。記録は --log、無ければ <pbi-dir>/.kiln/compact-events.jsonl。
1 行の項目: ts・run_id・host・regime・boundary・role・phase・tokens_before・peak_budget・soft_budget・handoff_at・
action・state_path・task_id・note (sac-strategic-compact の log_compact_event.py と同じ名前)。atelier_run_id は
<pbi-dir>/conductor/run.json の run_id (無ければ null)。seq は --seq (無ければ null)。tokens_source は given (--tokens)・
probe (測った)・unknown (測れなかった)。追記のあと、同じ判断を <pbi-dir>/events.jsonl の
context.compacted として出す (kiln off のときは出さない)。
標準出力は 1 行: `action <手立て> (role <役>, tokens <N>, soft <S>, peak <P>, handoff <H>) -> <記録のパス>`。
終了コード: 0 = 書いた (または kiln off)、2 = 引数・設定の誤り、3 = 測れなかった (行は書いた)。

tablet-check は状態の板の形を確かめる。見出しは任意の H1 のあと ## Objective・## Facts・## Changes・## Verification・
## Dead ends・## Remaining plan を 1 回ずつ (先頭に `1.` などの番号があってもよい。コードの囲みの中は数えない)、
ほかの ## は無いこと。文字数は上限以下 (--max-chars、無ければ <--pbi-dir か板のフォルダ>/kiln.json の tablet_max_chars、
無ければ同梱のテンプレートの kiln、どれも無ければ 6000)。行数の下限は見ない。
形どおりなら `ok (<文字数> chars, max <上限>)` を出して終了コード 0、外れていれば `problem: <理由>` を 1 つずつ出して 1。
conductor は同じ検査を tablet_problems(path, max_chars) で使う。
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import common

try:
    import events
except Exception:
    events = None

HERE = Path(__file__).resolve().parent
ROLES = ('designer', 'implementer')
BOUNDARIES = ('start', 'phase', 'wave', 'attempt', 'milestone')
ACTIONS = ('noop', 'suggest', 'compact', 'handoff', 'aux_on', 'fork')
LINES = ('designer_soft', 'designer_peak', 'implementer_soft', 'implementer_peak', 'handoff_at', 'aux_compress_at')
# 状態の板の見出し (## のあと、番号を除いた名前)。この順で雛形に並べる
TABLET_HEADINGS = ('Objective', 'Facts', 'Changes', 'Verification', 'Dead ends', 'Remaining plan')
# 状態の板の文字数の上限の既定。次の設計者が最初に丸ごと読むので、読み込みの重さを抑える
TABLET_MAX_CHARS = 6000
# 見出しの名前の前の番号 (`1.`・`2)`・`3`)
HEADING_NUMBER = re.compile(r'^\d+[.)]?\s*')
# 文脈を測れなかったときの終了コード (agent_stats.py --ctx の context=unknown と同じ)
CTX_UNKNOWN = 3
# agent_stats.py --ctx を待つ上限 (秒)。会話記録を読むだけなので、止まったときに区切りを止めない
PROBE_TIMEOUT = 120
# Windows で子のコンソールのウインドウを出さない。ほかの OS では 0
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def tablet_max_chars(pbi) -> int:
    """<pbi>/kiln.json の tablet_max_chars、無ければ同梱のテンプレートの kiln、どれも無ければ TABLET_MAX_CHARS。"""
    for source in (common.load_json(Path(pbi) / 'kiln.json'),
                   (common.load_json(HERE / 'workflows' / 'jev-routed.json', {}) or {}).get('kiln')):
        value = source.get('tablet_max_chars') if isinstance(source, dict) else None
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return TABLET_MAX_CHARS


def tablet_problems(path, max_chars: int = TABLET_MAX_CHARS) -> list[str]:
    """状態の板の形の外れ (無ければ空)。見出しと文字数を見る (モジュールの説明の tablet-check)。"""
    try:
        text = Path(path).read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return ['missing file']
    seen, unexpected, fenced = {}, [], False
    for line in text.splitlines():
        if line.lstrip().startswith(('```', '~~~')):
            fenced = not fenced
            continue
        if fenced or not line.startswith('## '):
            continue
        name = HEADING_NUMBER.sub('', line[3:].strip()).rstrip(':').strip()
        known = next((h for h in TABLET_HEADINGS if h.lower() == name.lower()), None)
        if known is None:
            unexpected.append(name)
        else:
            seen[known] = seen.get(known, 0) + 1
    problems = [f'missing ## {h}' for h in TABLET_HEADINGS if h not in seen]
    problems += [f'## {h} appears {n} times' for h, n in seen.items() if n > 1]
    problems += [f'unexpected ## {name}' for name in unexpected]
    if len(text) > max_chars:
        problems.append(f'{len(text)} chars > {max_chars}')
    return problems


def tablet_check(argv) -> int:
    ap = argparse.ArgumentParser(prog='kiln_log.py tablet-check', description='状態の板の形を確かめる')
    ap.add_argument('path')
    ap.add_argument('--pbi-dir', help='kiln.json を読むフォルダ (既定は板のフォルダ)')
    ap.add_argument('--max-chars', type=int, help='文字数の上限 (kiln.json の tablet_max_chars より先)')
    args = ap.parse_args(argv)
    common.utf8_stdout()
    if args.max_chars is not None and args.max_chars <= 0:
        print('--max-chars must be a positive integer', file=sys.stderr)
        return 2
    limit = args.max_chars or tablet_max_chars(Path(args.pbi_dir) if args.pbi_dir else Path(args.path).parent)
    problems = tablet_problems(args.path, limit)
    for problem in problems:
        print(f'problem: {problem}')
    if problems:
        return 1
    print(f'ok ({len(Path(args.path).read_text(encoding="utf-8"))} chars, max {limit})')
    return 0


def probe_tokens(agent: str | None, session: str | None) -> int | None:
    """agent_stats.py --ctx で測った文脈の大きさ。会話記録が無い・読めないときは None。"""
    command = [sys.executable, str(HERE / 'agent_stats.py'), '--ctx']
    command += ['--agent', agent] if agent is not None else ['--session', session]
    try:
        run = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace',
                             timeout=PROBE_TIMEOUT, creationflags=NO_WINDOW, env=dict(os.environ, PYTHONUTF8='1'))
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r'context=(\d+)', run.stdout)
    if run.returncode != 0 or match is None:
        return None
    return int(match.group(1))


def load_kiln(pbi: Path) -> dict:
    """run の写し、無ければ同梱のテンプレートの kiln。線の値が欠けていれば ValueError。"""
    bundled = (common.load_json(HERE / 'workflows' / 'jev-routed.json', {}) or {}).get('kiln', {})
    run = common.load_json(pbi / 'kiln.json')
    config = dict(bundled)
    if isinstance(run, dict):
        config.update(run)
    if config.get('enabled', True) is False:
        return {'enabled': False}
    for key in LINES:
        if not isinstance(config.get(key), int) or config[key] <= 0:
            raise ValueError(f'kiln: {key} must be a positive integer')
    return config


def decide(role: str, tokens: int, kiln: dict) -> str:
    if tokens >= kiln['handoff_at']:
        return 'handoff'
    if tokens >= kiln[f'{role}_peak']:
        return 'compact'
    if tokens >= kiln[f'{role}_soft']:
        return 'suggest'
    if role == 'designer' and tokens >= kiln['aux_compress_at']:
        return 'aux_on'
    return 'noop'


def run_id(pbi: Path, given: str | None = None) -> str:
    path = pbi / '.kiln' / 'run_id'
    if given:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'{given}\n', encoding='utf-8')
        return given
    if path.is_file() and path.read_text(encoding='utf-8').strip():
        return path.read_text(encoding='utf-8').strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    value = str(uuid.uuid4())
    path.write_text(value + '\n', encoding='utf-8')
    return value


def conductor_run_id(pbi: Path):
    """<pbi>/conductor/run.json の run_id。無い・読めない・型が違うなら None。"""
    raw = common.load_json(pbi / 'conductor' / 'run.json')
    value = raw.get('run_id') if isinstance(raw, dict) else None
    return value if isinstance(value, str) and value else None


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ['tablet-check']:
        return tablet_check(argv[1:])
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('pbi_dir')
    ap.add_argument('--role', required=True, choices=ROLES)
    ap.add_argument('--boundary', required=True, choices=BOUNDARIES)
    ap.add_argument('--phase', required=True)
    size = ap.add_mutually_exclusive_group(required=True)
    size.add_argument('--tokens', type=int)
    size.add_argument('--probe-agent', metavar='PREFIX', help='description がこの文字列で始まるサブエージェントを測る')
    size.add_argument('--probe-session', metavar='ID', help='このセッションの会話記録を測る (headless の設計者)')
    ap.add_argument('--seq', type=int, help='conductor の出番の番号')
    ap.add_argument('--action', choices=ACTIONS)
    ap.add_argument('--state-path', default='docs/pbi/CONTEXT_STATE.md')
    ap.add_argument('--task-id')
    ap.add_argument('--note')
    ap.add_argument('--host', default='claude-code')
    ap.add_argument('--regime', default='EoCC', choices=('EoCC', 'SAC'))
    ap.add_argument('--log')
    ap.add_argument('--run-id', help='resume で HANDOFF.md の Run id: から戻すときの run_id')
    args = ap.parse_args(argv)
    common.utf8_stdout()
    pbi = Path(args.pbi_dir)
    if not pbi.is_dir():
        print(f'{args.pbi_dir}: not a directory', file=sys.stderr)
        return 2
    if args.tokens is not None and args.tokens < 0:
        print('--tokens must not be negative', file=sys.stderr)
        return 2
    try:
        kiln = load_kiln(pbi)
    except ValueError as error:
        print(f'error {error}', file=sys.stderr)
        return 2
    if kiln.get('enabled') is False:
        print('kiln off')
        return 0
    tokens, source = args.tokens, 'given'
    if tokens is None:
        tokens = probe_tokens(args.probe_agent, args.probe_session)
        source = 'probe' if tokens is not None else 'unknown'
    action = args.action or ('noop' if tokens is None else decide(args.role, tokens, kiln))
    event = {
        'ts': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
        'run_id': run_id(pbi, args.run_id),
        'host': args.host,
        'regime': args.regime,
        'boundary': args.boundary,
        'role': args.role,
        'phase': args.phase,
        'tokens_before': tokens,
        'peak_budget': kiln[f'{args.role}_peak'],
        'soft_budget': kiln[f'{args.role}_soft'],
        'handoff_at': kiln['handoff_at'],
        'action': action,
        'state_path': args.state_path,
        'task_id': args.task_id,
        'note': args.note,
        'atelier_run_id': conductor_run_id(pbi),
        'seq': args.seq,
        'tokens_source': source,
    }
    log = Path(args.log) if args.log else pbi / '.kiln' / 'compact-events.jsonl'
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('a', encoding='utf-8', newline='\n') as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + '\n')
    if tokens is None:
        print(f'context=unknown (role {args.role}, action {action}) -> {common.shown(log)}')
        return CTX_UNKNOWN
    if events is not None:
        events.emit(pbi, 'context.compacted', {
            'boundary': args.boundary,
            'role': args.role,
            'phase': args.phase,
            'regime': args.regime,
            'tokens_before': tokens,
            'action': action,
        })
    print(f'action {action} (role {args.role}, tokens {tokens}, soft {event["soft_budget"]}, '
          f'peak {event["peak_budget"]}, handoff {event["handoff_at"]}) -> {common.shown(log)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
