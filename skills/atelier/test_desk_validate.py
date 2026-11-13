"""desk_validate.py のテスト。Studio を oracle にして、同じ入力の判定・文言を比べる。"""
import copy
import datetime
import hashlib
import http.client
import io
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / 'studio'))
import common  # noqa: E402
import desk_validate  # noqa: E402
import serve  # noqa: E402

SCRIPT = HERE / 'desk_validate.py'
NOW = datetime.datetime.fromisoformat('2030-01-02T03:04:05+00:00')

# 固定時計の子プロセスの起動コード。python -B -c BOOT 配布フォルダ ISO時刻 引数... で、common.now を固定してから main を呼ぶ。
BOOT = '''
import datetime, sys
sys.path.insert(0, sys.argv[1])
import common
instant = datetime.datetime.fromisoformat(sys.argv[2])
common.now = lambda: instant
import desk_validate
raise SystemExit(desk_validate.main(sys.argv[3:]))
'''


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


CHEAP = load(HERE / 'examples' / 'templates' / 'cheap-all-tiers.json')
JEV = load(HERE / 'workflows' / 'jev-routed.json')
APPLICATION = {'id': 'a-20291231T000000', 'template': 'cheap-all-tiers',
               'until': '2030-02-01T00:00:00+00:00', 'on_expire': 'ask', 'reason': '',
               'started_at': '2029-12-31T00:00:00+00:00', 'started_via': 'studio'}
AGENT_STEP = {'runner': 'agent', 'model': 'sonnet', 'effort': None}
ROUTING_OK = {'name': 'r', 'tiers': {t: [dict(AGENT_STEP)] for t in ('haiku', 'sonnet', 'opus')}}


def routing_with(first):
    table = copy.deepcopy(ROUTING_OK)
    table['tiers']['haiku'].insert(0, first)
    return table


def iso(seconds):
    return (NOW + datetime.timedelta(seconds=seconds)).isoformat()


def write_home(home, application=None, files=None):
    """Studio のテストに似た home。application があれば overrides.json に置く。files は相対パス -> 文字列かオブジェクト。"""
    def put(name, data):
        path = home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        path.write_text(text, encoding='utf-8')

    put('agents.json', {'runners': {
        'opencode': {'enabled': True, 'models': ['opencode-go/deepseek-v4.1-flash']},
        'codex': {'enabled': True},
        'agy': {'enabled': False},
        'agent': {'enabled': True, 'models': ['opus', 'sonnet', 'haiku']}}})
    put('models/codex.json', {'runner': 'codex', 'models': [{'id': 'gpt-6-astra', 'efforts': ['high']}]})
    put('templates/cheap-all-tiers.json', CHEAP)
    (home / 'components').mkdir()
    if application is not None:
        put('overrides.json', {'synced_at': None, 'updated_at': None, 'application': application})
    for name, data in (files or {}).items():
        put(name, data)


def snapshot(home):
    """名前・種別・内容の SHA-256・更新時刻 (ナノ秒) を、ディレクトリを含めて取る。"""
    state = {}
    for path in sorted(home.rglob('*')):
        stat = path.stat()
        digest = None if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
        state[path.relative_to(home).as_posix()] = (path.is_dir(), digest, stat.st_mtime_ns)
    return state


