"""setup_agents.py (検出・ヒアリングの記録・モデルの取り直し) の CLI テスト。

偽の opencode / codex / agy / dsh を PATH の先頭に置き、サブプロセスで CLI を動かして
agents.json と models/*.json だけを見る。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / 'setup_agents.py'
FIXTURES = HERE / 'testdata' / 'cli'

# 偽の CLI の本体。隣の <名前>.json の commands から、引数に合う応答を出す。
FAKE_SCRIPT = '''\
"""偽の CLI。同じ名前の .json の commands から、引数に合う応答を出す。"""
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
config = json.loads(Path(__file__).resolve().with_suffix('.json').read_text(encoding='utf-8'))
key = ' '.join(sys.argv[1:])
entry = config.get('commands', {}).get(key)
if entry is None:
    entry = config.get('default', {})
sys.stdout.write(entry.get('out', ''))
sys.stderr.write(entry.get('err', ''))
raise SystemExit(entry.get('code', 0))
'''


def fixture(name):
    """testdata/cli の fixture の中身。"""
    return (FIXTURES / name).read_text(encoding='utf-8')


def make_temp_dir():
    """中に書ける一時ディレクトリ。"""
    while True:
        path = Path(tempfile.gettempdir()) / f'pbi-setup-agents-{uuid.uuid4().hex}'
        try:
            path.mkdir()
            return path
        except FileExistsError:
            continue


def write_config(script, commands, default=None):
    """偽の CLI の応答を、スクリプトの隣の .json に書く。"""
    config = {'commands': commands}
    if default is not None:
        config['default'] = default
    Path(script).with_suffix('.json').write_text(json.dumps(config), encoding='utf-8')


def make_fake(directory, name, commands, default=None):
    """PATH に置く偽の CLI。Windows は .cmd、それ以外は実行権を付けたシェルスクリプト。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / f'{name}_fake.py'
    script.write_text(FAKE_SCRIPT, encoding='utf-8')
    write_config(script, commands, default)
    if os.name == 'nt':
        cli = directory / f'{name}.cmd'
        cli.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding='utf-8')
    else:
        cli = directory / name
        cli.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding='utf-8')
        cli.chmod(0o755)
    return cli


class Base(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()

    def env(self):
        """本物の CLI を拾わない env。PATH は偽の CLI と Python の実行に要るものだけ。"""
        env = dict(os.environ)
        parts = [str(self.bin), os.path.dirname(sys.executable)]
        system_root = env.get('SystemRoot')
        if system_root:
            parts.append(str(Path(system_root) / 'System32'))
        env['PATH'] = os.pathsep.join(parts)
        for key in ('FNM_DIR', 'NVM_DIR', 'NVM_HOME', 'PBI_DSH_BIN', 'ATELIER_HOME', 'PBI_HOME', 'PBI_TEMPLATES_HOME'):
            env.pop(key, None)
        for key, name in (('APPDATA', 'appdata'), ('LOCALAPPDATA', 'localappdata'),
                          ('HOME', 'home'), ('USERPROFILE', 'userprofile'),
                          ('XDG_CACHE_HOME', 'cache'), ('XDG_CONFIG_HOME', 'config')):
            path = self.root / 'env' / name
            path.mkdir(parents=True, exist_ok=True)
            env[key] = str(path)
        return env

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args, '--home', str(self.home)],
                              cwd=str(HERE), env=self.env(), capture_output=True, text=True,
                              encoding='utf-8', errors='replace')

    def fake(self, name, commands):
        return make_fake(self.bin, name, commands)

    def add_opencode(self, text=None, code=0):
        output = fixture('opencode-models.txt') if text is None else text
        return self.fake('opencode', {'--version': {'out': '2.0.29\n'},
                                      'models --standalone': {'out': output, 'code': code}})

    def add_codex(self, code=0):
        return self.fake('codex', {'--version': {'out': 'codex-cli 0.153.4\n'},
                                   'debug models': {'out': fixture('codex-debug-models.json'), 'code': code}})

    def add_agy(self, code=0):
        return self.fake('agy', {'--version': {'out': '1.4.3\n'},
                                 'models': {'out': fixture('agy-models.txt'), 'code': code}})

    def add_dsh(self, code=0):
        return self.fake('dsh', {'--version': {'out': '0.2.0-rc.2\n'},
                                 'headless --dump-config': {'out': fixture('dsh-dump-config.yml'),
                                                            'code': code}})

    def read_agents(self):
        return json.loads((self.home / 'agents.json').read_text(encoding='utf-8'))

    def read_models(self, runner):
        return json.loads((self.home / 'models' / f'{runner}.json').read_text(encoding='utf-8'))

    def seed_agents(self, runners):
        (self.home / 'agents.json').write_text(json.dumps({'runners': runners}), encoding='utf-8')


