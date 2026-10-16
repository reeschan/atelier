"""テンプレートと適用を扱う。

  python templates.py check [NAME ...] [--home DIR] [--now ISO]
  python templates.py sync --state FILE [--home DIR] [--now ISO]
  python templates.py due --state FILE [--within-hours 24] [--home DIR] [--now ISO]
  python templates.py apply --state FILE --template NAME --until ISO|none --on-expire ask|drop --reason TEXT [--via chat|web] [--home DIR] [--now ISO]
  python templates.py answer --state FILE --choice extend|stop|forever [--until ISO] [--home DIR] [--now ISO]
  python templates.py push [--existing NAME,...] [--home DIR] [--now ISO]

home は templates/ ディレクトリ・overrides.json・web.json を置くディレクトリ。解決順は
(1) --home (2) 環境変数 PBI_TEMPLATES_HOME (3) スクリプトのディレクトリの親の名前が
versions ならその親 (版管理のローカル配置。スキルのルート) (4) スクリプトのディレクトリ。

check は 1 件 1 行で ok <name> / ng <name>: <問題> を出し、1 件でも ng なら終了コード 1。
sync は写し overrides.json を書き、{"status", "application", "writes"} を UTF-8 の JSON で
出す。due は {"due", "hours_left"} か {"due": null} を出す。不正な引数・入力は終了コード
2 と標準エラーの 1 行。

apply は新しい適用 + 前の適用を history に移す writes、answer は期限への答えの writes、
push は templates/ への set と delete の writes を出す。どれもデータベースには触れない。

時刻はすべて ISO 8601 のタイムゾーン付き文字列 (例 2026-10-22T23:59:00+09:00)。
"""
import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path

NAME_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,62}$')
RUNNERS = ('opencode', 'codex', 'agy', 'agent')
TIERS = ('haiku', 'sonnet', 'opus')
# 形は振り分けテンプレート (routing.json) に揃える。$schema_note と手順の until (日付) も書ける
TEMPLATE_KEYS = frozenset(('name', 'description', 'tiers', '$schema_note'))
STEP_KEYS = frozenset(('runner', 'model', 'effort', 'until'))
# 交互グループ {"rotate": [手順, ...]}。PBI の番号で先頭を回す (select_model.expand_rotate)
ROTATE_KEY = 'rotate'
EMPTY_OVERRIDES = {'synced_at': None, 'application': None}


def resolve_home(arg: str | None) -> Path:
    """--home → 環境変数 → 親が versions ならスキルのルート → スクリプトのディレクトリ。"""
    if arg:
        return Path(arg)
    env = os.environ.get('PBI_TEMPLATES_HOME')
    if env:
        return Path(env)
    here = Path(__file__).parent
    if here.parent.name == 'versions':
        return here.parent.parent
    return here


def parse_time(s: str) -> datetime.datetime:
    """タイムゾーン付き ISO 8601 を aware な datetime に。無い・不正なら ValueError。"""
    if not isinstance(s, str):
        raise ValueError(f'時刻が文字列でない: {s!r}')
    try:
        parsed = datetime.datetime.fromisoformat(s)
    except ValueError:
        raise ValueError(f'時刻の形式が不正: {s!r}') from None
    if parsed.tzinfo is None:
        raise ValueError(f'タイムゾーンの無い時刻: {s!r}')
    return parsed


def validate(template: dict, filename_stem: str | None = None) -> list[str]:
    """問題の一覧を返す (空なら正しい)。"""
    if not isinstance(template, dict):
        return ['テンプレートがオブジェクトでない']
    problems = []
    name = template.get('name')
    if not isinstance(name, str):
        problems.append(f'name が文字列でない: {name!r}')
    else:
        if not NAME_RE.fullmatch(name):
            problems.append(f'name が {NAME_RE.pattern} に合わない: {name!r}')
        if filename_stem is not None and name != filename_stem:
            problems.append(f'name {name!r} がファイル名 {filename_stem!r} と違う')
    if not isinstance(template.get('description'), str):
        problems.append('description が文字列でない')
    extra = sorted(set(template) - TEMPLATE_KEYS)
    if extra:
        problems.append(f"余計なキー: {', '.join(extra)}")
    tiers = template.get('tiers')
    if not isinstance(tiers, dict) or not tiers:
        problems.append('tiers が空でないオブジェクトでない')
        return problems
    for tier, steps in tiers.items():
        if tier not in TIERS:
            problems.append(f'tiers のキー {tier!r} が無効（{" / ".join(TIERS)} のどれか）')
            continue
        if not isinstance(steps, list) or not steps:
            problems.append(f'{tier} が空でない配列でない')
            continue
        for i, step in enumerate(steps):
            if isinstance(step, dict) and ROTATE_KEY in step:
                problems.extend(rotate_problems(tier, i, step))
            else:
                problems.extend(step_problems(tier, i, step))
    return problems