# (ラベル, KIND, --name, 本文, write_home の引数, Studio が受理するか)
def cases():
    app = dict(template='cheap-all-tiers', until=None, on_expire='ask')
    jev = copy.deepcopy(JEV)
    no_build = copy.deepcopy(JEV)
    no_build['stages']['build'] = []
    bad_gate = copy.deepcopy(JEV)
    bad_gate['gates'] = [{'after': 'route', 'components': ['no-such-component']}]
    many = {'stages': {}, 'fix': {'max_rework': 99}}
    mini = dict(CHEAP, name='mini')
    stuck = dict(APPLICATION, until='not-a-time')
    return [
        ('runner selected list', 'runner', 'opencode', {'selected': ['a']}, {}, True),
        ('runner selected empty', 'runner', 'opencode', {'selected': []}, {}, True),
        ('runner selected null', 'runner', 'opencode', {'selected': None}, {}, True),
        ('runner enabled 0', 'runner', 'agent', {'enabled': 0}, {}, True),
        ('runner broken agents.json', 'runner', 'codex', {'selected': ['x']}, {'files': {'agents.json': '{bad'}}, True),
        ('runner selected blank', 'runner', 'opencode', {'selected': ['']}, {}, False),
        ('runner selected mixed', 'runner', 'opencode', {'selected': ['a', '']}, {}, False),
        ('runner selected number', 'runner', 'opencode', {'selected': [1]}, {}, False),
        ('runner selected string', 'runner', 'opencode', {'selected': 'a'}, {}, False),
        ('runner agent disabled', 'runner', 'agent', {'enabled': False}, {}, False),
        ('model ok', 'model', 'codex', {'id': 'm', 'efforts': ['low']}, {}, True),
        ('model efforts null', 'model', 'codex', {'id': 'm', 'efforts': None}, {}, True),
        ('model no efforts', 'model', 'opencode', {'id': 'm'}, {}, True),
        ('model empty id', 'model', 'codex', {'id': ' '}, {}, False),
        ('model id number', 'model', 'codex', {'id': 1}, {}, False),
        ('model efforts string', 'model', 'codex', {'id': 'm', 'efforts': 'high'}, {}, False),
        ('model broken models file', 'model', 'codex', {'id': 'm', 'efforts': ['low']},
         {'files': {'models/codex.json': '{bad'}}, True),
        ('model broken models file, empty id', 'model', 'codex', {'id': ' '},
         {'files': {'models/codex.json': '{bad'}}, False),
        ('routing agent only', 'routing', None, ROUTING_OK, {}, True),
        ('routing without agents.json', 'routing', None, routing_with({'runner': 'agy', 'model': 'z'}),
         {'remove': 'agents.json'}, True),
        ('routing broken agents.json', 'routing', None, routing_with({'runner': 'agy', 'model': 'z'}),
         {'files': {'agents.json': '{bad'}}, True),
        ('routing selected model', 'routing', None,
         routing_with({'runner': 'opencode', 'model': 'opencode-go/deepseek-v4.1-flash'}), {}, True),
        ('routing unselected model', 'routing', None,
         routing_with({'runner': 'opencode', 'model': 'other'}), {}, False),
        ('routing disabled runner', 'routing', None, routing_with({'runner': 'agy', 'model': 'z'}), {}, False),
        ('routing unknown runner', 'routing', None, routing_with({'runner': 'nope', 'model': 'z'}), {}, False),
        ('routing missing tier', 'routing', None, {'tiers': {'haiku': [dict(AGENT_STEP)]}}, {}, False),
        ('routing empty', 'routing', None, {}, {}, False),
        ('template ok', 'template', 'mini', mini, {}, True),
        ('template name mismatch', 'template', 'mini', CHEAP, {}, False),
        ('template missing tier steps', 'template', 'mini', dict(mini, tiers={'haiku': []}), {}, False),
        ('template japanese problems', 'template', 'mini', {'name': 'mini', 'extra': 1}, {}, False),
        ('workflow bundled definition', 'workflow', 'jev-routed', jev, {}, True),
        ('workflow no body name', 'workflow', 'mine', {k: v for k, v in JEV.items() if k != 'name'}, {}, True),
        ('workflow name mismatch', 'workflow', 'jev-routed', dict(JEV, name='other'), {}, False),
        ('workflow stage empty', 'workflow', 'jev-routed', no_build, {}, False),
        ('workflow unknown gate component', 'workflow', 'jev-routed', bad_gate, {}, False),
        ('workflow many problems', 'workflow', 'jev-routed', many, {}, False),
        ('workflow broken home component', 'workflow', 'jev-routed', jev,
         {'files': {'components/broken.json': '{bad'}}, False),
        ('application ask', 'application', None, app, {}, True),
        ('application none string', 'application', None, dict(app, until='none'), {}, True),
        ('application future', 'application', None, dict(app, until=iso(1), on_expire='drop', reason='r'), {}, True),
        ('application replaces', 'application', None, app, {'application': APPLICATION}, True),
        ('application broken overrides', 'application', None, app, {'files': {'overrides.json': '{bad'}}, True),
        ('application no template', 'application', None, dict(app, template='nope'), {}, False),
        ('application template number', 'application', None, dict(app, template=1), {}, False),
        ('application until number', 'application', None, dict(app, until=5), {}, False),
        ('application bad on_expire', 'application', None, dict(app, on_expire='x'), {}, False),
        ('application reason number', 'application', None, dict(app, reason=3), {}, False),
        ('application past', 'application', None, dict(app, until=iso(-1)), {}, False),
        ('application now', 'application', None, dict(app, until=iso(0)), {}, False),
        ('application no zone', 'application', None, dict(app, until='2031-01-01T00:00:00'), {}, False),
        ('application unparsable', 'application', None, dict(app, until='soon'), {}, False),
        ('application stuck saved until', 'application', None, app, {'application': stuck}, False),
        ('answer stop', 'application-answer', None, {'choice': 'stop'}, {'application': APPLICATION}, True),
        ('answer forever', 'application-answer', None, {'choice': 'forever'}, {'application': APPLICATION}, True),
        ('answer extend', 'application-answer', None, {'choice': 'extend', 'until': iso(1)},
         {'application': APPLICATION}, True),
        ('answer stop expired', 'application-answer', None, {'choice': 'stop'},
         {'application': dict(APPLICATION, until=iso(-5))}, True),
        ('answer no application', 'application-answer', None, {'choice': 'stop'}, {}, False),
        ('answer bad choice', 'application-answer', None, {'choice': 'x'}, {'application': APPLICATION}, False),
        ('answer until number', 'application-answer', None, {'choice': 'extend', 'until': 1},
         {'application': APPLICATION}, False),
        ('answer extend without until', 'application-answer', None, {'choice': 'extend'},
         {'application': APPLICATION}, False),
        ('answer extend past', 'application-answer', None, {'choice': 'extend', 'until': iso(-1)},
         {'application': APPLICATION}, False),
        ('answer extend now', 'application-answer', None, {'choice': 'extend', 'until': iso(0)},
         {'application': APPLICATION}, False),
        ('answer stop stuck saved until', 'application-answer', None, {'choice': 'stop'},
         {'application': stuck}, False),
        ('answer forever stuck saved until', 'application-answer', None, {'choice': 'forever'},
         {'application': stuck}, True),
    ]


