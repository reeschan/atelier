"""問い・進行役の問い・完了報告をユーザーに見せる 1 本の入口。照合情報を付け、am (answer-me-with-any) で描き、
見せた記録を残す (docs/specs/present-with-am.md、docs/pbi/notes/contract.md の `## present.py`)。

使い方 (リポジトリのルートで):
  python <このディレクトリ>/present.py <path> --kind <kind> [--format html|md|off] [--dir docs/pbi]

kind は approve・decision・send-approval・rework-limit・destructive・full-run・handoff-loop・launch-failure・report。
--format を省くと agents.json の confirm の設定と、am.mjs と node (20 以上) の有無で決まる
(adapter_gate.resolve_confirm)。--format html|md を明示しても am か node が無ければ off に落ち、
理由を missing-am / missing-node にする。--format off は am も node も見ない。

run 情報は <dir>/conductor/run.json から読む (conductor は import しない)。run.json が無ければ inline
(run_id null・seq 0)。有るのに読めない・壊れている、--kind が待っている問いと違う、<path> が待っている問いの
ファイルと違う、report 以外で待っている問いが無い、問いを待っているのに report を見せるときは exit 2。
run の下で html の approve を見せるとき、問いの check が design-record でなければ照合情報の無いページを描かず exit 3。

照合情報 (frontmatter の reply-meta-<name>: <value>) は html のときだけ付ける。
  approve で check が design-record: design_record.approve_meta (design_record.py markdown と同じ)。
  approve 以外の問い (run_id があるとき): design_record.question_meta (run_id・seq・kind・draft_hash)。
  md・off・report: 付けない (元の原稿の reply-meta- の行は残す)。
原稿は <dir>/conductor/present/<run8>-<seq>-<kind>.md に残す (run8 は run_id の先頭 8 文字、inline は inline)。

標準出力 (呼び出し側が読むもの。show: の行のあとをユーザーにそのまま見せる):
  error: <誤りの行>              (exit 3 のときだけ、1 行以上)
  format: <html|md|off> (<reason>)
  warn: <警告>                   (0 行以上)
  log: <dir>/presentations.jsonl
  then: <command>  # <label>     (0 行以上)
  show:
  <見せる部分>                    (html はページのリンク 1 行、md・off は本文)

見せた記録 <dir>/presentations.jsonl に shown の行を 1 行足す (schema atelier.presentation/1、method chars-est/1)。

終了コード: 0 見せた (off に落ちたときも 0) / 2 引数の誤り (ファイルが無い・run.json が読めないなど) /
3 am か照合情報の組み立てが原稿の誤りで描けなかった (off の形で見せ、shown 行は format off で書く)。
"""
import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import adapter_gate
import agents
import common
import design_record

SCHEMA = 'atelier.presentation/1'
METHOD = 'chars-est/1'
LOG_NAME = 'presentations.jsonl'
RUN_SCHEMA = 'atelier.run/1'
# 描いた原稿の置き場所 (<dir> の下)
PRESENT_DIR = Path('conductor') / 'present'
SKILL = Path(__file__).resolve().parent

KINDS = ('approve', 'decision', 'send-approval', 'rework-limit', 'destructive',
         'full-run', 'handoff-loop', 'launch-failure', 'report')
FORMATS = ('html', 'md', 'off')
# relaunch と abort を出す、conductor が作る問い
RUNNER_KINDS = ('handoff-loop', 'launch-failure')

# am の描画を待つ上限 (秒)。止まった am で全体を止めない
AM_TIMEOUT = 120
# 読めない run.json を取り直す長さ (秒)。conductor の READ_WAIT と同じ
READ_WAIT = 1.0
# Windows で子 (node) のコンソールのウインドウを出さない (test_no_window.py)
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
# am の標準出力の印 (ページのファイルと、am serve のときの URL)
PAGE_PREFIX = '✓ '
LINK_PREFIX = '  link:'

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_DRAFT = 3
# am が原稿の誤りで終わるときの終了コードと、知らないオプションのときの終了コード
AM_DRAFT = 1
AM_UNKNOWN_OPTION = 2


class UsageError(Exception):
    """引数・run.json の誤り (exit 2)。"""


