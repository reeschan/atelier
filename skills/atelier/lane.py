"""作業を始める前に、変更予定のファイル一覧や作業ツリーの差分から Atelier か軽い道かを決める。

  python lane.py --goal-file F --paths F [--new F] [--light|--full] [--pbi-dir D]
  python lane.py check --diff [--base origin/main] [--pbi-dir D]

軽い道の条件 (テストと文書を除く):
  - 変わるコードが MAX_FILES (8) 本以下
  - モジュールが MAX_MODULES (2) 個以下で、新しいモジュールが無い
  - データモデル・入出力・マニフェスト・配置先のファイルを新しく足さない
  - ゴールとパスが SENSITIVE_WORDS に当たらない

常に atelier になる条件 (--light でも上書きしない):
  - パス一覧が空
  - <pbi-dir>/conductor/run.json があり、state が finished / aborted でない (読めない場合を含む)
  - ATELIER_EPISODE が設定されている、または CLAUDE_CODE_SESSION_ID が無い

新しいファイルを check --diff に数えるには、呼び出し側が先に `git add -N` しておく。
標準出力: 1 行目 `lane: light` か `lane: atelier`、続いて当たった信号ごとの `reason: ...` (問いは出さない)。
終了コード: 0 = 正常 (判定に関わらず) / 2 = 引数の誤り・入力ファイルが読めない・git が失敗した。
"""
from __future__ import annotations

import argparse
import datetime
import fnmatch
import json
import os
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import adapter_gate
import agents
import common
import conductor
import heavy_check
import role_results
import task_profile

try:
    import events
except (Exception, SystemExit):  # events.py が無くても記録以外の判定は動く
    events = None

# 閾値
MAX_FILES = 8        # 呼び手の文脈で読み切れる本数
MAX_MODULES = 2      # 2 つを超えると分けて作る方が安い
SENSITIVE_WORDS = (  # 誤ると戻しにくい変更を重い道へ回す語
    'auth', 'permission', 'password', 'secret', 'token', 'credential', 'migration', 'schema',
    'encrypt', 'billing', 'payment', 'delete', '認証', '権限', '秘密', '削除', '暗号', '課金', '決済',
)
CLOSED_STATES = ('finished', 'aborted')
RUN_FILE = Path('conductor') / 'run.json'
TESTS_FILE = 'tests-at-end.json'
NEW_KINDS = (
    ('data-model', lambda p: heavy_check.matches(p, heavy_check.DATA_MODEL)),
    ('interface', lambda p: heavy_check.matches(p, heavy_check.INTERFACE)),
    ('manifest', heavy_check.is_manifest),
    ('deploy', lambda p: heavy_check.matches(p, heavy_check.DEPLOY)),
)
EXIT_OK, EXIT_USAGE = 0, 2


@dataclass(frozen=True)
class Change:
    """判定対象となる変更ファイルとベースツリーの組。"""

    paths: tuple[str, ...]        # posix、リポジトリルート基準、重複除去・順序保持
    new: frozenset[str]           # 追加ファイル (paths の部分集合)
    base_files: tuple[str, ...]   # ベースツリー上のファイル (新規モジュール判定用)


@dataclass(frozen=True)
class Verdict:
    """道の判定結果と理由一覧。"""

    lane: str                     # 'light' | 'atelier'
    reasons: tuple[str, ...]      # 'reason: ' の後ろに出す文言


def posix(text: str, root: Path) -> str:
    """パス文字列をリポジトリルート相対の POSIX 形式に正規化する。"""
    s = text.strip().replace('\\', '/')
    if not s:
        return ''
    if Path(s).is_absolute():
        try:
            return Path(s).resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return s
    while s.startswith('./'):
        s = s[2:]
    return s


def read_list(path: Path, root: Path) -> list[str]:
    """1 行 1 パスのファイルを読み、空行を除いて正規化した一覧を返す。"""
    items = []
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        p = posix(line, root)
        if p:
            items.append(p)
    return items


def test_globs(pbi: Path) -> tuple[str, ...]:
    """<pbi-dir>/tests-at-end.json の test_paths を返す。無ければ空。"""
    data = common.load_json(pbi / TESTS_FILE, {})
    if isinstance(data, dict):
        paths = data.get('test_paths')
        if isinstance(paths, list):
            return tuple(p.replace('\\', '/') for p in paths if isinstance(p, str))
    return ()


def is_test_path(path: str, globs: tuple[str, ...]) -> bool:
    """** を含む glob にも対応してテストパスかを判定する。"""
    for g in globs:
        if fnmatch.fnmatchcase(path, g):
            return True
        if '**/' in g and fnmatch.fnmatchcase(path, g.replace('**/', '')):
            return True
    return False