CONTRACT_KINDS = ('runner', 'model','routing', 'template', 'workflow', 'application', 'application-answer')

# Studio が 400 で返す検証の文 (契約の文言)。(KIND, --name, 本文, write_home の引数, 文, 文が前置きなら True)
# 前置きの文は、後ろに入力ごとの詳細が付く。--name の誤りと「the model id must not be empty.」は CLI では
# 終了 2 か、手前の検査で届かない文なので、ここには入れない。
STUDIO_MESSAGES = [
    ('runner', 'agent', {'enabled': False}, {}, 'agent cannot be disabled.', False),
    ('runner', 'opencode', {'selected': 'a'}, {}, 'models must be a list of non-empty model id strings.', False),
    ('model', 'codex', {'id': ''}, {}, 'id must be a non-empty string.', False),
    ('model', 'codex', {'id': 'm', 'efforts': 'x'}, {}, 'efforts must be a list.', False),
    ('routing', None, {}, {}, 'the routing table is invalid: ', True),
    ('routing', None, routing_with({'runner': 'agy', 'model': 'z'}), {}, 'agy is not enabled in agents.json.', False),
    ('routing', None, routing_with({'runner': 'opencode', 'model': 'other'}), {},
     'other is not selected for opencode.', False),
    ('template', 'mini', {}, {}, 'the template is not valid: ', True),
    ('workflow', 'jev-routed', dict(JEV, name='x'), {}, 'the name in the body must match the workflow name.', False),
    ('workflow', 'jev-routed', {'name': 'jev-routed'}, {}, 'the workflow is not valid: ', True),
    ('application', None, {'template': 'nope'}, {}, 'the template does not exist or is invalid.', False),
    ('application', None, {'template': 'cheap-all-tiers', 'until': 1, 'on_expire': 'ask'}, {},
     'until must be an ISO time or null.', False),
    ('application', None, {'template': 'cheap-all-tiers', 'on_expire': 'x'}, {},
     'on_expire must be ask or drop.', False),
    ('application', None, {'template': 'cheap-all-tiers', 'on_expire': 'ask', 'reason': 1}, {},
     'reason must be a string.', False),
    ('application', None, {'template': 'cheap-all-tiers', 'until': iso(-1), 'on_expire': 'ask'}, {},
     'the application was not started: ', True),
    ('application-answer', None, {'choice': 'x'}, {}, 'choice must be extend, stop or forever.', False),
    ('application-answer', None, {'choice': 'stop', 'until': 1}, {}, 'until must be an ISO time or null.', False),
    ('application-answer', None, {'choice': 'extend'}, {'application': APPLICATION},
     'until is required to extend.', False),
    ('application-answer', None, {'choice': 'stop'}, {}, 'there is no application to answer.', False),
    ('application-answer', None, {'choice': 'extend', 'until': iso(-1)}, {'application': APPLICATION},
     'the answer was not saved: ', True),
]


