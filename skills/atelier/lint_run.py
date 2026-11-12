"""lint の下調べと見直しで jevlint を流す (appendix/lint.md)。外のモデルに送るのは、許可の記録があるときだけ。

  python lint_run.py files   <pbi-dir> --name NAME (--paths P,... | --base REF) [--new F,...] [--sensitive G,...]
  python lint_run.py approve <pbi-dir> --name NAME --answer send|ast-only
  python lint_run.py survey  <pbi-dir> --name NAME --mode ast-only|dry-run|model
  python lint_run.py recheck <pbi-dir> --name NAME --base REF --mode ast-only|dry-run|model

files: 見るファイルの一覧を <pbi-dir>/jevlint/NAME.files.txt に書く。--paths はその下の git が追っているファイル、
  --base はマージ基点 (git merge-base REF HEAD) から作業ツリーまでに変わったファイル (消したものを除く)。どちらにも --new
  (PBI が作った、git がまだ追っていないファイル) を足す。
  lint.json の extensions に合わないものは外す。そのうち外のモデルに送ってよいものを NAME.send.txt に書く。
  秘密のファイル (SECRET_PATTERNS) と --sensitive の glob (sensitive な PBI の対象。ワイルドカードの無い値はディレクトリとしても見る) は送らない。外した理由は
  NAME.excluded.txt に 1 行ずつ。
approve: NAME.send.txt の中身のハッシュと答えを <pbi-dir>/adapters.json の sends に足す (送る直前にユーザーに聞いた答え)。
survey: NAME.files.txt を jevlint audit にかけ、NAME.json と <pbi-dir>/notes/NAME.md (点・長い関数・弱い問い・
  bug-hunt の候補) を書く。
recheck: マージ基点を一時的な git worktree (スクラッチ) に出して「前」を、作業ツリーで「後」を audit にかけ、
  NAME.before.json・NAME.after.json と、lint_compare.py の表 NAME.md を書く。worktree は終わったら消す。

--mode:
  ast-only  --ast-only で流す。ネットワークを使わない
  dry-run   送るファイル (NAME.send.txt) の数・トークン・費用・送り先を出し、NAME.dry-run.json に残す。何も送らない
  model     NAME.send.txt をモデルで判定し、残り (送らないファイル) は --ast-only で判定して JSON を合わせる。
            adapters.json の sends に、同じ NAME・同じハッシュ・answer send で、まだ使っていない記録が無ければ
            断る (終了コード 3)。使ったら used を真にする (次に送るときは、また聞く)

jevlint の場所は agents.find_cli (home の agents.json の tools.jevlint.path、PATH、fnm・nvm の Node の版)。
流すときはそのディレクトリを PATH の先頭に置く (agents.cli_env)。シェルの PATH の Node の版に左右されない。
.py のパスなら、いまの Python で動かす。jevlint fix・pr・commits・baseline は使わない。
終了コード: 0 = 正常、2 = 引数・入力の誤り、jevlint が見つからない・失敗した、3 = 送る許可の記録が無い。
"""
import argparse
import datetime
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import agents as agents_module
import common
import lint_compare

# イベントの口 (同じフォルダ)。無い環境でも承認の記録は続ける
try:
    import events
except (Exception, SystemExit):
    events = None

# Windows で子のコンソールのウインドウを出さない (ヘッドレス)。ほかの OS では 0
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

DEFAULT_EXTENSIONS = ('.ts', '.tsx', '.mts', '.cts', '.js', '.jsx', '.mjs', '.cjs', '.py')
# 許可があっても外のモデルに送らないファイル (名前で見る)
SECRET_PATTERNS = ('.env', '.env.*', '*.pem', '*.key', '*.p12', '*.pfx', 'id_rsa*', 'credentials*', 'secrets*',
                   '*.secret', '*.secrets.*')
MODES = ('ast-only', 'dry-run', 'model')
# 1 回の jevlint に渡すファイルの数 (Windows のコマンドラインの長さに収める)
CHUNK = 150
# jevlint audit --dry-run の要約の行。費用が分からないと「(about $…)」が無く、後ろに
# 「N files pinned, blank or cached, and not counted.」が続くことがある。送り先が無いと DRY_RUN_LOCAL の形になる
DRY_RUN_LINE = re.compile(r'(\d+) files?, about ([\d,]+) tokens(?: \(about (?:under )?\$([\d.]+)\))?, '
                          r'would be sent to (\S+?)\.(?:\s|$)')
