"""E2E の出番 (e2e) をモデルを使わずに流し、test-result と出番の結果を書く (appendix/tests-at-end.md、`## E2E`)。

conductor.py run が、最後の見直しと修正の段のあとの e2e の出番で起こす。設計者は起こさない
(docs/specs/fix-stage-once.md: E2E は最後の最後に流し、落ちたものだけを直して流し直す)。

  python e2e_run.py --dir docs/pbi --result <出番の結果のパス> --seq <n> [--before-review]

回 (round) を <dir>/verify/e2e.rounds.json に残す。1 回目は下の範囲を流す。2 回目からは、前の回に落ちた spec と、
修正の段の一覧 (<dir>/fix/round-<r>.json で source が e2e、e2e_round が前の回) の rerun だけを流す。前の回で通しが
落ちて spec を 1 つも読めず (`full suite` の行)、rerun も無ければ、通しをもう 1 回流す。

1 回目の範囲 (流す spec) は次の和で、順に重ねる:
  1. PBI の頭の `- Related specs: <spec>, <spec>` の行 (none は無し)。PBI-<n>.md だけを読む
  2. 変えたシンボルを読む spec: merge-base (tests-at-end.json の e2e.base、既定 origin/main、無ければ main) からの
     差分で、test_paths と docs/pbi の外のファイルの変わった行 (足した行と消した行と、それを囲む関数の行 (git の
     差分の見出し)。未追跡のファイルは全体) にある
     定義 (関数・クラス・定数・型・def) の名前を集め、e2e.specs の glob に合う spec のうち、その名前を語として含むもの。
     e2e.symbols が false なら飛ばす。e2e.specs が無ければ飛ばす。3 文字に満たない名前は使わない (どこにでも合うため)
  3. この実行で変わった spec: merge-base からの差分と未追跡のファイルのうち、e2e.specs の glob に合うもの
     (テストの工程や修正の段が直した spec)
  4. e2e.full が true で final があれば、範囲の spec のあとに通しを 1 回 (verify.py --final。承認は承認のときに済んで
     いるので、流す直前にこのスクリプトが full-run.approved.json を書く)。通しが落ちたら、そのログの落ちた印
     (✖・✗・fail) のある行に出てくる spec (e2e.specs の glob のパスか、その名前) ごとに 1 行にする。読めなければ
     `full suite` の 1 行に、ログを読んで落ちた spec を探すよう書く

spec は verify.py --related <spec,...> --each --name e2e で 1 本ずつ流す (サーバーは 1 回だけ用意する)。
結果は <dir>/verify/tests/e2e.test-result.json (atelier.test-result/1、kind e2e)。落ちた spec ごとに 1 行
(test・file は spec、pbi はその spec を関係 spec に持つ PBI か、そのシンボルを変えた PBI、分からなければ none、
cause は unknown)。そのあと role_results.classify で、PBI の Notes の Breaks by design: に合う行を intended にする。
範囲の一覧と理由は <dir>/verify/e2e.scope.md に書く。

出番の結果 (--result) は status done。next は、落ちた spec があり、流し直した回数 (回 - 1) が tests-at-end.json の
e2e.fix_rounds (既定 2) に満たなければ fix (修正の段が落ちたものだけを直す)。落ちたものが無いか上限に来たら finish
(止まった blocked だけで落ちた spec が無いときも finish。完了の報告に書く)。問いは出さない。
--before-review (conductor が、履歴に review の無い run に付ける。この形より前に始めた run の途中) では回を数えず、
1 回目の範囲を流して next を test にする (今までの動き)。

ATELIER_VERIFY_CMD (JSON の文字列の配列) があれば、verify.py の代わりにそれを起こす (テスト用)。

終了コード: 0 = 結果を書いた (落ちた spec や止まった実行があっても 0)。2 = 引数の誤り・結果を書けない。
"""
import argparse
import datetime
import fnmatch
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import common
import role_results

SKILL = Path(__file__).resolve().parent
VERIFY = SKILL / 'verify.py'
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

