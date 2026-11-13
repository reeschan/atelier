#!/usr/bin/env python3
"""Atelier Desk 向けの、副作用のない保存予定 CLI。Studio の保存と同じ結果を、何も書かずに計画として返す。

    python desk_plan.py KIND --home HOME --input FILE [--name NAME]

KIND は runner / model / routing / template / workflow / application / application-answer (本文は
desk_validate.py と同じ) と、本文のない template-delete / workflow-delete / example-copy /
application-expire (--input には {} だけを渡す。{} 以外は終了 2)。
--name は runner・model (runner 名)、template・template-delete・workflow・workflow-delete (対象名)、
example-copy (同梱 example 名) で必須、ほかの KIND では受け付けない。HOME は絶対パスで、存在しなくてよい。
成功 (終了 0): stdout に ASCII にエスケープした JSON を 1 行だけ書く。
  {"schema": "atelier.save-plan/1", "kind", "evaluated_at", "reads", "writes"}
  reads: 計画が頼った全ての読み取り (不在も)。
    {"root": "home"|"atelier", "type": "file", "path", "sha256": 小文字 64 桁 | "missing"}
    {"root", "type": "directory", "path", "missing": bool, "entries": [{"name", "type": "file", "sha256"}]}
  writes: 順序付きの {"op": "replace", "path", "content_utf8"} か {"op": "delete", "path"} (全て home 内)。
    content_utf8 はファイル全体で、Studio の保存と同じバイト列になる (履歴は os.linesep で追記)。
検証失敗 (終了 1): desk_validate.py と同じ atelier.validation/1 の valid:false (problems は Studio の error)。
呼び出しの誤りは desk_validate.py と同じ終了 2〜4 (stdout 空、stderr に 1 行)。削除・コピー元が無いのは 3 (Studio の 404)。
壊れた agents.json・models・overrides.json は Studio と同じく上書きする計画を返す (Desk が置換前に退避する)。
home にも配布版にも何も書かず、mkdir せず、ネットワークも使わない。リンク・ジャンクション・通常ファイル
以外の読み取り対象、配布版の外を指す部品の doc / invoke.path は終了 4。
"""
import sys

sys.dont_write_bytecode = True  # 配布版に __pycache__ を作らない (他の import より先に)

import dataclasses  # noqa: E402
import datetime  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import stat  # noqa: E402
from pathlib import Path, PurePosixPath  # noqa: E402
from typing import BinaryIO, Callable  # noqa: E402

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import common  # noqa: E402
import desk_validate  # noqa: E402
import flow  # noqa: E402
import setup_agents  # noqa: E402
import templates  # noqa: E402
from desk_validate import CallError, Exit  # noqa: E402

__all__ = ['main', 'plan', 'KINDS']

PROG = 'desk_plan.py'
SCHEMA = 'atelier.save-plan/1'
VALIDATION_SCHEMA = 'atelier.validation/1'
MISSING = 'missing'
# Studio が ValueError の文字列の前に付ける言葉 (serve.py の application・answer)
ENCODE_PREFIX = {'application': 'the application was not started: ',
                 'application-answer': 'the answer was not saved: '}


class Invalid(Exception):
    """Studio なら 400 / 409 になる検証失敗 (終了 1)。"""


@dataclasses.dataclass(frozen=True)
class Replace:
    path: str
    text: str
    encoded: str | None = None  # Studio が実際に UTF-8 にする部分 (追記なら足す行だけ)。None なら text 全体


@dataclasses.dataclass(frozen=True)
class Delete:
    path: str


@dataclasses.dataclass(frozen=True)
class Request:
    home: Path
    body: dict
    name: str | None
    now: datetime.datetime


def _broken(message: str) -> CallError:
    return CallError(Exit.BROKEN, f'{PROG}: {message}')


def _json_text(data) -> str:
    """common.write_json が書くファイル全体 (LF 固定)。"""
    return json.dumps(data, ensure_ascii=False, indent=2) + '\n'


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# os.path.isjunction・DirEntry.is_junction は 3.12 から。3.10 でも動くよう lstat の reparse tag で見分ける
_JUNCTION_TAG = getattr(stat, 'IO_REPARSE_TAG_MOUNT_POINT', 0xA0000003)


