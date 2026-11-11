"""進行役: 設計者の出番を 1 つずつ回す (LLM ではないスクリプト)。run の状態を <dir>/conductor/run.json に持つ。

使い方 (リポジトリのルートで):
  python <このディレクトリ>/conductor.py start --goal TEXT|--goal-file F [--workflow N] [--launch agent|headless] [--force] [--dir docs/pbi]
  python <このディレクトリ>/conductor.py next [--relaunch] [--dir docs/pbi]
  python <このディレクトリ>/conductor.py done [--dir docs/pbi]
  python <このディレクトリ>/conductor.py answer (--reply F | --text T | --approve [--choices 1=a,2=b] | --revise (--text T | --reply F)) [--dir docs/pbi]
  python <このディレクトリ>/conductor.py run [--max-episodes N] [--detach] [--launch agent|headless] [--dir docs/pbi]
  python <このディレクトリ>/conductor.py wait [--timeout S] [--dir docs/pbi]
  python <このディレクトリ>/conductor.py status [--dir docs/pbi]
  python <このディレクトリ>/conductor.py abort [--reason T] [--dir docs/pbi]

置き場所: <dir>/conductor/ の run.json (schema atelier.run/1)・goal.md・lock・episodes/<seq>-<出番>.prompt.md・
<run_id の先頭 8 文字>-<seq>-<出番>.result.json (run_id の無い run.json は <seq>-<出番>.result.json)・.alive・answers/<seq>-<種類>.md。
波の中身は <dir>/waves.json。
状態: none・ready・running・question・retry・finished・aborted。出力の 1 行目はいつも `state: <状態>`、全部で 8 行以内。
run は headless で設計者を `claude -p` (環境変数 ATELIER_DESIGNER_CMD で差し替え可) から起こし、依頼文・起動・done を
問い・終わり・失敗まで繰り返す。claude -p の子を待つ間はロックを持たず、retry になったら 12 で止まる。
run --detach は runner_pid を残してロックを放ち、自分を切り離してすぐ running で返る。wait は running でなくなるか
--timeout 秒まで run.json を読む (ロックは取らない)。running の子も runner も生きていなければ、timeout を待たずに stale を出して返る。

終了コード: 0 進める・終わった・running の案内 / 10 ユーザーの答え待ち / 12 起動か結果の失敗 /
2 引数と状態の誤り (ロック・答える問いが無い・終わっていない run がある start・waves.json に無い波・running でない run の --relaunch・
headless の設計者か runner が動いている間の done (state: running と then: wait を出す))

立て直し: next --relaunch は止まった running の子を止めて新しい seq で起こし直す。launch が headless の run の next は
起動の指示を出さず run --detach を案内する。no-agent-tool の問いのあとに run --launch headless が呼ばれたら、問いを閉じて
その出番を headless で起こす。start --force は前の run の子を止めてから run.json を退ける。失敗 (結果なし・壊れ・表に無い next・error) は retry にして数え、同じ出番で 2 回続けば
launch-failure の問い、handoff は進み (runs.jsonl の行数と STATUS.md のハッシュ) が 3 回続けて無ければ handoff-loop の問い、
error の no-agent-tool は数えずに decision の問い (どれも <dir>/conductor/episodes/<seq>-<種類>.question.md に説明を書く)。
エージェント定義が無ければ warn を出し general-purpose で起動する。
子の pid (child_pid・runner_pid) は record_pid で起動の時刻 (<key>_started) と一緒に残す。止める前に時刻を比べ、合わない・
読めない pid は自分の子でない (使い回し) として止めず、run.json から消して warn を出す。時刻の記録が無い古い run.json は、
launched_at より前に起きたプロセスだけ止める。
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import common
import claude_run
import role_results

SCHEMA = 'atelier.run/1'
SKILL = Path(__file__).resolve().parent
TEMPLATE = SKILL / 'prompts' / 'designer-episode.md'

# Windows で子プロセスのコンソールのウインドウを出さない (ほかのスクリプトと同じ。Windows 以外は 0)
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_ASK = 10
EXIT_FAIL = 12

CLOSED = ('finished', 'aborted')
MAX_LINES = 8
LOCK_WAIT = 5.0
SUMMARY_MAX = 200
# Windows では読み手がファイルを開いている間 os.replace が PermissionError になる。短く取り直す
REPLACE_WAIT = 2.0  # 置き換えを取り直す長さ (秒)
READ_WAIT = 1.0  # 読めない run.json を取り直す長さ (秒)
REMOVE_WAIT = 2.0  # 手放したロックを消すのを取り直す長さ (秒)
STOP_WAIT = 3.0  # 止めた子が死ぬのを待つ長さ (秒)
WAIT_POLL = 1.0  # wait が run.json を読み直す間隔 (秒)
WAIT_LOCK_WAIT = 5.0  # wait が、running でなくなったあとにロックの消えるのを待つ長さ (秒)
WAIT_DEFAULT = 300.0  # wait --timeout の既定 (秒)
LOOP_LIMIT = 3  # 進みの無い handoff がこの回数続いたら handoff-loop の問い
FAIL_LIMIT = 2  # 同じ出番の失敗がこの回数続いたら launch-failure の問い
AGENT_FILE = 'atelier-designer.md'
AGENT_ROLE = AGENT_FILE[:-3]
AGENT_DEFINITION = Path(__file__).resolve().parent / 'agents' / AGENT_FILE
STATES = ('none', 'ready', 'running', 'question', 'retry', 'finished', 'aborted')
# run.json が在るのに読めず、この処理で一度も状態を読めていないときに 1 行目に出す状態。
# none は「無い」と誤らせ、finished・aborted は start を誘うので、起こし直し待ちの retry にする
UNREADABLE_STATE = 'retry'

# 出番のつながり (done の next に許すもの)。finish は run を閉じる
NEXT_ALLOWED = {
    'design': ('build',),
    'build': ('build', 'test', 'review', 'finish'),
    'test': ('build', 'review'),
    'review': ('build', 'finish'),
    'finish': (),
}

DEFAULT_ROLES = {
    'designer': {'model': 'opus', 'effort': None, 'permission_mode': 'auto'},
    'reviewer': {'deep': {'model': 'opus', 'effort': None}, 'other': {'model': 'sonnet', 'effort': 'high'}},
    'tester': {'model': 'sonnet', 'effort': None},
}

DECISION = re.compile(r'^(\d+)\. \[([^\]]*)\] (.*)$')
ANSWER_LINE = re.compile(r'^ {3}[←→] (.*)$')
UNANSWERED = '**—**'
META_PREFIX = 'reply-meta-'
FENCE_OPEN = '```am-reply-meta'


class Refused(Exception):
    """引数か状態の誤り (終了コード 2)。メッセージは error: の行になる。"""


class Locked(Refused):
    """生きているロックがある (終了コード 2)。"""


class Unusable(Refused):
    """run.json は在るが使えない (取り直しても読めない・JSON のオブジェクトでない・schema が違う)。終了コード 2。"""


# ---- 出力 ----

def fold(text, limit=None) -> str:
    """複数行の文を 1 行に畳む (行の区切りは ' / ')。limit があればそこで切る。"""
    parts = [part.strip() for part in str(text).splitlines() if part.strip()]
    line = ' / '.join(parts)
    if limit is not None and len(line) > limit:
        line = line[:limit - 3] + '...'
    return line


def emit(lines) -> None:
    """行を出す。複数行の値は 1 行に畳む。8 行を超えるときは warn の行を 1 行にまとめ、それでも超えれば切る。"""
    lines = [fold(line) for line in lines if line is not None]
    if len(lines) > MAX_LINES:
        warns = [line for line in lines if line.startswith('warn ')]
        rest = [line for line in lines if not line.startswith('warn ')]
        if warns:
            rest.insert(1, f'{warns[0]} (+{len(warns) - 1} more warn)' if len(warns) > 1 else warns[0])
        lines = rest[:MAX_LINES]
    print('\n'.join(lines))


def quoted(path) -> str:
    text = Path(path).as_posix()
    return f'"{text}"' if ' ' in text else text


def command(*words) -> str:
    """呼び出し側に出す、このスクリプトのコマンドの行。"""
    return ' '.join(['python', quoted(SKILL / 'conductor.py'), *words])


def iso_now() -> str:
    return common.now().isoformat(timespec='seconds')


# ---- パス ----

class Paths:
    def __init__(self, base: str):
        self.base = Path(base)
        self.conductor = self.base / 'conductor'
        self.run = self.conductor / 'run.json'
        self.lock = self.conductor / 'lock'
        self.goal = self.conductor / 'goal.md'
        self.episodes = self.conductor / 'episodes'
        self.answers = self.conductor / 'answers'
        self.seen = None  # 最後に読んだ run の状態 (main の error の行に使う)

    def episode_file(self, seq: int, episode: str, suffix: str) -> Path:
        return self.episodes / f'{seq}-{episode}{suffix}'

    def result_file(self, seq: int, episode: str, run_short: str, launcher: str) -> Path:
        """出番の結果のパス。起動のしかたによらず、名前に run_id の先頭 8 文字を入れる。

        agent の設計者 (Agent ツールの子) は start --force で止められず、headless の設計者も止められなければ
        stuck_pids に移るだけで動き続ける。前の run の設計者があとから書いた結果を新しい run が受け取らない
        ように、<run8>-<seq>-<出番>.result.json にする。run_id の無い run.json だけ <seq>-<出番>.result.json。
        launcher は呼び出し元との互換のために受け取るだけで、名前には効かない。
        """
        if not run_short:
            return self.episode_file(seq, episode, '.result.json')
        return self.episodes / f'{run_short}-{seq}-{episode}.result.json'


def _backoff(attempt: int, cap: float) -> None:
    time.sleep(min(0.005 * 2 ** attempt, cap))


def load_run(paths: Paths):
    """run.json を読む。無ければ None (state: none)。

    書き換えの途中で読めない (PermissionError・JSON が壊れている) ときは READ_WAIT 秒まで取り直し、
    それでも読めなければ Unusable にする。JSON のオブジェクトでない・schema が違う・state が STATES に無いものも Unusable。
    読めない・使えない run.json を none と言わない。ロックは取らない (status はこのまま読む)。
    """
    missing = 0
    problem = None
    attempt = 0
    deadline = time.monotonic() + READ_WAIT
    while True:
        try:
            data = json.loads(paths.run.read_text(encoding='utf-8'))
        except FileNotFoundError:
            # 置き換えの瞬間を無いと見誤らないよう、もう 1 度だけ見る
            missing += 1
            if missing >= 2:
                paths.seen = 'none'
                return None
            problem = 'missing'
        except (OSError, ValueError) as error:
            problem = error
        else:
            if not isinstance(data, dict):
                raise Unusable(f'run.json is not an {SCHEMA} run (not a JSON object)')
            if data.get('schema') != SCHEMA:
                raise Unusable(f"run.json has schema {data.get('schema')!r}, not {SCHEMA}")
            state = data.get('state')
            if state not in STATES:
                raise Unusable(f'run.json has no valid state ({state!r})')
            paths.seen = state
            return data
        if time.monotonic() >= deadline:
            break
        _backoff(attempt, 0.1)
        attempt += 1
    raise Unusable(f'run.json unreadable ({fold(problem)})')


def replace_retry(source, target) -> None:
    """os.replace を、PermissionError のあいだ短く取り直す (Windows で読み手が開いている間)。"""
    # status はロックを取らずに読むので、読み手が開いていない隙を拾うよう待ちは短く刻む
    deadline = time.monotonic() + REPLACE_WAIT
    attempt = 0
    while True:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            _backoff(attempt, 0.02)
            attempt += 1


def write_json(path, data) -> None:
    """common.write_json と同じ形 (一時ファイル + 置き換え) で書き、置き換えだけを取り直す。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + '\n'
    handle, temporary = tempfile.mkstemp(prefix=f'.{target.name}.', suffix='.tmp', dir=str(target.parent))
    try:
        with os.fdopen(handle, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
        replace_retry(temporary, target)
    except BaseException:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


# ---- プロセスの生死とロック ----

def _win_open(pid: int):
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE, *([ctypes.POINTER(ctypes.c_ulonglong)] * 4))
    process_query_limited_information = 0x1000
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    return kernel32, handle


