"""desk_plan.py の試験。正常・異常・副作用なし・既存の保存とのバイト一致・依存の網羅を確かめる。"""
import builtins
import contextlib
import copy
import dataclasses
import datetime
import functools
import hashlib
import io
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / 'studio'))
import common  # noqa: E402
import desk_plan  # noqa: E402
import desk_validate  # noqa: E402
import flow  # noqa: E402
import serve  # noqa: E402
import setup_agents  # noqa: E402
import templates  # noqa: E402

PLAN = HERE / 'desk_plan.py'
JST = datetime.timezone(datetime.timedelta(hours=9))
NOW = datetime.datetime(2026, 5, 1, 12, 0, tzinfo=JST)
FUTURE = '2026-06-01T00:00:00+09:00'
PAST = '2026-04-01T00:00:00+09:00'
EMPTY_SHA = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
EXAMPLE = json.loads((HERE / 'examples' / 'templates' / 'cheap-all-tiers.json').read_text(encoding='utf-8'))
WORKFLOW = json.loads((HERE / 'workflows' / 'high-model.json').read_text(encoding='utf-8'))
ROUTING = {'tiers': {tier: [{'runner': 'agent', 'model': 'm'}] for tier in common.TIERS}}


def application(until, on_expire='ask'):
    return {'id': 'old', 'template': 'cheap', 'until': until, 'on_expire': on_expire, 'reason': 'r',
            'started_at': '2026-03-01T00:00:00+09:00', 'started_via': 'studio'}


def is_junction(path) -> bool:
    """os.path.isjunction は 3.12 から。3.10 でも動くよう、無いときは lstat の reparse tag で見分ける。"""
    check = getattr(os.path, 'isjunction', None)
    if check is not None:
        return check(path)
    tag = getattr(stat, 'IO_REPARSE_TAG_MOUNT_POINT', 0xA0000003)
    return getattr(os.lstat(path), 'st_reparse_tag', 0) == tag


def tree(root: Path) -> dict:
    """名前・種別・バイトの木 (リンクは辿らない)。"""
    result = {}
    for directory, names, files in os.walk(root, followlinks=False):
        for name in names + files:
            path = Path(directory) / name
            info = os.lstat(path)
            rel = path.relative_to(root).as_posix()
            if stat.S_ISREG(info.st_mode):
                result[rel] = ('file', path.read_bytes())
            else:
                linked = os.path.islink(path) or is_junction(path)
                result[rel] = ('dir' if stat.S_ISDIR(info.st_mode) and not linked else 'link',)
    return result


def refuse(*_args, **_kwargs):
    raise AssertionError('a side effect was attempted during planning')


def refuse_open(real):
    def opener(file, mode='r', *args, **kwargs):
        if any(flag in str(mode) for flag in 'wax+'):
            refuse()
        return real(file, mode, *args, **kwargs)
    return opener


@contextlib.contextmanager
def guard():
    """計画の間、書き込み・mkdir・置換・削除・ネットワーク・外部プロセスを失敗にする。"""
    with contextlib.ExitStack() as stack:
        for target in ('socket.socket', 'subprocess.Popen', 'os.replace', 'os.mkdir', 'os.rename', 'os.remove',
                       'os.unlink', 'common.write_json', 'templates.append_history', 'templates.save_overrides',
                       'setup_agents.model_fetch'):
            stack.enter_context(mock.patch(target, refuse))
        stack.enter_context(mock.patch('builtins.open', refuse_open(builtins.open)))
        stack.enter_context(mock.patch('io.open', refuse_open(io.open)))
        yield


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='desk-plan-')).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / 'home'

    def put(self, home: Path, rel: str, data, raw: bool = False):
        path = home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            path.write_text(data if raw else json.dumps(data, indent=2), encoding='utf-8', newline='')

    def make_home(self, home: Path):
        self.put(home, 'agents.json', {'runners': {'agent': {'enabled': True},
                                                   'opencode': {'enabled': True, 'models': ['m1']}}})
        self.put(home, 'templates/cheap.json', dict(EXAMPLE, name='cheap'))

    def cli(self, *args, body=None, input_bytes=None, home=None):
        """(終了コード, stdout, stderr)。KIND と --name は args に渡す。"""
        source = self.tmp / 'input.json'
        source.write_bytes(input_bytes if input_bytes is not None else json.dumps(body or {}).encode('utf-8'))
        command = [sys.executable, '-B', str(PLAN), *args, '--home', str(home or self.home), '--input', str(source)]
        done = subprocess.run(command, capture_output=True, cwd=self.tmp, timeout=120)
        return done.returncode, done.stdout, done.stderr

    def run_cli(self, *args, **kwargs):
        """終了コード付きで CLI を走らせ、home の前後の木が同じことも確かめる。"""
        self.home.mkdir(exist_ok=True)
        before = tree(self.home)
        out = self.cli(*args, **kwargs)
        self.assertEqual(tree(self.home), before)
        return out

    def plan_ok(self, *args, **kwargs) -> dict:
        """CLI が終了 0・stderr 空で返した計画。"""
        code, out, err = self.run_cli(*args, **kwargs)
        self.assertEqual((code, err), (0, b''))
        return json.loads(out)

    def apply(self, home: Path, result: dict):
        for write in result['writes']:
            path = home / write['path']
            if write['op'] == 'delete':
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(write['content_utf8'].encode('utf-8'))


COVERED_KINDS = set()  # covers() がクラス定義時に集める。テストの実行順や -k に依存しない


def covers(*kinds):
    """このテストが OracleTest.check で確かめる KIND を宣言する。実際に走らせた KIND と食い違えば落とす。"""
    def wrap(test):
        COVERED_KINDS.update(kinds)

        @functools.wraps(test)
        def run(self):
            self.ran = set()
            test(self)
            self.assertEqual(self.ran, set(kinds))
        return run
    return wrap


