#!/usr/bin/env python3
"""Atelier Desk 向けの、保存前の検証 CLI。Studio の保存 (PUT/POST) と同じ問題を、何も書かずに返す。

    python desk_validate.py KIND --home HOME --input FILE [--name NAME]

KIND は runner / model / routing / template / workflow / application / application-answer。
HOME は絶対パス (存在しなくてよく、空の home として扱う。何も作らない)。FILE は BOM なしの UTF-8 の
JSON オブジェクトで、KIND ごとの Studio の本文と同じ形。--name は runner・model (runner 名)、
template・workflow (対象名) で必須、ほかの KIND では受け付けない。
成功 (終了 0) と検証失敗 (終了 1): stdout に ASCII にエスケープした JSON を 1 行だけ書く。stderr は空。
  {"schema": "atelier.validation/1", "kind", "valid", "problems"}
  problems は valid なら空、そうでなければ Studio の HTTP エラー本文の error と同じ文字列 1 つ。
呼び出しの誤り: stdout は空で、stderr に短い診断を 1 行書く。
  2 = 引数の誤り・不正な JSON・JSON オブジェクトでない・未知の KIND・未知の runner・不正な名前・
      相対パスの --home・UTF-8 として読めない入力
  3 = --input のファイルが無い
  4 = 読み取り失敗 (入力がディレクトリ、home がディレクトリでない、予期しない例外)
home が壊れていても Studio と同じ結果を返す (Studio は壊れた定義を既定値として扱う)。

home に何も書かず、ネットワークも使わない。保存や取得の関数 (setup_agents・model_fetch・studio/serve) は
import しない。時刻は common.now() だけから取る。--help は通常のヘルプを出して 0。
"""
import sys

sys.dont_write_bytecode = True  # 選んだ版に __pycache__ を作らない (他の import より先に)

import argparse  # noqa: E402
import dataclasses  # noqa: E402
import datetime  # noqa: E402
import enum  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import BinaryIO, Callable, Mapping  # noqa: E402

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import agents  # noqa: E402
import common  # noqa: E402
import flow  # noqa: E402
import select_model  # noqa: E402
import templates  # noqa: E402

__all__ = ['main', 'check', 'KINDS']

PROG = 'desk_validate.py'
SCHEMA = 'atelier.validation/1'


class Exit(enum.IntEnum):
    OK = 0
    INVALID = 1
    USAGE = 2
    MISSING = 3
    BROKEN = 4


