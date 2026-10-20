"""common.py (スクリプト共通の小さな関数と定数) の単体テスト。"""
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_stats
import common

HOME_KEYS = ('PBI_HOME', 'PBI_TEMPLATES_HOME', 'XDG_CONFIG_HOME')


def clean_env(**extra):
    """home の解決に効く環境変数を消してから extra を足した環境。"""
    env = {k: v for k, v in os.environ.items() if k not in HOME_KEYS}
    env.update(extra)
    return env


def env_of(**extra):
    return mock.patch.dict(os.environ, clean_env(**extra), clear=True)


class ResolveHomeTest(unittest.TestCase):
    """引数 → PBI_HOME → PBI_TEMPLATES_HOME → XDG_CONFIG_HOME → ~/.config。"""

    def test_arg_wins_over_env(self):
        with env_of(PBI_HOME='pbi-home', PBI_TEMPLATES_HOME='old-home', XDG_CONFIG_HOME='xdg'):
            self.assertEqual(common.resolve_home('arg-home'), Path('arg-home'))

    def test_pbi_home_is_used(self):
        with env_of(PBI_HOME='pbi-home'):
            self.assertEqual(common.resolve_home(None), Path('pbi-home'))

    def test_old_name_is_still_read(self):
        with env_of(PBI_TEMPLATES_HOME='old-home'):
            self.assertEqual(common.resolve_home(None), Path('old-home'))

    def test_pbi_home_wins_over_old_name(self):
        with env_of(PBI_HOME='pbi-home', PBI_TEMPLATES_HOME='old-home'):
            self.assertEqual(common.resolve_home(None), Path('pbi-home'))

    def test_empty_value_falls_through(self):
        with env_of(PBI_HOME='', PBI_TEMPLATES_HOME='old-home'):
            self.assertEqual(common.resolve_home(None), Path('old-home'))

    def test_xdg_config_home(self):
        with env_of(XDG_CONFIG_HOME='xdg'):
            self.assertEqual(common.resolve_home(None), Path('xdg') / 'pbi-orchestration-jev')

    def test_pbi_home_wins_over_xdg(self):
        with env_of(PBI_HOME='pbi-home', XDG_CONFIG_HOME='xdg'):
            self.assertEqual(common.resolve_home(None), Path('pbi-home'))

    def test_default_is_config_under_home(self):
        with env_of(), tempfile.TemporaryDirectory() as d, mock.patch.object(Path, 'home', return_value=Path(d)):
            self.assertEqual(common.resolve_home(None), Path(d) / '.config' / 'pbi-orchestration-jev')

    def test_does_not_create_the_directory(self):
        with env_of(), tempfile.TemporaryDirectory() as d, mock.patch.object(Path, 'home', return_value=Path(d)):
            resolved = common.resolve_home(None)
            self.assertFalse(resolved.exists())


class ConstantsTest(unittest.TestCase):
    def test_tiers(self):
        self.assertEqual(common.TIERS, ('haiku', 'sonnet', 'opus'))

    def test_runners(self):
        self.assertEqual(common.EXTERNAL_RUNNERS, ('opencode', 'codex', 'agy', 'dsh'))
        self.assertEqual(common.AGENT, 'agent')
        self.assertEqual(common.RUNNERS, ('opencode', 'codex', 'agy', 'dsh', 'agent'))


class StepLabelTest(unittest.TestCase):
    def test_with_effort(self):
        step = {'runner': 'opencode', 'model': 'opencode-go/deepseek-v4.1-flash', 'effort': 'max'}
        self.assertEqual(common.step_label(step), 'opencode opencode-go/deepseek-v4.1-flash max')

    def test_without_effort(self):
        step = {'runner': 'agent', 'model': 'opus', 'effort': None}
        self.assertEqual(common.step_label(step), 'agent opus')

    def test_model_slash_is_kept(self):
        step = {'runner': 'codex', 'model': 'opencode-go/deepseek-v4.1-flash', 'effort': 'high'}
        self.assertIn('opencode-go/deepseek-v4.1-flash', common.step_label(step))
        self.assertNotIn('opencode-go deepseek', common.step_label(step))


class LoadJsonTest(unittest.TestCase):
    def test_reads_json(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'x.json'
            path.write_text(json.dumps({'a': 1}), encoding='utf-8')
            self.assertEqual(common.load_json(path), {'a': 1})

    def test_missing_file_returns_default(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'nope.json'
            self.assertIsNone(common.load_json(path))
            self.assertEqual(common.load_json(path, {}), {})

    def test_broken_json_returns_default(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'broken.json'
            path.write_text('{oops', encoding='utf-8')
            self.assertIsNone(common.load_json(path))
            self.assertEqual(common.load_json(path, []), [])


class PathTest(unittest.TestCase):
    """long_path・shown・project_dir_for は agent_stats.py と同じ結果にする。"""

    def test_long_path_and_shown_match_agent_stats(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'sub' / 'x.jsonl'
            path.parent.mkdir()
            path.write_text('x', encoding='utf-8')
            self.assertEqual(common.long_path(path), agent_stats.long_path(path))
            self.assertEqual(common.shown(common.long_path(path)),
                             agent_stats.shown(agent_stats.long_path(path)))

    def test_shown_removes_the_prefix(self):
        self.assertEqual(common.shown('\\\\?\\C:\\work\\x'), 'C:\\work\\x')
        self.assertEqual(common.shown('C:\\work\\x'), 'C:\\work\\x')

    def test_project_dir_for_matches_agent_stats(self):
        for cwd in ('C:/work/my repo', Path('C:/work/my repo'), '/work/my repo'):
            self.assertEqual(common.project_dir_for(cwd), agent_stats.project_dir_for(cwd))

    def test_project_dir_name_uses_dashes(self):
        result = common.project_dir_for('C:/work/my repo')
        self.assertEqual(result.name, 'C--work-my-repo')
        self.assertEqual(result.parent.name, 'projects')


class Utf8StdoutTest(unittest.TestCase):
    def test_reconfigures_both_streams(self):
        stream = mock.Mock()
        with mock.patch.object(sys, 'stdout', stream), mock.patch.object(sys, 'stderr', stream):
            common.utf8_stdout()
        self.assertEqual(stream.reconfigure.call_count, 2)
        stream.reconfigure.assert_called_with(encoding='utf-8')

    def test_streams_without_reconfigure_are_left_alone(self):
        with mock.patch.object(sys, 'stdout', io.StringIO()), mock.patch.object(sys, 'stderr', io.StringIO()):
            common.utf8_stdout()


class NowTest(unittest.TestCase):
    def test_now_has_a_timezone(self):
        self.assertIsNotNone(common.now().tzinfo)


if __name__ == '__main__':
    unittest.main()
