"""tools_check.py を CLI から、実際の PC の道具を見ない環境で動かし、有無・キーの無さ・入れ方と代わりの列を確かめる。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'tools_check.py')
FAKE = os.path.join(HERE, 'testdata', 'fake_jevlint.py')
TOOLS = ['omm', 'omm-scan', 'jevlint', 'yomiyasu', 'sac-strategic-compact',
         'principle-test-behavior-not-implementation', 'answer-me-with-any']


class ToolsCheckTest(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.join(tempfile.gettempdir(), f'tools-check-{uuid.uuid4().hex}')
        for name in ('home', 'plugins', 'skills', 'empty'):
            os.makedirs(os.path.join(self.dir, name))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        empty = os.path.join(self.dir, 'empty')
        self.env = dict(os.environ, PATH=empty, FNM_DIR=empty, NVM_DIR=empty, HOME=empty, APPDATA=empty,
                        LOCALAPPDATA=empty, USERPROFILE=empty, PBI_PLUGINS_DIR=os.path.join(self.dir, 'plugins'))

    def rows(self, **env):
        result = subprocess.run([sys.executable, SCRIPT, '--home', os.path.join(self.dir, 'home'),
                                 '--skills-dir', os.path.join(self.dir, 'skills')],
                                capture_output=True, text=True, encoding='utf-8', env=dict(self.env, **env))
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], 'tool\tstatus\tinstall\tfallback')
        return {line.split('\t')[0]: line.split('\t') for line in lines[1:]}

    def touch(self, *parts):
        path = os.path.join(self.dir, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, 'w').close()

    def test_everything_missing_still_lists_install_and_fallback(self):
        rows = self.rows()
        self.assertEqual(list(rows), TOOLS)
        for tool, row in rows.items():
            with self.subTest(tool=tool):
                self.assertEqual(row[1], 'missing')
                self.assertTrue(row[2] and row[3])
        self.assertIn('https://github.com/reeschan/sac-strategic-compact', rows['sac-strategic-compact'][2])
        self.assertIn('prompts/lint-fallback.md', rows['jevlint'][3])
        install = rows['answer-me-with-any'][2]
        for part in ('https://github.com/reeschan/answer-me-with-any', 'skills/answer-me-with-any',
                     '~/.claude/skills/answer-me-with-any', 'only after the user says yes'):
            self.assertIn(part, install)
        self.assertIn('the same Markdown in chat', rows['answer-me-with-any'][3])
        self.assertEqual(os.listdir(os.path.join(self.dir, 'skills')), [])

    def test_found_tools_and_a_missing_key(self):
        with open(os.path.join(self.dir, 'home', 'agents.json'), 'w', encoding='utf-8') as f:
            json.dump({'tools': {'jevlint': {'path': FAKE}}}, f)
        self.touch('plugins', 'oh-my-mermaid', '0.2.0', 'skills', 'omm-scan', 'SKILL.md')
        self.touch('plugins', 'pstack', '0.15.32', 'skills', 'principle-test-behavior-not-implementation', 'SKILL.md')
        self.touch('skills', 'sac-strategic-compact', 'SKILL.md')
        self.touch('skills', 'answer-me-with-any', 'SKILL.md')
        rows = self.rows()
        self.assertTrue(rows['jevlint'][1].startswith('ok ') and rows['jevlint'][1].endswith('0.0.0-fake'))
        for tool in ('omm-scan', 'sac-strategic-compact', 'principle-test-behavior-not-implementation',
                     'answer-me-with-any'):
            self.assertTrue(rows[tool][1].startswith('ok '), rows[tool])
        self.assertEqual(rows['omm'][1], 'missing')
        self.assertTrue(self.rows(FAKE_JEVLINT_KEYS='')['jevlint'][1].startswith('no-key '))


    def test_confirm_old_name_is_ok_with_a_rename_note(self):
        """古い名前のスキルしか無いときも ok にし、新しい名前に変えるよう添える。"""
        self.touch('skills', 'answer-me-with-html', 'SKILL.md')
        status = self.rows()['answer-me-with-any'][1]
        self.assertTrue(status.startswith('ok '), status)
        self.assertIn('old name answer-me-with-html; rename it to answer-me-with-any', status)

    def test_yomiyasu_missing_gives_the_install_and_the_skip(self):
        """yomiyasu が無いときは missing。入れ方は component.py と同じプラグインの入れ方、代わりは lint の点検を飛ばすこと。"""
        row = self.rows()['yomiyasu']
        self.assertEqual(row[1], 'missing')
        self.assertIn('claude plugin marketplace add nanaism/yomiyasu', row[2])
        self.assertIn('claude plugin install yomiyasu@yomiyasu --scope user', row[2])
        self.assertIn('lint の yomiyasu の点検を飛ばす', row[3])

    def test_yomiyasu_from_the_agents_json_path_before_the_cache(self):
        """home の agents.json の tools.yomiyasu.path が有るファイルなら、キャッシュの版より先にそれを ok にする。"""
        self.touch('home', 'yomiyasu_lint.py')
        self.touch('plugins', 'cache', 'yomiyasu', 'yomiyasu', '2.10.0', 'scripts', 'yomiyasu_lint.py')
        configured = os.path.join(self.dir, 'home', 'yomiyasu_lint.py')
        with open(os.path.join(self.dir, 'home', 'agents.json'), 'w', encoding='utf-8') as f:
            json.dump({'tools': {'yomiyasu': {'path': configured}}}, f)
        self.assertEqual(self.rows()['yomiyasu'][1], f'ok {configured}')

    def test_yomiyasu_from_the_plugin_cache_takes_the_newest_version(self):
        """プラグインのキャッシュに版が 2 つあるとき、数で比べていちばん新しい版 (2.10.0 > 2.9.0) を ok にする。"""
        self.touch('plugins', 'cache', 'yomiyasu', 'yomiyasu', '2.9.0', 'scripts', 'yomiyasu_lint.py')
        self.touch('plugins', 'cache', 'yomiyasu', 'yomiyasu', '2.10.0', 'scripts', 'yomiyasu_lint.py')
        newest = os.path.join(self.dir, 'plugins', 'cache', 'yomiyasu', 'yomiyasu', '2.10.0', 'scripts',
                              'yomiyasu_lint.py')
        self.assertEqual(self.rows()['yomiyasu'][1], f'ok {newest}')


if __name__ == '__main__':
    unittest.main()