class DrawError(Exception):
    """原稿を描けなかった (exit 3)。reason は draft-error / am-error、lines は error: に出す行。"""

    def __init__(self, reason: str, lines: list):
        super().__init__(reason)
        self.reason = reason
        self.lines = lines or ['unknown error']


@dataclass(frozen=True)
class RunInfo:
    run_id: str | None
    seq: int
    check: str | None
    relaunch: str | None
    # run.json の下で見せる (inline でない)
    in_run: bool = False


@dataclass
class Choice:
    format: str
    reason: str
    am: str | None
    node: str | None
    warnings: list


# --- 公開関数 (conductor・試験が使う) ---

def tokens_est(text: str) -> int:
    """chars-est/1 の見積もり。ASCII の文字は 4 文字で 1 トークン (切り上げ)、ほかは 1 文字 1 トークン。"""
    ascii_count = sum(1 for ch in text if ord(ch) < 128)
    return (ascii_count + 3) // 4 + (len(text) - ascii_count)


def log_path(base) -> Path:
    return Path(base) / LOG_NAME


def append_row(base, row: dict) -> None:
    """見せた記録に 1 行足す (ensure_ascii=False、改行は \\n)。"""
    path = log_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a', encoding='utf-8', newline='\n') as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + '\n')


def shown(base, run_id, seq, kind=None) -> bool:
    """presentations.jsonl に同じ run_id・seq の shown 行があるか (format・reason は問わない)。
    kind を渡せば行の kind も同じであることを求める。"""
    try:
        lines = log_path(base).read_text(encoding='utf-8').split('\n')
    except OSError:
        return False
    seq = 0 if seq is None else seq
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (isinstance(row, dict) and row.get('event') == 'shown' and row.get('run_id') == run_id
                and row.get('seq') == seq and (kind is None or row.get('kind') == kind)):
            return True
    return False


def draft_path(base, seq, kind, run_id=None) -> Path:
    """原稿の置き場所 <base>/conductor/present/<run8>-<seq>-<kind>.md。"""
    run8 = str(run_id)[:8] if run_id else 'inline'
    return Path(base) / PRESENT_DIR / f"{run8}-{0 if seq is None else seq}-{kind}.md"


def saved_draft(base, seq, kind, run_id=None) -> Path | None:
    """present が残した原稿があればそのパス、無ければ None。"""
    path = draft_path(base, seq, kind, run_id)
    return path if path.is_file() else None


def log_answered(base, run_id, seq, kind, reply_text, check) -> None:
    """answered の行を足す (conductor.py answer が呼ぶ)。check は ok / mismatch / none。"""
    append_row(base, {'schema': SCHEMA, 'event': 'answered', 'ts': common.now().isoformat(timespec='seconds'),
                      'run_id': run_id, 'seq': seq, 'kind': kind, 'reply_chars': len(reply_text),
                      'reply_bytes': len(reply_text.encode('utf-8')), 'reply_tokens_est': tokens_est(reply_text),
                      'check': check, 'method': METHOD})


# --- run 情報 ---

def read_run(path: Path):
    """run.json を読む。無ければ None。読めない・壊れているときは READ_WAIT 秒まで取り直し、だめなら UsageError。"""
    deadline = time.monotonic() + READ_WAIT
    missing = 0
    while True:
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            # 置き換えの瞬間を無いと見誤らないよう、もう 1 度だけ見る
            missing += 1
            if missing >= 2:
                return None
            problem = 'missing'
        except (OSError, ValueError) as error:
            problem = str(error)
        else:
            if isinstance(data, dict) and data.get('schema') == RUN_SCHEMA:
                return data
            problem = f'not an {RUN_SCHEMA} run'
        if time.monotonic() >= deadline:
            raise UsageError(f'{path.as_posix()}: unreadable ({problem})')
        time.sleep(0.1)


