"""agents.py (agents.json の実行先と route の手順を外す判定) の単体テスト。"""
import json
import os
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import agents

OPENCODE = {'runner': 'opencode', 'model': 'opencode-go/deepseek-v4.1-flash', 'effort': None}
CODEX = {'runner': 'codex', 'model': 'gpt-6-astra', 'effort': 'high'}
AGENT_STEP = {'runner': 'agent', 'model': 'sonnet', 'effort': 'high'}


def make_cli(directory, name):
    """PATH に置く偽の CLI。Windows は .cmd、それ以外は実行権を付けたシェルスクリプト。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == 'nt':
        path = directory / f'{name}.cmd'
        path.write_text('@echo off\n', encoding='utf-8')
    else:
        path = directory / name
        path.write_text('#!/bin/sh\n', encoding='utf-8')
        path.chmod(0o755)
    return path


def make_file(directory, name):
    """拡張子を付けない偽の CLI (fnm の installation/bin と nvm の bin に置く)。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text('#!/bin/sh\n', encoding='utf-8')
    if os.name != 'nt':
        path.chmod(0o755)
    return path


def make_temp_dir():
    """中に書ける一時ディレクトリ。既定の権限で作る (tempfile の 0o700 は権限を絞った環境で読めなくなる)。"""
    while True:
        path = Path(tempfile.gettempdir()) / f'pbi-agents-{uuid.uuid4().hex}'
        try:
            path.mkdir()
            return path
        except FileExistsError:
            continue


