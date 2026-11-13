#!/usr/bin/env python3
"""Atelier Desk 向けの、home の状態 (runner・model・routing・優先テンプレート) を版付き JSON で返す読み取り専用 CLI。

    python desk_state.py [--home HOME]

HOME は絶対パス。省略すると Atelier の既存の規則 (common.resolve_home) で決める。存在しない絶対パスは
空の home として扱い (既定の状態を返す)、何も作らない。期限切れの優先テンプレートも読み取りだけで
application_status に表し、履歴や overrides.json を書き換えない。
成功 (終了 0): stdout に ASCII にエスケープした JSON を 1 行だけ書く。stderr は空。
  {"schema": "atelier.state/1", "home", "runners", "routing", "templates", "examples",
   "application", "application_status", "warnings"}
  値は studio/serve.py の state() と同じ (flow だけは Desk が desk_catalog.py で取るので含めない)。
  JSON として読めたが意味の誤りがあるファイルは Studio と同じ既定値か warnings で 0 のまま返す。
失敗: stdout は空で、stderr に短い診断を 1 行書く。
  2 = 引数の誤り (未知の引数・相対パスの --home など)
  4 = 定義の破損・読み取り失敗 (JSON として読めないファイル、存在するがディレクトリでない home など)
  (3 は --input などの名前付きの対象を持つコマンド用で、このコマンドでは使わない)

この CLI は home に何も書かず、ネットワークも使わない。--help は通常のヘルプを出して 0。
"""
import sys

sys.dont_write_bytecode = True  # 配布版に __pycache__ を作らない (studio の serve を含め、他の import より先に)

import argparse  # noqa: E402
import enum  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import BinaryIO  # noqa: E402

HERE = Path(__file__).resolve().parent
for _path in (HERE, HERE / 'studio'):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import common  # noqa: E402
import serve  # noqa: E402
import templates  # noqa: E402

__all__ = ['main', 'build']

PROG = 'desk_state.py'
SCHEMA = 'atelier.state/1'
# 契約の並び。serve.state() が足したキーは、ここに加えるまで schema には漏れない
STATE_KEYS = ('home', 'runners', 'routing', 'templates', 'examples',
              'application', 'application_status', 'warnings')


class _Exit(enum.IntEnum):
    OK = 0
    USAGE = 2
    BROKEN = 4


class _StateError(Exception):
    def __init__(self, code: _Exit, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _StateError(_Exit.USAGE, f'{self.prog}: {message}')


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog=PROG, allow_abbrev=False,
                     description='Print the Atelier home state (runners, models, routing, templates) as versioned JSON (read-only).')
    parser.add_argument('--home', help='absolute path of the Atelier home (need not exist); default: the Atelier rule')
    return parser


def _home(arg) -> Path:
    """絶対パスの home。存在しなければそのまま返す。存在するがディレクトリでなければ BROKEN。何も作らない。"""
    if arg is None:
        try:
            home = common.resolve_home(None)
            home = home if home.is_absolute() else Path(os.path.abspath(home))
        except (OSError, RuntimeError, ValueError) as error:
            raise _StateError(_Exit.BROKEN, f'cannot resolve the default home: {error}')
    else:
        if not arg or '\0' in arg or not Path(arg).is_absolute():
            raise _StateError(_Exit.USAGE, f'{PROG}: --home must be an absolute path: {arg}')
        home = Path(arg)
    try:
        is_dir = home.is_dir()
        exists = is_dir or os.path.lexists(home)
    except (OSError, ValueError) as error:
        raise _StateError(_Exit.BROKEN, f'cannot read home {home}: {error}')
    if exists and not is_dir:
        raise _StateError(_Exit.BROKEN, f'home is not a directory: {home}')
    return home


def _reject_constant(name: str):
    raise ValueError(f'{name} is not valid JSON')


def _check_json(home: Path, relative: str) -> None:
    """home の下の相対パスが JSON として読めることを確かめる。無ければ通す。通常ファイルでない・読めない・不正は BROKEN。"""
    path = home / relative
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return
    except OSError as error:
        raise _StateError(_Exit.BROKEN, f'cannot read {relative}: {error}')
    try:
        json.loads(raw.decode('utf-8'), parse_constant=_reject_constant)
    except (ValueError, RecursionError) as error:
        raise _StateError(_Exit.BROKEN, f'{relative} is not readable JSON: {error}')


def _preflight(home: Path) -> None:
    """Studio が黙って既定値にする、JSON として読めないファイルを先に BROKEN にする (意味の誤りは通す)。"""
    names = ['agents.json', 'routing.json', 'overrides.json']
    names += [f'models/{runner}.json' for runner in common.RUNNERS]
    for name in names:
        _check_json(home, name)
    directory = home / 'templates'
    try:
        with os.scandir(directory):  # 読めないディレクトリは glob が黙って空にするので、先に確かめる
            pass
        # templates.load_all と同じ glob('*.json') (大文字小文字の扱いも OS ごとに load_all と一致する)
        stems = sorted(path.stem for path in directory.glob('*.json'))
    except FileNotFoundError:
        return
    except OSError as error:
        raise _StateError(_Exit.BROKEN, f'cannot read directory templates: {error}')
    for stem in stems:
        if templates.NAME_RE.fullmatch(stem):
            _check_json(home, f'templates/{stem}.json')


def build(home: Path, now) -> dict:
    """schema 付きの状態。now はタイムゾーン付き。home に書き込まず、ネットワークも使わない。"""
    _preflight(home)
    try:
        full = serve.state(home, serve.load_catalog(), now)
    except (Exception, SystemExit) as error:  # 同梱の routing.default の破損は sys.exit になる
        raise _StateError(_Exit.BROKEN, f'cannot read the state: {type(error).__name__}: {error}')
    return {'schema': SCHEMA, **{key: full[key] for key in STATE_KEYS}}


def _run(argv, out: BinaryIO, now) -> None:
    args = _parser().parse_args(argv)
    state = build(_home(args.home), common.now() if now is None else now)
    try:
        text = json.dumps(state, ensure_ascii=True, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as error:
        raise _StateError(_Exit.BROKEN, f'the state cannot be encoded as JSON: {error}')
    out.write(text.encode('ascii') + b'\n')
    out.flush()


def main(argv=None, out: BinaryIO | None = None, err: BinaryIO | None = None, now=None) -> int:
    """終了コードを返す。出力を全部組み立ててから 1 回だけ書く。失敗時は stdout に何も書かない。"""
    out = sys.stdout.buffer if out is None else out
    err = sys.stderr.buffer if err is None else err
    argv = sys.argv[1:] if argv is None else list(argv)
    try:
        _run(argv, out, now)
        return int(_Exit.OK)
    except SystemExit as stop:  # --help
        return stop.code if isinstance(stop.code, int) else int(_Exit.USAGE)
    except _StateError as error:
        code, message = error.code, error.message
    except Exception as error:  # 未捕捉の例外を終了 1 にしない
        code, message = _Exit.BROKEN, f'unexpected {type(error).__name__}: {error}'
    line = re.sub(r'\s*[\r\n]+\s*', ' ', message).strip()
    err.write(line.encode('utf-8', 'replace') + b'\n')
    err.flush()
    return int(code)


if __name__ == '__main__':
    raise SystemExit(main())
