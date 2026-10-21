"""Template Studio の編集画面のサーバー。home のファイルだけを読み書きする。

  python serve.py [--home DIR] [--port 0] [--no-open] [--timeout 1800]
                  [--tab agents|routing|priority]

127.0.0.1 だけで待ち受け、画面 (index.html) にその起動だけの合言葉 (token) と home を
埋め込む。画面は 3 つのタブ (agents / models・routing・priority) から、home の
agents.json・models/<runner>.json・routing.json・templates/<name>.json・overrides.json を
HTTP の API で読み書きする。
- GET /                 画面。studio-boot の JSON に token と home を入れる
- GET /api/state         home の今の中身 (runners・routing・templates・examples・application)
- PUT /api/agents/<runner>         選んだモデル・enabled・billing を変える
- POST /api/models/<runner>        手入力のモデルを足す
- PUT /api/routing                 振り分けテンプレートを書く (前の版は routing.prev.json)
- PUT / DELETE /api/templates/<name>  優先テンプレートを書く・消す
- POST /api/examples/<name>/copy   同梱の例を home にコピーする
- PUT /api/application             優先テンプレートの適用を始める
- POST /api/application/answer     期限切れへの答え (extend / stop / forever)
- POST /api/close                  応答を返してから終わる

標準出力は起動時に OPEN <url>、保存のたびに SAVED <kind> <path>、/api/close で CLOSED、
--timeout 秒の間なにも保存されなければ TIMEOUT (0 以下は無制限)。保存のたびに数え直す。
/api/* は X-Studio-Token が合言葉と一致しなければ 403。Host が 127.0.0.1:<port> か
localhost:<port> でなければ 403。書き込みは JSON で 64KB まで。
"""
import argparse
import hmac
import json
import re
import secrets
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import agents  # noqa: E402
import common  # noqa: E402
import select_model  # noqa: E402
import setup_agents  # noqa: E402
import templates  # noqa: E402

MAX_BODY = 64 * 1024
MARKER = '<script type="application/json" id="studio-boot"></script>'
CATALOG = HERE / 'catalog.json'
EXAMPLES = HERE.parent / 'examples' / 'templates'
# モデル id の末尾に付く effort の名前 (品質・価格の参考を catalog と照合するときに落とす)
EFFORT_SUFFIX = re.compile(r'-(?:low|medium|high|free)$')


class ApiError(Exception):
    """API の失敗。code は HTTP の状態、message は英語の 1 文。"""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def page(home: Path, token: str) -> bytes:
    """index.html の studio-boot の印に、合言葉と home を埋め込んだ画面を返す。"""
    html = (HERE / 'index.html').read_text(encoding='utf-8')
    info = json.dumps({'token': token, 'home': str(home)}, ensure_ascii=False).replace('<', '\\u003c')
    boot = f'<script type="application/json" id="studio-boot">{info}</script>'
    return html.replace(MARKER, boot, 1).encode('utf-8')


def load_catalog() -> dict:
    """品質・価格の参考に使う catalog.json。読めなければ空。"""
    data = common.load_json(CATALOG, {})
    return data if isinstance(data, dict) else {}


def catalog_models(catalog: dict) -> list:
    models = catalog.get('models') if isinstance(catalog, dict) else None
    return models if isinstance(models, list) else []


