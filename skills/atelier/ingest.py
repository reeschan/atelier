"""外の集計 (harness2-analyzer) が返す判定を取り込み、停滞を判定する。

`round.judged`・`triage.labeled`・`cluster.assigned` の 3 つだけを、`harness.schema.json` の
許可リストで点検してから `events.emit` に渡す。type が 3 つのどれでもない行、data が object でない行、
JSON でない行、必須の項目 (`round.judged.verdict`・`triage.labeled.root_cause`・
`cluster.assigned.cluster_id`) が許可リストを通らない行は、行ごと書かずに落とす。
落とした数だけを標準エラーに 1 行で出す (本文は出さない)。

停滞は `<dir>/events.jsonl` の `round.judged` を書かれた順に読み、末尾から accept 以外が
k 件 (既定 3) 続いたら True。契約は docs/specs/harness-metrics.md の追記 §3.6・§3.7。

同じ行 (type と data が同じ) は once の鍵 (`type:sha256(data)`) で 1 度しか書かない。同じ
ファイルを 2 度取り込んでも `round.judged` は増えないので、`stalled` が誤って立たない。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

try:
    import events
except (Exception, SystemExit):  # 同じフォルダに無いときは点検できない
    events = None

# 取り込む type と、行ごと落とすかを見る必須の項目。
TYPES = ('round.judged', 'triage.labeled', 'cluster.assigned')
REQUIRED = {
    'round.judged': 'verdict',
    'triage.labeled': 'root_cause',
    'cluster.assigned': 'cluster_id',
}
EVENT_LINE_SCHEMA = 'atelier.event/1'
DEFAULT_K = 3


def _schema_path() -> Path:
    here = Path(__file__).resolve().parent
    return Path(getattr(events, 'SCHEMA_PATH', here / 'harness.schema.json'))


def _load_schema() -> dict:
    """点検に使う許可リスト。読めなければ空 (何も通さない)。"""
    try:
        raw = json.loads(_schema_path().read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _passes_required(schema: dict, type_name: str, data: dict) -> bool:
    """必須の項目が許可リストを通るか。通らない・欠ける・null なら行ごと落とす。"""
    if events is None:
        return False
    try:
        clean, _dropped = events.check(schema, type_name, data)
    except (Exception, SystemExit):
        return False
    return clean.get(REQUIRED[type_name]) is not None


def _envelope(type_name: str, data: dict) -> dict:
    """cluster.assigned の unit_id は外枠にも出す (§3.1 の結び付けキー)。"""
    if type_name != 'cluster.assigned':
        return {}
    unit_id = data.get('unit_id')
    return {'unit_id': unit_id} if isinstance(unit_id, str) and unit_id else {}


def _row_key(type_name: str, data: dict) -> str:
    """同じ行を 2 度取り込まないための once の鍵。type と data の中身だけで決まる。"""
    text = json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return f'{type_name}:' + hashlib.sha256(text.encode('utf-8')).hexdigest()


def _seen_keys(base: Path) -> set:
    """`<dir>/.events/seen.txt` に残っている once の鍵 (events.emit の once と同じ置き場所)。"""
    try:
        return set((Path(base) / '.events' / 'seen.txt').read_text(encoding='utf-8').splitlines())
    except OSError:
        return set()


def ingest(dir, path) -> tuple[int, int]:
    """JSONL を 1 行ずつ取り込み、(書いた数, 落とした数) を返す。例外を投げず、stdout に出さない。"""
    base = Path(dir) if dir is not None else None
    source = Path(path) if path is not None else None
    written = 0
    dropped = 0
    if base is None or source is None or events is None:
        return 0, 0
    schema = _load_schema()
    seen = _seen_keys(base)
    try:
        handle = open(source, encoding='utf-8', errors='replace')
    except OSError:
        return 0, 0
    with handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except (ValueError, TypeError):
                dropped += 1
                continue
            if not isinstance(row, dict):
                dropped += 1
                continue
            type_name = row.get('type')
            data = row.get('data')
            if type_name not in TYPES or not isinstance(data, dict):
                dropped += 1
                continue
            if not _passes_required(schema, type_name, data):
                dropped += 1
                continue
            key = _row_key(type_name, data)
            if key in seen:
                continue  # 取り込み済みの行 (2 度目は何も足さない)
            try:
                ok = events.emit(base, type_name, data, stage=None, pbi=None, once=key,
                                 envelope=_envelope(type_name, data))
            except (Exception, SystemExit):
                ok = False
            if ok:
                written += 1
                seen.add(key)
            else:
                dropped += 1
    if dropped:
        print(f'ingest: dropped {dropped}', file=sys.stderr)
    return written, dropped


def stalled(dir, k: int = DEFAULT_K) -> bool:
    """events.jsonl の末尾で、accept 以外の round.judged が k 件続いているか。"""
    base = Path(dir) if dir is not None else None
    if base is None:
        return False
    k = k if isinstance(k, int) and not isinstance(k, bool) and k > 0 else DEFAULT_K
    verdicts = []
    try:
        handle = open(base / 'events.jsonl', encoding='utf-8', errors='replace')
    except OSError:
        return False
    with handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except (ValueError, TypeError):
                continue
            if not isinstance(row, dict) or row.get('type') != 'round.judged':
                continue
            data = row.get('data')
            verdict = data.get('verdict') if isinstance(data, dict) else None
            if isinstance(verdict, str):
                verdicts.append(verdict)
    streak = 0
    for verdict in reversed(verdicts):
        if verdict == 'accept':
            break
        streak += 1
    return streak >= k


# ---------- CLI ----------

def _usage(message: str) -> int:
    print('ingest: ' + message, file=sys.stderr)
    return 2


def _parse(parser: argparse.ArgumentParser, argv):
    try:
        return parser.parse_args(argv), None
    except SystemExit as error:
        return None, error.code if isinstance(error.code, int) else 2


def _cmd_ingest(argv) -> int:
    parser = argparse.ArgumentParser(prog='ingest.py ingest',
                                     description='analyzer の判定の JSONL を取り込む')
    parser.add_argument('--dir', default='docs/pbi')
    parser.add_argument('--file', required=True)
    args, code = _parse(parser, argv)
    if args is None:
        return code if code is not None else 2
    path = Path(args.file)
    if not path.is_file():
        print('ingest: no such file', file=sys.stderr)
        return 2
    ingest(args.dir, path)
    return 0


def _cmd_stalled(argv) -> int:
    parser = argparse.ArgumentParser(prog='ingest.py stalled',
                                     description='accept 以外の判定が続いているかを出す')
    parser.add_argument('--dir', default='docs/pbi')
    parser.add_argument('--k', type=int, default=DEFAULT_K)
    args, code = _parse(parser, argv)
    if args is None:
        return code if code is not None else 2
    print('stalled' if stalled(args.dir, args.k) else 'ok')
    return 0


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return _usage('usage: ingest.py ingest --dir D --file F | stalled --dir D [--k 3]')
    if args[0] in ('ingest', 'stalled'):
        command, rest = args[0], args[1:]
    elif args[0].startswith('-'):
        # events.py は `ingest` を付けずに残りを渡す。--file があれば ingest とみなす。
        command = 'ingest' if any(a == '--file' or a.startswith('--file=') for a in args) else 'stalled'
        rest = args
    else:
        return _usage(f'unknown command: {args[0]}')
    try:
        return _cmd_ingest(rest) if command == 'ingest' else _cmd_stalled(rest)
    except (Exception, SystemExit):
        return 2


if __name__ == '__main__':
    sys.exit(main())