class Base(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.empty = self.root / 'empty'
        self.empty.mkdir()

    def env(self, **extra):
        """PATH を空のディレクトリだけにした環境。extra で FNM_DIR などを足す。"""
        env = {'PATH': str(self.empty)}
        env.update(extra)
        return env

    def assertFound(self, actual, expected):
        self.assertIsNotNone(actual)
        self.assertEqual(Path(actual), Path(expected))

    def found(self, steps, table, env):
        return {s['runner']: agents.find_cli(s['runner'], table, env) for s in steps}


class VersionKeyTest(unittest.TestCase):
    def test_numeric_parts(self):
        self.assertEqual(agents._version_key('v26.8.1'), (26, 8, 1))
        self.assertLess(agents._version_key('v9.0.0'), agents._version_key('v26.8.1'))

    def test_the_leading_v_is_optional(self):
        self.assertEqual(agents._version_key('9.0.0'), (9, 0, 0))

    def test_non_numeric_parts_are_zero(self):
        self.assertEqual(agents._version_key('v1.2.rc'), (1, 2, 0))


class LoadAgentsTest(Base):
    def write(self, text):
        (self.root / 'agents.json').write_text(text, encoding='utf-8')

    def test_missing_file_is_none(self):
        self.assertIsNone(agents.load_agents(self.root))

    def test_broken_json_is_none(self):
        self.write('{oops')
        self.assertIsNone(agents.load_agents(self.root))

    def test_top_level_not_an_object_is_none(self):
        self.write('[]')
        self.assertIsNone(agents.load_agents(self.root))

    def test_runners_not_an_object_is_none(self):
        for runners in ([], 'opencode', 1, None):
            with self.subTest(runners=runners):
                self.write(json.dumps({'runners': runners}))
                self.assertIsNone(agents.load_agents(self.root))

    def test_valid_file_returns_the_data(self):
        data = {'runners': {'opencode': {'enabled': True}, 'codex': {'enabled': False}}}
        self.write(json.dumps(data))
        self.assertEqual(agents.load_agents(self.root), data)


class FindCliTest(Base):
    def test_agents_path_is_used_without_the_path_variable(self):
        cli = make_cli(self.root / 'bin', 'opencode')
        table = {'runners': {'opencode': {'path': str(cli)}}}
        self.assertFound(agents.find_cli('opencode', table, self.env()), cli)

    def test_agents_path_must_be_an_existing_file(self):
        directory = self.root / 'not-a-file'
        directory.mkdir()
        for path in (directory, self.root / 'nope'):
            with self.subTest(path=path):
                table = {'runners': {'opencode': {'path': str(path)}}}
                self.assertIsNone(agents.find_cli('opencode', table, self.env()))

    def test_path_finds_the_cli(self):
        cli = make_cli(self.empty, 'opencode')
        self.assertFound(agents.find_cli('opencode', None, self.env()), cli)

    def test_path_without_the_cli_is_none(self):
        self.assertIsNone(agents.find_cli('opencode', None, self.env()))

    def test_omitted_env_uses_os_environ(self):
        cli = make_cli(self.empty, 'opencode')
        with mock.patch.dict(os.environ, {'PATH': str(self.empty)}):
            self.assertFound(agents.find_cli('opencode', None), cli)

    def test_fnm_installation(self):
        cli = make_cli(self.root / 'fnm' / 'node-versions' / 'v20.1.0' / 'installation', 'opencode')
        env = self.env(FNM_DIR=str(self.root / 'fnm'))
        self.assertFound(agents.find_cli('opencode', None, env), cli)

    def test_cli_suffixes_order(self):
        self.assertEqual(agents.cli_suffixes('nt'), ('.cmd', '.exe', ''))
        self.assertEqual(agents.cli_suffixes('posix'), ('',))

    def test_fnm_prefers_cmd_with_the_windows_suffix_order(self):
        installation = self.root / 'fnm' / 'node-versions' / 'v20.1.0' / 'installation'
        make_file(installation, 'opencode')
        cmd = installation / 'opencode.cmd'
        cmd.write_text('@echo off\n', encoding='utf-8')
        env = self.env(FNM_DIR=str(self.root / 'fnm'))
        with mock.patch.object(agents, 'cli_suffixes', return_value=agents.CLI_SUFFIXES_WINDOWS):
            self.assertFound(agents.find_cli('opencode', None, env), cmd)

    def test_fnm_uses_numeric_version_order(self):
        make_cli(self.root / 'fnm' / 'node-versions' / 'v9.0.0' / 'installation', 'opencode')
        new = make_cli(self.root / 'fnm' / 'node-versions' / 'v26.8.1' / 'installation', 'opencode')
        env = self.env(FNM_DIR=str(self.root / 'fnm'))
        self.assertFound(agents.find_cli('opencode', None, env), new)

    def test_fnm_installation_bin(self):
        cli = make_file(self.root / 'fnm' / 'node-versions' / 'v20.1.0' / 'installation' / 'bin', 'opencode')
        env = self.env(FNM_DIR=str(self.root / 'fnm'))
        self.assertFound(agents.find_cli('opencode', None, env), cli)

    def test_fnm_uses_the_latest_version(self):
        make_cli(self.root / 'fnm' / 'node-versions' / 'v18.0.0' / 'installation', 'opencode')
        new = make_cli(self.root / 'fnm' / 'node-versions' / 'v20.1.0' / 'installation', 'opencode')
        env = self.env(FNM_DIR=str(self.root / 'fnm'))
        self.assertFound(agents.find_cli('opencode', None, env), new)

    @unittest.skipUnless(os.name == 'nt', 'Windows の既定の場所')
    def test_fnm_uses_appdata_when_fnm_dir_is_absent(self):
        appdata = self.root / 'appdata'
        cli = make_cli(appdata / 'fnm' / 'node-versions' / 'v20.1.0' / 'installation', 'opencode')
        self.assertFound(agents.find_cli('opencode', None, self.env(APPDATA=str(appdata))), cli)

    @unittest.skipIf(os.name == 'nt', 'Windows 以外の既定の場所')
    def test_fnm_uses_home_when_fnm_dir_is_absent(self):
        home = self.root / 'home'
        cli = make_cli(home / '.local' / 'share' / 'fnm' / 'node-versions' / 'v20.1.0' / 'installation',
                       'opencode')
        self.assertFound(agents.find_cli('opencode', None, self.env(HOME=str(home))), cli)

    def test_nvm_dir(self):
        cli = make_file(self.root / 'nvm' / 'versions' / 'node' / 'v18.20.0' / 'bin', 'opencode')
        env = self.env(NVM_DIR=str(self.root / 'nvm'))
        self.assertFound(agents.find_cli('opencode', None, env), cli)

    def test_nvm_dir_uses_the_latest_version(self):
        make_file(self.root / 'nvm' / 'versions' / 'node' / 'v18.20.0' / 'bin', 'opencode')
        new = make_file(self.root / 'nvm' / 'versions' / 'node' / 'v20.11.0' / 'bin', 'opencode')
        env = self.env(NVM_DIR=str(self.root / 'nvm'))
        self.assertFound(agents.find_cli('opencode', None, env), new)

    def test_nvm_dir_uses_numeric_version_order(self):
        make_file(self.root / 'nvm' / 'versions' / 'node' / 'v9.0.0' / 'bin', 'opencode')
        new = make_file(self.root / 'nvm' / 'versions' / 'node' / 'v26.8.1' / 'bin', 'opencode')
        env = self.env(NVM_DIR=str(self.root / 'nvm'))
        self.assertFound(agents.find_cli('opencode', None, env), new)

    @unittest.skipUnless(os.name == 'nt', 'nvm-windows の既定の場所')
    def test_nvm_home(self):
        cli = make_cli(self.root / 'nvm-home' / 'v20.11.0', 'opencode')
        env = self.env(NVM_HOME=str(self.root / 'nvm-home'))
        self.assertFound(agents.find_cli('opencode', None, env), cli)

    @unittest.skipUnless(os.name == 'nt', 'nvm-windows の既定の場所')
    def test_nvm_home_uses_the_latest_version(self):
        make_cli(self.root / 'nvm-home' / 'v18.20.0', 'opencode')
        new = make_cli(self.root / 'nvm-home' / 'v20.11.0', 'opencode')
        env = self.env(NVM_HOME=str(self.root / 'nvm-home'))
        self.assertFound(agents.find_cli('opencode', None, env), new)

    @unittest.skipUnless(os.name == 'nt', 'nvm-windows の既定の場所')
    def test_nvm_home_uses_numeric_version_order(self):
        make_cli(self.root / 'nvm-home' / 'v9.0.0', 'opencode')
        new = make_cli(self.root / 'nvm-home' / 'v26.8.1', 'opencode')
        env = self.env(NVM_HOME=str(self.root / 'nvm-home'))
        self.assertFound(agents.find_cli('opencode', None, env), new)

    def test_dsh_bin_environment_variable(self):
        cli = make_file(self.root / 'dsh', 'bin.js')
        self.assertFound(agents.find_cli('dsh', None, self.env(PBI_DSH_BIN=str(cli))), cli)

    def test_dsh_bin_must_be_an_existing_file(self):
        self.assertIsNone(agents.find_cli('dsh', None, self.env(PBI_DSH_BIN=str(self.root / 'nope'))))

    def test_dsh_bin_does_not_apply_to_other_runners(self):
        cli = make_file(self.root / 'dsh', 'bin.js')
        self.assertIsNone(agents.find_cli('opencode', None, self.env(PBI_DSH_BIN=str(cli))))

    def test_agy_default_location_on_windows(self):
        exe = make_file(self.root / 'local' / 'agy' / 'bin', 'agy.exe')
        env = self.env(LOCALAPPDATA=str(self.root / 'local'))
        self.assertFound(agents.default_location('agy', env, 'nt'), exe)
        # Windows 以外・ほかの実行先・ファイルが無いときは見ない
        self.assertIsNone(agents.default_location('agy', env, 'posix'))
        self.assertIsNone(agents.default_location('codex', env, 'nt'))
        self.assertIsNone(agents.default_location('agy', self.env(LOCALAPPDATA=str(self.empty)), 'nt'))

    @unittest.skipUnless(os.name == 'nt', 'Windows の agy の既定の場所')
    def test_find_cli_falls_back_to_agy_default_location(self):
        exe = make_file(self.root / 'local' / 'agy' / 'bin', 'agy.exe')
        self.assertFound(agents.find_cli('agy', None, self.env(LOCALAPPDATA=str(self.root / 'local'))), exe)


class DropReasonTest(Base):
    def test_agent_is_never_dropped(self):
        table = {'runners': {'agent': {'enabled': False, 'models': []}}}
        step = {'runner': 'agent', 'model': 'opus', 'effort': None}
        self.assertIsNone(agents.drop_reason(step, table, {}))

    def test_disabled_runner_is_dropped_even_if_found(self):
        table = {'runners': {'opencode': {'enabled': False}}}
        self.assertEqual(agents.drop_reason(OPENCODE, table, {'opencode': 'x'}),
                         agents.REASON_DISABLED)

    def test_disabled_wins_over_missing(self):
        table = {'runners': {'opencode': {'enabled': False}}}
        self.assertEqual(agents.drop_reason(OPENCODE, table, {'opencode': None}),
                         agents.REASON_DISABLED)

    def test_enabled_runner_is_kept(self):
        table = {'runners': {'opencode': {'enabled': True}}}
        self.assertIsNone(agents.drop_reason(OPENCODE, table, {'opencode': 'x'}))

    def test_missing_cli_is_dropped(self):
        for table in (None, {'runners': {}}, {'runners': {'codex': {'enabled': True}}}):
            with self.subTest(table=table):
                self.assertEqual(agents.drop_reason(OPENCODE, table, {'opencode': None}),
                                 agents.REASON_NOT_FOUND)
        self.assertEqual(agents.drop_reason(OPENCODE, None, {}), agents.REASON_NOT_FOUND)

    def test_model_outside_models_is_dropped(self):
        table = {'runners': {'opencode': {'models': ['opencode-go/other']}}}
        self.assertEqual(agents.drop_reason(OPENCODE, table, {'opencode': 'x'}),
                         agents.REASON_MODEL_NOT_SELECTED)

    def test_model_in_models_is_kept(self):
        table = {'runners': {'opencode': {'models': [OPENCODE['model']]}}}
        self.assertIsNone(agents.drop_reason(OPENCODE, table, {'opencode': 'x'}))

    def test_models_key_absent_accepts_any_model(self):
        table = {'runners': {'opencode': {'enabled': True}}}
        self.assertIsNone(agents.drop_reason({**OPENCODE, 'model': 'any'}, table, {'opencode': 'x'}))

    def test_reason_strings(self):
        self.assertEqual(agents.REASON_DISABLED, '未使用 (agents.json)')
        self.assertEqual(agents.REASON_NOT_FOUND, '見つからない')
        self.assertEqual(agents.REASON_MODEL_NOT_SELECTED, '未選択のモデル')


class FilterStepsTest(Base):
    def test_keeps_the_found_and_drops_the_missing(self):
        found = {'opencode': 'x', 'codex': None, 'agent': None}
        kept, dropped = agents.filter_steps([OPENCODE, CODEX, AGENT_STEP], None, found)
        self.assertEqual(kept, [OPENCODE, AGENT_STEP])
        self.assertEqual(dropped, [{**CODEX, 'reason': agents.REASON_NOT_FOUND}])

    def test_does_not_change_the_input(self):
        step = dict(OPENCODE)
        agents.filter_steps([step], {'runners': {'opencode': {'enabled': False}}}, {'opencode': 'x'})
        self.assertEqual(step, OPENCODE)
        self.assertNotIn('reason', step)

    def test_agent_is_kept_under_every_condition(self):
        table = {'runners': {'agent': {'enabled': False, 'models': []}}}
        kept, dropped = agents.filter_steps([AGENT_STEP], table, {})
        self.assertEqual(kept, [AGENT_STEP])
        self.assertEqual(dropped, [])

    def test_disabled_and_missing_are_both_dropped(self):
        table = {'runners': {'opencode': {'enabled': False}}}
        kept, dropped = agents.filter_steps([OPENCODE, CODEX], table, {'opencode': 'x'})
        self.assertEqual(kept, [])
        self.assertEqual([d['reason'] for d in dropped],
                         [agents.REASON_DISABLED, agents.REASON_NOT_FOUND])

    def test_no_steps(self):
        self.assertEqual(agents.filter_steps([], None, {}), ([], []))

    def test_found_by_find_cli(self):
        make_cli(self.empty, 'opencode')
        steps = [OPENCODE, {'runner': 'dsh', 'model': 'deepseek', 'effort': None}, AGENT_STEP]
        found = self.found(steps, None, self.env())
        kept, dropped = agents.filter_steps(steps, None, found)
        self.assertEqual(kept, [OPENCODE, AGENT_STEP])
        self.assertEqual([d['runner'] for d in dropped], ['dsh'])
        self.assertEqual(dropped[0]['reason'], agents.REASON_NOT_FOUND)


if __name__ == '__main__':
    unittest.main()