class DetectTest(Base):
    def test_found_missing_and_agent(self):
        self.add_opencode()
        self.add_codex()
        proc = self.run_cli('detect', '--json')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        detected = json.loads(proc.stdout)

        self.assertTrue(detected['opencode']['found'])
        self.assertEqual(Path(detected['opencode']['path']).name.lower(),
                         'opencode.cmd' if os.name == 'nt' else 'opencode')
        self.assertEqual(detected['opencode']['version'], '2.0.29')
        self.assertTrue(detected['codex']['found'])
        self.assertEqual(detected['codex']['version'], 'codex-cli 0.153.4')
        # 偽の CLI が無い実行先は missing
        for runner in ('agy', 'dsh', 'claude'):
            self.assertFalse(detected[runner]['found'])
            self.assertIsNone(detected[runner]['path'])
            self.assertIsNone(detected[runner]['version'])
        # agent は常に found
        self.assertTrue(detected['agent']['found'])
        self.assertIsNone(detected['agent']['path'])
        self.assertIsNone(detected['agent']['version'])

    def test_table_has_one_line_per_runner(self):
        self.add_opencode()
        proc = self.run_cli('detect')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.strip().splitlines()
        self.assertEqual(len(lines), 6)
        self.assertTrue(any(line.startswith('agent  found  -  -') for line in lines))
        self.assertTrue(any(line.startswith('dsh  missing  -  -') for line in lines))


class RecordTest(Base):
    def test_records_enabled_flags_and_keeps_existing_keys(self):
        self.add_opencode()
        self.add_agy()
        self.seed_agents({
            'opencode': {'enabled': False, 'path': 'old', 'version': 'old', 'checked_at': 'old',
                         'models': ['opencode-go/beam-free'], 'billing': 'api', 'note': 'keep'},
            'codex': {'enabled': True, 'models': ['gpt-6-astra']},
        })
        proc = self.run_cli('record', '--use', 'opencode,agy', '--now', '2026-10-21T12:00:00+00:00')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        agents = self.read_agents()

        for runner in ('opencode', 'agy', 'agent'):
            self.assertTrue(agents['runners'][runner]['enabled'], runner)
        for runner in ('codex', 'dsh', 'claude'):
            self.assertFalse(agents['runners'][runner]['enabled'], runner)

        for runner, version in (('opencode', '2.0.29'), ('agy', '1.4.3')):
            config = agents['runners'][runner]
            self.assertEqual(Path(config['path']).name.lower(),
                             f'{runner}.cmd' if os.name == 'nt' else runner)
            self.assertEqual(config['version'], version)
            self.assertEqual(config['checked_at'], '2026-10-21T12:00:00+00:00')

        # 既存の models・billing・そのほかのキーは残る
        self.assertEqual(agents['runners']['opencode']['models'], ['opencode-go/beam-free'])
        self.assertEqual(agents['runners']['opencode']['billing'], 'api')
        self.assertEqual(agents['runners']['opencode']['note'], 'keep')
        self.assertEqual(agents['runners']['codex']['models'], ['gpt-6-astra'])
        # 出力は 1 実行先 1 行
        self.assertIn('opencode  on  ' + agents['runners']['opencode']['path'], proc.stdout)
        self.assertIn('codex  off  -', proc.stdout)

    def test_empty_use_enables_agent_only(self):
        self.add_opencode()
        proc = self.run_cli('record', '--use', '')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        agents = self.read_agents()['runners']
        self.assertTrue(agents['agent']['enabled'])
        for runner in ('opencode', 'codex', 'agy', 'dsh', 'claude'):
            self.assertFalse(agents[runner]['enabled'], runner)