def run_info(base: Path, kind: str, source) -> RunInfo:
    """run.json から run_id と、待っている問いの seq・check・relaunch を読む。
    source (見せるファイル) が待っている問いのパスと違えば UsageError。"""
    path = base / 'conductor' / 'run.json'
    run = read_run(path) if path.exists() else None
    if run is None:
        # inline: approve は design-record.json があれば design-record で照合する
        check = None
        if kind == 'approve' and (base / design_record.RECORD_NAME).is_file():
            check = 'design-record'
        return RunInfo(None, 0, check, None)
    run_id = run.get('run_id')
    run_id = str(run_id) if run_id else None
    question = run.get('question')
    waiting = isinstance(question, dict) and run.get('state') == 'question'
    if kind == 'report':
        if run.get('state') == 'question':
            # 待っている問いと同じ seq で shown 行を書くと、問いを見せた証しと見誤られる
            raise UsageError(f'{path.as_posix()}: a question is waiting; show the report after it is answered')
        seq = run.get('seq')
        return RunInfo(run_id, seq if isinstance(seq, int) else 0, None, None, True)
    if not waiting:
        raise UsageError(f'{path.as_posix()}: no waiting question (state {run.get("state")!r})')
    if question.get('kind') != kind:
        raise UsageError(f"--kind {kind} but the waiting question is {question.get('kind')}")
    if not same_file(source, question.get('path')):
        raise UsageError(f"{Path(source).as_posix()} is not the waiting question ({question.get('path')})")
    seq = question.get('seq')
    return RunInfo(run_id, seq if isinstance(seq, int) else 0, question.get('check') or None,
                   question.get('relaunch') or None, True)


def same_file(source, waiting) -> bool:
    """source と待っている問いのパスが同じファイルを指すか。"""
    if not isinstance(waiting, str) or not waiting:
        return False
    try:
        return os.path.samefile(source, waiting)
    except (OSError, ValueError):
        return Path(source).resolve() == Path(waiting).resolve()


# --- 見せ方 ---

def resolve_choice(fmt_arg, home: Path, skills_dir, env) -> Choice:
    """--format か agents.json の confirm から見せ方を決める (am・node が無ければ off)。"""
    warnings = []
    setting = fmt_arg
    if setting is None:
        settings, load_warnings = agents.load_adapters(home)
        setting = settings['confirm']
        warnings += load_warnings
    resolved = adapter_gate.resolve_confirm(setting, skills_dir, env)
    warnings += resolved['warnings']
    return Choice(resolved['format'], resolved['reason'], resolved['am'], resolved['node'], warnings)


# --- 原稿 ---

def read_source(path) -> str:
    source = Path(path)
    if not source.is_file():
        raise UsageError(f'{source.as_posix()}: no such file')
    try:
        return source.read_bytes().decode('utf-8-sig', errors='replace')
    except OSError as error:
        raise UsageError(f'{source.as_posix()}: {error}') from error


def draft_body(text: str) -> str:
    """照合情報を足さない原稿: 改行を \\n にそろえ、末尾に \\n を置く (元の reply-meta- の行は残す)。"""
    body = design_record.normalize(text)
    return body if body.endswith('\n') else body + '\n'


def approve_draft(base: Path, raw: str) -> str:
    """design-record の照合情報を付けた承認の原稿。record が無い・原稿の誤りは DrawError (draft-error)。"""
    record_file = base / design_record.RECORD_NAME
    record = common.load_json(record_file)
    if not design_record.valid_record(record):
        raise DrawError('draft-error', [f'{record_file.as_posix()}: missing or not a design record (run write first)'])
    try:
        text = design_record.approve_meta(raw, record)
    except design_record.DraftError as error:
        raise DrawError('draft-error', [f'L{number} {what}' for number, what, _ in error.errors]) from error
    except ValueError as error:
        raise DrawError('draft-error', [str(error)]) from error
    return text if text.endswith('\n') else text + '\n'


def build_draft(base: Path, kind: str, raw: str, fmt: str, info: RunInfo, source) -> str:
    """残す原稿を組み立てる。照合情報は html のときだけ付ける。
    run の下の approve で check が design-record でなければ、照合情報の無いページを描かず DrawError。"""
    if fmt != 'html' or kind == 'report':
        return draft_body(raw)
    if kind == 'approve':
        if info.check == 'design-record':
            return approve_draft(base, raw)
        if info.in_run:
            raise DrawError('draft-error', [f'approve without check design-record: {Path(source).as_posix()}'])
        return draft_body(raw)
    if info.run_id is None:
        # inline の問いには照合する conductor がいない
        return draft_body(raw)
    return draft_body(design_record.question_meta(raw, info.run_id, info.seq, kind))


