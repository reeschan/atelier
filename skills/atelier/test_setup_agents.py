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


if __name__ == '__main__':
    unittest.main()
