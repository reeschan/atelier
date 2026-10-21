"""セットアップ (検出・ヒアリングの記録・モデルの取り直し) のスクリプト。

  python setup_agents.py detect [--home DIR] [--json]
  python setup_agents.py record --use opencode,codex [--home DIR]      (--use "" は agent だけ)
  python setup_agents.py models [--runner R ...] [--home DIR]
  python setup_agents.py add-model --runner R --id ID [--effort E ...] [--home DIR]
  python setup_agents.py select --runner R --models ID,ID [--home DIR]   (--models "" は何も選ばない)

home の agents.json に利用者が決めたこと (使う実行先・CLI の場所と版・選んだモデル) を、
models/<runner>.json に取得した一覧の控えを書く。取得の失敗は実行先ごとに閉じ込め、
取れなかった実行先は add-model で手入力できる状態にする。
関数は編集画面のサーバー (studio/serve.py) からも import する。
"""
import argparse
import datetime
import json
import sys
from pathlib import Path

import agents
import common
import model_fetch

AGENTS_FILE = 'agents.json'
MODELS_DIR = 'models'

# set_runner で models のキーを消すための番兵 (None は「触らない」)
REMOVE = object()


def detect(env=None, home=None) -> dict:
    """実行先ごとの CLI の有無・場所・版。agent は常に見つかる扱いにする。

    external は agents.find_cli(runner, load_agents(home), env) で探し、版は model_fetch.version。
    """
    if home is None:
        home = common.resolve_home(None)
    table = agents.load_agents(home)
    result = {}
    for runner in common.RUNNERS:
        if runner == common.AGENT:
            result[runner] = {'found': True, 'path': None, 'version': None}
            continue
        path = agents.find_cli(runner, table, env)
        found = path is not None
        result[runner] = {
            'found': found,
            'path': path,
            'version': model_fetch.version(runner, path, env) if found else None,
        }
    return result


def record(home, use, env=None, now=None) -> dict:
    """検出し直して agents.json を書く。既存の models・billing などのキーは残す。

    各 runner の enabled は use に含まれるか agent。見つかった外部の runner だけ
    path・version・checked_at を更新する。書いた agents.json を返す。
    """
    _check_runners(use)
    home = Path(home)
    chosen = set(use)
    stamp = _stamp(now)
    found = detect(env, home)
    table = agents.load_agents(home)
    if not isinstance(table, dict):
        table = {}
    if not isinstance(table.get('runners'), dict):
        table['runners'] = {}
    for runner in common.RUNNERS:
        current = table['runners'].get(runner)
        config = dict(current) if isinstance(current, dict) else {}
        config['enabled'] = runner == common.AGENT or runner in chosen
        info = found[runner]
        if runner != common.AGENT and info['found']:
            config['path'] = info['path']
            config['version'] = info['version']
            config['checked_at'] = stamp
        table['runners'][runner] = config
    common.write_json(home / AGENTS_FILE, table)
    return table


def refresh_models(home, runners=None, env=None, now=None) -> list:
    """実行先ごとにモデルの一覧を取り直して models/<runner>.json に書く。

    runners を省くと agents.json で enabled の runner (agents.json が無ければ agent と
    見つかった runner)。1 つの取得の失敗はほかの実行先に波及させない。
    """
    home = Path(home)
    if runners is None:
        runners = _enabled_runners(home, env)
    else:
        _check_runners(runners)
        runners = list(dict.fromkeys(runners))
    stamp = _stamp(now)
    table = agents.load_agents(home)
    return [_refresh_one(home, runner, table, env, stamp) for runner in runners]


def missing_selected(home, runner) -> list:
    """agents.json で選んだモデルのうち、models/<runner>.json の一覧に無い id。

    models/<runner>.json が無ければ []。
    """
    _check_runner(runner)
    selected = _selected_models(agents.load_agents(home), runner)
    if not selected:
        return []
    data = load_models(home, runner)
    if data is None:
        return []
    entries = data.get('models')
    if not isinstance(entries, list):
        entries = []
    known = {item.get('id') for item in entries if isinstance(item, dict)}
    return [model_id for model_id in selected if model_id not in known]


def add_manual_model(home, runner, model_id, efforts, now=None) -> dict:
    """models/<runner>.json に手入力のモデルを足す。

    同じ id があれば efforts を置き換えて manual を付ける。書いたファイルの中身を返す。
    """
    _check_runner(runner)
    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError('the model id must not be empty.')
    home = Path(home)
    data = load_models(home, runner)
    if data is None:
        data = {'runner': runner, 'fetched_at': None, 'method': 'manual',
                'error': None, 'failed_at': None, 'models': []}
    data['runner'] = runner
    entries = data.get('models')
    entries = list(entries) if isinstance(entries, list) else []
    effort_list = [str(value) for value in (efforts or [])]
    for entry in entries:
        if isinstance(entry, dict) and entry.get('id') == model_id:
            entry['efforts'] = effort_list
            entry['manual'] = True
            break
    else:
        entries.append({'id': model_id, 'name': None, 'efforts': effort_list, 'manual': True})
    data['models'] = entries
    common.write_json(_models_path(home, runner), data)
    return data