class Workspace(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix='validate-')).resolve()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.count = 0

    def new_home(self, **options):
        self.count += 1
        home = self.dir / f'home{self.count}'
        home.mkdir()
        write_home(home, options.get('application'), options.get('files'))
        if options.get('remove'):
            (home / options['remove']).unlink()
        return home

    def input_file(self, body, raw=None):
        self.count += 1
        path = self.dir / f'input{self.count}.json'
        path.write_bytes(raw if raw is not None else json.dumps(body).encode('utf-8'))
        return path

    def cli(self, kind, home, body, name=None):
        """プロセス内で main を呼ぶ。common.now は呼び出し側が差し替える。"""
        argv = [kind, '--home', str(home), '--input', str(self.input_file(body))]
        if name is not None:
            argv += ['--name', name]
        out, err = io.BytesIO(), io.BytesIO()
        code = desk_validate.main(argv, out, err)
        return code, out.getvalue(), err.getvalue()

    def process(self, argv, fixed=None):
        """子プロセスで実行する。fixed (ISO 時刻) があれば、common.now を固定する起動コード BOOT 越し。"""
        command = [sys.executable, '-B']
        command += ['-c', BOOT, str(HERE), fixed] if fixed else [str(SCRIPT)]
        return subprocess.run(command + [str(a) for a in argv], cwd=self.dir, capture_output=True)

    def studio(self, kind, home, body, name=None):
        """同じ内容の別 home の Studio に送り、(status, error か None) を返す。"""
        routes = {'runner': ('PUT', f'/api/agents/{name}'), 'model': ('POST', f'/api/models/{name}'),
                  'routing': ('PUT', '/api/routing'), 'template': ('PUT', f'/api/templates/{name}'),
                  'workflow': ('PUT', f'/api/workflows/{name}'), 'application': ('PUT', '/api/application'),
                  'application-answer': ('POST', '/api/application/answer')}
        method, path = routes[kind]
        server = serve.make_server(home, 0, 'tok')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
            conn.request(method, path, body=json.dumps(body).encode('utf-8'),
                         headers={'Host': f'127.0.0.1:{server.server_port}', 'Content-Type': 'application/json',
                                  'X-Studio-Token': 'tok'})
            response = conn.getresponse()
            data = json.loads(response.read())
            conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(10)
        return response.status, (None if response.status == 200 else data['error'])


