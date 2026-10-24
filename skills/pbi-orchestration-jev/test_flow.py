"""flow.py の解決順・検証・表・終了コードを、CLI を subprocess で動かして確かめる。

同梱の 2 つのプリセットと、component.py add で pstack の部品 (偽のプラグイン) を差したテンプレートが
誤りなく解決されること、同梱の部品が自作か Claude Code の built-in だけであること、home の部品と id の重なり、テンプレートの解決順 (作業リポジトリ → home → 同梱)、
検証の誤り (知らない工程・工程の抜け・工程に合わない部品・最後が後ろ盾でない・知らない部品・
PBI ごとの when の置き場所・max_rework の範囲・external_memory の値)、requires の cli と env の status、
同梱のコンポーネントの定義がすべて形どおりであることを見る。home は必ず一時ディレクトリにして、
実際のユーザーの home を読まないようにする。
"""
import contextlib
import copy
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'flow.py')
sys.path.insert(0, HERE)

import component  # noqa: E402
import flow  # noqa: E402

HEADER = 'stage\tcomponent\tinvoke\twhere\tmemory\twhen\tstatus\tdoc'
PRESETS = ('jev-routed', 'high-model')
# 偽の pstack に置くスキル (どれも disable-model-invocation なので read-skill の部品になる)
PSTACK_SKILLS = ('how', 'blast-radius', 'architect', 'interrogate')


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)


def bundled(name):
    with open(os.path.join(HERE, 'workflows', f'{name}.json'), encoding='utf-8') as f:
        return json.load(f)


