"""セットアップ (検出・ヒアリングの記録・モデルの取り直し) のスクリプト。

  python setup_agents.py detect [--home DIR] [--json]
  python setup_agents.py record --use opencode,codex [--home DIR]      (--use "" は agent だけ)
  python setup_agents.py models [--runner R ...] [--home DIR]
  python setup_agents.py add-model --runner R --id ID [--effort E ...] [--home DIR]
  python setup_agents.py select --runner R --models ID,ID [--home DIR]   (--models "" は何も選ばない)
  python setup_agents.py defs --install home|work|none [--work-root DIR] [--home DIR]

home の agents.json に利用者が決めたこと (使う実行先・CLI の場所と版・選んだモデル・エージェントの定義の置き場) を、
models/<runner>.json に取得した一覧の控えを書く。取得の失敗は実行先ごとに閉じ込め、
取れなかった実行先は add-model で手入力できる状態にする。
defs は同梱の agents/*.md を <CLAUDE_CONFIG_DIR か ~/.claude>/agents (home) か作業ルートの .claude/agents (work) に写し、
agents.json の agent_defs に置き場・写した中身のハッシュ・写したセッションの ID を残す (none は写さずに答えだけ残す)。
関数は編集画面のサーバー (studio/serve.py) と conductor.py からも import する。
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import agents
import common
import model_fetch

AGENTS_FILE = 'agents.json'
MODELS_DIR = 'models'

# set_runner で models のキーを消すための番兵 (None は「触らない」)
REMOVE = object()

# 同梱のエージェントの定義の版。どれかの定義 (frontmatter か本文) を変えたら 1 上げる。
# 写した定義のこの値が小さければ、conductor.py start が写し直す (test_setup_agents.py が各定義の atelier-defs と照らす)
DEFS_VERSION = 1
DEFS_KEY = 'atelier-defs'
DEF_FILES = ('atelier-designer.md', 'atelier-designer-skills.md', 'atelier-reviewer.md', 'atelier-tester.md',
             'pbi-worker.md')
DEFS_SOURCE = Path(__file__).resolve().parent / 'agents'
DEFS_PLACES = ('home', 'work', 'none')
DEFS_RECORD = 'agent_defs'
# 作業ルートに写した定義をコミットに混ぜないための .git/info/exclude の行
EXCLUDE_LINE = '.claude/agents/'
# 呼び手のセッションの ID (定義を写したセッションでは、その定義がまだ読み込まれていない)
SESSION_ENV = 'CLAUDE_CODE_SESSION_ID'
# 利用者が手を入れた定義の隣に置く、同梱の新しい版の名前の後ろ
NEW_SUFFIX = '.atelier-new'
FRONTMATTER = re.compile(r'^---\n(.*?)\n---\n?(.*)$', re.S)


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
    data = manual_models(load_models(home, runner), runner, model_id, efforts)
    common.write_json(_models_path(home, runner), data)
    return data


def manual_models(data, runner, model_id, efforts) -> dict:
    """models/<runner>.json に手入力のモデルを足したあとの中身 (何も書かない。data は書き換える)。

    data は load_models の結果 (None なら新しい骨組みから作る)。add_manual_model と保存予定が共有する。
    """
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
    return data


def set_runner(home, runner, enabled=None, models=None, billing=None) -> dict:
    """agents.json のその runner だけを更新する。None の引数は触らない。

    models に [] を渡すと空にし、REMOVE を渡すとキーを消す。agent の enabled false は ValueError。
    """
    _check_runner(runner)
    if runner == common.AGENT and enabled is False:  # 読む前に判定する (旧版と同じ順)
        raise ValueError('agent cannot be disabled.')
    home = Path(home)
    table = runner_table(agents.load_agents(home), runner, enabled, models, billing)
    common.write_json(home / AGENTS_FILE, table)
    return table


def runner_table(table, runner, enabled=None, models=None, billing=None) -> dict:
    """agents.json のその runner だけを更新したあとの中身 (何も書かない。table は書き換える)。

    table は agents.load_agents の結果 (None なら空から作る)。set_runner と保存予定が共有する。
    """
    _check_runner(runner)
    if runner == common.AGENT and enabled is False:
        raise ValueError('agent cannot be disabled.')
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
        # 文字列以外が入ると、読む側 (missing_selected の集合の照合) が壊れるので書かない
        if not isinstance(models, (list, tuple)) or not all(
                isinstance(model_id, str) and model_id.strip() for model_id in models):
            raise ValueError('models must be a list of non-empty model id strings.')
        config['models'] = list(models)
    if billing is not None:
        config['billing'] = billing
    table['runners'][runner] = config
    return table


def load_models(home, runner) -> dict | None:
    """models/<runner>.json を読む。無い・壊れている・オブジェクトでないときは None。"""
    _check_runner(runner)
    data = common.load_json(_models_path(home, runner))
    return data if isinstance(data, dict) else None


# ---- エージェントの定義 ----

def read_definition(path) -> tuple[dict, str]:
    """定義のファイルの frontmatter (キー → 値の文字列) と本文。frontmatter が無ければ ({}, 全文)。"""
    text = Path(path).read_text(encoding='utf-8').replace('\r\n', '\n')
    match = FRONTMATTER.match(text)
    if match is None:
        return {}, text
    meta = {}
    for line in match.group(1).split('\n'):
        key, sep, value = line.partition(':')
        if sep and key.strip():
            meta[key.strip()] = value.strip()
    return meta, match.group(2)


def definition_version(path) -> int:
    """定義の frontmatter の atelier-defs。無い・読めない・整数でないときは 0 (どの同梱の版より古い)。"""
    try:
        value = read_definition(path)[0].get(DEFS_KEY, '')
    except (OSError, UnicodeDecodeError):
        return 0
    return int(value) if value.isdigit() else 0


def claude_home(env=None) -> Path:
    """Claude Code の設定の置き場 (CLAUDE_CONFIG_DIR、無ければ ~/.claude)。"""
    env = os.environ if env is None else env
    given = env.get('CLAUDE_CONFIG_DIR')
    return Path(given).expanduser() if given else Path.home() / '.claude'


def defs_dir(place, env=None, work_root=None) -> Path:
    """定義を置くフォルダ。home は <claude_home>/agents、work は <作業ルート>/.claude/agents。"""
    if place == 'home':
        return claude_home(env) / 'agents'
    if place == 'work':
        return Path(work_root or Path.cwd()) / '.claude' / 'agents'
    raise ValueError(f'no folder for place {place}.')


def find_definition(file, env=None, work_root=None) -> Path | None:
    """定義のファイルの場所。Claude Code と同じく作業ルートの .claude/agents を home の agents より先に見る。"""
    for place in ('work', 'home'):
        path = defs_dir(place, env, work_root) / file
        if path.is_file():
            return path
    return None


def missing_definitions(env=None, work_root=None) -> list:
    """同梱の定義のうち、どちらの置き場にも無いか、atelier-defs が DEFS_VERSION より小さいファイルの名前。

    atelier-defs の無い古い写し (例えば前の版の手順で手で写したもの) は道具や本文が古いので、無いものとして数える。
    """
    missing = []
    for file in DEF_FILES:
        path = find_definition(file, env, work_root)
        if path is None or definition_version(path) < DEFS_VERSION:
            missing.append(file)
    return missing


def file_sha256(path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _load_table(home) -> dict | None:
    """agents.json の中身 (runners の無い、定義の記録だけの agents.json も読む)。読めなければ None。"""
    data = common.load_json(Path(home) / AGENTS_FILE)
    return data if isinstance(data, dict) else None


def defs_record(home, work_root=None) -> dict | None:
    """この作業ルートに当てはまる agent_defs の記録。work の記録は、写した作業ルートでだけ当てはまる。"""
    table = _load_table(home)
    record = table.get(DEFS_RECORD) if isinstance(table, dict) else None
    if not isinstance(record, dict) or record.get('place') not in DEFS_PLACES:
        return None
    if record['place'] == 'work':
        root = Path(work_root or Path.cwd()).resolve()
        if not record.get('work_root') or Path(record['work_root']).resolve() != root:
            return None
    return record


def copied_this_session(home, env=None) -> bool:
    """agent_defs の記録が、今の呼び手のセッション (CLAUDE_CODE_SESSION_ID) で写した定義か。ID が無ければ False。"""
    env = os.environ if env is None else env
    table = _load_table(home)
    record = table.get(DEFS_RECORD) if isinstance(table, dict) else None
    session = env.get(SESSION_ENV) or ''
    return bool(session) and isinstance(record, dict) and record.get('session') == session


def exclude_file(work_root) -> Path | None:
    """作業ルートの git の info/exclude。worktree でも本体の .git の側を返す。git でなければ None。"""
    root = Path(work_root)
    try:
        done = subprocess.run(['git', '-C', str(root), 'rev-parse', '--git-path', 'info/exclude'],
                              capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30,
                              creationflags=model_fetch.NO_WINDOW)
        if done.returncode == 0 and done.stdout.strip():
            path = Path(done.stdout.strip())
            return path if path.is_absolute() else root / path
    except (OSError, subprocess.SubprocessError):
        pass
    git = root / '.git'
    return git / 'info' / 'exclude' if git.is_dir() else None


def add_exclude(work_root) -> Path | None:
    """info/exclude に .claude/agents/ の行を 1 度だけ足す。足した (か元からあった) ファイルを返す。git でなければ None。"""
    path = exclude_file(work_root)
    if path is None:
        return None
    try:
        text = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        text = ''
    if EXCLUDE_LINE in text.splitlines():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a', encoding='utf-8', newline='\n') as stream:
        stream.write(('' if not text or text.endswith('\n') else '\n') + EXCLUDE_LINE + '\n')
    return path


def install_definitions(home, place, env=None, work_root=None, now=None) -> dict:
    """同梱の定義を place に写し、agents.json の agent_defs に記録する。書いた記録を返す。

    記録: place・dir (写したフォルダ)・hashes (ファイル名 → 写した中身の sha256)・session (呼び手の
    CLAUDE_CODE_SESSION_ID、無ければ空)・work の作業ルート・時刻。none は何も写さず、hashes は空。
    """
    if place not in DEFS_PLACES:
        raise ValueError(f'unknown place: {place}. Valid places: {", ".join(DEFS_PLACES)}.')
    env = os.environ if env is None else env
    home = Path(home)
    root = Path(work_root or Path.cwd()).resolve()
    record = {'place': place, 'dir': None, 'hashes': {}, 'session': env.get(SESSION_ENV) or '',
              'at': _stamp(now)}
    if place != 'none':
        target = defs_dir(place, env, root)
        target.mkdir(parents=True, exist_ok=True)
        for file in DEF_FILES:
            if (target / file).is_file() and file_sha256(target / file) != file_sha256(DEFS_SOURCE / file):
                # 写す前の中身 (利用者の手直しや前の版) を消さずに隣へ残す
                shutil.copyfile(target / file, target / f'{file}.atelier-old')
            shutil.copyfile(DEFS_SOURCE / file, target / file)
            record['hashes'][file] = file_sha256(target / file)
        record['dir'] = target.as_posix()
    if place == 'work':
        record['work_root'] = root.as_posix()
        add_exclude(root)
    table = _load_table(home)
    if not isinstance(table, dict):
        table = {}
    table[DEFS_RECORD] = record
    common.write_json(home / AGENTS_FILE, table)
    return record


def refresh_definitions(home, env=None, work_root=None) -> list:
    """写した定義を同梱の新しい版に揃え、呼び手に出す warn の行を返す。

    写したときのハッシュと今の中身が同じで atelier-defs が DEFS_VERSION より小さい定義と、置き場から消えた定義は
    写し直す (このセッションではまだ読み込まれないので、記録の session を今のセッションにする)。
    ハッシュが違う (利用者が手を入れた) 古い定義は上書きせず、隣に <file>.atelier-new として同梱の版を置く。
    """
    env = os.environ if env is None else env
    home = Path(home)
    record = defs_record(home, work_root)
    if record is None or record.get('place') == 'none' or not record.get('dir'):
        return []
    target = Path(record['dir'])
    hashes = record.get('hashes') if isinstance(record.get('hashes'), dict) else {}
    updated, kept = [], []
    for file in DEF_FILES:
        path = target / file
        if path.is_file() and definition_version(path) >= DEFS_VERSION:
            continue
        if path.is_file() and file_sha256(path) != hashes.get(file):
            fresh = target / (file + NEW_SUFFIX)
            if file_sha256(fresh) != file_sha256(DEFS_SOURCE / file):
                shutil.copyfile(DEFS_SOURCE / file, fresh)
            kept.append(fresh.as_posix())
            continue
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(DEFS_SOURCE / file, path)
        hashes[file] = file_sha256(path)
        updated.append(file)
    lines = []
    if updated:
        record['hashes'] = hashes
        record['session'] = env.get(SESSION_ENV) or ''
        record['at'] = _stamp(None)
        table = _load_table(home)
        table = table if isinstance(table, dict) else {}
        table[DEFS_RECORD] = record
        common.write_json(home / AGENTS_FILE, table)
        lines.append(f'warn agents: updated {", ".join(updated)} (next session)')
    if kept:
        lines.append(f'warn agents: kept your edited definitions; the bundled ones are at {", ".join(kept)}')
    return lines


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


def _cmd_defs(args) -> None:
    record = install_definitions(common.resolve_home(args.home), args.install, work_root=args.work_root,
                                 now=_now(args.now))
    if record['place'] == 'none':
        print('defs none: nothing copied; the designer runs as a headless claude')
        return
    print(f'defs {record["place"]}: {record["dir"]} ({len(record["hashes"])} files)')
    if record['place'] == 'work' and exclude_file(record['work_root']) is None:
        print(f'warn defs: {record["work_root"]} is not a git work tree; {EXCLUDE_LINE} is not excluded')


class Parser(argparse.ArgumentParser):
    """不正な引数は終了コード 2 と標準エラーの 1 行で止める。"""

    def error(self, message: str) -> None:
        sys.stderr.write(f'error: {message}\n')
        self.exit(2)


def build_parser() -> argparse.ArgumentParser:
    parser = Parser(prog='setup_agents.py', description='Detect runners and fetch their models.')
    sub = parser.add_subparsers(dest='sub', required=True, metavar='<command>')
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument('--home', metavar='DIR', help='home directory (default: ATELIER_HOME, then ~/.config/atelier)')
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

    defs_cmd = sub.add_parser('defs', parents=[shared], help='copy the agent definitions and record where')
    defs_cmd.add_argument('--install', required=True, choices=DEFS_PLACES,
                          help='home: ~/.claude/agents, work: the work root .claude/agents, none: copy nothing')
    defs_cmd.add_argument('--work-root', metavar='DIR', help='work root (default: the current directory)')
    defs_cmd.set_defaults(handler=_cmd_defs)
    return parser


def main(argv=None) -> int:
    common.utf8_stdout()
    args = build_parser().parse_args(argv)
    try:
        args.handler(args)
    except (ValueError, OSError) as error:
        sys.stderr.write(f'error: {error}\n')
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