def pid_alive(pid: int) -> bool:
    """pid のプロセスが生きているか。Windows は OpenProcess と GetExitCodeProcess (os.kill(pid, 0) は Ctrl+C を送る)。"""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel32, handle = _win_open(pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    # 止めたあと親がまだ回収していないゾンビも os.kill(pid, 0) は通るので、終わったものとして扱う
    return not _posix_zombie(pid)


def _posix_zombie(pid: int) -> bool:
    """POSIX で pid がゾンビ (終わったが親が回収していない) か。読めなければ False (生きているとみなす)。"""
    try:
        stat = Path(f'/proc/{pid}/stat')
        if stat.parent.parent.is_dir() and Path('/proc/self/stat').exists():
            # 2 つ目の欄 (コマンド名) は括弧の中に空白を含みうるので、最後の ')' のあとを読む
            return stat.read_text().rsplit(')', 1)[1].split()[0] in ('Z', 'X')
        # /proc が無い POSIX (macOS・BSD) は ps の状態の欄を読む
        out = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True,
                             env=dict(os.environ, LC_ALL='C'), creationflags=NO_WINDOW).stdout.strip()
        return out.startswith('Z')
    except (OSError, IndexError):
        return False


def stop_child(pid) -> bool:
    """pid が生きていれば止める。止まった (もう生きていない) ら True。止められなければ False。

    Windows は OpenProcess(PROCESS_TERMINATE) と TerminateProcess、POSIX は SIGTERM。自分自身は止めない。
    生死の確かめは pid_alive。止めたあと短く待って死んだことを確かめる。
    """
    if not pid_alive(pid):
        return True
    if pid == os.getpid():
        return False
    try:
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            handle = kernel32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
            if not handle:
                return not pid_alive(pid)
            try:
                kernel32.TerminateProcess(handle, 1)
            finally:
                kernel32.CloseHandle(handle)
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError:
        return not pid_alive(pid)
    deadline = time.monotonic() + STOP_WAIT
    while pid_alive(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


PID_KEYS = ('child_pid', 'runner_pid')
START_SLACK = 1.0  # 起動の時刻の比べに許す秒


def record_pid(holder: dict, key: str, pid: int) -> None:
    """pid を run.json に残す。使い回しの確かめ用に、そのプロセスの起動の時刻を <key>_started に一緒に残す。"""
    holder[key] = pid
    begun = process_started(pid)
    if begun is None:
        holder.pop(key + '_started', None)
    else:
        holder[key + '_started'] = begun


def pid_slots(run: dict) -> list:
    """止める対象 (holder, key, pid): running の子 (child_pid)、run --detach の runner_pid (running の中か run の直下)、
    前に止められなかった pid (run の stuck_pids の {pid, pid_started})。"""
    running = run.get('running') or {}
    slots = []
    for holder in (running, run):
        for key in PID_KEYS:
            pid = holder.get(key)
            if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
                slots.append((holder, key, pid))
    stuck = run.get('stuck_pids')
    for holder in stuck if isinstance(stuck, list) else ():
        pid = holder.get('pid') if isinstance(holder, dict) else None
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
            slots.append((holder, 'pid', pid))
    return slots


def move_stuck(target: dict, holder: dict) -> list:
    """holder (running か前の run) に残った child_pid・runner_pid を、target の stuck_pids に移す。移した pid を返す。

    stop_children が止められなかった pid は holder に残っている。running を None にしたり run.json を退けたりしても、
    次の abort・start --force が止めにいけるように run の直下に残す。
    """
    moved = []
    for key in PID_KEYS:
        pid = holder.get(key)
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            continue
        entry = {'pid': pid}
        # 残っているのは自分の子と確かめた pid なので、記録が無ければ今の起動の時刻を残す (次の確かめに使う)
        begun = holder.get(key + '_started')
        if begun is None:
            begun = process_started(pid)
        if begun is not None:
            entry['pid_started'] = begun
        target.setdefault('stuck_pids', []).append(entry)
        moved.append(pid)
        holder.pop(key, None)
        holder.pop(key + '_started', None)
    return moved


def stuck_line(pids) -> str:
    """止められなかった pid を run.json に残したことを知らせる warn の行。"""
    return f"warn stuck pids {' '.join(str(pid) for pid in pids)} kept in run.json; abort or start --force tries again"


def pid_is_ours(holder: dict, key: str, pid: int, launched_at) -> bool:
    """今この pid のプロセスが、記録した子か。pid の使い回しなら False。起動の時刻が読めなくても False。

    記録した時刻 (<key>_started) があれば 1 秒以内で合うこと。古い run.json (記録なし) は launched_at より前に起きたこと
    (ロックの確かめと同じ)。
    """
    begun = process_started(pid)
    if begun is None:
        return False
    recorded = holder.get(key + '_started')
    if isinstance(recorded, (int, float)) and not isinstance(recorded, bool):
        return abs(begun - recorded) <= START_SLACK
    launched = parse_time(launched_at)
    return launched is not None and begun <= launched.timestamp() + START_SLACK


def stop_children(run: dict):
    """生きている子の pid を止める。(warn の行, 止められなかったか) を返す。

    自分の子でない pid (使い回し・時刻が読めない) は止めず、run.json から消して warn を 1 行。止めた pid も消す。
    止められなかった pid だけ残す。
    """
    warns = []
    stuck = False
    running = run.get('running') or {}
    launched_at = running.get('launched_at') or run.get('created_at')
    for holder, key, pid in pid_slots(run):
        if pid_alive(pid) and not pid_is_ours(holder, key, pid, launched_at):
            warns.append(f'warn pid {pid} is not our child; not stopped')
        elif not stop_child(pid):
            warns.append(f'warn child {pid} still alive')
            stuck = True
            continue
        holder.pop(key, None)
        holder.pop(key + '_started', None)
    # 止めた・自分の子でなかった stuck_pids の項目は消す (止められなかったものだけ残る)
    if 'stuck_pids' in run:
        left = [entry for entry in run.get('stuck_pids') or [] if isinstance(entry, dict) and entry.get('pid')]
        if left:
            run['stuck_pids'] = left
        else:
            run.pop('stuck_pids', None)
    return warns, stuck


def process_started(pid: int):
    """プロセスの起動の時刻 (UNIX 時刻の秒)。取れなければ None。"""
    try:
        if os.name == 'nt':
            import ctypes
            kernel32, handle = _win_open(pid)
            if not handle:
                return None
            try:
                times = [ctypes.c_ulonglong() for _ in range(4)]
                if not kernel32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
                    return None
                # FILETIME (1601 年からの 100 ナノ秒) を UNIX 時刻に
                return (times[0].value - 116444736000000000) / 10_000_000
            finally:
                kernel32.CloseHandle(handle)
        if os.path.exists('/proc/self/stat'):
            stat = Path(f'/proc/{pid}/stat').read_text()
            fields = stat.rsplit(')', 1)[1].split()
            ticks = int(fields[19])  # starttime (22 番目の欄)
            boot = next(int(line.split()[1]) for line in Path('/proc/stat').read_text().splitlines()
                        if line.startswith('btime '))
            return boot + ticks / os.sysconf('SC_CLK_TCK')
        # /proc が無い POSIX (macOS・BSD) は ps の lstart (地方時) を読む
        out = subprocess.run(['ps', '-o', 'lstart=', '-p', str(pid)], capture_output=True, text=True,
                             env=dict(os.environ, LC_ALL='C'), creationflags=NO_WINDOW).stdout
        return time.mktime(time.strptime(out.strip(), '%a %b %d %H:%M:%S %Y'))
    except (OSError, ValueError, IndexError, StopIteration, AttributeError):
        return None


def parse_time(text):
    if not isinstance(text, str):
        return None
    try:
        moment = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.astimezone()


def lock_alive(info) -> bool:
    """ロックの持ち主が生きているか。ほかのホストのロックは生きているとみなす。"""
    if info.get('host') != socket.gethostname():
        return True
    pid = info.get('pid')
    if not pid_alive(pid):
        return False
    started = parse_time(info.get('started_at'))
    if started is not None:
        begun = process_started(pid)
        # started_at は秒に丸めてあるので 1 秒の余裕をみる。ロックより後に起きたプロセスは使い回しの pid
        if begun is not None and begun > started.timestamp() + 1:
            return False
    return True


class Lock:
    """<dir>/conductor/lock。状態を読み書きする間だけ持つ。状態を書いてから消す。"""

    def __init__(self, paths: Paths):
        self.path = paths.lock

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + LOCK_WAIT
        holder = None
        while True:
            try:
                handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                holder = self._holder()
                if holder is not None and not holder.get('alive', True):
                    # 外せたときだけすぐ作り直す。外せなければ下の期限と待ちを通る (空回りしない)
                    if self._take_over(holder):
                        continue
            else:
                # 時刻はマイクロ秒まで書く (同じ処理の続けての取り直しでも中身が変わり、手放した印と混ざらない)
                info = {'pid': os.getpid(), 'started_at': common.now().isoformat(), 'host': socket.gethostname()}
                with os.fdopen(handle, 'w', encoding='utf-8') as stream:
                    stream.write(json.dumps(info))
                return self
            if time.monotonic() >= deadline:
                pid = holder.get('pid', '?') if holder else '?'
                host = holder.get('host', '?') if holder else '?'
                raise Locked(f'locked by pid {pid} on {host} ({self.path.as_posix()})')
            time.sleep(0.2)

    @staticmethod
    def _mark(path: Path):
        """ロックのファイルの見分け (中身と更新時刻)。同じ死んだロックかを確かめるのに使う。"""
        try:
            stat = path.stat()
        except OSError:
            return None
        try:
            text = path.read_text(encoding='utf-8')
        except OSError:
            text = None
        return text, stat.st_mtime_ns, stat.st_size

    def _holder(self):
        """今のロックの中身と生死。読めないロックは、5 秒より古ければ死とみなす (書きかけの途中を待つ)。"""
        mark = self._mark(self.path)
        if mark is None:
            return None
        try:
            if mark[0] is None:
                raise OSError
            info = json.loads(mark[0])
            if not isinstance(info, dict):
                raise ValueError
        except (OSError, ValueError):
            old = time.time() - mark[1] / 1e9 > LOCK_WAIT
            return {'alive': not old, 'mark': mark}
        # この処理が手放したのに消せずに残ったロックは、自分のものとして取る
        mine = mark[0] in _RELEASED and info.get('pid') == os.getpid() and info.get('host') == socket.gethostname()
        return dict(info, alive=False if mine else lock_alive(info), mark=mark)

    def _take_over(self, holder: dict) -> bool:
        """死んだロックを外す。外せたら True。ほかの待ち手が作ったばかりの生きたロックを消さないよう、
        まず一意の名前に移し、移したものが見た死んだロックのままかを確かめてから消す。違えば元に戻す。"""
        aside = self.path.with_name(f'{self.path.name}.stale.{os.getpid()}.{uuid.uuid4().hex[:8]}')
        try:
            os.rename(self.path, aside)
        except FileNotFoundError:
            return True  # もう誰かが外した。作り直しから取り直す
        except OSError:
            return False  # 移せない (読み手が開いている など)。待ってから見直す
        if self._mark(aside) == holder.get('mark'):
            _RELEASED.discard(holder['mark'][0])
            try:
                os.remove(aside)
            except OSError:
                pass
            return True
        # 見たあとで入れ替わった生きたロック: 元の名前に戻す
        try:
            os.rename(aside, self.path)
        except OSError:
            # 3 つの処理が重なると、戻す前に別の待ち手が作ったロックと 2 つが並ぶ窓が残る (受け入れ済み)。
            # 移したロックは戻せないので残し、持ち主は __exit__ で自分の pid を確かめる
            pass
        return False

    def __exit__(self, *exc):
        try:
            text = self.path.read_text(encoding='utf-8')
            info = json.loads(text)
        except (OSError, ValueError):
            return False
        if not (isinstance(info, dict) and info.get('pid') == os.getpid()):
            return False
        # Windows では読み手が開いている間は消せない。短く取り直し、それでも残れば手放した印を覚えておく
        # 読み手が開いていない隙を拾うよう、待ちは短く刻む
        deadline = time.monotonic() + REMOVE_WAIT
        attempt = 0
        while True:
            try:
                os.remove(self.path)
                return False
            except FileNotFoundError:
                return False
            except OSError:
                if time.monotonic() >= deadline:
                    break
                _backoff(attempt, 0.02)
                attempt += 1
        _RELEASED.add(text)
        return False


# この処理が手放したが消せなかったロックの中身 (次の Lock が自分のものとして取る)
_RELEASED = set()


# ---- roles ----

def _valid_str(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _role_part(given, default: dict, name: str, warns: list) -> dict:
    """1 つの役割 (か reviewer の段) の値を確かめ、不正な値は既定にして warn を足す。"""
    if given is None:
        return dict(default)
    if not isinstance(given, dict):
        warns.append(f'warn roles.{name}: not an object; using the default')
        return dict(default)
    part = {}
    for key, value in default.items():
        got = given.get(key, value)
        ok = _valid_str(got) if key != 'effort' else (got is None or _valid_str(got))
        if not ok:
            warns.append(f'warn roles.{name}.{key}: {got!r} is not valid; using {value!r}')
            got = value
        part[key] = got
    return part


def load_roles():
    """home の agents.json の roles。(roles, warn の行)。"""
    warns = []
    data = common.load_json(common.resolve_home(None) / 'agents.json', {})
    given = data.get('roles') if isinstance(data, dict) else None
    if given is not None and not isinstance(given, dict):
        warns.append('warn roles: not an object; using the defaults')
        given = None
    given = given or {}
    reviewer = given.get('reviewer')
    if reviewer is not None and not isinstance(reviewer, dict):
        warns.append('warn roles.reviewer: not an object; using the default')
        reviewer = None
    reviewer = reviewer or {}
    roles = {
        'designer': _role_part(given.get('designer'), DEFAULT_ROLES['designer'], 'designer', warns),
        'reviewer': {stage: _role_part(reviewer.get(stage), DEFAULT_ROLES['reviewer'][stage], f'reviewer.{stage}', warns)
                     for stage in ('deep', 'other')},
        'tester': _role_part(given.get('tester'), DEFAULT_ROLES['tester'], 'tester', warns),
    }
    return roles, warns


def model_label(part: dict) -> str:
    return part['model'] + (f"/{part['effort']}" if part.get('effort') else '')


# ---- 依頼文 ----

def template_block() -> list:
    text = TEMPLATE.read_text(encoding='utf-8')
    match = re.search(r'^```text\n(.*?)^```', text.replace('\r\n', '\n'), re.S | re.M)
    if match is None:
        raise Refused(f'no text block in {TEMPLATE.as_posix()}')
    return match.group(1).rstrip('\n').split('\n')


def render(values: dict) -> str:
    """{?name} の行は値があるときだけ残し、{name} を埋める。"""
    out = []
    for line in template_block():
        optional = re.match(r'^\{\?(\w+)\}', line)
        if optional:
            if not values.get(optional.group(1)):
                continue
            line = line[optional.end():]
        out.append(re.sub(r'\{(\w+)\}', lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0),
                          line))
    return '\n'.join(out) + '\n'


def alive_age(path: Path):
    """.alive の古さ (秒)。無ければ None。status (PBI-014) も使う。"""
    try:
        return max(0, int(time.time() - path.stat().st_mtime))
    except OSError:
        return None


def alive_line(path: Path) -> str:
    age = alive_age(path)
    return 'alive: none' if age is None else f'alive: {age}s ago'


# ---- 状態の行 ----

def episode_line(item: dict) -> str:
    line = f"episode: {item.get('episode')} seq {item.get('seq')}"
    return line + (f" wave {item['wave']}" if item.get('wave') is not None else '')


def question_lines(run: dict) -> list:
    q = run.get('question') or {}
    lines = ['state: question', f"question: {q.get('kind')} {q.get('path')}", f"summary: {fold(q.get('summary'), SUMMARY_MAX)}"]
    if q.get('kind') == 'approve':
        lines.append('then: ' + command('answer', '--reply', 'F'))
        lines.append('approve: ' + command('answer', '--approve', '[--choices 1=a,2=b]'))
        lines.append('revise: ' + command('answer', '--revise', '--text', '"..."'))
    else:
        lines.append('then: ' + command('answer', '--text', '"..."'))
        if q.get('relaunch'):
            lines.append('relaunch: ' + q['relaunch'])
    return lines


def closed_lines(run: dict) -> list:
    lines = [f"state: {run.get('state')}"]
    if run.get('state') == 'finished' and run.get('report'):
        lines.append(f"report: {run['report']}")
    if run.get('state') == 'aborted' and run.get('reason'):
        lines.append(f"reason: {run['reason']}")
    return lines


def no_run(paths: Paths) -> bool:
    """conductor/ が無ければ run.json も無い (ロックのためにディレクトリを作らない)。"""
    return not paths.conductor.is_dir()


def none_exit(lines) -> int:
    emit(lines)
    return EXIT_USAGE


def save(paths: Paths, run: dict) -> None:
    write_json(paths.run, run)
    paths.seen = run.get('state')


# ---- start ----

def aside_name(paths: Paths) -> Path:
    stamp = common.now().strftime('%Y%m%d-%H%M%S')
    target = paths.conductor / f'run.{stamp}.json'
    n = 1
    while target.exists():
        target = paths.conductor / f'run.{stamp}-{n}.json'
        n += 1
    return target


def cmd_start(args, paths: Paths) -> int:
    if (args.goal is None) == (args.goal_file is None):
        raise Refused('give one of --goal or --goal-file')
    if args.goal_file is not None:
        try:
            goal = Path(args.goal_file).read_text(encoding='utf-8-sig')
        except OSError as error:
            raise Refused(f'{args.goal_file}: {error}')
    else:
        goal = args.goal
    warns = []
    carried = {}  # 前の run の止められなかった pid (新しい run の stuck_pids に引き継ぐ)
    with Lock(paths):
        try:
            old = load_run(paths)
        except Unusable as error:
            # 壊れた・形の違う run.json も、--force なら退けて始められる
            if not args.force:
                raise Unusable(f'{error}; start --force moves it aside')
            old = None
        if old is not None and old.get('state') not in CLOSED and not args.force:
            raise Refused(f"a run is in progress (state {old.get('state')}); finish, abort or start --force")
        if paths.run.exists():
            # 前の run の子 (runner と designer) を止めてから退ける。止められなければ warn にして進む
            if old is not None:
                warns, stuck = stop_children(old)
                if stuck:
                    carried['stuck_pids'] = [entry for entry in old.get('stuck_pids') or []
                                             if isinstance(entry, dict)]
                    move_stuck(carried, old.get('running') or {})
                    move_stuck(carried, old)
                    if carried['stuck_pids']:
                        warns.append(stuck_line(entry['pid'] for entry in carried['stuck_pids']))
            # 前の run は消さずに退ける (閉じた run も履歴として残す)
            try:
                replace_retry(paths.run, aside_name(paths))
            except OSError as error:
                raise Refused(f'cannot move run.json aside: {error}')
        paths.goal.parent.mkdir(parents=True, exist_ok=True)
        paths.goal.write_text(goal if goal.endswith('\n') else goal + '\n', encoding='utf-8', newline='\n')
        run = {
            'schema': SCHEMA, 'run_id': str(uuid.uuid4()), 'created_at': iso_now(),
            'goal': paths.goal.as_posix(), 'workflow': args.workflow, 'launch': args.launch,
            'state': 'ready', 'seq': 0,
            'pending': {'episode': 'design', 'wave': None, 'pbis': None, 'continue_from': None, 'answer': None},
            'running': None, 'question': None, 'history': [],
        }
        if carried.get('stuck_pids'):
            run['stuck_pids'] = carried['stuck_pids']
        save(paths, run)
    emit(['state: ready', *warns, f"run: {run['run_id'][:8]}", 'next: design', 'then: ' + command('next')])
    return EXIT_OK


# ---- next ----

def agents_installed() -> bool:
    """設計者の定義が在るか。<CLAUDE_CONFIG_DIR か ~/.claude>/agents と作業ルートの .claude/agents のどちらか。"""
    home = Path(os.environ.get('CLAUDE_CONFIG_DIR') or (Path.home() / '.claude'))
    return any(path.is_file() for path in (home / 'agents' / AGENT_FILE, Path.cwd() / '.claude' / 'agents' / AGENT_FILE))


def launch(paths: Paths, run: dict, launcher=None) -> list:
    """ready・retry の run の次の出番の依頼文を書き、running にする。出す行を返す。

    定義が無ければ warn を足し、agent では general-purpose で起こして依頼文の先頭に定義を読ませる行を付ける
    (headless の後ろ盾は run が --agents で足す)。launcher を渡すと、run.json の launch によらずその起動のしかたで
    依頼文と running を書く (run は常に headless として自分で起こす)。
    """
    pending = run.get('pending') or {}
    episode = pending.get('episode') or 'design'
    seq = int(run.get('seq') or 0) + 1
    run_short = str(run.get('run_id', ''))[:8]
    launcher = launcher or run.get('launch') or 'agent'
    prompt = paths.episode_file(seq, episode, '.prompt.md')
    result = paths.result_file(seq, episode, run_short, launcher)
    alive = paths.episode_file(seq, episode, '.alive')
    # 前の run の同じ名前の結果を読まないように消す
    for stale in (result, alive):
        try:
            stale.unlink()
        except OSError:
            pass
    roles, warns = load_roles()
    session_id = str(uuid.uuid4())
    description = f'ATELIER.{run_short}.{seq} {episode}'
    if launcher == 'headless':
        ctx_probe = f"python {quoted(SKILL / 'agent_stats.py')} --ctx --session {session_id}"
    else:
        ctx_probe = f"python {quoted(SKILL / 'agent_stats.py')} --ctx --agent \"ATELIER.{run_short}.{seq} \""
    reviewer = roles['reviewer']
    values = {
        'work_root': Path.cwd().resolve().as_posix(), 'skill': SKILL.as_posix(), 'episode': episode, 'seq': seq,
        'wave': pending.get('wave'), 'pbis': ','.join(pending.get('pbis') or []),
        'goal': paths.goal.as_posix(), 'workflow': run.get('workflow') or 'default',
        'reviewer': (f"deep={model_label(reviewer['deep'])} other={model_label(reviewer['other'])}"
                     ' (switch to the other when it equals the built model)'),
        'tester': model_label(roles['tester']),
        'continue_from': pending.get('continue_from'), 'answer': pending.get('answer'),
        'conductor_answer': pending.get('conductor_answer'), 'sent_by': pending.get('sent_by'),
        'result': result.as_posix(), 'alive': alive.as_posix(), 'ctx_probe': ctx_probe,
    }
    installed = agents_installed()
    fallback = not installed and launcher != 'headless'
    text = render(values)
    if fallback:
        text = f'Read {(SKILL / "agents" / AGENT_FILE).as_posix()} and follow it as your instructions.\n' + text
    if not installed:
        warns = [*warns, 'warn agents: atelier-designer not installed; using fallback']
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text(text, encoding='utf-8', newline='\n')
    run['seq'] = seq
    run['running'] = {
        'seq': seq, 'episode': episode, 'wave': pending.get('wave'), 'pbis': pending.get('pbis'),
        'launched_at': iso_now(), 'launcher': launcher, 'session_id': session_id,
        'continue_from': pending.get('continue_from'), 'answer': pending.get('answer'),
        'conductor_answer': pending.get('conductor_answer'), 'sent_by': pending.get('sent_by'),
        'prompt': prompt.as_posix(), 'result': result.as_posix(), 'alive': alive.as_posix(),
    }
    run['pending'] = None
    run['state'] = 'running'
    model = roles['designer']['model']
    return ['state: running', *warns, f'episode: {episode} seq {seq}',
            f'launch: {launcher} subagent_type={"general-purpose" if fallback else "atelier-designer"} '
            f'model={model} description="{description}"',
            f'prompt: {prompt.as_posix()}', f'result: {result.as_posix()}', 'then: ' + command('done')]


def running_lines(run: dict) -> list:
    running = run.get('running') or {}
    return [f"state: running since {running.get('launched_at')}", episode_line(running),
            alive_line(Path(running.get('alive') or '')) if running.get('alive') else 'alive: none',
            'relaunch: ' + command('next', '--relaunch')]


def headless_lines(run: dict, warns=()) -> list:
    """launch が headless の run の next: 起動の指示は出さず、起動する run を案内する。"""
    return [f"state: {run.get('state')}", *warns, 'launch: headless', 'then: ' + command('run', '--detach')]


def relaunch(paths: Paths, run: dict):
    """running の子を止め、同じ出番・波・Continue from・Answer で新しい seq と session を起こす。(出す行, 終了コード) を返す。

    headless の run で自分の子を止められなければ running のまま error (終了コード 2): 生きた runner の隣で run --detach を起こさせない。
    """
    running = run.get('running') or {}
    warns, stuck = stop_children(run)
    if stuck and run.get('launch') == 'headless':
        return [f"state: {run.get('state')}", *warns, 'error: the running child could not be stopped; not relaunched'], EXIT_USAGE
    history_add(run, running, 'relaunched', None)
    run['pending'] = {'episode': running.get('episode'), 'wave': running.get('wave'), 'pbis': running.get('pbis'),
                      'continue_from': running.get('continue_from'), 'answer': running.get('answer'),
                      'conductor_answer': running.get('conductor_answer'), 'sent_by': running.get('sent_by')}
    run['running'] = None
    if run.get('launch') == 'headless':
        run['state'] = 'ready'
        return headless_lines(run, warns), EXIT_OK
    lines = launch(paths, run)
    lines[1:1] = warns
    return lines, EXIT_OK


def relaunch_question(run: dict, launcher) -> bool:
    """問いの relaunch が案内した起動 (run --launch <launcher> --detach) と同じ切り替えで run が呼ばれたら、
    答え無しの再開として問いを閉じ ready に戻す。閉じたら True。ほかの問いは閉じない。"""
    q = run.get('question') or {}
    if not launcher or f'--launch {launcher}' not in str(q.get('relaunch') or ''):
        return False
    run['pending'] = {'episode': q.get('episode'), 'wave': q.get('wave'), 'pbis': q.get('pbis'),
                      'continue_from': q.get('state'), 'answer': None,
                      'sent_by': q.get('sent_by')}
    run['question'] = None
    run['failures'] = None
    if run.get('handoff'):
        run['handoff']['count'] = 0
    run['state'] = 'ready'
    return True


def cmd_next(args, paths: Paths) -> int:
    none = ['state: none', 'then: ' + command('start', '--goal', '"..."')]
    if no_run(paths):
        return none_exit(none)
    with Lock(paths):
        run = load_run(paths)
        if run is None:
            return none_exit(none)
        state = run.get('state')
        if args.relaunch and state != 'running':
            raise Refused(f'--relaunch needs a running episode (state {state})')
        if state in CLOSED:
            emit(closed_lines(run))
            return EXIT_OK
        if state == 'question':
            emit(question_lines(run))
            return EXIT_ASK
        if state == 'running':
            if not args.relaunch:
                emit(running_lines(run))
                return EXIT_OK
            lines, code = relaunch(paths, run)
            save(paths, run)
            emit(lines)
            return code
        if state not in ('ready', 'retry'):
            raise Refused(f'unknown state {state!r} in run.json')
        if run.get('launch') == 'headless':
            emit(headless_lines(run))
            return EXIT_OK
        lines = launch(paths, run)
        save(paths, run)
    emit(lines)
    return EXIT_OK


# ---- done ----

def history_add(run: dict, running: dict, status: str, summary) -> None:
    run.setdefault('history', []).append({
        'seq': running.get('seq'), 'episode': running.get('episode'), 'wave': running.get('wave'),
        'launcher': running.get('launcher'), 'status': status,
        'summary': summary if isinstance(summary, list) else []})


def bump_failures(run: dict, running: dict) -> int:
    """同じ出番 (出番と波) で続けた失敗の数を 1 足して返す。出番が変われば 1 から数え直す。"""
    given = run.get('failures') or {}
    same = given.get('episode') == running.get('episode') and given.get('wave') == running.get('wave')
    count = int(given.get('count') or 0) + 1 if same else 1
    run['failures'] = {'episode': running.get('episode'), 'wave': running.get('wave'), 'count': count}
    return count


def last_handoff_state(run: dict, running: dict):
    """同じ出番・波の、前の handoff の state (Continue from に使う)。無ければ None。"""
    given = run.get('handoff') or {}
    if given.get('episode') == running.get('episode') and given.get('wave') == running.get('wave'):
        return given.get('state')
    return None


def conductor_question(paths: Paths, run: dict, running: dict, kind: str, what: str, relaunch=None) -> tuple:
    """進行役が作る問い (handoff-loop・launch-failure・decision)。説明を episodes/<seq>-<種類>.question.md に書く。
    (終了コード, 出す行) を返す (出すのは呼び出し側)。"""
    path = paths.episode_file(running.get('seq'), kind, '.question.md')
    lines = [f'# Conductor question: {kind}', '', what, '', 'Next:',
             '- ' + command('answer', '--text', '"..."') + '  (the same episode starts again with Answer:)']
    if relaunch:
        lines.append(f'- {relaunch}  (run the episodes headless)')
    lines.append('- ' + command('abort') + '  (close the run)')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8', newline='\n')
    run['question'] = {'kind': kind, 'path': path.as_posix(), 'summary': what, 'check': None,
                       'state': last_handoff_state(run, running), 'seq': running.get('seq'),
                       'episode': running.get('episode'), 'wave': running.get('wave'), 'pbis': running.get('pbis'),
                       'relaunch': relaunch, 'sent_by': running.get('sent_by')}
    run['state'] = 'question'
    run['running'] = None
    run['pending'] = None
    save(paths, run)
    return EXIT_ASK, question_lines(run)


def fail(paths: Paths, run: dict, running: dict, reason: str, status: str, summary=None) -> tuple:
    """出番の失敗: 数えて retry にし、同じ出番を Continue from 無しで起こせるようにする。
    同じ出番で FAIL_LIMIT 回続いたら launch-failure の問いにする。(終了コード, 出す行) を返す。"""
    history_add(run, running, status, summary)
    run['last_error'] = reason
    count = bump_failures(run, running)
    if count >= FAIL_LIMIT:
        return conductor_question(paths, run, running, 'launch-failure',
                                  f"{running.get('episode')} seq {running.get('seq')} failed {count} times in a row: "
                                  f'{fold(reason, 200)}')
    run['state'] = 'retry'
    run['running'] = None
    run['pending'] = {'episode': running.get('episode'), 'wave': running.get('wave'), 'pbis': running.get('pbis'),
                      'continue_from': None, 'answer': running.get('answer'),
                      'conductor_answer': running.get('conductor_answer'), 'sent_by': running.get('sent_by')}
    save(paths, run)
    return EXIT_FAIL, ['state: retry', episode_line(running), f'error: {fold(reason, 300)}',
                       'then: ' + command('next')]


def handoff_count(paths: Paths, run: dict, running: dict, state_path: str) -> int:
    """handoff の進みを測る: runs.jsonl の行の数と STATUS.md の sha256 を前の handoff と比べ、
    どちらも変わらなかった回数を返す (変われば 0)。記録は run.json の handoff に残す。"""
    try:
        text = (paths.base / 'runs.jsonl').read_text(encoding='utf-8', errors='replace')
        rows = len([line for line in text.splitlines() if line.strip()])
    except OSError:
        rows = 0
    try:
        sha = hashlib.sha256((paths.base / 'STATUS.md').read_bytes()).hexdigest()
    except OSError:
        sha = None
    given = run.get('handoff') or {}
    same = given.get('episode') == running.get('episode') and given.get('wave') == running.get('wave')
    count = int(given.get('count') or 0) + 1 if same and given.get('rows') == rows and given.get('sha') == sha else 0
    run['handoff'] = {'episode': running.get('episode'), 'wave': running.get('wave'), 'rows': rows, 'sha': sha,
                      'state': state_path, 'count': count}
    return count


def ready_lines(run: dict) -> list:
    pending = run.get('pending') or {}
    nxt = f"next: {pending.get('episode')}"
    if pending.get('wave') is not None:
        nxt += f" wave {pending['wave']}"
    if pending.get('pbis'):
        nxt += f" pbis {','.join(pending['pbis'])}"
    return ['state: ready', nxt, 'then: ' + command('next')]


def process_done(paths: Paths, run: dict) -> tuple:
    """running の出番の結果を読み、状態を進める (ロックは呼び出し側が持つ)。(終了コード, 出す行) を返す。"""
    if run.get('state') != 'running':
        raise Refused(f"no running episode (state {run.get('state')})")
    running = run['running']
    data = common.load_json(running.get('result') or '')
    if not isinstance(data, dict):
        return fail(paths, run, running, f"result missing or broken: {running.get('result')}", 'missing')
    errors = role_results.check_episode(data)
    if not errors and (data.get('seq') != running.get('seq') or data.get('episode') != running.get('episode')):
        errors.append(f"seq/episode {data.get('seq')} {data.get('episode')} != running "
                      f"{running.get('seq')} {running.get('episode')}")
    summary = data.get('summary')
    if errors:
        return fail(paths, run, running, '; '.join(errors), 'invalid', summary)
    status = data['status']
    episode = running['episode']
    if status == 'error':
        if data.get('reason') == 'no-agent-tool':
            # 数えない。headless で回すかをユーザーに聞く
            history_add(run, running, status, summary)
            return conductor_question(
                paths, run, running, 'decision',
                f"{episode} seq {running.get('seq')}: the designer has no Agent tool (no-agent-tool). "
                'Run the episodes headless instead.', relaunch=command('run', '--launch', 'headless', '--detach'))
        return fail(paths, run, running, f"episode error: {data.get('reason')}", 'error', summary)
    if status == 'question':
        run['failures'] = None
        q = data['question']
        history_add(run, running, status, summary)
        run['question'] = {'kind': q['kind'], 'path': q['path'], 'summary': q['summary'], 'check': q.get('check'),
                           'state': data.get('state'), 'seq': running['seq'], 'episode': episode,
                           'wave': running.get('wave'), 'pbis': running.get('pbis'),
                           'sent_by': running.get('sent_by')}
        run['state'] = 'question'
        run['running'] = None
        save(paths, run)
        return EXIT_ASK, question_lines(run)
    if status == 'handoff':
        state_path = data['state']
        if not Path(state_path).is_file():
            return fail(paths, run, running, f'handoff state not found: {state_path}', 'invalid', summary)
        history_add(run, running, status, summary)
        run['failures'] = None
        if handoff_count(paths, run, running, state_path) >= LOOP_LIMIT:
            return conductor_question(
                paths, run, running, 'handoff-loop',
                f"{episode} seq {running.get('seq')}: {LOOP_LIMIT} handoffs in a row without progress "
                '(runs.jsonl and STATUS.md did not change).')
        run['pending'] = {'episode': episode, 'wave': running.get('wave'), 'pbis': running.get('pbis'),
                          'continue_from': state_path, 'answer': None,
                          'conductor_answer': None, 'sent_by': running.get('sent_by')}
    elif episode == 'finish':
        history_add(run, running, status, summary)
        run['failures'] = None
        run['handoff'] = None
        run['state'] = 'finished'
        run['running'] = None
        run['report'] = data.get('report')
        run['finished_at'] = iso_now()
        save(paths, run)
        return EXIT_OK, closed_lines(run)
    else:
        nxt = data['next']
        target = nxt['episode']
        if target not in NEXT_ALLOWED.get(episode, ()):
            return fail(paths, run, running, f'{episode} -> {target} is not allowed', 'invalid', summary)
        # 差し戻しの build は、送った出番 (test か review) にしか返せない (finish も通さない)
        if episode == 'build' and running.get('sent_by') and target != running.get('sent_by'):
            return fail(paths, run, running,
                        f"build sent by {running.get('sent_by')} -> {target} is not allowed "
                        f"(end with next {running.get('sent_by')})", 'invalid', summary)
        wave = nxt.get('wave')
        if wave is None:
            wave = running.get('wave')  # 無い・null は今の波を受け継ぐ
        elif isinstance(wave, bool) or not isinstance(wave, int):
            return fail(paths, run, running, f'next.wave {wave!r} is not an integer', 'invalid', summary)
        if target == 'build' and role_results.wave_pbis(paths.base, wave) is None:
            return fail(paths, run, running, f"wave {wave} is not in {(paths.base / 'waves.json').as_posix()}",
                        'invalid', summary)
        history_add(run, running, status, summary)
        run['failures'] = None
        run['handoff'] = None
        # 差し戻し (pbis 付きの build) は、送った出番を残して依頼文と done の照合に使う
        sent_by = episode if target == 'build' and nxt.get('pbis') and episode in ('test', 'review') else None
        run['pending'] = {'episode': target, 'wave': wave, 'pbis': nxt.get('pbis') or None,
                          'continue_from': None, 'answer': None, 'conductor_answer': None, 'sent_by': sent_by}
    run['state'] = 'ready'
    run['running'] = None
    save(paths, run)
    return EXIT_OK, ready_lines(run)


def cmd_done(args, paths: Paths) -> int:
    if no_run(paths):
        return none_exit(['state: none'])
    with Lock(paths):
        run = load_run(paths)
        if run is None:
            return none_exit(['state: none'])
        if headless_busy(run):
            # headless の出番は runner の settle_episode だけが片付ける。設計者が動いている間は片付けない
            emit(['state: running', episode_line(run.get('running') or {}),
                  'error: the headless designer is still running; its runner settles the episode',
                  'then: ' + command('wait', '--timeout', '300')])
            return EXIT_USAGE
        code, lines = process_done(paths, run)
    emit(lines)
    return code


def headless_busy(run: dict) -> bool:
    """headless の出番の設計者か runner が、まだ動いている (生きていて自分の子) か。"""
    running = run.get('running') or {}
    if run.get('state') != 'running' or running.get('launcher') != 'headless':
        return False
    launched_at = running.get('launched_at') or run.get('created_at')
    for holder, key, pid in pid_slots(run):
        if key not in PID_KEYS:
            continue  # stuck_pids は前の出番の残り
        if pid != os.getpid() and pid_alive(pid) and pid_is_ours(holder, key, pid, launched_at):
            return True
    return False


# ---- answer ----

def parse_choices(text):
    choices = {}
    if not text:
        return choices
    for item in text.split(','):
        number, sep, value = item.partition('=')
        if not sep or not number.strip().isdigit() or not value.strip():
            raise Refused(f'--choices: {item!r} is not N=choice')
        choices[int(number.strip())] = value.strip()
    return choices


def approve_reply(draft_path: str, choices: dict) -> str:
    """問いの下書きの reply-meta- 行と choices から、check-reply が読む形の返信を組む (末尾に am-reply-meta の囲み)。"""
    try:
        draft = Path(draft_path).read_bytes().decode('utf-8-sig', errors='replace')
    except OSError as error:
        raise Refused(f'{draft_path}: {error}')
    lines = draft.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    meta = {}
    for line in lines:
        if line.startswith(META_PREFIX) and ':' in line:
            name, _, value = line.partition(':')
            meta[name[len(META_PREFIX):].strip()] = value.strip()
    body = []
    numbers = set()
    for index, line in enumerate(lines):
        match = DECISION.match(line)
        if not match:
            continue
        number = int(match.group(1))
        numbers.add(number)
        given = ANSWER_LINE.match(lines[index + 1]) if index + 1 < len(lines) else None
        old = given.group(1).split(' _(')[0].strip() if given else ''
        if number in choices:
            picked = f'**{choices[number]}**'
        elif old and old != UNANSWERED:
            picked = old
        else:
            picked = UNANSWERED
        body += [line, f'   → {picked}', '']
    unknown = sorted(set(choices) - numbers)
    if unknown:
        raise Refused(f"--choices: no decision {', '.join(map(str, unknown))} in {draft_path}")
    text = '\n'.join(body)
    if meta:
        text += f'{FENCE_OPEN}\n{json.dumps(meta, ensure_ascii=False)}\n```\n'
    return text


def check_reply(paths: Paths, answer: Path, draft: str):
    proc = subprocess.run([sys.executable, str(SKILL / 'design_record.py'), 'check-reply', '--reply', str(answer),
                           '--draft', draft, '--dir', str(paths.base)],
                          capture_output=True, text=True, encoding='utf-8', errors='replace',
                          creationflags=NO_WINDOW)
    out = (proc.stdout + proc.stderr).strip().splitlines()
    return proc.returncode, out[-1] if out else ''


def approvals_size(paths: Paths) -> int:
    try:
        return (paths.base / 'design-approvals.jsonl').stat().st_size
    except OSError:
        return 0


def checked_reply(paths: Paths, run: dict, q: dict, answer: Path):
    """check-reply を呼ぶ。0 なら design-approvals.jsonl に 1 行足される。

    呼ぶ前に、答えの中身のハッシュと approvals の大きさを run.json に残す。前の answer が check-reply の
    あとの保存で失敗していれば (同じ答えで、approvals がそのときより伸びている)、呼び直さずに通ったとみなす
    (同じ行を 2 度足さない)。
    """
    digest = hashlib.sha256(answer.read_bytes()).hexdigest()
    mark = q.get('checking') or {}
    if mark.get('sha256') == digest and approvals_size(paths) > int(mark.get('approvals_size') or 0):
        return 0, 'already recorded in design-approvals.jsonl'
    q['checking'] = {'sha256': digest, 'approvals_size': approvals_size(paths)}
    save(paths, run)
    return check_reply(paths, answer, q['path'])


def cmd_answer(args, paths: Paths) -> int:
    given = [x is not None for x in (args.reply, args.text)]
    if args.approve:
        if any(given) or args.revise:
            raise Refused('--approve takes no --reply, --text or --revise')
    elif sum(given) != 1:
        raise Refused('give one of --reply or --text (or --approve)')
    if args.choices is not None and not args.approve:
        raise Refused('--choices goes with --approve')
    choices = parse_choices(args.choices)
    if no_run(paths):
        return none_exit(['state: none'])
    with Lock(paths):
        run = load_run(paths)
        if run is None:
            return none_exit(['state: none'])
        q = run.get('question')
        if run.get('state') != 'question' or not q:
            raise Refused(f"no question to answer (state {run.get('state')})")
        kind = q['kind']
        if (args.approve or args.revise) and kind != 'approve':
            raise Refused(f'--approve and --revise answer only an approve question (this one is {kind})')
        answer = paths.answers / f"{q['seq']}-{kind}.md"
        answer.parent.mkdir(parents=True, exist_ok=True)
        if args.approve:
            answer.write_text(approve_reply(q['path'], choices), encoding='utf-8', newline='\n')
        elif args.reply is not None:
            try:
                shutil.copyfile(args.reply, answer)
            except OSError as error:
                raise Refused(f'{args.reply}: {error}')
        else:
            text = args.text
            answer.write_text(text if text.endswith('\n') else text + '\n', encoding='utf-8', newline='\n')
        if kind == 'approve' and q.get('check') == 'design-record' and not args.revise:
            code, last = checked_reply(paths, run, q, answer)
            if code in (3, 4, 5):
                emit(['state: question', f'check-reply exit {code}', f'detail: {last[:300]}',
                      'approve: ' + command('answer', '--approve', '[--choices 1=a,2=b]'),
                      'revise: ' + command('answer', '--revise', '--text', '"..."')])
                return EXIT_ASK
            if code != 0:
                raise Refused(f'check-reply exit {code}: {last[:300]}')
        episode = 'design' if args.revise else q['episode']
        continue_from = q.get('state') if kind != 'approve' else None
        # 前の出番の state が無い conductor の問いの答えは、承認の答えと取り違えないよう別の行の名前で渡す
        conductor_answer = answer.as_posix() if continue_from is None and kind != 'approve' else None
        run['pending'] = {'episode': episode, 'wave': q.get('wave'), 'pbis': q.get('pbis'),
                          'continue_from': continue_from,
                          'answer': None if conductor_answer else answer.as_posix(),
                          'conductor_answer': conductor_answer, 'sent_by': q.get('sent_by')}
        run.setdefault('answers', []).append({'seq': q['seq'], 'kind': kind, 'path': answer.as_posix(),
                                              'revise': bool(args.revise), 'at': iso_now()})
        run['question'] = None
        run['failures'] = None  # どの答えのあとも数を 0 に戻す
        if run.get('handoff'):
            run['handoff']['count'] = 0
        run['state'] = 'ready'
        save(paths, run)
    emit([*ready_lines(run)[:2], f'answer: {answer.as_posix()}', 'then: ' + command('next')])
    return EXIT_OK


# ---- status・abort ----

def cost_line(run: dict) -> str:
    """history の cost_usd の合計。1 つも無ければ unmeasured (agent で起こした出番は測れない)。"""
    costs = [row['cost_usd'] for row in run.get('history') or []
             if isinstance(row, dict) and isinstance(row.get('cost_usd'), (int, float))
             and not isinstance(row.get('cost_usd'), bool)]
    return f'cost: ${sum(costs):.2f}' if costs else 'cost: unmeasured'


def stale_running(run: dict) -> bool:
    """state が running なのに、runner_pid も child_pid も生きていない (pid_alive が False) か。"""
    slots = pid_slots(run)  # pid が 1 つも無い (agent 起動) 出番は生きているものとして扱う
    return run.get('state') == 'running' and bool(slots) and not any(pid_alive(pid) for _holder, _key, pid in slots)


def status_lines(run: dict) -> list:
    """status と wait が同じ形で出す行 (1 行目は state、全部で 8 行以内)。"""
    state = run.get('state')
    lines = [f'state: {state}']
    if state == 'running':
        running = run.get('running') or {}
        lines += [episode_line(running), f"since: {running.get('launched_at')}",
                  alive_line(Path(running['alive'])) if running.get('alive') else 'alive: none']
        if stale_running(run):
            lines += ['stale: no live runner', 'relaunch: ' + command('next', '--relaunch')]
    elif state == 'question':
        q = run.get('question') or {}
        lines += [episode_line(q), f"question: {q.get('kind')} {q.get('path')}"]
    elif state in ('ready', 'retry'):
        pending = run.get('pending') or {}
        lines.append(episode_line(dict(pending, seq=int(run.get('seq') or 0) + 1)))
    else:
        lines += closed_lines(run)[1:]
    lines += [f"episodes: {int(run.get('seq') or 0)}", cost_line(run)]
    return lines


def status_none_lines(paths: Paths) -> list:
    """run.json が無いときの status の行 (v5 の引き継ぎが在れば 1 行足す)。"""
    lines = ['state: none']
    if (paths.base / 'HANDOFF.md').is_file():
        lines.append('inline: v5 run (follow designer.md)')
    return lines


def cmd_status(args, paths: Paths) -> int:
    # ロックを取らない。書き手の置き換えの間の読めなさは load_run が短く取り直す
    run = None if no_run(paths) else load_run(paths)
    if run is None:
        emit(status_none_lines(paths))
        return EXIT_OK
    emit(status_lines(run))
    return EXIT_OK


def cmd_abort(args, paths: Paths) -> int:
    if no_run(paths):
        return none_exit(['state: none'])
    with Lock(paths):
        run = load_run(paths)
        if run is None:
            return none_exit(['state: none'])
        if run.get('state') in CLOSED:
            if not run.get('stuck_pids'):
                raise Refused(f"the run is already {run.get('state')}")
            # 閉じた run に残った止められなかった pid を、もう一度止めにいく
            warns, stuck = stop_children(run)
            if stuck:
                warns.append(stuck_line(entry['pid'] for entry in run.get('stuck_pids') or []))
            save(paths, run)
            emit([*closed_lines(run), *warns])
            return EXIT_OK
        warns, stuck = stop_children(run)
        if stuck:
            # 止められなかった子は、running を None にしても stuck_pids に残す (次の abort・start --force が止める)
            move_stuck(run, run.get('running') or {})
            move_stuck(run, run)
            warns.append(stuck_line(entry['pid'] for entry in run.get('stuck_pids') or []))
        if run.get('running'):
            history_add(run, run['running'], 'aborted', None)
        run['state'] = 'aborted'
        run['running'] = None
        run['reason'] = args.reason or 'aborted'
        run['aborted_at'] = iso_now()
        save(paths, run)
    emit([*closed_lines(run), *warns])
    return EXIT_OK


# ---- run・wait ----

def merge_warnings(old, new) -> list:
    """warn の行を足す (同じ行は 1 度だけ)。"""
    merged = list(old)
    for line in new:
        if line not in merged:
            merged.append(line)
    return merged


def warnings_in(lines) -> list:
    return [line for line in lines if str(line).startswith('warn ')]


def with_warnings(lines, warns) -> list:
    """出す行の 1 行目 (state) のあとに warn を置く。state は 1 行目から動かさない。"""
    lines = list(lines)
    extra = [line for line in warns if line not in lines]
    return [lines[0], *extra, *lines[1:]] if lines else extra


def running_resumable(running: dict) -> bool:
    """run --detach の親が書いた、まだ起こしていない headless の出番か (running の案内で止まらない)。"""
    return (running.get('launcher') == 'headless' and not running.get('child_pid')
            and bool(running.get('prompt')))


def designer_command():
    """設計者を起こすコマンドの先頭と、見つからないときの理由 (見つかれば理由は None)。

    ATELIER_DESIGNER_CMD (JSON の文字列の配列) があればそれ。無ければ claude_run.claude_command()。
    ただし claude_command は見つからないと終了するので、先に場所を確かめて、無ければ理由を返す。
    """
    given = os.environ.get('ATELIER_DESIGNER_CMD')
    if given:
        try:
            words = json.loads(given)
        except ValueError:
            words = None
        if isinstance(words, list) and words and all(isinstance(word, str) and word.strip() for word in words):
            return list(words), None
        return None, 'ATELIER_DESIGNER_CMD is not a JSON array of strings'
    configured = os.environ.get('PBI_CLAUDE_BIN')
    found = configured or shutil.which('claude')
    if not found or not Path(found).is_file():
        return None, 'claude not found (install Claude Code and put it on PATH, or set PBI_CLAUDE_BIN)'
    try:
        return claude_run.claude_command(), None
    except SystemExit as error:
        return None, f'claude not found (claude_command exits {error.code})'


def agent_definition_json() -> dict:
    """スキルの agents/atelier-designer.md から、--agents に渡す JSON を組む (frontmatter と本文)。"""
    text = AGENT_DEFINITION.read_text(encoding='utf-8').replace('\r\n', '\n')
    meta = {}
    body = text
    match = re.match(r'^---\n(.*?)\n---\n?(.*)$', text, re.S)
    if match:
        body = match.group(2)
        for line in match.group(1).split('\n'):
            key, sep, value = line.partition(':')
            if sep and key.strip():
                meta[key.strip()] = value.strip()
    spec = {'description': meta.get('description', ''), 'prompt': body.strip(),
            'tools': [tool.strip() for tool in meta.get('tools', '').split(',') if tool.strip()]}
    if meta.get('model'):
        spec['model'] = meta['model']
    return {AGENT_ROLE: spec}


def designer_argv(launcher: list, running: dict, designer: dict) -> list:
    """claude -p の引数。定義が home に無ければ --agents を --agent の前に足す。"""
    argv = [*launcher, '-p', '--output-format', 'json']
    if not agents_installed():
        argv += ['--agents', json.dumps(agent_definition_json())]
    argv += ['--agent', AGENT_ROLE, '--model', designer['model']]
    if designer.get('effort'):
        argv += ['--effort', designer['effort']]
    argv += ['--permission-mode', designer['permission_mode'], '--permission-prompts', 'none',
             '--session-id', str(running.get('session_id') or uuid.uuid4())]
    return argv


def run_input(prompt_path) -> bytes:
    """標準入力に渡す文: 依頼文のパスを読む指示と、依頼文の本文。"""
    text = Path(prompt_path).read_text(encoding='utf-8', errors='replace')
    head = f'{Path(prompt_path).as_posix()} を読み、その指示どおりに作業してください。\n\n'
    return (head + text).encode('utf-8')


def output_json(path):
    """claude -p --output-format json の出力。読めなければ None (全体、だめなら最後の行)。"""
    try:
        text = Path(path).read_text(encoding='utf-8', errors='replace').strip()
    except OSError:
        return None
    for candidate in (text, text.splitlines()[-1] if text else ''):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def launch_problem(code, data, out_json) -> str:
    """起動の失敗の理由。失敗でなければ空。is_error は subtype によらず失敗 (課金の失敗が success で返る)。"""
    if isinstance(data, dict) and data.get('is_error'):
        return f"designer is_error: {fold(data.get('result') or data.get('subtype') or 'is_error', 200)}"
    if code != 0:
        return f'designer exit {code}'
    if not isinstance(data, dict):
        return f'designer output is not JSON: {Path(out_json).as_posix()}'
    return ''


def attach_cost(run: dict, running: dict, data) -> bool:
    """history のその seq の行に session_id と cost_usd を足す。足したら True。"""
    if not isinstance(data, dict):
        return False
    session = data.get('session_id')
    cost = data.get('total_cost_usd')
    changed = False
    for row in reversed(run.get('history') or []):
        if not isinstance(row, dict) or row.get('seq') != running.get('seq'):
            continue
        if isinstance(session, str) and session:
            row['session_id'] = session
            changed = True
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            row['cost_usd'] = cost
            changed = True
        break
    return changed


def runner_argv(args, run: dict) -> list:
    """run --detach が切り離して起こす、--detach を除いた同じ run。親が running にした run_id と seq を渡す。"""
    argv = [sys.executable, str(SKILL / 'conductor.py'), 'run']
    if args.max_episodes is not None:
        argv += ['--max-episodes', str(args.max_episodes)]
    if args.launch:
        argv += ['--launch', args.launch]
    argv += ['--run-id', str(run.get('run_id')), '--seq', str(int((run.get('running') or {}).get('seq') or 0))]
    argv += ['--dir', str(args.dir)]
    return argv


def spawn_detached(argv, log):
    """自分を切り離して起こす (Windows は新しいプロセスグループ、POSIX は新しいセッション)。起こした Popen を返す。"""
    extra = {}
    if os.name == 'nt':
        extra['creationflags'] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        extra['start_new_session'] = True
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=str(Path.cwd()), **extra)


