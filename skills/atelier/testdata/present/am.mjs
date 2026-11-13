"""偽の am (answer-me-with-any の am.mjs) の代わり。present.py などの試験が使う。

present は `node <am.mjs> render <原稿> [--template doc] [--format html|md]` の形で呼ぶ。
この PC の node や am を見ないよう、同じフォルダの fake_node.py を `node.cmd` / `node` として
一時ディレクトリに置き (fakes.install_node)、その偽の node がこのファイルを Python として実行する
(拡張子は present が探す .mjs に合わせてある)。

受けるもの: `render <原稿> [--template doc] [--format html|md]`。
  - 呼び出しの引数を FAKE_AM_LOG のファイルに 1 行ずつ JSON で追記する。
  - FAKE_AM_EXIT=<n> (n が 0 でない) は、記録だけして n で終わる (想定外の終了コード)。
  - BROKEN を含む原稿は、✗ の行を標準エラーに出して 1 で終わる (present は 3 で止まる)。
  - FAKE_AM_NO_FORMAT が立っているときの --format は、--format を知らない古い am をまねて 2 で終わる。
  - md はページを書かず、frontmatter の reply-meta- の行を除いた Markdown を標準出力に出す。
  - html は FAKE_AM_PAGES にページを 1 つ書き、`✓ <パス>` の行を出す。
"""
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
log = os.environ.get('FAKE_AM_LOG')
if log:
    with open(log, 'a', encoding='utf-8') as stream:
        stream.write(json.dumps(args, ensure_ascii=False) + '\n')
forced = os.environ.get('FAKE_AM_EXIT')
if forced and int(forced) != 0:
    sys.stderr.write(f'✗ fake am forced exit {forced}\n')
    sys.exit(int(forced))
if not args or args[0] != 'render' or len(args) < 2:
    sys.stderr.write('✗ usage: render <file> [--template doc] [--format html|md]\n')
    sys.exit(2)
if '--format' in args:
    if os.environ.get('FAKE_AM_NO_FORMAT'):
        sys.stderr.write('✗ Unknown option --format\n')
        sys.exit(2)
    fmt = args[args.index('--format') + 1]
else:
    fmt = 'html'
text = Path(args[1]).read_bytes().decode('utf-8')
if 'BROKEN' in text:
    sys.stderr.write('✗ L1 [ask] broken\n✗ 1 errors in the draft; no page was written\n')
    sys.exit(1)
if fmt == 'md':
    body = '\n'.join(line for line in text.split('\n') if not line.startswith('reply-meta-'))
    sys.stdout.write('# md\n' + body)
else:
    pages = Path(os.environ['FAKE_AM_PAGES'])
    pages.mkdir(parents=True, exist_ok=True)
    page = pages / f'page-{len(list(pages.glob("page-*.html")))}.html'
    page.write_text('<html><body>' + text + '</body></html>', encoding='utf-8')
    sys.stdout.write(f'✓ {page}\n  sheet · paper · 1 panels\n')
sys.stdout.flush()