class StudioParityTest(Workspace):
    def compare(self, kind, name, body, options, expect_valid, fixed=False):
        cli_home = self.new_home(**options)
        studio_home = self.new_home(**options)
        before = snapshot(cli_home)
        with mock.patch.object(common, 'now', return_value=NOW):
            status, error = self.studio(kind, studio_home, body, name)
            if fixed:
                argv = [kind, '--home', cli_home, '--input', self.input_file(body)]
                argv += ['--name', name] if name else []
                done = self.process(argv, fixed=NOW.isoformat())
                code, stdout, stderr = done.returncode, done.stdout, done.stderr
            else:
                code, stdout, stderr = self.cli(kind, cli_home, body, name)
        self.assertEqual(status, 200 if expect_valid else 400, (kind, body, error))
        result = json.loads(stdout)
        self.assertEqual(stderr, b'')
        self.assertEqual(result, {'schema': 'atelier.validation/1', 'kind': kind, 'valid': expect_valid,
                                  'problems': [] if expect_valid else [error]})
        self.assertEqual(code, 0 if expect_valid else 1)
        self.assertEqual(snapshot(cli_home), before)
        self.assertEqual(stdout.count(b'\n'), 1)
        self.assertTrue(stdout.isascii())
        return result

    def test_every_case_matches_studio(self):
        for label, kind, name, body, options, valid in cases():
            with self.subTest(label):
                self.compare(kind, name, body, options, valid)

    def test_every_kind_has_valid_and_invalid_cases(self):
        seen = {(kind, valid) for _, kind, _, _, _, valid in cases()}
        for kind in CONTRACT_KINDS:
            self.assertIn((kind, True), seen)
            self.assertIn((kind, False), seen)

    def test_time_boundaries_in_a_fixed_clock_process(self):
        template = dict(template='cheap-all-tiers', on_expire='ask')
        for seconds, valid in ((-1, False), (0, False), (1, True)):
            with self.subTest(f'apply {seconds}'):
                self.compare('application', None, dict(template, until=iso(seconds)), {}, valid, fixed=True)
            with self.subTest(f'extend {seconds}'):
                self.compare('application-answer', None, {'choice': 'extend', 'until': iso(seconds)},
                             {'application': APPLICATION}, valid, fixed=True)

    def test_expiry_of_the_saved_application_uses_the_fixed_clock(self):
        for seconds, label in ((-1, 'declined'), (1, 'stopped')):
            with self.subTest(label):
                saved = dict(APPLICATION, until=iso(seconds))
                self.compare('application-answer', None, {'choice': 'stop'}, {'application': saved}, True, fixed=True)
                self.compare('application', None, dict(template='cheap-all-tiers', on_expire='ask'),
                             {'application': saved}, True, fixed=True)

    def test_several_violations_give_the_one_problem_studio_gives(self):
        both_bad = copy.deepcopy(JEV)
        both_bad['stages']['build'] = []
        both_bad['gates'] = [{'after': 'route', 'components': ['no-such-component']}]
        two_bad_steps = routing_with({'runner': 'nope', 'model': 'z'})
        two_bad_steps['tiers']['opus'].insert(0, {'runner': 'agy', 'model': 'z'})
        joined = []  # 問題を ';' で結ぶ KIND は、複数の違反が 1 文の中に残る
        for label, kind, name, body in (
                ('runner', 'runner', 'agent', {'enabled': False, 'selected': 'a'}),
                ('model', 'model', 'codex', {'id': '', 'efforts': 'x'}),
                ('routing', 'routing', None, two_bad_steps),
                ('template', 'template', 'mini', dict(CHEAP, name='other', tiers={'haiku': []}, extra=1)),
                ('workflow', 'workflow', 'jev-routed', both_bad),
                ('application', 'application', None,
                 {'template': 'nope', 'until': 5, 'on_expire': 'x', 'reason': 3}),
                ('answer', 'application-answer', None, {'choice': 'x', 'until': 1})):
            with self.subTest(label):
                result = self.compare(kind, name, body, {}, False)
                self.assertEqual(len(result['problems']), 1)
                if kind in ('template', 'workflow'):
                    joined.append(result['problems'][0])
        for problem in joined:
            self.assertIn('; ', problem)

    def test_japanese_problems_are_escaped_and_preserved(self):
        home = self.new_home()
        argv = ['application', '--home', home, '--input',
                self.input_file(dict(template='cheap-all-tiers', until=iso(-1), on_expire='ask'))]
        done = self.process(argv, fixed=NOW.isoformat())
        self.assertEqual(done.returncode, 1)
        self.assertTrue(done.stdout.isascii())
        problem = json.loads(done.stdout)['problems'][0]
        self.assertIn('the application was not started: ', problem)
        self.assertFalse(problem.isascii())


    def test_content_that_cannot_be_utf8_is_the_error_studio_gives(self):
        """単独サロゲートを含む本文は、Studio では書き込みの UnicodeEncodeError で 400 になる。CLI も同じ文字列 1 つ。"""
        lone = 'x\ud800'
        saved_application = json.dumps({'synced_at': None, 'updated_at': None,
                                        'application': dict(APPLICATION, reason=lone)})
        applying = dict(template='cheap-all-tiers', until=None, on_expire='ask', reason=lone)
        for label, kind, name, body, options, prefix in (
                ('template', 'template', 't2', dict(CHEAP, name='t2', description=lone), {}, ''),
                ('application', 'application', None, applying, {},
                 'the application was not started: '),
                ('application replacing a saved one', 'application', None,
                 dict(template='cheap-all-tiers', until=None, on_expire='ask'),
                 {'files': {'overrides.json': saved_application}}, 'the application was not started: '),
                ('application-answer', 'application-answer', None, {'choice': 'forever'},
                 {'files': {'overrides.json': saved_application}}, 'the answer was not saved: '),
                ('runner', 'runner', 'opencode', {'billing': lone}, {}, ''),
                ('model', 'model', 'codex', {'id': lone}, {}, ''),
                ('routing', 'routing', None, dict(ROUTING_OK, name=lone), {}, '')):
            with self.subTest(label):
                result = self.compare(kind, name, body, options, False)
                problem = result['problems'][0]
                self.assertTrue(problem.startswith(prefix + "'utf-8' codec can't encode character '\\ud800' in "),
                                problem)
                self.assertTrue(problem.endswith(': surrogates not allowed'), problem)