EXIT_OK = 0
EXIT_USAGE = 2
BLOCKED_EXIT = 3
# verify.py の止めた実行 ("timeout"・"idle") を test-result の整数の終了コードに置き換える
STOPPED_CODE = {'timeout': 124, 'idle': 125}
RESULT_NAME = 'e2e'
FULL_NAME = 'e2e-full'
DEFAULT_BASES = ('origin/main', 'main')
ROUNDS_NAME = 'e2e.rounds.json'
DEFAULT_FIX_ROUNDS = 2
FULL_ROW = 'full suite'
FIX_ROUND_FILE = re.compile(r'^round-(\d+)\.json$')
MIN_NAME = 3

PBI_FILE = re.compile(r'^PBI-\d+\.md$')
RELATED = re.compile(r'^\s*[-*]?\s*Related specs:\s*(.*?)\s*$', re.M)
SECTION = re.compile(r'^##\s+(.+?)\s*$', re.M)
PATH_WORD = re.compile(r'`([^`\s]+)`|(?<![\w/.-])([\w.-]+(?:/[\w.$\[\]-]+)+)')
# 定義の名前 (JS・TS・Python)。行の頭か export などの後ろにあるものだけ
DEFINITIONS = [
    re.compile(r'\b(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)'),
    re.compile(r'\bclass\s+([A-Za-z_$][\w$]*)'),
    re.compile(r'^\s*(?:export\s+)?(?:default\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*[=:]'),
    re.compile(r'^\s*(?:export\s+)?(?:declare\s+)?(?:interface|type|enum)\s+([A-Za-z_$][\w$]*)'),
    re.compile(r'^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)'),
]


def git(*args) -> tuple[int, str]:
    try:
        proc = subprocess.run(['git', *args], capture_output=True, text=True, encoding='utf-8', errors='replace',
                              creationflags=NO_WINDOW)
    except OSError as error:
        return 127, str(error)
    return proc.returncode, proc.stdout


def pbi_files(base: Path) -> list[Path]:
    return sorted(path for path in base.glob('PBI-*.md') if PBI_FILE.match(path.name))


def related_specs(base: Path) -> dict[str, list[str]]:
    """spec → それを関係 spec に持つ PBI の一覧 (PBI の順)。"""
    found: dict[str, list[str]] = {}
    for path in pbi_files(base):
        match = RELATED.search(path.read_text(encoding='utf-8', errors='replace'))
        if not match:
            continue
        for spec in match.group(1).split(','):
            spec = spec.strip().strip('`')
            if spec and spec.lower() != 'none':
                found.setdefault(spec, []).append(path.stem)
    return found


def pbi_targets(base: Path) -> dict[str, set[str]]:
    """PBI → `## Targets` の節に書かれたパス。"""
    out = {}
    for path in pbi_files(base):
        text = path.read_text(encoding='utf-8', errors='replace')
        heads = list(SECTION.finditer(text))
        paths = set()
        for i, head in enumerate(heads):
            if head.group(1).strip().lower() != 'targets':
                continue
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            for match in PATH_WORD.finditer(text[head.end():end]):
                paths.add((match.group(1) or match.group(2)).replace('\\', '/'))
        out[path.stem] = paths
    return out


def merge_base(wanted) -> str | None:
    for ref in [wanted] if wanted else DEFAULT_BASES:
        code, out = git('merge-base', ref, 'HEAD')
        if code == 0 and out.strip():
            return out.strip()
    return None


def matches(path: str, globs) -> bool:
    return any(fnmatch.fnmatch(path, glob) for glob in globs)


