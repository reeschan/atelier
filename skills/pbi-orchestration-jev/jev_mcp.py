"""PBI を MCP で受け渡す実行先のための MCP サーバー (jev-mcp)。

設計者が docs/pbi/mcp/<報告名>.prompt.md に書いた依頼文を、MCP クライアント (Harness² などの外部の
実装担当) が受け取り、結果を同じ形 (詳細ファイルと 5 行の要約) で返すための口。ファイルの形と設計者の
流れ (承認・verify.py・レビュー・差し戻し) は変えない。依存は標準ライブラリだけ。

使い方:
  python <このディレクトリ>/jev_mcp.py --root <作業リポジトリ> [--root ...]            # stdio
  python <このディレクトリ>/jev_mcp.py --http [--port 7830] --root <作業リポジトリ>    # Streamable HTTP

--root (または環境変数 PBI_MCP_ROOTS、os.pathsep 区切り) の下にあるリポジトリだけを扱う。
HTTP は 127.0.0.1 だけで待ち受け、Host が 127.0.0.1・localhost・[::1] でない要求は断る。

ツール:
  list_tasks     docs/pbi/mcp/ の依頼文と、その状態 (queued / claimed / done)
  claim_task     依頼文・PBI・差し戻し文・facts・route・worker.md を返し、<報告名>.claim.json で取得済みにする
  submit_result  docs/pbi/reports/<報告名>.md と docs/pbi/mcp/<報告名>.last.md (5 行) を書く
  fail_task      NEEDS_CONTEXT / BLOCKED として同じ 2 つを書く
  verify         verify.py を作業ディレクトリで実行し、表と番号ごとの終了コードを返す
  jev_gate       jev/jev.py でゲートを 1 回聞く (API キーはこのサーバーの側だけが読む)
"""
import argparse
import datetime
import http.server
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERIFY = HERE / 'verify.py'
JEV = HERE / 'jev' / 'jev.py'

SERVER_NAME = 'jev-mcp'
SERVER_VERSION = '0.1.0'
PROTOCOL_VERSIONS = ('2025-06-18', '2025-03-26', '2024-11-05')
DEFAULT_PORT = 7830
ALLOWED_HOSTS = ('127.0.0.1', 'localhost', '[::1]')

REPORT_RE = re.compile(r'^(PBI-\d+)(?:\.rework-(\d+))?$')
NAME_RE = re.compile(r'^[A-Za-z0-9._-]+$')
STATUSES = ('DONE', 'DONE_WITH_CONCERNS', 'NEEDS_CONTEXT', 'BLOCKED')
FAIL_STATUSES = ('NEEDS_CONTEXT', 'BLOCKED')
# claim_task が sections に入れる PBI の見出し (SKILL.md 2 章のテンプレート)
SECTIONS = ('目的', '対象', '触らない範囲', '受け入れ条件', '検証コマンド', '参照', '補足')
QUEUE = 'mcp'


class ToolError(Exception):
    """呼び出し側の誤り。isError の結果として返す。"""


def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding='utf-8')
    except OSError:
        return None


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def roots_from(args_roots, env=None) -> list[Path]:
    """扱ってよいディレクトリ。--root を先に、無ければ PBI_MCP_ROOTS。"""
    env = os.environ if env is None else env
    raw = list(args_roots or [])
    if not raw and env.get('PBI_MCP_ROOTS'):
        raw = [p for p in env['PBI_MCP_ROOTS'].split(os.pathsep) if p]
    return [Path(p).expanduser().resolve() for p in raw]


def within(path: Path, roots) -> bool:
    return any(path == r or r in path.parents for r in roots)


def sections_of(pbi_text: str) -> dict:
    """PBI の「## 見出し」ごとの本文 (前後の空白を落とす)。"""
    out = {}
    for name in SECTIONS:
        m = re.search(rf'^## {re.escape(name)}\s*\n(.*?)(?=^## |\Z)', pbi_text, re.S | re.M)
        if m:
            out[name] = m.group(1).strip()
    return out


def verify_commands(pbi_text: str) -> list[dict]:
    """verify.py と同じ読み方で検証コマンドを取り出す。"""
    sys.path.insert(0, str(HERE))
    try:
        import verify
    finally:
        sys.path.pop(0)
    return [{'n': n, 'command': c} for n, c in verify.commands(pbi_text)]