class CallErrorTest(Workspace):
    def failing(self, argv, code, fixed=None):
        done = self.process(argv, fixed)
        self.assertEqual(done.returncode, code, (argv, done.stderr))
        self.assertEqual(done.stdout, b'')
        self.assertTrue(done.stderr.strip())
        self.assertEqual(done.stderr.count(b'\n'), 1)

    def test_exit_codes(self):
        home = self.new_home()
        before = snapshot(home)
        good = self.input_file({})
        base = ['--home', home, '--input', good]
        not_file = self.dir / 'plain-file'
        not_file.write_text('x', encoding='utf-8')
        cases = [
            (['routing', *base, '--name', 'x'], 2),
            (['runner', *base], 2),
            (['runner', *base, '--name', 'unknown'], 2),
            (['model', *base, '--name', 'unknown'], 2),
            (['template', *base], 2),
            (['template', *base, '--name', 'Bad Name'], 2),
            (['workflow', *base, '--name', '../x'], 2),
            (['application', *base, '--name', 'x'], 2),
            (['application-answer', *base, '--name', 'x'], 2),
            (['nope', *base], 2),
            (['routing'], 2),
            (['routing', '--home', 'relative', '--input', good], 2),
            (['routing', '--home', home], 2),
            (['routing', '--bogus', 'x', *base], 2),
            (['routing', '--home', home, '--input', self.input_file(None, b'{bad')], 2),
            (['routing', '--home', home, '--input', self.input_file(None, b'[]')], 2),
            (['routing', '--home', home, '--input', self.input_file(None, b'"text"')], 2),
            (['routing', '--home', home, '--input', self.input_file(None, b'\xef\xbb\xbf{}')], 2),
            (['routing', '--home', home, '--input', self.input_file(None, b'\xff\xfe{}')], 2),
            (['routing', '--home', home, '--input', self.dir / 'missing.json'], 3),
            (['routing', '--home', home, '--input', self.dir], 4),
            (['routing', '--home', not_file, '--input', good], 4),
        ]
        for argv, code in cases:
            with self.subTest(argv=[str(a) for a in argv]):
                self.failing(argv, code)
        self.assertEqual(snapshot(home), before)

    def test_non_json_constants_in_the_body_are_exit_2(self):
        home = self.new_home()
        before = snapshot(home)
        for constant in (b'NaN', b'Infinity', b'-Infinity'):
            for kind, template in (('routing', b'{"x": %s}'), ('template', b'{"steps": [%s]}')):
                argv = [kind, '--home', home, '--input', self.input_file(None, template % constant)]
                if kind == 'template':
                    argv += ['--name', 'sample']
                with self.subTest(kind=kind, constant=constant):
                    self.failing(argv, 2)
        self.assertEqual(snapshot(home), before)

    def test_missing_home_is_an_empty_home_and_is_not_created(self):
        missing = self.dir / 'no-such-home'
        done = self.process(['application', '--home', missing, '--input',
                             self.input_file(dict(template='x', on_expire='ask'))])
        self.assertEqual(done.returncode, 1)
        self.assertEqual(json.loads(done.stdout)['problems'], ['the template does not exist or is invalid.'])
        self.assertFalse(missing.exists())
        done = self.process(['routing', '--home', missing, '--input', self.input_file(ROUTING_OK)])
        self.assertEqual(done.returncode, 0)
        self.assertFalse(missing.exists())

    def test_help_is_ordinary_text(self):
        done = self.process(['--help'])
        self.assertEqual(done.returncode, 0)
        self.assertIn(b'KIND', done.stdout)

    def test_unexpected_exception_is_exit_4_with_empty_stdout(self):
        home = self.new_home()
        with mock.patch.object(desk_validate.select_model, 'routing_error', side_effect=RuntimeError('boom')):
            code, out, err = self.cli('routing', home, ROUTING_OK)
        self.assertEqual((code, out), (4, b''))

    def test_input_over_64_kb_is_still_validated(self):
        """Studio の本文 64 KB の上限は CLI には無い。大きい入力も、小さい入力と同じ判定になる。"""
        home = self.new_home()
        big = 'x' * 100_000
        self.assertGreater(len(big), 64 * 1024)
        code, out, err = self.cli('application', home,
                                  dict(template='cheap-all-tiers', on_expire='ask', reason=big))
        self.assertEqual((code, err), (0, b''))
        self.assertEqual(json.loads(out)['problems'], [])
        code, out, err = self.cli('application', home, dict(template='nope', on_expire='ask', reason=big))
        self.assertEqual((code, err), (1, b''))
        self.assertEqual(json.loads(out)['problems'], ['the template does not exist or is invalid.'])


