"""examples/question-send-approval.md と examples/question-grill.md が、design_record.py lint --kind をいつも通ることを確かめる。

誤りが無い (終了コード 0、標準エラーに ✗ が無い) ことと、内部の言葉の警告 (! L<行>) が無いことを見る。
環境は切り離す: ATELIER_HOME と PBI_PLUGINS_DIR は空の一時フォルダ、HARNESS_LOG_CONFIG は無いファイルを指す。
本物の home やプラグインの yomiyasu は呼ばない。
"""
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'design_record.py')
EXAMPLES = os.path.join(HERE, 'examples')


class QuestionExamplesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = os.path.join(tmp.name, 'home')
        plugins = os.path.join(tmp.name, 'plugins')
        os.makedirs(home)
        os.makedirs(plugins)
        self.env = {
            **os.environ,
            'ATELIER_HOME': home,
            'PBI_PLUGINS_DIR': plugins,
            'HARNESS_LOG_CONFIG': os.path.join(tmp.name, 'no-harness-log.json'),
            'ATELIER_EVENTS_SETTLE': 'off',
            'ATELIER_EVENTS_QUIET': '1',
            'PYTHONIOENCODING': 'utf-8',
        }

    def lint(self, name, kind):
        result = subprocess.run([sys.executable, SCRIPT, 'lint', '--draft', os.path.join(EXAMPLES, name),
                                 '--kind', kind], capture_output=True, env=self.env)
        return result.returncode, result.stderr.decode('utf-8', errors='replace')

    def assert_clean(self, name, kind):
        code, stderr = self.lint(name, kind)
        self.assertEqual(code, 0, stderr)
        self.assertNotIn('✗', stderr)
        self.assertEqual([row for row in stderr.splitlines() if row.startswith('! L')], [], stderr)

    def test_send_approval_example_is_clean(self):
        self.assert_clean('question-send-approval.md', 'send-approval')

    def test_grill_example_is_clean(self):
        self.assert_clean('question-grill.md', 'decision')


if __name__ == '__main__':
    unittest.main()