class OracleTest(Base):
    """計画を適用した木が、既存の保存関数を実行した木とバイト単位で一致する。副作用なしも確かめる。"""

    def check(self, kind, body, name, setup, legacy, paths):
        a, b = self.tmp / 'a', self.tmp / 'b'
        for home in (a, b):
            shutil.rmtree(home, ignore_errors=True)
            self.make_home(home)
            setup(home)
        before_a, before_atelier = tree(a), tree(HERE)
        with guard():
            result = desk_plan.plan(kind, a, body, name, NOW)
        self.assertEqual(tree(a), before_a)
        self.assertEqual(tree(HERE), before_atelier)
        self.assertEqual([w['path'] for w in result['writes']], paths)
        self.assertEqual(result['schema'], 'atelier.save-plan/1')
        self.assertEqual(result['evaluated_at'], NOW.isoformat())
        file_reads = {dep['path']: dep for dep in result['reads'] if dep['type'] == 'file' and dep['root'] == 'home'}
        for write in result['writes']:
            self.assertIn(write['path'], file_reads)  # 書き込み先の変更前の状態を含む
            path = a / write['path']
            self.assertEqual(file_reads[write['path']]['sha256'], desk_plan._sha256(path.read_bytes())
                             if path.is_file() else 'missing')
        self.apply(a, result)
        legacy(b)
        self.assertEqual(tree(a), tree(b))
        self.ran.add(kind)
        return result

    @covers('runner')
    def test_runner(self):
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt):
                self.check('runner', {'enabled': True, 'selected': ['m1', 'm2'], 'billing': 'free'}, 'opencode',
                           (lambda h: self.put(h, 'agents.json', '{bad', raw=True)) if corrupt else (lambda h: None),
                           lambda h: setup_agents.set_runner(h, 'opencode', enabled=True, models=['m1', 'm2'],
                                                             billing='free'), ['agents.json'])
        self.check('runner', {'selected': None}, 'opencode', lambda h: None,
                   lambda h: setup_agents.set_runner(h, 'opencode', models=setup_agents.REMOVE), ['agents.json'])

    @covers('model')
    def test_model(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                self.check('model', {'id': 'x', 'efforts': ['low']}, 'opencode',
                           (lambda h: self.put(h, 'models/opencode.json', {'models': [{'id': 'x', 'efforts': []}]}))
                           if existing else (lambda h: None),
                           lambda h: setup_agents.add_manual_model(h, 'opencode', 'x', ['low']),
                           ['models/opencode.json'])

    @covers('routing')
    def test_routing(self):
        previous = b'\xef\xbb\xbf{"tiers": 1}\r\n'
        result = self.check('routing', ROUTING, None, lambda h: self.put(h, 'routing.json', previous),
                            lambda h: serve.write_routing(h, ROUTING), ['routing.prev.json', 'routing.json'])
        self.assertEqual(result['writes'][0]['content_utf8'].encode('utf-8'), previous)
        self.check('routing', ROUTING, None, lambda h: None, lambda h: serve.write_routing(h, ROUTING),
                   ['routing.json'])

    @covers('template', 'template-delete', 'example-copy')
    def test_template_and_example(self):
        body = dict(EXAMPLE, name='mine')
        self.check('template', body, 'mine', lambda h: None,
                   lambda h: common.write_json(h / 'templates' / 'mine.json', body), ['templates/mine.json'])
        self.check('template-delete', {}, 'cheap', lambda h: None,
                   lambda h: (h / 'templates' / 'cheap.json').unlink(), ['templates/cheap.json'])
        self.check('example-copy', {}, 'cheap-all-tiers', lambda h: None,
                   lambda h: common.write_json(h / 'templates' / 'cheap-all-tiers.json',
                                               dict(EXAMPLE, name='cheap-all-tiers')),
                   ['templates/cheap-all-tiers.json'])

    @covers('workflow', 'workflow-delete')
    def test_workflow(self):
        body = dict(WORKFLOW, name='mine')
        self.check('workflow', body, 'mine', lambda h: None,
                   lambda h: common.write_json(h / 'workflows' / 'mine.json', body), ['workflows/mine.json'])
        result = self.check('workflow-delete', {}, 'high-model',
                            lambda h: self.put(h, 'workflows/high-model.json', body),
                            lambda h: (h / 'workflows' / 'high-model.json').unlink(), ['workflows/high-model.json'])
        bundled = [d for d in result['reads'] if d['root'] == 'atelier' and d['path'] == 'workflows/high-model.json']
        self.assertEqual(len(bundled), 1)
        self.assertNotEqual(bundled[0]['sha256'], 'missing')

    @covers('application')
    def test_application(self):
        body = {'template': 'cheap', 'until': FUTURE, 'on_expire': 'ask', 'reason': 'why'}

        def legacy(h):
            templates.apply_local(h, 'cheap', FUTURE, 'ask', 'why', 'studio', NOW)
        self.check('application', body, None, lambda h: None, legacy, ['overrides.json'])
        # 前の適用があれば履歴に移す。末尾の改行が無く CRLF の履歴にも、既存の追記と同じ結果になる
        setup = (lambda h: (self.put(h, 'overrides.json', {'synced_at': 's', 'updated_at': 'u',
                                                           'application': application(FUTURE)}),
                            self.put(h, 'history.jsonl', b'{"a": 1}\r\n{"b": 2}')))
        result = self.check('application', body, None, setup, legacy, ['history.jsonl', 'overrides.json'])
        self.assertTrue(result['writes'][0]['content_utf8'].endswith(os.linesep))

    @covers('application-answer')
    def test_answer(self):
        for answer in ({'choice': 'extend', 'until': FUTURE}, {'choice': 'stop'}, {'choice': 'forever'}):
            with self.subTest(answer=answer):
                paths = ['overrides.json'] if answer['choice'] != 'stop' else ['history.jsonl', 'overrides.json']
                self.check('application-answer', answer, None,
                           lambda h: self.put(h, 'overrides.json', {'synced_at': 's', 'updated_at': 'u',
                                                                    'application': application(PAST)}),
                           lambda h: templates.answer_local(h, answer['choice'], answer.get('until'), NOW), paths)

    @covers('application-expire')
    def test_expire(self):
        def setup(on_expire, until):
            return lambda h: self.put(h, 'overrides.json', {'synced_at': 's', 'updated_at': 'u',
                                                            'application': application(until, on_expire)})
        self.check('application-expire', {}, None, setup('drop', PAST),
                   lambda h: templates.status_local(h, NOW), ['history.jsonl', 'overrides.json'])
        for label, on_expire, until in (('ask', 'ask', PAST), ('active', 'drop', FUTURE), ('none', None, None)):
            with self.subTest(label):
                result = self.check('application-expire', {}, None,
                                    (lambda h: None) if label == 'none' else setup(on_expire, until),
                                    lambda h: templates.status_local(h, NOW), [])
                self.assertEqual(result['writes'], [])

    def test_cases_cover_the_11_contract_kinds(self):
        contract = {'runner', 'model', 'routing', 'template', 'workflow', 'application', 'application-answer',
                    'template-delete', 'example-copy', 'workflow-delete', 'application-expire'}  # 契約の 11 KIND
        self.assertEqual(COVERED_KINDS, contract)


class ErrorTest(Base):
    def test_success_output_is_one_ascii_line(self):
        self.make_home(self.home)
        before = tree(HERE)
        code, out, err = self.run_cli('template', '--name', 'mine', body=dict(EXAMPLE, name='mine', description='日本語'))
        self.assertEqual((code, err), (0, b''))
        self.assertEqual(tree(HERE), before)
        self.assertEqual(out.count(b'\n'), 1)
        out.decode('ascii')
        result = json.loads(out)
        self.assertEqual(result['writes'][0]['op'], 'replace')
        self.assertIn('日本語', result['writes'][0]['content_utf8'])
        self.assertIsNotNone(datetime.datetime.fromisoformat(result['evaluated_at']).tzinfo)

    def test_missing_home_stays_missing(self):
        before = tree(self.tmp)
        code, out, _ = self.cli('runner', '--name', 'agent', body={'billing': 'x'})
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['reads'][0], {'root': 'home', 'type': 'file', 'path': 'agents.json',
                                                      'sha256': 'missing'})
        self.assertEqual(set(tree(self.tmp)) - set(before), {'input.json'})
        self.assertFalse(self.home.exists())

    def test_validation_failure_is_exit_1(self):
        self.make_home(self.home)
        code, out, err = self.run_cli('runner', '--name', 'agent', body={'enabled': False})
        self.assertEqual((code, err), (1, b''))
        self.assertEqual(json.loads(out), {'schema': 'atelier.validation/1', 'kind': 'runner', 'valid': False,
                                           'problems': ['agent cannot be disabled.']})
        self.put(self.home, 'overrides.json', {'application': application(FUTURE)})
        code, out, _ = self.run_cli('template-delete', '--name', 'cheap')
        self.assertEqual((code, json.loads(out)['problems']), (1, ['the template is used by the current application.']))
        self.put(self.home, 'templates/cheap-all-tiers.json', EXAMPLE)
        code, out, _ = self.run_cli('example-copy', '--name', 'cheap-all-tiers')
        self.assertEqual((code, json.loads(out)['problems']), (1, ['a template with that name already exists.']))
        (self.home / 'templates' / 'cheap-all-tiers.json').unlink()
        (self.home / 'templates' / 'cheap-all-tiers.json').mkdir()  # ディレクトリでも Studio は 409
        self.assertEqual(self.run_cli('example-copy', '--name', 'cheap-all-tiers')[0], 1)

    def test_bodyless_kinds_accept_only_empty_object(self):
        self.make_home(self.home)
        for args in (('example-copy', '--name', 'cheap-all-tiers'), ('template-delete', '--name', 'cheap'),
                     ('workflow-delete', '--name', 'mine'), ('application-expire',)):
            for body in ({'as': 'other'}, {'x': 1}, {'as': 'cheap-all-tiers'}):
                with self.subTest(args=args, body=body):
                    code, out, err = self.run_cli(*args, body=body)
                    self.assertEqual((code, out), (2, b''))
                    self.assertTrue(err)
        self.assertEqual(self.run_cli('example-copy', '--name', 'cheap-all-tiers', body={})[0], 0)
        self.assertFalse((self.home / 'templates' / 'other.json').exists())

    def test_validation_matches_desk_validate(self):
        self.make_home(self.home)
        bad = dict(EXAMPLE, name='mine', tiers={})
        code, out, _ = self.run_cli('template', '--name', 'mine', body=bad)
        problem = desk_validate.check('template', self.home, bad, 'mine')
        self.assertEqual((code, json.loads(out)['problems']), (1, [problem]))

    def test_usage_errors_are_exit_2(self):
        cases = [(('nope',), {}), (('runner',), {}), (('runner', '--name', 'nope'), {}),
                 (('template', '--name', 'bad name'), {'body': {}}), (('application', '--name', 'x'), {}),
                 (('template-delete', '--name', '../x'), {}), (('runner', '--name', 'agent'), {'input_bytes': b'[]'}),
                 (('runner', '--name', 'agent'), {'input_bytes': b'\xff\xfe'}),
                 (('runner', '--name', 'agent'), {'input_bytes': b'{'}),
                 (('example-copy', '--name', 'cheap-all-tiers'), {'body': {'as': 'other'}})]
        for args, kwargs in cases:
            with self.subTest(args=args):
                code, out, err = self.run_cli(*args, **kwargs)
                self.assertEqual((code, out), (2, b''))
                self.assertTrue(err)
        code, _, _ = self.run_cli('runner', '--name', 'agent', home=Path('relative'))
        self.assertEqual(code, 2)

    def test_missing_target_is_exit_3(self):
        self.make_home(self.home)
        for args in (('template-delete', '--name', 'ghost'), ('workflow-delete', '--name', 'ghost'),
                     ('example-copy', '--name', 'ghost')):
            with self.subTest(args=args):
                code, out, err = self.run_cli(*args)
                self.assertEqual((code, out), (3, b''))
                self.assertTrue(err)
        source = self.tmp / 'gone.json'
        done = subprocess.run([sys.executable, '-B', str(PLAN), 'runner', '--name', 'agent', '--home', str(self.home),
                               '--input', str(source)], capture_output=True)
        self.assertEqual((done.returncode, done.stdout), (3, b''))

    def broken(self, *args, **kwargs):
        code, out, err = self.run_cli(*args, **kwargs)
        self.assertEqual((code, out), (4, b''))
        self.assertTrue(err)

    def test_unreadable_state_is_exit_4(self):
        self.make_home(self.home)
        self.put(self.home, 'routing.json', b'\xff\xfe{}')
        self.broken('routing', body=ROUTING)
        (self.home / 'routing.json').unlink()
        (self.home / 'templates' / 'mine.json').mkdir()  # x.json という名前のディレクトリ
        self.broken('template', '--name', 'mine', body=dict(EXAMPLE, name='mine'))
        self.put(self.home, 'overrides.json', {'application': application(FUTURE)})
        self.put(self.home, 'history.jsonl', b'\xff')
        self.broken('application', body={'template': 'cheap', 'on_expire': 'ask'})
        file_home = self.tmp / 'file'
        file_home.write_text('x')
        self.broken('runner', '--name', 'agent', home=file_home)

    def test_links_are_refused(self):
        self.make_home(self.home)
        outside = self.tmp / 'outside'
        outside.mkdir()
        (self.home / 'models').mkdir()
        link = self.home / 'models' / 'opencode.json'
        try:
            os.symlink(outside, link, target_is_directory=True)
        except OSError:
            link = None
        if link is not None:
            self.broken('model', '--name', 'opencode', body={'id': 'x'})
            link.unlink()
        if sys.platform == 'win32':
            import _winapi
            junction = self.home / 'workflows'
            _winapi.CreateJunction(str(outside), str(junction))
            self.addCleanup(os.rmdir, junction)
            self.broken('workflow', '--name', 'mine', body=dict(WORKFLOW, name='mine'))
            self.assertEqual(os.listdir(outside), [])

    def test_component_pointing_outside_is_exit_4(self):
        self.make_home(self.home)
        component = json.loads((HERE / 'components' / sorted(os.listdir(HERE / 'components'))[0]).read_text('utf-8'))
        component.update(id='mine-comp', doc='../atelier/SKILL.md')
        self.put(self.home, 'components/mine-comp.json', component)
        self.broken('workflow', '--name', 'mine', body=dict(WORKFLOW, name='mine'))