def record_runner(paths: Paths, run_id, seq: int, pid: int) -> None:
    """run --detach の親が、起こした runner の pid を runner_pid に記録する (start --force・abort が止められるように)。

    親が running にした出番のままで、runner_pid がまだ親の pid のときだけ書く (runner が先に自分を書いていれば書かない)。
    """
    try:
        with Lock(paths):
            run = load_run(paths)
            if (run is None or run.get('run_id') != run_id or run.get('state') != 'running'
                    or (run.get('running') or {}).get('seq') != seq or run.get('runner_pid') != os.getpid()):
                return
            record_pid(run, 'runner_pid', pid)
            save(paths, run)
    except (Refused, OSError):
        pass


def clear_runner_pid(paths: Paths) -> None:
    """run --detach の子が終わるときに runner_pid を消す (自分の pid のときだけ)。"""
    if no_run(paths):
        return
    try:
        with Lock(paths):
            run = load_run(paths)
            if run is None or run.get('runner_pid') != os.getpid():
                return
            run.pop('runner_pid', None)
            run.pop('runner_pid_started', None)
            save(paths, run)
    except (Refused, OSError):
        pass


def state_exit(state) -> int:
    """wait の終了コード: question 10、retry 12、ほかは 0。"""
    if state == 'question':
        return EXIT_ASK
    if state == 'retry':
        return EXIT_FAIL
    return EXIT_OK