DRY_RUN_LOCAL = re.compile(r'(\d+) files? would be analysed locally, and nothing would be sent')
ANSI = re.compile(r'\x1b\[[0-9;]*m')


class Refused(Exception):
    """送る許可の記録が無い。"""


def out_dir(pbi: Path) -> Path:
    return pbi / 'jevlint'


def git(root: Path, *args: str) -> str:
    done = subprocess.run(['git', *args], cwd=root, capture_output=True, text=True, encoding='utf-8',
                          creationflags=NO_WINDOW)
    if done.returncode != 0:
        raise RuntimeError(f'git {" ".join(args)}: {done.stderr.strip()}')
    return done.stdout


def repo_root(start: Path) -> Path:
    return Path(git(start, 'rev-parse', '--show-toplevel').strip())


def lint_config(pbi: Path) -> dict:
    data = common.load_json(pbi / 'lint.json', {})
    return data if isinstance(data, dict) else {}


def extensions(config: dict) -> tuple:
    found = config.get('extensions')
    if isinstance(found, list) and all(isinstance(x, str) for x in found) and found:
        return tuple(found)
    return DEFAULT_EXTENSIONS


def split(value: str | None) -> list[str]:
    return [x.strip().replace('\\', '/') for x in (value or '').split(',') if x.strip()]


def changed_files(root: Path, base: str) -> list[str]:
    merge_base = git(root, 'merge-base', base, 'HEAD').strip()
    listed = git(root, 'diff', '--name-only', '--diff-filter=d', merge_base, '--').splitlines()
    return [p.strip() for p in listed if p.strip()]


def tracked_under(root: Path, paths: list[str]) -> list[str]:
    return [p.strip() for p in git(root, 'ls-files', '--', *paths).splitlines() if p.strip()]


def secret(path: str) -> bool:
    name = path.rsplit('/', 1)[-1]
    return any(fnmatch.fnmatch(name, pattern) for pattern in SECRET_PATTERNS)


def sensitive_match(path: str, glob: str) -> bool:
    """glob に合うか。ワイルドカードの無い値はディレクトリの前置きとしても見る (src/auth は src/auth/login.ts に合う)。"""
    if fnmatch.fnmatch(path, glob):
        return True
    if any(ch in glob for ch in '*?['):
        return False
    prefix = glob.rstrip('/')
    return path == prefix or path.startswith(prefix + '/')


def select(paths: list[str], config: dict, sensitive: list[str], root: Path) -> tuple[list, list, list]:
    """(見るファイル, 送ってよいファイル, (外したファイル, 理由))。"""
    seen, files, send, excluded = set(), [], [], []
    allowed = extensions(config)
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        if not (root / path).is_file():
            excluded.append((path, 'missing'))
            continue
        if secret(path):
            excluded.append((path, 'secret'))
            continue
        if not path.endswith(allowed):
            excluded.append((path, 'extension'))
            continue
        files.append(path)
        if any(sensitive_match(path, glob) for glob in sensitive):
            excluded.append((path, 'sensitive (ast only)'))
            continue
        send.append(path)
    return files, send, excluded


