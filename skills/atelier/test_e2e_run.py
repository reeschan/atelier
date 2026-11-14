"""e2e_run.py の範囲の決め方と、書く test-result・出番の結果を、一時の git リポジトリで確かめる。

verify.py は偽物 (ATELIER_VERIFY_CMD) にし、spec ごとの終了コードを環境変数で決める。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / 'e2e_run.py'

FAKE_VERIFY = r'''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
base = Path(args[args.index('--dir') + 1])
name = args[args.index('--name') + 1]
(base / 'verify').mkdir(parents=True, exist_ok=True)
with open(os.environ['FAKE_VERIFY_LOG'], 'a', encoding='utf-8') as log:
    log.write(json.dumps(args) + '\n')
if os.environ.get('FAKE_VERIFY_HANG'):
    import subprocess, time
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    Path(os.environ['FAKE_VERIFY_HANG']).write_text(str(child.pid))
    time.sleep(60)
if '--final' in args:
    code = os.environ.get('FAKE_FULL_CODE', '0')
    code = int(code) if code.lstrip('-').isdigit() else code
    approved = (base / 'full-run.approved.json').is_file()
    log = base / 'verify' / 'full.log'
    log.write_text(os.environ.get('FAKE_FULL_LOG', ''), encoding='utf-8')
    if os.environ.get('FAKE_FULL_NO_RESULT') == '1':
        sys.exit(2)
    (base / 'verify' / f'{name}.json').write_text(json.dumps({'results': [
        {'n': 1, 'command': 'full', 'code': code if approved else 'no-approval', 'log': str(log)}]}))
    if code == 'idle':
        print('blocked: idle (1 seconds)')
        sys.exit(3)
    sys.exit(0)
specs = args[args.index('--related') + 1].split(',')
failing = set(filter(None, os.environ.get('FAKE_FAIL', '').split(',')))
rows = [{'n': n, 'spec': s, 'command': f'run {s}', 'code': 1 if s in failing else 0, 'tries': 3 if s in failing else 1,
         'log': f'e2e.{n}.log'} for n, s in enumerate(specs, start=1)]
(base / 'verify' / f'{name}.json').write_text(json.dumps({'results': rows}))
'''


def git(cwd, *args):
    subprocess.run(['git', '-c', 'user.email=t@example.com', '-c', 'user.name=t', *args], cwd=cwd, check=True,
                   capture_output=True)


class E2ERunTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='e2e-run-'))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.write('src/cart.ts', 'export function addItem(cart, item) {\n  return [...cart, item]\n}\n'
                   'export const TAX_RATE = 0.1\n')
        self.write('e2e/cart.cy.ts', "import { addItem } from '../src/cart'\nit('adds', () => addItem([], 1))\n")
        self.write('e2e/tax.cy.ts', "cy.contains('TAX_RATE_LABEL')\n")
        self.write('e2e/login.cy.ts', "it('logs in', () => {})\n")
        git(self.root, 'init', '-q', '-b', 'main')
        git(self.root, 'add', '.')
        git(self.root, 'commit', '-q', '-m', 'base')
        git(self.root, 'checkout', '-q', '-b', 'work')
        # 作業ツリーの変更 (PBI はコミットしない): addItem を変え、新しいファイルに定義を足す
        self.write('src/cart.ts', 'export function addItem(cart, item) {\n  return cart.concat(item)\n}\n'
                   'export const TAX_RATE = 0.1\n')
        self.write('src/new.ts', 'export function loginBanner() {}\n')
        self.write('docs/pbi/PBI-001.md', '# PBI-001: cart\n\n- Related specs: e2e/login.cy.ts\n\n## Targets\n'
                   '- `src/cart.ts`: addItem\n\n## Notes\n- Breaks by design: e2e/login.cy.ts — the form moved\n')
        self.write('docs/pbi/PBI-002.md', '# PBI-002: other\n\n- Related specs: none\n\n## Targets\n- src/new.ts\n')
        self.settings = {'enabled': True, 'test_paths': ['e2e/**'], 'investigate': 'npx cypress run --spec {specs}',
                         'final': 'npm run e2e', 'e2e': {'base': 'main', 'specs': 'e2e/**/*.cy.ts'}}
        self.write('fake_verify.py', FAKE_VERIFY)
        self.log = self.root / 'verify.log'

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8', newline='\n')

    def run_e2e(self, **env):
        self.write('docs/pbi/tests-at-end.json', json.dumps(self.settings))
        full = dict(os.environ, PYTHONIOENCODING='utf-8', FAKE_VERIFY_LOG=str(self.log),
                    ATELIER_VERIFY_CMD=json.dumps([sys.executable, str(self.root / 'fake_verify.py')]), **env)
        proc = subprocess.run([sys.executable, str(SCRIPT), '--dir', 'docs/pbi', '--result',
                               'docs/pbi/conductor/episodes/x-7-e2e.result.json', '--seq', '7'],
                              cwd=self.root, capture_output=True, text=True, encoding='utf-8', env=full)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.root / 'docs/pbi/verify/tests/e2e.test-result.json').read_text(encoding='utf-8'))
        episode = json.loads((self.root / 'docs/pbi/conductor/episodes/x-7-e2e.result.json').read_text(encoding='utf-8'))
        return result, episode

    def calls(self):
        return [json.loads(line) for line in self.log.read_text(encoding='utf-8').splitlines()] if self.log.exists() else []

    def test_scope_is_related_specs_then_specs_reading_changed_symbols(self):
        result, episode = self.run_e2e(FAKE_FAIL='e2e/login.cy.ts,e2e/cart.cy.ts')
        related = self.calls()[0]
        self.assertEqual(related[:4], ['--related', 'e2e/login.cy.ts,e2e/cart.cy.ts', '--each', '--name'])
        # tax.cy.ts は TAX_RATE を語として含まない (TAX_RATE_LABEL)。変えていない定義の名前は差分に無い
        scope = (self.root / 'docs/pbi/verify/e2e.scope.md').read_text(encoding='utf-8')
        self.assertIn('| e2e/login.cy.ts | related spec of PBI-001 | PBI-001 |', scope)
        self.assertIn('| e2e/cart.cy.ts | reads addItem (src/cart.ts) | PBI-001 |', scope)
        self.assertNotIn('tax.cy.ts', scope)
        failing = {row['test']: (row['cause'], row['pbi']) for row in result['failing']}
        self.assertEqual(failing, {'e2e/login.cy.ts': ('intended', 'PBI-001'),
                                   'e2e/cart.cy.ts': ('unknown', 'PBI-001')})
        self.assertEqual(result['kind'], 'e2e')
        self.assertEqual(result['recommendation'], 'needs-designer')
        self.assertEqual((episode['status'], episode['next'], episode['seq']), ('done', {'episode': 'test'}, 7))
        self.assertIn('2 specs (1 related, 1 by changed symbols), 2 failing (1 intended by design)',
                      episode['summary'][0])
        self.assertEqual(episode['summary'][1], 'full suite: not approved')
        # 通しは承認が無ければ流さない
        self.assertFalse(any('--final' in call for call in self.calls()))
        check = subprocess.run([sys.executable, str(HERE / 'role_results.py'), 'check',
                                str(self.root / 'docs/pbi/verify/tests/e2e.test-result.json'),
                                str(self.root / 'docs/pbi/conductor/episodes/x-7-e2e.result.json')],
                               capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(check.returncode, 0, check.stdout)

    def test_full_suite_runs_once_when_approved_and_a_stop_is_written_not_asked(self):
        self.settings['e2e']['full'] = True
        result, episode = self.run_e2e(FAKE_FULL_CODE='idle')
        finals = [call for call in self.calls() if '--final' in call]
        self.assertEqual(len(finals), 1)
        self.assertEqual(result['failing'][-1]['test'], 'full suite')
        self.assertIn('full suite: idle', result['blocked'])
        self.assertEqual(result['commands'][-1], {'command': 'npm run e2e', 'code': 125})
        self.assertEqual(episode['status'], 'done')
        self.assertTrue(any(line.startswith('blocked: ') for line in episode['summary']))

    def test_full_suite_failures_become_one_row_per_spec_from_the_log(self):
        self.settings['e2e']['full'] = True
        result, episode = self.run_e2e(FAKE_FULL_CODE='1', FAKE_FULL_LOG='  ✔  login.cy.ts  00:01\n'
                                                                       '  ✖  cart.cy.ts   00:05  1 failing\n')
        rows = {row['test']: row['pbi'] for row in result['failing']}
        self.assertEqual(rows, {'e2e/cart.cy.ts': 'PBI-001'})
        self.assertIn('failed in the full suite', result['failing'][0]['why'])

    def test_an_old_full_result_never_stands_in_for_a_failed_run(self):
        self.settings['e2e']['full'] = True
        stale = self.root / 'docs/pbi/verify/e2e-full.json'
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text(json.dumps({'results': [{'n': 1, 'command': 'full', 'code': 0, 'log': 'old.log'}]}))
        result, episode = self.run_e2e(FAKE_FULL_NO_RESULT='1')
        self.assertEqual(result['failing'][-1]['test'], 'full suite')
        self.assertEqual(result['commands'][-1]['code'], 2)
        self.assertIn('full suite: failed', episode['summary'][1])

    def test_stopping_the_script_stops_verify_and_its_children(self):
        """conductor が e2e_run.py を止めると、verify.py とその子も止まる (止めた出番の横でテストが残らない)。"""
        sys.path.insert(0, str(HERE))
        import conductor
        self.write('docs/pbi/tests-at-end.json', json.dumps(self.settings))
        marker = self.root / 'grandchild.pid'
        env = dict(os.environ, PYTHONIOENCODING='utf-8', FAKE_VERIFY_LOG=str(self.log), FAKE_VERIFY_HANG=str(marker),
                   ATELIER_VERIFY_CMD=json.dumps([sys.executable, str(self.root / 'fake_verify.py')]))
        proc = subprocess.Popen([sys.executable, str(SCRIPT), '--dir', 'docs/pbi', '--result', 'r.json', '--seq', '1'],
                                cwd=self.root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(proc.kill)
        deadline = time.monotonic() + 30
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(marker.exists(), 'the fake verify never started its child')
        time.sleep(0.5)
        grandchild = int(marker.read_text())
        self.assertTrue(conductor.stop_child(proc.pid))
        deadline = time.monotonic() + 15
        while conductor.pid_alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(conductor.pid_alive(grandchild), 'the test process outlived the stopped script')

    def test_symbols_off_and_no_investigate(self):
        self.settings['e2e']['symbols'] = False
        del self.settings['investigate']
        result, episode = self.run_e2e()
        self.assertEqual(self.calls(), [])
        self.assertIn('no investigate command', result['blocked'])
        scope = (self.root / 'docs/pbi/verify/e2e.scope.md').read_text(encoding='utf-8')
        self.assertIn('symbols: off', scope)
        self.assertNotIn('cart.cy.ts', scope)

    def test_nothing_in_range_passes(self):
        self.write('docs/pbi/PBI-001.md', '# PBI-001\n\n- Related specs: none\n')
        self.settings['e2e'] = {}
        result, episode = self.run_e2e()
        self.assertEqual((result['commands'], result['failing'], result['recommendation']), ([], [], 'pass'))
        self.assertIn('e2e: 0 specs', episode['summary'][0])


if __name__ == '__main__':
    unittest.main()