def rotate_problems(tier: str, i: int, group: dict) -> list[str]:
    """交互グループ 1 つの問題の一覧。メンバーは 2 つ以上の通常の手順で、agent と入れ子は不可。"""
    where = f'{tier}[{i}]'
    problems = []
    extra = sorted(set(group) - {ROTATE_KEY})
    if extra:
        problems.append(f"{where} の余計なキー: {', '.join(extra)}")
    members = group[ROTATE_KEY]
    if not isinstance(members, list) or len(members) < 2:
        return problems + [f'{where} の rotate は手順 2 つ以上の配列にする']
    for j, member in enumerate(members):
        at = f'{where}.rotate[{j}]'
        if isinstance(member, dict) and ROTATE_KEY in member:
            problems.append(f'{at} は交互グループの入れ子にできない')
            continue
        if isinstance(member, dict) and member.get('runner') == 'agent':
            problems.append(f'{at} に agent は置けない (agent は route の最後に付く)')
            continue
        problems.extend(step_problems(tier, i, member, at))
    return problems


def step_problems(tier: str, i: int, step, where: str | None = None) -> list[str]:
    """段の手順 1 つの問題の一覧。"""
    where = where or f'{tier}[{i}]'
    if not isinstance(step, dict):
        return [f'{where} がオブジェクトでない']
    problems = []
    if step.get('runner') not in RUNNERS:
        problems.append(f"{where} の runner {step.get('runner')!r} が無効（{' / '.join(RUNNERS)} のどれか）")
    model = step.get('model')
    if not isinstance(model, str) or not model:
        problems.append(f'{where} の model が空でない文字列でない')
    effort = step.get('effort')
    if effort is not None and not isinstance(effort, str):
        problems.append(f'{where} の effort が文字列でも null でもない')
    until = step.get('until')
    if until is not None:
        try:
            datetime.date.fromisoformat(until)
        except (TypeError, ValueError):
            problems.append(f'{where} の until が日付 (YYYY-MM-DD) でない: {until!r}')
    extra = sorted(set(step) - STEP_KEYS)
    if extra:
        problems.append(f"{where} の余計なキー: {', '.join(extra)}")
    return problems


