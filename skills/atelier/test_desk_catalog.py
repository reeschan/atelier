"""desk_catalog.py の契約: 引数・schema・終了コード・出力・home を変えないこと。"""
import contextlib
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
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import desk_catalog  # noqa: E402
import flow  # noqa: E402

SCRIPT = HERE / 'desk_catalog.py'
DOC = HERE / 'docs' / 'desk-json.md'


def snapshot(*roots):
    """roots の下のすべての項目 (ディレクトリも) の種別・サイズ・SHA-256・mtime_ns。存在しなければ None。"""
    result = {}
    for root in roots:
        root = Path(root)
        if not os.path.lexists(root):
            result[str(root)] = None
            continue
        for current, directories, files in os.walk(root):
            for name in directories + files:
                path = Path(current) / name
                info = path.lstat()
                digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
                result[str(path)] = ('dir' if path.is_dir() else 'file', info.st_size, digest, info.st_mtime_ns)
    return result


def run(*argv):
    """main をプロセス内で呼ぶ。ネットワークに触れたら失敗する。(終了コード, stdout, stderr)"""
    out, err = io.BytesIO(), io.BytesIO()

    def no_network(*args, **kwargs):
        raise AssertionError('network access')

    with mock.patch.object(socket, 'socket', no_network):
        code = desk_catalog.main([str(a) for a in argv], out=out, err=err)
    return code, out.getvalue(), err.getvalue()


@contextlib.contextmanager
def unreadable(path):
    """path を本当に読めない状態にする (POSIX は mode 000、Windows は共有拒否で開きっぱなし)。"""
    path = Path(path)
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        create = ctypes.windll.kernel32.CreateFileW
        create.restype = wintypes.HANDLE
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                           wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        handle = create(str(path), 0x80000000, 0, None, 3, 0, None)
        if handle in (None, wintypes.HANDLE(-1).value):
            raise OSError(ctypes.get_last_error() or 'cannot lock the file')
        try:
            yield
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    else:
        mode = path.stat().st_mode
        path.chmod(0)
        try:
            if os.access(path, os.R_OK):
                raise unittest.SkipTest('permissions are not enforced for this user')
            yield
        finally:
            path.chmod(mode)


def ok(*argv):
    code, out, err = run(*argv)
    assert code == 0 and not err, (code, err)
    return json.loads(out)


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'home'
        self.home.mkdir()
        self.watched = (self.home, flow.COMPONENTS, flow.BUNDLED_WORKFLOWS)

    def component(self, cid, **changes):
        data = json.loads((flow.COMPONENTS / 'understand.builtin.json').read_text(encoding='utf-8'))
        data.update(id=cid, **changes)
        path = self.home / 'components' / f'{cid}.json'
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        return data

    def workflow(self, name, mutate=None):
        data = json.loads((flow.BUNDLED_WORKFLOWS / 'high-model.json').read_text(encoding='utf-8'))
        if mutate:
            mutate(data)
        path = self.home / 'workflows' / f'{name}.json'
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        return path, data

    def write(self, relative, content):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode('utf-8'))
        return path

    def assertFails(self, command, code, fragment=None):
        before = snapshot(*self.watched)
        got, out, err = run(command, '--home', self.home)
        self.assertEqual((got, out), (code, b''), err)
        self.assertEqual(err.count(b'\n'), 1, err)
        if fragment:
            self.assertIn(fragment, err.decode('utf-8'))
        self.assertEqual(snapshot(*self.watched), before)