class ModelsTest(Base):
    def test_writes_enabled_runners(self):
        self.add_opencode()
        self.add_agy()
        self.run_cli('record', '--use', 'opencode,agy')
        proc = self.run_cli('models')
        self.assertEqual(proc.returncode, 0, proc.stderr)

        for runner, method, count in (('opencode', 'cli', 6), ('agy', 'cli', 13), ('agent', 'builtin', 4)):
            data = self.read_models(runner)
            self.assertEqual(data['runner'], runner)
            self.assertTrue(data['fetched_at'])
            self.assertEqual(data['method'], method)
            self.assertIsNone(data['error'])
            self.assertIsNone(data['failed_at'])
            self.assertEqual(len(data['models']), count)
            self.assertIn(f'{runner}  ok  {count} models ({method})', proc.stdout)

        self.assertEqual([m['id'] for m in self.read_models('opencode')['models']][0],
                         'opencode-go/beam-free')
        self.assertEqual(len(self.read_models('agy')['models']), 13)
        self.assertEqual([m['id'] for m in self.read_models('agent')['models']],
                         ['haiku', 'sonnet', 'opus', 'fable'])
        # enabled でない codex は取りに行かない
        self.assertFalse((self.home / 'models' / 'codex.json').exists())

    def test_selected_models_stay_after_a_refresh(self):
        self.add_opencode()
        self.run_cli('record', '--use', 'opencode')
        chosen = ['opencode-go/beam-free', 'opencode-go/glm-5.2']
        self.assertEqual(self.run_cli('select', '--runner', 'opencode',
                                      '--models', ','.join(chosen)).returncode, 0)

        proc = self.run_cli('models', '--runner', 'opencode')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read_agents()['runners']['opencode']['models'], chosen)
        self.assertEqual([m['id'] for m in self.read_models('opencode')['models']],
                         ['opencode-go/beam-free', 'opencode-go/claude-haiku-5-5',
                          'opencode-go/deepseek-v4-pro', 'opencode-go/deepseek-v4.1-flash',
                          'opencode-go/gemini-4-argon-contributor', 'opencode-go/glm-5.2'])

    def test_select_with_empty_models_removes_the_selection(self):
        self.add_opencode()
        self.run_cli('record', '--use', 'opencode')
        self.run_cli('select', '--runner', 'opencode', '--models', 'opencode-go/beam-free')
        proc = self.run_cli('select', '--runner', 'opencode', '--models', '')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read_agents()['runners']['opencode']['models'], [])

    def test_warns_about_a_selected_model_that_disappeared(self):
        self.add_opencode()
        self.run_cli('record', '--use', 'opencode')
        self.run_cli('select', '--runner', 'opencode', '--models', 'opencode-go/beam-free')
        # 偽の CLI の出力から選んだモデルを消して取り直す
        self.add_opencode(text='opencode-go/glm-5.2\n')
        proc = self.run_cli('models', '--runner', 'opencode')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('warn opencode: selected model opencode-go/beam-free is no longer listed',
                      proc.stdout)
        self.assertEqual([m['id'] for m in self.read_models('opencode')['models']],
                         ['opencode-go/glm-5.2'])

    def test_failure_keeps_the_previous_list_and_does_not_stop_others(self):
        self.add_codex()
        self.run_cli('record', '--use', 'codex')
        self.assertEqual(self.run_cli('models', '--runner', 'codex').returncode, 0)
        before = self.read_models('codex')

        self.add_codex(code=1)
        proc = self.run_cli('models', '--runner', 'codex')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        after = self.read_models('codex')
        self.assertIn('codex  failed', proc.stdout)
        self.assertIn('exited with code 1', after['error'])
        self.assertTrue(after['failed_at'])
        self.assertEqual(after['fetched_at'], before['fetched_at'])
        self.assertEqual(after['method'], 'cli')
        self.assertEqual([m['id'] for m in after['models']],
                         [m['id'] for m in before['models']])

        # ほかの実行先 (agent) は ok
        all_proc = self.run_cli('models')
        self.assertEqual(all_proc.returncode, 0, all_proc.stderr)
        self.assertIn('codex  failed', all_proc.stdout)
        self.assertIn('agent  ok  4 models (builtin)', all_proc.stdout)

    def test_failure_without_a_previous_file_creates_a_manual_file(self):
        self.add_codex(code=1)
        self.run_cli('record', '--use', 'codex')
        proc = self.run_cli('models', '--runner', 'codex')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = self.read_models('codex')
        self.assertEqual(data['models'], [])
        self.assertEqual(data['method'], 'manual')
        self.assertIsNone(data['fetched_at'])
        self.assertTrue(data['error'])
        self.assertTrue(data['failed_at'])
        self.assertIn('codex  failed', proc.stdout)

    def test_dsh_uses_the_bundled_catalog(self):
        self.add_dsh()
        self.run_cli('record', '--use', 'dsh')
        proc = self.run_cli('models', '--runner', 'dsh')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = self.read_models('dsh')
        self.assertEqual(data['method'], 'catalog')
        self.assertEqual([m['id'] for m in data['models']], ['deepseek-official/deepseek-flash'])
        self.assertIn('dsh  ok  1 models (catalog)', proc.stdout)