def set_runner(home, runner, enabled=None, models=None, billing=None) -> dict:
    """agents.json のその runner だけを更新する。None の引数は触らない。

    models に [] を渡すと空にし、REMOVE を渡すとキーを消す。agent の enabled false は ValueError。
    """
    _check_runner(runner)
    if runner == common.AGENT and enabled is False:
        raise ValueError('agent cannot be disabled.')
    home = Path(home)
    table = agents.load_agents(home)
    if not isinstance(table, dict):
        table = {}
    if not isinstance(table.get('runners'), dict):
        table['runners'] = {}
    current = table['runners'].get(runner)
    config = dict(current) if isinstance(current, dict) else {}
    if runner == common.AGENT:
        # agent は常に使う (enabled false は受け付けない)
        config['enabled'] = True
    elif enabled is not None:
        config['enabled'] = bool(enabled)
    if models is REMOVE:
        config.pop('models', None)
    elif models is not None:
        if not isinstance(models, (list, tuple)):
            raise ValueError('models must be a list.')
        config['models'] = list(models)
    if billing is not None:
        config['billing'] = billing
    table['runners'][runner] = config
    common.write_json(home / AGENTS_FILE, table)
    return table


def load_models(home, runner) -> dict | None:
    """models/<runner>.json を読む。無い・壊れている・オブジェクトでないときは None。"""
    _check_runner(runner)
    data = common.load_json(_models_path(home, runner))
    return data if isinstance(data, dict) else None


def _refresh_one(home, runner, table, env, stamp) -> dict:
    """1 つの実行先の取り直し。失敗してもファイルと戻り値を整えて返す。"""
    try:
        cli = None if runner == common.AGENT else agents.find_cli(runner, table, env)
        fetched = model_fetch.fetch(runner, cli, env)
    except model_fetch.FetchError as error:
        message = str(error)
        data = _failed_models(home, runner, message, stamp)
        common.write_json(_models_path(home, runner), data)
        return {'runner': runner, 'ok': False, 'count': _model_count(data),
                'method': data.get('method'), 'error': message,
                'missing_selected': missing_selected(home, runner)}
    previous = load_models(home, runner) or {}
    entries = previous.get('models')
    entries = entries if isinstance(entries, list) else []
    manual = [entry for entry in entries
              if isinstance(entry, dict) and entry.get('manual') is True]
    ids = {entry.get('id') for entry in fetched['models']}
    data = {
        'runner': runner,
        'fetched_at': stamp,
        'method': fetched['method'],
        'error': None,
        'failed_at': None,
        'models': list(fetched['models']) + [entry for entry in manual if entry.get('id') not in ids],
    }
    common.write_json(_models_path(home, runner), data)
    return {'runner': runner, 'ok': True, 'count': len(data['models']),
            'method': data['method'], 'error': None,
            'missing_selected': missing_selected(home, runner)}


def _failed_models(home, runner, message, stamp) -> dict:
    """取り直しに失敗したときの models/<runner>.json。前の一覧はそのまま残す。"""
    previous = load_models(home, runner)
    if previous is None:
        return {'runner': runner, 'fetched_at': None, 'method': 'manual',
                'error': message, 'failed_at': stamp, 'models': []}
    data = dict(previous)
    data['runner'] = runner
    data.setdefault('fetched_at', None)
    data.setdefault('method', 'manual')
    if not isinstance(data.get('models'), list):
        data['models'] = []
    data['error'] = message
    data['failed_at'] = stamp
    return data


def _enabled_runners(home, env) -> list:
    """取り直す既定の実行先。agents.json が無ければ agent と見つかった runner。"""
    table = agents.load_agents(home)
    if table is None:
        found = detect(env, home)
        return [runner for runner in common.RUNNERS
                if runner == common.AGENT or found[runner]['found']]
    configs = table.get('runners') or {}
    return [runner for runner in common.RUNNERS
            if isinstance(configs.get(runner), dict) and configs[runner].get('enabled')]


def _selected_models(table, runner) -> list:
    """agents.json のその runner で選ばれたモデル。models キーが無い・一覧でないときは []。"""
    config = (table or {}).get('runners') if isinstance(table, dict) else None
    entry = config.get(runner) if isinstance(config, dict) else None
    selected = entry.get('models') if isinstance(entry, dict) else None
    return selected if isinstance(selected, list) else []


def _models_path(home, runner) -> Path:
    return Path(home) / MODELS_DIR / f'{runner}.json'


def _model_count(data) -> int:
    entries = data.get('models') if isinstance(data, dict) else None
    return len(entries) if isinstance(entries, list) else 0


def _stamp(now) -> str:
    """書き込む時刻の ISO 文字列。now を省くと今。"""
    if now is None:
        now = common.now()
    if isinstance(now, str):
        return now
    return now.isoformat()