def load_template(home: Path, name: str) -> dict | None:
    """<home>/templates/<name>.json を読む。無い・壊れている・不正なら None。"""
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        return None
    try:
        template = json.loads((home / 'templates' / f'{name}.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if validate(template, name):
        return None
    return template


def load_all(home: Path) -> dict[str, dict]:
    """正しいテンプレートだけを name で引ける辞書にする。"""
    templates = {}
    directory = home / 'templates'
    if directory.is_dir():
        for path in sorted(directory.glob('*.json')):
            template = load_template(home, path.stem)
            if template is not None:
                templates[template['name']] = template
    return templates


def load_overrides(home: Path) -> dict:
    """写しを読む。無い・壊れていれば {"synced_at": None, "application": None}。"""
    try:
        data = json.loads((home / 'overrides.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return dict(EMPTY_OVERRIDES)
    if not isinstance(data, dict):
        return dict(EMPTY_OVERRIDES)
    return {'synced_at': data.get('synced_at'), 'application': data.get('application')}


def state_of(application: dict | None, now: datetime.datetime) -> str:
    """none / active / expired_ask / expired_drop。"""
    if application is None:
        return 'none'
    until = application.get('until')
    if until is None or now < parse_time(until):
        return 'active'
    return 'expired_drop' if application.get('on_expire') == 'drop' else 'expired_ask'


def read_state(path: str) -> dict:
    """state/current のドキュメントの data。ファイルが無ければ {}。"""
    state = Path(path)
    if not state.exists():
        return {}
    data = json.loads(state.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError(f'state がオブジェクトでない: {path}')
    return data


def new_application_id(now: datetime.datetime) -> str:
    """a- + 始めた時刻の UTC を YYYYMMDDTHHMMSS にしたもの。"""
    return 'a-' + now.astimezone(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S')


def history_set(application: dict, ended_how: str, ended_at: str) -> dict:
    """history への set の write。"""
    data = dict(application, ended_at=ended_at, ended_how=ended_how)
    return {'op': 'set', 'collection': 'history', 'doc_id': application.get('id'), 'data': data}


def state_set(application: dict | None, now: datetime.datetime) -> dict:
    """state/current への set の write。"""
    return {'op': 'set', 'collection': 'state', 'doc_id': 'current',
            'data': {'application': application, 'updated_at': now.isoformat()}}


def cmd_check(home: Path, names: list[str]) -> int:
    """1 件 1 行で出す。1 件でも ng なら 1 を返す。"""
    if names:
        targets = [(name, home / 'templates' / f'{name}.json') for name in names]
    else:
        directory = home / 'templates'
        targets = [(path.stem, path) for path in sorted(directory.glob('*.json'))] if directory.is_dir() else []
    status = 0
    for name, path in targets:
        problems = check_one(path, name)
        if problems:
            status = 1
            print(f"ng {name}: {'; '.join(problems)}")
        else:
            print(f'ok {name}')
    return status


def check_one(path: Path, name: str) -> list[str]:
    """テンプレート 1 つの問題の一覧。"""
    if not path.is_file():
        return ['ファイルが無い']
    try:
        template = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        return [f'JSON が読めない: {error}']
    return validate(template, name)


def cmd_sync(home: Path, state_path: str, now: datetime.datetime) -> dict:
    """写しを書き、{"status", "application", "writes"} を返す。"""
    application = read_state(state_path).get('application')
    if application is None:
        status, kept = 'none', None
    elif not isinstance(application, dict) or load_template(home, application.get('template')) is None:
        status, kept = 'unknown_template', application
    else:
        status = state_of(application, now)
        kept = None if status == 'expired_drop' else application
    writes = drop_writes(application, now) if status == 'expired_drop' else []
    home.mkdir(parents=True, exist_ok=True)
    overrides = {'synced_at': now.isoformat(), 'application': kept}
    (home / 'overrides.json').write_text(json.dumps(overrides, ensure_ascii=False), encoding='utf-8')
    return {'status': status, 'application': kept, 'writes': writes}


def drop_writes(application: dict, now: datetime.datetime) -> list[dict]:
    """expired_drop のときの writes (history への set と state/current への set)。"""
    ended = dict(application, ended_at=application.get('until'), ended_how='expired')
    return [
        {'op': 'set', 'collection': 'history', 'doc_id': application.get('id'), 'data': ended},
        {'op': 'set', 'collection': 'state', 'doc_id': 'current',
         'data': {'application': None, 'updated_at': now.isoformat()}},
    ]


def cmd_due(state_path: str, now: datetime.datetime, within_hours: float) -> dict:
    """until が窓の中なら {"due", "hours_left"}、それ以外は {"due": null}。"""
    application = read_state(state_path).get('application')
    if isinstance(application, dict) and application.get('until') is not None:
        until = parse_time(application['until'])
        if now < until <= now + datetime.timedelta(hours=within_hours):
            return {'due': application, 'hours_left': (until - now).total_seconds() / 3600}
    return {'due': None}


def cmd_apply(home: Path, state_path: str, name: str, until_arg: str, on_expire: str,
              reason: str, via: str, now: datetime.datetime) -> dict:
    """新しい適用の writes を出す。前の適用があれば history に移す。"""
    if load_template(home, name) is None:
        raise ValueError(f'テンプレートが無い・不正: {name}')
    if until_arg == 'none':
        until = None
    else:
        until = parse_time(until_arg)
        if now >= until:
            raise ValueError(f'until が now より後でない: {until_arg}')
    application = {'id': new_application_id(now), 'template': name,
                   'until': until.isoformat() if until is not None else None,
                   'on_expire': on_expire, 'reason': reason,
                   'started_at': now.isoformat(), 'started_via': via}
    writes = previous_writes(read_state(state_path).get('application'), now)
    writes.append(state_set(application, now))
    return {'application': application, 'writes': writes}


def previous_writes(application: dict | None, now: datetime.datetime) -> list[dict]:
    """前の適用を history に移す writes。適用が無ければ空。"""
    if not isinstance(application, dict):
        return []
    if state_of(application, now) == 'expired_drop':
        return [history_set(application, 'expired', application.get('until'))]
    return [history_set(application, 'replaced', now.isoformat())]


def cmd_answer(state_path: str, choice: str, until_arg: str | None, now: datetime.datetime) -> dict:
    """今の適用への答え (extend / stop / forever) の writes を出す。"""
    application = read_state(state_path).get('application')
    if not isinstance(application, dict):
        raise ValueError('適用が無い')
    if choice == 'stop':
        ended_how = 'declined' if state_of(application, now) == 'expired_ask' else 'stopped'
        writes = [history_set(application, ended_how, now.isoformat()), state_set(None, now)]
        return {'application': None, 'writes': writes}
    if choice == 'forever':
        answered = dict(application, until=None)
    else:
        if not until_arg:
            raise ValueError('extend には --until が要る')
        until = parse_time(until_arg)
        if now >= until:
            raise ValueError(f'--until が now より後でない: {until_arg}')
        answered = dict(application, until=until.isoformat())
    return {'application': answered, 'writes': [state_set(answered, now)]}


def cmd_push(home: Path, now: datetime.datetime, existing: str | None) -> dict:
    """テンプレートを Web に写す set と、消えた名前の delete の writes を出す。"""
    templates = load_all(home)
    writes = [{'op': 'set', 'collection': 'templates', 'doc_id': name,
               'data': dict(template, synced_at=now.isoformat())}
              for name, template in templates.items()]
    if existing:
        for name in existing_names(existing):
            if name not in templates:
                writes.append({'op': 'delete', 'collection': 'templates', 'doc_id': name})
    return {'writes': writes}


def existing_names(value: str) -> list[str]:
    """--existing NAME,... を名前の一覧にする (空白を落として重複を除く)。"""
    names = []
    for piece in value.split(','):
        name = piece.strip()
        if name and name not in names:
            names.append(name)
    return names


class Parser(argparse.ArgumentParser):
    """不正な引数は終了コード 2 と標準エラーの 1 行で止める。"""

    def error(self, message: str) -> None:
        sys.stderr.write(f'error: {message}\n')
        self.exit(2)


def build_parser() -> argparse.ArgumentParser:
    parser = Parser(prog='templates.py', description='テンプレートと適用を扱う')
    sub = parser.add_subparsers(dest='sub', required=True, metavar='<sub>')
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--home', metavar='DIR', help='home のディレクトリ (省略時は解決順で決める)')
    common.add_argument('--now', metavar='ISO', help='今の時刻 (省略時は現在時刻)')

    check = sub.add_parser('check', parents=[common], help='home のテンプレートを検査する')
    check.add_argument('names', nargs='*', metavar='NAME', help='検査するテンプレート (省略時は全部)')

    sync = sub.add_parser('sync', parents=[common], help='state/current から写しを書き、writes を出す')
    sync.add_argument('--state', required=True, metavar='FILE', help='state/current のドキュメントの data のファイル')

    due = sub.add_parser('due', parents=[common], help='期限が近い適用を返す')
    due.add_argument('--state', required=True, metavar='FILE', help='state/current のドキュメントの data のファイル')
    due.add_argument('--within-hours', type=float, default=24, metavar='HOURS', help='窓の幅 (時間、既定 24)')

    apply = sub.add_parser('apply', parents=[common], help='新しい適用を始める writes を出す')
    apply.add_argument('--state', required=True, metavar='FILE', help='state/current のドキュメントの data のファイル')
    apply.add_argument('--template', required=True, metavar='NAME', help='始めるテンプレート')
    apply.add_argument('--until', required=True, metavar='ISO|none', help='期限 (none は期限なし)')
    apply.add_argument('--on-expire', required=True, choices=('ask', 'drop'), metavar='ask|drop', help='期限切れのときの扱い')
    apply.add_argument('--reason', required=True, metavar='TEXT', help='始める理由')
    apply.add_argument('--via', choices=('chat', 'web'), default='chat', metavar='chat|web', help='始めた場所 (既定 chat)')

    answer = sub.add_parser('answer', parents=[common], help='期限切れへの答えの writes を出す')
    answer.add_argument('--state', required=True, metavar='FILE', help='state/current のドキュメントの data のファイル')
    answer.add_argument('--choice', required=True, choices=('extend', 'stop', 'forever'), metavar='extend|stop|forever', help='答えの種類')
    answer.add_argument('--until', metavar='ISO', help='extend の新しい期限')

    push = sub.add_parser('push', parents=[common], help='テンプレートを Web に写す writes を出す')
    push.add_argument('--existing', metavar='NAME,...', help='Web にある名前 (カンマ区切り)')
    return parser


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    args = build_parser().parse_args()
    try:
        now = parse_time(args.now) if args.now else datetime.datetime.now().astimezone()
        home = resolve_home(args.home)
        if args.sub == 'check':
            sys.exit(cmd_check(home, args.names))
        if args.sub == 'sync':
            result = cmd_sync(home, args.state, now)
        elif args.sub == 'due':
            result = cmd_due(args.state, now, args.within_hours)
        elif args.sub == 'apply':
            result = cmd_apply(home, args.state, args.template, args.until, args.on_expire,
                               args.reason, args.via, now)
        elif args.sub == 'answer':
            result = cmd_answer(args.state, args.choice, args.until, now)
        else:
            result = cmd_push(home, now, args.existing)
    except (OSError, ValueError) as error:
        sys.stderr.write(f'error: {error}\n')
        sys.exit(2)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
