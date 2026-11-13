#!/usr/bin/env python3
"""Atelier Desk 向けの、部品とフローの定義を版付き JSON で返す読み取り専用 CLI。

    python desk_catalog.py components --home HOME
    python desk_catalog.py workflows  --home HOME

HOME は絶対パス。存在しなければ空の home として扱い (同梱の定義だけを返す)、何も作らない。
成功 (終了 0): stdout に ASCII にエスケープした JSON を 1 行だけ書く。stderr は空。
  components: {"schema": "atelier.components/1", "items": [{"id", "source": "bundled|home", "data"}]}  (id の昇順)
  workflows:  {"schema": "atelier.workflows/1", "items": [{"name", "source": "bundled|home", "bundled", "data"}]}  (name の昇順)
失敗: stdout は空で、stderr に短い診断を 1 行書く。
  2 = 引数の誤り (未知のコマンド・--home の欠落・相対パスなど)
  4 = 定義の破損・読み取り失敗 (部品やフローの誤りが 1 件でもあれば一覧全体を失敗にする)
  (3 は --input などの名前付きの対象を持つコマンド用で、このコマンドでは使わない)

定義の検証は flow.load_components / flow.workflow_errors の規則そのまま。この CLI は home に何も書かない。
"""
import sys

sys.dont_write_bytecode = True  # 配布版に __pycache__ を作らない (他の import より先に)

import argparse  # noqa: E402
import enum  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import BinaryIO  # noqa: E402

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import flow  # noqa: E402
import templates  # noqa: E402

__all__ = ['main']

PROG = 'desk_catalog.py'
COMPONENTS_SCHEMA = 'atelier.components/1'
WORKFLOWS_SCHEMA = 'atelier.workflows/1'


class _Exit(enum.IntEnum):
    OK = 0
    USAGE = 2
    BROKEN = 4


class _CatalogError(Exception):
    def __init__(self, code: _Exit, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _CatalogError(_Exit.USAGE, f'{self.prog}: {message}')


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog=PROG, allow_abbrev=False,
                     description='Print Atelier component or workflow definitions as versioned JSON (read-only).')
    commands = parser.add_subparsers(dest='command', required=True, metavar='{components,workflows}')
    for name in _KINDS:
        sub = commands.add_parser(name, allow_abbrev=False, help=f'list {name}')
        sub.add_argument('--home', required=True, help='absolute path of the Atelier home (need not exist)')
    return parser


def _home(arg: str) -> Path:
    """絶対パスの home。存在しなければそのまま返す。存在するがディレクトリでなければ BROKEN。何も作らない。"""
    if '\0' in arg or not Path(arg).is_absolute():
        raise _CatalogError(_Exit.USAGE, f'{PROG}: --home must be an absolute path: {arg}')
    home = Path(arg)
    try:
        is_dir = home.is_dir()
        exists = is_dir or os.path.lexists(home)
    except (OSError, ValueError) as error:
        raise _CatalogError(_Exit.BROKEN, f'cannot read home {home}: {error}')
    if exists and not is_dir:
        raise _CatalogError(_Exit.BROKEN, f'home is not a directory: {home}')
    return home


def _json_files(directory: Path) -> list:
    """directory 直下の *.json の一覧 (名前順)。無ければ []。ディレクトリの読み取り失敗や *.json が通常ファイルでないときは BROKEN。"""
    try:
        with os.scandir(directory) as entries:
            list(entries)
    except FileNotFoundError:
        return []
    except OSError as error:
        raise _CatalogError(_Exit.BROKEN, f'cannot read directory {directory}: {error}')
    try:
        paths = sorted(directory.glob('*.json'))
        for path in paths:
            if not path.is_file():
                raise _CatalogError(_Exit.BROKEN, f'not a regular file: {path}')
    except OSError as error:
        raise _CatalogError(_Exit.BROKEN, f'cannot read directory {directory}: {error}')
    return paths


def _reject_constant(name: str):
    raise ValueError(f'{name} is not valid JSON')


def _read_object(path: Path) -> dict:
    """UTF-8 (BOM なし) の JSON オブジェクトを厳密に読む。読めない・不正・オブジェクトでないときは BROKEN。"""
    try:
        data = json.loads(path.read_bytes().decode('utf-8'), parse_constant=_reject_constant)
    except (OSError, ValueError, RecursionError) as error:
        raise _CatalogError(_Exit.BROKEN, f'cannot read {path}: {error}')
    if not isinstance(data, dict):
        raise _CatalogError(_Exit.BROKEN, f'not a JSON object: {path}')
    return data


