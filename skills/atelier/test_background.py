"""background.py (切り離して起こし、あとで待つ) を CLI で確かめる。子は Python の短いスクリプト。"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
BG = HERE / 'background.py'


class BackgroundTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='atelier-bg-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def bg(self, *args):
        p = subprocess.run([sys.executable, str(BG), *args], cwd=self.tmp, capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=60, check=False)
        return p.returncode, p.stdout + p.stderr

    def test_start_returns_at_once_and_wait_reports_done_with_the_exit_code(self):
        code = 'import time,sys; time.sleep(2); print("hello"); sys.exit(5)'
        began = time.monotonic()
        rc, out = self.bg('start', '--name', 'job-1', '--', sys.executable, '-c', code)
        self.assertEqual(rc, 0, out)
        self.assertLess(time.monotonic() - began, 10)
        self.assertIn('state: started', out)
        rc, out = self.bg('wait', '--name', 'job-1', '--timeout', '30')
        self.assertEqual((rc, out.strip()), (0, 'state: done exit 5'))
        self.assertIn('hello', (self.tmp / 'docs/pbi/bg/job-1.log').read_text(encoding='utf-8'))

    def test_wait_times_out_while_running_and_start_refuses_the_same_name(self):
        rc, out = self.bg('start', '--name', 'slow', '--', sys.executable, '-c', 'import time; time.sleep(8)')
        self.assertEqual(rc, 0, out)
        rc, out = self.bg('wait', '--name', 'slow', '--timeout', '1')
        self.assertEqual((rc, out.strip()), (3, 'state: running'))
        rc, out = self.bg('start', '--name', 'slow', '--', sys.executable, '-c', 'pass')
        self.assertEqual(rc, 2, out)
        rc, out = self.bg('status')
        self.assertIn('slow  running', out)
        rc, out = self.bg('wait', '--name', 'slow', '--timeout', '30')
        self.assertEqual((rc, out.strip()), (0, 'state: done exit 0'))

    def test_bad_input(self):
        self.assertEqual(self.bg('start', '--name', 'x')[0], 2)  # コマンドが無い
        self.assertEqual(self.bg('start', '--name', '../x', '--', sys.executable, '-c', 'pass')[0], 2)
        self.assertEqual(self.bg('wait', '--name', 'missing', '--timeout', '0')[0], 2)

    def test_missing_command_records_127(self):
        rc, out = self.bg('start', '--name', 'nope', '--', os.path.join(str(self.tmp), 'no-such.exe'))
        self.assertEqual(rc, 0, out)
        rc, out = self.bg('wait', '--name', 'nope', '--timeout', '30')
        self.assertEqual((rc, out.strip()), (0, 'state: done exit 127'))


if __name__ == '__main__':
    unittest.main()