def wait_lock_gone(paths: Paths) -> None:
    """状態を書いた側がロックを消すのを待つ (返った直後の answer がロックに当たらないように)。"""
    deadline = time.monotonic() + WAIT_LOCK_WAIT
    while paths.lock.exists() and time.monotonic() < deadline:
        time.sleep(0.05)


def cmd_run(args, paths: Paths) -> int:
    if args.max_episodes is not None and args.max_episodes < 1:
        raise Refused('--max-episodes is 1 or more')
    if no_run(paths):
        return none_exit(['state: none', 'then: ' + command('start', '--goal', '"..."')])
    if args.detach:
        return cmd_run_detach(args, paths)
    return cmd_run_loop(args, paths)


def cmd_run_detach(args, paths: Paths) -> int:
    """依頼文を書いて running にし、runner_pid を残してロックを放ち、切り離した run を起こしてすぐ返る。"""
    warns = []
    runner = None
    log = None
    with Lock(paths):
        run = load_run(paths)
        if run is None:
            return none_exit(['state: none', 'then: ' + command('start', '--goal', '"..."')])
        if args.launch and run.get('launch') != args.launch:
            run['launch'] = args.launch
            save(paths, run)
        state = run.get('state')
        if state in CLOSED:
            emit(closed_lines(run))
            return EXIT_OK
        if state == 'question' and not relaunch_question(run, args.launch):
            emit(question_lines(run))
            return EXIT_ASK
        state = run.get('state')
        if state == 'running':
            if not running_resumable(run.get('running') or {}):
                emit(running_lines(run))
                return EXIT_OK
        elif state in ('ready', 'retry'):
            warns = merge_warnings(warns, warnings_in(launch(paths, run, 'headless')))
            save(paths, run)
        else:
            raise Refused(f'unknown state {state!r} in run.json')
        record_pid(run, 'runner_pid', os.getpid())
        save(paths, run)
        runner = runner_argv(args, run)
        claimed = (run.get('run_id'), int((run.get('running') or {}).get('seq') or 0))
        try:
            log = open(paths.conductor / 'run.log', 'ab')
        except OSError as error:
            # 起こせない: 失敗として数え、retry に戻して runner_pid を消す
            code, lines = fail(paths, run, run.get('running') or {},
                               f'cannot open the run log: {error}', 'launch-failed')
            run.pop('runner_pid', None)
            run.pop('runner_pid_started', None)
            save(paths, run)
            emit(with_warnings(lines, warns))
            return code
    # ロックを放してから起こす (切り離した子は同じ引数の run。--detach は付けない)
    try:
        child = spawn_detached(runner, log)
    except OSError as error:
        with Lock(paths):
            run = load_run(paths)
            if run is not None and run.get('state') == 'running':
                code, lines = fail(paths, run, run.get('running') or {},
                                   f'cannot start the runner: {error}', 'launch-failed')
                run.pop('runner_pid', None)
                run.pop('runner_pid_started', None)
                save(paths, run)
                emit(with_warnings(lines, warns))
                return code
            emit(status_lines(run) if run is not None else status_none_lines(paths))
            return EXIT_OK
    finally:
        if log is not None:
            log.close()
    record_runner(paths, claimed[0], claimed[1], child.pid)
    emit(['state: running', *warns, 'then: ' + command('wait', '--timeout', '300')])
    return EXIT_OK