def _summary(problems: list, limit: int = 1) -> str:
    rest = len(problems) - limit
    return '; '.join(problems[:limit]) + (f' (+{rest} more)' if rest > 0 else '')


def _components(home: Path) -> dict:
    """{id: (source, 定義)}。部品の誤りが 1 件でもあれば BROKEN。

    各部品ファイルは 1 回だけ、厳密に読む (NaN・BOM などを拒む)。その内容に flow.load_components と同じ規則
    (flow.component_error・id の重複・then が知らない部品を指す) を当て、同じ内容を出力に使う。
    """
    read = []  # (source, label, path, 定義)。先に全部を厳密に読み、読めないものは規則の前に BROKEN にする
    for source, label, directory in (('bundled', 'component', flow.COMPONENTS),
                                     ('home', 'home component', home / 'components')):
        for path in _json_files(directory):
            read.append((source, label, path, _read_object(path)))
    found, errors = {}, []
    for source, label, path, data in read:
        problem = flow.component_error(data, path.stem)
        if problem:
            errors.append(f'{label} {path.name}: {problem}')
        elif data['id'] in found:
            errors.append(f'{label} {path.name}: a bundled component already has this id')
        else:
            found[data['id']] = (source, data)
    for cid, (_, data) in found.items():
        for following in data.get('then', []):
            if following not in found:
                errors.append(f'component {cid}.json: then points at an unknown component: {following}')
    if errors:
        raise _CatalogError(_Exit.BROKEN, f'invalid components: {_summary(errors)}')
    return found


def _component_items(home: Path) -> list:
    components = _components(home)
    return [{'id': cid, 'source': components[cid][0], 'data': components[cid][1]} for cid in sorted(components)]


def _workflow_names(directory: Path) -> set:
    return {path.stem for path in _json_files(directory) if templates.NAME_RE.fullmatch(path.stem)}


def _workflow_items(home: Path) -> list:
    components = {cid: data for cid, (_, data) in _components(home).items()}
    home_dir = home / 'workflows'
    items = []
    for name in sorted(_workflow_names(flow.BUNDLED_WORKFLOWS) | _workflow_names(home_dir)):
        path = home_dir / f'{name}.json'
        source = 'home' if path.is_file() else 'bundled'
        if source == 'bundled':
            path = flow.BUNDLED_WORKFLOWS / f'{name}.json'
        data = _read_object(path)
        problems = flow.workflow_errors(data, components)
        if problems:
            raise _CatalogError(_Exit.BROKEN, f'invalid workflow {name} ({path}): {_summary(problems)}')
        items.append({'name': name, 'source': source,
                      'bundled': (flow.BUNDLED_WORKFLOWS / f'{name}.json').is_file(), 'data': data})
    return items


_KINDS = {'components': (COMPONENTS_SCHEMA, _component_items),
          'workflows': (WORKFLOWS_SCHEMA, _workflow_items)}


def _run(argv, out: BinaryIO) -> None:
    args = _parser().parse_args(argv)
    schema, build = _KINDS[args.command]
    items = build(_home(args.home))
    try:
        text = json.dumps({'schema': schema, 'items': items}, ensure_ascii=True, allow_nan=False)
    except (ValueError, RecursionError) as error:
        raise _CatalogError(_Exit.BROKEN, f'definitions cannot be encoded as JSON: {error}')
    out.write(text.encode('ascii') + b'\n')
    out.flush()


def main(argv=None, out: BinaryIO | None = None, err: BinaryIO | None = None) -> int:
    """終了コードを返す。出力を全部組み立ててから 1 回だけ書く。失敗時は stdout に何も書かない。"""
    out = sys.stdout.buffer if out is None else out
    err = sys.stderr.buffer if err is None else err
    argv = sys.argv[1:] if argv is None else list(argv)
    try:
        _run(argv, out)
        return int(_Exit.OK)
    except SystemExit as stop:  # --help
        return stop.code if isinstance(stop.code, int) else int(_Exit.USAGE)
    except _CatalogError as error:
        code, message = error.code, error.message
    except Exception as error:  # 未捕捉の例外を終了 1 にしない
        code, message = _Exit.BROKEN, f'unexpected {type(error).__name__}: {error}'
    line = re.sub(r'\s*[\r\n]+\s*', ' ', message).strip()
    err.write(line.encode('utf-8', 'replace') + b'\n')
    err.flush()
    return int(code)


if __name__ == '__main__':
    raise SystemExit(main())