def changed_names(base: Path, settings: dict, mb: str) -> dict[str, str]:
    """変えたシンボルの名前 → そのファイル。test_paths と <dir> の下は見ない。"""
    skip = [*(settings.get('test_paths') or []), f'{base.as_posix().rstrip("/")}/**']
    names: dict[str, str] = {}

    def take(file, lines):
        for line in lines:
            for pattern in DEFINITIONS:
                for match in pattern.finditer(line):
                    name = match.group(1)
                    if len(name) >= MIN_NAME:
                        names.setdefault(name, file)

    code, out = git('diff', '--name-only', mb)
    tracked = [line.strip() for line in out.splitlines() if line.strip()] if code == 0 else []
    for file in tracked:
        if matches(file, skip):
            continue
        code, diff = git('diff', '-U0', mb, '--', file)
        if code == 0:
            # 変わった行と、変わった行を囲む関数 (git の見出し @@ ... @@ <関数の行>) の定義を取る
            take(file, [line[1:] if line[:1] in '+-' else line.split('@@', 2)[-1]
                        for line in diff.splitlines()
                        if (line[:1] in '+-' and not line.startswith(('+++', '---'))) or line.startswith('@@')])
    code, out = git('ls-files', '--others', '--exclude-standard')
    for file in (line.strip() for line in out.splitlines() if code == 0 and line.strip()):
        if matches(file, skip):
            continue
        try:
            take(file, Path(file).read_text(encoding='utf-8', errors='replace').splitlines())
        except OSError:
            continue
    return names


def spec_files(patterns) -> list[str]:
    if isinstance(patterns, str):
        patterns = [patterns]
    found = set()
    for pattern in patterns or []:
        for path in Path('.').glob(pattern):
            if path.is_file():
                found.add(path.as_posix())
    return sorted(found)


def symbol_specs(names: dict[str, str], specs: list[str]) -> dict[str, str]:
    """spec → それが読む変えたシンボルの名前 (最初の 1 つ)。"""
    if not names:
        return {}
    words = re.compile(r'(?<![\w$])(' + '|'.join(re.escape(name) for name in sorted(names)) + r')(?![\w$])')
    out = {}
    for spec in specs:
        try:
            match = words.search(Path(spec).read_text(encoding='utf-8', errors='replace'))
        except OSError:
            continue
        if match:
            out[spec] = match.group(1)
    return out


def owner_of(file: str, targets: dict[str, set[str]]) -> str:
    for pbi, paths in targets.items():
        if any(file == path or file.endswith('/' + path) or path.endswith('/' + file) for path in paths):
            return pbi
    return 'none'


def scope(base: Path, settings: dict) -> tuple[list[dict], list[str]]:
    """流す spec の一覧 [{spec, reason, pbi}] と、範囲を決めるときの注記の行。"""
    e2e = settings.get('e2e') if isinstance(settings.get('e2e'), dict) else {}
    notes = []
    rows = []
    for spec, pbis in related_specs(base).items():
        rows.append({'spec': spec, 'reason': f"related spec of {', '.join(pbis)}", 'pbi': pbis[0]})
    if e2e.get('symbols', True) is False:
        notes.append('symbols: off (e2e.symbols false)')
    elif not e2e.get('specs'):
        notes.append('symbols: skipped (no e2e.specs glob)')
    else:
        mb = merge_base(e2e.get('base'))
        if mb is None:
            notes.append(f"symbols: skipped (no merge-base with {e2e.get('base') or ' or '.join(DEFAULT_BASES)})")
        else:
            names = changed_names(base, settings, mb)
            targets = pbi_targets(base)
            known = {row['spec'] for row in rows}
            for spec, name in symbol_specs(names, spec_files(e2e['specs'])).items():
                if spec not in known:
                    rows.append({'spec': spec, 'reason': f'reads {name} ({names[name]})',
                                 'pbi': owner_of(names[name], targets)})
            notes.append(f'symbols: {len(names)} changed names')
    if e2e.get('specs'):
        mb = merge_base(e2e.get('base'))
        if mb is not None:
            known = {row['spec'] for row in rows}
            targets = pbi_targets(base)
            for spec in changed_specs(mb, spec_files(e2e['specs'])):
                if spec not in known:
                    rows.append({'spec': spec, 'reason': 'changed in this run', 'pbi': owner_of(spec, targets)})
    return rows, notes


