"""OMM (oh-my-mermaid) の走査の結果を、リポジトリの外にフィンガープリント付きで置いて使い回す (appendix/map.md)。

  python omm_cache.py lookup <pbi-dir> --base REF [--cache-dir DIR] [--home DIR]
  python omm_cache.py store  <pbi-dir> --base REF [--perspectives a,b] [--note TEXT] [--cache-dir DIR] [--home DIR]
  python omm_cache.py check  <pbi-dir>
  python omm_cache.py export <pbi-dir> --base REF [--cache-dir DIR] [--home DIR]

置き場所: --cache-dir、home の agents.json の omm.cache_dir、無ければメインの clone の親フォルダの .omm-cache。
その下の <メインの clone の名前>/<フィンガープリントの先頭 12 桁>/ に .omm/ と fingerprint.json を置く。
フィンガープリント: <pbi-dir>/map.json の scan_dirs (既定は src があれば ["src"]、無ければ ["."]) の、
マージ基点 (git merge-base REF HEAD) での木のハッシュ (git rev-parse <基点>:<dir>)。ディレクトリが 1 つなら
そのハッシュ、2 つ以上なら並べたものの sha256。

lookup: `hit <フォルダ>` か `miss <作るフォルダ>` を 1 行出す (どちらも終了コード 0)。
store: 作業ツリーの .omm/ を置き場所へ移し、fingerprint.json を書き、作業ツリーに .omm/ が残っていないことを
  確かめる。.omm/ を git が追っているリポジトリでは何も動かさず終了コード 2。
check: 作業ツリーに .omm/ があれば `left <パス>` で終了コード 1、無ければ `none` で 0。
export: 当たりの置き場所の .omm/ を作業ツリーへ写し (置き場所には残す)、`placed <パス>` を出す。ユーザーが
  リポジトリに置くと選んだときに finish で使う。作業ツリーの .omm/ は、git が追っていて手元の変更も追っていない
  ファイルも無いときだけ置き換える (git で戻せる)。それ以外は上書きせず終了コード 2。写しは横の一時フォルダに
  作ってから入れ替え、途中で失敗したら元の .omm/ を戻す。
終了コード: 0 = 正常、1 = (check) .omm/ が残っている、2 = 引数・git・入力の誤り (export の外れを含む)。
"""
import argparse
import datetime
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import common

# Windows で子のコンソールのウインドウを出さない (ヘッドレス)。ほかの OS では 0
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

OMM = '.omm'
PREFIX = 12


def git(root: Path, *args: str) -> str:
    done = subprocess.run(['git', *args], cwd=root, capture_output=True, text=True, encoding='utf-8',
                          creationflags=NO_WINDOW)
    if done.returncode != 0:
        raise RuntimeError(f'git {" ".join(args)}: {done.stderr.strip()}')
    return done.stdout.strip()


def repo_root(pbi: Path) -> Path:
    return Path(git(pbi.resolve(), 'rev-parse', '--show-toplevel'))


