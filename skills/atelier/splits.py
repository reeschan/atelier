"""case を train / heldout / regression に分ける (split)。events.py の `split` サブコマンドが main(argv) に委ねる。

仕様は docs/specs/harness-metrics.md の末尾の追記 §3.2・§3.7。manifest は home
(`common.resolve_home(home)/splits/<name>.json`) に置き、リポジトリには置かない。振り方は
sha256(f"{seed}:{case_id}") だけで決まるので、同じ seed と同じ case_id なら何度でも同じ結果になる。
一度振った case は変えない。held-out を読んだら `retire` で regression に移す。

使い方:
  python skills/atelier/splits.py init --name default --seed 42 --home <home>
  python skills/atelier/splits.py assign --dir docs/pbi --home <home>
  python skills/atelier/splits.py retire --case <case_id> --reason read-heldout --dir docs/pbi
  python skills/atelier/events.py split init --name default --seed 42

終了コード: 0 できた / 2 引数の誤り・manifest が既にある・振れない case (何も変えない)。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import common

MANIFEST_SCHEMA = 'atelier.split/1'
NAME_RE = re.compile(r'^[A-Za-z0-9._-]{1,55}\Z')
PBI_TAIL = re.compile(r'(PBI-\d+)\Z')
SPLITS = ('train', 'heldout', 'regression', 'unsplit')
STRATA = ('task_type', 'domain', 'size_bucket')
ASSIGNED = 'split.assigned'
RETIRED = 'split.retired'
UNKNOWN = 'unknown'
WRITE_ATTEMPTS = 3


# ---------- manifest の読み書き ----------

def _manifest_path(name: str, home=None) -> Path:
    return common.resolve_home(home) / 'splits' / f'{name}.json'


def _valid_name(name) -> bool:
    return isinstance(name, str) and NAME_RE.match(name) is not None


def load_manifest(name='default', home=None):
    """manifest を読む。無い・壊れている・形が違うときは None (決して投げない)。"""
    try:
        if not _valid_name(name):
            return None
        raw = json.loads(_manifest_path(name, home).read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(raw, dict) or raw.get('schema') != MANIFEST_SCHEMA:
        return None
    if not isinstance(raw.get('split_set_id'), str) or not raw['split_set_id']:
        return None
    if not isinstance(raw.get('seed'), str) or not isinstance(raw.get('cases'), dict):
        return None
    return raw


def _save(path: Path, manifest: dict) -> bool:
    """一時ファイルに書いて os.replace で差し替える。Windows の一時的な PermissionError は数回取り直す。"""
    for attempt in range(WRITE_ATTEMPTS):
        try:
            common.write_json(path, manifest)
            return True
        except PermissionError:
            if attempt + 1 >= WRITE_ATTEMPTS:
                return False
            time.sleep(0.05)
        except OSError:
            return False
    return False


def split_in(manifest, case_id):
    """読み込み済みの manifest の下で case が持つ split。無ければ None。

    manifest['cases'] にある case は覚えている split をそのまま返す (retire した case は
    regression のまま)。まだ manifest に無い case は、assign が与えるのと同じ split を、
    同じ種・同じ比率 (`_heldout_ratio`) で先に返す。ただし assign が `cases.jsonl` から拾う形の
    id (`PBI_TAIL` が `PBI-<n>` に一致する id) だけで、`<repo>:<key>:run` のような擬似 case や
    その他の id は None。manifest は書かない。

    events.emit や leakcheck が呼ぶので、manifest か case_id が不正でも投げない。
    """
    try:
        if not isinstance(manifest, dict) or not isinstance(case_id, str) or not case_id:
            return None
        cases = manifest.get('cases')
        if isinstance(cases, dict) and case_id in cases:
            row = cases.get(case_id)
            if not isinstance(row, dict):
                return None
            split = row.get('split')
            return split if isinstance(split, str) and split in SPLITS else None
        if PBI_TAIL.search(case_id) is None:
            return None
        seed = manifest.get('seed')
        if not isinstance(seed, str) or not seed:
            return None
        return _assigned_split(seed, case_id, _heldout_ratio(manifest))
    except (Exception, SystemExit):
        return None


def is_heldout(manifest, case_id) -> bool:
    """case が held-out か。`split_in` が heldout のときだけ True (投げない)。"""
    return split_in(manifest, case_id) == 'heldout'


def split_of(case_id, name='default', home=None):
    """case の (split, split_set_id)。manifest が無い・壊れている・split が無いときは None。

    manifest にある case は覚えている split をそのまま返す (retire した case は regression のまま)。
    まだ manifest に無い case は、assign が与えるのと同じ split を、同じ種・同じ比率
    (`_heldout_ratio`) で先に返す。build 中に `pbi.attempted` を出しても split が付くようにするため。
    ただし返すのは assign が `cases.jsonl` から拾う形の id (`PBI-<n>` で終わる id) だけで、
    `<repo>:<key>:run` のような擬似 case や assign が決して足さない id は None。
    manifest は書かない (書くのは assign だけ)。

    events.emit が `case_id` を持つイベントのたびに呼ぶので、どんな入力でも投げない。
    """
    try:
        manifest = load_manifest(name, home)
        if manifest is None:
            return None
        split = split_in(manifest, case_id)
        if split is None:
            return None
        return split, manifest['split_set_id']
    except (Exception, SystemExit):
        return None


# ---------- 読み取りの補助 ----------

def _read_jsonl(path: Path) -> list:
    try:
        text = path.read_text(encoding='utf-8')
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _read_cases(base: Path) -> list:
    """`<base>/cases.jsonl` の (case_id, pbi) を、出た順に重複を除いて返す。"""
    out = []
    seen = set()
    for row in _read_jsonl(Path(base) / 'cases.jsonl'):
        cid = row.get('id')
        if not isinstance(cid, str) or not cid or cid in seen:
            continue
        seen.add(cid)
        pbi = row.get('pbi')
        out.append((cid, pbi if isinstance(pbi, str) and pbi else _pbi_of(cid)))
    return out


def _pbi_of(case_id: str):
    match = PBI_TAIL.search(case_id)
    return match.group(1) if match else None


def _profiles(base: Path) -> dict:
    """`<base>/events.jsonl` の task.profiled (scope pbi) を case_id ごとの最後の行にする。"""
    out = {}
    for row in _read_jsonl(Path(base) / 'events.jsonl'):
        if row.get('type') != 'task.profiled':
            continue
        data = row.get('data')
        if not isinstance(data, dict):
            continue
        cid = data.get('case_id')
        if not isinstance(cid, str) or not cid:
            continue
        if data.get('scope') not in (None, 'pbi'):
            continue
        out[cid] = data
    return out


def _strata_for(case_id: str, profiles: dict) -> dict:
    data = profiles.get(case_id) or {}
    size = data.get('size')
    files = size.get('files') if isinstance(size, dict) else None
    return {
        'task_type': data.get('task_type') if isinstance(data.get('task_type'), str) else UNKNOWN,
        'domain': data.get('domain') if isinstance(data.get('domain'), str) else UNKNOWN,
        'size_bucket': files if isinstance(files, str) and files else UNKNOWN,
    }


def _heldout_ratio(manifest: dict) -> float:
    """manifest の heldout_ratio。数値でない (bool を含む) ときは 0.2。assign と split_in で同じ読み方。"""
    ratio = manifest.get('heldout_ratio')
    return ratio if isinstance(ratio, (int, float)) and not isinstance(ratio, bool) else 0.2


def _assigned_split(seed: str, case_id: str, heldout_ratio: float) -> str:
    digest = hashlib.sha256(f'{seed}:{case_id}'.encode('utf-8')).hexdigest()
    if int(digest, 16) / 16 ** 64 < heldout_ratio:
        return 'heldout'
    return 'train'


def _set_id(name: str, seed: str) -> str:
    return f'{name}-{hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]}'


# ---------- イベント ----------

def _emit(base, type_: str, data: dict, *, pbi=None, envelope=None, once=None) -> bool:
    """events を関数の中で import する (events.py から委ねられたときの循環を避ける)。失敗は黙って飛ばす。"""
    if base is None:
        return False
    try:
        import events
    except (Exception, SystemExit):
        return False
    try:
        return bool(events.emit(base, type_, data, pbi=pbi, envelope=envelope, once=once))
    except (Exception, SystemExit):
        return False


def _announce(base, type_: str, case_id: str, split: str, set_id: str, reason: str, pbi) -> bool:
    data = {'case_id': case_id, 'split': split, 'split_set_id': set_id, 'reason': reason}
    envelope = {'split': split, 'split_set_id': set_id}
    return _emit(base, type_, data, pbi=pbi, envelope=envelope,
                 once=f'{type_}:{set_id}:{case_id}')


# ---------- CLI ----------

def _usage(message: str) -> int:
    print('splits: ' + message, file=sys.stderr)
    return 2


def _parse(parser, argv):
    try:
        return parser.parse_args(argv), None
    except SystemExit as error:
        return None, error.code if isinstance(error.code, int) else 2


def _init(argv) -> int:
    parser = argparse.ArgumentParser(prog='splits.py init', description='split の manifest を作る')
    parser.add_argument('--name', default='default')
    parser.add_argument('--seed', required=True)
    parser.add_argument('--heldout-ratio', dest='heldout_ratio', type=float, default=0.2)
    parser.add_argument('--home')
    args, code = _parse(parser, argv)
    if args is None:
        return code if code is not None else 2
    if not _valid_name(args.name):
        return _usage(f'bad --name: {args.name}')
    if not args.seed:
        return _usage('--seed must not be empty')
    if not (0.0 <= args.heldout_ratio <= 1.0):
        return _usage(f'bad --heldout-ratio: {args.heldout_ratio}')
    path = _manifest_path(args.name, args.home)
    if path.exists():
        return _usage(f'manifest already exists: {path}')
    manifest = {
        'schema': MANIFEST_SCHEMA,
        'name': args.name,
        'split_set_id': _set_id(args.name, args.seed),
        'seed': args.seed,
        'heldout_ratio': args.heldout_ratio,
        'strata': list(STRATA),
        'frozen': True,
        'cases': {},
    }
    if not _save(path, manifest):
        return _usage(f'cannot write manifest: {path}')
    return 0


def _assign(argv) -> int:
    parser = argparse.ArgumentParser(prog='splits.py assign', description='cases.jsonl の新しい case を振る')
    parser.add_argument('--dir', default='docs/pbi')
    parser.add_argument('--name', default='default')
    parser.add_argument('--home')
    args, code = _parse(parser, argv)
    if args is None:
        return code if code is not None else 2
    if not _valid_name(args.name):
        return _usage(f'bad --name: {args.name}')
    manifest = load_manifest(args.name, args.home)
    if manifest is None:
        return _usage(f'no manifest: {_manifest_path(args.name, args.home)}')
    base = Path(args.dir)
    profiles = _profiles(base)
    cases = manifest['cases']
    ratio = _heldout_ratio(manifest)
    seed = manifest['seed']
    set_id = manifest['split_set_id']
    fresh = []
    for cid, pbi in _read_cases(base):
        if cid in cases:
            continue
        split = _assigned_split(seed, cid, ratio)
        entry = {'split': split, 'strata': _strata_for(cid, profiles)}
        if pbi:
            entry['pbi'] = pbi
        cases[cid] = entry
        fresh.append((cid, split, pbi))
    if not fresh:
        return 0
    if not _save(_manifest_path(args.name, args.home), manifest):
        return _usage(f'cannot write manifest: {_manifest_path(args.name, args.home)}')
    for cid, split, pbi in fresh:
        _announce(base, ASSIGNED, cid, split, set_id, 'seeded', pbi)
    return 0


def _retire(argv) -> int:
    parser = argparse.ArgumentParser(prog='splits.py retire',
                                     description='heldout の case を regression に移す')
    parser.add_argument('--case', required=True)
    parser.add_argument('--reason', choices=('read-heldout', 'manual'), default='read-heldout')
    parser.add_argument('--name', default='default')
    parser.add_argument('--home')
    parser.add_argument('--dir')
    args, code = _parse(parser, argv)
    if args is None:
        return code if code is not None else 2
    if not _valid_name(args.name):
        return _usage(f'bad --name: {args.name}')
    manifest = load_manifest(args.name, args.home)
    if manifest is None:
        return _usage(f'no manifest: {_manifest_path(args.name, args.home)}')
    if split_in(manifest, args.case) != 'heldout':
        return _usage(f'not heldout: {args.case}')
    base = Path(args.dir) if args.dir else None
    stored = manifest['cases'].get(args.case)
    fresh = not isinstance(stored, dict)
    if not fresh:
        pbi = stored.get('pbi')
        stored['split'] = 'regression'
    else:
        # assign 前でも、assign が後で入れるのと同じ予測が heldout なら regression に移す
        pbi = _pbi_of(args.case)
        if base is not None:
            for cid, case_pbi in _read_cases(base):
                if cid == args.case:
                    pbi = case_pbi
                    break
        entry = {'split': 'regression',
                 'strata': _strata_for(args.case, _profiles(base) if base is not None else {})}
        if isinstance(pbi, str) and pbi:
            entry['pbi'] = pbi
        manifest['cases'][args.case] = entry
    if not _save(_manifest_path(args.name, args.home), manifest):
        return _usage(f'cannot write manifest: {_manifest_path(args.name, args.home)}')
    if base is not None:
        if not fresh and (not isinstance(pbi, str) or not pbi):
            for cid, case_pbi in _read_cases(base):
                if cid == args.case:
                    pbi = case_pbi
                    break
        _announce(base, RETIRED, args.case, 'regression', manifest['split_set_id'],
                  args.reason, pbi if isinstance(pbi, str) else _pbi_of(args.case))
    return 0


def main(argv=None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list and args_list[0] == 'split':
        args_list = args_list[1:]  # `events.py split ...` から直接呼ばれたとき
    if not args_list or args_list[0] not in ('init', 'assign', 'retire'):
        return _usage('usage: splits.py init|assign|retire ...')
    try:
        if args_list[0] == 'init':
            return _init(args_list[1:])
        if args_list[0] == 'assign':
            return _assign(args_list[1:])
        return _retire(args_list[1:])
    except (Exception, SystemExit):
        return 2


if __name__ == '__main__':
    sys.exit(main())