class SuccessTest(CatalogTest):
    def test_empty_home_lists_bundled_definitions_in_order(self):
        before = snapshot(*self.watched)
        components = ok('components', '--home', self.home)
        workflows = ok('workflows', '--home', self.home)
        self.assertEqual(components['schema'], 'atelier.components/1')
        self.assertEqual(workflows['schema'], 'atelier.workflows/1')
        ids = [c['id'] for c in components['items']]
        names = [w['name'] for w in workflows['items']]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(names, sorted(names))
        self.assertEqual(len(ids), len(list(flow.COMPONENTS.glob('*.json'))))
        self.assertEqual({w['source'] for w in workflows['items']}, {'bundled'})
        self.assertTrue(all(w['bundled'] for w in workflows['items']))
        self.assertEqual(snapshot(*self.watched), before)
        self.assertFalse((self.home / 'components').exists() or (self.home / 'workflows').exists())

    def test_data_is_the_whole_definition(self):
        items = {c['id']: c for c in ok('components', '--home', self.home)['items']}
        for path in flow.COMPONENTS.glob('*.json'):
            self.assertEqual(items[path.stem]['data'], json.loads(path.read_text(encoding='utf-8')))
            self.assertEqual(items[path.stem]['source'], 'bundled')
        self.assertEqual(items['review.code-review']['data']['link'],
                         json.loads((flow.COMPONENTS / 'review.code-review.json').read_text(encoding='utf-8'))['link'])
        flows = {w['name']: w for w in ok('workflows', '--home', self.home)['items']}
        for path in flow.BUNDLED_WORKFLOWS.glob('*.json'):
            self.assertEqual(flows[path.stem]['data'], json.loads(path.read_text(encoding='utf-8')))

    def test_nonexistent_home_is_an_empty_home(self):
        missing = Path(self.tmp.name) / 'no-such-home'
        before = snapshot(missing, flow.COMPONENTS, flow.BUNDLED_WORKFLOWS)
        self.assertEqual(ok('components', '--home', missing), ok('components', '--home', self.home))
        self.assertEqual(ok('workflows', '--home', missing), ok('workflows', '--home', self.home))
        self.assertEqual(snapshot(missing, flow.COMPONENTS, flow.BUNDLED_WORKFLOWS), before)
        self.assertFalse(missing.exists())

    def test_home_component_is_listed_with_source_home(self):
        data = self.component('custom.note', title={'ja': 'メモ', 'en': 'Note'})
        before = snapshot(*self.watched)
        items = {c['id']: c for c in ok('components', '--home', self.home)['items']}
        self.assertEqual(items['custom.note'], {'id': 'custom.note', 'source': 'home', 'data': data})
        self.assertEqual(items['understand.builtin']['source'], 'bundled')
        self.assertEqual(snapshot(*self.watched), before)

    def test_home_workflow_hides_the_bundled_one(self):
        _, copy = self.workflow('high-model')
        _, own = self.workflow('my-flow', lambda d: d.update(name='my-flow'))
        self.write('workflows/Bad_Name.json', '{}')
        self.write('workflows/readme.txt', 'x')
        before = snapshot(*self.watched)
        flows = {w['name']: w for w in ok('workflows', '--home', self.home)['items']}
        self.assertEqual(flows['high-model'], {'name': 'high-model', 'source': 'home', 'bundled': True, 'data': copy})
        self.assertEqual(flows['my-flow'], {'name': 'my-flow', 'source': 'home', 'bundled': False, 'data': own})
        self.assertNotIn('Bad_Name', flows)
        self.assertEqual(flows['jev-routed']['source'], 'bundled')
        self.assertEqual(list(flows), sorted(flows))
        self.assertEqual(snapshot(*self.watched), before)

    def test_matches_the_studio_listing(self):
        self.component('custom.note')
        self.workflow('high-model')
        self.workflow('my-flow')
        self.write('workflows/Bad_Name.json', '{}')
        sys.path.insert(0, str(HERE / 'studio'))
        try:
            import serve
        finally:
            sys.path.remove(str(HERE / 'studio'))
        state = serve.flow_state(self.home)
        flows = {w['name']: w for w in ok('workflows', '--home', self.home)['items']}
        self.assertEqual({n: (w['source'], w['bundled']) for n, w in flows.items()},
                         {n: (w['source'], w['bundled']) for n, w in state['workflows'].items()})
        components = {c['id']: c['source'] for c in ok('components', '--home', self.home)['items']}
        self.assertEqual(components, {cid: c['source'] for cid, c in state['components'].items()})

    def test_output_is_one_ascii_json_line_independent_of_the_code_page(self):
        self.component('custom.note', title={'ja': '日本語の題名', 'en': 'Note'})
        home = Path(self.tmp.name) / 'ホーム'
        self.home.rename(home)
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONUTF8'}
        env['PYTHONIOENCODING'] = 'ascii'
        runs = [subprocess.run([sys.executable, '-B', str(SCRIPT), 'components', '--home', str(home)],
                               capture_output=True, env=env, cwd=self.tmp.name) for _ in range(2)]
        self.assertEqual(runs[0].returncode, 0, runs[0].stderr)
        self.assertEqual(runs[0].stdout, runs[1].stdout)
        self.assertEqual(runs[0].stderr, b'')
        out = runs[0].stdout
        out.decode('ascii')
        self.assertTrue(out.endswith(b'\n') and out.count(b'\n') == 1 and b'\r' not in out)
        item = next(c for c in json.loads(out)['items'] if c['id'] == 'custom.note')
        self.assertEqual(item['data']['title']['ja'], '日本語の題名')

    def test_items_are_in_code_point_order_across_home_and_bundled(self):
        # 大文字 < '_' < 小文字 < 非 ASCII (ロケールや大文字小文字を無視した並びではない)
        expected = ['B.x', '_x', 'a.x', 'é.x']
        for cid in reversed(expected):
            self.component(cid)
        ids = [c['id'] for c in ok('components', '--home', self.home)['items']]
        self.assertEqual([i for i in ids if i in expected], expected)
        self.assertEqual(ids, sorted(ids, key=lambda text: [ord(ch) for ch in text]))
        self.assertTrue(set(expected) < set(ids) and 'understand.builtin' in ids)

    def test_home_component_with_uppercase_extension_is_listed_like_load_components(self):
        data = self.component('custom.note')
        (self.home / 'components' / 'custom.note.json').unlink()
        self.write('components/custom.note.JSON', json.dumps(data))
        found, errors = flow.load_components(flow.COMPONENTS, self.home)
        self.assertEqual(errors, [])
        listed = {c['id'] for c in ok('components', '--home', self.home)['items']}
        self.assertEqual(listed, set(found))
        self.assertIn('understand.builtin', listed)

    def test_emoji_and_control_characters_round_trip_as_ascii(self):
        title = {'ja': '🎨絵文字\x00\x1f\x7f制御', 'en': '😀 tab\t newline\n quote" backslash\\  '}
        data = self.component('custom.note', title=title)
        code, out, err = run('components', '--home', self.home)
        self.assertEqual((code, err), (0, b''))
        out.decode('ascii')
        self.assertEqual((out.count(b'\n'), out.endswith(b'\n')), (1, True))
        item = next(c for c in json.loads(out)['items'] if c['id'] == 'custom.note')
        self.assertEqual(item['data'], data)
        self.assertEqual(item['data']['title'], title)

    def test_failure_through_a_subprocess_has_empty_stdout(self):
        done = subprocess.run([sys.executable, '-B', str(SCRIPT), 'components', '--home', 'relative'],
                              capture_output=True, cwd=self.tmp.name)
        self.assertEqual((done.returncode, done.stdout), (2, b''))
        self.assertEqual(done.stderr.count(b'\n'), 1)