def _is_link(info: os.stat_result) -> bool:
    """lstat の結果がシンボリックリンクかジャンクション。"""
    return stat.S_ISLNK(info.st_mode) or getattr(info, 'st_reparse_tag', 0) == _JUNCTION_TAG


def _bundle_rel(target: str) -> str | None:
    """部品の doc・invoke.path を、flow.component_error と同じく配布版のルートから解決した正規の POSIX 相対パス。
    ./ ・空の要素・(Windows では) \\ 区切りは受け入れる。絶対パス・ドライブ付き・途中で配布版の外に出るものは終了 4。
    配布版のルートそのものを指すなら None (ファイルではないので読まない)。"""
    pure = Path(target)
    if '\0' in target or pure.is_absolute() or pure.drive or pure.root:
        raise _broken(f'unsafe path: {target!r}')
    parts: list[str] = []
    for part in pure.parts:
        if part == '..':
            if not parts:
                raise _broken(f'unsafe path: {target!r}')
            parts.pop()
        elif part != '.':
            parts.append(part)
    return '/'.join(parts) or None


def _check_rel(rel: str) -> PurePosixPath:
    """/ 区切りの相対パス。絶対パス・.. ・空の要素・\\ や : を含むものは終了 4。"""
    path = PurePosixPath(rel)
    if (not rel or path.is_absolute() or '\\' in rel or ':' in rel or '\0' in rel
            or any(part in ('', '.', '..') for part in rel.split('/'))):
        raise _broken(f'unsafe path: {rel!r}')
    return path


