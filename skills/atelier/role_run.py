"""レビュー担当・テスト担当を、agent 以外の実行先で 1 回動かす。runners.md の「Roles on a runner」の節から呼ぶ。

  python <このディレクトリ>/role_run.py <名前> --role reviewer|tester --runner codex|claude|dsh|opencode|agy
      --model <model> [--effort <effort>] [--sandbox workspace-write|danger-full-access] [--pbi-dir docs/pbi]

- 依頼文は設計者が <pbi-dir>/roles/<名前>.prompt.md に書いておく (prompts/pbi-review.md・prompts/tester.md の prompt の部分)。
  先頭に「agents/atelier-<役割>.md を読んで従う」の行が無ければ足して書き直す (外部の実行先には定義を読む仕組みが無い)。
- 名前はレビューなら <報告名>.review、テストなら test-<結果の名前>。
- 実行先ごとの起動:
    codex:    codex -a never exec --json -s <--sandbox、既定 danger-full-access> (shared-memory の承認は自動) を標準入力の依頼で。
              ログは <名前>.jsonl・.err・.last.md (-o)
    claude:   claude_run.py <名前> --folder roles (claude -p。MCP なし)。ログは <名前>.json・.err・.run.json
    dsh:      dsh_run.py <名前> --folder roles。ログは <名前>.jsonl・.err・.final.md・.run.json
    opencode: opencode run -m <model>[#<effort>] --format json (OPENCODE_CONFIG はスキルの opencode.config.json)
    agy:      agy -p ... --mode accept-edits --settings <スキルの agy.settings.json> --output-format json
  CLI は runner_cli.find_command (ATELIER_<RUNNER>_CMD・agents.json の path・PATH) で探す。
- 合否は役割が書く結果の JSON (reviews/<報告名>.review.json・verify/tests/<名前>.test-result.json) が決める。
  ここが返すのは子の終了コード。引数・依頼文・CLI の不備は 2。
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import runner_cli

NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
SKILL = Path(__file__).resolve().parent
ROLES = ('reviewer', 'tester')
RUNNERS = tuple(r for r in runner_cli.ROLE_RUNNERS if r != 'agent')
NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
MODEL = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/:\[\]-]{0,127}$')
EFFORT = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$')
# 役割はソースを変えず結果の JSON だけを書くが、verify のコマンド (pnpm・ユーザーの Node) を動かすので
# サンドボックスでは塞がれる (workspace-write は fnm・uv の下の実行ファイルとネットワークを通さない)。
# 既定は開けておき、verify が中で動くリポジトリでは --sandbox で絞れる
CODEX_SANDBOX = runner_cli.CODEX_DEFAULT_SANDBOX
ROLE_SANDBOXES = tuple(s for s in runner_cli.CODEX_SANDBOXES if s != 'read-only')


def now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


def fail(msg: str) -> None:
    print(f'role_run: {msg}', file=sys.stderr)
    sys.exit(2)


def definition_line(role: str) -> str:
    return f'Read {(SKILL / "agents" / f"atelier-{role}.md").as_posix()} and follow it as your instructions.'


def with_definition(prompt: Path, role: str) -> str:
    """依頼文の先頭に定義を読む行を置く (既にあれば書き直さない)。依頼文の本文を返す。"""
    text = prompt.read_text(encoding='utf-8', errors='replace')
    line = definition_line(role)
    if not text.startswith(line):
        text = line + '\n' + text
        prompt.write_text(text, encoding='utf-8', newline='\n')
    return text


def command_or_fail(runner: str) -> list:
    words, reason = runner_cli.find_command(runner)
    if words is None or reason:
        fail(reason)
    return words


def run_script(script: str, args) -> int:
    """claude_run.py・dsh_run.py に roles の置き場で任せる。"""
    argv = [sys.executable, str(SKILL / script), args.name, '--model', args.model, '--pbi-dir', args.pbi_dir,
            '--folder', 'roles']
    if args.effort:
        argv += ['--effort', args.effort]
    return subprocess.run(argv, creationflags=NO_WINDOW, check=False).returncode


def run_codex(args, base: Path, text: str) -> int:
    argv = [*command_or_fail('codex'),
            *runner_cli.codex_args(args.model, args.effort, args.sandbox, base / f'{args.name}.last.md')]
    with open(base / f'{args.name}.jsonl', 'wb') as out, open(base / f'{args.name}.err', 'wb') as err:
        task = f'Writing files: {runner_cli.CODEX_TEXT_RULE}\n' + text
        return subprocess.run(argv, input=task.encode('utf-8'), stdout=out, stderr=err,
                              creationflags=NO_WINDOW, check=False).returncode


def run_opencode(args, base: Path, prompt: Path) -> int:
    words = command_or_fail('opencode')
    model = args.model + (f'#{args.effort}' if args.effort else '')
    argv = [*words, 'run', '-m', model, '--format', 'json', f'Read {prompt.as_posix()} and follow it.']
    env = dict(os.environ, OPENCODE_CONFIG=str(SKILL / 'opencode.config.json'))
    # fnm・nvm の Node に入った opencode は、自分のフォルダが PATH の先頭に無いと node を見つけられない
    env['PATH'] = str(Path(words[0]).parent) + os.pathsep + env.get('PATH', '')
    with open(base / f'{args.name}.jsonl', 'wb') as out, open(base / f'{args.name}.err', 'wb') as err:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env,
                              creationflags=NO_WINDOW, check=False).returncode


def run_agy(args, base: Path, prompt: Path) -> int:
    argv = [*command_or_fail('agy'), '-p', f'Read {prompt.as_posix()} and follow it.', '--model', args.model,
            '--mode', 'accept-edits', '--settings', str(SKILL / 'agy.settings.json'),
            '--output-format', 'json', '--print-timeout', '3600s']
    with open(base / f'{args.name}.json', 'wb') as out, open(base / f'{args.name}.err', 'wb') as err:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                              creationflags=NO_WINDOW, check=False).returncode


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('name')
    ap.add_argument('--role', required=True, choices=ROLES)
    ap.add_argument('--runner', required=True)
    ap.add_argument('--model', required=True)
    ap.add_argument('--effort', default=None)
    # read-only は除く: 役割は結果の JSON を書くので、書けないサンドボックスでは終わらない
    ap.add_argument('--sandbox', default=CODEX_SANDBOX, choices=ROLE_SANDBOXES,
                    help='codex のサンドボックス (既定 danger-full-access)。ほかの実行先では使わない')
    ap.add_argument('--pbi-dir', default='docs/pbi')
    args = ap.parse_args()

    if args.runner == 'agent':
        fail('agent の役割は Agent ツールで起こす。Agent ツールが無ければ --runner claude で起こす')
    if args.runner not in RUNNERS:
        fail(f"--runner は {', '.join(RUNNERS)} のどれか: {args.runner!r}")
    if not NAME.match(args.name):
        fail(f'名前に使えない文字があります: {args.name!r}')
    if not MODEL.match(args.model):
        fail(f'--model に使えない文字があります: {args.model!r}')
    args.effort = None if args.effort in (None, '', 'null', 'none') else args.effort
    if args.effort is not None and not EFFORT.match(args.effort):
        fail(f'--effort に使えない文字があります: {args.effort!r}')

    base = Path(args.pbi_dir) / 'roles'
    prompt = base / f'{args.name}.prompt.md'
    if not prompt.is_file():
        fail(f'依頼文がありません: {prompt.as_posix()}')
    text = with_definition(prompt, args.role)

    start = now()
    if args.runner == 'claude':
        code = run_script('claude_run.py', args)
    elif args.runner == 'dsh':
        code = run_script('dsh_run.py', args)
    elif args.runner == 'codex':
        code = run_codex(args, base, text)
    elif args.runner == 'opencode':
        code = run_opencode(args, base, prompt)
    else:
        code = run_agy(args, base, prompt)
    if args.runner not in ('claude', 'dsh'):
        # claude_run.py・dsh_run.py は自分で .run.json を書く
        run = {'start': start, 'end': now(), 'exit': code, 'role': args.role, 'runner': args.runner,
               'model': args.model, 'effort': args.effort}
        (base / f'{args.name}.run.json').write_text(json.dumps(run, ensure_ascii=False) + '\n', encoding='utf-8')
    print(f'role_run: exit={code} role={args.role} runner={args.runner} model={args.model} effort={args.effort} '
          f'logs={base.as_posix()}/{args.name}.*')
    sys.exit(code)


if __name__ == '__main__':
    main()