class DependencyTest(Base):
    """記録した読み取りを 1 つ変えれば指紋が変わり、記録に無い変更は計画を変えない。"""

    def reads(self, kind, body, name=None):
        return desk_plan.plan(kind, self.home, body, name, NOW)['reads']

    def test_missing_empty_and_present_differ(self):
        self.make_home(self.home)
        body = {'template': 'cheap', 'until': FUTURE, 'on_expire': 'ask'}
        shas = []
        for setup in (lambda: None, lambda: self.put(self.home, 'overrides.json', b''),
                      lambda: self.put(self.home, 'overrides.json', {'application': None})):
            setup()
            shas.append([d['sha256'] for d in self.reads('application', body) if d['path'] == 'overrides.json'][0])
        self.assertEqual(shas[0], 'missing')
        self.assertEqual(shas[1], EMPTY_SHA)
        self.assertEqual(len(set(shas)), 3)

    def test_component_listing_detects_additions_and_changes(self):
        self.make_home(self.home)
        body = dict(WORKFLOW, name='mine')

        def listing():
            return [d for d in self.reads('workflow', body, 'mine') if d['type'] == 'directory']
        first = listing()
        self.assertEqual([(d['root'], d['path']) for d in first], [('atelier', 'components'), ('home', 'components')])
        self.assertTrue(first[1]['missing'] and first[1]['entries'] == [])
        (self.home / 'components').mkdir()
        empty = listing()[1]
        self.assertFalse(empty['missing'])
        names = sorted(os.listdir(HERE / 'components'))
        component = json.loads((HERE / 'components' / names[0]).read_text('utf-8'))
        self.put(self.home, 'components/zz.json', dict(component, id='zz'))
        self.put(self.home, 'components/notes.txt', 'ignored')  # *.json 以外は列挙に入らない
        entries = listing()[1]['entries']
        self.assertEqual([e['name'] for e in entries], ['zz.json'])
        self.put(self.home, 'components/zz.json', dict(component, id='zz', cost=component['cost']), raw=False)
        self.assertEqual(len(listing()[0]['entries']), len(names) - sum(not n.endswith('.json') for n in names))

    def test_listing_matches_what_load_components_reads(self):
        """X.JSON (大文字拡張子) を含め、列挙は flow.load_components が読む集合・並びと一致する。"""
        self.make_home(self.home)
        component = json.loads((HERE / 'components' / sorted(os.listdir(HERE / 'components'))[0]).read_text('utf-8'))
        for filename in ('X.JSON', 'y.json', 'B.json', 'a.json', 'c.txt'):
            stem = filename.split('.')[0]
            self.put(self.home, f'components/{filename}', dict(component, id=stem))
        listing = [d for d in self.reads('workflow', dict(WORKFLOW, name='mine'), 'mine')
                   if d['type'] == 'directory' and d['root'] == 'home'][0]
        names = [e['name'] for e in listing['entries']]
        read_by_glob = [p.name for p in (self.home / 'components').glob('*.json')]
        self.assertEqual(set(names), set(read_by_glob))  # OS の規則そのまま (Windows は X.JSON も入る)
        self.assertNotIn('c.txt', names)
        self.assertEqual(names, sorted(read_by_glob))  # コードポイント順
        found, _ = flow.load_components(home=self.home)
        self.assertEqual({n.rsplit('.', 1)[0] for n in names}, {cid for cid in found if cid in ('X', 'y', 'B', 'a')})
        if sys.platform == 'win32':  # 実測: Windows の glob は X.JSON も拾う
            self.assertEqual(names, ['B.json', 'X.JSON', 'a.json', 'y.json'])

    def test_unrecorded_change_does_not_alter_plan(self):
        self.make_home(self.home)
        body = {'enabled': True}
        before = desk_plan.plan('runner', self.home, body, 'opencode', NOW)
        self.put(self.home, 'overrides.json', {'application': application(FUTURE)})
        self.put(self.home, 'templates/zz.json', {'x': 1})
        self.assertEqual(desk_plan.plan('runner', self.home, body, 'opencode', NOW), before)
        self.put(self.home, 'agents.json', {'runners': {'agent': {'enabled': True}}})
        self.assertNotEqual(desk_plan.plan('runner', self.home, body, 'opencode', NOW), before)

    def test_plan_is_built_from_the_recorded_bytes(self):
        """計画の元は rec.file が指紋を取ったバイトそのもの。パスからの読み直し (Path.read_*・open) は 1 度もない。"""
        self.make_home(self.home)
        self.put(self.home, 'models/opencode.json', {'models': [{'id': 'old'}]})
        cases = [
            ('runner', {'enabled': True}, 'opencode', {}),
            ('model', {'id': 'new'}, 'opencode', {}),
            ('application', {'template': 'cheap', 'until': FUTURE, 'on_expire': 'ask'}, None,
             {'overrides.json': {'application': application(FUTURE)}}),
            ('application-answer', {'choice': 'stop'}, None,
             {'overrides.json': {'application': application(FUTURE)}}),
            ('application-expire', {}, None, {'overrides.json': {'application': application(PAST, 'drop')}}),
            ('template-delete', {}, 'spare', {'templates/spare.json': dict(EXAMPLE, name='spare')}),
        ]

        def forbidden(*args, **kwargs):
            raise AssertionError('a recorded file was read again from its path')
        for kind, body, name, files in cases:
            for rel, data in files.items():
                self.put(self.home, rel, data)
            with self.subTest(kind=kind):
                expected = desk_plan.plan(kind, self.home, body, name, NOW)
                with mock.patch.object(Path, 'read_text', forbidden), mock.patch.object(Path, 'read_bytes', forbidden), \
                        mock.patch.object(desk_validate, 'check', return_value=None):
                    actual = desk_plan.plan(kind, self.home, body, name, NOW)
                self.assertEqual(actual, expected)

    def test_cli_fingerprint_matches_the_bytes_the_plan_came_from(self):
        self.make_home(self.home)
        agents_bytes = (self.home / 'agents.json').read_bytes()
        result = self.plan_ok('runner', '--name', 'opencode', body={'billing': 'flat'})
        sha = [d['sha256'] for d in result['reads'] if d['path'] == 'agents.json'][0]
        self.assertEqual(sha, hashlib.sha256(agents_bytes).hexdigest())
        content = json.loads(result['writes'][0]['content_utf8'])
        self.assertEqual(content['runners']['opencode']['models'], ['m1'])  # 指紋を取ったバイトの中身から作られた
        self.assertEqual(content['runners']['opencode']['billing'], 'flat')

    def test_file_is_opened_once_and_read_from_that_handle(self):
        self.put(self.home, 'a.json', {'x': 1})
        opened = []
        real_open = os.open

        def counting(path, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, *args, **kwargs)
        rec = desk_plan.Recorder(self.home)
        with mock.patch.object(Path, 'read_bytes', side_effect=AssertionError('reopened by path')), \
                mock.patch.object(desk_plan.os, 'open', counting):
            data = rec.file('a.json')
        self.assertEqual(data, (self.home / 'a.json').read_bytes())
        self.assertEqual(opened, [str(self.home / 'a.json')])

    def test_file_swapped_after_the_check_is_rejected(self):
        """lstat で通常ファイルと確かめたあと、開く前に別のファイル (リンク先など) に差し替わったら終了 4 相当。"""
        self.put(self.home, 'a.json', {'x': 1})
        self.put(self.tmp, 'outside.json', {'secret': True})
        real_open = os.open

        def swapped(path, *args, **kwargs):
            return real_open(self.tmp / 'outside.json', *args, **kwargs)
        rec = desk_plan.Recorder(self.home)
        with mock.patch.object(desk_plan.os, 'open', swapped):
            with self.assertRaises(desk_validate.CallError) as caught:
                rec.file('a.json')
        self.assertEqual(caught.exception.code, desk_validate.Exit.BROKEN)
        self.assertNotIn(('home', 'a.json'), rec.deps)

    def test_file_that_turns_into_a_directory_after_the_check_is_rejected(self):
        self.put(self.home, 'a.json', {'x': 1})
        (self.tmp / 'dir').mkdir()
        real_open = os.open

        def swapped(path, flags, *args, **kwargs):
            if os.name == 'nt':  # Windows は os.open でディレクトリを開けない
                raise PermissionError(13, 'Permission denied')
            return real_open(self.tmp / 'dir', flags, *args, **kwargs)
        rec = desk_plan.Recorder(self.home)
        with mock.patch.object(desk_plan.os, 'open', swapped):
            with self.assertRaises(desk_validate.CallError) as caught:
                rec.file('a.json')
        self.assertEqual(caught.exception.code, desk_validate.Exit.BROKEN)

    def test_every_expire_boundary(self):
        self.make_home(self.home)
        until = datetime.datetime(2026, 5, 1, 12, 0, tzinfo=JST)
        self.put(self.home, 'overrides.json', {'application': application(until.isoformat(), 'drop')})
        for delta, expected in ((-1, 0), (0, 2), (1, 2)):
            now = until + datetime.timedelta(seconds=delta)
            with self.subTest(delta=delta):
                result = desk_plan.plan('application-expire', self.home, {}, None, now)
                self.assertEqual(len(result['writes']), expected)
                self.assertEqual(result['evaluated_at'], now.isoformat())