class Recorder:
    """ファイルを読む唯一の口。読んだものを依存として (root, path) ごとに 1 回だけ記録する。"""

    def __init__(self, home: Path, atelier: Path = flow.HERE):
        self.bases = {'home': home, 'atelier': atelier}
        self.deps: dict[tuple[str, str], dict] = {}
        self.raw: dict[tuple[str, str], bytes | None] = {}

    def _target(self, root: str, rel: str) -> Path:
        """リンク・ジャンクションを含まない実パス (不在の手前までを検査する)。
        Windows は親が通常ファイルでも FileNotFoundError を返すので、不在のときは手前の要素が
        ディレクトリかを確かめ、POSIX の NotADirectoryError と同じく終了 4 にする。"""
        parts = _check_rel(rel).parts
        current = self.bases[root]
        parent = None  # 直前に存在した要素の lstat (まだ無ければ None で root のベース)
        for part in parts:
            current = current / part
            try:
                info = os.lstat(current)
            except FileNotFoundError as error:
                self._require_directory(root, rel, parent, error)
                break
            except OSError as error:
                raise _broken(f'cannot read {root}/{rel}: {error}')
            if _is_link(info):
                raise _broken(f'{root}/{rel} goes through a link or junction.')
            parent = info
        return self.bases[root].joinpath(*parts)

    def _require_directory(self, root: str, rel: str, parent, error: OSError) -> None:
        """不在の要素の手前 (parent、None なら root のベース) がディレクトリでなければ終了 4。
        ベースそのものが無いときは不在のまま (missing)。"""
        if parent is None:
            try:
                parent = os.stat(self.bases[root])
            except FileNotFoundError:
                return
            except OSError as base_error:
                raise _broken(f'cannot read {root}/{rel}: {base_error}')
        if not stat.S_ISDIR(parent.st_mode):
            raise _broken(f'cannot read {root}/{rel}: {error}')

    def _read_regular(self, root: str, rel: str, path: Path, before) -> bytes:
        """ファイルを 1 回だけ開き、開いたハンドルで通常ファイルと確かめて同じハンドルから読む。
        リンクをたどらず開き (O_NOFOLLOW があれば)、開く前の lstat と同じファイルでなければ終了 4。
        lstat の後に FIFO へ差し替えられても開く所で止まらないよう、ブロックしない形で開く (O_NONBLOCK があれば)。"""
        flags = (os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
                 | getattr(os, 'O_NONBLOCK', 0))
        try:
            descriptor = os.open(path, flags)
            try:
                handle = os.fdopen(descriptor, 'rb')
            except BaseException:
                os.close(descriptor)
                raise
            with handle:
                opened = os.fstat(handle.fileno())
                if (not stat.S_ISREG(opened.st_mode)
                        or (opened.st_ino, opened.st_dev) != (before.st_ino, before.st_dev)):
                    raise _broken(f'{root}/{rel} is not a regular file, or was replaced while reading.')
                return handle.read()
        except OSError as error:
            raise _broken(f'cannot read {root}/{rel}: {error}')

    def file(self, rel: str, root: str = 'home') -> bytes | None:
        """生のバイト。不在は None (読み取りエラーや通常ファイルでないものは終了 4)。"""
        key = (root, rel)
        if key in self.raw:
            return self.raw[key]
        path = self._target(root, rel)
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            data = None
        except OSError as error:
            raise _broken(f'cannot read {root}/{rel}: {error}')
        else:
            if not stat.S_ISREG(info.st_mode):
                raise _broken(f'{root}/{rel} is not a regular file.')
            data = self._read_regular(root, rel, path, info)
        self.raw[key] = data
        self.deps[key] = {'root': root, 'type': 'file', 'path': rel,
                          'sha256': MISSING if data is None else _sha256(data)}
        return data

    def parsed(self, rel: str, root: str = 'home'):
        """記録したバイトを JSON として解析した値 (common.load_json と同じ: 不在・読めない・JSON でなければ None)。
        計画の元になるファイルは必ずこの口か file() の同じバイトから作り、パスから読み直さない。"""
        raw = self.file(rel, root)
        if raw is None:
            return None
        try:
            return json.loads(raw.decode('utf-8'))
        except ValueError:
            return None

    def listing(self, rel: str, root: str, pattern: str = '*.json') -> list[tuple[str, bytes]]:
        """直下の pattern に合う通常ファイルを名前のコードポイント順に (名前, バイト) で返す。"""
        key = (root, rel + '/')
        path = self._target(root, rel)
        try:
            scan = os.scandir(path)
        except FileNotFoundError:
            self.deps[key] = {'root': root, 'type': 'directory', 'path': rel, 'missing': True, 'entries': []}
            return []
        except OSError as error:
            raise _broken(f'cannot list {root}/{rel}: {error}')
        found = []
        with scan:
            # 何が合うかは flow.load_components と同じ Path.glob に任せる (大文字小文字は OS 次第)
            matched = {item.name for item in path.glob(pattern)}
            for entry in sorted(scan, key=lambda item: item.name):
                if entry.name not in matched:
                    continue
                try:
                    is_regular = entry.is_file(follow_symlinks=False)
                    linked = entry.is_symlink() or _is_link(os.lstat(entry.path))
                except OSError as error:
                    raise _broken(f'cannot read {root}/{rel}/{entry.name}: {error}')
                if linked or not is_regular:
                    raise _broken(f'{root}/{rel}/{entry.name} is not a regular file.')
                data = self.file(f'{rel}/{entry.name}', root)
                found.append((entry.name, data))
        self.deps[key] = {'root': root, 'type': 'directory', 'path': rel, 'missing': False,
                          'entries': [{'name': name, 'type': 'file', 'sha256': _sha256(data)}
                                      for name, data in found]}
        return found

    def reads(self) -> list[dict]:
        """記録順。ディレクトリ内のファイルは一覧の entries が表すので別には出さない。"""
        listed = {(d['root'], f"{d['path']}/{e['name']}") for d in self.deps.values()
                  if d['type'] == 'directory' for e in d['entries']}
        return [dep for key, dep in self.deps.items() if key not in listed]


# --- 共通の部品 ---

def _history_append(rec: Recorder, record: dict) -> Replace:
    """history.jsonl に 1 行を足した完成後の全内容 (templates.append_history と同じ。改行は os.linesep)。"""
    raw = rec.file('history.jsonl')
    try:
        old = '' if raw is None else raw.decode('utf-8')
    except UnicodeDecodeError:
        raise _broken('history.jsonl is not valid UTF-8.')
    line = json.dumps(record, ensure_ascii=False)
    return Replace('history.jsonl', old + line + os.linesep, line + '\n')


