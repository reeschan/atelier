"""runner_skip.py (今の run で使わない実行先・モデルの記録 runners.skip.json) の振る舞い。

load は今の run_id に合う項目だけを集合で返し、壊れていても空にすること、
add / show は CLI として一時フォルダで動かし、出る行・書いたファイル・終了コードを見ること。
home・プラグイン・ハーネスのログは一時フォルダに切り離す。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'runner_skip.py')
sys.path.insert(0, HERE)

import runner_skip  # noqa: E402

MODEL_ENTRY = {'runner': None, 'model': 'gpt-x', 'cause': 'limit', 'fallback': 'sonnet', 'at': '2026-01-01T00:00:00'}


class RunnerSkipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.base = Path(self.tmp) / 'docs' / 'pbi'
        self.base.mkdir(parents=True)
        self.env = {**os.environ,
                    'ATELIER_HOME': os.path.join(self.tmp, 'home'),
                    'PBI_PLUGINS_DIR': os.path.join(self.tmp, 'plugins'),
                    'HARNESS_LOG_CONFIG': os.path.join(self.tmp, 'no-harness-log.json'),
                    'ATELIER_EVENTS_SETTLE': 'off', 'ATELIER_EVENTS_QUIET': '1',
                    'PYTHONIOENCODING': 'utf-8'}

    @property
    def skip_file(self) -> Path:
        return self.base / 'runners.skip.json'

    def write_run(self, run_id='r1'):
        folder = self.base / 'conductor'
        folder.mkdir(exist_ok=True)
        (folder / 'run.json').write_text(json.dumps({'run_id': run_id}), encoding='utf-8')

    def write_skip(self, run_id='r1', entries=None):
        entries = [MODEL_ENTRY] if entries is None else entries
        self.skip_file.write_text(json.dumps({'schema': 'atelier.runners-skip/1', 'run_id': run_id,
                                              'entries': entries}), encoding='utf-8')

    def cli(self, *argv):
        result = subprocess.run([sys.executable, SCRIPT, *argv], capture_output=True, env=self.env,
                                cwd=self.tmp)
        return (result.returncode, result.stdout.decode('utf-8', errors='replace'),
                result.stderr.decode('utf-8', errors='replace'))

    def saved(self):
        return json.loads(self.skip_file.read_text(encoding='utf-8'))

    def test_C1(self):
        self.write_run('r1')
        self.write_skip('r1')
        self.assertEqual(runner_skip.load(self.base), frozenset({'gpt-x'}))

    def test_C2(self):
        self.write_run('r2')
        self.write_skip('r1')
        self.assertEqual(runner_skip.load(self.base), frozenset())

    def test_C3(self):
        self.write_skip('r1')
        self.assertEqual(runner_skip.load(self.base), frozenset(), 'run.json が無い')
        self.write_run('r1')
        for text in ('[1, 2]', '{broken'):
            with self.subTest(text=text):
                self.skip_file.write_text(text, encoding='utf-8')
                self.assertEqual(runner_skip.load(self.base), frozenset())

    def test_C4(self):
        self.write_run('r1')
        for _ in range(2):
            rc, out, err = self.cli('add', str(self.base), '--model', 'gpt-x', '--cause', 'limit',
                                    '--fallback', 'sonnet')
            self.assertEqual(rc, 0, err)
            self.assertEqual(out.strip(), 'skip model gpt-x cause limit fallback sonnet')
        data = self.saved()
        self.assertEqual(data['run_id'], 'r1')
        self.assertEqual(len(data['entries']), 1)

    def test_C5(self):
        rc, out, err = self.cli('add', str(self.base), '--model', 'gpt-x', '--cause', 'limit',
                                '--fallback', 'sonnet')
        self.assertEqual(rc, 2, out + err)
        self.assertFalse(self.skip_file.exists())

    def test_C6(self):
        self.write_run('r1')
        rc, out, err = self.cli('add', str(self.base), '--runner', 'agent', '--cause', 'broken',
                                '--fallback', 'claude')
        self.assertEqual(rc, 2, out + err)
        self.assertFalse(self.skip_file.exists())

    def test_C7(self):
        self.write_run('r1')
        rc, out, err = self.cli('add', str(self.base), '--runner', 'codex', '--cause', 'broken',
                                '--fallback', 'claude')
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.strip(), 'skip runner codex cause broken fallback claude')
        self.assertEqual(runner_skip.load(self.base), frozenset({'codex'}))

    def test_C8(self):
        self.write_run('r1')
        self.write_skip('r1')
        rc, out, err = self.cli('show', str(self.base))
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.strip().splitlines(), ['skip model gpt-x cause limit fallback sonnet'])

    def test_C9(self):
        self.write_run('r2')
        self.write_skip('r1')
        rc, out, err = self.cli('show', str(self.base))
        self.assertEqual((rc, out.strip()), (0, 'runners.skip: none'), err)
        self.skip_file.unlink()
        rc, out, err = self.cli('show', str(self.base))
        self.assertEqual((rc, out.strip()), (0, 'runners.skip: none'), err)


if __name__ == '__main__':
    unittest.main()
