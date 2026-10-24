"""PBI の「触らない範囲」のファイルが変わっていないかを、起動前のハッシュと比べて確かめる (appendix/plan.md の「Writing verify commands」)。

git status --porcelain -- <files> の代わりに使う。git status は、前の波の未コミットの新しいファイル (??) や
ほかの PBI の変更でも行を出すので、その PBI が触ったかどうかを区別できない。このスクリプトは、
起動前に取ったハッシュと今の中身だけを比べる。

opencode などはリポジトリの外を読めないので、開始時に docs/pbi/tools/unchanged.py にコピーして使う。
使い方 (リポジトリのルートで):
  python docs/pbi/tools/unchanged.py save PBI-003 <ファイル> ...   # 設計者が PBI を起動する直前に実行
  python docs/pbi/tools/unchanged.py check PBI-003                 # 検証コマンドに書く

ハッシュは <--dir>/verify/<名前>.unchanged.json に置く (--dir の既定は docs/pbi)。
save の時点で無いファイルは「無い」として記録し、check の時点でできていれば変わったとみなす。
check の終了コード: 0 = どれも変わっていない / 1 = 変わったファイルがある (`changed <パス>` を 1 行ずつ出す) /
2 = save の記録が無い。
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path


def digest(path):
    p = Path(path)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None


def snapshot_path(base, name):
    return Path(base) / 'verify' / f'{name}.unchanged.json'


def save(base, name, files):
    snap = snapshot_path(base, name)
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(json.dumps({f: digest(f) for f in files}, ensure_ascii=False, indent=1), encoding='utf-8')
    return snap


def changed(base, name):
    saved = json.loads(snapshot_path(base, name).read_text(encoding='utf-8'))
    return [f for f, h in saved.items() if digest(f) != h]


def main(argv=None):
    ap = argparse.ArgumentParser(description='触らない範囲のファイルが起動前から変わっていないかを確かめる')
    ap.add_argument('--dir', default='docs/pbi', help='作業記録のディレクトリ (既定 docs/pbi)')
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('save', help='起動前のハッシュを取る')
    s.add_argument('name')
    s.add_argument('files', nargs='+')
    c = sub.add_parser('check', help='変わったファイルだけを出す')
    c.add_argument('name')
    args = ap.parse_args(argv)

    if args.cmd == 'save':
        snap = save(args.dir, args.name, args.files)
        print(f'saved {len(args.files)} files to {snap.as_posix()}')
        return 0
    if not snapshot_path(args.dir, args.name).is_file():
        print(f'no snapshot for {args.name}: run save first', file=sys.stderr)
        return 2
    files = changed(args.dir, args.name)
    for f in files:
        print(f'changed {f}')
    return 1 if files else 0


if __name__ == '__main__':
    sys.exit(main())