def _load_agents(rec: Recorder) -> dict | None:
    """agents.load_agents と同じ判定を、記録したバイトから行う。"""
    data = rec.parsed(setup_agents.AGENTS_FILE)
    if not isinstance(data, dict) or not isinstance(data.get('runners'), dict):
        return None
    return data


def _load_models(rec: Recorder, runner: str) -> dict | None:
    """setup_agents.load_models と同じ判定を、記録したバイトから行う。"""
    setup_agents._check_runner(runner)
    data = rec.parsed(f'{setup_agents.MODELS_DIR}/{runner}.json')
    return data if isinstance(data, dict) else None


def _load_overrides(rec: Recorder) -> dict:
    """templates.load_overrides と同じ判定を、記録したバイトから行う。"""
    data = rec.parsed('overrides.json')
    if not isinstance(data, dict):
        return dict(templates.EMPTY_OVERRIDES)
    return {'synced_at': data.get('synced_at'), 'updated_at': data.get('updated_at'),
            'application': data.get('application')}


def _load_template(rec: Recorder, name) -> dict | None:
    """templates.load_template と同じ判定を、記録したバイトから行う。"""
    if not isinstance(name, str) or not templates.NAME_RE.fullmatch(name):
        return None
    template = rec.parsed(_template_file(name))
    if template is None or templates.validate(template, name):
        return None
    return template


def _new_application(rec: Recorder, name: str, until_arg: str, on_expire: str, reason: str,
                     via: str, now: datetime.datetime) -> dict:
    """templates.new_application と同じ結果を、テンプレートを記録したバイトから確かめて作る。"""
    if _load_template(rec, name) is None:
        raise ValueError(f'テンプレートが無い・不正: {name}')
    if until_arg == 'none':
        until = None
    else:
        until = templates.parse_time(until_arg)
        if now >= until:
            raise ValueError(f'until が now より後でない: {until_arg}')
    return {'id': templates.new_application_id(now), 'template': name,
            'until': until.isoformat() if until is not None else None,
            'on_expire': on_expire, 'reason': reason,
            'started_at': now.isoformat(), 'started_via': via}


def _overrides(rec: Recorder, req: Request, application: dict | None) -> Replace:
    current = _load_overrides(rec)
    return Replace('overrides.json', _json_text(
        {'synced_at': current['synced_at'], 'updated_at': req.now.isoformat(), 'application': application}))


def _template_file(name) -> str:
    return f'templates/{name}.json'


def _load_example(rec: Recorder, name: str) -> dict:
    raw = rec.file(f'examples/templates/{name}.json', 'atelier')
    if raw is None:
        raise CallError(Exit.MISSING, f'{PROG}: the example does not exist: {name}')
    try:
        template = json.loads(raw.decode('utf-8'))
    except ValueError:
        template = None
    if not isinstance(template, dict):
        raise _broken(f'the example could not be read: {name}')
    return template


def _component_dependencies(rec: Recorder) -> None:
    """workflow の検証が頼る部品の定義の一覧と、各部品が指す配布版のファイル (doc・script の path)。"""
    for root, rel in (('atelier', 'components'), ('home', 'components')):
        for name, raw in rec.listing(rel, root):
            try:
                data = json.loads(raw.decode('utf-8'))
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            targets = [data.get('doc')]
            invoke = data.get('invoke')
            if isinstance(invoke, dict) and invoke.get('kind') == 'script':
                targets.append(invoke.get('path'))
            for target in targets:
                if isinstance(target, str) and target:
                    rel = _bundle_rel(target)  # 配布版の外を指せば終了 4
                    if rel is not None:
                        rec.file(rel, 'atelier')


# --- KIND ごとの計画 ---

def _runner(rec: Recorder, req: Request) -> list:
    body = req.body
    models = None
    if 'selected' in body:
        models = setup_agents.REMOVE if body['selected'] is None else body['selected']
    table = setup_agents.runner_table(_load_agents(rec), req.name,
                                      enabled=body.get('enabled') if 'enabled' in body else None,
                                      models=models, billing=body.get('billing'))
    return [Replace(setup_agents.AGENTS_FILE, _json_text(table))]


