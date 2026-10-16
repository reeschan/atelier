"""Template Studio を手元で開き、画面の「適用」で routing.json を対象リポジトリに置く。

  python serve.py [--target <リポジトリのルート>] [--port 0] [--no-open] [--keep] [--timeout 1800]

ログインの画面が手元のアドレスに結果を返すのと同じ形で、127.0.0.1 だけで待ち受ける小さなサーバーを立てる。
- GET /             画面 (index.html)。適用先と、この起動だけで使える合言葉 (token) を埋め込む
- GET /catalog.json モデル一覧
- POST /apply       画面から送られた routing.json を確かめて <target>/docs/pbi/routing.json に書く。
                    前の routing.json があれば routing.prev.json に残す
合言葉が違う・Host が 127.0.0.1 でない・形が違う (select_model.routing_error)・書き先がリンクでリポジトリの外を指す、のどれかなら書かない。
書いたら標準出力に「APPLIED <パス>」を出して終わる。--timeout 秒たっても適用されなければ「TIMEOUT」を出して終わる。
--keep のときは適用しても時間がたっても止まらない (Ctrl+C で止める)。
"""
import argparse
import hmac
import json
import os
import secrets
import sys
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from select_model import routing_error  # noqa: E402

MAX_BODY = 64 * 1024
MARKER = '<script type="application/json" id="studio-apply"></script>'


def page(target: Path, token: str) -> bytes:
    html = (HERE / 'index.html').read_text(encoding='utf-8')
    info = json.dumps({'token': token, 'target': str(target / 'docs' / 'pbi' / 'routing.json')},
                      ensure_ascii=False).replace('<', '\\u003c')
    return html.replace(MARKER, f'<script type="application/json" id="studio-apply">{info}</script>', 1).encode('utf-8')


APPLY_LOCK = threading.Lock()


def destination(target: Path) -> Path:
    """書き先を決める。docs・docs/pbi・routing.json・routing.prev.json のどれかがリンクなら、外に書かないよう止める。"""
    root = target.resolve()
    out = root / 'docs' / 'pbi' / 'routing.json'
    for p in (root / 'docs', root / 'docs' / 'pbi', out, out.with_name('routing.prev.json')):
        if p.is_symlink():
            raise ValueError(f'{p} がリンクなので書かない')
    if not out.resolve().is_relative_to(root):
        raise ValueError(f'{out} が対象リポジトリの外を指すので書かない')
    return out


def apply(target: Path, table: dict) -> Path:
    """前の routing.json を routing.prev.json に残し、新しい中身を一時ファイルから置き換える (同時の適用は 1 つずつ)。"""
    with APPLY_LOCK:
        out = destination(target)
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            out.with_name('routing.prev.json').write_bytes(out.read_bytes())
        fd, tmp = tempfile.mkstemp(prefix='.routing.', suffix='.json', dir=out.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(json.dumps(table, ensure_ascii=False, indent=2) + '\n')
            os.replace(tmp, out)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return out


def make_server(target: Path, port: int, token: str, on_apply) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def host_ok(self) -> bool:
            host = self.headers.get('Host', '')
            return host in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')

        def send(self, code: int, body: bytes, ctype: str = 'application/json; charset=utf-8'):
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def fail(self, code: int, msg: str):
            self.send(code, json.dumps({'ok': False, 'error': msg}, ensure_ascii=False).encode('utf-8'))

        def do_GET(self):
            if not self.host_ok():
                return self.fail(403, 'Host が違う')
            path = self.path.split('?')[0]
            if path in ('/', '/index.html'):
                return self.send(200, page(target, token), 'text/html; charset=utf-8')
            if path == '/catalog.json':
                return self.send(200, (HERE / 'catalog.json').read_bytes())
            self.fail(404, 'ない')

        def do_POST(self):
            if not self.host_ok():
                return self.fail(403, 'Host が違う')
            if self.path != '/apply':
                return self.fail(404, 'ない')
            if not hmac.compare_digest(self.headers.get('X-Studio-Token', ''), token):
                return self.fail(403, '合言葉が違う。画面を開き直してください')
            if not self.headers.get('Content-Type', '').startswith('application/json'):
                return self.fail(415, 'JSON で送ってください')
            length = int(self.headers.get('Content-Length') or 0)
            if length <= 0 or length > MAX_BODY:
                return self.fail(413, '大きさが違う')
            try:
                table = json.loads(self.rfile.read(length))
            except ValueError:
                return self.fail(400, 'JSON として読めない')
            err = routing_error(table)
            if err:
                return self.fail(400, err)
            try:
                out = apply(target, table)
            except ValueError as e:
                return self.fail(400, str(e))
            on_apply(out)
            self.send(200, json.dumps({'ok': True, 'path': str(out)}, ensure_ascii=False).encode('utf-8'))

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('--target', default='.')
    ap.add_argument('--port', type=int, default=0)
    ap.add_argument('--no-open', action='store_true')
    ap.add_argument('--keep', action='store_true')
    ap.add_argument('--timeout', type=int, default=1800)
    args = ap.parse_args()
    target = Path(args.target).resolve()
    if not target.is_dir():
        sys.exit(f'--target が無い: {target}')

    token = secrets.token_urlsafe(24)
    done = threading.Event()

    def on_apply(path: Path):
        print(f'APPLIED {path}', flush=True)
        if not args.keep:
            done.set()

    server = make_server(target, args.port, token, on_apply)
    url = f'http://127.0.0.1:{server.server_port}/'
    print(f'OPEN {url}', flush=True)
    print(f'適用先: {target / "docs" / "pbi" / "routing.json"}', flush=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if not args.no_open:
        webbrowser.open(url)
    # --keep のときは適用しても止まらないので、時間切れでも止めない (Ctrl+C で止める)
    if not done.wait(None if args.keep or args.timeout <= 0 else args.timeout):
        print('TIMEOUT', flush=True)
    server.shutdown()


if __name__ == '__main__':
    main()
