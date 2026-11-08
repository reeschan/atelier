import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'verify.py')


def pbi(*cmds):
    lines = '\n'.join(f'{i}. `{c}`' for i, c in enumerate(cmds, start=1))
    return f'# PBI\n\n## Verify commands\n{lines}\n\n## Refs\nnone\n'


class VerifyDeferredTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.base = self.root / 'docs' / 'pbi'
        self.base.mkdir(parents=True)
        # 流れたコマンドを数えるため、流れるたびに 1 行足す
        self.count = 'echo x >> ran.txt'
        (self.base / 'PBI-001.md').write_text(pbi('echo fast', f'{self.count} && echo heavy'), encoding='utf-8')
        (self.base / 'PBI-002.md').write_text(pbi(f'{self.count} && echo heavy', 'exit 3'), encoding='utf-8')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def defer(self, *patterns):
        (self.base / 'tests-at-end.json').write_text(json.dumps({'deferred': list(patterns)}), encoding='utf-8')

    def run_script(self, *args):
        return subprocess.run([sys.executable, SCRIPT, *args], cwd=self.root,
                              capture_output=True, text=True, encoding='utf-8')

    def results(self, name):
        data = json.loads((self.base / 'verify' / f'{name}.json').read_text(encoding='utf-8'))
        return {r['n']: r['code'] for r in data['results']}

    def ran(self):
        p = self.root / 'ran.txt'
        if not p.is_file():
            return 0
        return len(p.read_text(encoding='utf-8').splitlines())

    def test_list_deferred_prints_matching_numbers(self):
        self.defer('echo heavy')
        r = self.run_script('PBI-001', '--list-deferred')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), '2')

    def test_list_deferred_is_empty_without_settings(self):
        r = self.run_script('PBI-001', '--list-deferred')
        self.assertEqual(r.stdout.strip(), '')

    def test_turned_off_settings_defer_nothing(self):
        (self.base / 'tests-at-end.json').write_text(
            json.dumps({'enabled': False, 'deferred': ['echo heavy']}), encoding='utf-8')
        self.assertEqual(self.run_script('PBI-001', '--list-deferred').stdout.strip(), '')
        r = self.run_script('PBI-001', '--skip-deferred')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ran(), 1)

    def test_skip_deferred_runs_only_the_rest(self):
        self.defer('echo heavy')
        r = self.run_script('PBI-001', '--skip-deferred')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ran(), 0)
        self.assertEqual(self.results('PBI-001'), {1: 0})

    def test_skip_deferred_without_settings_runs_everything(self):
        self.run_script('PBI-001', '--skip-deferred')
        self.assertEqual(self.ran(), 1)
        self.assertEqual(self.results('PBI-001'), {1: 0, 2: 0})

    def test_deferred_runs_a_shared_command_once_and_records_it_for_each_report(self):
        self.defer('echo heavy')
        self.run_script('PBI-001', '--skip-deferred')
        r = self.run_script('PBI-001,PBI-002.rework-1', '--deferred')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ran(), 1)
        # 先に流した番号の結果は残り、あとに回した番号が重なる
        self.assertEqual(self.results('PBI-001'), {1: 0, 2: 0})
        self.assertEqual(self.results('PBI-002.rework-1'), {1: 0})

    def test_deferred_records_a_failure_for_the_reports_that_use_it(self):
        self.defer('exit 3')
        self.run_script('PBI-002', '--deferred')
        self.assertEqual(self.results('PBI-002'), {2: 3})

    def test_deferred_without_matching_commands_exits_2(self):
        self.defer('no such command')
        r = self.run_script('PBI-001', '--deferred')
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.ran(), 0)


if __name__ == '__main__':
    unittest.main()