def changed_specs(mb: str, specs: list[str]) -> list[str]:
    """spec の一覧のうち、merge-base から変わったもの (未追跡のものも)。"""
    code, out = git('diff', '--name-only', mb)
    changed = {line.strip() for line in out.splitlines() if line.strip()} if code == 0 else set()
    code, out = git('ls-files', '--others', '--exclude-standard')
    if code == 0:
        changed |= {line.strip() for line in out.splitlines() if line.strip()}
    return [spec for spec in specs if spec in changed]


def verify_head() -> list[str]:
    given = os.environ.get('ATELIER_VERIFY_CMD')
    if given:
        try:
            words = json.loads(given)
        except ValueError:
            words = None
        if isinstance(words, list) and words and all(isinstance(word, str) and word for word in words):
            return words
    return [sys.executable, str(VERIFY)]


# 止められたとき (conductor の abort・relaunch) に、verify.py とその子 (テスト・サーバー) を道連れにする。
# Windows: このプロセスが終わると閉じるジョブ (KILL_ON_JOB_CLOSE) に verify.py を入れる。子と孫はジョブを受け継ぐので、
# TerminateProcess で止められても木ごと止まる。POSIX: verify.py を新しいセッションで起こし、SIGTERM でグループに送る
_JOB = None
_CHILD = None
# verify.py が自分の子の木を止める時間。conductor.stop_child の待ち (3 秒) より短くする
STOP_WAIT = 2


def _windows_job():
    """閉じると中のプロセスを止めるジョブ。作れなければ None (止めたときに子が残ることがある)。"""
    global _JOB
    if _JOB is not None or os.name != 'nt':
        return _JOB
    try:
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                        ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                        ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                        ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD),
                        ('SchedulingClass', wintypes.DWORD)]

        class Counters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ('a', 'b', 'c', 'd', 'e', 'f')]

        class Extended(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', Basic), ('IoInfo', Counters),
                        ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                        ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]

        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            return None
        _JOB = (kernel32, job)
    except (OSError, AttributeError):
        return None
    return _JOB


def _join_job(proc) -> None:
    job = _windows_job()
    if job is None:
        return
    import ctypes
    from ctypes import wintypes
    kernel32, handle = job
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.AssignProcessToJobObject(handle, wintypes.HANDLE(int(proc._handle)))


def _on_stop(signum, _frame):
    """POSIX で止められたら、verify.py のグループに同じ合図を送り、待ってから終わる。"""
    # 中断した本体が child.wait() のロックを持っているので、ここでは Popen.wait を使わず os.waitpid で回収する
    # (Popen.wait はロックが空くまで timeout いっぱい待ち、conductor の待ち時間を超える)
    child = _CHILD
    if child is not None:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.monotonic() + STOP_WAIT
        while time.monotonic() < deadline:
            try:
                if os.waitpid(child.pid, os.WNOHANG)[0]:
                    break
            except ChildProcessError:
                break
            time.sleep(0.05)
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except OSError:
            pass
    sys.exit(128 + signum)


def run_verify(base: Path, *args) -> int:
    """verify.py を流し、出力を <dir>/verify/<名前>.run.log に書く。終了コードを返す。

    このスクリプトが止められたら verify.py の木も止まる (Windows はジョブ、POSIX はセッションへの合図)。
    """
    global _CHILD
    name = args[args.index('--name') + 1]
    log = base / 'verify' / f'{name}.run.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    # 前の実行の <名前>.json を今回の結果と取り違えないよう、流す前に消す
    (base / 'verify' / f'{name}.json').unlink(missing_ok=True)
    extra = {} if os.name == 'nt' else {'start_new_session': True}
    with open(log, 'wb') as out:
        try:
            _CHILD = subprocess.Popen([*verify_head(), *args, '--dir', str(base)], stdout=out,
                                      stderr=subprocess.STDOUT, creationflags=NO_WINDOW, **extra)
        except OSError as error:
            out.write(f'e2e_run: cannot start verify.py: {error}\n'.encode('utf-8'))
            return 127
        try:
            if os.name == 'nt':
                _join_job(_CHILD)
            return _CHILD.wait()
        finally:
            _CHILD = None