class Tasks:
    """docs/pbi/mcp/ の依頼文を受け渡す操作。roots の外のパスは扱わない。"""

    def __init__(self, roots):
        if not roots:
            raise ValueError('--root か PBI_MCP_ROOTS で、扱うリポジトリを 1 つ以上指定する')
        self.roots = [Path(r).resolve() for r in roots]

    # --- パスの確かめ ---

    def repo(self, repo: str) -> Path:
        if not isinstance(repo, str) or not repo:
            raise ToolError('repo を指定する')
        path = Path(repo).expanduser().resolve()
        if not within(path, self.roots):
            raise ToolError(f'扱えるリポジトリの外: {repo}')
        if not (path / 'docs' / 'pbi').is_dir():
            raise ToolError(f'docs/pbi が無い: {repo}')
        return path

    def workdir(self, workdir: str | None, repo: Path) -> Path:
        if not workdir:
            return repo
        path = Path(workdir).expanduser().resolve()
        if not within(path, self.roots):
            raise ToolError(f'扱えるディレクトリの外: {workdir}')
        if not path.is_dir():
            raise ToolError(f'ディレクトリが無い: {workdir}')
        return path

    @staticmethod
    def report(report: str) -> re.Match:
        m = REPORT_RE.match(report or '')
        if not m:
            raise ToolError(f'報告名は PBI-<連番> か PBI-<連番>.rework-<n>: {report}')
        return m

    @staticmethod
    def paths(repo: Path, report: str) -> dict:
        base = repo / 'docs' / 'pbi'
        queue = base / QUEUE
        return {
            'base': base,
            'prompt': queue / f'{report}.prompt.md',
            'claim': queue / f'{report}.claim.json',
            'last': queue / f'{report}.last.md',
            'artifacts': queue / f'{report}.artifacts.json',
            'details': base / 'reports' / f'{report}.md',
        }

    @staticmethod
    def state(p: dict) -> str:
        if p['last'].is_file():
            return 'done'
        if p['claim'].is_file():
            return 'claimed'
        return 'queued'

    # --- ツール ---

    def list_tasks(self, repo: str, include_done: bool = False) -> dict:
        root = self.repo(repo)
        queue = root / 'docs' / 'pbi' / QUEUE
        tasks = []
        for prompt in sorted(queue.glob('*.prompt.md')) if queue.is_dir() else []:
            report = prompt.name[:-len('.prompt.md')]
            m = REPORT_RE.match(report)
            if not m:
                continue
            p = self.paths(root, report)
            state = self.state(p)
            if state == 'done' and not include_done:
                continue
            pbi_text = read_text(p['base'] / f'{m.group(1)}.md') or ''
            title = pbi_text.splitlines()[0].lstrip('# ').strip() if pbi_text else ''
            tasks.append({'report': report, 'pbi': m.group(1), 'title': title, 'state': state,
                          'claim': read_json(p['claim'])})
        return {'repo': str(root), 'tasks': tasks}

    def claim_task(self, repo: str, report: str, runner: str = 'mcp') -> dict:
        root = self.repo(repo)
        m = self.report(report)
        if not isinstance(runner, str) or not NAME_RE.match(runner):
            raise ToolError(f'runner は英数字と ._- だけ: {runner}')
        p = self.paths(root, report)
        prompt = read_text(p['prompt'])
        if prompt is None:
            raise ToolError(f'依頼文が無い: docs/pbi/{QUEUE}/{report}.prompt.md')
        if p['last'].is_file():
            raise ToolError(f'{report} は報告済み')
        try:
            fd = os.open(p['claim'], os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise ToolError(f'{report} はほかの実行先が取得済み: {read_json(p["claim"])}') from None
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump({'runner': runner, 'claimed_at': now_iso()}, f, ensure_ascii=False)
        pbi = m.group(1)
        pbi_text = read_text(p['base'] / f'{pbi}.md') or ''
        rework = read_text(p['base'] / f'{report}.md') if m.group(2) else None
        return {
            'repo': str(root),
            'report': report,
            'pbi': pbi,
            'prompt': prompt,
            'pbi_markdown': pbi_text,
            'sections': sections_of(pbi_text),
            'verify_commands': verify_commands(pbi_text),
            'rework': rework,
            'facts': read_json(p['base'] / f'{pbi}.facts.json'),
            'route': read_json(p['base'] / f'{pbi}.route.json'),
            'worker_md': read_text(p['base'] / 'worker.md'),
            'summary_path': f'docs/pbi/{QUEUE}/{report}.last.md',
            'details_path': f'docs/pbi/reports/{report}.md',
        }

    def _write_report(self, root: Path, report: str, status: str, result: str, changed, unresolved: str,
                      details: str, artifacts=None) -> dict:
        p = self.paths(root, report)
        if not p['claim'].is_file():
            raise ToolError(f'{report} は取得されていない (先に claim_task)')
        if p['last'].is_file():
            raise ToolError(f'{report} は報告済み')
        changed = [str(c) for c in (changed or [])]
        unresolved = (unresolved or '').strip() or 'なし'
        body = (details or '').strip()
        if body.startswith('状態:'):
            body = body.split('\n', 1)[1].strip() if '\n' in body else ''
        p['details'].parent.mkdir(parents=True, exist_ok=True)
        p['details'].write_text(f'状態: {status}\n\n{body}\n', encoding='utf-8')
        summary = '\n'.join([
            f'状態: {status}',
            f'結果: {result}',
            f'変更: {", ".join(changed) if changed else "なし"}',
            f'未解決: {" ".join(unresolved.splitlines())}',
            f'詳細: docs/pbi/reports/{report}.md',
        ]) + '\n'
        if artifacts is not None:
            p['artifacts'].write_text(json.dumps(artifacts, ensure_ascii=False, indent=1), encoding='utf-8')
        # .last.md を最後に書く (これがあれば報告済みとみなす)
        p['last'].write_text(summary, encoding='utf-8')
        return {'report': report, 'summary': summary,
                'summary_path': f'docs/pbi/{QUEUE}/{report}.last.md',
                'details_path': f'docs/pbi/reports/{report}.md',
                'artifacts_path': f'docs/pbi/{QUEUE}/{report}.artifacts.json' if artifacts is not None else None}

    def submit_result(self, repo: str, report: str, status: str, passed: int, total: int,
                      changed=None, unresolved: str = '', details: str = '', artifacts=None) -> dict:
        root = self.repo(repo)
        self.report(report)
        if status not in STATUSES:
            raise ToolError(f'status は {" / ".join(STATUSES)} のどれか: {status}')
        if not (isinstance(passed, int) and isinstance(total, int) and 0 <= passed <= total):
            raise ToolError('passed と total は 0 <= passed <= total の整数')
        return self._write_report(root, report, status, f'受け入れ条件 pass {passed}/{total}', changed,
                                  unresolved, details, artifacts)

    def fail_task(self, repo: str, report: str, status: str, reason: str, details: str = '') -> dict:
        root = self.repo(repo)
        self.report(report)
        if status not in FAIL_STATUSES:
            raise ToolError(f'status は {" / ".join(FAIL_STATUSES)} のどれか: {status}')
        if not (reason or '').strip():
            raise ToolError('reason を書く')
        body = details.strip() or f'## 未解決\n- {reason.strip()}'
        return self._write_report(root, report, status, '未実施', [], reason, body)

    def verify(self, repo: str, report: str, workdir: str | None = None, name: str | None = None,
               timeout: int = 900) -> dict:
        root = self.repo(repo)
        m = self.report(report)
        cwd = self.workdir(workdir, root)
        name = name or f'{report}.mcp'
        if not NAME_RE.match(name):
            raise ToolError(f'name は英数字と ._- だけ: {name}')
        base = root / 'docs' / 'pbi'
        r = subprocess.run([sys.executable, str(VERIFY), m.group(1), '--dir', str(base), '--name', name,
                            '--timeout', str(int(timeout))],
                           cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace')
        results = read_json(base / 'verify' / f'{name}.json')
        return {'code': r.returncode, 'table': r.stdout, 'stderr': r.stderr[-2000:],
                'results': (results or {}).get('results')}

    def jev_gate(self, repo: str, gate: str, text: str) -> dict:
        root = self.repo(repo)
        if not isinstance(gate, str) or not NAME_RE.match(gate):
            raise ToolError(f'gate は英数字と ._- だけ: {gate}')
        if not (text or '').strip():
            raise ToolError('text を書く')
        config = root / 'docs' / 'pbi' / 'jev.config.json'
        if not config.is_file():
            raise ToolError('docs/pbi/jev.config.json が無い')
        fd, tmp = tempfile.mkstemp(suffix='.txt')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(text)
            r = subprocess.run([sys.executable, str(JEV), str(config), gate, tmp], cwd=root,
                               capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300)
        finally:
            os.unlink(tmp)
        try:
            answer = json.loads(r.stdout)
        except ValueError:
            answer = None
        return {'code': r.returncode, 'answer': answer, 'stdout': None if answer is not None else r.stdout,
                'stderr': r.stderr[-2000:]}


REPO = {'type': 'string', 'description': '作業リポジトリの絶対パス (サーバーの --root の下)'}
REPORT = {'type': 'string', 'description': '報告名 (PBI-007 / PBI-007.rework-1)'}

TOOLS = [
    {'name': 'list_tasks',
     'description': 'docs/pbi/mcp/ に置かれた依頼文と状態 (queued / claimed / done) を返す。',
     'inputSchema': {'type': 'object', 'required': ['repo'], 'properties': {
         'repo': REPO,
         'include_done': {'type': 'boolean', 'default': False, 'description': '報告済みも含める'}}}},
    {'name': 'claim_task',
     'description': '依頼文を取得済みにし、依頼文・PBI 本文と見出しごとの節・検証コマンド・差し戻し文・facts・route・'
                    'worker.md を返す。同じ報告名を 2 回は取得できない。',
     'inputSchema': {'type': 'object', 'required': ['repo', 'report'], 'properties': {
         'repo': REPO, 'report': REPORT,
         'runner': {'type': 'string', 'default': 'mcp', 'description': '取得した実行先の名前 (例 harness2)'}}}},
    {'name': 'submit_result',
     'description': '結果を docs/pbi/reports/<報告名>.md と docs/pbi/mcp/<報告名>.last.md (worker.md の 5 行) に書く。'
                    'artifacts は実行先の出力 (Harness² の summary など) をそのまま JSON で残す。',
     'inputSchema': {'type': 'object', 'required': ['repo', 'report', 'status', 'passed', 'total'], 'properties': {
         'repo': REPO, 'report': REPORT,
         'status': {'type': 'string', 'enum': list(STATUSES)},
         'passed': {'type': 'integer', 'minimum': 0, 'description': '自分で確かめて pass した受け入れ条件の数'},
         'total': {'type': 'integer', 'minimum': 0, 'description': '受け入れ条件の数'},
         'changed': {'type': 'array', 'items': {'type': 'string'}, 'description': '変更したファイル'},
         'unresolved': {'type': 'string', 'description': '未解決 (無ければ空)'},
         'details': {'type': 'string', 'description': '詳細ファイルの本文 (worker.md の 4 つの見出し)'},
         'artifacts': {'type': 'object', 'description': '実行先の出力 (任意)'}}}},
    {'name': 'fail_task',
     'description': '進められなかったことを NEEDS_CONTEXT / BLOCKED として報告する。',
     'inputSchema': {'type': 'object', 'required': ['repo', 'report', 'status', 'reason'], 'properties': {
         'repo': REPO, 'report': REPORT,
         'status': {'type': 'string', 'enum': list(FAIL_STATUSES)},
         'reason': {'type': 'string'},
         'details': {'type': 'string'}}}},
    {'name': 'verify',
     'description': 'verify.py で PBI の検証コマンドを workdir (省略時は repo) で実行し、表と番号ごとの終了コードを返す。'
                    '合否は判定しない。',
     'inputSchema': {'type': 'object', 'required': ['repo', 'report'], 'properties': {
         'repo': REPO, 'report': REPORT,
         'workdir': {'type': 'string', 'description': '検証する作業ツリー (worktree など。--root の下)'},
         'name': {'type': 'string', 'description': 'ログ名 (既定 <報告名>.mcp)'},
         'timeout': {'type': 'integer', 'default': 900}}}},
    {'name': 'jev_gate',
     'description': 'jev/jev.py で docs/pbi/jev.config.json のゲートを 1 回聞く。文章だけを送る (コード・差分・秘密は送らない)。',
     'inputSchema': {'type': 'object', 'required': ['repo', 'gate', 'text'], 'properties': {
         'repo': REPO,
         'gate': {'type': 'string', 'description': 'brief / pbi_quality / report / rework_scope / rework_quality など'},
         'text': {'type': 'string'}}}},
]


class Server:
    """MCP の JSON-RPC (initialize・ping・tools/list・tools/call) を Tasks につなぐ。"""

    def __init__(self, tasks: Tasks):
        self.tasks = tasks

    def handle(self, msg):
        """1 件の要求に答える。通知 (id なし) には None を返す。"""
        if not isinstance(msg, dict) or msg.get('jsonrpc') != '2.0' or not isinstance(msg.get('method'), str):
            return self._error(msg.get('id') if isinstance(msg, dict) else None, -32600, 'Invalid Request')
        if 'id' not in msg:
            return None
        rid, method, params = msg['id'], msg['method'], msg.get('params') or {}
        if method == 'initialize':
            asked = params.get('protocolVersion')
            version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return self._result(rid, {'protocolVersion': version, 'capabilities': {'tools': {}},
                                      'serverInfo': {'name': SERVER_NAME, 'version': SERVER_VERSION}})
        if method == 'ping':
            return self._result(rid, {})
        if method == 'tools/list':
            return self._result(rid, {'tools': TOOLS})
        if method == 'tools/call':
            return self._result(rid, self.call(params.get('name'), params.get('arguments') or {}))
        return self._error(rid, -32601, f'Method not found: {method}')

    def call(self, name, arguments) -> dict:
        if name not in {t['name'] for t in TOOLS}:
            return self._tool_error(f'ツールが無い: {name}')
        if not isinstance(arguments, dict):
            return self._tool_error('arguments はオブジェクト')
        try:
            value = getattr(self.tasks, name)(**arguments)
        except ToolError as e:
            return self._tool_error(str(e))
        except TypeError as e:
            return self._tool_error(f'引数が合わない: {e}')
        return {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}],
                'structuredContent': value, 'isError': False}

    def handle_body(self, body):
        """1 件か配列 (batch) を受け、返す値 (無ければ None) を返す。"""
        if isinstance(body, list):
            out = [r for r in (self.handle(m) for m in body) if r is not None]
            return out or None
        return self.handle(body)

    @staticmethod
    def _result(rid, result):
        return {'jsonrpc': '2.0', 'id': rid, 'result': result}

    @staticmethod
    def _error(rid, code, message):
        return {'jsonrpc': '2.0', 'id': rid, 'error': {'code': code, 'message': message}}

    @staticmethod
    def _tool_error(message):
        return {'content': [{'type': 'text', 'text': message}], 'isError': True}


