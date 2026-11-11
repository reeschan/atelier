"""長く走る子 (作業者・役割) を切り離して起こし、あとで待つ。Agent ツールも run_in_background も無い設計者 (codex) 用。

使い方 (リポジトリのルートで):
  python <このディレクトリ>/background.py start --name NAME [--dir docs/pbi/bg] -- COMMAND [ARGS...]
  python <このディレクトリ>/background.py wait --name NAME [--timeout 300] [--dir docs/pbi/bg]
  python <このディレクトリ>/background.py status [--name NAME] [--dir docs/pbi/bg]

start は子を切り離して起こし、<dir>/<NAME>.json に pid と開始時刻を書いてすぐ戻る。子の標準出力と標準エラーは
<dir>/<NAME>.log に、終わったら終了コードを <dir>/<NAME>.exit に書く (見張りのプロセスが書く)。
wait は終われば `state: done exit <n>` を出して 0 で、--timeout 秒で終わらなければ `state: running` を出して 3 で戻る。
見張りが死んで終了コードの無いまま子もいなければ `state: lost` を出して 4 で戻る。

終了コード: 0 終わった (start は起こした) / 2 引数の誤り・同じ名前が動いている / 3 まだ動いている / 4 行方が分からない
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
POLL = 1.0
STARTING = 30.0  # 見張りの pid がまだ無くても、起こしてからこの秒数は running とみなす


def now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


def fail(msg: str) -> None:
    print(f'background: {msg}', file=sys.stderr)
    sys.exit(2)


def pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def files(base: Path, name: str) -> dict:
    return {key: base / f'{name}.{key}' for key in ('json', 'log', 'exit', 'pid')}


def read_exit(path: Path):
    try:
        return int(path.read_text(encoding='utf-8').strip())
    except (OSError, ValueError):
        return None


def state_of(base: Path, name: str):
    """(状態, 終了コード)。状態は done・running・lost・none。"""
    f = files(base, name)
    code = read_exit(f['exit'])
    if code is not None:
        return 'done', code
    if not f['json'].is_file():
        return 'none', None
    try:
        watcher = int(f['pid'].read_text(encoding='utf-8').strip())
    except (OSError, ValueError):
        watcher = None
    if watcher is None and time.time() - f['json'].stat().st_mtime < STARTING:
        return 'running', None  # start が見張りの pid を書く前
    if pid_alive(watcher):
        return 'running', None
    # 見張りが書き終える前に読んだときのために、もう一度だけ見る
    code = read_exit(f['exit'])
    return ('done', code) if code is not None else ('lost', None)


def watch(args) -> int:
    """見張り: 子を起こして待ち、終了コードを書く (start が切り離して起こす)。"""
    base = Path(args.dir)
    f = files(base, args.name)
    with open(f['log'], 'ab') as log:
        try:
            child = subprocess.Popen(args.command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except OSError as error:
            log.write(f'background: cannot start: {error}\n'.encode('utf-8'))
            f['exit'].write_text('127\n', encoding='utf-8')
            return 127
        info = json.loads(f['json'].read_text(encoding='utf-8'))
        info['child_pid'] = child.pid
        f['json'].write_text(json.dumps(info, ensure_ascii=False) + '\n', encoding='utf-8')
        code = child.wait()
    info['ended_at'] = now()
    info['exit'] = code
    f['json'].write_text(json.dumps(info, ensure_ascii=False) + '\n', encoding='utf-8')
    f['exit'].write_text(f'{code}\n', encoding='utf-8')
    return code


def start(args) -> int:
    if not args.command:
        fail('give the command after --')
    base = Path(args.dir)
    base.mkdir(parents=True, exist_ok=True)
    state, _ = state_of(base, args.name)
    if state == 'running':
        fail(f'{args.name} is still running; wait for it or pick another name')
    f = files(base, args.name)
    for path in f.values():
        try:
            path.unlink()
        except OSError:
            pass
    f['json'].write_text(json.dumps({'name': args.name, 'command': args.command, 'started_at': now()},
                                    ensure_ascii=False) + '\n', encoding='utf-8')
    argv = [sys.executable, str(Path(__file__).resolve()), '_watch', '--name', args.name, '--dir', str(base),
            '--', *args.command]
    extra = {}
    if os.name == 'nt':
        extra['creationflags'] = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                                  | getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    else:
        extra['start_new_session'] = True
    watcher = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, cwd=str(Path.cwd()), **extra)
    # 見張りの pid は別のファイルに書く (見張りが .json を書き換えるのと競らない)
    f['pid'].write_text(f'{watcher.pid}\n', encoding='utf-8')
    print(f"state: started\nname: {args.name}\nlog: {f['log'].as_posix()}\n"
          f"then: python {Path(__file__).resolve().as_posix()} wait --name {args.name} --timeout 300")
    return 0


def wait(args) -> int:
    base = Path(args.dir)
    deadline = time.monotonic() + max(0.0, args.timeout)
    while True:
        state, code = state_of(base, args.name)
        if state == 'done':
            print(f'state: done exit {code}')
            return 0
        if state == 'none':
            fail(f'no job named {args.name} in {base.as_posix()}')
        if state == 'lost':
            print('state: lost (the watcher is gone and left no exit code)')
            return 4
        if time.monotonic() >= deadline:
            print('state: running')
            return 3
        time.sleep(POLL)


def status(args) -> int:
    base = Path(args.dir)
    names = [args.name] if args.name else sorted(p.name[:-5] for p in base.glob('*.json')) if base.is_dir() else []
    for name in names:
        state, code = state_of(base, name)
        print(f'{name}  {state}' + (f' exit {code}' if code is not None else ''))
    return 0


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding='utf-8')
    argv = list(sys.argv[1:] if argv is None else argv)
    command = []
    if '--' in argv:
        cut = argv.index('--')
        argv, command = argv[:cut], argv[cut + 1:]
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ('start', 'wait', 'status', '_watch'):
        p = sub.add_parser(name)
        p.add_argument('--name', required=name != 'status')
        p.add_argument('--dir', default='docs/pbi/bg')
        if name == 'wait':
            p.add_argument('--timeout', type=float, default=300.0)
    args = ap.parse_args(argv)
    args.command = command
    if args.name is not None and not NAME.match(args.name):
        fail(f'the name has characters it cannot use: {args.name!r}')
    return {'start': start, 'wait': wait, 'status': status, '_watch': watch}[args.cmd](args)


if __name__ == '__main__':
    sys.exit(main())