def main_clone(root: Path) -> Path:
    """worktree でも、メインの clone (共通の .git の親) を返す。"""
    common_dir = Path(git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
    return common_dir.parent if common_dir.name == '.git' else common_dir


def scan_dirs(pbi: Path, root: Path, commit: str) -> list[str]:
    data = common.load_json(pbi / 'map.json', {})
    dirs = data.get('scan_dirs') if isinstance(data, dict) else None
    if isinstance(dirs, list) and dirs and all(isinstance(d, str) and d for d in dirs):
        return dirs
    has_src = subprocess.run(['git', 'cat-file', '-e', f'{commit}:src'], cwd=root, capture_output=True,
                             creationflags=NO_WINDOW).returncode == 0
    return ['src'] if has_src else ['.']


def trees(root: Path, commit: str, dirs: list[str]) -> dict:
    found = {}
    for d in dirs:
        spec = f'{commit}^{{tree}}' if d in ('.', '') else f'{commit}:{d.rstrip("/")}'
        found[d] = git(root, 'rev-parse', spec)
    return found


def fingerprint(found: dict) -> str:
    if len(found) == 1:
        return next(iter(found.values()))
    joined = '\n'.join(f'{d} {h}' for d, h in sorted(found.items()))
    return hashlib.sha256(joined.encode('utf-8')).hexdigest()


def cache_root(args, root: Path) -> Path:
    if args.cache_dir:
        return Path(args.cache_dir)
    agents = common.load_json(common.resolve_home(args.home) / 'agents.json', {})
    configured = (agents.get('omm') or {}).get('cache_dir') if isinstance(agents, dict) else None
    if isinstance(configured, str) and configured:
        return Path(configured)
    return main_clone(root).parent / '.omm-cache'


def locate(args, pbi: Path) -> tuple[Path, Path, str, dict]:
    """(リポジトリのルート, キャッシュのフォルダ, 基点のコミット, 木のハッシュ)。"""
    root = repo_root(pbi)
    commit = git(root, 'merge-base', args.base, 'HEAD')
    found = trees(root, commit, scan_dirs(pbi, root, commit))
    folder = cache_root(args, root) / main_clone(root).name / fingerprint(found)[:PREFIX]
    return root, folder, commit, found


def is_hit(folder: Path, found: dict) -> bool:
    """置き場所の記録が今のフィンガープリントと合い、.omm/ が在るか。"""
    stored = common.load_json(folder / 'fingerprint.json', {})
    same = isinstance(stored, dict) and (stored.get('trees') == found
                                         or (list(found) == ['src'] and stored.get('src_tree') == found['src']))
    return same and (folder / OMM).is_dir()


def cmd_lookup(args, pbi: Path) -> int:
    _root, folder, _commit, found = locate(args, pbi)
    print(f'{"hit" if is_hit(folder, found) else "miss"} {folder}')
    return 0


def remote_name(root: Path) -> str | None:
    done = subprocess.run(['git', 'remote', 'get-url', 'origin'], cwd=root, capture_output=True, text=True,
                          creationflags=NO_WINDOW)
    url = done.stdout.strip()
    if done.returncode != 0 or not url:
        return None
    tail = url.rstrip('/').removesuffix('.git').replace(':', '/').split('/')
    return '/'.join(tail[-2:])


def omm_version() -> str | None:
    found = shutil.which('omm')
    if not found:
        return None
    done = subprocess.run([found, '--version'], capture_output=True, text=True, creationflags=NO_WINDOW)
    return f'omm {done.stdout.strip()}' if done.returncode == 0 and done.stdout.strip() else None


def cmd_store(args, pbi: Path) -> int:
    root, folder, commit, found = locate(args, pbi)
    source = root / OMM
    if git(root, 'ls-files', '--', OMM):
        print(f'error {OMM}/ is tracked in this repository; leave it as it is', file=sys.stderr)
        return 2
    if not source.is_dir():
        print(f'error {source} is missing (run the scan first)', file=sys.stderr)
        return 2
    folder.mkdir(parents=True, exist_ok=True)
    # 移す途中で止まっても、古い記録が新しい .omm/ を当たりにしないよう、先に記録を消す
    (folder / 'fingerprint.json').unlink(missing_ok=True)
    target = folder / OMM
    if target.exists():
        shutil.rmtree(target)
    shutil.move(str(source), str(target))
    record = {
        'repository': remote_name(root),
        'commit': commit,
        'scan_dirs': list(found),
        'trees': found,
        'fingerprint_rule': 'git rev-parse <commit>:<dir> for each scan_dirs entry equals trees: reuse this .omm/',
        'omm_version': omm_version(),
        'scanned_at': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
        'perspectives': [p for p in (args.perspectives or '').split(',') if p],
        'scanned_for': args.note or 'atelier map',
    }
    if list(found) == ['src']:
        record['src_tree'] = found['src']
    common.write_json(folder / 'fingerprint.json', record)
    if source.exists():
        print(f'error {source} is still in the working tree', file=sys.stderr)
        return 2
    print(f'stored {folder}')
    return 0


def cmd_check(_args, pbi: Path) -> int:
    left = repo_root(pbi) / OMM
    if left.exists():
        print(f'left {left}')
        return 1
    print('none')
    return 0


def cmd_export(args, pbi: Path) -> int:
    root, folder, _commit, found = locate(args, pbi)
    if not is_hit(folder, found):
        print(f'error no stored {OMM}/ for this base in {folder} (run the map stage first)', file=sys.stderr)
        return 2
    target = root / OMM
    if target.exists():
        if not git(root, 'ls-files', '--', OMM):
            print(f'error {target} is already in the working tree and not tracked; leave it as it is', file=sys.stderr)
            return 2
        if git(root, 'status', '--porcelain', '--untracked-files=all', '--', OMM):
            print(f'error {target} has local changes or untracked files; leave it as it is', file=sys.stderr)
            return 2
    place(folder / OMM, target)
    print(f'placed {target}')
    return 0


def place(source: Path, target: Path) -> None:
    """source を target に写す。横の一時フォルダに写してから入れ替え、失敗したら元の target を戻す。"""
    fresh = target.with_name(f'{target.name}.export-{os.getpid()}')
    old = target.with_name(f'{target.name}.replaced-{os.getpid()}')
    shutil.copytree(source, fresh)
    try:
        if target.exists():
            target.rename(old)
        try:
            fresh.rename(target)
        except OSError:
            if old.exists():
                old.rename(target)
            raise
    except OSError:
        shutil.rmtree(fresh, ignore_errors=True)
        raise
    shutil.rmtree(old, ignore_errors=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='command', required=True)
    for name in ('lookup', 'store', 'check', 'export'):
        p = sub.add_parser(name)
        p.add_argument('pbi_dir')
        if name != 'check':
            p.add_argument('--base', required=True, help='マージ先のブランチ')
            p.add_argument('--cache-dir')
            p.add_argument('--home')
        if name == 'store':
            p.add_argument('--perspectives')
            p.add_argument('--note')
    args = ap.parse_args(argv)
    common.utf8_stdout()
    pbi = Path(args.pbi_dir)
    if not pbi.is_dir():
        print(f'{args.pbi_dir}: not a directory', file=sys.stderr)
        return 2
    try:
        return {'lookup': cmd_lookup, 'store': cmd_store, 'check': cmd_check,
                'export': cmd_export}[args.command](args, pbi)
    except (RuntimeError, OSError) as error:
        print(f'error {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
