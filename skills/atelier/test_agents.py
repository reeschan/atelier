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
ADAPTERS_DEFAULT = {'design': 'auto', 'review': 'auto', 'security': 'auto', 'confirm': 'html'}


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


class LoadAdaptersTest(Base):
    def write(self, data):
        text = data if isinstance(data, str) else json.dumps(data)
        (self.root / 'agents.json').write_text(text, encoding='utf-8')

    def test_missing_file_is_all_auto(self):
        self.assertEqual(agents.load_adapters(self.root), (ADAPTERS_DEFAULT, []))

    def test_broken_json_and_top_level_not_an_object_are_all_auto(self):
        for text in ('{oops', '[]', '1', 'null'):
            with self.subTest(text=text):
                self.write(text)
                self.assertEqual(agents.load_adapters(self.root), (ADAPTERS_DEFAULT, []))

    def test_adapters_absent_is_all_auto(self):
        for data in ({}, {'runners': {'opencode': {'enabled': True}}}, {'runners': []}):
            with self.subTest(data=data):
                self.write(data)
                self.assertEqual(agents.load_adapters(self.root), (ADAPTERS_DEFAULT, []))

    def test_adapters_is_read_without_runners(self):
        self.write({'adapters': {'design': 'off'}})
        expected = {**ADAPTERS_DEFAULT, 'design': 'off'}
        self.assertEqual(agents.load_adapters(self.root), (expected, []))

    def test_broken_runners_does_not_matter(self):
        self.write({'runners': [], 'adapters': {'review': 'off'}})
        expected = {**ADAPTERS_DEFAULT, 'review': 'off'}
        self.assertEqual(agents.load_adapters(self.root), (expected, []))

    def test_all_four_keys_accept_auto_off_and_skill_names_except_confirm(self):
        self.write({'adapters': {'design': 'auto', 'review': 'off',
                                 'security': 'mattpocock-skills:code-review', 'confirm': 'answer-me-with-html'}})
        expected = {'design': 'auto', 'review': 'off', 'security': 'mattpocock-skills:code-review',
                    'confirm': 'html'}
        self.assertEqual(agents.load_adapters(self.root),
                         (expected, ['adapters.confirm: answer-me-with-html is read as html']))

    def test_skill_name_characters_and_length_boundaries(self):
        valid = 'a' + 'A0:_./-' * 18 + 'a' * 1  # 128 文字
        self.assertEqual(len(valid), 128)
        self.write({'adapters': {'design': valid, 'review': 'a' * 128, 'security': 'a' * 129}})
        expected = {**ADAPTERS_DEFAULT, 'design': valid, 'review': 'a' * 128}
        self.assertEqual(agents.load_adapters(self.root),
                         (expected, ['adapters.security: invalid value, using auto']))

    def test_adapters_not_an_object_warns_once_and_uses_auto(self):
        for adapters in ([], 'off', 1, None, True):
            with self.subTest(adapters=adapters):
                self.write({'adapters': adapters})
                self.assertEqual(agents.load_adapters(self.root),
                                 (ADAPTERS_DEFAULT, ['adapters: not an object, using auto for every phase']))

    def test_unknown_key_is_ignored_with_a_warning(self):
        self.write({'adapters': {'other': 'off', 'review': 'off'}})
        expected = {**ADAPTERS_DEFAULT, 'review': 'off'}
        self.assertEqual(agents.load_adapters(self.root),
                         (expected, ['adapters.other: unknown phase, ignored']))

    def test_values_that_are_not_strings_use_auto_with_a_warning(self):
        for value in (True, 1, None, [], {}):
            with self.subTest(value=value):
                self.write({'adapters': {'review': value}})
                self.assertEqual(agents.load_adapters(self.root),
                                 (ADAPTERS_DEFAULT, ['adapters.review: invalid value, using auto']))

    def test_values_not_matching_the_skill_name_form_use_auto_with_a_warning(self):
        for value in ('', ' has space', '-lead', 'a' * 129, 'auto\n', 'off '):
            with self.subTest(value=value):
                self.write({'adapters': {'security': value}})
                self.assertEqual(agents.load_adapters(self.root),
                                 (ADAPTERS_DEFAULT, ['adapters.security: invalid value, using auto']))

    def test_warnings_follow_the_order_in_the_file(self):
        self.write({'adapters': {'review': 1, 'zzz': 'off', 'design': []}})
        self.assertEqual(agents.load_adapters(self.root)[1],
                         ['adapters.review: invalid value, using auto',
                          'adapters.zzz: unknown phase, ignored',
                          'adapters.design: invalid value, using auto'])

    def test_confirm_accepts_html_md_and_off(self):
        for value in ('html', 'md', 'off'):
            with self.subTest(value=value):
                self.write({'adapters': {'confirm': value}})
                self.assertEqual(agents.load_adapters(self.root), ({**ADAPTERS_DEFAULT, 'confirm': value}, []))

    def test_confirm_auto_and_skill_names_are_read_as_html_with_a_warning(self):
        for value in ('auto', 'answer-me-with-any', 'am:answer-me-with-html', 'my-confirm'):
            with self.subTest(value=value):
                self.write({'adapters': {'confirm': value}})
                self.assertEqual(agents.load_adapters(self.root),
                                 (ADAPTERS_DEFAULT, [f'adapters.confirm: {value} is read as html']))

    def test_confirm_invalid_value_is_html_and_other_phases_stay_auto(self):
        for value in (True, 1, None, [], '', ' has space', 'a' * 129):
            with self.subTest(value=value):
                self.write({'adapters': {'confirm': value, 'review': 1}})
                self.assertEqual(agents.load_adapters(self.root),
                                 (ADAPTERS_DEFAULT, ['adapters.confirm: invalid value, using html',
                                                     'adapters.review: invalid value, using auto']))


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

    def test_fnm_finds_a_version_outside_the_one_on_path(self):
        # PATH の Node (v22・v26.8.1) には無く、fnm の v26.12.1 にだけ入っている
        fnm = self.root / 'fnm' / 'node-versions'
        for version in ('v22.0.0', 'v26.8.1'):
            (fnm / version / 'installation').mkdir(parents=True)
        cli = make_cli(fnm / 'v26.12.1' / 'installation', 'jevlint')
        path = os.pathsep.join(str(fnm / v / 'installation') for v in ('v22.0.0', 'v26.8.1'))
        env = {'PATH': path, 'FNM_DIR': str(self.root / 'fnm')}
        self.assertFound(agents.find_cli('jevlint', None, env), cli)

    def test_fnm_default_locations_are_searched_even_with_fnm_dir(self):
        home = self.root / 'home'
        cli = make_cli(home / '.local' / 'share' / 'fnm' / 'node-versions' / 'v26.12.1' / 'installation', 'jevlint')
        env = self.env(FNM_DIR=str(self.root / 'other-fnm'), HOME=str(home))
        self.assertFound(agents.find_cli('jevlint', None, env), cli)

    def test_fnm_xdg_data_home_and_legacy_dir(self):
        for parts in (('xdg', 'fnm'), ('home', '.fnm')):
            with self.subTest(parts=parts):
                base = self.root.joinpath(*parts)
                cli = make_cli(base / 'node-versions' / 'v26.12.1' / 'installation', 'jevlint')
                env = self.env(XDG_DATA_HOME=str(self.root / 'xdg'), HOME=str(self.root / 'home'))
                self.assertFound(agents.find_cli('jevlint', None, env), cli)
                shutil.rmtree(base)

    def test_fnm_takes_the_newest_version_across_locations(self):
        make_cli(self.root / 'a' / 'node-versions' / 'v22.0.0' / 'installation', 'jevlint')
        new = make_cli(self.root / 'xdg' / 'fnm' / 'node-versions' / 'v26.12.1' / 'installation', 'jevlint')
        env = self.env(FNM_DIR=str(self.root / 'a'), XDG_DATA_HOME=str(self.root / 'xdg'))
        self.assertFound(agents.find_cli('jevlint', None, env), new)

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

    def test_claude_is_found_on_path(self):
        cli = make_cli(self.empty, 'claude')
        self.assertFound(agents.find_cli('claude', None, self.env()), cli)

    def test_claude_is_found_in_node_global(self):
        cli = make_file(self.root / 'fnm' / 'node-versions' / 'v22.0.0' / 'installation' / 'bin', 'claude')
        self.assertFound(agents.find_cli('claude', None, self.env(FNM_DIR=str(self.root / 'fnm'))), cli)

    def test_claude_default_location_on_windows(self):
        exe = make_file(self.root / 'profile' / '.local' / 'bin', 'claude.exe')
        env = self.env(USERPROFILE=str(self.root / 'profile'))
        self.assertFound(agents.default_location('claude', env, 'nt'), exe)
        self.assertIsNone(agents.default_location('claude', env, 'posix'))

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

    def test_claude_step_is_kept_only_when_enabled_and_found(self):
        step = {'runner': 'claude', 'model': 'sonnet', 'effort': 'high'}
        table = {'runners': {'claude': {'enabled': True}}}
        self.assertEqual(agents.filter_steps([step, AGENT_STEP], table, {'claude': 'x'}), ([step, AGENT_STEP], []))
        off = {'runners': {'claude': {'enabled': False}}}
        kept, dropped = agents.filter_steps([step, AGENT_STEP], off, {'claude': 'x'})
        self.assertEqual((kept, [d['reason'] for d in dropped]), ([AGENT_STEP], [agents.REASON_DISABLED]))
        kept, dropped = agents.filter_steps([step, AGENT_STEP], table, {'claude': None})
        self.assertEqual((kept, [d['reason'] for d in dropped]), ([AGENT_STEP], [agents.REASON_NOT_FOUND]))
        picked = {'runners': {'claude': {'models': ['opus']}}}
        kept, dropped = agents.filter_steps([step], picked, {'claude': 'x'})
        self.assertEqual((kept, [d['reason'] for d in dropped]), ([], [agents.REASON_MODEL_NOT_SELECTED]))

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


