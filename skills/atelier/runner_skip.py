"""1 回の実行 (run_id) で使わない実行先・モデルの記録 `<pbi-dir>/runners.skip.json` を読み書きする。

role_run.py と select_model.py が呼び、停止した実行先・上限に達したモデルを同じ run のあいだ除外する。

使い方:
  python <このディレクトリ>/runner_skip.py add <pbi-dir> (--runner X | --model M) --cause C --fallback F
  python <このディレクトリ>/runner_skip.py show <pbi-dir>
"""
import argparse
import re
import sys
from pathlib import Path

import common
import events
import runner_cli

SCHEMA = 'atelier.runners-skip/1'
FILENAME = 'runners.skip.json'
CAUSES = ('broken', 'limit', 'auth', 'other')
# agent は実行系の本体なので飛ばせない
RUNNERS = tuple(r for r in runner_cli.ROLE_RUNNERS if r != 'agent')
MODEL = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/:\[\]-]{0,127}$')


def _fail(msg: str) -> None:
    print(f'runner_skip: {msg}', file=sys.stderr)
    sys.exit(2)


def _current_run_id(base: Path) -> str | None:
    """<base>/conductor/run.json の run_id。無ければ None。"""
    run_id, _ = events._run_keys(Path(base))
    return run_id


def _valid_entry(item: object) -> dict | None:
    """entries の 1 項目を点検して返す。型が壊れていれば None。"""
    if not isinstance(item, dict):
        return None
    runner = item.get('runner')
    model = item.get('model')
    if not (runner is None or isinstance(runner, str)) or not (model is None or isinstance(model, str)):
        return None
    if runner == 'agent':
        return None
    if not (runner or model):
        return None
    return item


def _active_entries(base: Path) -> list[dict]:
    """今の run_id に合う有効な項目だけを返す。ファイル無し・破損・run_id 不一致なら空。"""
    base_dir = Path(base)
    run_id = _current_run_id(base_dir)
    if not run_id:
        return []
    data = common.load_json(base_dir / FILENAME)
    if not isinstance(data, dict) or data.get('run_id') != run_id:
        return []
    raw = data.get('entries')
    if not isinstance(raw, list):
        return []
    return [item for item in (_valid_entry(x) for x in raw) if item is not None]


def load(base: Path) -> frozenset[str]:
    """今の run で飛ばす実行先名とモデル ID の集合。壊れていても例外を出さず空集合を返す。"""
    try:
        names = set()
        for item in _active_entries(Path(base)):
            runner = item.get('runner')
            model = item.get('model')
            if isinstance(runner, str) and runner:
                names.add(runner)
            if isinstance(model, str) and model:
                names.add(model)
        return frozenset(names)
    except (OSError, ValueError, TypeError):
        return frozenset()


def entry(base: Path, name: str) -> dict | None:
    """今の run で name (実行先名またはモデル ID) に一致する最初の項目。無ければ None。"""
    try:
        for item in _active_entries(Path(base)):
            if item.get('runner') == name or item.get('model') == name:
                return item
    except (OSError, ValueError, TypeError):
        return None
    return None


def format_line(item: dict) -> str:
    """1 項目を 1 行の文字列に整形する。"""
    runner = item.get('runner')
    if isinstance(runner, str) and runner:
        kind, name = 'runner', runner
    else:
        kind, name = 'model', str(item.get('model') or '')
    cause = item.get('cause') or 'other'
    fallback = item.get('fallback') or ''
    return f'skip {kind} {name} cause {cause} fallback {fallback}'


def _add(args) -> None:
    base = Path(args.pbi_dir)
    run_id = _current_run_id(base)
    if not run_id:
        _fail(f'run.json がありません: {(base / "conductor" / "run.json").as_posix()}')
    if args.runner is not None:
        if args.runner == 'agent':
            _fail('agent はスキップできません')
        if args.runner not in RUNNERS:
            _fail(f"--runner は {', '.join(RUNNERS)} のどれか: {args.runner!r}")
    if args.model is not None and not MODEL.match(args.model):
        _fail(f'--model に使えない文字があります: {args.model!r}')
    if not args.fallback.strip():
        _fail('--fallback は空にできません')

    # run_id が変わった後のファイルは _active_entries が空を返すので、新しい entries から書き直す
    entries = _active_entries(base)
    if args.runner is not None:
        entries = [e for e in entries if e.get('runner') != args.runner]
    else:
        entries = [e for e in entries if e.get('model') != args.model]

    item = {
        'runner': args.runner,
        'model': args.model,
        'cause': args.cause,
        'fallback': args.fallback,
        'at': common.now().isoformat(timespec='seconds'),
    }
    entries.append(item)
    common.write_json(base / FILENAME, {'schema': SCHEMA, 'run_id': run_id, 'entries': entries})
    print(format_line(item))


def _show(args) -> None:
    entries = _active_entries(Path(args.pbi_dir))
    if not entries:
        print('runners.skip: none')
        return
    for item in entries:
        print(format_line(item))


def main(argv=None) -> None:
    common.utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest='cmd', required=True)

    add_p = sub.add_parser('add', help='スキップする実行先またはモデルを追加する')
    add_p.add_argument('pbi_dir')
    target = add_p.add_mutually_exclusive_group(required=True)
    target.add_argument('--runner')
    target.add_argument('--model')
    add_p.add_argument('--cause', required=True, choices=CAUSES)
    add_p.add_argument('--fallback', required=True)

    show_p = sub.add_parser('show', help='今の run で有効なスキップ一覧を出す')
    show_p.add_argument('pbi_dir')

    args = ap.parse_args(argv)
    if args.cmd == 'add':
        _add(args)
    else:
        _show(args)


if __name__ == '__main__':
    main()