def model_ref(catalog: dict, runner: str, model_id, item: dict):
    """モデル 1 つの品質・価格の参考。catalog → aa_slug → CLI の単価 → None の順に探す。"""
    if not isinstance(model_id, str):
        return None
    for model in catalog_models(catalog):
        if not isinstance(model, dict):
            continue
        options = model.get('options')
        if not isinstance(options, list):
            continue
        for option in options:
            if isinstance(option, dict) and option.get('runner') == runner and option.get('model') == model_id:
                return {'quality': model.get('quality'), 'price_in': model.get('price_in'),
                        'price_out': model.get('price_out'), 'sample': model.get('sample'),
                        'source': 'catalog'}
    # provider の部分 (/ より前) と末尾の effort を落とし、. を - にすると aa_slug になる
    slug = EFFORT_SUFFIX.sub('', model_id.rsplit('/', 1)[-1]).replace('.', '-')
    for model in catalog_models(catalog):
        if isinstance(model, dict) and model.get('aa_slug') == slug:
            return {'quality': model.get('quality'), 'price_in': model.get('price_in'),
                    'price_out': model.get('price_out'), 'sample': model.get('sample'),
                    'source': 'catalog'}
    price_in, price_out = item.get('price_in'), item.get('price_out')
    if isinstance(price_in, (int, float)) or isinstance(price_out, (int, float)):
        return {'quality': None, 'price_in': price_in, 'price_out': price_out,
                'sample': False, 'source': 'cli'}
    return None


def model_entry(catalog: dict, runner: str, item: dict) -> dict:
    """models/<runner>.json の 1 件を、画面に返す形にする。"""
    entry = {'id': item.get('id'), 'name': item.get('name'), 'efforts': item.get('efforts') or []}
    if item.get('manual') is True:
        entry['manual'] = True
    entry['ref'] = model_ref(catalog, runner, item.get('id'), item)
    return entry


def runner_state(home: Path, runner: str, catalog: dict) -> dict:
    """runner 1 つの状態 (agents.json の設定と models/<runner>.json の一覧)。"""
    table = agents.load_agents(home) or {}
    runners = table.get('runners') if isinstance(table.get('runners'), dict) else {}
    config = runners.get(runner) if isinstance(runners.get(runner), dict) else {}
    selected = config.get('models') if isinstance(config.get('models'), list) else None
    data = setup_agents.load_models(home, runner)
    models = None
    if data is not None:
        entries = data.get('models') if isinstance(data.get('models'), list) else []
        models = {'fetched_at': data.get('fetched_at'), 'method': data.get('method'),
                  'error': data.get('error'), 'failed_at': data.get('failed_at'),
                  'list': [model_entry(catalog, runner, item) for item in entries
                           if isinstance(item, dict)]}
    return {
        'enabled': True if runner == common.AGENT else bool(config.get('enabled')),
        'path': config.get('path'),
        'version': config.get('version'),
        'checked_at': config.get('checked_at'),
        'billing': config.get('billing'),
        'selected': selected,
        'missing_selected': setup_agents.missing_selected(home, runner),
        'models': models,
    }


def routing_state(home: Path) -> tuple[dict, list]:
    """routing の出どころと表。home の routing.json が壊れていれば既定を使って警告する。"""
    path = home / 'routing.json'
    if path.is_file():
        table = common.load_json(path)
        if table is not None and select_model.routing_error(table) is None:
            return {'source': 'home', 'table': table}, []
        return ({'source': 'default', 'table': select_model.default_routing()},
                ['routing.json is invalid; the bundled default is used.'])
    return {'source': 'default', 'table': select_model.default_routing()}, []


def examples() -> dict:
    """同梱の examples/templates/*.json をファイル名で引ける辞書にする。"""
    out = {}
    if EXAMPLES.is_dir():
        for path in sorted(EXAMPLES.glob('*.json')):
            template = common.load_json(path)
            if isinstance(template, dict):
                out[path.stem] = template
    return out


def application_status(home: Path, application, now) -> str:
    """適用の状態。status_local と違って書き込みはしない。"""
    if application is None:
        return 'none'
    if not isinstance(application, dict):
        return 'unknown_template'
    if templates.load_template(home, application.get('template')) is None:
        return 'unknown_template'
    try:
        return templates.state_of(application, now)
    except ValueError:
        return 'unknown_template'