def _model(rec: Recorder, req: Request) -> list:
    rel = f'{setup_agents.MODELS_DIR}/{req.name}.json'
    efforts = req.body.get('efforts', [])
    data = setup_agents.manual_models(_load_models(rec, req.name), req.name,
                                      req.body['id'], [] if efforts is None else efforts)
    return [Replace(rel, _json_text(data))]


def _routing(rec: Recorder, req: Request) -> list:
    rec.file(setup_agents.AGENTS_FILE)
    previous = rec.file('routing.json')
    rec.file('routing.prev.json')
    writes = []
    if previous is not None:
        try:
            writes.append(Replace('routing.prev.json', previous.decode('utf-8')))
        except UnicodeDecodeError:
            raise _broken('routing.json is not valid UTF-8.')
    writes.append(Replace('routing.json', _json_text(req.body)))
    return writes


def _template(rec: Recorder, req: Request) -> list:
    rec.file(_template_file(req.name))
    return [Replace(_template_file(req.name), _json_text(req.body))]


def _template_delete(rec: Recorder, req: Request) -> list:
    application = _load_overrides(rec)['application']
    if isinstance(application, dict) and application.get('template') == req.name:
        raise Invalid('the template is used by the current application.')
    if rec.file(_template_file(req.name)) is None:
        raise CallError(Exit.MISSING, f'{PROG}: the template does not exist: {req.name}')
    return [Delete(_template_file(req.name))]


def _workflow(rec: Recorder, req: Request) -> list:
    _component_dependencies(rec)
    rel = f'workflows/{req.name}.json'
    rec.file(rel)
    return [Replace(rel, _json_text(dict(req.body, name=req.name)))]


def _workflow_delete(rec: Recorder, req: Request) -> list:
    rel = f'workflows/{req.name}.json'
    present = rec.file(rel)
    rec.file(rel, 'atelier')  # 同梱の同名版があれば削除後はそちらに戻る
    if present is None:
        raise CallError(Exit.MISSING, f'{PROG}: the workflow is not in home: {req.name}')
    return [Delete(rel)]


def _example_copy(rec: Recorder, req: Request) -> list:
    template = _load_example(rec, req.name)
    target = _template_file(req.name)  # --name の名前のまま写す
    if os.path.lexists(rec._target('home', target)):  # ディレクトリでも Studio は 409
        raise Invalid('a template with that name already exists.')
    rec.file(target)
    return [Replace(target, _json_text(dict(template, name=req.name)))]


def _application(rec: Recorder, req: Request) -> list:
    body = req.body
    rec.file(_template_file(body['template']))
    overrides = _load_overrides(rec)
    until = body.get('until')
    application = _new_application(rec, body['template'], 'none' if until is None else until,
                                   body['on_expire'], body.get('reason', ''), 'studio', req.now)
    writes = []
    previous = templates.previous_writes(overrides['application'], req.now)
    if previous:
        writes.append(_history_append(rec, previous[0]['data']))
    writes.append(_overrides(rec, req, application))
    return writes


def _answer(rec: Recorder, req: Request) -> list:
    application = _load_overrides(rec)['application']
    answered, ended_how = templates.answered_application(application, req.body['choice'],
                                                         req.body.get('until'), req.now)
    writes = []
    if ended_how is not None:
        writes.append(_history_append(rec, templates.history_data(application, ended_how, req.now.isoformat())))
    writes.append(_overrides(rec, req, answered))
    return writes


def _expire(rec: Recorder, req: Request) -> list:
    application = _load_overrides(rec)['application']
    if not isinstance(application, dict):
        return []
    name = application.get('template')
    if _load_template(rec, name) is None:
        return []
    try:
        state = templates.state_of(application, req.now)
    except ValueError:  # until が解析できない: 期限切れと判定できないので何もしない
        return []
    if state != 'expired_drop':
        return []
    return [_history_append(rec, templates.history_data(application, 'expired', application.get('until'))),
            _overrides(rec, req, None)]


@dataclasses.dataclass(frozen=True)
class Kind:
    name_rule: Callable
    build: Callable
    validated: bool  # True なら desk_validate.check の検査を先に通す
    bodyless: bool = False  # True なら本文は {} のみ (それ以外は終了 2)