class AddManualModelTest(Base):
    def test_adds_and_keeps_a_manual_model_across_a_refresh(self):
        self.add_opencode()
        self.run_cli('record', '--use', 'opencode')
        proc = self.run_cli('add-model', '--runner', 'opencode', '--id', 'local/secret',
                            '--effort', 'low', '--effort', 'high')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = self.read_models('opencode')
        self.assertEqual(data['method'], 'manual')
        entry = [m for m in data['models'] if m['id'] == 'local/secret'][0]
        self.assertEqual(entry['efforts'], ['low', 'high'])
        self.assertTrue(entry['manual'])
        self.assertIsNone(entry['name'])

        # 取り直しても手入力の項目は末尾に残る
        proc = self.run_cli('models', '--runner', 'opencode')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = self.read_models('opencode')
        self.assertEqual(data['method'], 'cli')
        self.assertEqual(data['models'][-1]['id'], 'local/secret')
        self.assertTrue(data['models'][-1]['manual'])

    def test_same_id_replaces_efforts_and_marks_manual(self):
        self.add_opencode()
        self.run_cli('record', '--use', 'opencode')
        self.run_cli('models', '--runner', 'opencode')
        self.run_cli('add-model', '--runner', 'opencode', '--id', 'opencode-go/glm-5.2',
                     '--effort', 'medium')
        data = self.read_models('opencode')
        entries = [m for m in data['models'] if m['id'] == 'opencode-go/glm-5.2']
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['efforts'], ['medium'])
        self.assertTrue(entries[0]['manual'])
        # キャッシュに無いモデルなので名前は None のまま
        self.assertIsNone(entries[0]['name'])