def read_list(path: Path) -> list[str]:
    if not path.is_file():
        raise ValueError(f'{common.shown(path)} is missing (run lint_run.py files first)')
    return [line.strip() for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def write_list(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(f'{line}\n' for line in lines), encoding='utf-8')


def list_hash(lines: list[str]) -> str:
    return hashlib.sha256('\n'.join(lines).encode('utf-8')).hexdigest()


def jevlint_command(home: Path) -> list[str]:
    agents = common.load_json(home / 'agents.json', {})
    found = agents_module.find_cli('jevlint', agents if isinstance(agents, dict) else {}, os.environ)
    if not found:
        raise RuntimeError('jevlint not found (install @indxjp/jevlint, or set tools.jevlint.path in agents.json)')
    return [sys.executable, found] if found.endswith('.py') else [found]


def run_jevlint(command: list[str], cwd: Path, args: list[str]) -> str:
    # 見つけた jevlint のディレクトリを PATH の先頭に置き、その版の Node で動かす (agents.cli_env)
    env = dict(agents_module.cli_env(command[-1], os.environ), NO_COLOR='1', FORCE_COLOR='0')
    done = subprocess.run([*command, *args], cwd=cwd, capture_output=True, text=True, encoding='utf-8', env=env,
                          creationflags=NO_WINDOW)
    if done.returncode != 0:
        raise RuntimeError(f'jevlint {args[0]} failed ({done.returncode}): {(done.stderr or done.stdout).strip()[-400:]}')
    return ANSI.sub('', done.stdout)


def chunks(items: list[str]) -> list[list[str]]:
    return [items[i:i + CHUNK] for i in range(0, len(items), CHUNK)]


def audit(command: list[str], cwd: Path, files: list[str], ast_only: bool, preset: str | None) -> dict:
    """files を audit --json にかけ、files を合わせた 1 つの報告を返す。"""
    merged = {'files': [], 'usage': {'inputTokens': 0, 'estimatedCostUsd': 0.0, 'modelCalls': 0, 'cacheHits': 0}}
    for part in chunks(files):
        args = ['audit', '--json', *(['--ast-only'] if ast_only else []), *([f'--preset={preset}'] if preset else []),
                *part]
        report = json.loads(run_jevlint(command, cwd, args))
        for key, value in report.items():
            if key == 'files':
                merged['files'] += value
            elif key == 'usage' and isinstance(value, dict):
                for k, v in value.items():
                    if isinstance(v, (int, float)):
                        merged['usage'][k] = merged['usage'].get(k, 0) + v
            else:
                merged.setdefault(key, value)
    return merged


def dry_run_summary(text: str) -> tuple:
    """(要約の行の合致, 送り先が無いか)。下から探す。見つからなければ (None, False)。"""
    for line in reversed(text.splitlines()):
        if match := DRY_RUN_LINE.search(line.strip()):
            return match, False
        if match := DRY_RUN_LOCAL.search(line.strip()):
            return match, True
    return None, False


def dry_run(command: list[str], cwd: Path, files: list[str], preset: str | None) -> dict:
    total = {'files': 0, 'tokens': 0, 'cost_usd': 0.0, 'provider': None}
    for part in chunks(files):
        text = run_jevlint(command, cwd, ['audit', '--dry-run', *([f'--preset={preset}'] if preset else []), *part])
        match, local = dry_run_summary(text)
        if match is None:
            raise RuntimeError('jevlint audit --dry-run printed no summary line')
        total['files'] += int(match.group(1))
        if local:
            continue
        total['tokens'] += int(match.group(2).replace(',', ''))
        total['cost_usd'] += float(match.group(3) or 0)
        total['provider'] = match.group(4)
    return total


def judge(command, cwd, files, send, mode, preset) -> dict:
    """mode に合わせて files を判定する。model は send をモデルで、残りを AST だけで。"""
    if mode == 'ast-only':
        return audit(command, cwd, files, True, preset)
    rest = [f for f in files if f not in set(send)]
    report = audit(command, cwd, send, False, preset) if send else {'files': [], 'usage': {}}
    if rest:
        report['files'] += audit(command, cwd, rest, True, preset)['files']
    return report


def take_approval(pbi: Path, name: str, send: list[str]) -> None:
    """まだ使っていない send の許可の記録を使ったことにする。無ければ Refused。"""
    path = pbi / 'adapters.json'
    data = common.load_json(path, {})
    sends = data.get('sends') if isinstance(data, dict) else None
    digest = list_hash(send)
    for record in sends or []:
        if (isinstance(record, dict) and record.get('name') == name and record.get('files_sha256') == digest
                and record.get('answer') == 'send' and not record.get('used')):
            record['used'] = True
            common.write_json(path, data)
            return
    raise Refused(f'no unused send approval for {name} with this file list (ask the user, then run approve)')


def cmd_files(args, pbi: Path, root: Path) -> int:
    config = lint_config(pbi)
    if args.paths:
        paths = tracked_under(root, split(args.paths)) + split(args.new)
    else:
        paths = changed_files(root, args.base) + split(args.new)
    files, send, excluded = select(paths, config, split(args.sensitive), root)
    base = out_dir(pbi)
    write_list(base / f'{args.name}.files.txt', files)
    write_list(base / f'{args.name}.send.txt', send)
    write_list(base / f'{args.name}.excluded.txt', [f'{path}\t{reason}' for path, reason in excluded])
    print(f'files {len(files)} send {len(send)} excluded {len(excluded)} -> '
          f'{common.shown(base / (args.name + ".files.txt"))}')
    return 0


def cmd_approve(args, pbi: Path, _root: Path) -> int:
    send = read_list(out_dir(pbi) / f'{args.name}.send.txt')
    path = pbi / 'adapters.json'
    data = common.load_json(path, {})
    if not isinstance(data, dict):
        data = {}
    record = {'name': args.name, 'files_sha256': list_hash(send), 'count': len(send), 'answer': args.answer,
              'at': datetime.datetime.now().astimezone().isoformat(timespec='seconds')}
    data.setdefault('sends', []).append(record)
    common.write_json(path, data)
    # 送る直前にユーザーが選んだ答え (send / ast-only) を残す
    if events is not None:
        events.emit(pbi, 'stage.decided', {'component': 'lint-survey.jevlint', 'decision': args.answer,
                                           'by': 'user', 'reason': 'approved'})
    print(f'approval {args.name} {args.answer} ({len(send)} files) -> {common.shown(path)}')
    return 0


def keep_dry_run(pbi: Path, name: str, totals: dict) -> None:
    """完了報告が読む dry-run の数字 (側ごと) を NAME.dry-run.json に書く。"""
    common.write_json(out_dir(pbi) / f'{name}.dry-run.json', totals)


def print_dry_run(label: str, total: dict) -> None:
    print(f'dry-run {label}: {total["files"]} files, about {total["tokens"]:,} tokens '
          f'(about ${total["cost_usd"]:.4f}), provider {total["provider"]}')


def cmd_survey(args, pbi: Path, root: Path, command: list[str]) -> int:
    base, config = out_dir(pbi), lint_config(pbi)
    files = read_list(base / f'{args.name}.files.txt')
    send = read_list(base / f'{args.name}.send.txt')
    preset = config.get('preset') if isinstance(config.get('preset'), str) else None
    if args.mode == 'dry-run':
        total = dry_run(command, root, send, preset) if send else empty_total()
        keep_dry_run(pbi, args.name, {'survey': total})
        print_dry_run(args.name, total)
        return 0
    if args.mode == 'model':
        take_approval(pbi, args.name, send)
    report = judge(command, root, files, send, args.mode, preset)
    common.write_json(base / f'{args.name}.json', report)
    note = pbi / 'notes' / f'{args.name}.md'
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(survey_note(report, lint_compare.load_config(str(pbi / 'lint.json')), args.mode), encoding='utf-8')
    print(f'survey {args.name} ({args.mode}): {len(report["files"])} files -> {common.shown(note)}')
    return 0


def empty_total() -> dict:
    return {'files': 0, 'tokens': 0, 'cost_usd': 0.0, 'provider': None}


def survey_note(report: dict, config: dict, mode: str) -> str:
    files = sorted(report['files'], key=lambda f: (f.get('score', 100), f.get('path', '')))
    lines = [f'# jevlint の下調べ ({mode})', '', '## 点の低い順', '', '| file | score |', '|---|---|']
    lines += [f'| {f["path"]} | {f.get("score")} |' for f in files[:30]]
    lines += ['', f'## {config["function_lines"]} 行以上の関数', '']
    long = [(f['path'], x) for f in files for x in (f.get('ast') or {}).get('findings') or []
            if x.get('rule') == lint_compare.FUNCTION_LINES and x.get('value', 0) >= config['function_lines']]
    lines += [f'- {path}:{x.get("line")} {x.get("value")} 行' for path, x in long] or ['- なし']
    weak, bugs = {}, []
    for f in files:
        for qid, (value, fix) in (lint_compare.answers(f) or {}).items():
            if qid in config['exclude_questions'] or value >= config['compliance']:
                continue
            if qid.startswith(lint_compare.BUG_HUNT):
                bugs.append(f'- {f["path"]} `{qid}` {round(value, 2)}: {fix}')
            else:
                weak.setdefault(qid, []).append(f['path'])
    lines += ['', '## 弱い問い (ファイルの数)', '']
    lines += [f'- `{qid}` {len(paths)}: {", ".join(paths[:5])}' for qid, paths in
              sorted(weak.items(), key=lambda kv: -len(kv[1]))] or ['- なし (AST だけのときは問いを見ない)']
    lines += ['', '## bug-hunt の候補 (設計者がファイルを開いて確かめる)', ''] + (bugs or ['- なし'])
    return '\n'.join(lines + [''])


def cmd_recheck(args, pbi: Path, root: Path, command: list[str]) -> int:
    base, config = out_dir(pbi), lint_config(pbi)
    files = read_list(base / f'{args.name}.files.txt')
    send = read_list(base / f'{args.name}.send.txt')
    preset = config.get('preset') if isinstance(config.get('preset'), str) else None
    merge_base = git(root, 'merge-base', args.base, 'HEAD').strip()
    scratch = Path(tempfile.mkdtemp(prefix='atelier-lint-'))
    tree = scratch / 'before'
    try:
        git(root, 'worktree', 'add', '--detach', str(tree), merge_base)
        # 許可は送る直前に使う (worktree を作れずに止まったときは使わずに残す)
        if args.mode == 'model':
            take_approval(pbi, args.name, send)
        old = [f for f in files if (tree / f).is_file()]
        old_send = [f for f in send if f in set(old)]
        if args.mode == 'dry-run':
            before = dry_run(command, tree, old_send, preset) if old_send else empty_total()
            after = dry_run(command, root, send, preset) if send else empty_total()
            keep_dry_run(pbi, args.name, {'before': before, 'after': after})
            print_dry_run(f'{args.name} before', before)
            print_dry_run(f'{args.name} after', after)
            return 0
        before = judge(command, tree, old, old_send, args.mode, preset) if old else {'files': []}
    finally:
        subprocess.run(['git', 'worktree', 'remove', '--force', str(tree)], cwd=root, capture_output=True,
                       creationflags=NO_WINDOW)
        subprocess.run(['git', 'worktree', 'prune'], cwd=root, capture_output=True, creationflags=NO_WINDOW)
        shutil.rmtree(scratch, ignore_errors=True)
    after = judge(command, root, files, send, args.mode, preset)
    before_path, after_path = base / f'{args.name}.before.json', base / f'{args.name}.after.json'
    common.write_json(before_path, before)
    common.write_json(after_path, after)
    return lint_compare.main([str(before_path), str(after_path), '--config', str(pbi / 'lint.json'),
                              '--out', str(base / f'{args.name}.md')])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='command', required=True)
    for name in ('files', 'approve', 'survey', 'recheck'):
        p = sub.add_parser(name)
        p.add_argument('pbi_dir')
        p.add_argument('--name', required=True)
        p.add_argument('--home', help='agents.json を置く home (common.resolve_home)')
        if name == 'files':
            p.add_argument('--paths', help='見るディレクトリかファイル (カンマ区切り)')
            p.add_argument('--base', help='マージ先のブランチ (変わったファイルを見る)')
            p.add_argument('--new', help='PBI が作った新しいファイル (カンマ区切り)')
            p.add_argument('--sensitive', help='送らないファイルの glob (カンマ区切り)')
        if name == 'approve':
            p.add_argument('--answer', required=True, choices=('send', 'ast-only'))
        if name in ('survey', 'recheck'):
            p.add_argument('--mode', required=True, choices=MODES)
        if name == 'recheck':
            p.add_argument('--base', required=True)
    args = ap.parse_args(argv)
    common.utf8_stdout()
    pbi = Path(args.pbi_dir)
    if not pbi.is_dir():
        print(f'{args.pbi_dir}: not a directory', file=sys.stderr)
        return 2
    if args.command == 'files' and bool(args.paths) == bool(args.base):
        print('files needs either --paths or --base', file=sys.stderr)
        return 2
    try:
        root = repo_root(pbi.resolve())
        if args.command == 'files':
            return cmd_files(args, pbi, root)
        if args.command == 'approve':
            return cmd_approve(args, pbi, root)
        command = jevlint_command(common.resolve_home(args.home))
        if args.command == 'survey':
            return cmd_survey(args, pbi, root, command)
        return cmd_recheck(args, pbi, root, command)
    except Refused as error:
        print(f'refused {error}', file=sys.stderr)
        return 3
    except (RuntimeError, ValueError, OSError) as error:
        print(f'error {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