def _rule(kind: str) -> Callable:
    return desk_validate.KINDS[kind].name_rule


KINDS: dict[str, Kind] = {
    'runner': Kind(_rule('runner'), _runner, True),
    'model': Kind(_rule('model'), _model, True),
    'routing': Kind(_rule('routing'), _routing, True),
    'template': Kind(_rule('template'), _template, True),
    'template-delete': Kind(_rule('template'), _template_delete, False, True),
    'workflow': Kind(_rule('workflow'), _workflow, True),
    'workflow-delete': Kind(_rule('workflow'), _workflow_delete, False, True),
    'example-copy': Kind(_rule('template'), _example_copy, False, True),
    'application': Kind(_rule('application'), _application, True),
    'application-answer': Kind(_rule('application-answer'), _answer, True),
    'application-expire': Kind(_rule('application'), _expire, False, True),
}


def plan(kind: str, home, body: dict, name: str | None = None,
         now: datetime.datetime | None = None, atelier: Path = flow.HERE) -> dict:
    """保存予定 (atelier.save-plan/1 の dict)。検証失敗は Invalid、呼び出しの誤りは CallError。"""
    spec = KINDS.get(kind)
    if spec is None:
        raise CallError(Exit.USAGE, f'{PROG}: unknown KIND: {kind}')
    reason = spec.name_rule(name)
    if reason is not None:
        raise CallError(Exit.USAGE, f'{PROG}: {reason}')
    if not isinstance(body, dict):
        raise CallError(Exit.USAGE, f'{PROG}: the input must be a JSON object.')
    if spec.bodyless and body:
        raise CallError(Exit.USAGE, f'{PROG}: {kind} takes no input; pass {{}}.')
    now = common.now() if now is None else now
    home = Path(home)
    if spec.validated:
        problem = desk_validate.check(kind, home, body, name, now)
        if problem is not None:
            raise Invalid(problem)
    rec = Recorder(home, atelier)
    try:
        writes = spec.build(rec, Request(home, body, name, now))
    except ValueError as error:  # Studio の ValueError (400) と同じ
        raise Invalid(str(error))
    for write in writes:  # UTF-8 にできない内容 (単独サロゲート) は Studio の書き込みと同じく 400
        if isinstance(write, Replace):
            try:
                (write.text if write.encoded is None else write.encoded).encode('utf-8')
            except UnicodeEncodeError as error:
                raise Invalid(ENCODE_PREFIX.get(kind, '') + str(error))
    seen = set()
    for write in writes:
        _check_rel(write.path)
        if write.path in seen:
            raise _broken(f'duplicate write target: {write.path}')
        seen.add(write.path)
        rec.file(write.path)  # 書き込み先の変更前の状態も依存に入れる (通常ファイルでなければ終了 4)
    return {'schema': SCHEMA, 'kind': kind, 'evaluated_at': now.isoformat(), 'reads': rec.reads(),
            'writes': [{'op': 'replace', 'path': w.path, 'content_utf8': w.text} if isinstance(w, Replace)
                       else {'op': 'delete', 'path': w.path} for w in writes]}


def _parser():
    parser = desk_validate._Parser(prog=PROG, allow_abbrev=False,
                                   description='Plan a Studio save without saving it (read-only).')
    parser.add_argument('kind', metavar='KIND', choices=list(KINDS), help=' | '.join(KINDS))
    parser.add_argument('--home', required=True, help='absolute path of the Atelier home (need not exist)')
    parser.add_argument('--input', required=True, help='UTF-8 JSON object file ({} for KINDs without a body)')
    parser.add_argument('--name', default=None, help='runner or target name (see the module docstring)')
    return parser


def _emit(out: BinaryIO, result: dict) -> None:
    out.write(json.dumps(result, ensure_ascii=True, allow_nan=False).encode('ascii') + b'\n')
    out.flush()


