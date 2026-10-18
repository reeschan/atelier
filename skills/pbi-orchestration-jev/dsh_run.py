"""実行先 dsh（DeepSeek Harness の headless）で PBI を 1 件動かす。runners.md の「dsh」の節から呼ぶ。

  python <このディレクトリ>/dsh_run.py <報告名> --model <provider>/<model> [--effort <effort>] [--pbi-dir docs/pbi]

- 依頼文は <pbi-dir>/dsh/<報告名>.prompt.md（先に書いておく）。リポジトリのルートで実行する（そこが dsh の作業ルートになる）。
- モデルと effort は、実行ごとの上書き設定 <pbi-dir>/dsh/<報告名>.patch.yml に書いて `--patch` で渡す
  （agent-default-model の provider・model・reasoningEffort）。DSH_HOME の共通の設定より後に当たる。
- 出力: <報告名>.jsonl（`--json` のイベント）・<報告名>.err（dsh の診断）・<報告名>.final.md（final イベントの本文）・
  <報告名>.run.json（開始・終了の時刻と終了コード。record_run.py が読む）。
  4 行の報告 <報告名>.last.md は実装担当が書く（依頼文の末尾の「要約の書き先」）。
- API キーは環境変数を優先し、無ければ下の KEY_FILES のファイルから読む。値は表示もファイルへの書き出しもしない。
- 権限は DSH_PERMISSION_MODE=workspace-write（書き込みは作業ルートの中だけ。headless では昇格の確認に答える人がいないので拒否される）。
- 終了コードは dsh のもの（0 = 完了、1 = 中断・エラー）。引数・環境の不備は 2。

dsh の場所: 既定は PATH の `dsh`（`npm i -g @deepseek-ai/dsh`）。PATH に無いときは環境変数で渡す:
  PBI_DSH_BIN（@deepseek-ai/dsh/lib/bin.js）と PBI_DSH_NODE（node。省略時は PATH の node）。
  DSH_HOME は渡されたものをそのまま使う（省略時は dsh の既定）。
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


# provider ごとのキー: (環境変数, 環境変数が無いときに読むファイル)。ファイルはキーだけを書いたもの。
KEY_FILES = {
    'deepseek-official': ('DEEPSEEK_API_KEY', '~/.config/deepseek/deepseek.key'),
    'opencode-go': ('OPENCODE_GO_API_KEY', '~/.config/opencode/opencode-go.key'),
}
# ローカルの llama.cpp などはキーが要らないが、dsh が値を求めるので固定の値を渡す
LOCAL_KEY_ENV = 'QWEN_LOCAL_API_KEY'

# YAML に書き込む値。!!js や改行が入らないよう、使える文字を限る
NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
REPORT = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')


def now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


def fail(msg: str) -> None:
    print(f'dsh_run: {msg}', file=sys.stderr)
    sys.exit(2)


def parse_model(value: str) -> tuple[str, str]:
    provider, sep, model = value.partition('/')
    if not sep or not NAME.match(provider) or not NAME.match(model):
        fail(f'--model は <provider>/<model> の形（英数字と . _ - だけ）: {value!r}')
    return provider, model


def patch_yaml(provider: str, model: str, effort: str | None) -> str:
    lines = [
        '# dsh_run.py が実行ごとに書く上書き設定 (手で直さない)',
        '- id: agent-default-model',
        '  config:',
        f'    provider: {provider}',
        f'    model: {model}',
    ]
    if effort is not None:
        lines.append(f'    reasoningEffort: {effort}')
    return '\n'.join(lines) + '\n'


def read_key(path: str) -> str | None:
    p = Path(os.path.expanduser(path))
    if not p.is_file():
        return None
    key = p.read_text(encoding='utf-8-sig').strip()
    return key or None


def build_env(provider: str) -> dict:
    env = dict(os.environ)
    env['DSH_PERMISSION_MODE'] = 'workspace-write'
    env.setdefault(LOCAL_KEY_ENV, 'local')
    for prov, (var, path) in KEY_FILES.items():
        if env.get(var):
            continue
        key = read_key(path)
        if key:
            env[var] = key
        elif prov == provider:
            fail(f'{provider} のキーがありません（環境変数 {var} か {path}）')
    return env


def final_text(jsonl: Path) -> str | None:
    """--json のイベントのうち、最後の final の本文を返す。"""
    text = None
    if not jsonl.is_file():
        return None
    for line in jsonl.read_text(encoding='utf-8', errors='replace').splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict) and ev.get('type') == 'final':
            t = ev.get('text', ev.get('message'))
            if isinstance(t, str):
                text = t
    return text


def dsh_command() -> list[str]:
    """dsh を起動するコマンドの先頭。PBI_DSH_BIN があれば node で直接、無ければ PATH の dsh。"""
    bin_js = os.environ.get('PBI_DSH_BIN')
    if bin_js:
        node = os.environ.get('PBI_DSH_NODE') or shutil.which('node')
        if not node or not Path(node).is_file():
            fail(f'node が見つかりません: {node}（PBI_DSH_NODE で指定）')
        if not Path(bin_js).is_file():
            fail(f'dsh が見つかりません: {bin_js}（PBI_DSH_BIN で指定）')
        return [node, bin_js]
    dsh = shutil.which('dsh')
    if not dsh:
        fail('dsh が見つかりません（npm i -g @deepseek-ai/dsh、または PBI_DSH_BIN で bin.js を指定）')
    return [dsh]


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('report')
    ap.add_argument('--model', required=True)
    ap.add_argument('--effort', default=None)
    ap.add_argument('--pbi-dir', default='docs/pbi')
    args = ap.parse_args()

    if not REPORT.match(args.report):
        fail(f'報告名に使えない文字があります: {args.report!r}')
    provider, model = parse_model(args.model)
    effort = None if args.effort in (None, '', 'null', 'none') else args.effort
    if effort is not None and not NAME.match(effort):
        fail(f'--effort に使えない文字があります: {effort!r}')

    base = Path(args.pbi_dir) / 'dsh'
    prompt = base / f'{args.report}.prompt.md'
    if not prompt.is_file():
        fail(f'依頼文がありません: {prompt}')
    launcher = dsh_command()

    env = build_env(provider)
    patch = base / f'{args.report}.patch.yml'
    patch.write_text(patch_yaml(provider, model, effort), encoding='utf-8')
    task = f'{prompt.as_posix()} を読み、その指示どおりに作業してください。'
    jsonl = base / f'{args.report}.jsonl'
    err = base / f'{args.report}.err'
    start = now()
    with open(jsonl, 'wb') as out, open(err, 'wb') as errf:
        code = subprocess.call(
            [*launcher, '--profile', 'headless', '--patch', str(patch.resolve()), '--json', task],
            stdin=subprocess.DEVNULL, stdout=out, stderr=errf, env=env)
    # dsh の --json には時刻が無いので、record_run.py が読む開始・終了時刻をここで残す
    run = {'start': start, 'end': now(), 'exit': code, 'model': f'{provider}/{model}', 'effort': effort}
    (base / f'{args.report}.run.json').write_text(json.dumps(run, ensure_ascii=False) + '\n', encoding='utf-8')
    text = final_text(jsonl)
    if text is not None:
        (base / f'{args.report}.final.md').write_text(text, encoding='utf-8')
    print(f'dsh_run: exit={code} model={provider}/{model} effort={effort} jsonl={jsonl.as_posix()}')
    sys.exit(code)


if __name__ == '__main__':
    main()