class BrokenTest(CatalogTest):
    def test_component_problems_exit_4(self):
        cases = {
            'invalid json': ('components/x.json', '{invalid', 'x.json'),
            'not an object': ('components/x.json', '[]', 'x.json'),
            'bom': ('components/x.json', b'\xef\xbb\xbf{}', 'x.json'),
            'bad utf-8': ('components/x.json', b'{"id": "\xff"}', 'x.json'),
            'id mismatch': ('components/x.json', json.dumps({'id': 'y'}), 'x.json'),
        }
        for label, (relative, content, fragment) in cases.items():
            with self.subTest(label):
                path = self.write(relative, content)
                self.assertFails('components', 4, fragment)
                self.assertFails('workflows', 4, fragment)
                path.unlink()

    def test_bundled_id_collision_exits_4(self):
        self.component('understand.builtin')
        self.assertFails('components', 4, 'understand.builtin.json')

    def test_dangling_then_exits_4(self):
        self.component('custom.note', then=['no.such.component'])
        self.assertFails('components', 4, 'no.such.component')

    def test_nan_in_a_definition_exits_4(self):
        data = self.component('custom.note')
        text = json.dumps(data)[:-1] + ', "extra": NaN}'
        self.write('components/custom.note.json', text)
        self.assertFails('components', 4)

    def test_workflow_problems_exit_4(self):
        def break_rework(data):
            data['fix'] = {'max_rework': 99}

        cases = {
            'invalid json': '{invalid',
            'not an object': '[]',
            'bom': b'\xef\xbb\xbf{}',
            'bad utf-8': b'{"name": "\xff"}',
            'nan': '{"name": "a", "x": NaN}',
        }
        for label, content in cases.items():
            with self.subTest(label):
                path = self.write('workflows/my-flow.json', content)
                self.assertFails('workflows', 4, 'my-flow')
                path.unlink()
        with self.subTest('semantic'):
            self.workflow('my-flow', break_rework)
            self.assertFails('workflows', 4, 'max_rework')

    def test_broken_home_flow_does_not_fall_back_to_the_bundled_one(self):
        self.write('workflows/high-model.json', '{invalid')
        self.assertFails('workflows', 4, 'high-model')

    def test_components_still_list_when_only_a_flow_is_broken(self):
        self.write('workflows/my-flow.json', '{invalid')
        self.assertEqual(ok('components', '--home', self.home)['schema'], 'atelier.components/1')

    def test_directory_problems_exit_4(self):
        (self.home / 'components' / 'x.json').mkdir(parents=True)
        self.assertFails('components', 4, 'x.json')
        (self.home / 'components' / 'x.json').rmdir()
        (self.home / 'components').rmdir()
        self.write('components', 'a file')
        self.assertFails('components', 4)
        (self.home / 'components').unlink()
        (self.home / 'workflows' / 'my-flow.json').mkdir(parents=True)
        self.assertFails('workflows', 4, 'my-flow.json')

    def test_home_that_is_a_file_exits_4(self):
        target = Path(self.tmp.name) / 'file-home'
        target.write_text('x', encoding='utf-8')
        before = snapshot(target)
        code, out, err = run('components', '--home', target)
        self.assertEqual((code, out), (4, b''))
        self.assertTrue(err)
        self.assertEqual(snapshot(target), before)

    def test_read_failure_exits_4_not_1(self):
        self.component('custom.note')
        self.component('custom.locked')
        self.assertEqual(ok('components', '--home', self.home)['schema'], 'atelier.components/1')
        before = snapshot(*self.watched)
        with unreadable(self.home / 'components' / 'custom.locked.json'):
            for command in ('components', 'workflows'):
                with self.subTest(command):
                    code, out, err = run(command, '--home', self.home)
                    self.assertEqual((code, out), (4, b''))
                    self.assertEqual(err.count(b'\n'), 1, err)
        self.assertEqual(snapshot(*self.watched), before)
        self.assertEqual(ok('components', '--home', self.home)['schema'], 'atelier.components/1')

    def test_unexpected_error_exits_4_not_1(self):
        with mock.patch.object(flow, 'component_error', side_effect=RuntimeError('boom')):
            code, out, err = run('components', '--home', self.home)
        self.assertEqual((code, out), (4, b''))
        self.assertEqual(err.count(b'\n'), 1, err)

    def test_broken_component_also_fails_the_workflow_list(self):
        """部品の誤りが 1 件でもあれば、workflows も一覧全体が終了 4 (components と同じ)。"""
        cases = {
            'dangling then': (lambda: self.component('custom.note', then=['no.such.component']), 'no.such.component'),
            'bundled id collision': (lambda: self.component('understand.builtin'), 'understand.builtin.json'),
        }
        for label, (make, fragment) in cases.items():
            with self.subTest(label):
                make()
                self.assertFails('workflows', 4, fragment)
                for path in (self.home / 'components').glob('*.json'):
                    path.unlink()

    def test_nan_in_a_component_also_fails_the_workflow_list(self):
        data = self.component('custom.note')
        self.write('components/custom.note.json', json.dumps(data)[:-1] + ', "extra": NaN}')
        self.assertFails('workflows', 4)


