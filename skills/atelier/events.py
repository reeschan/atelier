"""atelier のイベントの口。許可リストで点検して `<dir>/events.jsonl` に書き、送り手 (harness-log) があれば渡す。

どの失敗も呼び出し側へ例外も出力も漏らさない。送り手が無くても手元の行は点検して書く。
契約は docs/specs/harness-metrics.md の「atelier のイベント (atelier.event/1)」「atelier への組み込み」と
末尾の追記 §3.1・§3.7。
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import importlib.util
import io
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA_PATH = HERE / 'harness.schema.json'
CHANGELOG = HERE / 'CHANGELOG.md'
LINE_SCHEMA = 'atelier.event/1'
ID_RE = re.compile(r'^[A-Za-z0-9._:/@-]{1,64}\Z')
NAME_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}\Z')
VERSION_RE = re.compile(r'^##\s+(?:.*?\s)?(v\d+(?:\.\d+)*)(?:\s|$)')

# 外枠の任意キー。どのスキルにも共通の表 (atelier の許可リストには書かない)。
ENVELOPE_SPECS = {
    'split': {'type': 'enum', 'values': ['train', 'heldout', 'regression', 'unsplit']},
    'split_set_id': {'type': 'id'},
    'change_id': {'type': 'id'},
    'config_fp': {'type': 'id'},
    'trial': {'type': 'int', 'min': 0},
    'unit_id': {'type': 'id'},
}

# 委ねるサブコマンドと、その実装のモジュール。profile/backfill は同じ task_profile.py。
DELEGATES = {
    'profile': 'task_profile',
    'backfill': 'task_profile',
    'split': 'splits',
    'change': 'changes',
    'leakcheck': 'leakcheck',
    'ingest': 'ingest',
    'failure-embed': 'failures',
}

_UNSET = object()
_DROP = object()
_SCHEMA_CACHE: dict = {}
_SENDER_CACHE: dict = {}


def _reset_caches() -> None:
    """許可リストと送り手のキャッシュを捨てる (テストが環境変数を差し替えるため)。"""
    _SCHEMA_CACHE.clear()
    _SENDER_CACHE.clear()


# ---------- 許可リストと点検 ----------

def _load_schema(path: Path = SCHEMA_PATH) -> dict:
    path = Path(path)
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        key = (str(path), None)
    cached = _SCHEMA_CACHE.get(key)
    if isinstance(cached, dict):
        return cached
    raw = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(raw, dict) or not isinstance(raw.get('types'), dict):
        raise ValueError('allowlist: bad file')
    _SCHEMA_CACHE.clear()
    _SCHEMA_CACHE[key] = raw
    return raw


def _safe_name(name) -> str:
    return name if isinstance(name, str) and NAME_RE.match(name) else '?'


def _check_value(spec, value, name: str, dropped: list):
    """1 つの値を点検する。通せば値 (hash も元の値のまま) を、通せなければ _DROP を返す。"""
    if value is None:
        return None
    t = spec.get('type') if isinstance(spec, dict) else None
    if t == 'int':
        ok = isinstance(value, int) and not isinstance(value, bool) and value >= spec.get('min', value)
        return value if ok else _DROP
    if t == 'number':
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        return value if ok else _DROP
    if t == 'bool':
        return value if isinstance(value, bool) else _DROP
    if t == 'id':
        return value if isinstance(value, str) and ID_RE.match(value) else _DROP
    if t == 'hash':
        # 元の値の長さは問わない (改行を含む・空のものだけ落とす)。長さはハッシュの出力で見る。
        if not isinstance(value, str) or not value or '\n' in value or '\r' in value:
            return _DROP
        digest = 'h:' + hashlib.sha256(value.encode('utf-8')).hexdigest()[:12]
        return value if ID_RE.match(digest) else _DROP
    if t == 'enum':
        values = spec.get('values')
        return value if isinstance(value, str) and isinstance(values, list) and value in values else _DROP
    if t == 'object':
        if not isinstance(value, dict):
            return _DROP
        return _check_fields(spec.get('fields') or {}, value, name + '.', dropped)
    if t == 'map':
        if not isinstance(value, dict):
            return _DROP
        out = {}
        for k, v in value.items():
            kk = _check_value(spec.get('keys') or {'type': 'id'}, k, name + '.*', dropped)
            vv = _DROP if kk is _DROP or kk is None else _check_value(spec.get('values'), v, name + '.*', dropped)
            if vv is _DROP:
                dropped.append(name + '.*')
            else:
                out[kk] = vv
        return out
    if t == 'list':
        if not isinstance(value, list):
            return _DROP
        cap = spec.get('max', 20)
        out = []
        for item in value:
            r = _check_value(spec.get('items'), item, name + '[]', dropped)
            if r is _DROP or len(out) >= cap:
                dropped.append(name + '[]')
            else:
                out.append(r)
        return out
    return _DROP


def _check_fields(fields, data, prefix: str, dropped: list) -> dict:
    out = {}
    for k, v in data.items():
        nm = prefix + _safe_name(k)
        spec = fields.get(k) if isinstance(k, str) else None
        r = _DROP if spec is None else _check_value(spec, v, nm, dropped)
        if r is _DROP:
            dropped.append(nm)
        else:
            out[k] = r
    return out


def check(schema: dict, type: str, data: dict):
    """許可リストで data を点検し (通った data, 落とした名前) を返す。未知の type は ValueError。"""
    types = schema.get('types') if isinstance(schema, dict) else None
    entry = types.get(type) if isinstance(types, dict) and isinstance(type, str) else None
    if not isinstance(entry, dict):
        raise ValueError('unknown type')
    if not isinstance(data, dict):
        raise ValueError('data must be an object')
    dropped: list = []
    clean = _check_fields(entry.get('fields') or {}, data, '', dropped)
    return clean, list(dict.fromkeys(dropped))


def _enum_values(type: str, field: str) -> list:
    try:
        spec = _load_schema(SCHEMA_PATH)['types'][type]['fields'][field]
    except (KeyError, TypeError, OSError, ValueError):
        return []
    values = spec.get('values') if isinstance(spec, dict) else None
    return list(values) if isinstance(values, list) else []


# ---------- 結び付けキーと once ----------

def _run_keys(base: Path):
    """<base>/conductor/run.json の run_id と seq。無い・読めない・型が違うなら (None, None)。"""
    path = Path(base) / 'conductor' / 'run.json'
    if not path.is_file():
        return None, None
    for attempt in range(3):
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(raw, dict):
                return None, None
            run_id = raw.get('run_id')
            seq = raw.get('seq')
            run_id = run_id if isinstance(run_id, str) and run_id else None
            seq = seq if isinstance(seq, int) and not isinstance(seq, bool) else None
            return run_id, seq
        except OSError:
            if attempt < 2:
                time.sleep(0.02)
        except (ValueError, TypeError):
            return None, None
    return None, None


run_keys = _run_keys  # 呼び出し側が、行を書いた時点のキーを先に取っておくための公開名


def _once_key(once):
    if not isinstance(once, str) or not once:
        return None
    if '\n' in once or '\r' in once:
        return 'sha256:' + hashlib.sha256(once.encode('utf-8')).hexdigest()
    return once


def _once_path(base: Path) -> Path:
    return Path(base) / '.events' / 'seen.txt'


def _once_seen(base: Path, key: str) -> bool:
    try:
        return key in set(_once_path(base).read_text(encoding='utf-8').splitlines())
    except OSError:
        return False


def _once_mark(base: Path, key: str) -> None:
    path = _once_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a', encoding='utf-8', newline='\n') as h:
        h.write(key + '\n')


# ---------- 送り手 ----------

def _sender_path():
    """HARNESS_LOG_PATH (ファイルかフォルダ) → 隣の ../harness-log → ~/.claude/skills/harness-log。"""
    given = os.environ.get('HARNESS_LOG_PATH')
    if given:
        p = Path(given)
        if p.is_file():
            return p
        cand = p / 'harness_log.py'
        return cand if cand.is_file() else None  # 指定された場所に無ければ手元だけ (隣へ戻らない)
    for cand in (HERE.parent / 'harness-log' / 'harness_log.py',
                 Path.home() / '.claude' / 'skills' / 'harness-log' / 'harness_log.py'):
        if cand.is_file():
            return cand
    return None


def _sender(base):
    path = _sender_path()
    if path is None:
        return None
    key = str(path)
    if key in _SENDER_CACHE:
        return _SENDER_CACHE[key]
    _SENDER_CACHE[key] = None  # 読み込み中の再入を止める
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            name = '_atelier_sender_' + hashlib.sha256(key.encode('utf-8')).hexdigest()[:12]
            spec = importlib.util.spec_from_file_location(name, path)
            if spec is None or spec.loader is None:
                raise ImportError('cannot load sender')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
    except (Exception, SystemExit) as e:
        _log_error(base, 'sender', e)
        return None
    _SENDER_CACHE[key] = module
    return module


def sender_setup_line(base):
    """送り手があって設定がまだ無いとき、呼び出し側にユーザーへ聞かせる 1 行を返す。それ以外は None。
    答えは送り手の init (--remote <URL>・--local・--off) で、どれを選んでも設定ができるので 1 回だけになる。"""
    try:
        module = _sender(base)
        if module is None or module.load_config() is not None:
            return None
        return (f'ask harness-log: no destination set; ask the user once and run '
                f'python {Path(module.__file__).as_posix()} init --remote <URL> | --local | --off '
                '(appendix/events.md, ## First run)')
    except Exception as e:  # 聞く行が出せなくても start は止めない
        _log_error(base, 'sender_setup_line', e)
        return None


def _send(base, name: str, **kwargs):
    module = _sender(base)
    if module is None:
        return None
    fn = getattr(module, name, None)
    if not callable(fn):
        return None
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return fn(**kwargs)
    except (Exception, SystemExit) as e:
        _log_error(base, name, e)
        return None


class _FlushFailed(Exception):
    """送り手の flush が失敗を返したことを errors.log に残すための目印 (値は持たない)。"""


def _flush(base):
    """送り手の flush を呼ぶ。送っていない設定なら何もせず、失敗のときは errors.log に 1 行残す。

    失敗しても外に投げない。未送の行は送り手が持ち越すので、次の flush が送り直す。
    """
    module = _sender(base)
    if module is None:
        return None
    fn = getattr(module, 'flush', None)
    if not callable(fn):
        return None
    load = getattr(module, 'load_config', None)
    if callable(load):
        try:
            cfg = load()
        except (Exception, SystemExit) as e:
            _log_error(base, 'flush.config', e)
            return None
        # 設定が無い・止めている・送り先が無い、は失敗ではないので黙って戻る。
        if not isinstance(cfg, dict) or cfg.get('enabled') is not True or not cfg.get('remote'):
            return None
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = fn()
    except (Exception, SystemExit) as e:
        _log_error(base, 'flush', e)
        return False
    if result is False:
        _log_error(base, 'flush', _FlushFailed())
        return False
    return result


def _log_error(base, where: str, exc: BaseException, type_name=None) -> None:
    """<base>/.events/errors.log に 1 行。例外の型名だけを書き、値やメッセージは書かない。"""
    try:
        if base is None:
            return
        folder = Path(base) / '.events'
        folder.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        parts = [ts, str(where)[:64], exc.__class__.__name__]
        if type_name is not None:
            clean_type = str(type_name).replace('\n', ' ').replace('\r', ' ')[:64]
            parts.append('type=' + clean_type)
        with open(folder / 'errors.log', 'a', encoding='utf-8', newline='\n') as h:
            h.write('\t'.join(parts) + '\n')
    except (Exception, SystemExit):
        pass


def _peer(name: str):
    """同じフォルダの (後の PBI が作る) モジュール。無ければ None。失敗しても出力しない。"""
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        pass
    except (Exception, SystemExit):
        return None
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
        try:
            return importlib.import_module(name)
        except (Exception, SystemExit):
            return None
    return None


# ---------- 外枠の任意キー ----------

def _auto_envelope(base: Path, data: dict) -> dict:
    env = {}
    changes = _peer('changes')
    if changes is not None:
        fn = getattr(changes, 'current_envelope', None)
        if callable(fn):
            try:
                got = fn(base)
            except (Exception, SystemExit) as e:
                _log_error(base, 'changes.current_envelope', e)
                got = None
            if isinstance(got, dict):
                for k in ('change_id', 'config_fp'):
                    if got.get(k) is not None:
                        env[k] = got[k]
    trial = os.environ.get('HARNESS_TRIAL')
    if trial is not None and trial.strip() != '':
        try:
            env['trial'] = int(trial.strip())
        except ValueError:
            pass
    case_id = data.get('case_id') if isinstance(data, dict) else None
    if isinstance(case_id, str) and case_id:
        splits = _peer('splits')
        fn = getattr(splits, 'split_of', None) if splits is not None else None
        if callable(fn):
            try:
                got = fn(case_id)
            except (Exception, SystemExit) as e:
                _log_error(base, 'splits.split_of', e)
                got = None
            if isinstance(got, (tuple, list)) and len(got) == 2:
                env['split'] = got[0]
                env['split_set_id'] = got[1]
    return env


def _check_envelope(envelope) -> tuple:
    out = {}
    dropped = []
    if envelope is None:
        return out, dropped
    if not isinstance(envelope, dict):
        return out, ['envelope']
    for k, v in envelope.items():
        spec = ENVELOPE_SPECS.get(k) if isinstance(k, str) else None
        if spec is None:
            dropped.append('envelope.' + _safe_name(k))
            continue
        r = _check_value(spec, v, '', [])
        if r is _DROP:
            dropped.append('envelope.' + k)
        elif r is not None:
            out[k] = r
    return out, dropped


# ---------- 公開の口 ----------

def emit(dir, type: str, data: dict, *, stage=None, pbi=None, once=None, envelope=None,
         run_id=_UNSET, seq=_UNSET) -> bool:
    """イベントを 1 件出す。手元の行を書いたら True。例外を投げず、何も print しない。"""
    base = Path(dir) if dir is not None else None
    try:
        if base is None or not base.is_dir():
            return False
        schema = _load_schema(SCHEMA_PATH)
        if not isinstance(data, dict):
            return False
        clean, dropped = check(schema, type, data)
        once_key = _once_key(once) if once is not None else None
        if once_key is not None and _once_seen(base, once_key):
            return False
        if run_id is _UNSET or seq is _UNSET:
            auto_run_id, auto_seq = _run_keys(base)
            if run_id is _UNSET:
                run_id = auto_run_id
            if seq is _UNSET:
                seq = auto_seq
        run_id = run_id if isinstance(run_id, str) and run_id else None
        seq = seq if isinstance(seq, int) and not isinstance(seq, bool) else None
        merged = _auto_envelope(base, clean)
        if isinstance(envelope, dict):
            merged.update(envelope)  # 明示の値が自動の値に勝つ
        elif envelope is not None:
            dropped.append('envelope')
        env, env_dropped = _check_envelope(merged)
        dropped = list(dict.fromkeys([*dropped, *env_dropped]))
        line = {
            'schema': LINE_SCHEMA,
            'ts': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'run_id': run_id,
            'seq': seq,
            'stage': stage if isinstance(stage, str) else None,
            'pbi': pbi if isinstance(pbi, str) else None,
        }
        line.update(env)
        line['type'] = type
        line['data'] = clean
        if dropped:
            line['dropped'] = dropped
        text = json.dumps(line, ensure_ascii=False, separators=(',', ':')) + '\n'
        with open(base / 'events.jsonl', 'a', encoding='utf-8', newline='\n') as h:
            h.write(text)
        if once_key is not None:
            try:
                _once_mark(base, once_key)
            except OSError:
                pass
        kwargs = {
            'schema_path': SCHEMA_PATH,
            'type': type,
            'data': clean,
            'run_id': run_id,
            'seq': seq,
            'stage': line['stage'],
            'pbi': line['pbi'],
            'skill_version': skill_version(),
        }
        if env:
            kwargs['envelope'] = env
        _send(base, 'emit', **kwargs)
        return True
    except (Exception, SystemExit) as e:
        _log_error(base, 'emit', e, type)
        return False


def skill_version(changelog: Path = CHANGELOG) -> str:
    """atelier の版。同じフォルダの ACTIVE → CHANGELOG の最初の `## v<版>` 見出し → unreleased。"""
    try:
        path = Path(changelog)
        active = path.parent / 'ACTIVE'
        if active.is_file():
            value = active.read_text(encoding='utf-8').strip()
            if re.match(r'^v\d+(?:\.\d+)*\Z', value):
                return value
        for line in path.read_text(encoding='utf-8').splitlines():
            m = VERSION_RE.match(line)
            if m:
                return m.group(1)
    except (Exception, SystemExit):
        pass
    return 'unreleased'


# ---------- CLI ----------

def _usage(message: str) -> int:
    print('events: ' + message, file=sys.stderr)
    return 2


def _cmd_stage(args) -> int:
    if args.decision not in _enum_values('stage.decided', 'decision'):
        return _usage(f'bad --decision: {args.decision}')
    if args.by not in _enum_values('stage.decided', 'by'):
        return _usage(f'bad --by: {args.by}')
    if args.reason is not None and args.reason not in _enum_values('stage.decided', 'reason'):
        return _usage(f'bad --reason: {args.reason}')
    if not (isinstance(args.component, str) and ID_RE.match(args.component)):
        return _usage(f'bad --component: {args.component}')
    for value in (args.stage, args.pbi):
        if value is not None and not (isinstance(value, str) and ID_RE.match(value)):
            return _usage(f'bad id: {value}')
    data = {'component': args.component, 'decision': args.decision, 'by': args.by}
    if args.reason is not None:
        data['reason'] = args.reason
    emit(args.dir, 'stage.decided', data, stage=args.stage, pbi=args.pbi)
    return 0


def _cmd_answer(args) -> int:
    if args.kind not in _enum_values('question.answered', 'kind'):
        return _usage(f'bad --kind: {args.kind}')
    if args.topic not in _enum_values('question.answered', 'topic'):
        return _usage(f'bad --topic: {args.topic}')
    if args.choice is not None and args.choice not in _enum_values('question.answered', 'choice'):
        return _usage(f'bad --choice: {args.choice}')
    data = {'kind': args.kind, 'topic': args.topic, 'choice': args.choice,
            'wait_s': args.wait_s, 'revise': bool(args.revise)}
    emit(args.dir, 'question.answered', data)
    return 0


def _read_run(base: Path) -> dict:
    path = Path(base) / 'conductor' / 'run.json'
    if not path.is_file():
        return {}
    for attempt in range(3):
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
            return raw if isinstance(raw, dict) else {}
        except OSError:
            if attempt < 2:
                time.sleep(0.02)
        except (ValueError, TypeError):
            return {}
    return {}


def _episode_of(run: dict, seq: int):
    for row in reversed(run.get('history') or []):
        if isinstance(row, dict) and row.get('seq') == seq:
            episode = row.get('episode')
            return episode if isinstance(episode, str) else None
    running = run.get('running') or {}
    episode = running.get('episode') if isinstance(running, dict) else None
    return episode if isinstance(episode, str) else None


def _cmd_settle(args) -> int:
    base = Path(args.dir)
    run = _read_run(base)
    seq = args.seq
    episode = _episode_of(run, seq)
    method, roles = 'unmeasured', {}
    try:
        agent_stats = _peer('agent_stats')
        fn = getattr(agent_stats, 'episode_cost', None) if agent_stats is not None else None
        if callable(fn):
            got = fn(run, seq)
            if isinstance(got, dict):
                m = got.get('method')
                method = m if m in ('transcript', 'headless', 'unmeasured') else 'unmeasured'
                if isinstance(got.get('roles'), dict):
                    roles = got['roles']
    except (Exception, SystemExit) as e:
        _log_error(base, 'settle.episode_cost', e)
        method, roles = 'unmeasured', {}
    run_id = run.get('run_id') if isinstance(run.get('run_id'), str) else None
    emit(base, 'episode.cost', {'episode': episode, 'method': method, 'roles': roles},
         seq=seq, run_id=run_id, once=f'episode.cost:{run_id}:{seq}')
    _flush(base)
    return 0


def _delegated_failure(command: str, exc: BaseException) -> int:
    """委ねた main が落ちたときの終了コード。leakcheck だけは失敗を pass に見せない。"""
    if isinstance(exc, SystemExit) and isinstance(exc.code, int):
        return exc.code
    if command == 'leakcheck':
        print(f'events: leakcheck failed: {exc.__class__.__name__}', file=sys.stderr)
        return 2
    if isinstance(exc, SystemExit):
        return 0 if exc.code is None else 2
    return 0


def _delegate(command: str, rest: list) -> int:
    module_name = DELEGATES.get(command)
    if module_name is None:
        return 2
    module = _peer(module_name)
    fn = getattr(module, 'main', None) if module is not None else None
    if not callable(fn):
        print(f'events: {module_name}.py not found', file=sys.stderr)
        return 2
    # profile/backfill は同じモジュールなので、どちらかを先頭に付けて渡す。ほかは残りだけを渡す。
    argv = ([command] if module_name == 'task_profile' else []) + list(rest)
    try:
        rc = fn(argv)
    except (Exception, SystemExit) as e:
        return _delegated_failure(command, e)
    return rc if isinstance(rc, int) else 0


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog='events.py', description='atelier のイベントを出す')
    sub = ap.add_subparsers(dest='cmd')
    stage = sub.add_parser('stage')
    stage.add_argument('--dir', required=True)
    stage.add_argument('--component', required=True)
    stage.add_argument('--decision', required=True)
    stage.add_argument('--by', required=True)
    stage.add_argument('--reason')
    stage.add_argument('--stage')
    stage.add_argument('--pbi')
    answer = sub.add_parser('answer')
    answer.add_argument('--dir', required=True)
    answer.add_argument('--kind', required=True)
    answer.add_argument('--topic', required=True)
    answer.add_argument('--choice')
    answer.add_argument('--wait-s', type=int)
    answer.add_argument('--revise', action='store_true')
    settle = sub.add_parser('settle')
    settle.add_argument('--dir', required=True)
    settle.add_argument('--seq', type=int, required=True)
    return ap


def main(argv=None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    # 委ねるコマンドは argparse に通さない (REMAINDER はサブコマンドの後ろの --dir を取りこぼす)。
    if args_list and args_list[0] in DELEGATES:
        command = args_list[0]
        try:
            return _delegate(command, args_list[1:])
        except (Exception, SystemExit) as e:
            if command == 'leakcheck':
                return _delegated_failure(command, e)
            return 0
    try:
        args = _build_parser().parse_args(args_list)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2
    if args.cmd is None:
        return 0
    try:
        if args.cmd == 'stage':
            return _cmd_stage(args)
        if args.cmd == 'answer':
            return _cmd_answer(args)
        if args.cmd == 'settle':
            return _cmd_settle(args)
        return 0
    except (Exception, SystemExit):
        return 0


if __name__ == '__main__':
    sys.exit(main())