def state(home: Path, catalog: dict, now) -> dict:
    """GET /api/state の応答。"""
    runner_table = {}
    warnings = []
    for runner in common.RUNNERS:
        runner_table[runner] = runner_state(home, runner, catalog)
        models = runner_table[runner]['models']
        if models is not None and models.get('error'):
            warnings.append(f'{runner}: the model list could not be fetched: {models["error"]}')
        for model_id in runner_table[runner]['missing_selected']:
            warnings.append(f'{runner}: selected model {model_id} is not in the model list')
    routing, routing_warnings = routing_state(home)
    warnings.extend(routing_warnings)
    application = templates.load_overrides(home)['application']
    status = application_status(home, application, now)
    if status == 'unknown_template':
        warnings.append('the applied template is missing or invalid.')
    return {'home': str(home), 'runners': runner_table, 'routing': routing,
            'templates': templates.load_all(home), 'examples': examples(),
            'application': application, 'application_status': status, 'warnings': warnings}


def write_routing(home: Path, table: dict) -> Path:
    """routing.json を書く。前のファイルは routing.prev.json に残す。"""
    path = home / 'routing.json'
    if path.is_file():
        (home / 'routing.prev.json').write_bytes(path.read_bytes())
    common.write_json(path, table)
    return path


def routing_problem(home: Path, table: dict) -> str | None:
    """振り分けテンプレートの問題 (英語の 1 文)。無ければ None。"""
    error = select_model.routing_error(table)
    if error is not None:
        return f'the routing table is invalid: {error}'
    table_agents = agents.load_agents(home)
    if table_agents is None:
        return None
    configs = table_agents.get('runners')
    configs = configs if isinstance(configs, dict) else {}
    for tier in common.TIERS:
        for step in table['tiers'][tier]:
            runner = step.get('runner')
            if runner == common.AGENT:
                continue
            config = configs.get(runner)
            if not isinstance(config, dict) or not config.get('enabled'):
                return f'{runner} is not enabled in agents.json.'
            selected = config.get('models')
            if isinstance(selected, list) and step.get('model') not in selected:
                return f"{step.get('model')} is not selected for {runner}."
    return None


def template_path(home: Path, name: str) -> Path:
    """home の templates/<name>.json。名前は templates.NAME_RE に合うものだけ。"""
    if not isinstance(name, str) or not templates.NAME_RE.fullmatch(name):
        raise ApiError(400, 'the template name is not valid.')
    return home / 'templates' / f'{name}.json'


class Deadline:
    """保存のたびに数え直す時間切れ。seconds が 0 以下なら無制限。"""

    def __init__(self, seconds: int):
        self.seconds = seconds
        self.lock = threading.Lock()
        self.at = None if seconds <= 0 else time.monotonic() + seconds

    def touch(self) -> None:
        if self.seconds <= 0:
            return
        with self.lock:
            self.at = time.monotonic() + self.seconds

    def expired(self) -> bool:
        with self.lock:
            return self.at is not None and time.monotonic() >= self.at