def write_draft(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode('utf-8'))


# --- then の行 ---

quoted = common.quoted


def then_lines(kind: str, info: RunInfo, skill: Path = SKILL) -> list:
    """呼び出し側が打つコマンドの行 (then: <command>  # <label>)。"""
    if kind == 'report':
        return []
    c = f"python {quoted(skill / 'conductor.py')}"
    answer = (f'{c} answer --text "..."', 'answer')
    reply = (f'{c} answer --reply <file>', 'reply')
    approve = (f'{c} answer --approve [--choices 1=a,2=b]', 'approve')
    revise = (f'{c} answer --revise --text "..."', 'revise')
    if kind == 'approve':
        pairs = [reply, approve, revise] if info.check else [approve, revise, answer]
    elif kind == 'full-run':
        pairs = [(f'{c} answer --full-run yes', 'yes'), (f'{c} answer --full-run no', 'no')]
    elif kind in RUNNER_KINDS:
        pairs = [answer]
    else:
        pairs = [answer, reply]
    if kind in RUNNER_KINDS or info.relaunch:
        if info.relaunch:
            pairs.append((info.relaunch, 'relaunch'))
        pairs.append((f'{c} abort', 'abort'))
    return [f'then: {command}  # {label}' for command, label in pairs]


# --- am ---

def render_words(saved: Path, kind: str, fmt: str | None = None) -> list:
    words = ['render', str(saved)]
    if kind == 'report':
        words += ['--template', 'doc']
    if fmt:
        words += ['--format', fmt]
    return words