class ComponentRulesParityTest(CatalogTest):
    """desk_catalog._components は flow.load_components の規則の写し。id の集合と誤りの有無・内容が一致すること。"""

    def assertParity(self):
        found, errors = flow.load_components(flow.COMPONENTS, self.home)
        try:
            mine = desk_catalog._components(self.home)
        except desk_catalog._CatalogError as error:
            self.assertTrue(errors, error.message)
            if error.message.startswith('invalid components:'):  # 厳密な読みで先に落ちたものは別の文面
                self.assertIn(errors[0], error.message)
            return
        self.assertFalse(errors, errors)
        self.assertEqual(set(mine), set(found))
        self.assertEqual({cid: data for cid, (_, data) in mine.items()}, found)

    def test_bundled_only(self):
        self.assertParity()

    def test_valid_home_components(self):
        self.component('custom.note')
        self.component('custom.other', then=['custom.note'])
        self.assertParity()

    def test_each_kind_of_mistake(self):
        cases = {
            'id mismatch': lambda: self.write('components/custom.note.json', json.dumps({'id': 'other'})),
            'bundled id collision': lambda: self.component('understand.builtin'),
            'dangling then': lambda: self.component('custom.note', then=['no.such.component']),
            'bad stages': lambda: self.component('custom.note', stages=[]),
            'not an object': lambda: self.write('components/custom.note.json', '[]'),
        }
        for label, make in cases.items():
            with self.subTest(label):
                make()
                self.assertParity()
                for path in (self.home / 'components').glob('*.json'):
                    path.unlink()

    def test_output_comes_from_the_strictly_read_content(self):
        """読みの間に部品ファイルが変わっても、出力は厳密に読んだ内容 (または終了 4) で、読み直した内容ではない。"""
        self.component('custom.note', description='first')
        target = self.home / 'components' / 'custom.note.json'
        changed = target.read_bytes().replace(b'first', b'second')
        original = flow.load_components

        def swapped(*args, **kwargs):
            target.write_bytes(changed)
            return original(*args, **kwargs)

        with mock.patch.object(flow, 'load_components', swapped):
            code, out, err = run('components', '--home', self.home)
        if code == 0:
            items = {item['id']: item['data'] for item in json.loads(out)['items']}
            self.assertEqual(items['custom.note']['description'], 'first')
        else:
            self.assertEqual((code, out), (4, b''), err)