def counted(path: str, globs: tuple[str, ...]) -> bool:
    """文書とテストを除いたコードファイルとして数えるかを返す。"""
    return heavy_check.is_code(path) and not is_test_path(path, globs)


def base_tree(root: Path, ref: str = 'HEAD') -> tuple[str, ...]:
    """git ls-tree で基準ツリーのファイル一覧を返す。"""
    out = heavy_check.git(root, 'ls-tree', '-r', '--name-only', ref)
    return tuple(line for line in out.splitlines() if line)


def change_from_lists(paths: list[str], new: list[str], root: Path) -> Change:
    """--paths と --new のファイル一覧と HEAD から Change を組み立てる。"""
    merged = tuple(dict.fromkeys([*paths, *new]))
    base = base_tree(root) if merged else ()
    base_set = set(base)
    new_set = frozenset(new) | {p for p in merged if p not in base_set}
    return Change(paths=merged, new=new_set, base_files=base)


def change_from_diff(root: Path, base: str, pbi: Path) -> Change:
    """heavy_check.measure で作業ツリーの差分を測り Change を返す。"""
    measured = heavy_check.measure(root, base, pbi)
    paths = tuple(dict.fromkeys(item['path'] for item in measured['files']))
    new = frozenset(item['path'] for item in measured['files'] if item['status'] == 'A')
    return Change(paths=paths, new=new, base_files=tuple(measured['base_files']))


def open_run_state(pbi: Path) -> str | None:
    """閉じていない run.json があれば state (読めなければ 'unreadable')、無ければ None を返す。"""
    run_path = pbi / RUN_FILE
    try:
        text = run_path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return None
    except OSError:
        return 'unreadable'
    try:
        data = json.loads(text)
    except ValueError:
        return 'unreadable'
    if not isinstance(data, dict):
        return 'unreadable'
    state = data.get('state')
    if not isinstance(state, str) or not state:
        return 'unreadable'
    if state in CLOSED_STATES:
        return None
    return state


def main_session(env: Mapping[str, str]) -> bool:
    """呼び手が Claude Code の main セッションかを返す。"""
    return 'ATELIER_EPISODE' not in env and bool(env.get('CLAUDE_CODE_SESSION_ID'))


def hard_reasons(change: Change, pbi: Path, env: Mapping[str, str]) -> list[str]:
    """--light でも上書きしない、常に atelier となる理由を返す。"""
    reasons = []
    if not change.paths:
        reasons.append('empty path list')
    state = open_run_state(pbi)
    if state is not None:
        reasons.append(f'open run {(pbi / RUN_FILE).as_posix()} (state {state})')
    if not main_session(env):
        reasons.append('not the main Claude Code session')
    return reasons


def size_reasons(change: Change, globs: tuple[str, ...]) -> tuple[list[str], int, int]:
    """ファイル数・モジュール数・新規モジュールの理由と件数を返す。"""
    code = [p for p in change.paths if counted(p, globs)]
    modules = sorted({heavy_check.module_of(p) for p in code})
    base_modules = {heavy_check.module_of(p) for p in change.base_files if counted(p, globs)}
    new_modules = [m for m in modules if m != '.' and m not in base_modules]
    reasons = []
    if len(code) > MAX_FILES:
        reasons.append(f'files {len(code)} > {MAX_FILES}')
    if len(modules) > MAX_MODULES:
        reasons.append(f'modules {len(modules)} > {MAX_MODULES}')
    for m in new_modules:
        reasons.append(f'new module {m}')
    return reasons, len(code), len(modules)


def new_file_reasons(change: Change, globs: tuple[str, ...]) -> list[str]:
    """新規追加されたデータモデル・入出力・マニフェスト・配置先ファイルの理由を返す。"""
    reasons = []
    for path in change.paths:
        if path not in change.new or not counted(path, globs):
            continue
        for kind, predicate in NEW_KINDS:
            if predicate(path):
                reasons.append(f'new {kind} file {path}')
    return reasons


def sensitive_reasons(goal: str, paths: tuple[str, ...] | list[str]) -> list[str]:
    """ゴール文面とパスに含まれる sensitive 語の理由を語ごとに最初の 1 件返す。"""
    goal_lower = goal.lower()
    lowered = [(p, p.lower()) for p in paths]
    reasons = []
    for word in SENSITIVE_WORDS:
        w = word.lower()
        if w in goal_lower:
            reasons.append(f'sensitive word {word} (goal)')
        else:
            hit = next((p for p, pl in lowered if w in pl), None)
            if hit is not None:
                reasons.append(f'sensitive word {word} (path {hit})')
    return reasons