class UsageTest(Base):
    def test_unknown_runner_is_exit_code_2_with_one_line(self):
        proc = self.run_cli('models', '--runner', 'nope')
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(len(proc.stderr.strip().splitlines()), 1)
        self.assertEqual(proc.stdout, '')

    def test_missing_required_option_is_exit_code_2(self):
        for args in (('record',), ('select', '--runner', 'opencode'),
                     ('add-model', '--runner', 'opencode'), ('bogus',)):
            with self.subTest(args=args):
                proc = self.run_cli(*args)
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(len(proc.stderr.strip().splitlines()), 1)

    def test_select_does_not_change_the_enabled_flag_of_agent(self):
        # select は models だけを変えるので enabled は触らない
        self.assertEqual(self.run_cli('select', '--runner', 'agent', '--models', 'haiku').returncode, 0)
        self.assertTrue(self.read_agents()['runners']['agent']['enabled'])


def load_module():
    """同じフォルダの setup_agents を、関数を直接呼ぶテスト用に import する。"""
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import setup_agents
    return setup_agents


def tree(root):
    """root の下の全ファイルとフォルダ (相対パス) と、ファイルの中身。"""
    result = {}
    for path in sorted(Path(root).rglob('*')):
        key = path.relative_to(root).as_posix()
        result[key] = path.read_bytes() if path.is_file() else None
    return result


class ManualSkeletonTest(Base):
    EXPECTED = {'runner': 'opencode', 'fetched_at': None, 'method': 'manual',
                'error': None, 'failed_at': None,
                'models': [{'id': 'm1', 'name': None, 'efforts': ['high'], 'manual': True}]}

    def test_add_model_without_a_models_file_writes_the_whole_skeleton(self):
        proc = self.run_cli('add-model', '--runner', 'opencode', '--id', 'm1', '--effort', 'high')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read_models('opencode'), self.EXPECTED)

    def test_add_model_over_a_broken_models_file_starts_from_the_skeleton(self):
        (self.home / 'models').mkdir()
        (self.home / 'models' / 'opencode.json').write_text('{not json', encoding='utf-8')
        proc = self.run_cli('add-model', '--runner', 'opencode', '--id', 'm1', '--effort', 'high')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read_models('opencode'), self.EXPECTED)


class RejectedInputWritesNothingTest(Base):
    def setUp(self):
        super().setUp()
        self.setup_agents = load_module()
        self.seed_agents({'opencode': {'enabled': True, 'models': ['keep']}})

    def test_disabling_agent_raises_and_leaves_agents_json_as_it_was(self):
        before = (self.home / 'agents.json').read_bytes()
        with self.assertRaises(ValueError):
            self.setup_agents.set_runner(self.home, 'agent', enabled=False)
        self.assertEqual((self.home / 'agents.json').read_bytes(), before)

    def test_models_with_an_empty_id_raise_and_leave_agents_json_as_it_was(self):
        before = (self.home / 'agents.json').read_bytes()
        with self.assertRaises(ValueError):
            self.setup_agents.set_runner(self.home, 'opencode', models=['a', ''])
        self.assertEqual((self.home / 'agents.json').read_bytes(), before)

    def test_a_valid_models_list_is_written(self):
        # 上の 2 件が「書けない入力だから書かない」ことの対 (有効な入力なら書く)
        self.setup_agents.set_runner(self.home, 'opencode', models=['a', 'b'])
        self.assertEqual(self.read_agents()['runners']['opencode']['models'], ['a', 'b'])

    def test_empty_model_id_raises_and_creates_no_models_file(self):
        for model_id in ('', '   '):
            with self.assertRaises(ValueError):
                self.setup_agents.add_manual_model(self.home, 'opencode', model_id, [])
        self.assertFalse((self.home / 'models' / 'opencode.json').exists())