def last_line(base: Path, name: str, prefix: str) -> str:
    try:
        lines = (base / 'verify' / f'{name}.run.log').read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return ''
    return next((line[len(prefix):].strip() for line in reversed(lines) if line.startswith(prefix)), '')


FAIL_WORDS = re.compile(r'✖|✗|\bfail(?:ed|ing|ure)?\b|\bFAIL\b', re.I)


def failed_specs(log, known: list[str]) -> list[str]:
    """通しのログの、落ちた印のある行に出てくる spec (known のパスか、その名前)。順は known のまま。"""
    if not log or not known:
        return []
    try:
        lines = Path(log).read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return []
    marked = [line for line in lines if FAIL_WORDS.search(line)]
    found = []
    for spec in known:
        name = Path(spec).name
        if any(spec in line or re.search(r'(?<![\w.-])' + re.escape(name) + r'(?![\w.-])', line) for line in marked):
            found.append(spec)
    return found


def int_code(code) -> int:
    if isinstance(code, int) and not isinstance(code, bool):
        return code
    return STOPPED_CODE.get(code, 1)


def write_scope(base: Path, rows: list[dict], notes: list[str], full: str) -> Path:
    path = base / 'verify' / 'e2e.scope.md'
    lines = ['# E2E scope', '', '| spec | why | PBI |', '|---|---|---|']
    lines += [f"| {row['spec']} | {row['reason']} | {row['pbi']} |" for row in rows] or ['| none | | |']
    lines += ['', *(f'- {note}' for note in notes), f'- full suite: {full}', '']
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(lines), encoding='utf-8', newline='\n')
    return path


def load_settings(base: Path) -> tuple[dict, dict]:
    settings = common.load_json(base / 'tests-at-end.json', {})
    settings = settings if isinstance(settings, dict) else {}
    e2e = settings.get('e2e') if isinstance(settings.get('e2e'), dict) else {}
    return settings, e2e


def fix_rounds(e2e: dict) -> int:
    """落ちた spec を直して流し直す回数の上限 (e2e.fix_rounds、0 以上の整数。無い・不正なら既定)。"""
    value = e2e.get('fix_rounds')
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return DEFAULT_FIX_ROUNDS


def load_rounds(base: Path) -> list[dict]:
    data = common.load_json(base / 'verify' / ROUNDS_NAME, {})
    rounds = data.get('rounds') if isinstance(data, dict) else None
    return [row for row in rounds or [] if isinstance(row, dict)]


def fix_rerun(base: Path, e2e_round) -> list[str]:
    """修正の段が e2e の回 e2e_round の落ちたものから書いた一覧の rerun (通しのログから読んだ spec)。"""
    found = []
    for path in sorted((base / 'fix').glob('round-*.json')):
        if not FIX_ROUND_FILE.match(path.name):
            continue
        data = common.load_json(path, {})
        if not isinstance(data, dict) or data.get('source') != 'e2e' or data.get('e2e_round') != e2e_round:
            continue
        for spec in data.get('rerun') or []:
            if isinstance(spec, str) and spec.strip() and spec.strip() not in found:
                found.append(spec.strip())
    return found


def rerun_scope(base: Path, last: dict) -> tuple[list[dict], list[str], bool]:
    """2 回目からの範囲: 前の回に落ちた spec と修正の段の rerun。(行, 注記, 通しを流し直すか) を返す。"""
    rows, seen = [], set()
    full_failed = False
    for row in last.get('failing') or []:
        if not isinstance(row, dict):
            continue
        if row.get('test') == FULL_ROW:
            full_failed = True
            continue
        spec = row.get('file') or row.get('test')
        if isinstance(spec, str) and spec and spec not in seen:
            seen.add(spec)
            rows.append({'spec': spec, 'reason': f"failed in round {last.get('round')}",
                         'pbi': row.get('pbi') or 'none'})
    named = fix_rerun(base, last.get('round'))
    for spec in named:
        if spec not in seen:
            seen.add(spec)
            rows.append({'spec': spec, 'reason': 'named by the fix stage (rerun)', 'pbi': 'none'})
    return rows, [f"rerun of round {last.get('round')}: {len(rows)} specs"], full_failed and not named


