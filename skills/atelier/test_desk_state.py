"""desk_state.py の契約: 引数・schema・終了コード・出力・home を変えないこと・ネットワークを使わないこと。"""
import datetime
import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import webbrowser
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import common  # noqa: E402
import desk_state  # noqa: E402
import serve  # noqa: E402
import templates  # noqa: E402

SCRIPT = HERE / 'desk_state.py'
BASE = HERE.parents[2] / '.atelier-tmp'  # 一時領域は work root の中だけに作る
NOW = datetime.datetime(2026, 6, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)
BEFORE = (NOW - datetime.timedelta(days=1)).isoformat()
AFTER = (NOW + datetime.timedelta(days=1)).isoformat()
ENV_DROP = ('ATELIER_HOME', 'PBI_HOME', 'PBI_TEMPLATES_HOME', 'XDG_CONFIG_HOME')


def snapshot(root):
    """root とその下のすべての項目 (ディレクトリも) の種別・サイズ・SHA-256・mtime_ns。存在しなければ None。"""
    root = Path(root)
    if not os.path.lexists(root):
        return None
    result = {'.': ('dir', root.lstat().st_mtime_ns)}
    for current, directories, files in os.walk(root):
        for name in directories + files:
            path = Path(current) / name
            info = path.lstat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            result[str(path.relative_to(root))] = ('dir' if path.is_dir() else 'file', info.st_size, digest,
                                                   info.st_mtime_ns)
    return result


def _forbidden(name):
    def raiser(*args, **kwargs):
        raise AssertionError(f'{name} must not be called')
    return raiser


def run(*argv, now=NOW):
    """main をプロセス内で呼ぶ。ネットワーク・外部プロセス・ブラウザ・書き込み関数に触れたら失敗する。(終了コード, stdout, stderr)"""
    out, err = io.BytesIO(), io.BytesIO()
    patches = [mock.patch.object(socket, 'socket', _forbidden('socket.socket')),
               mock.patch.object(socket, 'create_connection', _forbidden('socket.create_connection')),
               mock.patch.object(subprocess, 'Popen', _forbidden('subprocess.Popen')),
               mock.patch.object(webbrowser, 'open', _forbidden('webbrowser.open')),
               mock.patch.object(common, 'write_json', _forbidden('common.write_json')),
               mock.patch.object(templates, 'save_overrides', _forbidden('templates.save_overrides')),
               mock.patch.object(templates, 'append_history', _forbidden('templates.append_history'))]
    for patch in patches:
        patch.start()
    try:
        code = desk_state.main([str(a) for a in argv], out=out, err=err, now=now)
    finally:
        for patch in reversed(patches):
            patch.stop()
    return code, out.getvalue(), err.getvalue()


def process(*argv, env=None, extra_env=None):
    """スクリプトを別プロセスで実行する。親の home 関連の環境変数は消す。"""
    base = {k: v for k, v in os.environ.items() if k not in ENV_DROP}
    base.update(extra_env or {})
    result = subprocess.run([sys.executable, '-B', str(SCRIPT), *map(str, argv)], cwd=HERE, env=env or base,
                            capture_output=True)
    return result.returncode, result.stdout, result.stderr


def template(name, description='d'):
    step = {'runner': 'agent', 'model': 'sonnet'}
    return {'name': name, 'description': description, 'tiers': {tier: [step] for tier in common.TIERS}}