class RunnerTableSettingsTest(Base):
    def test_remove_drops_the_models_key_and_billing_is_recorded(self):
        setup_agents = load_module()
        self.seed_agents({'opencode': {'enabled': True, 'models': ['a'], 'path': '/x'}})
        setup_agents.set_runner(self.home, 'opencode', models=setup_agents.REMOVE, billing='flat')
        config = self.read_agents()['runners']['opencode']
        self.assertEqual(config, {'enabled': True, 'path': '/x', 'billing': 'flat'})


class PureFunctionsWriteNothingTest(Base):
    def test_manual_models_and_runner_table_leave_the_home_tree_unchanged(self):
        setup_agents = load_module()
        before = tree(self.root)
        models = setup_agents.manual_models(None, 'opencode', 'm1', ['high'])
        table = setup_agents.runner_table(None, 'opencode', True, ['m1'], 'flat')
        self.assertEqual(tree(self.root), before)
        # 何も書かない代わりに、結果は返す
        self.assertEqual([entry['id'] for entry in models['models']], ['m1'])
        self.assertEqual(table['runners']['opencode'],
                         {'enabled': True, 'models': ['m1'], 'billing': 'flat'})


DEF_FILES = ('atelier-designer.md', 'atelier-designer-skills.md', 'atelier-reviewer.md', 'atelier-tester.md',
             'pbi-worker.md')


def frontmatter(path):
    """定義のファイルの frontmatter (キー → 値) と本文。"""
    text = Path(path).read_text(encoding='utf-8').replace('\r\n', '\n')
    head, _, body = text[len('---\n'):].partition('\n---\n')
    meta = {}
    for line in head.split('\n'):
        key, sep, value = line.partition(':')
        if sep:
            meta[key.strip()] = value.strip()
    return meta, body