def run_e2e(base: Path, rows=None, notes=None, full_again=False) -> tuple[dict, list[str]]:
    """範囲を決めて流し、test-result (辞書) と要約の行を返す。rows を渡すと、その範囲だけを流す (2 回目から)。"""
    settings, e2e = load_settings(base)
    first = rows is None
    if first:
        rows, notes = scope(base, settings)
    commands, failing, blocked = [], [], []
    investigate = settings.get('investigate')
    if rows and not (isinstance(investigate, str) and '{specs}' in investigate):
        blocked.append('no investigate command with {specs} in tests-at-end.json')
    elif rows:
        code = run_verify(base, '--related', ','.join(row['spec'] for row in rows), '--each', '--name', RESULT_NAME)
        if code == BLOCKED_EXIT:
            blocked.append(last_line(base, RESULT_NAME, 'blocked:') or 'verify.py stopped')
        elif code != 0:
            blocked.append(f'verify.py exited {code} (see {(base / "verify" / f"{RESULT_NAME}.run.log").as_posix()})')
        ran = common.load_json(base / 'verify' / f'{RESULT_NAME}.json', {})
        by_spec = {row.get('spec'): row for row in (ran.get('results') or []) if isinstance(row, dict)}
        for row in rows:
            got = by_spec.get(row['spec'])
            if got is None:
                continue
            code = int_code(got.get('code'))
            commands.append({'command': str(got.get('command') or row['spec']), 'code': code})
            if code != 0:
                failing.append({'test': row['spec'], 'file': row['spec'], 'pbi': row['pbi'], 'cause': 'unknown',
                                'why': f"exit {got.get('code')} after {got.get('tries', 1)} tries; {row['reason']}; "
                                       f"log {got.get('log')}"})
    full = 'not approved' if first else 'not run again'
    final = settings.get('final')
    if (e2e.get('full') is True if first else full_again) and isinstance(final, str) and final.strip():
        token = base / 'full-run.approved.json'
        common.write_json(token, {'by': 'approval (e2e.full)', 'at': datetime.datetime.now().isoformat()})
        code = run_verify(base, '--final', '--name', FULL_NAME)
        ran = common.load_json(base / 'verify' / f'{FULL_NAME}.json', {})
        got = next(iter(ran.get('results') or []), {}) if isinstance(ran, dict) else {}
        # 結果の無い実行 (verify.py が起きない・断った) は、その終了コードで落ちたものとする
        raw = got.get('code', code if code != 0 else 'no result')
        full_code = int_code(raw)
        commands.append({'command': final, 'code': full_code})
        full = 'passed' if full_code == 0 else f'failed (exit {raw})'
        if full_code != 0:
            owners = {row['spec']: row['pbi'] for row in rows}
            specs = failed_specs(got.get('log'), spec_files(e2e.get('specs')))
            for spec in specs:
                failing.append({'test': spec, 'file': spec, 'pbi': owners.get(spec, 'none'), 'cause': 'unknown',
                                'why': f"failed in the full suite (exit {raw}); log {got.get('log')}"})
            if not specs:
                failing.append({'test': FULL_ROW, 'file': final, 'pbi': 'none', 'cause': 'unknown',
                                'why': f"the full suite exited {raw} and no failing spec could be read from its "
                                       f"log; read the log {got.get('log') or (base / 'verify' / f'{FULL_NAME}.run.log').as_posix()} "
                                       'for the failing specs'})
        if code == BLOCKED_EXIT:
            blocked.append('full suite: ' + (last_line(base, FULL_NAME, 'blocked:') or 'stopped'))
    elif first and e2e.get('full') is True:
        full = 'approved, but no final command'
    data = {'schema': role_results.TEST_RESULT, 'kind': 'e2e', 'commands': commands, 'failing': failing,
            'recommendation': 'pass' if not failing and not blocked else 'needs-designer'}
    if blocked:
        data['blocked'] = '; '.join(blocked)
    intended = role_results.classify(data, role_results.breaks_by_design(base))
    scope_path = write_scope(base, rows, notes, full)
    related = sum(1 for row in rows if row['reason'].startswith('related'))
    head = (f'{len(rows)} specs ({related} related, {len(rows) - related} by changed symbols or files)' if first
            else f'{len(rows)} specs (the failing ones again)')
    summary = [f'e2e: {head}, {len(failing)} failing ({len(intended)} intended by design)',
               f'full suite: {full}', f'scope: {scope_path.as_posix()}']
    if blocked:
        summary.append(common_fold('blocked: ' + data['blocked']))
    return data, summary