class StateTest(unittest.TestCase):
    def setUp(self):
        BASE.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=BASE)
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'home'
        self.home.mkdir()

    def put(self, relative, content):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, (dict, list)):
            content = json.dumps(content, ensure_ascii=False)
        path.write_bytes(content if isinstance(content, bytes) else content.encode('utf-8'))
        return path

    def application(self, until, on_expire='ask', name='alpha'):
        return {'id': 'a-20260101T000000', 'template': name, 'until': until, 'on_expire': on_expire,
                'reason': 'r', 'started_at': BEFORE, 'started_via': 'test'}

    def full_home(self, until=AFTER, on_expire='ask'):
        self.put('agents.json', {'runners': {'opencode': {'enabled': True, 'path': 'x', 'version': '1',
                                                          'billing': 'api', 'models': ['m1', 'gone']}}})
        self.put('models/opencode.json', {'fetched_at': 'f', 'method': 'cli',
                                          'models': [{'id': 'm1', 'name': 'M1', 'efforts': ['high'],
                                                      'price_in': 1, 'price_out': 2}, {'id': 'm2'}]})
        self.put('routing.json', json.loads((HERE / 'routing.default.json').read_text(encoding='utf-8')))
        self.put('templates/alpha.json', template('alpha', '日本語の説明'))
        self.put('overrides.json', {'synced_at': None, 'updated_at': BEFORE,
                                    'application': self.application(until, on_expire)})

    def expected(self):
        state = serve.state(self.home, serve.load_catalog(), NOW)
        state.pop('flow')
        return json.loads(json.dumps({'schema': 'atelier.state/1', **state}))

    def ok(self, *argv, **kwargs):
        before = snapshot(self.home)
        code, out, err = run(*argv, **kwargs)
        self.assertEqual((code, err), (0, b''), err)
        self.assertEqual(snapshot(self.home), before)
        self.assertTrue(out.endswith(b'\n') and out.count(b'\n') == 1 and b'\r' not in out)
        out.decode('ascii')
        return json.loads(out)

    def fails(self, expected_code, *argv, **kwargs):
        before = snapshot(self.home)
        code, out, err = run(*argv, **kwargs)
        self.assertEqual(code, expected_code, err)
        self.assertEqual(out, b'')
        self.assertTrue(err.endswith(b'\n') and err.count(b'\n') == 1 and err.strip(), err)
        self.assertEqual(snapshot(self.home), before)
        return err.decode('utf-8')

    # --- 正常系 ---

    def test_empty_home_returns_contract_keys(self):
        state = self.ok('--home', self.home)
        self.assertEqual(set(state), {'schema', 'home', 'runners', 'routing', 'templates', 'examples',
                                      'application', 'application_status', 'warnings'})
        self.assertEqual(state['schema'], 'atelier.state/1')
        self.assertEqual(Path(state['home']), self.home)
        self.assertEqual(set(state['runners']), set(common.RUNNERS))
        self.assertEqual(state['application'], None)
        self.assertEqual(state['application_status'], 'none')
        self.assertEqual(state['warnings'], [])
        self.assertEqual(state, self.expected())

    def test_state_keys_follow_studio_without_flow(self):
        full = serve.state(self.home, serve.load_catalog(), NOW)
        self.assertEqual(set(desk_state.STATE_KEYS), set(full) - {'flow'})

    def test_populated_home_matches_studio_state(self):
        self.full_home()
        state = self.ok('--home', self.home)
        self.assertEqual(state, self.expected())
        self.assertEqual(state['application_status'], 'active')
        self.assertEqual(state['templates']['alpha']['description'], '日本語の説明')
        self.assertEqual(state['runners']['opencode']['selected'], ['m1', 'gone'])
        self.assertEqual(state['runners']['opencode']['missing_selected'], ['gone'])
        self.assertEqual(state['runners']['opencode']['models']['list'][0]['ref']['source'], 'cli')
        self.assertTrue(any('gone' in w for w in state['warnings']))
        self.assertIn('cheap-all-tiers', state['examples'])

    def test_expired_drop_is_reported_without_writing(self):
        self.full_home(until=BEFORE, on_expire='drop')
        state = self.ok('--home', self.home)
        self.assertEqual(state['application_status'], 'expired_drop')
        self.assertEqual(state['application']['id'], 'a-20260101T000000')
        self.assertFalse((self.home / 'history.jsonl').exists())
        self.assertEqual(json.loads((self.home / 'overrides.json').read_text(encoding='utf-8'))['application']['until'],
                         BEFORE)

    def test_expired_drop_adds_no_cleanup_warning(self):
        self.full_home(until=AFTER, on_expire='drop')
        active = self.ok('--home', self.home)['warnings']
        self.full_home(until=BEFORE, on_expire='drop')
        warnings = self.ok('--home', self.home)['warnings']
        self.assertEqual(warnings, active)
        self.assertEqual(warnings, ['opencode: selected model gone is not in the model list'])

    def test_expired_ask_and_boundary(self):
        self.full_home(until=BEFORE, on_expire='ask')
        self.assertEqual(self.ok('--home', self.home)['application_status'], 'expired_ask')
        self.full_home(until=NOW.isoformat(), on_expire='ask')
        self.assertEqual(self.ok('--home', self.home)['application_status'], 'expired_ask')
        self.full_home(until=(NOW + datetime.timedelta(seconds=1)).isoformat())
        self.assertEqual(self.ok('--home', self.home)['application_status'], 'active')
        self.full_home(until=None)
        self.assertEqual(self.ok('--home', self.home)['application_status'], 'active')

    def test_unknown_template_is_a_warning(self):
        self.full_home()
        (self.home / 'templates' / 'alpha.json').unlink()
        state = self.ok('--home', self.home)
        self.assertEqual(state['application_status'], 'unknown_template')
        self.assertIn('the applied template is missing or invalid.', state['warnings'])

    def test_nonexistent_home_is_empty_and_not_created(self):
        missing = Path(self.tmp.name) / 'a' / 'b' / 'home'
        code, out, err = run('--home', missing)
        self.assertEqual((code, err), (0, b''))
        state = json.loads(out)
        self.assertEqual(Path(state['home']), missing)
        self.assertEqual(state['application_status'], 'none')
        self.assertFalse((Path(self.tmp.name) / 'a').exists())

    def test_semantic_errors_exit_zero_with_studio_values(self):
        self.put('agents.json', [1, 2])
        self.put('routing.json', {'tiers': 'bad'})
        self.put('overrides.json', '[]')
        self.put('models/codex.json', '"text"')
        self.put('templates/bad.json', {'name': 'other'})
        self.put('templates/Bad Name.json', '{ not json')
        self.put('templates/readme.txt', 'x')
        state = self.ok('--home', self.home)
        self.assertEqual(state['routing']['source'], 'default')
        self.assertIn('routing.json is invalid; the bundled default is used.', state['warnings'])
        self.assertEqual(state['templates'], {})
        self.assertEqual(state['application_status'], 'none')
        self.assertEqual(state, self.expected())

    def test_template_with_uppercase_extension_is_checked_like_load_all(self):
        # Studio (templates.load_all) は glob('*.json') で読む。Windows など大文字小文字を区別しない FS では foo.JSON も読む
        path = self.put('templates/foo.JSON', b'{')
        reads = (self.home / 'templates' / 'foo.json').exists()  # load_all が foo.JSON を読む FS か
        self.assertEqual(reads, [p.name for p in (self.home / 'templates').glob('*.json')] == ['foo.JSON'])
        if reads:
            message = self.fails(4, '--home', self.home)
            self.assertIn('templates/foo.json is not readable JSON', message)
        else:
            self.ok('--home', self.home)
        path.unlink()

    def test_directory_named_like_a_template_exits_four(self):
        (self.home / 'templates' / 'x.json').mkdir(parents=True)
        message = self.fails(4, '--home', self.home)
        self.assertIn('templates/x.json', message)

    def test_files_outside_the_state_files_do_not_change_the_result(self):
        self.full_home()
        clean = self.ok('--home', self.home)
        self.put('history.jsonl', b'\xff not json at all')
        self.put('notes.json', '{ broken')
        self.put('models/other.json', '{ broken')
        self.put('templates/readme.txt', b'\xff')
        self.put('templates/Not_Valid.json', b'\xff')
        self.assertEqual(self.ok('--home', self.home), clean)

    # --- 異常系 ---

    def test_relative_home_and_bad_arguments_exit_two(self):
        for argv in (['--home', 'relative/home'], ['--home', ''], ['--home'], ['--nope'], ['extra'],
                     ['--home', str(self.home), '--home2', 'x']):
            with self.subTest(argv=argv), mock.patch('sys.stderr', io.StringIO()):
                self.fails(2, *argv)

    def test_home_that_is_a_file_exits_four(self):
        path = self.put('file-home', 'x')
        message = self.fails(4, '--home', path)
        self.assertIn('not a directory', message)

    def test_unreadable_json_exits_four_for_every_state_file(self):
        names = ['agents.json', 'routing.json', 'overrides.json', 'models/opencode.json', 'models/agent.json',
                 'templates/alpha.json']
        bad = {'syntax': b'{ "a": ', 'empty': b'', 'bom': b'\xef\xbb\xbf{}', 'utf8': b'\xff\xfe{}',
               'nan': b'{"a": NaN}'}
        for name in names:
            for label, content in bad.items():
                with self.subTest(name=name, label=label):
                    self.put(name, content)
                    message = self.fails(4, '--home', self.home)
                    self.assertIn(name, message)
                    (self.home / name).unlink()

    def test_directory_in_place_of_a_state_file_exits_four(self):
        (self.home / 'agents.json').mkdir()
        self.assertIn('agents.json', self.fails(4, '--home', self.home))

    def test_invalid_template_name_with_broken_json_is_ignored(self):
        self.put('templates/Not_Valid.json', b'\xff')
        self.ok('--home', self.home)

    def test_exceptions_in_studio_state_exit_four(self):
        for failure in (SystemExit('routing.default.json: broken'), TypeError('unhashable'), ValueError('x')):
            with self.subTest(failure=failure), mock.patch.object(serve, 'state', side_effect=failure):
                self.fails(4, '--home', self.home)

    def test_unencodable_state_exits_four(self):
        full = serve.state(self.home, serve.load_catalog(), NOW)
        full['warnings'] = [float('nan')]
        with mock.patch.object(serve, 'state', return_value=full):
            self.fails(4, '--home', self.home)

    def test_failures_exit_two_or_four_never_three(self):
        def file_home():
            return self.put('file-home', 'x')

        def broken_json():
            self.put('routing.json', '{ broken')
            return self.home

        def directory_state_file():
            (self.home / 'agents.json').mkdir()
            return self.home

        def directory_template():
            (self.home / 'templates' / 'x.json').mkdir(parents=True)
            return self.home

        cases = [('relative home', 2, lambda: 'relative/home'), ('unknown option', 2, None),
                 ('home is a file', 4, file_home), ('broken json', 4, broken_json),
                 ('directory in place of a state file', 4, directory_state_file),
                 ('directory named like a template', 4, directory_template)]
        for label, expected, make in cases:
            with self.subTest(label=label), mock.patch('sys.stderr', io.StringIO()):
                if make is None:
                    argv = ['--nope']
                else:
                    argv = ['--home', make()]
                before = snapshot(self.home)
                code, out, err = run(*argv)
                self.assertEqual(code, expected, err)
                self.assertNotEqual(code, 3)
                self.assertIn(code, {2, 4})
                self.assertEqual((out, snapshot(self.home)), (b'', before))
                for path in (self.home / 'agents.json', self.home / 'routing.json', self.home / 'templates'):
                    if path.is_dir():
                        shutil.rmtree(path)
                    elif path.exists():
                        path.unlink()

    # --- 別プロセス ---

    def test_process_success_contract(self):
        self.full_home(until=BEFORE, on_expire='drop')
        before = snapshot(self.home)
        code, out, err = process('--home', self.home)
        self.assertEqual((code, err), (0, b''))
        self.assertTrue(out.endswith(b'\n') and out.count(b'\n') == 1)
        state = json.loads(out.decode('ascii'))
        self.assertEqual(state['schema'], 'atelier.state/1')
        self.assertEqual(state['application_status'], 'expired_drop')
        self.assertEqual(snapshot(self.home), before)

    def test_process_failures(self):
        for argv, expected in ((['--home', 'relative'], 2), (['--bogus'], 2)):
            with self.subTest(argv=argv):
                code, out, err = process(*argv)
                self.assertEqual((code, out), (expected, b''))
                self.assertTrue(err.strip())
        self.put('routing.json', '{ broken')
        before = snapshot(self.home)
        code, out, err = process('--home', self.home)
        self.assertEqual((code, out), (4, b''))
        self.assertIn(b'routing.json', err)
        self.assertEqual(snapshot(self.home), before)

    def test_process_help_exits_zero(self):
        code, out, err = process('--help')
        self.assertEqual(code, 0)
        self.assertIn(b'--home', out)

    def test_process_default_home_uses_atelier_rule(self):
        target = Path(self.tmp.name) / 'env-home'
        target.mkdir()
        self.put('env-home/overrides.json', {'application': self.application(AFTER)})
        before = snapshot(target)
        code, out, err = process(extra_env={'ATELIER_HOME': str(target)})
        self.assertEqual((code, err), (0, b''))
        self.assertEqual(Path(json.loads(out)['home']).resolve(), target.resolve())
        self.assertEqual(snapshot(target), before)

    def test_process_default_home_from_xdg_is_not_created(self):
        base = Path(self.tmp.name) / 'xdg'
        code, out, err = process(extra_env={'XDG_CONFIG_HOME': str(base), 'HOME': self.tmp.name,
                                            'USERPROFILE': self.tmp.name})
        self.assertEqual((code, err), (0, b''))
        self.assertEqual(Path(json.loads(out)['home']).resolve(), (base / 'atelier').resolve())
        self.assertFalse(base.exists())

    def test_process_without_dash_B_writes_no_pycache_into_distribution(self):
        # -B なしで実行しても、配布版 (studio を含む) に __pycache__ を作らない。写しと home は BASE (work root 内の .atelier-tmp) の一時領域に作り、終わりに消す
        BASE.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=BASE) as outer:
            copy = Path(outer) / 'atelier'
            shutil.copytree(HERE, copy, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            home = Path(outer) / 'home'
            home.mkdir()
            env = {k: v for k, v in os.environ.items() if k not in ENV_DROP + ('PYTHONDONTWRITEBYTECODE',)}
            result = subprocess.run([sys.executable, str(copy / 'desk_state.py'), '--home', str(home)],
                                    cwd=outer, env=env, capture_output=True)
            self.assertEqual((result.returncode, result.stderr), (0, b''))
            self.assertEqual(json.loads(result.stdout)['schema'], 'atelier.state/1')
            self.assertEqual(sorted(p.relative_to(copy).as_posix() for p in copy.rglob('__pycache__')), [])
            self.assertEqual(list(home.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