def soft_reasons(change: Change, goal: str, globs: tuple[str, ...]) -> tuple[list[str], str]:
    """上書き可能な理由一覧と、軽い道時の小規模サマリ行を返す。"""
    sizes, n_files, n_modules = size_reasons(change, globs)
    reasons = sizes + new_file_reasons(change, globs) + sensitive_reasons(goal, change.paths)
    return reasons, f'small ({n_files} files, {n_modules} modules)'


def decide(hard: list[str], soft: list[str], small: str, override: str | None) -> Verdict:
    """信号と上書き指定から最終的な Verdict を決める。"""
    if hard:
        extra = ['override --full'] if override == 'full' else []
        return Verdict('atelier', tuple([*hard, *soft, *extra]))
    if override == 'full':
        return Verdict('atelier', tuple([*soft, 'override --full']))
    if soft and override == 'light':
        return Verdict('light', tuple([*soft, 'override --light']))
    if soft:
        return Verdict('atelier', tuple(soft))
    return Verdict('light', (small,))


def render(verdict: Verdict) -> str:
    """Verdict を標準出力向けの行テキストに整形する。"""
    return '\n'.join([f'lane: {verdict.lane}', *(f'reason: {r}' for r in verdict.reasons)])


def cmd_decide(args: argparse.Namespace) -> int:
    """--goal-file と --paths から道を判定して出力する。"""
    root = Path.cwd()
    goal = Path(args.goal_file).read_text(encoding='utf-8', errors='replace')
    paths = read_list(Path(args.paths), root)
    new = read_list(Path(args.new), root) if args.new else []
    pbi = Path(args.pbi_dir)
    change = change_from_lists(paths, new, root)
    hard = hard_reasons(change, pbi, os.environ)
    soft, small = soft_reasons(change, goal, test_globs(pbi))
    print(render(decide(hard, soft, small, args.override)))
    return EXIT_OK


def cmd_check(args: argparse.Namespace) -> int:
    """作業ツリーの差分を測って道を判定して出力する。"""
    pbi = Path(args.pbi_dir)
    change = change_from_diff(Path.cwd(), args.base, pbi)
    soft, small = soft_reasons(change, '', test_globs(pbi))
    print(render(decide([], soft, small, None)))
    return EXIT_OK


LIGHT_FILE = 'light.json'
BASE_REFS = ('origin/main', 'main')


def repo_root(pbi: Path) -> Path:
    """pbi または作業ディレクトリの git ルートを返す。git 外なら cwd。"""
    for candidate in (pbi, Path.cwd()):
        out = task_profile._git(candidate, 'rev-parse', '--show-toplevel')
        if out and out.strip():
            return Path(out.strip())
    return Path.cwd()


def merge_base(root: Path) -> str | None:
    """origin/main または main と HEAD の merge-base を返す。取れなければ None。"""
    for ref in BASE_REFS:
        try:
            out = heavy_check.git(root, 'merge-base', ref, 'HEAD').strip()
        except heavy_check.GitError:
            continue
        if out:
            return out
    return None


def pick_reviewer(builder_model: str) -> str:
    """作り手と別系統のレビュー用モデルラベルを返す。"""
    roles = conductor.load_roles(None, None)[0]
    reviewer = dict(roles['reviewer']['other'])
    if role_results.same_model(reviewer['model'], builder_model):
        alt = 'sonnet' if role_results.same_model(builder_model, 'opus') else 'opus'
        reviewer = {'runner': common.AGENT, 'model': alt, 'effort': reviewer.get('effort')}
    return conductor.model_label(reviewer)


def emit_task_profile(pbi: Path, goal_text: str, base: str | None, run_id: str) -> None:
    """ゴール文面から task.profiled を組み立て、軽い道の run_id・seq=0 で書く。"""
    if events is None:
        return
    tax = task_profile._load_taxonomy()
    target = task_profile.Target(scope='run', pbi='', text=goal_text, body_chars=len(goal_text), refs=None)
    measured = task_profile.measure(pbi, target, base or 'HEAD', tax)
    config = task_profile.embedding_config()
    anchors, vector = task_profile.anchors_for(target.text, tax, config)
    cid = task_profile.case_id(task_profile.repo_name(pbi), task_profile.work_key(pbi), target.scope, target.pbi)
    tags = {'task_type': None, 'domain': None, 'difficulty_hint': None}
    data = task_profile.build(target, tags, measured, anchors, tax, cid)
    events.emit(pbi, task_profile.EVENT_TYPE, data, run_id=run_id, seq=0)
    if vector is not None:
        task_profile.append_vector(pbi, task_profile._vector_row(config, cid, vector))