class UsageTest(CatalogTest):
    def test_bad_arguments_exit_2_with_empty_stdout(self):
        cases = [(), ('unknown', '--home', self.home), ('components',), ('components', '--hom', self.home),
                 ('components', '--home', 'relative/home'), ('components', '--home', self.home, 'extra'),
                 ('components', '--home'), ('components', '--home', self.home, '--bogus')]
        for argv in cases:
            with self.subTest(argv=argv):
                before = snapshot(*self.watched)
                code, out, err = run(*argv)
                self.assertEqual((code, out), (2, b''))
                self.assertEqual(err.count(b'\n'), 1, err)
                self.assertEqual(snapshot(*self.watched), before)

    def test_help_exits_0(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code, out, err = run('--help')
        self.assertEqual((code, out, err), (0, b'', b''))
        self.assertIn('components', stdout.getvalue())


class NoBytecodeTest(CatalogTest):
    def test_run_without_dash_B_leaves_no_pycache_in_the_distribution(self):
        # 配布版を一時フォルダに写し、-B なし・PYTHONDONTWRITEBYTECODE なしで CLI を動かす
        with tempfile.TemporaryDirectory() as parent:
            copy = Path(parent) / 'atelier'
            shutil.copytree(HERE, copy, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            env = {key: value for key, value in os.environ.items()
                   if key not in ('PYTHONDONTWRITEBYTECODE', 'PYTHONPYCACHEPREFIX')}
            for command in ('components', 'workflows'):
                with self.subTest(command=command):
                    done = subprocess.run([sys.executable, str(copy / 'desk_catalog.py'), command,
                                           '--home', str(self.home)],
                                          capture_output=True, env=env, timeout=120)
                    self.assertEqual(done.returncode, 0, done.stderr)
            found = [str(p.relative_to(copy)) for p in copy.rglob('__pycache__')]
            self.assertEqual(found, [])


class ContractTest(CatalogTest):
    def test_documentation_covers_every_desk_cli(self):
        text = DOC.read_text(encoding='utf-8')
        for fragment in ('atelier.state/1', 'atelier.validation/1', 'atelier.save-plan/1',
                         'desk_state.py', 'desk_validate.py', 'desk_plan.py',
                         'template-delete', 'example-copy', 'workflow-delete', 'application-expire'):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)

    def test_documentation_lists_every_schema(self):
        text = DOC.read_text(encoding='utf-8')
        schemas = {command: ok(command, '--home', self.home)['schema'] for command in ('components', 'workflows')}
        self.assertEqual(schemas, {'components': 'atelier.components/1', 'workflows': 'atelier.workflows/1'})
        for schema in schemas.values():
            self.assertIn(schema, text)


if __name__ == '__main__':
    unittest.main()