class CallError(Exception):
    """検証の判定ではない呼び出しの誤り (終了 2〜4)。stdout には何も書かない。"""

    def __init__(self, code: Exit, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclasses.dataclass(frozen=True)
class Request:
    home: Path
    body: dict
    name: str | None
    now: datetime.datetime


# Studio の error 文字列、問題が無ければ None
Check = Callable[[Request], 'str | None']


@dataclasses.dataclass(frozen=True)
class Kind:
    name_rule: Callable[['str | None'], 'str | None']  # 終了 2 にする理由。判定より先に評価する
    checks: tuple  # Studio のハンドラと同じ順序。最初に出た 1 件で止める


# --- --name の規則 (Studio はパスの名前を判定の前に確かめる) ---

def _no_name(name):
    return None if name is None else 'this KIND does not take --name.'


def _runner_name(name):
    if name is None:
        return '--name (the runner) is required.'
    return None if name in common.RUNNERS else f'unknown runner: {name}.'


def _template_name(name):
    if name is None:
        return '--name (the template) is required.'
    return None if templates.NAME_RE.fullmatch(name) else 'the template name is not valid.'


def _workflow_name(name):
    if name is None:
        return '--name (the workflow) is required.'
    return None if templates.NAME_RE.fullmatch(name) else 'the workflow name is not valid.'


# --- 保存する内容を UTF-8 にできるか (Studio は common.write_json / append_history の書き込みで気づく) ---
# UnicodeEncodeError は ValueError なので、Studio と同じく str(error) が問題の文字列になる
# (application・answer は前置き付き)。position の数も同じになるよう、Studio と同じ書き方で文字列にする。

def _file_text(data) -> str:
    """common.write_json が書く文字列 (ensure_ascii=False・indent 2・末尾の改行)。"""
    return json.dumps(data, ensure_ascii=False, indent=2) + '\n'


def _history_text(record) -> str:
    """templates.append_history が書く 1 行。"""
    return json.dumps(record, ensure_ascii=False) + '\n'


def _encodable(*texts: str) -> None:
    """保存する文字列を順に UTF-8 にする。できなければ UnicodeEncodeError (単独サロゲートなど)。"""
    for text in texts:
        text.encode('utf-8')


# --- runner (PUT /api/agents/<runner> -> setup_agents.set_runner の検査) ---

def _runner_agent_enabled(req: Request) -> str | None:
    if req.name == common.AGENT and req.body.get('enabled') is False:
        return 'agent cannot be disabled.'
    return None


def _runner_selected(req: Request) -> str | None:
    models = req.body.get('selected')
    if 'selected' not in req.body or models is None:
        return None
    if not isinstance(models, (list, tuple)) or not all(
            isinstance(model_id, str) and model_id.strip() for model_id in models):
        return 'models must be a list of non-empty model id strings.'
    return None


def _runner_saved(req: Request) -> str | None:
    """set_runner が agents.json に書く内容 (runner_table と同じ計算)。"""
    table = agents.load_agents(req.home)
    if not isinstance(table, dict):
        table = {}
    if not isinstance(table.get('runners'), dict):
        table['runners'] = {}
    current = table['runners'].get(req.name)
    config = dict(current) if isinstance(current, dict) else {}
    if req.name == common.AGENT:
        config['enabled'] = True
    elif req.body.get('enabled') is not None:
        config['enabled'] = bool(req.body['enabled'])
    if 'selected' in req.body:
        if req.body['selected'] is None:
            config.pop('models', None)
        else:
            config['models'] = list(req.body['selected'])
    if req.body.get('billing') is not None:
        config['billing'] = req.body['billing']
    table['runners'][req.name] = config
    _encodable(_file_text(table))
    return None


# --- model (POST /api/models/<runner> -> setup_agents.add_manual_model の検査) ---

def _model_id(req: Request) -> str | None:
    model_id = req.body.get('id')
    if not isinstance(model_id, str) or not model_id.strip():
        return 'id must be a non-empty string.'
    return None


def _model_efforts(req: Request) -> str | None:
    efforts = req.body.get('efforts', [])
    if efforts is not None and not isinstance(efforts, list):
        return 'efforts must be a list.'
    return None


def _model_saved(req: Request) -> str | None:
    """add_manual_model が models/<runner>.json に書く内容 (manual_models と同じ計算)。"""
    data = common.load_json(req.home / 'models' / f'{req.name}.json')
    if not isinstance(data, dict):
        data = {'runner': req.name, 'fetched_at': None, 'method': 'manual',
                'error': None, 'failed_at': None, 'models': []}
    data['runner'] = req.name
    entries = data.get('models')
    entries = list(entries) if isinstance(entries, list) else []
    model_id = req.body['id']
    efforts = [str(value) for value in (req.body.get('efforts') or [])]
    for entry in entries:
        if isinstance(entry, dict) and entry.get('id') == model_id:
            entry['efforts'] = efforts
            entry['manual'] = True
            break
    else:
        entries.append({'id': model_id, 'name': None, 'efforts': efforts, 'manual': True})
    data['models'] = entries
    _encodable(_file_text(data))
    return None


# --- routing (PUT /api/routing) ---

def routing_problem(home: Path, table: dict) -> str | None:
    """振り分けテンプレートの問題 (英語の 1 文)。無ければ None。"""
    error = select_model.routing_error(table)
    if error is not None:
        return f'the routing table is invalid: {error}'
    table_agents = agents.load_agents(home)
    if table_agents is None:
        return None
    configs = table_agents.get('runners')
    configs = configs if isinstance(configs, dict) else {}
    for tier in common.TIERS:
        for step in table['tiers'][tier]:
            runner = step.get('runner')
            if runner == common.AGENT:
                continue
            config = configs.get(runner)
            if not isinstance(config, dict) or not config.get('enabled'):
                return f'{runner} is not enabled in agents.json.'
            selected = config.get('models')
            if isinstance(selected, list) and step.get('model') not in selected:
                return f"{step.get('model')} is not selected for {runner}."
    return None


def _routing(req: Request) -> str | None:
    return routing_problem(req.home, req.body)


def _routing_saved(req: Request) -> str | None:
    _encodable(_file_text(req.body))
    return None


# --- template (PUT /api/templates/<name>) ---

def _template(req: Request) -> str | None:
    problems = templates.validate(req.body, req.name)
    if problems:
        return 'the template is not valid: ' + '; '.join(problems)
    return None


def _template_saved(req: Request) -> str | None:
    _encodable(_file_text(req.body))
    return None


# --- workflow (PUT /api/workflows/<name>) ---

def _workflow_name_matches(req: Request) -> str | None:
    if req.body.get('name', req.name) != req.name:
        return 'the name in the body must match the workflow name.'
    return None


def _workflow(req: Request) -> str | None:
    body = dict(req.body, name=req.name)
    components, errors = flow.load_components(home=req.home)
    errors += flow.workflow_errors(body, components)
    if errors:
        return 'the workflow is not valid: ' + '; '.join(errors)
    return None


def _workflow_saved(req: Request) -> str | None:
    _encodable(_file_text(dict(req.body, name=req.name)))
    return None


# --- application (PUT /api/application) ---

def _application_template(req: Request) -> str | None:
    name = req.body.get('template')
    if not isinstance(name, str) or templates.load_template(req.home, name) is None:
        return 'the template does not exist or is invalid.'
    return None


def _application_until(req: Request) -> str | None:
    until = req.body.get('until')
    if until is not None and not isinstance(until, str):
        return 'until must be an ISO time or null.'
    return None


def _application_on_expire(req: Request) -> str | None:
    if req.body.get('on_expire') not in ('ask', 'drop'):
        return 'on_expire must be ask or drop.'
    return None


def _application_reason(req: Request) -> str | None:
    if not isinstance(req.body.get('reason', ''), str):
        return 'reason must be a string.'
    return None


def _application_start(req: Request) -> str | None:
    """apply_local の検査だけ (new_application と、前の適用を history に移す計算)。書き込みは呼ばない。"""
    until = req.body.get('until')
    try:
        application = templates.new_application(
            req.home, req.body['template'], 'none' if until is None else until,
            req.body['on_expire'], req.body.get('reason', ''), 'studio', req.now)
        overrides = templates.load_overrides(req.home)
        writes = templates.previous_writes(overrides['application'], req.now)
        if writes:  # apply_local は前の適用を history.jsonl に足してから overrides.json を書く
            _encodable(_history_text(writes[0]['data']))
        _encodable(_file_text({'synced_at': overrides['synced_at'], 'updated_at': req.now.isoformat(),
                               'application': application}))
    except ValueError as error:
        return f'the application was not started: {error}'
    return None


# --- application-answer (POST /api/application/answer) ---

def _answer_choice(req: Request) -> str | None:
    if req.body.get('choice') not in ('extend', 'stop', 'forever'):
        return 'choice must be extend, stop or forever.'
    return None


def _answer_until(req: Request) -> str | None:
    until = req.body.get('until')
    if until is not None and not isinstance(until, str):
        return 'until must be an ISO time or null.'
    return None


def _answer_extend_needs_until(req: Request) -> str | None:
    if req.body.get('choice') == 'extend' and not req.body.get('until'):
        return 'until is required to extend.'
    return None


def _answer_has_application(req: Request) -> str | None:
    if not isinstance(templates.load_overrides(req.home)['application'], dict):
        return 'there is no application to answer.'
    return None


def _answer_save(req: Request) -> str | None:
    """answer_local の検査だけ (answered_application)。書き込みは呼ばない。"""
    overrides = templates.load_overrides(req.home)
    application = overrides['application']
    try:
        answered, ended_how = templates.answered_application(
            application, req.body['choice'], req.body.get('until'), req.now)
        if ended_how is not None:  # answer_local は終えた適用を history.jsonl に足してから overrides.json を書く
            _encodable(_history_text(templates.history_data(application, ended_how, req.now.isoformat())))
        _encodable(_file_text({'synced_at': overrides['synced_at'], 'updated_at': req.now.isoformat(),
                               'application': answered}))
    except ValueError as error:
        return f'the answer was not saved: {error}'
    return None


# KIND の唯一の一覧 (argparse の choices と --name の方針もここから作る)
KINDS: Mapping[str, Kind] = {
    'runner': Kind(_runner_name, (_runner_agent_enabled, _runner_selected, _runner_saved)),
    'model': Kind(_runner_name, (_model_id, _model_efforts, _model_saved)),
    'routing': Kind(_no_name, (_routing, _routing_saved)),
    'template': Kind(_template_name, (_template, _template_saved)),
    'workflow': Kind(_workflow_name, (_workflow_name_matches, _workflow, _workflow_saved)),
    'application': Kind(_no_name, (_application_template, _application_until, _application_on_expire,
                                   _application_reason, _application_start)),
    'application-answer': Kind(_no_name, (_answer_choice, _answer_until, _answer_extend_needs_until,
                                          _answer_has_application, _answer_save)),
}


def _require_name(kind: str, name: str | None) -> Kind:
    spec = KINDS.get(kind)
    if spec is None:
        raise CallError(Exit.USAGE, f'{PROG}: unknown KIND: {kind}')
    reason = spec.name_rule(name)
    if reason is not None:
        raise CallError(Exit.USAGE, f'{PROG}: {reason}')
    return spec


def check(kind: str, home, body: dict, name: str | None = None,
          now: datetime.datetime | None = None) -> str | None:
    """Studio が同じ入力に返す error の文字列。問題が無ければ None。

    呼び出しの誤り (未知の KIND・名前の規則・本文がオブジェクトでない) は CallError。Studio の ValueError と同じく、
    検査の中の ValueError は問題の文字列にする。ほかの例外はそのまま投げる。
    """
    spec = _require_name(kind, name)
    if not isinstance(body, dict):
        raise CallError(Exit.USAGE, f'{PROG}: the input must be a JSON object.')
    request = Request(Path(home), body, name, common.now() if now is None else now)
    try:
        for step in spec.checks:
            problem = step(request)
            if problem is not None:
                return problem
    except ValueError as error:
        return str(error)
    return None


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CallError(Exit.USAGE, f'{self.prog}: {message}')


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog=PROG, allow_abbrev=False,
                     description='Validate a Studio save body without saving it (read-only).')
    parser.add_argument('kind', metavar='KIND', choices=list(KINDS), help=' | '.join(KINDS))
    parser.add_argument('--home', required=True, help='absolute path of the Atelier home (need not exist)')
    parser.add_argument('--input', required=True, help='UTF-8 JSON object file (the Studio request body)')
    parser.add_argument('--name', default=None, help='runner (runner, model) or target name (template, workflow)')
    return parser