def make_server(home: Path, port: int, token: str, on_saved=None,
                on_close=None) -> ThreadingHTTPServer:
    """127.0.0.1 で待ち受けるサーバーを作る。home への書き込みは 1 つのロックで行う。"""
    home = Path(home)
    lock = threading.Lock()
    saved = on_saved or (lambda kind, path: None)
    closed = on_close or (lambda: None)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def host_ok(self) -> bool:
            host = self.headers.get('Host', '')
            return host in (f'127.0.0.1:{self.server.server_port}',
                            f'localhost:{self.server.server_port}')

        def token_ok(self) -> bool:
            return hmac.compare_digest(self.headers.get('X-Studio-Token', ''), token)

        def send(self, code: int, body: bytes, ctype: str = 'application/json; charset=utf-8'):
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, code: int, data: dict):
            self.send(code, json.dumps(data, ensure_ascii=False).encode('utf-8'))

        def fail(self, code: int, message: str):
            self.send_json(code, {'ok': False, 'error': message})

        def request_path(self) -> str:
            return self.path.split('?')[0]

        def check_api(self) -> bool:
            """共通の入口。Host と /api/* の合言葉を確かめる。"""
            if not self.host_ok():
                self.fail(403, 'the Host header is not the local studio address.')
                return False
            if self.request_path().startswith('/api/') and not self.token_ok():
                self.fail(403, 'the studio token is missing or wrong.')
                return False
            return True

        def read_json(self, allow_empty: bool = False):
            """本文の JSON。形が違えば ApiError。"""
            length_header = self.headers.get('Content-Length')
            try:
                length = int(length_header or 0)
            except ValueError:
                length = 0
            if length == 0 and allow_empty:
                return {}
            if not self.headers.get('Content-Type', '').startswith('application/json'):
                raise ApiError(415, 'send the body with Content-Type: application/json.')
            if length <= 0 or length > MAX_BODY:
                raise ApiError(413, 'the body must be between 1 byte and 64 KB.')
            try:
                body = json.loads(self.rfile.read(length))
            except ValueError:
                raise ApiError(400, 'the body is not valid JSON.') from None
            return body

        def body_object(self, allow_empty: bool = False) -> dict:
            body = self.read_json(allow_empty)
            if not isinstance(body, dict):
                raise ApiError(400, 'the body must be a JSON object.')
            return body

        def save(self, kind: str, path: Path) -> None:
            saved(kind, path)

        def do_GET(self):
            if not self.check_api():
                return
            path = self.request_path()
            try:
                if path in ('/', '/index.html'):
                    return self.send(200, page(home, token), 'text/html; charset=utf-8')
                if path == '/catalog.json':
                    return self.send(200, CATALOG.read_bytes())
                if path == '/api/state':
                    return self.send_json(200, state(home, load_catalog(), common.now()))
                self.fail(404, 'the requested path does not exist.')
            except Exception:
                self.fail(500, 'the server could not read the state.')

        def do_PUT(self):
            if not self.check_api():
                return
            try:
                self.handle_put()
            except ApiError as error:
                self.fail(error.code, error.message)
            except ValueError as error:
                self.fail(400, str(error))
            except Exception:
                self.fail(500, 'the server could not complete the request.')

        def do_POST(self):
            if not self.check_api():
                return
            try:
                self.handle_post()
            except ApiError as error:
                self.fail(error.code, error.message)
            except ValueError as error:
                self.fail(400, str(error))
            except Exception:
                self.fail(500, 'the server could not complete the request.')

        def do_DELETE(self):
            if not self.check_api():
                return
            try:
                self.handle_delete()
            except ApiError as error:
                self.fail(error.code, error.message)
            except ValueError as error:
                self.fail(400, str(error))
            except Exception:
                self.fail(500, 'the server could not complete the request.')

        def handle_put(self):
            path = self.request_path()
            if path.startswith('/api/agents/'):
                return self.put_agent(path.removeprefix('/api/agents/'))
            if path == '/api/routing':
                return self.put_routing()
            if path.startswith('/api/templates/'):
                return self.put_template(path.removeprefix('/api/templates/'))
            if path == '/api/application':
                return self.put_application()
            self.fail(404, 'the requested path does not exist.')

        def handle_post(self):
            path = self.request_path()
            if path == '/api/close':
                return self.close_studio()
            if path.startswith('/api/models/'):
                return self.post_model(path.removeprefix('/api/models/'))
            if path.startswith('/api/examples/') and path.endswith('/copy'):
                name = path.removeprefix('/api/examples/').removesuffix('/copy')
                return self.copy_example(name)
            if path == '/api/application/answer':
                return self.post_answer()
            self.fail(404, 'the requested path does not exist.')

        def handle_delete(self):
            path = self.request_path()
            if path.startswith('/api/templates/'):
                return self.delete_template(path.removeprefix('/api/templates/'))
            self.fail(404, 'the requested path does not exist.')

        def put_agent(self, runner: str):
            if runner not in common.RUNNERS:
                raise ApiError(400, f'unknown runner: {runner}.')
            body = self.body_object()
            enabled = body.get('enabled') if 'enabled' in body else None
            models = None
            if 'selected' in body:
                models = setup_agents.REMOVE if body['selected'] is None else body['selected']
            billing = body.get('billing')
            with lock:
                try:
                    setup_agents.set_runner(home, runner, enabled=enabled, models=models,
                                            billing=billing)
                except ValueError as error:
                    raise ApiError(400, str(error)) from None
                runner_now = runner_state(home, runner, load_catalog())
            self.save('agents', home / setup_agents.AGENTS_FILE)
            self.send_json(200, {'ok': True, 'runner': runner_now})

        def post_model(self, runner: str):
            if runner not in common.RUNNERS:
                raise ApiError(400, f'unknown runner: {runner}.')
            body = self.body_object()
            model_id = body.get('id')
            if not isinstance(model_id, str) or not model_id.strip():
                raise ApiError(400, 'id must be a non-empty string.')
            efforts = body.get('efforts', [])
            if efforts is None:
                efforts = []
            if not isinstance(efforts, list):
                raise ApiError(400, 'efforts must be a list.')
            with lock:
                try:
                    data = setup_agents.add_manual_model(home, runner, model_id, efforts)
                except ValueError as error:
                    raise ApiError(400, str(error)) from None
            self.save('models', home / setup_agents.MODELS_DIR / f'{runner}.json')
            self.send_json(200, {'ok': True, 'models': data})

        def put_routing(self):
            body = self.body_object()
            problem = routing_problem(home, body)
            if problem is not None:
                raise ApiError(400, problem)
            with lock:
                out = write_routing(home, body)
            self.save('routing', out)
            self.send_json(200, {'ok': True, 'path': str(out)})

        def put_template(self, name: str):
            out = template_path(home, name)
            body = self.body_object()
            problems = templates.validate(body, name)
            if problems:
                raise ApiError(400, 'the template is not valid: ' + '; '.join(problems))
            with lock:
                common.write_json(out, body)
            self.save('template', out)
            self.send_json(200, {'ok': True, 'name': name, 'path': str(out)})

        def delete_template(self, name: str):
            out = template_path(home, name)
            application = templates.load_overrides(home)['application']
            if isinstance(application, dict) and application.get('template') == name:
                raise ApiError(409, 'the template is used by the current application.')
            if not out.is_file():
                raise ApiError(404, 'the template does not exist.')
            with lock:
                out.unlink()
            self.save('template', out)
            self.send_json(200, {'ok': True})

        def copy_example(self, name: str):
            if not isinstance(name, str) or not templates.NAME_RE.fullmatch(name):
                raise ApiError(400, 'the example name is not valid.')
            source = EXAMPLES / f'{name}.json'
            if not source.is_file():
                raise ApiError(404, 'the example does not exist.')
            body = self.body_object(allow_empty=True)
            new_name = body.get('as') if 'as' in body else name
            if not isinstance(new_name, str) or not templates.NAME_RE.fullmatch(new_name):
                raise ApiError(400, 'the new template name is not valid.')
            out = home / 'templates' / f'{new_name}.json'
            if out.exists():
                raise ApiError(409, 'a template with that name already exists.')
            template = common.load_json(source)
            if not isinstance(template, dict):
                raise ApiError(500, 'the example could not be read.')
            template = dict(template, name=new_name)
            with lock:
                common.write_json(out, template)
            self.save('template', out)
            self.send_json(200, {'ok': True, 'name': new_name, 'path': str(out)})

        def put_application(self):
            body = self.body_object()
            name = body.get('template')
            if not isinstance(name, str) or templates.load_template(home, name) is None:
                raise ApiError(400, 'the template does not exist or is invalid.')
            until = body.get('until')
            if until is not None and not isinstance(until, str):
                raise ApiError(400, 'until must be an ISO time or null.')
            on_expire = body.get('on_expire')
            if on_expire not in ('ask', 'drop'):
                raise ApiError(400, 'on_expire must be ask or drop.')
            reason = body.get('reason', '')
            if not isinstance(reason, str):
                raise ApiError(400, 'reason must be a string.')
            until_arg = 'none' if until is None else until
            with lock:
                try:
                    result = templates.apply_local(home, name, until_arg, on_expire,
                                                   reason, 'studio', common.now())
                except ValueError as error:
                    raise ApiError(400, f'the application was not started: {error}') from None
                template = templates.load_template(home, name)
                opus = isinstance(template, dict) and bool(template['tiers'].get('opus'))
            self.save('application', home / 'overrides.json')
            self.send_json(200, {'ok': True, 'application': result['application'],
                                 'writes': result['writes'], 'opus_override': opus})

        def post_answer(self):
            body = self.body_object()
            choice = body.get('choice')
            if choice not in ('extend', 'stop', 'forever'):
                raise ApiError(400, 'choice must be extend, stop or forever.')
            until = body.get('until')
            if until is not None and not isinstance(until, str):
                raise ApiError(400, 'until must be an ISO time or null.')
            if choice == 'extend' and not until:
                raise ApiError(400, 'until is required to extend.')
            if not isinstance(templates.load_overrides(home)['application'], dict):
                raise ApiError(400, 'there is no application to answer.')
            with lock:
                try:
                    result = templates.answer_local(home, choice, until, common.now())
                except ValueError as error:
                    raise ApiError(400, f'the answer was not saved: {error}') from None
            self.save('application', home / 'overrides.json')
            self.send_json(200, {'ok': True, 'application': result['application'],
                                 'writes': result['writes']})

        def close_studio(self):
            # 本文は要らない。応答を返してから止める
            self.send_json(200, {'ok': True})
            try:
                self.wfile.flush()
            except OSError:
                pass
            closed()

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