def parse_findings(text: str) -> dict[str, int]:
    """'c,m,n' を {'critical': c, 'major': m, 'minor': n} に変換する。"""
    parts = [p.strip() for p in text.split(',')]
    if len(parts) != 3:
        raise ValueError(f'invalid --findings {text!r}: expected c,m,n')
    try:
        values = [int(p) for p in parts]
    except ValueError as error:
        raise ValueError(f'invalid --findings {text!r}: expected integers') from error
    if any(v < 0 for v in values):
        raise ValueError(f'invalid --findings {text!r}: counts must be non-negative')
    return {'critical': values[0], 'major': values[1], 'minor': values[2]}


def elapsed_seconds(started_at: object) -> int | None:
    """ISO 時刻から現在までの経過秒 (0 以上の整数) を返す。読めなければ None。"""
    if not isinstance(started_at, str) or not started_at:
        return None
    try:
        start = datetime.datetime.fromisoformat(started_at)
        end = common.now()
        if start.tzinfo is None and end.tzinfo is not None:
            start = start.replace(tzinfo=end.tzinfo)
        elif end.tzinfo is None and start.tzinfo is not None:
            end = end.replace(tzinfo=start.tzinfo)
        return int(max(0.0, (end - start).total_seconds()))
    except (ValueError, OverflowError):
        return None


def measure_diff(root: Path, base: str | None, pbi: Path) -> tuple[int, int]:
    """light.json の base (無ければ HEAD) に対する変更ファイル数と変更行数を返す。"""
    if base:
        measured = heavy_check.measure(root, base, pbi)
    else:
        try:
            measured = heavy_check.measure(root, 'HEAD', pbi)
        except heavy_check.GitError:
            return 0, 0
    files = measured['files']
    return len(files), sum(item['added'] + item['deleted'] for item in files)


def cmd_record_start(args: argparse.Namespace) -> int:
    """軽い道の開始を記録し、見直しモデルとレビュー用スキルを出力する。"""
    goal_text = Path(args.goal_file).read_text(encoding='utf-8-sig') if args.goal_file else None
    pbi = Path(args.pbi_dir)
    pbi.mkdir(parents=True, exist_ok=True)
    root = repo_root(pbi)
    base = merge_base(root)
    run_id = str(uuid.uuid4())
    state = {
        'run_id': run_id,
        'started_at': common.now().isoformat(timespec='seconds'),
        'builder_model': args.builder_model,
        'base': base,
    }
    common.write_json(pbi / LIGHT_FILE, state)
    if events is not None:
        events.emit(pbi, 'run.started', {'workflow': 'light', 'launch': 'main', 'lane': 'light'},
                    run_id=run_id, seq=0)
    if goal_text is None:
        print('warn: no goal file, task.profiled not written')
    else:
        emit_task_profile(pbi, goal_text, base, run_id)
    print(f'reviewer: {pick_reviewer(args.builder_model)}')
    settings, _ = agents.load_adapters(common.resolve_home(None))
    _, decision, reason = adapter_gate.review_skill_row(settings['review'], root, os.environ)
    print(f'review-skill: {decision} ({reason})')
    return EXIT_OK


def cmd_record_end(args: argparse.Namespace) -> int:
    """light.json を読み、実際の差分の大きさとともに run.ended を記録する。"""
    pbi = Path(args.pbi_dir)
    light_path = pbi / LIGHT_FILE
    if not light_path.is_file():
        print(f'missing {light_path.as_posix()}', file=sys.stderr)
        return EXIT_USAGE
    state = common.load_json(light_path)
    run_id = state.get('run_id') if isinstance(state, dict) else None
    if not isinstance(run_id, str) or not run_id:
        print(f'invalid {light_path.as_posix()}: missing run_id', file=sys.stderr)
        return EXIT_USAGE
    try:
        findings = parse_findings(args.findings) if args.findings is not None else None
    except ValueError as error:
        print(error, file=sys.stderr)
        return EXIT_USAGE
    base = state.get('base') if isinstance(state.get('base'), str) and state.get('base') else None
    files, lines = measure_diff(repo_root(pbi), base, pbi)
    data: dict[str, object] = {'result': args.result, 'files': files, 'lines': lines}
    seconds = elapsed_seconds(state.get('started_at'))
    if seconds is not None:
        data['seconds'] = seconds
    if args.context_before is not None and args.context_after is not None:
        data['context_delta'] = args.context_after - args.context_before
    if findings is not None:
        data['findings'] = findings
    if events is not None:
        events.emit(pbi, 'run.ended', data, run_id=run_id, seq=0)
    parts = [f'result={args.result}', f'files={files}', f'lines={lines}']
    if 'context_delta' in data:
        parts.append(f"context_delta={data['context_delta']}")
    if args.findings is not None:
        parts.append(f'findings={args.findings}')
    print(f"run.ended: {' '.join(parts)}")
    return EXIT_OK