def _home(arg: str) -> Path:
    """絶対パスの home。存在しなければそのまま返す (何も作らない)。存在するがディレクトリでなければ BROKEN。"""
    if not arg or '\0' in arg or not Path(arg).is_absolute():
        raise CallError(Exit.USAGE, f'{PROG}: --home must be an absolute path: {arg}')
    home = Path(arg)
    try:
        is_dir = home.is_dir()
        exists = is_dir or os.path.lexists(home)
    except (OSError, ValueError) as error:
        raise CallError(Exit.BROKEN, f'cannot read home {home}: {error}')
    if exists and not is_dir:
        raise CallError(Exit.BROKEN, f'home is not a directory: {home}')
    return home


def _reject_constant(name: str):
    """NaN・Infinity・-Infinity は JSON ではない (desk_catalog.py・desk_state.py と同じ規則)。"""
    raise ValueError(f'{name} is not valid JSON')


def _read_body(arg: str) -> dict:
    if not arg or '\0' in arg:
        raise CallError(Exit.USAGE, f'{PROG}: --input is not a valid path.')
    try:
        raw = Path(arg).read_bytes()
    except FileNotFoundError:
        raise CallError(Exit.MISSING, f'{PROG}: the input file does not exist: {arg}')
    except OSError as error:
        raise CallError(Exit.BROKEN, f'{PROG}: cannot read the input {arg}: {error}')
    try:
        body = json.loads(raw.decode('utf-8'), parse_constant=_reject_constant)
    except UnicodeDecodeError:
        raise CallError(Exit.USAGE, f'{PROG}: the input is not UTF-8 without BOM.')
    except ValueError as error:
        raise CallError(Exit.USAGE, f'{PROG}: the input is not valid JSON: {error}')
    if not isinstance(body, dict):
        raise CallError(Exit.USAGE, f'{PROG}: the input must be a JSON object.')
    return body