def serve_stdio(server: Server, stdin=None, stdout=None):
    """1 行 1 メッセージの JSON-RPC を読み、答えを 1 行ずつ書く。"""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            body = json.loads(line)
        except ValueError:
            reply = Server._error(None, -32700, 'Parse error')
        else:
            reply = server.handle_body(body)
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + '\n')
            stdout.flush()


def make_handler(server: Server, port: int):
    allowed = {f'{h}:{port}' for h in ALLOWED_HOSTS} | set(ALLOWED_HOSTS)

    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, code, body=None):
            data = b'' if body is None else json.dumps(body, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            if body is not None:
                self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _host_ok(self):
            return self.headers.get('Host', '') in allowed

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, {'error': 'host not allowed'})
            if self.path == '/health':
                return self._send(200, {'ok': True})
            self.send_response(405)
            self.send_header('Allow', 'POST')
            self.send_header('Content-Length', '0')
            self.end_headers()

        def do_POST(self):
            if not self._host_ok():
                return self._send(403, {'error': 'host not allowed'})
            if self.path != '/mcp':
                return self._send(404, {'error': 'not found'})
            try:
                length = int(self.headers.get('Content-Length') or 0)
                body = json.loads(self.rfile.read(length).decode('utf-8'))
            except (ValueError, UnicodeDecodeError):
                return self._send(400, Server._error(None, -32700, 'Parse error'))
            reply = server.handle_body(body)
            if reply is None:
                return self._send(202)
            self._send(200, reply)

        def log_message(self, fmt, *args):
            sys.stderr.write('jev-mcp: ' + fmt % args + '\n')

    return Handler


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--root', action='append', default=[], help='扱う作業リポジトリ (くり返し可)')
    ap.add_argument('--http', action='store_true', help='Streamable HTTP で待ち受ける (省略時は stdio)')
    ap.add_argument('--port', type=int, default=DEFAULT_PORT)
    args = ap.parse_args(argv)
    roots = roots_from(args.root)
    if not roots:
        ap.error('--root か PBI_MCP_ROOTS で、扱うリポジトリを 1 つ以上指定する')
    server = Server(Tasks(roots))
    if not args.http:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8')
        serve_stdio(server)
        return 0
    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(server, args.port))
    sys.stderr.write(f'jev-mcp: http://127.0.0.1:{args.port}/mcp\n')
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