def cmd_run_loop(args, paths: Paths) -> int:
    try:
        return run_episodes(args, paths)
    finally:
        clear_runner_pid(paths)


def launch_designer(paths: Paths, run: dict, running: dict, warns: list) -> tuple:
    """設計者を起こし、子の pid を run.json に記録して保存する。(child, 終了コード, 出す行) を返す。

    起動できなければ child は None で、出す行は失敗の行 (成功なら空)。
    標準入力にまだ書いていない依頼文は child.task に、書けなかったときの .err のパスは child.err_path に入れる
    (依頼文を書くのはロックを放してから。大きい依頼文でも、待つ間の abort・next --relaunch を止めない)。
    """
    seq = int(running.get('seq') or 0)
    episode = str(running.get('episode') or 'design')
    out_json = paths.episode_file(seq, episode, '.json')
    err_path = paths.episode_file(seq, episode, '.err')
    launcher, problem = designer_command()
    if problem:
        code, lines = fail(paths, run, running, problem, 'launch-failed')
        return None, code, with_warnings(lines, warns)
    roles, role_warns = load_roles()
    warns[:] = merge_warnings(warns, role_warns)
    try:
        argv = designer_argv(launcher, running, roles['designer'])
        task = run_input(running.get('prompt') or '')
    except OSError as error:
        code, lines = fail(paths, run, running, f'cannot read the prompt or the agent definition: {error}',
                           'launch-failed')
        return None, code, with_warnings(lines, warns)
    try:
        with open(out_json, 'wb') as out, open(err_path, 'wb') as err:
            child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=out, stderr=err,
                                     env=claude_run.build_env(), cwd=str(Path.cwd()), creationflags=NO_WINDOW)
    except OSError as error:
        code, lines = fail(paths, run, running, f'cannot start the designer: {error}', 'launch-failed')
        return None, code, with_warnings(lines, warns)
    child.task = task
    child.err_path = err_path
    record_pid(run['running'], 'child_pid', child.pid)
    save(paths, run)
    return child, EXIT_OK, []