def _run(argv, out: BinaryIO) -> int:
    args = _parser().parse_args(argv)
    home = _home(args.home)
    _require_name(args.kind, args.name)
    body = _read_body(args.input)
    problem = check(args.kind, home, body, args.name)
    result = {'schema': SCHEMA, 'kind': args.kind, 'valid': problem is None,
              'problems': [] if problem is None else [problem]}
    text = json.dumps(result, ensure_ascii=True, allow_nan=False)
    out.write(text.encode('ascii') + b'\n')
    out.flush()
    return int(Exit.OK if problem is None else Exit.INVALID)


def main(argv=None, out: BinaryIO | None = None, err: BinaryIO | None = None) -> int:
    """終了コードを返す。出力を組み立ててから 1 回だけ書く。呼び出しの誤りでは stdout に何も書かない。"""
    out = sys.stdout.buffer if out is None else out
    err = sys.stderr.buffer if err is None else err
    argv = sys.argv[1:] if argv is None else list(argv)
    try:
        return _run(argv, out)
    except SystemExit as stop:  # --help
        return stop.code if isinstance(stop.code, int) else int(Exit.USAGE)
    except CallError as error:
        code, message = error.code, error.message
    except Exception as error:  # 未捕捉の例外を終了 1 にしない (Studio の 500 に当たる)
        code, message = Exit.BROKEN, f'{PROG}: unexpected {type(error).__name__}: {error}'
    line = re.sub(r'\s*[\r\n]+\s*', ' ', message).strip()
    err.write(line.encode('utf-8', 'replace') + b'\n')
    err.flush()
    return int(code)


if __name__ == '__main__':
    raise SystemExit(main())