def sha256(path):
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class DefinitionsTest(Base):
    """defs --install: 写す先・.git/info/exclude・agents.json の記録。"""

    def setUp(self):
        super().setUp()
        self.claude = self.root / 'claude'
        self.work = self.root / 'work'
        (self.work / '.git' / 'info').mkdir(parents=True)

    def defs(self, *args, session=None):
        env = self.env()
        env['CLAUDE_CONFIG_DIR'] = str(self.claude)
        env.pop('CLAUDE_CODE_SESSION_ID', None)
        if session is not None:
            env['CLAUDE_CODE_SESSION_ID'] = session
        return subprocess.run([sys.executable, str(SCRIPT), 'defs', *args, '--home', str(self.home)],
                              cwd=str(self.work), env=env, capture_output=True, text=True,
                              encoding='utf-8', errors='replace')

    def test_bundled_definitions_carry_the_defs_version(self):
        setup_agents = load_module()
        self.assertEqual(tuple(setup_agents.DEF_FILES), DEF_FILES)
        for name in DEF_FILES:
            meta, _ = frontmatter(HERE / 'agents' / name)
            self.assertEqual(meta.get('atelier-defs'), str(setup_agents.DEFS_VERSION), name)

    def test_the_two_designer_definitions_differ_only_in_name_description_and_skill(self):
        plain, plain_body = frontmatter(HERE / 'agents' / 'atelier-designer.md')
        skills, skills_body = frontmatter(HERE / 'agents' / 'atelier-designer-skills.md')
        self.assertEqual(plain_body, skills_body)
        self.assertEqual((plain['name'], skills['name']), ('atelier-designer', 'atelier-designer-skills'))
        self.assertNotEqual(plain['description'], skills['description'])
        plain_tools = [tool.strip() for tool in plain['tools'].split(',')]
        skills_tools = [tool.strip() for tool in skills['tools'].split(',')]
        self.assertNotIn('Skill', plain_tools)
        self.assertEqual([tool for tool in skills_tools if tool != 'Skill'], plain_tools)
        self.assertIn('Skill', skills_tools)
        for key in set(plain) - {'name', 'description', 'tools'}:
            self.assertEqual(plain[key], skills.get(key), key)

    def test_install_home_copies_every_definition_and_records_hashes_and_session(self):
        self.seed_agents({'agent': {'enabled': True}})
        proc = self.defs('--install', 'home', session='caller-1')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        target = self.claude / 'agents'
        self.assertIn(f'defs home: {target.as_posix()}', proc.stdout)
        for name in DEF_FILES:
            self.assertEqual((target / name).read_bytes(), (HERE / 'agents' / name).read_bytes(), name)
        table = self.read_agents()
        self.assertEqual(table['runners'], {'agent': {'enabled': True}})  # 前の記録は残す
        record = table['agent_defs']
        self.assertEqual(record['place'], 'home')
        self.assertEqual(record['session'], 'caller-1')
        self.assertEqual(record['hashes'], {name: sha256(target / name) for name in DEF_FILES})
        self.assertFalse((self.work / '.claude').exists())

    def test_install_keeps_an_earlier_copy_beside_it(self):
        """前からある別の中身の定義は .atelier-old として隣に残し、同梱の版を写す。"""
        target = self.claude / 'agents'
        target.mkdir(parents=True)
        old = '---\nname: atelier-designer\n---\nold copy\n'
        (target / 'atelier-designer.md').write_text(old, encoding='utf-8')
        proc = self.defs('--install', 'home')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((target / 'atelier-designer.md.atelier-old').read_text(encoding='utf-8'), old)
        self.assertEqual((target / 'atelier-designer.md').read_bytes(),
                         (HERE / 'agents' / 'atelier-designer.md').read_bytes())
        self.assertFalse((target / 'atelier-tester.md.atelier-old').exists())

    def test_a_copy_without_the_version_counts_as_missing(self):
        """atelier-defs の無い古い写しは、足りない定義として数える。"""
        target = self.claude / 'agents'
        target.mkdir(parents=True)
        for name in DEF_FILES:
            shutil.copyfile(HERE / 'agents' / name, target / name)
        (target / 'atelier-designer.md').write_text('---\nname: atelier-designer\ntools: Agent, Skill\n---\nold\n',
                                                    encoding='utf-8')
        sys.path.insert(0, str(HERE))
        import setup_agents
        env = {**os.environ, 'CLAUDE_CONFIG_DIR': str(self.claude)}
        self.assertEqual(setup_agents.missing_definitions(env, self.work), ['atelier-designer.md'])

    def test_install_work_copies_to_the_work_root_and_excludes_it_once(self):
        exclude = self.work / '.git' / 'info' / 'exclude'
        exclude.write_text('# git ls-files --others --exclude-from=.git/info/exclude\n', encoding='utf-8')
        for _ in range(2):
            proc = self.defs('--install', 'work')
            self.assertEqual(proc.returncode, 0, proc.stderr)
        target = self.work / '.claude' / 'agents'
        self.assertEqual(sorted(p.name for p in target.iterdir()), sorted(DEF_FILES))
        lines = exclude.read_text(encoding='utf-8').splitlines()
        self.assertEqual(lines.count('.claude/agents/'), 1, lines)
        self.assertTrue(lines[0].startswith('#'))  # もとの行は残す
        record = self.read_agents()['agent_defs']
        self.assertEqual((record['place'], record['session']), ('work', ''))
        self.assertEqual(Path(record['work_root']).resolve(), self.work.resolve())
        self.assertFalse((self.claude / 'agents').exists())

    def test_install_none_copies_nothing_and_records_the_answer(self):
        proc = self.defs('--install', 'none', session='caller-2')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('defs none', proc.stdout)
        self.assertFalse((self.claude / 'agents').exists())
        self.assertFalse((self.work / '.claude').exists())
        record = self.read_agents()['agent_defs']
        self.assertEqual((record['place'], record['hashes']), ('none', {}))

    def test_unknown_place_is_exit_code_2_and_writes_nothing(self):
        proc = self.defs('--install', 'elsewhere')
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertFalse((self.home / 'agents.json').exists())


if __name__ == '__main__':
    unittest.main()