def am_run(choice: Choice, words: list, env):
    """node <am> <words> を流す。流せない・止まったときは None。"""
    try:
        return subprocess.run([choice.node, choice.am, *words], capture_output=True, text=True, encoding='utf-8',
                              errors='replace', env=agents.cli_env(choice.node, env), timeout=AM_TIMEOUT,
                              creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def am_errors(result) -> list:
    """am の標準エラーの ✗ の行。"""
    return [line for line in (result.stderr or '').replace('\r\n', '\n').split('\n') if line.startswith('✗')]


def am_failure(result) -> DrawError:
    """am の失敗を DrawError にする (exit 1 は原稿の誤り、ほかは am-error。どちらも am の ✗ の行を渡す)。"""
    if result is None:
        return DrawError('am-error', ['am could not run or timed out'])
    lines = am_errors(result) or [f'am exited {result.returncode}']
    return DrawError('draft-error' if result.returncode == AM_DRAFT else 'am-error', lines)


def unknown_format(result) -> bool:
    """--format を知らない (古い) am が 2 で終わったか。標準エラーがそのオプションを名指ししているときだけ。"""
    if result is None or result.returncode != AM_UNKNOWN_OPTION:
        return False
    return any('--format' in line and 'unknown' in line.lower()
               for line in (result.stderr or '').replace('\r\n', '\n').split('\n'))


def page_link(stdout: str) -> tuple:
    """am の標準出力から (見せるリンク, ページのファイル)。link: が無ければ ✓ のパス。"""
    link, page = None, None
    for line in stdout.replace('\r\n', '\n').split('\n'):
        if line.startswith(LINK_PREFIX):
            link = line[len(LINK_PREFIX):].strip()
        elif line.startswith(PAGE_PREFIX):
            page = line[len(PAGE_PREFIX):].strip()
    return link or page, page


def render_html(choice: Choice, kind: str, saved: Path, env) -> dict:
    # am の config (am config set format md) によらず html で描く。--format を知らない am には付けずに 1 度だけ流し直す
    result = am_run(choice, render_words(saved, kind, 'html'), env)
    if unknown_format(result):
        result = am_run(choice, render_words(saved, kind), env)
    if result is None or result.returncode != 0:
        raise am_failure(result)
    link, page = page_link(result.stdout)
    if link is None:
        raise DrawError('am-error', ['am printed no page link'])
    counted = am_run(choice, render_words(saved, kind, 'md'), env)
    md_tokens = tokens_est(counted.stdout) if counted is not None and counted.returncode == 0 else None
    page_bytes = Path(page).stat().st_size if page and Path(page).is_file() else None
    return {'shown': link, 'md_tokens': md_tokens, 'page_bytes': page_bytes}


def render_md(choice: Choice, kind: str, saved: Path, body: str, env) -> dict:
    result = am_run(choice, render_words(saved, kind, 'md'), env)
    if unknown_format(result):
        # --format を知らない am。原稿をそのまま見せ、理由を残す
        choice.format, choice.reason = 'off', 'missing-am'
        choice.warnings.append('am: --format md not supported; showing the file as it is')
        return {'shown': body, 'md_tokens': None, 'page_bytes': None}
    if result is None or result.returncode != 0:
        raise am_failure(result)
    return {'shown': result.stdout, 'md_tokens': tokens_est(result.stdout), 'page_bytes': None}


def render(choice: Choice, kind: str, saved: Path, body: str, env) -> dict:
    if choice.format == 'html':
        return render_html(choice, kind, saved, env)
    if choice.format == 'md':
        return render_md(choice, kind, saved, body, env)
    return {'shown': body, 'md_tokens': None, 'page_bytes': None}


# --- 出力と記録 ---

def emit(choice: Choice, base: Path, then: list, shown_text: str, errors: list) -> None:
    out = [f'error: {line}' for line in errors]
    out.append(f'format: {choice.format} ({choice.reason})')
    out += [f'warn: {warning}' for warning in choice.warnings]
    out.append(f'log: {log_path(base).as_posix()}')
    out += then
    out.append('show:')
    sys.stdout.write('\n'.join(out) + '\n')
    sys.stdout.write(shown_text if not shown_text or shown_text.endswith('\n') else shown_text + '\n')
    sys.stdout.flush()


def shown_row(kind: str, info: RunInfo, choice: Choice, raw: str, shown_text: str, result: dict) -> dict:
    return {'schema': SCHEMA, 'event': 'shown', 'ts': common.now().isoformat(timespec='seconds'),
            'run_id': info.run_id, 'seq': info.seq, 'kind': kind, 'format': choice.format, 'reason': choice.reason,
            'draft_chars': len(raw), 'shown_chars': len(shown_text), 'shown_bytes': len(shown_text.encode('utf-8')),
            'shown_tokens_est': tokens_est(shown_text), 'md_tokens_est': result.get('md_tokens'),
            'page_bytes': result.get('page_bytes') if choice.format == 'html' else None, 'method': METHOD}


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('path', help='問い (または完了報告) のファイル')
    ap.add_argument('--kind', required=True, choices=KINDS, help='問いの種類 (完了報告は report)')
    ap.add_argument('--format', choices=FORMATS, help='見せ方 (省くと agents.json の confirm)')
    ap.add_argument('--dir', default='docs/pbi', help='作業の docs/pbi')
    ap.add_argument('--home', help=argparse.SUPPRESS)
    ap.add_argument('--skills-dir', help=argparse.SUPPRESS)
    return ap.parse_args(argv)


def main(argv=None) -> int:
    common.utf8_stdout()
    args = parse_args(argv)
    base = Path(args.dir)
    try:
        raw = read_source(args.path)
        info = run_info(base, args.kind, args.path)
    except UsageError as error:
        print(f'error: {error}')
        return EXIT_USAGE
    env = os.environ
    skills_dir = args.skills_dir or env.get('ATELIER_SKILLS_DIR') or None
    choice = resolve_choice(args.format, common.resolve_home(args.home), skills_dir, env)
    saved = draft_path(base, info.seq, args.kind, info.run_id)
    body = draft_body(raw)
    try:
        draft = build_draft(base, args.kind, raw, choice.format, info, args.path)
        write_draft(saved, draft)
        result = render(choice, args.kind, saved, body, env)
        errors, code = [], EXIT_OK
    except DrawError as error:
        # 原稿は照合情報を足さない形で残し直し、見せる本文からは照合情報を外す
        write_draft(saved, body)
        choice.format, choice.reason = 'off', error.reason
        result = {'shown': design_record.strip_meta_lines(body), 'md_tokens': None, 'page_bytes': None}
        errors, code = error.lines, EXIT_DRAFT
    shown_text = result['shown']
    emit(choice, base, then_lines(args.kind, info), shown_text, errors)
    append_row(base, shown_row(args.kind, info, choice, raw, shown_text, result))
    return code


if __name__ == '__main__':
    sys.exit(main())