def _home(arg: str) -> Path:
    """絶対パスの home (desk_validate._home と同じ判定。診断は desk_plan.py を名乗る)。何も作らない。"""
    if not arg or '\0' in arg or not Path(arg).is_absolute():
        raise CallError(Exit.USAGE, f'{PROG}: --home must be an absolute path: {arg}')
    home = Path(arg)
    try:
        is_dir = home.is_dir()
        exists = is_dir or os.path.lexists(home)
    except (OSError, ValueError) as error:
        raise _broken(f'cannot read home {home}: {error}')
    if exists and not is_dir:
        raise _broken(f'home is not a directory: {home}')
    return home


def _read_body(arg: str) -> dict:
    """入力ファイルの JSON オブジェクト (desk_validate._read_body と同じ判定。診断は desk_plan.py を名乗る)。"""
    if not arg or '\0' in arg:
        raise CallError(Exit.USAGE, f'{PROG}: --input is not a valid path.')
    try:
        raw = Path(arg).read_bytes()
    except FileNotFoundError:
        raise CallError(Exit.MISSING, f'{PROG}: the input file does not exist: {arg}')
    except OSError as error:
        raise _broken(f'cannot read the input {arg}: {error}')
    try:
        body = json.loads(raw.decode('utf-8'), parse_constant=desk_validate._reject_constant)
    except UnicodeDecodeError:
        raise CallError(Exit.USAGE, f'{PROG}: the input is not UTF-8 without BOM.')
    except ValueError as error:
        raise CallError(Exit.USAGE, f'{PROG}: the input is not valid JSON: {error}')
    if not isinstance(body, dict):
        raise CallError(Exit.USAGE, f'{PROG}: the input must be a JSON object.')
    return body


# --- CLI として動くときの副作用の拒否 (import やテストの plan() には入れない。audit hook は外せないため) ---

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
_REFUSED_EVENTS = frozenset({
    'os.mkdir', 'os.rename', 'os.remove', 'os.rmdir', 'os.truncate', 'os.symlink', 'os.link',
    'os.chmod', 'os.chown', 'os.utime', 'os.system', 'os.exec', 'os.posix_spawn', 'os.spawn',
    'os.startfile', 'os.fork', 'os.forkpty', 'shutil.copyfile', 'shutil.copytree', 'shutil.move',
    'shutil.rmtree', '_winapi.CreateJunction', '_winapi.CreateProcess', '_winapi.CreateNamedPipe'})
_REFUSED_PREFIXES = ('socket.', 'subprocess.')


def _audit(event: str, args) -> None:
    """書き込みモードの open・mkdir・rename/replace/remove・socket・subprocess を拒む (終了 4)。読み取りの open は通す。"""
    if event == 'open':
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else 0
        if not ((isinstance(mode, str) and any(c in mode for c in 'wax+'))
                or (isinstance(flags, int) and flags & _WRITE_FLAGS)):
            return
    elif event not in _REFUSED_EVENTS and not event.startswith(_REFUSED_PREFIXES):
        return
    raise _broken(f'refused {event} while planning (desk_plan.py is read-only).')


def _install_guard() -> None:
    sys.addaudithook(_audit)


def _run(argv, out: BinaryIO) -> int:
    args = _parser().parse_args(argv)
    home = _home(args.home)
    reason = KINDS[args.kind].name_rule(args.name)  # 名前の誤り (終了 2) は入力ファイルの読み込みより先
    if reason is not None:
        raise CallError(Exit.USAGE, f'{PROG}: {reason}')
    body = _read_body(args.input)
    try:
        result = plan(args.kind, home, body, args.name)
    except Invalid as problem:
        _emit(out, {'schema': VALIDATION_SCHEMA, 'kind': args.kind, 'valid': False, 'problems': [str(problem)]})
        return int(Exit.INVALID)
    _emit(out, result)
    return int(Exit.OK)


def main(argv=None, out: BinaryIO | None = None, err: BinaryIO | None = None, *, guard: bool = False) -> int:
    """終了コードを返す。出力を組み立ててから 1 回だけ書く。呼び出しの誤りでは stdout に何も書かない。
    guard=True (CLI として動くとき) は import を終えた後に audit hook を入れ、副作用を終了 4 にする。"""
    out = sys.stdout.buffer if out is None else out
    err = sys.stderr.buffer if err is None else err
    argv = sys.argv[1:] if argv is None else list(argv)
    if guard:
        _install_guard()
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
    raise SystemExit(main(guard=True))
