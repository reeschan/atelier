"""kiln (窯) の区切りで文脈の大きさを線と比べ、決めた手立てを compact-event の 1 行として足す (appendix/kiln.md)。

  python kiln_log.py <pbi-dir> --role designer|implementer --boundary phase|wave|attempt --phase NAME
                     --tokens N [--action ACTION] [--state-path P] [--task-id ID] [--note TEXT]
                     [--host claude-code] [--regime EoCC] [--log FILE] [--run-id ID]

線は <pbi-dir>/kiln.json、無ければ同梱の workflows/jev-routed.json の kiln。"enabled" が false なら
`kiln off` を出して何も書かない。--action を省くと線から決める (handoff_at 以上 → handoff、peak 以上 → compact、
soft 以上 → suggest、設計者で aux_compress_at 以上 → aux_on、ほかは noop)。
run_id は <pbi-dir>/.kiln/run_id (無ければ作る)。--run-id を渡すと、その値をそこに書いて使う (resume で HANDOFF.md の
Run id: から戻すとき)。記録は --log、無ければ <pbi-dir>/.kiln/compact-events.jsonl。
1 行の項目: ts・run_id・host・regime・boundary・role・phase・tokens_before・peak_budget・soft_budget・handoff_at・
action・state_path・task_id・note (sac-strategic-compact の log_compact_event.py と同じ名前)。
標準出力は 1 行: `action <手立て> (role <役>, tokens <N>, soft <S>, peak <P>, handoff <H>) -> <記録のパス>`。
終了コード: 0 = 書いた (または kiln off)、2 = 引数・設定の誤り。
"""
import argparse
import datetime
import json
import sys
import uuid
from pathlib import Path

import common

HERE = Path(__file__).resolve().parent
ROLES = ('designer', 'implementer')
BOUNDARIES = ('phase', 'wave', 'attempt', 'milestone')
ACTIONS = ('noop', 'suggest', 'compact', 'handoff', 'aux_on', 'fork')
LINES = ('designer_soft', 'designer_peak', 'implementer_soft', 'implementer_peak', 'handoff_at', 'aux_compress_at')


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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('pbi_dir')
    ap.add_argument('--role', required=True, choices=ROLES)
    ap.add_argument('--boundary', required=True, choices=BOUNDARIES)
    ap.add_argument('--phase', required=True)
    ap.add_argument('--tokens', required=True, type=int)
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
    if args.tokens < 0:
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
    action = args.action or decide(args.role, args.tokens, kiln)
    event = {
        'ts': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
        'run_id': run_id(pbi, args.run_id),
        'host': args.host,
        'regime': args.regime,
        'boundary': args.boundary,
        'role': args.role,
        'phase': args.phase,
        'tokens_before': args.tokens,
        'peak_budget': kiln[f'{args.role}_peak'],
        'soft_budget': kiln[f'{args.role}_soft'],
        'handoff_at': kiln['handoff_at'],
        'action': action,
        'state_path': args.state_path,
        'task_id': args.task_id,
        'note': args.note,
    }
    log = Path(args.log) if args.log else pbi / '.kiln' / 'compact-events.jsonl'
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('a', encoding='utf-8', newline='\n') as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + '\n')
    print(f'action {action} (role {args.role}, tokens {args.tokens}, soft {event["soft_budget"]}, '
          f'peak {event["peak_budget"]}, handoff {event["handoff_at"]}) -> {common.shown(log)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
