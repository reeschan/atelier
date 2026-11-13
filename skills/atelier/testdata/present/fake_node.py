"""偽の node。present.py・adapter_gate・tools_check・conductor の試験が使う (am.mjs の説明も見ること)。

`node --version` は v22.1.0 を出し、それ以外の呼び出しは第 1 引数の偽の am.mjs を Python として
実行する。試験はこのファイルを一時ディレクトリに写し、Windows では `node.cmd`、それ以外では
実行できる `node` から呼ぶ (fakes.install_node)。中で本物の node は使わない。

環境変数で振る舞いを変える:
  FAKE_NODE_VERSION  `--version` が出す版 (既定 v22.1.0。`junk` は読めない版)
  FAKE_NODE_EXIT     0 でない整数なら `--version` が何も出さずにその終了コードで終わる
  FAKE_NODE_SLEEP    秒。起動してすぐ、その時間だけ眠る (タイムアウトの試験用)
  FAKE_NODE_MARKER   このファイルがあれば作る (node が起動されたかを見る印。眠る前に作る)
"""
import os
import sys
import time
from pathlib import Path


def main(argv):
    marker = os.environ.get('FAKE_NODE_MARKER')
    if marker:
        Path(marker).parent.mkdir(parents=True, exist_ok=True)
        Path(marker).write_text('launched\n', encoding='utf-8')
    delay = os.environ.get('FAKE_NODE_SLEEP')
    if delay:
        time.sleep(float(delay))
    if argv[:1] == ['--version']:
        code = os.environ.get('FAKE_NODE_EXIT')
        if code and int(code) != 0:
            return int(code)
        print(os.environ.get('FAKE_NODE_VERSION') or 'v22.1.0')
        return 0
    if not argv:
        print('node: no script', file=sys.stderr)
        return 2
    script = Path(argv[0])
    sys.argv = [str(script), *argv[1:]]
    source = script.read_bytes().decode('utf-8')
    exec(compile(source, str(script), 'exec'), {'__name__': '__main__', '__file__': str(script)})
    return 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', newline='\n')
    sys.stderr.reconfigure(encoding='utf-8')
    try:
        code = main(sys.argv[1:])
    except SystemExit as error:
        code = error.code if isinstance(error.code, int) else (0 if error.code is None else 2)
    sys.exit(code)