def _check_runner(runner) -> None:
    if runner not in common.RUNNERS:
        raise ValueError(f'unknown runner: {runner}. '
                         f'Valid runners: {", ".join(common.RUNNERS)}.')


def _check_runners(runners) -> None:
    for runner in runners:
        _check_runner(runner)


def _split(value: str) -> list:
    """カンマ区切りの値を、空の項目を捨てた一覧にする ("" は空の一覧)。"""
    return [item.strip() for item in value.split(',') if item.strip()]


def _now(value):
    """--now の ISO 文字列を datetime に。省略は None。"""
    if value is None:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError(f'--now is not an ISO time: {value}.') from None


def _cmd_detect(args) -> None:
    result = detect(home=common.resolve_home(args.home))
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        return
    for runner in common.RUNNERS:
        info = result[runner]
        state = 'found' if info['found'] else 'missing'
        print(f'{runner}  {state}  {info["path"] or "-"}  {info["version"] or "-"}')


def _cmd_record(args) -> None:
    table = record(common.resolve_home(args.home), _split(args.use), now=_now(args.now))
    for runner in common.RUNNERS:
        config = table['runners'].get(runner) or {}
        state = 'on' if config.get('enabled') else 'off'
        print(f'{runner}  {state}  {config.get("path") or "-"}')


def _cmd_models(args) -> None:
    for result in refresh_models(common.resolve_home(args.home), args.runner, now=_now(args.now)):
        runner = result['runner']
        if result['ok']:
            print(f'{runner}  ok  {result["count"]} models ({result["method"]})')
        else:
            print(f'{runner}  failed  {result["error"]}  '
                  f'(kept {result["count"]}; add by hand: python setup_agents.py '
                  f'add-model --runner {runner} --id <model>)')
        for model_id in result['missing_selected']:
            print(f'warn {runner}: selected model {model_id} is no longer listed')


def _cmd_add_model(args) -> None:
    data = add_manual_model(common.resolve_home(args.home), args.runner, args.id, args.effort,
                            now=_now(args.now))
    print(f'{args.runner}  added  {args.id}  ({_model_count(data)} models)')


def _cmd_select(args) -> None:
    table = set_runner(common.resolve_home(args.home), args.runner, models=_split(args.models))
    config = table['runners'].get(args.runner) or {}
    print(f'{args.runner}  selected  {len(config.get("models") or [])} models')


class Parser(argparse.ArgumentParser):
    """不正な引数は終了コード 2 と標準エラーの 1 行で止める。"""

    def error(self, message: str) -> None:
        sys.stderr.write(f'error: {message}\n')
        self.exit(2)


def build_parser() -> argparse.ArgumentParser:
    parser = Parser(prog='setup_agents.py', description='Detect runners and fetch their models.')
    sub = parser.add_subparsers(dest='sub', required=True, metavar='<command>')
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument('--home', metavar='DIR', help='home directory (default: PBI_HOME, then ~/.config/pbi-orchestration-jev)')
    shared.add_argument('--now', metavar='ISO', help='time to record (default: now)')

    detect_cmd = sub.add_parser('detect', parents=[shared], help='show which runner CLIs are found')
    detect_cmd.add_argument('--json', action='store_true', help='print JSON instead of a table')
    detect_cmd.set_defaults(handler=_cmd_detect)

    record_cmd = sub.add_parser('record', parents=[shared], help='write the chosen runners to agents.json')
    record_cmd.add_argument('--use', required=True, metavar='R,R', help='runners to use, comma-separated ("" for agent only)')
    record_cmd.set_defaults(handler=_cmd_record)

    models_cmd = sub.add_parser('models', parents=[shared], help='fetch the model lists again')
    models_cmd.add_argument('--runner', action='append', choices=common.RUNNERS, help='runner to fetch (repeatable; default: enabled runners)')
    models_cmd.set_defaults(handler=_cmd_models)

    add_cmd = sub.add_parser('add-model', parents=[shared], help='add a model id by hand')
    add_cmd.add_argument('--runner', required=True, choices=common.RUNNERS)
    add_cmd.add_argument('--id', required=True, metavar='MODEL')
    add_cmd.add_argument('--effort', action='append', default=[], metavar='EFFORT', help='effort level (repeatable)')
    add_cmd.set_defaults(handler=_cmd_add_model)

    select_cmd = sub.add_parser('select', parents=[shared], help='set the selected models of a runner')
    select_cmd.add_argument('--runner', required=True, choices=common.RUNNERS)
    select_cmd.add_argument('--models', required=True, metavar='ID,ID', help='model ids, comma-separated ("" selects none)')
    select_cmd.set_defaults(handler=_cmd_select)
    return parser


def main(argv=None) -> int:
    common.utf8_stdout()
    args = build_parser().parse_args(argv)
    try:
        args.handler(args)
    except ValueError as error:
        sys.stderr.write(f'error: {error}\n')
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