def note_prompt_error(err_path, error) -> None:
    """依頼文を書けなかったことを .err に 1 行残す (書けなくても止めない: 終了コードと結果のファイルが決める)。"""
    try:
        with open(err_path, 'a', encoding='utf-8') as err:
            err.write(f'conductor: cannot write the prompt to the designer: {error}\n')
    except OSError:
        pass


def write_prompt(child) -> None:
    """子の標準入力に依頼文を書いて閉じる (ロックを放してから。止められていれば OSError になる)。"""
    try:
        child.stdin.write(child.task)
    except OSError as error:
        note_prompt_error(child.err_path, error)
    finally:
        try:
            child.stdin.close()
        except OSError as error:
            note_prompt_error(child.err_path, error)


def adopt_runner(paths: Paths, run: dict) -> None:
    """run --detach の子が、自分の pid を runner_pid に書き換える (既に自分なら書かない)。"""
    if run.get('runner_pid') != os.getpid():
        record_pid(run, 'runner_pid', os.getpid())
        save(paths, run)


def settle_episode(paths: Paths, run: dict, code: int, out_json) -> tuple:
    """待ち終えた出番の結果を、done と同じに処理する。(終了コード, 出す行, 止まるか) を返す。"""
    active = run.get('running') or {}
    active.pop('child_pid', None)
    active.pop('child_pid_started', None)
    data = output_json(out_json)
    problem = launch_problem(code, data, out_json)
    if problem:
        done_code, lines = fail(paths, run, active, problem, 'launch-failed')
    else:
        done_code, lines = process_done(paths, run)
    if attach_cost(run, active, data):
        save(paths, run)
    stopping = done_code != EXIT_OK or not lines or not str(lines[0]).startswith('state: ready')
    if stopping and run.get('runner_pid') == os.getpid():
        # 状態を書いたのと同じロックの中で runner_pid も消す (wait が古い pid を残して返らないように)
        run.pop('runner_pid', None)
        run.pop('runner_pid_started', None)
        save(paths, run)
    return done_code, lines, stopping


