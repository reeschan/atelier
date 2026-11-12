#!/usr/bin/env python3
"""harness-log: スキルのイベントを許可リストで点検し、外枠 (harness.event/1) を付けて手元のスプールにため、
enable のときだけ flush で private のリポジトリへ commit と push をする送り手。標準ライブラリと git だけを使う。

関数: load_config・check・emit・flush・preview・keyed_hash。CLI: init・emit・flush・preview・enable・disable・status・hash。
emit と flush は例外を外に投げない。flush の失敗は標準エラーに 1 行で、CLI の終了コードは 0。
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ENVELOPE = 'harness.event/1'
PHASES = ('design', 'implement', 'review', 'test', 'release', 'ops', 'other')
# 末尾は \Z (`$` は最後の改行の直前にも合うので、改行で終わる値が通ってしまう)
ID_RE = re.compile(r'^[A-Za-z0-9._:/@-]{1,64}\Z')
FILE_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z')  # パスに入る値 (run_id・skill)
NAME_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}\Z')  # dropped に入れてよい項目名
REPO_RE = re.compile(r'^[a-z0-9._-]+/[a-z0-9._-]+\Z')
CONFIG_ENV = 'HARNESS_LOG_CONFIG'
DEFAULT_CONFIG = '~/.claude/harness-log.json'
DEFAULT_SPOOL = '~/.claude/harness-log/spool'
DEFAULT_CLONE = '~/.claude/harness-log/clone'
EMBEDDING_DEFAULTS = {
    'url': None, 'model': 'qwen3-embedding:0.6b', 'provider': 'ollama',
    'dims': None, 'normalize': True, 'prefix': '', 'timeout': 10,
}
LOCK_STALE_SECONDS = 600
MODES = ('remote', 'local', 'off')  # 送る / 手元にだけためる / 記録しない
COVERAGE_AXES = ('task_type', 'domain', 'size')
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
GIT_IDENT = ['-c', 'user.name=harness-log', '-c', 'user.email=harness-log@localhost', '-c', 'commit.gpgsign=false']

# 外枠の任意キー。どのスキルにも共通 (スキルの許可リストには書かない)。None・欠けは行に出さない。
OPTIONAL_KEYS = {
    'split': {'type': 'enum', 'values': ['train', 'heldout', 'regression', 'unsplit']},
    'split_set_id': {'type': 'id'},
    'change_id': {'type': 'id'},
    'config_fp': {'type': 'id'},
    'trial': {'type': 'int', 'min': 0},
    'unit_id': {'type': 'id'},
}

# 外枠の項目と意味。行のキーの順と SCHEMA.md の表は、この 1 つから作る。
ENVELOPE_FIELDS = (
    ('schema', '`harness.event/1`'),
    ('ts', 'UTC の ISO 8601 (秒まで)'),
    ('phase', '開発段階: ' + '・'.join(PHASES)),
    ('skill', '送ったスキル'),
    ('skill_version', 'スキルの版'),
    ('data_schema', '`data` の版'),
    ('repo', '`show_repos` に合えば `owner/name`、合わなければ `h:<12 桁>`、remote が無ければ `none`'),
    ('branch', '常に `h:<12 桁>`'),
    ('host', '端末の名前の `h:<12 桁>`'),
    ('run_id', '結び付けキー。無ければ null'),
    ('seq', '結び付けキー。無ければ null'),
    ('stage', '結び付けキー。無ければ null'),
    ('pbi', '結び付けキー。無ければ null'),
    ('split', '任意。評価の分割 (train・heldout・regression・unsplit)'),
    ('split_set_id', '任意。分割の版 (id)'),
    ('change_id', '任意。このとき有効な変更 (id)'),
    ('config_fp', '任意。構成の指紋 (id)'),
    ('trial', '任意。同じ条件での試行番号 (0 以上の整数)'),
    ('unit_id', '任意。評価の単位 (id)'),
    ('type', 'イベントの種類'),
    ('data', '許可リストで点検した中身'),
    ('dropped', '落とした項目の名前 (値は入れない)。無ければ省く'),
)

_DROP = object()


# ---------- 設定 ----------

def config_path() -> Path:
    return Path(os.path.expanduser(os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG))


def _read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def load_config(path=None) -> dict | None:
    """設定を読み、欠けた項目を既定で補った dict を返す。無い・読めなければ None。知らない項目は残す。"""
    try:
        raw = _read_json(Path(path) if path else config_path())
        if not isinstance(raw, dict):
            return None
        cfg = dict(raw)
        # mode の無い古い設定は remote (送り先のある設定) として読む
        mode = raw.get('mode')
        cfg['mode'] = mode if mode in MODES else ('remote' if raw.get('remote') else 'local')
        cfg['enabled'] = raw.get('enabled') is True and cfg['mode'] == 'remote'
        repos = raw.get('show_repos')
        cfg['show_repos'] = [r for r in repos if isinstance(r, str)] if isinstance(repos, list) else []
        cfg['spool_dir'] = os.path.expanduser(raw.get('spool_dir') or DEFAULT_SPOOL)
        cfg['clone_dir'] = os.path.expanduser(raw.get('clone_dir') or DEFAULT_CLONE)
        emb = dict(raw['embedding']) if isinstance(raw.get('embedding'), dict) else {}
        for k, v in EMBEDDING_DEFAULTS.items():
            emb.setdefault(k, v)
        cfg['embedding'] = emb
        return cfg
    except Exception:
        return None


def _save_config(path: Path, cfg: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with open(tmp, 'w', encoding='utf-8', newline='\n') as h:
        h.write(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n')
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _key(cfg) -> str | None:
    k = cfg.get('key') if cfg else None
    return k if isinstance(k, str) and k else None


# ---------- ハッシュ ----------

def keyed_hash(key: str, value: str) -> str:
    """HMAC-SHA256 の先頭 12 桁に `h:` を付けて返す。"""
    return 'h:' + hmac.new(key.encode('utf-8'), str(value).encode('utf-8'), hashlib.sha256).hexdigest()[:12]


# ---------- 許可リストと点検 ----------

def _safe_name(name) -> str:
    return name if isinstance(name, str) and NAME_RE.match(name) else '?'


def _check_value(spec, value, key, name, dropped):
    """spec に合えば (hash 型は key があれば置き換えた) 値を、合わなければ _DROP を返す。"""
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
        # 先にハッシュし、確かめるのはハッシュの出力だけ (元の値は空でなく改行の無い文字列なら長さを問わない)
        if not (isinstance(value, str) and value and '\n' not in value and '\r' not in value):
            return _DROP
        if not key:
            return value
        hashed = keyed_hash(key, value)
        return hashed if ID_RE.match(hashed) else _DROP
    if t == 'enum':
        vals = spec.get('values')
        return value if isinstance(value, str) and isinstance(vals, list) and value in vals else _DROP
    if t == 'object':
        if not isinstance(value, dict):
            return _DROP
        return _check_fields(spec.get('fields') or {}, value, key, name + '.', dropped)
    if t == 'map':
        if not isinstance(value, dict):
            return _DROP
        out = {}
        for k, v in value.items():
            kk = _check_value(spec.get('keys') or {'type': 'id'}, k, key, name + '.*', dropped)
            vv = _DROP if kk is _DROP or kk is None else _check_value(spec.get('values'), v, key, name + '.*', dropped)
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
            r = _check_value(spec.get('items'), item, key, name + '[]', dropped)
            if r is _DROP or len(out) >= cap:
                dropped.append(name + '[]')
            else:
                out.append(r)
        return out
    return _DROP


def _check_fields(fields, data, key, prefix, dropped):
    out = {}
    for k, v in data.items():
        nm = prefix + _safe_name(k)
        spec = fields.get(k) if isinstance(k, str) else None
        r = _DROP if spec is None else _check_value(spec, v, key, nm, dropped)
        if r is _DROP:
            dropped.append(nm)
        else:
            out[k] = r
    return out


def _check(schema, type_, data, key):
    types = schema.get('types') if isinstance(schema, dict) else None
    entry = types.get(type_) if isinstance(types, dict) and isinstance(type_, str) else None
    if not isinstance(entry, dict):
        raise ValueError(f'unknown type: {_safe_name(type_)}')
    if not isinstance(data, dict):
        raise ValueError('data must be an object')
    dropped: list[str] = []
    clean = _check_fields(entry.get('fields') or {}, data, key, '', dropped)
    return clean, list(dict.fromkeys(dropped))


def check(schema: dict, type: str, data: dict) -> tuple[dict, list[str]]:
    """許可リストで data を点検し (通った data, 落とした項目の名前) を返す。hash 型は元の値のまま (鍵が無いので emit でハッシュする)。
    未知の type か data が dict でなければ ValueError。"""
    return _check(schema, type, data, None)


def _load_schema(path: Path) -> dict:
    s = _read_json(path)
    if not isinstance(s, dict):
        raise ValueError('allowlist is not a JSON object')
    if not (isinstance(s.get('skill'), str) and FILE_RE.match(s['skill'])):
        raise ValueError('allowlist: bad skill')
    if s.get('phase') not in PHASES:
        raise ValueError('allowlist: bad phase')
    if not (isinstance(s.get('data_schema'), str) and ID_RE.match(s['data_schema'])):
        raise ValueError('allowlist: bad data_schema')
    if not isinstance(s.get('types'), dict):
        raise ValueError('allowlist: types must be an object')
    return s


# ---------- git ----------

def _git(args, cwd=None, timeout=60) -> subprocess.CompletedProcess:
    cmd = ['git', '-c', 'core.autocrlf=false', *[str(a) for a in args]]
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0')
    try:
        return subprocess.run(cmd, cwd=str(cwd) if cwd else None, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, encoding='utf-8', errors='replace', timeout=timeout, env=env,
                              creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(cmd, 127, '', str(e))


def _redact(text: str) -> str:
    line = next((ln.strip() for ln in (text or '').splitlines() if ln.strip()), '')
    return re.sub(r'://[^/@\s]*@', '://***@', line)[:200]


def _say(err, msg: str) -> None:
    try:
        print('harness-log: ' + _redact(msg), file=err or sys.stderr)
    except Exception:
        pass


def _owner_name(url: str) -> str | None:
    u = url.strip().replace('\\', '/').rstrip('/')
    if u.endswith('.git'):
        u = u[:-4]
    if '://' in u:
        rest = u.split('://', 1)[1]
        path = rest.split('/', 1)[1] if '/' in rest else ''
    elif re.match(r'^[^/]+@[^/:]+:', u):
        path = u.split(':', 1)[1]
    else:
        path = u
    parts = [p for p in path.split('/') if p]
    if len(parts) < 2:
        return None
    name = f'{parts[-2]}/{parts[-1]}'.lower()
    return name if REPO_RE.match(name) else None


def _repo_value(cfg, key, cwd) -> str:
    r = _git(['remote', 'get-url', 'origin'], cwd)
    url = r.stdout.strip() if r.returncode == 0 else ''
    if not url:
        return 'none'
    name = _owner_name(url)
    if name is None:
        return keyed_hash(key, url)
    if any(fnmatch.fnmatchcase(name, p.lower()) for p in cfg['show_repos']):
        return name
    return keyed_hash(key, name)


def _branch_value(key, cwd) -> str:
    r = _git(['symbolic-ref', '--short', 'HEAD'], cwd)
    if r.returncode != 0:
        r = _git(['rev-parse', '--abbrev-ref', 'HEAD'], cwd)
    return keyed_hash(key, r.stdout.strip() if r.returncode == 0 else '')


# ---------- emit ----------

def _log_error(cfg, msg: str) -> None:
    try:
        spool = Path(cfg['spool_dir'])
        spool.mkdir(parents=True, exist_ok=True)
        with open(spool / 'errors.log', 'a', encoding='utf-8', newline='\n') as h:
            h.write(f'{datetime.now(timezone.utc).replace(microsecond=0).isoformat()} {_redact(msg)}\n')
    except Exception:
        pass


def _head_value(value, ok, name, dropped):
    if value is None:
        return None
    if ok(value):
        return value
    dropped.append(name)
    return None


def _envelope_extra(envelope, dropped) -> dict:
    out = {}
    if envelope is None:
        return out
    if not isinstance(envelope, dict):
        dropped.append('envelope')
        return out
    for k, v in envelope.items():
        spec = OPTIONAL_KEYS.get(k) if isinstance(k, str) else None
        if spec is None:
            dropped.append('envelope.' + _safe_name(k))
            continue
        r = _check_value(spec, v, None, 'envelope.' + k, dropped)
        if r is _DROP:
            dropped.append('envelope.' + k)
        elif r is not None:
            out[k] = r
    return out


def _write_text_if_changed(path: Path, text: str) -> None:
    try:
        if path.read_text(encoding='utf-8') == text:
            return
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as h:
        h.write(text)


def _emit(cfg, key, schema_path, type_, data, run_id, seq, stage, pbi, skill_version, cwd, envelope):
    schema = _load_schema(Path(schema_path))
    clean, dropped = _check(schema, type_, data, key)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    cwd = cwd or os.getcwd()
    is_id = lambda v: isinstance(v, str) and bool(ID_RE.match(v))
    values = {
        'schema': ENVELOPE, 'ts': now.isoformat(), 'phase': schema['phase'], 'skill': schema['skill'],
        'skill_version': _head_value(skill_version, is_id, 'skill_version', dropped),
        'data_schema': schema['data_schema'],
        'repo': _repo_value(cfg, key, cwd), 'branch': _branch_value(key, cwd),
        'host': keyed_hash(key, socket.gethostname()),
        'run_id': _head_value(run_id, lambda v: isinstance(v, str) and bool(FILE_RE.match(v)), 'run_id', dropped),
        'seq': _head_value(seq, lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0, 'seq', dropped),
        'stage': _head_value(stage, is_id, 'stage', dropped),
        'pbi': _head_value(pbi, is_id, 'pbi', dropped),
        'type': type_, 'data': clean,
    }
    values.update(_envelope_extra(envelope, dropped))
    values['dropped'] = list(dict.fromkeys(dropped))
    line = {}
    for k, _ in ENVELOPE_FIELDS:
        if k not in values or (k in OPTIONAL_KEYS and values[k] is None) or (k == 'dropped' and not values[k]):
            continue
        line[k] = values[k]
    spool = Path(cfg['spool_dir'])
    name = values['run_id'] or 'adhoc-' + now.strftime('%Y-%m-%d')
    path = spool / schema['phase'] / schema['skill'] / now.strftime('%Y-%m') / f'{name}.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a', encoding='utf-8', newline='\n') as h:
        h.write(json.dumps(line, ensure_ascii=False, separators=(',', ':')) + '\n')
    _write_text_if_changed(spool / '.schemas' / f"{schema['skill']}.json",
                           json.dumps(schema, ensure_ascii=False, indent=2) + '\n')
    return line


def emit(schema_path, type, data, *, run_id=None, seq=None, stage=None, pbi=None, skill_version=None,
         cwd=None, config_path=None, envelope=None) -> dict | None:
    """点検して外枠を付けた行をスプールに追記し、その行を返す。設定が無い・失敗のときは None。例外を出さない。"""
    try:
        cfg = load_config(config_path)
        key = _key(cfg)
        if cfg is None or key is None or cfg['mode'] == 'off':
            return None
    except Exception:
        return None
    try:
        return _emit(cfg, key, schema_path, type, data, run_id, seq, stage, pbi, skill_version, cwd, envelope)
    except Exception as e:
        _log_error(cfg, f'emit: {e.__class__.__name__}: {e}')
        return None


# ---------- flush ----------

def _spool_files(root: Path):
    """root の下の *.jsonl。'.' で始まるフォルダ (.schemas・.flushing) は除く。"""
    out = []
    if not root.is_dir():
        return out
    for f in sorted(root.rglob('*.jsonl')):
        if not any(p.startswith('.') for p in f.relative_to(root).parts[:-1]):
            out.append(f)
    return out


def _take_lock(lock: Path) -> bool:
    for _ in range(2):
        try:
            os.close(os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return True
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > LOCK_STALE_SECONDS:
                    lock.unlink()
                    continue
            except OSError:
                pass
            return False
    return False


def _axis(v) -> str:
    if isinstance(v, dict):  # 段階の object (例: size) は、その段階 (size_bucket か files) を使う
        v = next((v[k] for k in ('size_bucket', 'files') if isinstance(v.get(k), str) and v[k]), None)
    return v if isinstance(v, str) and v else 'unknown'


def _coverage(clone: Path, phase: str, skill: str) -> dict:
    """task.profiled (scope run) を軸ごとに数える。同じ run (run_id、無ければ case_id) の行は 1 回、後の行が勝つ。"""
    latest: dict = {}
    for f in sorted((clone / phase / skill).rglob('*.jsonl')):
        for i, line in enumerate(f.read_text(encoding='utf-8', errors='replace').splitlines()):
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            d = ev.get('data') if isinstance(ev, dict) and ev.get('type') == 'task.profiled' else None
            if not isinstance(d, dict) or d.get('scope') != 'run':
                continue
            run, case = ev.get('run_id'), d.get('case_id')
            ident = (('run', run) if isinstance(run, str) and run else
                     ('case', case) if isinstance(case, str) and case else ('line', f.as_posix(), i))
            latest.pop(ident, None)
            latest[ident] = tuple(_axis(d.get(a)) for a in COVERAGE_AXES)
    counts: dict = {}
    for k in latest.values():
        counts[k] = counts.get(k, 0) + 1
    rows = [dict(zip(COVERAGE_AXES, k), count=n) for k, n in sorted(counts.items())]
    return {'schema': 'harness.coverage/1', 'axes': list(COVERAGE_AXES), 'rows': rows}


_SECTION_RE = re.compile(r'<!-- skill:([A-Za-z0-9._-]+) -->\n.*?<!-- /skill:\1 -->\n', re.S)


def _schema_md(existing: str, spool: Path) -> str:
    sections = {m.group(1): m.group(0) for m in _SECTION_RE.finditer(existing or '')}
    sdir = spool / '.schemas'
    if sdir.is_dir():
        for f in sorted(sdir.glob('*.json')):
            s = _read_json(f)
            if isinstance(s, dict) and isinstance(s.get('skill'), str) and FILE_RE.match(s['skill']):
                body = json.dumps(s, ensure_ascii=False, indent=2)
                sections[s['skill']] = f"<!-- skill:{s['skill']} -->\n### {s['skill']}\n\n```json\n{body}\n```\n<!-- /skill:{s['skill']} -->\n"
    lines = ['# SCHEMA', '', f'harness-log が送るイベントの形 (`{ENVELOPE}`)。', '', '## 外枠', '',
             '| 項目 | 意味 |', '|---|---|']
    lines += [f'| `{k}` | {m} |' for k, m in ENVELOPE_FIELDS]
    lines += ['', '## スキルごとの許可リスト', '']
    return '\n'.join(lines) + '\n' + '\n'.join(sections[k] for k in sorted(sections))


def _has_head(clone: Path) -> bool:
    return _git(['rev-parse', '--verify', '-q', 'HEAD'], clone).returncode == 0


def _reset_clone(clone: Path) -> None:
    _git(['rebase', '--abort'], clone)
    if _has_head(clone):
        _git(['reset', '-q', '--hard', 'HEAD'], clone)
    else:
        _git(['read-tree', '--empty'], clone)
    _git(['clean', '-fdq'], clone)


def _fail(cfg, err, msg: str) -> bool:
    """失敗を標準エラーとスプールの errors.log に 1 行ずつ書き、False を返す。"""
    _say(err, msg)
    _log_error(cfg, msg)
    return False


def _sync_clone(clone: Path) -> str | None:
    """remote を取り込み、clone を remote の先頭に合わせる。remote にまだ枝が無ければ手元の先頭のまま。
    取り込めなければ git のエラーを返す。手元の未 push のコミットは捨てる (その行はスプールの batch に残っている)。"""
    r = _git(['fetch', '-q', '--prune', 'origin'], clone, timeout=120)
    if r.returncode != 0:
        return r.stderr or r.stdout or 'fetch failed'
    b = _git(['symbolic-ref', '-q', '--short', 'HEAD'], clone).stdout.strip()
    up = f'refs/remotes/origin/{b}'
    if b and _git(['rev-parse', '--verify', '-q', up], clone).returncode == 0:
        r = _git(['reset', '-q', '--hard', up], clone)
        if r.returncode != 0:
            return r.stderr or 'reset failed'
    return None


def _undo_commit(clone: Path, before: str | None) -> None:
    """この flush のコミットを取り消す (行はスプールの batch に残すので、次の flush で作り直す)。"""
    if before:
        _git(['reset', '-q', '--hard', before], clone)
    else:
        _git(['update-ref', '-d', 'HEAD'], clone)
    _reset_clone(clone)


def _flush(cfg, spool: Path, err) -> bool:
    clone = Path(cfg['clone_dir'])
    if not (clone / '.git').exists():
        clone.parent.mkdir(parents=True, exist_ok=True)
        r = _git(['clone', '-q', cfg['remote'], clone], timeout=120)
        if r.returncode != 0:
            return _fail(cfg, err, 'flush: clone failed (will retry on next flush): ' + _redact(r.stderr))
    _reset_clone(clone)
    # remote に合わせられないときは何もコミットせず、スプールの行もそのまま残す (古い土台に積まない)
    problem = _sync_clone(clone)
    if problem is not None:
        return _fail(cfg, err, 'flush: pull failed (lines kept for next flush): ' + _redact(problem))
    log = _git(['log', '--format=%B'], clone) if _has_head(clone) else None
    done = set(log.stdout.splitlines()) if log is not None and log.returncode == 0 else set()
    flushing = spool / '.flushing'
    batches = []
    if flushing.is_dir():
        for b in sorted(p for p in flushing.iterdir() if p.is_dir()):
            if f'harness-log batch {b.name}' in done:
                shutil.rmtree(b, ignore_errors=True)
            else:
                batches.append(b)
    new = flushing / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8])
    for f in _spool_files(spool):
        dest = new / f.relative_to(spool)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(f, dest)
        except OSError:
            continue  # emit が開いているファイルは次の flush に回す
    if new.is_dir():
        batches.append(new)
    touched = set()
    for b in batches:
        for f in _spool_files(b):
            rel = f.relative_to(b)
            lines = []
            for ln in f.read_text(encoding='utf-8', errors='replace').splitlines():
                try:
                    if isinstance(json.loads(ln), dict):
                        lines.append(ln)
                except ValueError:
                    pass
            if not lines:
                continue
            dest = clone / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, 'a', encoding='utf-8', newline='\n') as h:
                h.write(''.join(ln + '\n' for ln in lines))
            if len(rel.parts) >= 3:
                touched.add((rel.parts[0], rel.parts[1]))
    for phase, skill in sorted(touched):
        _write_text_if_changed(clone / phase / skill / 'coverage.json',
                               json.dumps(_coverage(clone, phase, skill), ensure_ascii=False, indent=2) + '\n')
    if touched:
        md = clone / 'SCHEMA.md'
        old = md.read_text(encoding='utf-8') if md.is_file() else ''
        _write_text_if_changed(md, _schema_md(old, spool))
    _git(['add', '-A'], clone)
    before = _git(['rev-parse', '--verify', '-q', 'HEAD'], clone).stdout.strip() or None
    committed = False
    if _git(['status', '--porcelain'], clone).stdout.strip():
        msg = 'harness-log flush\n\n' + ''.join(f'harness-log batch {b.name}\n' for b in batches)
        r = _git([*GIT_IDENT, 'commit', '-q', '-m', msg], clone)
        if r.returncode != 0:
            _reset_clone(clone)
            return _fail(cfg, err, 'flush: commit failed (lines kept for next flush): ' + _redact(r.stderr or r.stdout))
        committed = True
    if _has_head(clone):
        r = _git(['push', '-q', '-u', 'origin', 'HEAD'], clone, timeout=120)
        if r.returncode != 0:
            if committed:
                _undo_commit(clone, before)
            return _fail(cfg, err, 'flush: push failed (lines kept for next flush): ' + _redact(r.stderr))
    # push が通ってから batch を消す (消す前に止まっても、次の flush が log の batch 名で見分けて消す)
    for b in batches:
        shutil.rmtree(b, ignore_errors=True)
    return True


def flush(*, config_path=None, err=None) -> bool:
    """enabled のときだけ、remote の先頭に合わせた clone にスプールの行を移して 1 回コミットし push する。
    送れたら True。送れなければ行をスプールに残し、errors.log に 1 行を書いて False。例外を出さない。"""
    try:
        cfg = load_config(config_path)
        if cfg is None or cfg['mode'] != 'remote' or cfg['enabled'] is not True or not cfg.get('remote'):
            return False
        spool = Path(cfg['spool_dir'])
        spool.mkdir(parents=True, exist_ok=True)
        lock = spool / '.lock'
        if not _take_lock(lock):
            _say(err, 'flush: another flush holds the lock')
            return False
        try:
            return _flush(cfg, spool, err)
        finally:
            try:
                lock.unlink()
            except OSError:
                pass
    except Exception as e:
        _say(err, f'flush: {e.__class__.__name__}: {e}')
        return False


# ---------- preview ----------

def preview(*, config_path=None, out=None) -> bool:
    """スプールの行と、push していないコミットをそのまま表示する。設定が無ければ False。"""
    out = out or sys.stdout
    cfg = load_config(config_path)
    if cfg is None:
        return False
    spool = Path(cfg['spool_dir'])
    files = _spool_files(spool)
    flushing = spool / '.flushing'
    if flushing.is_dir():
        files += [f for b in sorted(flushing.iterdir()) if b.is_dir() for f in _spool_files(b)]
    for f in files:
        print('== ' + f.relative_to(spool).as_posix(), file=out)
        out.write(f.read_text(encoding='utf-8', errors='replace'))
    clone = Path(cfg['clone_dir'])
    if (clone / '.git').exists():
        r = _git(['log', '--branches', '--not', '--remotes', '-p', '--no-color'], clone)
        if r.returncode == 0 and r.stdout.strip():
            print('== unpushed commits', file=out)
            out.write(r.stdout)
    return True


# ---------- CLI ----------

def _clone_remote(cfg) -> None:
    clone = Path(cfg['clone_dir'])
    if not (clone / '.git').exists():
        clone.parent.mkdir(parents=True, exist_ok=True)
        r = _git(['clone', '-q', cfg['remote'], clone], timeout=120)
        if r.returncode != 0:
            _say(sys.stderr, 'init: clone failed (flush will retry): ' + _redact(r.stderr))


def _cmd_init(a) -> int:
    """設定を作る。どの mode でも enabled: false で、送らない。local の設定に --remote を打つと、
    鍵とスプールを残したまま送り先を足す (ためた行は enable の後の flush で送られる)。"""
    modes = [m for m, on in (('remote', a.remote is not None), ('local', a.local), ('off', a.off)) if on]
    if len(modes) != 1:
        _say(sys.stderr, 'init: give exactly one of --remote <URL>, --local or --off')
        return 2
    mode = modes[0]
    p = config_path()
    if p.exists():
        raw = _read_json(p)
        old = load_config(p)
        if not (isinstance(raw, dict) and old and old['mode'] == 'local' and mode == 'remote'):
            _say(sys.stderr, f'init: config already exists: {p} (remove it yourself to start over)')
            return 2
        raw.update({'mode': 'remote', 'remote': a.remote, 'enabled': False})
        if a.show_repo:
            raw['show_repos'] = a.show_repo
        _save_config(p, raw)
        _clone_remote(load_config(p))
        print(f'harness-log: remote added to {p} (enabled: false; kept lines stay in the spool). '
              'Run preview, then enable.')
        return 0
    cfg = {
        'mode': mode, 'remote': a.remote, 'enabled': False, 'key': secrets.token_hex(32),
        'show_repos': a.show_repo or [],
        'spool_dir': os.path.abspath(os.path.expanduser(DEFAULT_SPOOL)),
        'clone_dir': os.path.abspath(os.path.expanduser(DEFAULT_CLONE)),
        'embedding': dict(EMBEDDING_DEFAULTS),
    }
    _save_config(p, cfg)
    if mode == 'remote':
        _clone_remote(cfg)
    note = {'remote': 'Run preview, then enable.',
            'local': 'Events stay in the local spool; add a remote later with init --remote <URL>.',
            'off': 'Nothing is recorded.'}[mode]
    print(f'harness-log: config written to {p} (mode: {mode}, enabled: false). {note}')
    return 0


def _cmd_status() -> int:
    """記録のしかたを 1 行で出す: none・off・local spool=<行数>・remote enabled|disabled spool=<行数>。"""
    cfg = load_config()
    if cfg is None:
        print('none')
        return 0
    if cfg['mode'] == 'off':
        print('off')
        return 0
    lines = 0
    for f in _spool_files(Path(cfg['spool_dir'])):
        try:
            lines += sum(1 for ln in f.read_text(encoding='utf-8', errors='replace').splitlines() if ln.strip())
        except OSError:
            pass
    if cfg['mode'] == 'local':
        print(f'local spool={lines}')
    else:
        print(f"remote {'enabled' if cfg['enabled'] else 'disabled'} spool={lines}")
    return 0


def _parse_obj(text, what):
    try:
        v = json.loads(text)
    except ValueError:
        v = None
    if not isinstance(v, dict):
        _say(sys.stderr, f'emit: {what} must be a JSON object')
        return None
    return v


def _cmd_emit(a) -> int:
    data = _parse_obj(a.data, '--data')
    env = _parse_obj(a.envelope, '--envelope') if a.envelope is not None else {}
    if data is None or env is None:
        return 2
    emit(a.schema, a.type, data, run_id=a.run_id, seq=a.seq, stage=a.stage, pbi=a.pbi,
         skill_version=a.skill_version, envelope=env or None)
    return 0


def _cmd_toggle(on: bool) -> int:
    p = config_path()
    raw = _read_json(p)
    if not isinstance(raw, dict):
        _say(sys.stderr, 'no config; run init first')
        return 2
    if on and load_config(p)['mode'] != 'remote':
        _say(sys.stderr, 'enable: no remote to send to; run init --remote <URL> first')
        return 2
    raw['enabled'] = on
    _save_config(p, raw)
    print('harness-log: ' + ('enabled' if on else 'disabled'))
    return 0


def _cmd_hash(a) -> int:
    key = _key(load_config())
    if key is None:
        _say(sys.stderr, 'hash: no config or key; run init first')
        return 2
    print(keyed_hash(key, a.value))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='harness_log.py', description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('init')
    p.add_argument('--remote')
    p.add_argument('--local', action='store_true')
    p.add_argument('--off', action='store_true')
    p.add_argument('--show-repo', action='append')
    p = sub.add_parser('emit')
    p.add_argument('--schema', required=True)
    p.add_argument('--type', required=True)
    p.add_argument('--data', required=True)
    p.add_argument('--run-id')
    p.add_argument('--seq', type=int)
    p.add_argument('--stage')
    p.add_argument('--pbi')
    p.add_argument('--skill-version')
    p.add_argument('--envelope')
    for name in ('flush', 'preview', 'enable', 'disable', 'status'):
        sub.add_parser(name)
    p = sub.add_parser('hash')
    p.add_argument('value')
    a = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
    if a.cmd == 'init':
        return _cmd_init(a)
    if a.cmd == 'emit':
        return _cmd_emit(a)
    if a.cmd == 'flush':
        flush()
        return 0
    if a.cmd == 'preview':
        if not preview():
            _say(sys.stderr, 'no config; nothing to preview')
        return 0
    if a.cmd == 'status':
        return _cmd_status()
    if a.cmd in ('enable', 'disable'):
        return _cmd_toggle(a.cmd == 'enable')
    return _cmd_hash(a)


if __name__ == '__main__':
    sys.exit(main())
