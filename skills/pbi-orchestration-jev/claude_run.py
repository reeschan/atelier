"""実行先 claude（Claude Code の headless）で PBI を 1 件動かす。runners.md の「claude」の節から呼ぶ。

  python <このディレクトリ>/claude_run.py <報告名> --model <model> [--effort <effort>]
      [--setting default|edit-only] [--pbi-dir docs/pbi]

- 依頼文は <pbi-dir>/claude/<報告名>.prompt.md（先に書いておく）。リポジトリのルートで実行する（そこが作業ルートになる）。
  依頼文は標準入力で「依頼文のファイルを読み、その指示どおりに作業してください」の 1 文と一緒に渡す。
- 起動: claude -p --output-format json --model <model> [--effort <effort>] --tools <設定の道具>
  --permission-mode acceptEdits --permission-prompts none --strict-mcp-config（edit-only では --restricted も付く）。
  --add-dir は付けない（書き込みは作業ルートの中だけ）。MCP の道具は使わない。
- 設定（SETTINGS）: default は pbi-worker と同じ道具のうち headless で使えるもの、
  edit-only は Read・Edit・Write・Glob・Grep だけ（コマンドの道具を持たない）。
- 出力: <報告名>.json（--output-format json の結果）・<報告名>.err（診断）・
  <報告名>.run.json（開始・終了の時刻・終了コード・使った設定。record_run.py が読む）。
  5 行の報告 <報告名>.last.md は実装担当が書く（依頼文の末尾の「要約の書き先」）。
- 認証は Claude Code のログインのまま。子の環境から API キー・認証トークン・接続先の環境変数（STRIPPED_ENV）を外す。
- 終了コードは claude のもの（0 = 完了）。引数・環境の不備は 2。

claude の場所: 環境変数 PBI_CLAUDE_BIN、無ければ PATH の `claude`。見つからなければ終了コード 2。
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


# 設定ごとの道具（--tools に渡す）。default は agents/pbi-worker.md の tools から、headless で使えないもの
# （MCP の道具と ToolSearch）を除いたもの。PowerShell の道具は Windows だけにある
def default_tools(os_name: str) -> tuple:
    """default の設定の道具。PowerShell の道具は Windows (os.name が 'nt') だけにある。"""
    return ('Read', 'Write', 'Edit', 'Glob', 'Grep', 'Bash') + (('PowerShell',) if os_name == 'nt' else ())


SETTINGS = {
    'default': default_tools(os.name),
    'edit-only': ('Read', 'Edit', 'Write', 'Glob', 'Grep'),
}

# 子の環境から外す変数（API 課金・外部の接続先に切り替わらないよう、ログインの認証だけにする）
STRIPPED_ENV = ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL',
                'ANTHROPIC_BEDROCK_BASE_URL', 'CLAUDE_CODE_USE_BEDROCK')

# コマンドラインに渡す値。使える文字を限る（model は opus[1m] のような別名も通す）
NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._\[\]-]{0,127}$')
REPORT = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')


def now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


def fail(msg: str) -> None:
    print(f'claude_run: {msg}', file=sys.stderr)
    sys.exit(2)


def build_args(model: str, effort: str | None, setting: str) -> list[str]:
    """claude に渡す引数（先頭の実行ファイルを除く）。依頼は標準入力で渡すので、ここには含めない。"""
    args = ['-p', '--output-format', 'json', '--model', model]
    if effort is not None:
        args += ['--effort', effort]
    args += ['--tools', ','.join(SETTINGS[setting]),
             '--permission-mode', 'acceptEdits', '--permission-prompts', 'none', '--strict-mcp-config']
    if setting == 'edit-only':
        args.append('--restricted')
    return args


def build_env() -> dict:
    return {k: v for k, v in os.environ.items() if k not in STRIPPED_ENV}


def claude_command() -> list[str]:
    """claude を起動するコマンドの先頭。PBI_CLAUDE_BIN があればそれ、無ければ PATH の claude。"""
    configured = os.environ.get('PBI_CLAUDE_BIN')
    if configured:
        if not Path(configured).is_file():
            fail(f'claude が見つかりません: {configured}（PBI_CLAUDE_BIN で指定）')
        return [configured]
    found = shutil.which('claude')
    if not found:
        fail('claude が見つかりません（Claude Code を入れて PATH に通すか、PBI_CLAUDE_BIN で指定）')
    return [found]


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('report')
    ap.add_argument('--model', required=True)
    ap.add_argument('--effort', default=None)
    ap.add_argument('--setting', default='default', choices=sorted(SETTINGS))
    ap.add_argument('--pbi-dir', default='docs/pbi')
    args = ap.parse_args()

    if not REPORT.match(args.report):
        fail(f'報告名に使えない文字があります: {args.report!r}')
    if not NAME.match(args.model):
        fail(f'--model に使えない文字があります: {args.model!r}')
    effort = None if args.effort in (None, '', 'null', 'none') else args.effort
    if effort is not None and not NAME.match(effort):
        fail(f'--effort に使えない文字があります: {effort!r}')

    base = Path(args.pbi_dir) / 'claude'
    prompt = base / f'{args.report}.prompt.md'
    if not prompt.is_file():
        fail(f'依頼文がありません: {prompt}')
    launcher = claude_command()

    task = f'{prompt.as_posix()} を読み、その指示どおりに作業してください。'
    out_json = base / f'{args.report}.json'
    err = base / f'{args.report}.err'
    start = now()
    with open(out_json, 'wb') as out, open(err, 'wb') as errf:
        code = subprocess.run([*launcher, *build_args(args.model, effort, args.setting)],
                              input=task.encode('utf-8'), stdout=out, stderr=errf, env=build_env()).returncode
    run = {'start': start, 'end': now(), 'exit': code, 'model': args.model, 'effort': effort,
           'setting': args.setting}
    (base / f'{args.report}.run.json').write_text(json.dumps(run, ensure_ascii=False) + '\n', encoding='utf-8')
    print(f'claude_run: exit={code} model={args.model} effort={effort} setting={args.setting} '
          f'json={out_json.as_posix()}')
    sys.exit(code)


if __name__ == '__main__':
    main()