class Parser(argparse.ArgumentParser):
    """不正な引数は標準エラーの 1 行で止める。"""

    def error(self, message: str) -> None:
        sys.stderr.write(f'error: {message}\n')
        self.exit(2)


def build_parser() -> argparse.ArgumentParser:
    parser = Parser(prog='serve.py', description='Open the Template Studio in a local browser.')
    parser.add_argument('--home', metavar='DIR',
                        help='home directory (default: PBI_HOME, then ~/.config/pbi-orchestration-jev)')
    parser.add_argument('--port', type=int, default=0, help='port to listen on (0 picks a free one)')
    parser.add_argument('--no-open', action='store_true', help='do not open a browser')
    parser.add_argument('--timeout', type=int, default=1800,
                        help='seconds without a save before exit (0 or less: no timeout)')
    parser.add_argument('--tab', choices=('agents', 'routing', 'priority'),
                        help='tab to open (#agents, #routing or #priority)')
    return parser


def main(argv=None) -> int:
    common.utf8_stdout()
    args = build_parser().parse_args(argv)
    home = Path(common.resolve_home(args.home)).resolve()
    token = secrets.token_urlsafe(24)
    stop = threading.Event()
    deadline = Deadline(args.timeout)

    def on_saved(kind: str, path: Path):
        print(f'SAVED {kind} {path}', flush=True)
        deadline.touch()

    def on_close():
        print('CLOSED', flush=True)
        stop.set()

    server = make_server(home, args.port, token, on_saved, on_close)
    url = f'http://127.0.0.1:{server.server_port}/'
    if args.tab:
        url += f'#{args.tab}'
    print(f'OPEN {url}', flush=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if not args.no_open:
        webbrowser.open(url)
    while not stop.wait(0.2):
        if deadline.expired():
            print('TIMEOUT', flush=True)
            break
    server.shutdown()
    server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