class FlowCliTest(unittest.TestCase):
    def setUp(self):
        # tempfile の 0o700 は権限を絞った環境で読めなくなるので、既定の権限で作る。
        self.dir = os.path.join(tempfile.gettempdir(), f'pbi-flow-{uuid.uuid4().hex}')
        os.mkdir(self.dir)
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.base = os.path.join(self.dir, 'pbi')
        self.home = os.path.join(self.dir, 'home')
        os.makedirs(self.base)
        os.makedirs(self.home)

    def run_flow(self, *args, env=None):
        full_env = dict(os.environ)
        full_env.update(env or {})
        return subprocess.run([sys.executable, SCRIPT, self.base, '--home', self.home, *args],
                              capture_output=True, text=True, encoding='utf-8', env=full_env)

    def rows(self, stdout):
        lines = stdout.splitlines()
        self.assertTrue(lines[0].startswith('workflow '))
        self.assertEqual(lines[1], HEADER)
        return [line.split('\t') for line in lines[2:]]

    def install_pstack(self):
        '''偽の pstack をプラグインの置き場に作り、component.py add で home の jev-routed に差す。

        how は理解、blast-radius と architect は振り分けのあとの関所 (PBI ごと)、interrogate は deep のレビュー。
        '''
        self.plugins = os.path.join(self.dir, 'plugins')
        root = os.path.join(self.plugins, 'cache', 'cursor-plugins', 'pstack', '0.15.30')
        write_json(os.path.join(root, '.claude-plugin', 'plugin.json'),
                   {'name': 'pstack', 'license': 'MIT', 'repository': 'https://github.com/cursor/plugins'})
        for name in PSTACK_SKILLS:
            path = os.path.join(root, 'skills', name, 'SKILL.md')
            os.makedirs(os.path.dirname(path))
            with open(path, 'w', encoding='utf-8') as f:
                f.write(f'---\nname: {name}\ndescription: {name}\ndisable-model-invocation: true\n---\n')
        adds = [
            ['pstack:how', '--stage', 'understand', '--when', 'clearly-complex'],
            ['pstack:blast-radius', '--stage', 'gate', '--after', 'route', '--when', 'coupling-high'],
            ['pstack:architect', '--stage', 'gate', '--after', 'route', '--when', 'tier-opus'],
            ['pstack:interrogate', '--stage', 'review', '--when', 'deep'],
        ]
        with mock.patch.dict(os.environ, {'PBI_PLUGINS_DIR': self.plugins}), contextlib.redirect_stdout(io.StringIO()):
            for args in adds:
                self.assertEqual(component.main(['--home', self.home, 'add', *args]), 0, args)
        return {'PBI_PLUGINS_DIR': self.plugins}

    def errors_for(self, workflow):
        write_json(os.path.join(self.base, 'workflow.json'), workflow)
        result = self.run_flow()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        return result.stdout.splitlines()

    def test_presets_resolve(self):
        for name in PRESETS:
            with self.subTest(name=name):
                result = self.run_flow('--workflow', name)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                rows = self.rows(result.stdout)
                stages = [row[0] for row in rows if not row[0].startswith('gate')]
                self.assertEqual(list(dict.fromkeys(stages)), list(flow.STAGES))

    def test_added_components_resolve(self):
        env = self.install_pstack()
        result = self.run_flow(env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = self.rows(result.stdout)
        self.assertEqual([row[1] for row in rows if row[0] == 'understand'], ['understand.pstack-how', 'understand.builtin'])
        self.assertEqual([row[1] for row in rows if row[0] == 'build'], ['build.runners'])

    def test_default_is_jev_routed(self):
        result = self.run_flow()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith('workflow jev-routed ('))

    def test_build_uses_the_worker_in_every_preset(self):
        for name in PRESETS:
            with self.subTest(name=name):
                rows = self.rows(self.run_flow('--workflow', name).stdout)
                self.assertEqual([row[1] for row in rows if row[0] == 'build'], ['build.runners'])

    def test_home_component_cannot_replace_a_bundled_one(self):
        with open(os.path.join(HERE, 'components', 'review.builtin.json'), encoding='utf-8') as f:
            write_json(os.path.join(self.home, 'components', 'review.builtin.json'), json.load(f))
        lines = self.run_flow().stdout.splitlines()
        self.assertIn('error home component review.builtin.json: a bundled component already has this id', lines)

    def test_gates_follow_their_stage(self):
        env = self.install_pstack()
        rows = self.rows(self.run_flow(env=env).stdout)
        places = [row[0] for row in rows]
        self.assertEqual(places.index('gate after plan'), places.index('plan') + 1)
        route_gates = [row[1] for row in rows if row[0] == 'gate after route']
        self.assertEqual(route_gates, ['gate.pstack-blast-radius', 'gate.pstack-architect'])

    def test_resolution_order(self):
        custom = bundled('jev-routed')
        custom['name'] = 'from-home'
        write_json(os.path.join(self.home, 'workflows', 'jev-routed.json'), custom)
        self.assertTrue(self.run_flow().stdout.startswith('workflow from-home ('))
        custom['name'] = 'from-repo'
        write_json(os.path.join(self.base, 'workflow.json'), custom)
        self.assertTrue(self.run_flow().stdout.startswith('workflow from-repo ('))
        # --workflow は作業リポジトリの workflow.json を読まない
        self.assertTrue(self.run_flow('--workflow', 'jev-routed').stdout.startswith('workflow from-home ('))

    def test_validation_errors(self):
        self.install_pstack()
        good = bundled('jev-routed')
        cases = []

        def case(expected, change):
            data = copy.deepcopy(good)
            change(data)
            cases.append((expected, data))

        case('error unknown stage: deploy', lambda d: d['stages'].update(deploy=['finish.builtin']))
        case('error verify: needs at least one component', lambda d: d['stages'].pop('verify'))
        case('error design: unknown component design.nope', lambda d: d['stages'].update(design=['design.nope', 'design.builtin']))
        case('error review: finish.builtin does not serve review', lambda d: d['stages'].update(review=['finish.builtin']))
        case('error review: the last component must be builtin or a script with no requires (review.code-review)',
             lambda d: d['stages'].update(review=['review.code-review']))
        case('error gate after plan: gate.pstack-blast-radius uses when coupling-high, which only a gate after route can decide',
             lambda d: d.update(gates=[{'after': 'plan', 'components': [{'id': 'gate.pstack-blast-radius', 'when': 'coupling-high'}]}]))
        case('error understand: understand.pstack-how has an unknown when: sometimes',
             lambda d: d['stages'].update(understand=[{'id': 'understand.pstack-how', 'when': 'sometimes'}, 'understand.builtin']))
        case('error fix.max_rework must be an integer from 0 to 5', lambda d: d.update(fix={'max_rework': 6}))
        case('error build.external_memory must be one of none, refs-only', lambda d: d.update(build={'external_memory': 'full'}))
        for expected, data in cases:
            with self.subTest(expected=expected):
                lines = self.errors_for(data)
                self.assertIn(expected, lines)
                self.assertNotIn(HEADER, lines)

    def write_route(self, pbi, tier, coupling, review):
        write_json(os.path.join(self.base, f'{pbi}.route.json'),
                   {'tier': tier, 'route': [], 'review': review, 'scores': {'coupling': coupling}})

    def test_decide_per_pbi_gates_and_deep_review(self):
        self.write_route('PBI-001', 'opus', 1, 'normal')
        self.write_route('PBI-002', 'sonnet', 2, 'deep')
        env = self.install_pstack()
        result = self.run_flow('--decide', env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[1], flow.DECIDE_HEADER)
        rows = [line.split('\t') for line in lines[2:]]
        self.assertEqual(rows, [
            ['gate after route', 'gate.pstack-blast-radius', 'PBI-001', 'skip', 'coupling 1'],
            ['gate after route', 'gate.pstack-blast-radius', 'PBI-002', 'run', 'coupling 2'],
            ['gate after route', 'gate.pstack-architect', 'PBI-001', 'run', 'tier opus'],
            ['gate after route', 'gate.pstack-architect', 'PBI-002', 'skip', 'tier sonnet'],
            ['review', 'review.pstack-interrogate', 'all', 'run', 'review deep: PBI-002'],
        ])

    def test_decide_rejects_bad_routes(self):
        self.write_route('PBI-001', 'opus', 1, 'normal')
        with open(os.path.join(self.base, 'PBI-002.route.json'), 'w', encoding='utf-8') as f:
            f.write('{broken')
        write_json(os.path.join(self.base, 'PBI-003.route.json'), {'tier': 'opus', 'scores': 2})
        result = self.run_flow('--decide')
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(flow.DECIDE_HEADER, result.stdout)
        self.assertIn('error PBI-002.route.json: not a readable JSON object', result.stderr)
        self.assertIn('error PBI-003.route.json: scores must be an object', result.stderr)

    def test_decide_needs_routes(self):
        result = self.run_flow('--decide')
        self.assertEqual(result.returncode, 2)
        self.assertIn('no PBI-*.route.json yet', result.stderr)

    def test_read_skill_status_finds_the_plugin(self):
        env = self.install_pstack()
        interrogate_dir = os.path.join(self.plugins, 'cache', 'cursor-plugins', 'pstack', '0.15.30', 'skills', 'interrogate')
        shutil.rmtree(interrogate_dir)
        rows = self.rows(self.run_flow(env=env).stdout)
        [how] = [row for row in rows if row[1] == 'understand.pstack-how']
        skill = os.path.join(self.plugins, 'cache', 'cursor-plugins', 'pstack', '0.15.30', 'skills', 'how', 'SKILL.md')
        self.assertEqual(how[6], f'plugin ok {skill}')
        [interrogate] = [row for row in rows if row[1] == 'review.pstack-interrogate']
        self.assertEqual(interrogate[6], 'plugin missing pstack')

    def test_missing_dir_and_workflow(self):
        missing = subprocess.run([sys.executable, SCRIPT, os.path.join(self.dir, 'nope'), '--home', self.home],
                                 capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(missing.returncode, 2)
        unknown = self.run_flow('--workflow', 'no-such-workflow')
        self.assertEqual(unknown.returncode, 2)
        self.assertIn('workflow not found', unknown.stderr)


class ComponentDefinitionsTest(unittest.TestCase):
    def test_bundled_components_are_valid(self):
        components, errors = flow.load_components()
        self.assertEqual(errors, [])
        self.assertGreater(len(components), 0)

    def test_bundled_components_call_no_one_elses_skill(self):
        # 同梱するのはこのスキルの部品と Claude Code の built-in だけ。他の人のスキルは component.py add で home に足す
        components, _ = flow.load_components()
        for component in components.values():
            with self.subTest(component=component['id']):
                self.assertNotEqual(component['invoke']['kind'], 'read-skill')
                self.assertNotIn('plugins', component.get('requires', {}))
                if component['invoke']['kind'] == 'skill':
                    self.assertEqual(component.get('link'), 'built-in')

    def test_then_must_name_a_known_component(self):
        directory = os.path.join(tempfile.gettempdir(), f'pbi-flow-components-{uuid.uuid4().hex}')
        os.mkdir(directory)
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        write_json(os.path.join(directory, 'x.y.json'), {
            'id': 'x.y', 'stages': ['design'], 'title': {'ja': 'あ', 'en': 'a'},
            'invoke': {'kind': 'builtin'}, 'doc': 'SKILL.md', 'where': 'main', 'memory': 'full',
            'when': 'always', 'cost': 'low', 'then': ['x.missing'], 'interactive': False, 'calls_back': False,
        })
        _, errors = flow.load_components(directory)
        self.assertEqual(errors, ['component x.y.json: then points at an unknown component: x.missing'])

    def test_runner_components_never_use_full_memory(self):
        components, _ = flow.load_components()
        for component in components.values():
            if component['where'] == 'runner':
                self.assertNotEqual(component['memory'], 'full', component['id'])

    def test_status_for_requires(self):
        base = {'invoke': {'kind': 'script', 'path': 'verify.py'}}
        self.assertEqual(flow.status(dict(base, requires={'env': ['KEY']}), None, {}), 'env missing KEY')
        self.assertEqual(flow.status(dict(base, requires={'env': ['KEY']}), None, {'KEY': 'x'}), 'script ok')
        self.assertEqual(flow.status(dict(base, requires={'cli': ['dsh']}), {'runners': {}}, {'PATH': ''}), 'cli missing dsh')
        self.assertEqual(flow.status({'invoke': {'kind': 'skill', 'name': 'x:y'}, 'requires': {'skills': ['x:y']}},
                                     None, {}), 'check skill list')
        self.assertEqual(flow.status({'invoke': {'kind': 'builtin'}}, None, {}), 'builtin')

    def test_component_errors(self):
        good = {
            'id': 'x.y', 'stages': ['review'], 'title': {'ja': 'あ', 'en': 'a'},
            'invoke': {'kind': 'builtin'}, 'doc': 'SKILL.md', 'where': 'main', 'memory': 'full',
            'when': 'always', 'cost': 'low', 'interactive': False, 'calls_back': False,
        }
        self.assertIsNone(flow.component_error(good, 'x.y'))
        bad = [
            ({'id': 'other'}, 'id does not match the file name'),
            ({'where': 'runner'}, 'a runner component cannot use memory full'),
            ({'requires': {'cli': ['dsh']}}, 'a builtin component cannot have requires'),
            ({'invoke': {'kind': 'script', 'path': 'no-such.py'}}, 'invoke points at a missing file: no-such.py'),
            ({'doc': 'appendix/no-such.md'}, 'doc points at a missing file: appendix/no-such.md'),
            ({'doc': None}, 'doc must name the file that holds the procedure'),
            ({'invoke': {'kind': 'read-skill', 'plugin': 'pstack'}}, 'invoke (read-skill) needs plugin, path'),
            ({'calls_back': 'no'}, 'calls_back must be true or false'),
        ]
        for change, expected in bad:
            with self.subTest(expected=expected):
                data = dict(good, **change)
                self.assertEqual(flow.component_error(data, 'x.y'), expected)


if __name__ == '__main__':
    unittest.main()