def claimed_by_parent(args, run: dict) -> bool:
    """run --detach の子が最初にロックを取ったとき、親が running にした run_id と seq の出番がまだ running か。

    --run-id の無い (ふつうの) run は常に True。
    """
    if getattr(args, 'run_id', None) is None:
        return True
    running = run.get('running') or {}
    return (run.get('run_id') == args.run_id and run.get('state') == 'running'
            and running.get('seq') == getattr(args, 'seq', None))


def run_episodes(args, paths: Paths) -> int:
    """依頼文 → claude -p → done を、問い・終わり・失敗まで回す。ロックは状態の読み書きの間だけ持つ。"""
    warns = []
    episodes = 0
    while True:
        with Lock(paths):
            run = load_run(paths)
            if run is None:
                return none_exit(['state: none', 'then: ' + command('start', '--goal', '"..."')])
            if episodes == 0 and not claimed_by_parent(args, run):
                # run --detach の親が running にした出番がもう無い (start --force・abort・next --relaunch のあと): 何もしない
                emit(status_lines(run))
                return EXIT_OK
            if args.launch and run.get('launch') != args.launch:
                run['launch'] = args.launch
                save(paths, run)
            state = run.get('state')
            if state in CLOSED:
                emit(closed_lines(run))
                return EXIT_OK
            if state == 'question':
                if not relaunch_question(run, args.launch):
                    emit(question_lines(run))
                    return EXIT_ASK
                save(paths, run)
            state = run.get('state')
            # run --detach の子は、親が書いた child_pid の無い headless の出番から続ける
            if state == 'running' and not running_resumable(run.get('running') or {}):
                emit(running_lines(run))
                return EXIT_OK
            if state == 'running':
                running = run.get('running') or {}
                adopt_runner(paths, run)
            elif state in ('ready', 'retry'):
                lines = launch(paths, run, 'headless')
                warns = merge_warnings(warns, warnings_in(lines))
                save(paths, run)
                running = run['running']
            else:
                raise Refused(f'unknown state {state!r} in run.json')
            run_id = run.get('run_id')
            seq = int(running.get('seq') or 0)
            out_json = paths.episode_file(seq, str(running.get('episode') or 'design'), '.json')
            child, code, lines = launch_designer(paths, run, running, warns)
            if child is None:
                emit(lines)
                return code
        # 依頼文を書くのはロックを放してから (その間の abort・next --relaunch を止めない)
        write_prompt(child)
        # 子を待つ間もロックを持たない (その間の abort・next --relaunch が動く)
        code = child.wait()
        with Lock(paths):
            run = load_run(paths)
            if run is None:
                final = (EXIT_OK, ['state: none'])
            elif (run.get('run_id') != run_id or run.get('state') != 'running'
                  or (run.get('running') or {}).get('seq') != seq):
                # 待つ間に abort・next --relaunch・start --force が動いた: この出番の結果は読まない
                final = (EXIT_OK, status_lines(run))
            else:
                episodes += 1
                done_code, lines, stopping = settle_episode(paths, run, code, out_json)
                stopping = stopping or (args.max_episodes is not None and episodes >= args.max_episodes)
                final = (done_code if stopping else EXIT_OK, with_warnings(lines, warns)) if stopping else None
        if final is not None:
            emit(final[1])
            return final[0]


