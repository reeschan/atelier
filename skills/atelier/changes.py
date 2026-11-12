"""hillclimb の 1 ラウンド 1 変更 (harness.change/1)。

`change apply` で変更を保全先の `changes.jsonl` に 1 行記録し、`change.applied` を出す。
`current_envelope(dir)` は、events.emit が外枠に `change_id`・`config_fp` を足すために毎回呼ぶ
(docs/specs/harness-metrics.md の追記 §3.1・§3.3・§3.7)。どの失敗でも例外を投げない。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA = 'harness.change/1'
ID_RE = re.compile(r'^[A-Za-z0-9._:/@-]{1,64}\Z')
PART_NAMES = ('skill_version', 'worker', 'designer', 'roles', 'workflow', 'routing')

# 部品 → その部品を動かす kind。multi の判定はこの表を使わず、変わった部品の数で数える (門の決め)。
PART_KINDS = {
    'worker': ('worker-prompt',),
    'designer': ('designer-prompt', 'appendix'),
    'roles': ('model',),
    'routing': ('routing',),
    'workflow': ('workflow',),
    'skill_version': ('tool', 'grader'),
}
# 仕様 §3.3 と harness.schema.json の change.applied.kind と同じ順。
KINDS = ('worker-prompt', 'designer-prompt', 'appendix', 'routing', 'model', 'workflow',
         'tool', 'grader')
assert set(KINDS) == {kind for kinds in PART_KINDS.values() for kind in kinds}


class _Invalid(ValueError):
    """引数の語彙・id 型の違反 (CLI では終了コード 2)。"""


# ---------- 指紋の部品 ----------

def _sha12(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def _read_bytes(path: Path):
    try:
        return Path(path).read_bytes()
    except (OSError, ValueError):
        return None


def _events():
    """同じフォルダの events.py。関数の中でだけ import する (events.emit → ここ の循環を避ける)。"""
    try:
        import events
        return events
    except (Exception, SystemExit):
        pass
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    try:
        import events
        return events
    except (Exception, SystemExit):
        return None


def _skill_version() -> str:
    events = _events()
    if events is None:
        return 'none'
    value = events.skill_version()
    return value if isinstance(value, str) and value else 'none'


def _file_version(path: Path):
    """record_run.file_version と同じ値 (sha256 の先頭 12)。読めなければ None。"""
    try:
        import record_run
        return record_run.file_version(Path(path))
    except (Exception, SystemExit):
        data = _read_bytes(path)
        return _sha12(data) if data is not None else None


def _designer_version() -> str:
    """designer.md・agents/*.md・appendix/*.md を名前順に連結した sha12 (門の決め)。"""
    data = _read_bytes(HERE / 'designer.md')
    if data is None:
        return 'none'
    for folder in (HERE / 'agents', HERE / 'appendix'):
        try:
            files = sorted(folder.glob('*.md'), key=lambda p: p.name)
        except OSError:
            files = []
        for path in files:
            chunk = _read_bytes(path)
            if chunk is None:
                return 'none'
            data += chunk
    return _sha12(data)


def _routing_version() -> str:
    data = _read_bytes(HERE / 'routing.default.json')
    return _sha12(data) if data is not None else 'none'


def _read_run(base: Path) -> dict:
    path = Path(base) / 'conductor' / 'run.json'
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _roles_fp(base: Path, run: dict) -> str:
    """解決後の roles (run → repo → home → 既定) の sha12。

    層の重ね方は conductor.load_roles をそのまま使う (ここに写さない)。conductor の import は
    関数の中でだけ行う (重い・events を import するため)。
    """
    try:
        import conductor
        roles, _sources, _warns = conductor.load_roles(conductor.Paths(str(base)), run)
        return _sha12(json.dumps(roles, sort_keys=True).encode('utf-8'))
    except (Exception, SystemExit):
        return 'none'


def _workflow(run: dict, base: Path) -> str:
    if isinstance(run, dict):
        value = run.get('workflow')
        if isinstance(value, str) and value.strip():
            return value.strip()
    try:
        text = (Path(base) / 'STATUS.md').read_text(encoding='utf-8')
    except (OSError, ValueError):
        return 'none'
    m = re.search(r'^Workflow:\s*(\S+)', text, re.MULTILINE)
    return m.group(1) if m else 'none'


def _safe(fn) -> str:
    try:
        value = fn()
    except (Exception, SystemExit):
        return 'none'
    return value if isinstance(value, str) and value else 'none'


def config_parts(dir) -> dict:
    """6 つの部品 (すべて文字列、読めないものは 'none')。例外を投げない。"""
    try:
        base = Path(dir) if dir is not None else None
    except (TypeError, ValueError):
        base = None
    run = _read_run(base) if base is not None else {}
    return {
        'skill_version': _safe(_skill_version),
        'worker': _safe(lambda: _file_version(base / 'worker.md') if base is not None else None),
        'designer': _safe(_designer_version),
        'roles': _safe(lambda: _roles_fp(base, run) if base is not None else None),
        'workflow': _safe(lambda: _workflow(run, base) if base is not None else None),
        'routing': _safe(_routing_version),
    }


def _fp(parts: dict) -> str:
    return _sha12(json.dumps(parts, sort_keys=True).encode('utf-8'))


def config_fp(dir) -> str:
    """今の構成の指紋 (parts の sort_keys の JSON の sha256 先頭 12)。"""
    return _fp(config_parts(dir))


# ---------- changes.jsonl ----------

def _archive_root() -> Path:
    try:
        import record_run
        return Path(record_run.archive_root())
    except (Exception, SystemExit):
        env = os.environ.get('PBI_RUNS_ARCHIVE')
        return Path(env) if env else Path.home() / '.claude' / 'pbi-runs'


def _changes_path() -> Path:
    return _archive_root() / 'changes.jsonl'


def _last_row(path: Path):
    """changes.jsonl の最後の空でない 1 行だけを後ろから読む。無ければ None。

    途中で切れた行・JSON でない行は ValueError のまま外へ出す (呼び手が決める)。
    """
    path = Path(path)
    try:
        size = path.stat().st_size
        if size <= 0:
            return None
        with open(path, 'rb') as h:
            pos = size
            tail = b''
            while pos > 0:
                step = min(4096, pos)
                pos -= step
                h.seek(pos)
                tail = h.read(step) + tail
                lines = tail.split(b'\n')
                if pos > 0:
                    lines = lines[1:]  # 先頭は途中の行
                for raw in reversed(lines):
                    text = raw.strip()
                    if not text:
                        continue
                    row = json.loads(text.decode('utf-8'))
                    if not isinstance(row, dict):
                        raise ValueError('changes.jsonl: row is not an object')
                    return row
        return None
    except FileNotFoundError:
        return None


def _changed(prev, now: dict) -> list:
    if not isinstance(prev, dict):
        return []
    if not all(name in prev for name in PART_NAMES):
        return []  # 形の違う古い行は比べられない
    return [name for name in sorted(PART_NAMES) if prev.get(name) != now.get(name)]


def _multi(changed: list) -> bool:
    """前の change と比べて変わった部品が 2 つ以上なら true (part → kind の対応では数えない)。"""
    return len(changed) >= 2


# ---------- 変更の記録 ----------

def apply(dir, kind: str, target: str, hypothesis: str) -> dict:
    """変更を 1 つ記録し、change.applied を出す。引数が語彙・id 型に合わなければ _Invalid。"""
    if kind not in KINDS:
        raise _Invalid(f'bad --kind: {kind}')
    for name, value in (('target', target), ('hypothesis', hypothesis)):
        if not (isinstance(value, str) and ID_RE.match(value)):
            raise _Invalid(f'bad --{name}: {value}')
    base = Path(dir) if dir is not None else None
    if base is None:
        raise _Invalid('bad --dir')
    parts = config_parts(base)
    fp = _fp(parts)
    path = _changes_path()
    prev = _last_row(path)
    parent = prev.get('change_id') if isinstance(prev, dict) else None
    if not (isinstance(parent, str) and ID_RE.match(parent)):
        parent = None
    prev_parts = prev.get('config_parts') if isinstance(prev, dict) else None
    changed = _changed(prev_parts, parts)
    multi = _multi(changed)
    now = datetime.now(timezone.utc)
    ts = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    change_id = f'ch-{now.strftime("%Y%m%d%H%M%S")}-{fp[:6]}'
    row = {
        'schema': SCHEMA,
        'ts': ts,
        'change_id': change_id,
        'parent': parent,
        'kind': kind,
        'target': target,
        'hypothesis': hypothesis,
        'config_fp': fp,
        'config_parts': parts,
        'multi': multi,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a', encoding='utf-8', newline='\n') as h:
        h.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')
    # 行を先に書く。こうすると change.applied 自身の外枠にも新しい change_id が付く。
    events = _events()
    if events is not None:
        try:
            events.emit(base, 'change.applied', {
                'change_id': change_id,
                'parent': parent,
                'kind': kind,
                'target': target,
                'hypothesis': hypothesis,
                'multi': multi,
            })
        except (Exception, SystemExit):
            pass
    return row


def current_envelope(dir) -> dict:
    """events.emit の外枠に載せる `change_id` と今の `config_fp`。どの失敗でも {}。"""
    try:
        env = {'config_fp': config_fp(dir)}
        row = _last_row(_changes_path())
        if isinstance(row, dict):
            change_id = row.get('change_id')
            if isinstance(change_id, str) and ID_RE.match(change_id):
                env['change_id'] = change_id
        return env
    except (Exception, SystemExit):
        return {}


# ---------- CLI ----------

def main(argv=None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog='events.py change', description='1 ラウンド 1 変更を記録する')
    sub = parser.add_subparsers(dest='cmd', required=True)
    apply_parser = sub.add_parser('apply', help='変更を changes.jsonl に記録する')
    apply_parser.add_argument('--dir', required=True)
    apply_parser.add_argument('--kind', required=True, choices=list(KINDS))
    apply_parser.add_argument('--target', required=True)
    apply_parser.add_argument('--hypothesis', required=True)
    try:
        args = parser.parse_args(args_list)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if args.cmd != 'apply':
        return 2
    try:
        row = apply(args.dir, args.kind, args.target, args.hypothesis)
    except _Invalid as exc:
        print(f'change: {exc}', file=sys.stderr)
        return 2
    except (Exception, SystemExit) as exc:
        print(f'change: {exc.__class__.__name__}', file=sys.stderr)
        return 1
    parent = row['parent'] if row['parent'] else 'none'
    print(f"change: {row['change_id']} multi={str(row['multi']).lower()} parent={parent}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