class ContractTest(Base):
    """CLI の出力 (reads・writes・evaluated_at) と終了コードで、save-plan/1 の契約を確かめる。"""
    FAR = '2999-01-01T00:00:00+09:00'

    @staticmethod
    def sha(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def outside_home(self):
        return {p for p in tree(self.tmp) if p != 'input.json' and p != 'home' and not p.startswith('home/')}

    def put_application(self, until, on_expire='ask'):
        self.put(self.home, 'overrides.json', {'synced_at': 's', 'updated_at': 'u',
                                               'application': application(until, on_expire)})

    def kind_cases(self):
        """KIND ごとの (引数, 本文, 準備, 期待する書き込み先)。"""
        return {
            'runner': (('runner', '--name', 'opencode'), {'enabled': True}, None, ['agents.json']),
            'model': (('model', '--name', 'opencode'), {'id': 'x'}, None, ['models/opencode.json']),
            'routing': (('routing',), ROUTING, lambda h: self.put(h, 'routing.json', ROUTING),
                        ['routing.prev.json', 'routing.json']),
            'template': (('template', '--name', 'mine'), dict(EXAMPLE, name='mine'), None, ['templates/mine.json']),
            'template-delete': (('template-delete', '--name', 'cheap'), {}, None, ['templates/cheap.json']),
            'workflow': (('workflow', '--name', 'mine'), dict(WORKFLOW, name='mine'), None, ['workflows/mine.json']),
            'workflow-delete': (('workflow-delete', '--name', 'mine'), {},
                                lambda h: self.put(h, 'workflows/mine.json', dict(WORKFLOW, name='mine')),
                                ['workflows/mine.json']),
            'example-copy': (('example-copy', '--name', 'cheap-all-tiers'), {}, None,
                             ['templates/cheap-all-tiers.json']),
            'application': (('application',), {'template': 'cheap', 'on_expire': 'ask'},
                            lambda h: self.put_application(self.FAR), ['history.jsonl', 'overrides.json']),
            'application-answer': (('application-answer',), {'choice': 'stop'},
                                   lambda h: self.put_application(PAST), ['history.jsonl', 'overrides.json']),
            'application-expire': (('application-expire',), {}, lambda h: self.put_application(PAST, 'drop'),
                                   ['history.jsonl', 'overrides.json']),
        }

    def test_write_targets_are_unique_and_inside_home(self):
        cases = self.kind_cases()
        self.assertEqual(len(cases), 11)
        for kind, (args, body, setup, expected) in cases.items():
            with self.subTest(kind=kind):
                shutil.rmtree(self.home, ignore_errors=True)
                self.make_home(self.home)
                if setup:
                    setup(self.home)
                before_outside = self.outside_home()
                result = self.plan_ok(*args, body=body)
                paths = [w['path'] for w in result['writes']]
                self.assertEqual(paths, expected)
                self.assertEqual(len(set(paths)), len(paths))
                for path in paths:
                    self.assertFalse(path.startswith('/') or '\\' in path or ':' in path, path)
                    self.assertNotIn('..', path.split('/'))
                self.apply(self.home, result)  # 適用しても home の外に何も増えない
                self.assertEqual(self.outside_home(), before_outside)

    def test_unsafe_names_are_refused_without_a_plan(self):
        self.make_home(self.home)
        for kind in ('template', 'workflow', 'template-delete', 'workflow-delete', 'example-copy', 'model'):
            for bad in ('/abs', '../x', 'a/../b', 'a/b', 'a\\b', 'c:x', ''):
                with self.subTest(kind=kind, bad=bad):
                    code, out, err = self.run_cli(kind, '--name', bad, body={})
                    self.assertEqual((code, out), (2, b''))
                    self.assertTrue(err)
        self.assertEqual(self.outside_home(), set())

    def test_duplicate_or_escaping_write_target_is_exit_4(self):
        """名前の検査をすり抜けて作られた書き込み先を、main の終了コードで拒否する。"""
        self.make_home(self.home)
        source = self.tmp / 'mine.json'
        source.write_text(json.dumps(dict(EXAMPLE, name='mine')), encoding='utf-8')
        replace = desk_plan.Replace
        bad_lists = {'duplicate': [replace('templates/mine.json', 'a'), replace('templates/mine.json', 'b')],
                     'dotdot': [replace('templates/../x.json', 'a')], 'absolute': [replace('/etc/x', 'a')],
                     'backslash': [replace('templates\\x.json', 'a')]}
        for label, writes in bad_lists.items():
            with self.subTest(label), mock.patch.dict(
                    desk_plan.KINDS, {'template': dataclasses.replace(desk_plan.KINDS['template'],
                                                                     build=lambda rec, req, w=writes: w)}):
                out, err = io.BytesIO(), io.BytesIO()
                code = desk_plan.main(['template', '--name', 'mine', '--home', str(self.home), '--input', str(source)],
                                      out, err)
                self.assertEqual((code, out.getvalue()), (4, b''))
                self.assertTrue(err.getvalue())
                self.assertFalse((self.home / 'templates' / 'mine.json').exists())

    def test_example_copy_reads_bundled_source_and_missing_target(self):
        self.make_home(self.home)
        result = self.plan_ok('example-copy', '--name', 'cheap-all-tiers')
        source = (HERE / 'examples' / 'templates' / 'cheap-all-tiers.json').read_bytes()
        self.assertIn({'root': 'atelier', 'type': 'file', 'path': 'examples/templates/cheap-all-tiers.json',
                       'sha256': self.sha(source)}, result['reads'])
        self.assertIn({'root': 'home', 'type': 'file', 'path': 'templates/cheap-all-tiers.json',
                       'sha256': 'missing'}, result['reads'])
        self.assertEqual(json.loads(result['writes'][0]['content_utf8'])['name'], 'cheap-all-tiers')

    def test_existing_template_and_workflow_files_are_in_reads(self):
        self.make_home(self.home)
        raw = json.dumps(dict(EXAMPLE, name='mine', description='old')).encode('utf-8') + b'\r\n'
        self.put(self.home, 'templates/mine.json', raw)
        reads = self.plan_ok('template', '--name', 'mine', body=dict(EXAMPLE, name='mine'))['reads']
        self.assertIn({'root': 'home', 'type': 'file', 'path': 'templates/mine.json', 'sha256': self.sha(raw)}, reads)
        raw = json.dumps(dict(WORKFLOW, name='mine', description='old')).encode('utf-8') + b'\r\n'
        self.put(self.home, 'workflows/mine.json', raw)
        reads = self.plan_ok('workflow', '--name', 'mine', body=dict(WORKFLOW, name='mine'))['reads']
        self.assertIn({'root': 'home', 'type': 'file', 'path': 'workflows/mine.json', 'sha256': self.sha(raw)}, reads)

    def test_corrupt_models_file_is_overwritten_like_studio(self):
        a, b = self.tmp / 'a', self.tmp / 'b'
        for home in (a, b):
            self.make_home(home)
            self.put(home, 'models/opencode.json', '{bad', raw=True)
        result = self.plan_ok('model', '--name', 'opencode', body={'id': 'x', 'efforts': ['low']}, home=a)
        self.apply(a, result)
        setup_agents.add_manual_model(b, 'opencode', 'x', ['low'])
        self.assertEqual(tree(a), tree(b))
        saved = json.loads((a / 'models' / 'opencode.json').read_text('utf-8'))
        self.assertEqual([m['id'] for m in saved['models']], ['x'])

    def test_corrupt_overrides_means_no_application(self):
        self.make_home(self.home)
        for raw in (b'{bad', b'[]'):
            self.put(self.home, 'overrides.json', raw)
            with self.subTest(raw=raw):
                result = self.plan_ok('application', body={'template': 'cheap', 'on_expire': 'ask'})
                self.assertEqual([w['path'] for w in result['writes']], ['overrides.json'])  # 前の適用が無いので履歴なし
                saved = json.loads(result['writes'][0]['content_utf8'])
                self.assertEqual((saved['synced_at'], saved['application']['template']), (None, 'cheap'))
                self.assertIn({'root': 'home', 'type': 'file', 'path': 'overrides.json', 'sha256': self.sha(raw)},
                              result['reads'])
                self.assertEqual(self.plan_ok('template-delete', '--name', 'cheap')['writes'],
                                 [{'op': 'delete', 'path': 'templates/cheap.json'}])
                self.assertEqual(self.plan_ok('application-expire')['writes'], [])

    @staticmethod
    def history_reads(result):
        return [d for d in result['reads'] if d['path'] == 'history.jsonl']

    def test_application_reads_history_only_when_it_is_needed(self):
        self.make_home(self.home)
        body = {'template': 'cheap', 'on_expire': 'ask'}
        self.assertEqual(self.history_reads(self.plan_ok('application', body=body)), [])  # 前の適用が無い
        self.put_application(self.FAR)
        missing = {'root': 'home', 'type': 'file', 'path': 'history.jsonl', 'sha256': 'missing'}
        self.assertEqual(self.history_reads(self.plan_ok('application', body=body)), [missing])
        history = b'{"a": 1}\r\n'
        self.put(self.home, 'history.jsonl', history)
        present = dict(missing, sha256=self.sha(history))
        self.assertEqual(self.history_reads(self.plan_ok('application', body=body)), [present])
        self.put_application(PAST)  # 答え: 続ける (forever) は履歴に移さず、止める (stop) は移す
        self.assertEqual(self.history_reads(self.plan_ok('application-answer', body={'choice': 'forever'})), [])
        self.assertEqual(self.history_reads(self.plan_ok('application-answer', body={'choice': 'stop'})), [present])

    def test_workflow_reads_each_component_file_it_points_to(self):
        self.make_home(self.home)
        gate = json.loads((HERE / 'components' / 'gate.jev.json').read_text('utf-8'))
        self.put(self.home, 'components/zz.json', dict(gate, id='zz', doc='SKILL.md'))
        reads = self.plan_ok('workflow', '--name', 'mine', body=dict(WORKFLOW, name='mine'))['reads']
        files = {d['path']: d['sha256'] for d in reads if d['type'] == 'file' and d['root'] == 'atelier'}
        for rel in ('appendix/plan.md', 'appendix/review.md', 'prepare.py', 'SKILL.md'):  # 同梱・home の部品が指す先
            self.assertEqual(files.get(rel), self.sha((HERE / rel).read_bytes()), rel)

    def test_application_id_and_history_time_share_evaluated_at(self):
        self.make_home(self.home)
        self.put_application(self.FAR)
        result = self.plan_ok('application', body={'template': 'cheap', 'on_expire': 'ask'})
        at = datetime.datetime.fromisoformat(result['evaluated_at'])
        by_path = {w['path']: w['content_utf8'] for w in result['writes']}
        saved = json.loads(by_path['overrides.json'])
        stamp = at.astimezone(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S')
        self.assertEqual(saved['application']['id'], 'a-' + stamp)
        self.assertEqual((saved['application']['started_at'], saved['updated_at']), (at.isoformat(),) * 2)
        replaced = json.loads(by_path['history.jsonl'].splitlines()[-1])
        self.assertEqual((replaced['id'], replaced['ended_how'], replaced['ended_at']),
                         ('old', 'replaced', at.isoformat()))
        self.put_application(PAST)
        result = self.plan_ok('application-answer', body={'choice': 'stop'})
        by_path = {w['path']: w['content_utf8'] for w in result['writes']}
        self.assertEqual(json.loads(by_path['history.jsonl'].splitlines()[-1])['ended_at'], result['evaluated_at'])
        self.assertEqual(json.loads(by_path['overrides.json'])['updated_at'], result['evaluated_at'])
        self.put_application(PAST, 'drop')
        result = self.plan_ok('application-expire')
        by_path = {w['path']: w['content_utf8'] for w in result['writes']}
        self.assertEqual(json.loads(by_path['history.jsonl'].splitlines()[-1])['ended_at'], PAST)  # 期限の時刻
        self.assertEqual(json.loads(by_path['overrides.json'])['updated_at'], result['evaluated_at'])


class Rework3Test(Base):
    """差し戻し 3 の 7 点 (3.10 のジャンクション判定は検証コマンド 3 が確かめる)。"""

    def test_expire_with_unparsable_until_plans_nothing(self):
        self.put(self.home, 'templates/t1.json', dict(EXAMPLE, name='t1'))
        self.put(self.home, 'overrides.json', {'application': {
            'template': 't1', 'until': 'garbage', 'on_expire': 'drop', 'id': 'a', 'reason': '',
            'started_at': '2026-01-01T00:00:00+00:00', 'started_via': 'studio'}})
        self.assertEqual(self.plan_ok('application-expire')['writes'], [])

    def put_map(self, doc):
        component = json.loads((HERE / 'components' / 'map.builtin.json').read_text('utf-8'))
        component.update(id='my.map', doc=doc)
        self.put(self.home, 'components/my.map.json', component)

    def test_component_doc_inside_the_bundle_is_accepted(self):
        self.make_home(self.home)
        workflow = json.loads((HERE / 'workflows' / 'jev-routed.json').read_text('utf-8'))
        docs = ['./appendix/map.md', 'appendix//map.md', 'appendix/map.md/']
        if os.sep == '\\':
            docs.append('appendix\\map.md')
        for doc in docs:
            with self.subTest(doc=doc):
                self.put_map(doc)
                reads = self.plan_ok('workflow', '--name', 'mine', body=dict(workflow, name='mine'))['reads']
                paths = {d['path'] for d in reads if d['root'] == 'atelier' and d['type'] == 'file'}
                self.assertIn('appendix/map.md', paths)
                self.assertFalse([p for p in paths if p.startswith('.') or '//' in p or '\\' in p], paths)

    def test_component_doc_outside_the_bundle_is_exit_4(self):
        self.make_home(self.home)
        workflow = json.loads((HERE / 'workflows' / 'jev-routed.json').read_text('utf-8'))
        # 実在するファイルを指す (不在なら desk_validate と同じく先に検証失敗の終了 1 になる)
        for doc in ('../atelier/SKILL.md', 'appendix/../../atelier/SKILL.md', str(HERE / 'SKILL.md')):
            with self.subTest(doc=doc):
                self.put_map(doc)
                code, out, err = self.run_cli('workflow', '--name', 'mine', body=dict(workflow, name='mine'))
                self.assertEqual((code, out), (4, b''))
                self.assertTrue(err.startswith(b'desk_plan.py: unsafe path'), err)

    def test_lone_surrogate_is_exit_1_like_studio(self):
        self.make_home(self.home)
        body = dict(EXAMPLE, name='t2', description='x\ud800')
        try:
            desk_plan._json_text(body).encode('utf-8')
        except UnicodeEncodeError as error:
            expected = str(error)
        code, out, err = self.run_cli('template', '--name', 't2', body=body)
        self.assertEqual((code, err), (1, b''))
        self.assertEqual(json.loads(out)['problems'], [expected])
        # desk_validate の検査が通しても、desk_plan は自分の出力を守る
        with mock.patch('desk_validate.check', return_value=None):
            with self.assertRaises(desk_plan.Invalid) as caught:
                desk_plan.plan('template', self.home, body, 't2', NOW)
            self.assertEqual(str(caught.exception), expected)
            with self.assertRaises(desk_plan.Invalid) as caught:
                desk_plan.plan('application', self.home,
                               {'template': 'cheap', 'on_expire': 'ask', 'reason': 'r\ud800'}, None, NOW)
            self.assertTrue(str(caught.exception).startswith('the application was not started: '))
            self.assertIn('surrogates not allowed', str(caught.exception))

    def test_set_runner_refuses_agent_disable_before_reading(self):
        self.home.mkdir()
        self.put(self.home, 'agents.json', '[' * 100000 + ']' * 100000, raw=True)
        before = tree(self.home)
        for home in (self.home, None):
            with self.subTest(home=home):
                with self.assertRaisesRegex(ValueError, 'agent cannot be disabled.'):
                    setup_agents.set_runner(home, 'agent', enabled=False)
        self.assertEqual(tree(self.home), before)

    def test_name_error_comes_before_input_and_diagnostics_name_desk_plan(self):
        self.home.mkdir()
        gone = str(self.tmp / 'gone.json')

        def run(*args):
            done = subprocess.run([sys.executable, '-B', str(PLAN), *args, '--home', str(self.home),
                                   '--input', gone], capture_output=True, cwd=self.tmp, timeout=120)
            return done.returncode, done.stdout, done.stderr

        code, out, err = run('template')
        self.assertEqual((code, out), (2, b''))
        self.assertTrue(err.startswith(b'desk_plan.py: '), err)
        validate = subprocess.run([sys.executable, '-B', str(HERE / 'desk_validate.py'), 'template', '--home',
                                   str(self.home), '--input', gone], capture_output=True, cwd=self.tmp, timeout=120)
        self.assertEqual(validate.returncode, code)
        code, out, err = run('template', '--name', 'mine')
        self.assertEqual((code, out), (3, b''))
        self.assertTrue(err.startswith(b'desk_plan.py: the input file does not exist'), err)
        file_home = self.tmp / 'file'
        file_home.write_text('x')
        done = subprocess.run([sys.executable, '-B', str(PLAN), 'runner', '--name', 'agent', '--home', str(file_home),
                               '--input', gone], capture_output=True, cwd=self.tmp, timeout=120)
        self.assertEqual(done.returncode, 4)
        self.assertTrue(done.stderr.startswith(b'desk_plan.py: home is not a directory'), done.stderr)

    def test_cli_guard_refuses_side_effects_but_not_output(self):
        self.make_home(self.home)
        self.assertEqual(self.plan_ok('runner', '--name', 'agent', body={'billing': 'x'})['writes'][0]['path'],
                         'agents.json')  # __main__ (guard 付き) でも stdout に計画が出る
        source = self.tmp / 'input.json'
        kept = self.tmp / 'kept.txt'
        kept.write_text('k')
        script = '\n'.join([
            'import os, socket, sys',
            f'sys.path.insert(0, {str(HERE)!r})',
            'import desk_plan',
            f'code = desk_plan.main(["runner", "--name", "agent", "--home", {str(self.home)!r}, '
            f'"--input", {str(source)!r}], guard=True)',
            'sys.stdout.flush()',
            f'kept, new = {str(kept)!r}, {str(self.tmp / "new")!r}',
            'actions = [("open-w", lambda: open(new, "w")), ("open-a", lambda: open(kept, "a")),',
            '           ("os-open", lambda: os.open(new, os.O_WRONLY | os.O_CREAT)),',
            '           ("mkdir", lambda: os.mkdir(new)), ("replace", lambda: os.replace(kept, new)),',
            '           ("remove", lambda: os.remove(kept)), ("socket", lambda: socket.socket()),',
            '           ("subprocess", lambda: __import__("subprocess").run([sys.executable, "-c", "0"]))]',
            'results = []',
            'for label, action in actions:',
            '    try:',
            '        action()',
            '        results.append(label + ":allowed")',
            '    except Exception as error:',
            '        results.append(label + ":" + type(error).__name__)',
            'open(kept, "rb").read()',
            'print(code, *results)',
        ])
        done = subprocess.run([sys.executable, '-B', '-c', script], capture_output=True, cwd=self.tmp, timeout=120)
        lines = done.stdout.decode('ascii').splitlines()
        self.assertEqual(json.loads(lines[0])['schema'], 'atelier.save-plan/1')
        words = lines[1].split()
        self.assertEqual(words[0], '0')
        self.assertTrue(all(word.endswith(':CallError') for word in words[1:]), words)
        self.assertEqual(len(words), 9)
        self.assertEqual(kept.read_text(), 'k')
        self.assertFalse((self.tmp / 'new').exists())
        before = tree(self.home)
        desk_plan.plan('runner', self.home, {'billing': 'x'}, 'agent', NOW)  # import では hook を入れない
        (self.tmp / 'after-plan.txt').write_text('ok')
        self.assertEqual(tree(self.home), before)


class Rework4Test(Base):
    """親が通常ファイルのときの扱いを OS によらずそろえる (Windows は FileNotFoundError を返す)。"""

    def test_templates_as_regular_file_is_exit_4(self):
        self.home.mkdir()
        self.put(self.home, 'templates', b'not a directory')
        code, out, err = self.run_cli('template', '--name', 'x', body=dict(EXAMPLE, name='x'))
        self.assertEqual((code, out), (4, b''))
        self.assertIn(b'templates/x.json', err)

    def test_missing_templates_directory_stays_missing(self):
        self.home.mkdir()
        result = self.plan_ok('template', '--name', 'x', body=dict(EXAMPLE, name='x'))
        self.assertIn({'root': 'home', 'type': 'file', 'path': 'templates/x.json', 'sha256': 'missing'},
                      result['reads'])
        self.assertFalse((self.home / 'templates').exists())


if __name__ == '__main__':
    unittest.main()