def cmd_wait(args, paths: Paths) -> int:
    """状態が running でなくなるか、--timeout 秒たつまで待って、status と同じ行を出す (ロックは取らない)。"""
    timeout = WAIT_DEFAULT if args.timeout is None else args.timeout
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        try:
            run = None if no_run(paths) else load_run(paths)
        except Unusable as error:
            if time.monotonic() >= deadline:
                emit([f'state: {UNREADABLE_STATE}', f'error: {error}'])
                return EXIT_FAIL
            time.sleep(WAIT_POLL)
            continue
        if run is None:
            emit(status_none_lines(paths))
            return EXIT_OK
        if stale_running(run):
            # 生きている runner がいない: timeout を待たずに、立て直しの行を出して返る
            emit(status_lines(run))
            return EXIT_OK
        if run.get('state') != 'running':
            wait_lock_gone(paths)
            try:
                run = load_run(paths) or run
            except Unusable:
                pass
            emit(status_lines(run))
            return state_exit(run.get('state'))
        if time.monotonic() >= deadline:
            emit(status_lines(run))
            return EXIT_OK
        time.sleep(WAIT_POLL)


# ---- main ----

def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description='Atelier の進行役。設計者の出番を 1 つずつ回す。')
    sub = ap.add_subparsers(dest='command', required=True)

    def add(name, help_text):
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.add_argument('--dir', default='docs/pbi', help='PBI のディレクトリ (既定 docs/pbi)')
        return p

    p = add('start', 'run を始める (状態 ready、次は design)')
    p.add_argument('--goal', help='目標の文')
    p.add_argument('--goal-file', help='目標のファイル')
    p.add_argument('--workflow', help='ワークフローの名前')
    p.add_argument('--launch', choices=('agent', 'headless'), default='agent', help='起動のしかた (既定 agent)')
    p.add_argument('--force', action='store_true', help='今の run.json を run.<日時>.json に退けて始める')
    p = add('next', '次の出番の依頼文を書き、起動のしかたを出す')
    p.add_argument('--relaunch', action='store_true', help='running で止まった出番を、子を止めて新しい seq で起こし直す')
    add('done', 'running の出番の結果を読み、状態を進める')
    p = add('answer', '残っている問いに答える')
    p.add_argument('--reply', help='答えのファイル')
    p.add_argument('--text', help='答えの文')
    p.add_argument('--approve', action='store_true', help='承認の問いを承認する (返信の囲みは進行役が組む)')
    p.add_argument('--choices', help='--approve の選択 (1=a,2=b)')
    p.add_argument('--revise', action='store_true', help='承認の問いを「直してほしい」として閉じる (--text か --reply)')
    p = add('run', 'headless で出番を回す (claude -p で設計者を起こし、依頼文→起動→done を繰り返す)')
    p.add_argument('--max-episodes', type=int, help='この回数だけ出番を起こして ready のまま止める')
    p.add_argument('--detach', action='store_true', help='runner_pid を残してロックを放ち、切り離して running ですぐ返る')
    p.add_argument('--launch', choices=('agent', 'headless'), help='起動のしかたを run.json に書き換えてから動く')
    # 内部の引数: run --detach が切り離した runner に、親が running にした run_id と seq を渡す
    p.add_argument('--run-id', dest='run_id', help=argparse.SUPPRESS)
    p.add_argument('--seq', type=int, help=argparse.SUPPRESS)
    p = add('wait', 'run の状態が running でなくなるか、--timeout 秒たつまで待って status と同じ行を出す')
    p.add_argument('--timeout', type=float, default=WAIT_DEFAULT, help='待つ秒数 (既定 300)')
    add('status', '今の状態を出す (ロックを取らない)')
    p = add('abort', 'run を aborted で閉じる')
    p.add_argument('--reason', help='止める理由')
    return ap


COMMANDS = {'start': cmd_start, 'next': cmd_next, 'done': cmd_done, 'answer': cmd_answer,
            'run': cmd_run, 'wait': cmd_wait, 'status': cmd_status, 'abort': cmd_abort}


def error_state(paths: Paths, error) -> str:
    """error の行に添える 1 行目の状態。この処理で最後に読めた状態、無ければロックを取らずに読み直す。
    run.json が無いときだけ none。在るのに読めない・使えないときは UNREADABLE_STATE。"""
    if paths.seen in STATES:
        return paths.seen
    if not isinstance(error, Unusable):
        try:
            run = None if no_run(paths) else load_run(paths)
        except Unusable:
            run = False
        if run is None:
            return 'none'
        if run and run.get('state') in STATES:
            return run['state']
    return UNREADABLE_STATE if paths.run.exists() else 'none'


def main(argv=None) -> int:
    common.utf8_stdout()
    args = parser().parse_args(argv)
    paths = Paths(args.dir)
    try:
        return COMMANDS[args.command](args, paths)
    except Refused as error:
        emit([f'state: {error_state(paths, error)}', f'error: {error}'])
        return EXIT_USAGE


if __name__ == '__main__':
    sys.exit(main())