class NoSideEffectTest(Workspace):
    def guards(self):
        """ネットワークと、書き込みモードの open (Path.write_text なども内部でここを通る) を拒否する。"""
        def deny(*args, **kwargs):
            raise AssertionError('a write or network call was made')

        def writes(mode):
            return any(flag in str(mode) for flag in 'wax+')

        def open_guard(real):
            def guarded(file, mode='r', *args, **kwargs):
                if writes(mode):
                    deny()
                return real(file, mode, *args, **kwargs)
            return guarded

        def path_open_guard(real):
            def guarded(self, mode='r', *args, **kwargs):
                if writes(mode):
                    deny()
                return real(self, mode, *args, **kwargs)
            return guarded

        # Python 3.10 の pathlib は import の時に io.open を束縛するので、io.open の差し替えは Path.write_text に効かない。
        # Path.open も差し替える (Path.write_text などは Path.open を通る)。
        return [mock.patch('socket.socket', deny), mock.patch('socket.create_connection', deny),
                mock.patch('builtins.open', open_guard(open)), mock.patch('io.open', open_guard(io.open)),
                mock.patch.object(pathlib.Path, 'open', path_open_guard(pathlib.Path.open))]

    def test_the_guards_see_a_write_and_a_connection(self):
        target = self.dir / 'probe.txt'
        patches = self.guards()
        for patch in patches:
            patch.start()
        try:
            with self.assertRaises(AssertionError):
                target.write_text('x', encoding='utf-8')
            with self.assertRaises(AssertionError):
                socket.create_connection(('127.0.0.1', 9))
        finally:
            for patch in reversed(patches):
                patch.stop()
        self.assertFalse(target.exists())

    def test_no_write_or_network_call_and_home_is_unchanged(self):
        for label, kind, name, body, options, valid in cases():
            with self.subTest(label):
                home = self.new_home(**options)
                before = snapshot(home)
                path = self.input_file(body)
                argv = [kind, '--home', str(home), '--input', str(path)] + (['--name', name] if name else [])
                out, err = io.BytesIO(), io.BytesIO()
                patches = self.guards()
                for patch in patches:
                    patch.start()
                try:
                    with mock.patch.object(common, 'now', return_value=NOW):
                        code = desk_validate.main(argv, out, err)
                finally:
                    for patch in reversed(patches):
                        patch.stop()
                self.assertEqual(code, 0 if valid else 1, err.getvalue())
                self.assertEqual(snapshot(home), before)

    def test_running_without_dash_b_leaves_no_pycache_in_the_distribution(self):
        """-B なしでも、選んだ版 (配布版の写し) に __pycache__ を作らない。"""
        dist = self.dir / 'dist'
        shutil.copytree(HERE, dist, ignore=shutil.ignore_patterns('__pycache__'))
        home = self.new_home()
        before = snapshot(home)
        env = {key: value for key, value in os.environ.items()
               if key not in ('PYTHONDONTWRITEBYTECODE', 'PYTHONPYCACHEPREFIX')}
        done = subprocess.run([sys.executable, str(dist / 'desk_validate.py'), 'template', '--home', str(home),
                               '--input', str(self.input_file({})), '--name', 't1'],
                              cwd=self.dir, env=env, capture_output=True)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertFalse(json.loads(done.stdout)['valid'])
        self.assertEqual([p for p in dist.rglob('__pycache__')], [])
        self.assertEqual(snapshot(home), before)

    def test_call_errors_leave_the_home_unchanged(self):
        home = self.new_home()
        before = snapshot(home)
        for argv in (['routing', '--home', home, '--input', self.dir / 'missing.json'],
                     ['runner', '--home', home, '--input', self.input_file({})],
                     ['routing', '--home', home, '--input', self.input_file(None, b'{bad')]):
            self.assertIn(self.process(argv).returncode, (2, 3))
        self.assertEqual(snapshot(home), before)