def cmd_record(args: argparse.Namespace) -> int:
    """record start / end サブコマンドを振り分ける。"""
    if args.record_command == 'start':
        return cmd_record_start(args)
    return cmd_record_end(args)


COMMANDS = {'decide': cmd_decide, 'check': cmd_check, 'record': cmd_record}


def common_parent() -> argparse.ArgumentParser:
    """全サブコマンド共通の --pbi-dir オプション定義を返す。"""
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument('--pbi-dir', default='docs/pbi', help='作業記録のディレクトリ (既定 docs/pbi)')
    return parent


def add_decide_parser(sub: argparse._SubParsersAction, parent: argparse.ArgumentParser) -> None:
    """既定の判定コマンド用サブパーサーを登録する。"""
    p = sub.add_parser('decide', parents=[parent], help='パス一覧から道を判定する')
    p.add_argument('--goal-file', required=True, help='ゴールのテキストファイル')
    p.add_argument('--paths', required=True, help='変更予定パスのファイル (1 行 1 パス)')
    p.add_argument('--new', help='新規追加パスのファイル (1 行 1 パス)')
    group = p.add_mutually_exclusive_group()
    group.add_argument('--light', dest='override', action='store_const', const='light',
                       help='大きさと sensitive の信号を上書きして light にする')
    group.add_argument('--full', dest='override', action='store_const', const='full',
                       help='強制的に atelier にする')
    p.set_defaults(override=None)


def add_check_parser(sub: argparse._SubParsersAction, parent: argparse.ArgumentParser) -> None:
    """check --diff サブコマンド用サブパーサーを登録する。"""
    p = sub.add_parser('check', parents=[parent], help='作業ツリーの差分から閾値を測る')
    p.add_argument('--diff', action='store_true', required=True, help='作業ツリーの差分を測る')
    p.add_argument('--base', default='origin/main', help='比較の基準 ref (既定 origin/main)')


def add_record_parser(sub: argparse._SubParsersAction, parent: argparse.ArgumentParser) -> None:
    """record start / end サブコマンド用サブパーサーを登録する。"""
    p = sub.add_parser('record', help='軽い道の記録 (start / end) を残す')
    rec_sub = p.add_subparsers(dest='record_command', required=True, metavar='{start,end}')
    start = rec_sub.add_parser('start', parents=[parent], help='軽い道の開始を記録し、見直し先を出す')
    start.add_argument('--builder-model', required=True, help='作り手のモデル名')
    start.add_argument('--goal-file', help='ゴールのテキストファイル')
    end = rec_sub.add_parser('end', parents=[parent], help='軽い道の終了を記録する')
    end.add_argument('--result', required=True, choices=('finished', 'escalated', 'aborted'), help='終了結果')
    end.add_argument('--findings', help='見直しの指摘件数 (c,m,n)')
    end.add_argument('--context-before', type=int, help='開始時の文脈トークン数')
    end.add_argument('--context-after', type=int, help='終了時の文脈トークン数')


def build_parser() -> argparse.ArgumentParser:
    """CLI パーサーを構築する。"""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='command', required=True, metavar='{check,record}')
    parent = common_parent()
    add_decide_parser(sub, parent)
    add_check_parser(sub, parent)
    add_record_parser(sub, parent)
    return ap


def normalize_argv(argv: list[str]) -> list[str]:
    """先頭引数がオプションなら既定の decide サブコマンドとして補う。"""
    if not argv or (argv[0].startswith('-') and argv[0] not in ('-h', '--help')):
        return ['decide', *argv]
    return list(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI のエントリポイント。"""
    common.utf8_stdout()
    raw = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(normalize_argv(raw))
    try:
        return COMMANDS[args.command](args)
    except (OSError, heavy_check.GitError) as error:
        print(error, file=sys.stderr)
        return EXIT_USAGE


if __name__ == '__main__':
    sys.exit(main())