def common_fold(text: str, limit: int = role_results.SUMMARY_MAX_CHARS) -> str:
    return text if len(text) <= limit else text[:limit - 3] + '...'


def main(argv=None) -> int:
    common.utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--dir', default='docs/pbi')
    ap.add_argument('--result', required=True, help='出番の結果のパス (conductor の Result:)')
    ap.add_argument('--seq', type=int, required=True)
    ap.add_argument('--run', help='run の id (conductor が渡す)。回の記録はこの run のものだけを数える')
    ap.add_argument('--before-review', action='store_true',
                    help='この形より前に始めた run の途中 (履歴に review が無い): 回を数えず、next を test にする')
    args = ap.parse_args(argv)
    if os.name != 'nt':
        signal.signal(signal.SIGTERM, _on_stop)
    base = Path(args.dir)
    test_result = base / 'verify' / 'tests' / f'{RESULT_NAME}.test-result.json'
    kept = load_rounds(base)
    # 前の run の回は数えない (同じ docs/pbi で start をやり直したとき)
    rounds = [row for row in kept if not args.run or row.get('run') == args.run]
    if args.before_review:
        data, summary = run_e2e(base)
        nxt = 'test'
    else:
        _, e2e = load_settings(base)
        limit = fix_rounds(e2e)
        number = len(rounds) + 1
        if rounds:
            rows, notes, full_again = rerun_scope(base, rounds[-1])
            data, summary = run_e2e(base, rows, notes, full_again)
        else:
            data, summary = run_e2e(base)
        if data['failing'] and number - 1 < limit:
            nxt = 'fix'
            summary.insert(1, f'round {number} of at most {limit + 1}: the fix stage fixes the failing specs, '
                              'then only those run again')
        elif data['failing']:
            nxt = 'finish'
            summary.insert(1, f'round {number} of at most {limit + 1}: limit reached; the failing specs go to the '
                              'completion report')
        else:
            nxt = 'finish'
            summary.insert(1, f'round {number}: nothing failing')
        full = next((line[len('full suite: '):] for line in summary if line.startswith('full suite: ')), None)
        rounds.append({'run': args.run, 'round': number, 'at': datetime.datetime.now().isoformat(timespec='seconds'),
                       'specs': len(data['commands']), 'failing': data['failing'], 'full': full,
                       'blocked': data.get('blocked'), 'next': nxt})
    summary.append(f'result: {test_result.as_posix()}')
    episode = {'schema': role_results.EPISODE, 'episode': 'e2e', 'seq': args.seq, 'status': 'done',
               'summary': [common_fold(line) for line in summary[:role_results.SUMMARY_MAX_LINES]],
               'next': {'episode': nxt}}
    try:
        common.write_json(test_result, data)
        if not args.before_review:
            common.write_json(base / 'verify' / ROUNDS_NAME, {'rounds': rounds})
        common.write_json(args.result, episode)
    except OSError as error:
        print(f'e2e_run: cannot write the result: {error}', file=sys.stderr)
        return EXIT_USAGE
    for line in episode['summary']:
        print(line)
    return EXIT_OK


if __name__ == '__main__':
    sys.exit(main())