class CliEnvTest(Base):
    def test_the_cli_directory_goes_first_and_is_not_repeated(self):
        cli = make_cli(self.root / 'node' / 'v26.12.1', 'jevlint')
        env = {'PATH': os.pathsep.join(['/a', str(cli.parent), '/b']), 'OTHER': 'x'}
        result = agents.cli_env(str(cli), env)
        self.assertEqual(result['PATH'].split(os.pathsep), [str(cli.parent.absolute()), '/a', '/b'])
        self.assertEqual(result['OTHER'], 'x')
        self.assertEqual(env['PATH'].split(os.pathsep)[1], str(cli.parent))  # 元の env は変えない

    def test_keeps_the_case_of_the_path_key(self):
        cli = make_cli(self.root / 'bin', 'jevlint')
        result = agents.cli_env(str(cli), {'Path': '/a'})
        self.assertNotIn('PATH', result)
        self.assertEqual(result['Path'].split(os.pathsep)[0], str(cli.parent.absolute()))

    @unittest.skipIf(os.name == 'nt', 'symlink')
    def test_a_symlinked_cli_keeps_its_own_directory(self):
        # fnm の installation/bin/jevlint は lib の下への symlink。node があるのは bin の方
        target = make_file(self.root / 'lib', 'cli.js')
        link_dir = self.root / 'bin'
        link_dir.mkdir()
        (link_dir / 'jevlint').symlink_to(target)
        result = agents.cli_env(str(link_dir / 'jevlint'), {'PATH': ''})
        self.assertEqual(result['PATH'], str(link_dir.absolute()))

    def test_no_cli_leaves_the_env_as_is(self):
        self.assertEqual(agents.cli_env(None, {'PATH': '/a'}), {'PATH': '/a'})