class DriftTest(Workspace):
    """各入力を Studio にも CLI にも送り、Studio の実応答の文が CLI の problems に全文一致で出ることを見る。
    studio/serve.py だけ文言を変えても、desk_validate だけ変えても落ちる。ソースは読まない。"""

    def test_every_studio_message_is_in_the_cli(self):
        for kind, name, body, options, message, is_prefix in STUDIO_MESSAGES:
            with self.subTest(f'{kind}: {message}'):
                cli_home = self.new_home(**options)
                studio_home = self.new_home(**options)
                with mock.patch.object(common, 'now', return_value=NOW):
                    status, error = self.studio(kind, studio_home, body, name)
                    code, out, err = self.cli(kind, cli_home, body, name)
                self.assertEqual(status, 400, (kind, body, error))
                # 契約の文が Studio の応答に今もあること (前置きなら、その後ろに詳細が付く)。
                if is_prefix:
                    self.assertTrue(error.startswith(message), error)
                    self.assertGreater(len(error), len(message))
                else:
                    self.assertEqual(error, message)
                self.assertEqual((code, err), (1, b''))
                result = json.loads(out)
                self.assertFalse(result['valid'])
                # 前置きの文も、コロンの後ろの詳細を含めた全文が Studio と一致する。
                self.assertEqual(result['problems'], [error])


if __name__ == '__main__':
    unittest.main()
