import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

import verify

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

    def test_skip_deferred_runs_nothing_when_every_command_is_deferred(self):
        self.defer('echo')
        r = self.run_script('PBI-001', '--skip-deferred')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ran(), 0)
        self.assertIn('すべてあとに回す', r.stdout)

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


# 子の bash から流す Python (Git Bash でも読める / の形)
PY = '"' + sys.executable.replace('\\', '/') + '"'


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def answers(url):
    try:
        urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=2).close()
        return True
    except OSError:
        return False


class FindBashTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def touch(self, *parts):
        p = self.root.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('', encoding='utf-8')
        return str(p)

    def test_windows_rejects_the_system32_bash(self):
        wsl = r'C:\Windows\System32\bash.exe'
        found = verify.find_bash(self.root, 'nt', lambda name: wsl if name == 'bash' else None)
        self.assertIsNone(found)

    def test_windows_rejects_the_windowsapps_bash(self):
        shim = r'C:\Users\u\AppData\Local\Microsoft\WindowsApps\bash.exe'
        self.assertIsNone(verify.find_bash(self.root, 'nt', lambda name: shim if name == 'bash' else None))

    def test_windows_prefers_tools_bash_path(self):
        configured = self.touch('custom', 'bash.exe')
        git = self.touch('Git', 'cmd', 'git.exe')
        self.touch('Git', 'bin', 'bash.exe')
        (self.root / 'agents.json').write_text(json.dumps({'tools': {'bash': {'path': configured}}}),
                                               encoding='utf-8')
        found = verify.find_bash(self.root, 'nt', lambda name: git if name == 'git' else None)
        self.assertEqual(found, configured)

    def test_windows_uses_the_bash_of_the_git_install(self):
        for where in (('cmd',), ('bin',), ('mingw64', 'bin')):
            with self.subTest(where=where):
                git = self.touch('Git', *where, 'git.exe')
                bash = self.touch('Git', 'bin', 'bash.exe')
                wsl = r'C:\Windows\System32\bash.exe'
                found = verify.find_bash(self.root / 'nohome', 'nt',
                                         lambda name, g=git: g if name == 'git' else wsl)
                self.assertEqual(found, bash)


class VerifyEnvAndModesTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.base = self.root / 'docs' / 'pbi'
        self.base.mkdir(parents=True)
        self.env = dict(os.environ, ATELIER_HOME=str(self.root / 'home'), FNM_DIR=str(self.root / 'fnm'))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def settings(self, **data):
        (self.base / 'tests-at-end.json').write_text(json.dumps(data), encoding='utf-8')

    def run_script(self, *args):
        return subprocess.run([sys.executable, SCRIPT, *args], cwd=self.root, env=self.env,
                              capture_output=True, text=True, encoding='utf-8')

    def results(self, name):
        data = json.loads((self.base / 'verify' / f'{name}.json').read_text(encoding='utf-8'))
        return {r['n']: r['code'] for r in data['results']}

    def approve_full_run(self):
        (self.base / 'full-run.approved.json').write_text('{}', encoding='utf-8')

    def fake_node(self, version):
        installation = self.root / 'fnm' / 'node-versions' / f'v{version}' / 'installation'
        directory = installation if os.name == 'nt' else installation / 'bin'
        directory.mkdir(parents=True)
        node = directory / ('node.exe' if os.name == 'nt' else 'node')
        node.write_text('#!/bin/sh\necho v' + version + '\n', encoding='utf-8')
        node.chmod(0o755)
        return directory

    def test_node_of_node_version_comes_first_on_the_child_path(self):
        directory = self.fake_node('99.1.2')
        self.fake_node('99.0.0')
        (self.root / '.node-version').write_text('99.1.2\n', encoding='utf-8')
        # Git の bin\bash.exe は mingw64/bin などを PATH の前に足すので、子の中で node がどこに解決されるかを見る
        (self.base / 'PBI-001.md').write_text(
            pbi(f'{PY} -c "import shutil;print(shutil.which(\'node\'))" > node.txt'), encoding='utf-8')
        r = self.run_script('PBI-001')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        node = (self.root / 'node.txt').read_text(encoding='utf-8').strip()
        self.assertEqual(os.path.normcase(os.path.dirname(os.path.normpath(node))),
                         os.path.normcase(str(directory)))
        log = (self.base / 'verify' / 'PBI-001.1.log').read_text(encoding='utf-8').splitlines()
        self.assertTrue(log[0].startswith('bash: '), log)
        self.assertTrue(log[1].startswith('node: '), log)

    def test_missing_node_version_warns_and_runs(self):
        (self.root / 'package.json').write_text(json.dumps({'engines': {'node': '>=98'}}), encoding='utf-8')
        (self.base / 'PBI-001.md').write_text(pbi('echo ok'), encoding='utf-8')
        r = self.run_script('PBI-001')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('warn node: 98 not found; using PATH', r.stdout)
        self.assertEqual(self.results('PBI-001'), {1: 0})

    def test_deferred_when_end_runs_only_the_end_entries(self):
        self.settings(deferred=['echo wave', {'match': 'echo end', 'when': 'end'}])
        (self.base / 'PBI-001.md').write_text(pbi('echo wave >> wave.txt', 'echo end >> end.txt'),
                                              encoding='utf-8')
        self.assertEqual(self.run_script('PBI-001', '--list-deferred').stdout.strip(), '1,2')
        r = self.run_script('PBI-001', '--deferred', '--when', 'end')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.root / 'wave.txt').exists())
        self.assertTrue((self.root / 'end.txt').exists())
        self.assertEqual(self.results('PBI-001'), {2: 0})
        self.run_script('PBI-001', '--deferred')
        self.assertTrue((self.root / 'wave.txt').exists())

    def test_deferred_entry_timeout_applies(self):
        self.settings(deferred=[{'match': 'sleep', 'timeout': 1}])
        (self.base / 'PBI-001.md').write_text(pbi('sleep 20'), encoding='utf-8')
        started = time.monotonic()
        self.run_script('PBI-001', '--deferred')
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(self.results('PBI-001'), {1: 'timeout'})

    def test_related_fills_specs_into_investigate(self):
        self.settings(investigate='echo {specs} > specs.txt')
        r = self.run_script('--related', 'a.cy.ts,b.cy.ts')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.root / 'specs.txt').read_text(encoding='utf-8').strip(), 'a.cy.ts,b.cy.ts')
        self.assertEqual(self.results('related'), {1: 0})

    def test_related_stops_at_specs_times_per_spec(self):
        self.settings(investigate='echo {specs}; sleep 30', limits={'per_spec': 1})
        started = time.monotonic()
        r = self.run_script('--related', 'a.cy.ts,b.cy.ts', '--name', 'rel')
        elapsed = time.monotonic() - started
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn('blocked: limit', r.stdout)
        self.assertGreaterEqual(elapsed, 2)
        self.assertLess(elapsed, 20)
        self.assertEqual(self.results('rel'), {1: 'timeout'})

    def test_final_stops_when_output_goes_quiet(self):
        self.settings(final='echo start; sleep 30; echo never', limits={'idle': 1})
        self.approve_full_run()
        started = time.monotonic()
        r = self.run_script('--final')
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn('blocked: idle', r.stdout)
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(self.results('final'), {1: 'idle'})

    def test_final_has_no_total_limit_while_output_flows(self):
        self.settings(final='for i in 1 2 3; do echo $i; sleep 1; done', limits={'idle': 3, 'per_spec': 1})
        self.approve_full_run()
        r = self.run_script('--final')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.results('final'), {1: 0})

    def test_final_without_approval_is_refused(self):
        self.settings(final='echo ran > ran.txt')
        r = self.run_script('--final')
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn('refused: no full-run approval (conductor.py answer --full-run yes)', r.stdout)
        self.assertFalse((self.root / 'ran.txt').exists())

    def test_final_uses_up_the_approval(self):
        self.settings(final='echo ran >> ran.txt')
        self.approve_full_run()
        self.assertEqual(self.run_script('--final').returncode, 0)
        self.assertFalse((self.base / 'full-run.approved.json').exists())
        r = self.run_script('--final')
        self.assertEqual(r.returncode, 2)
        self.assertEqual(len((self.root / 'ran.txt').read_text(encoding='utf-8').splitlines()), 1)

    def test_pbi_verify_that_runs_final_is_refused(self):
        self.settings(final='echo whole suite')
        for cmd in ('echo whole suite', 'cd . && echo whole suite >> ran.txt'):
            with self.subTest(cmd=cmd):
                (self.base / 'PBI-004.md').write_text(pbi('echo ok', cmd), encoding='utf-8')
                r = self.run_script('PBI-004')
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn('refused: PBI-004 verify 2 runs the whole suite (final); use related specs', r.stdout)
                self.assertFalse((self.root / 'ran.txt').exists())

    def test_deferred_patterns_lists_every_match(self):
        self.settings(deferred=['pnpm test', {'match': 'cy:run', 'when': 'end'}])
        self.assertEqual(verify.deferred_patterns(self.base), ['pnpm test', 'cy:run'])

    def test_server_that_answers_is_used_and_left_running(self):
        httpd = http.server.HTTPServer(('127.0.0.1', 0), QuietHandler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        url = f'http://127.0.0.1:{httpd.server_address[1]}/'
        self.settings(final='echo ok', server={'start': 'echo started > started.txt', 'url': url})
        self.approve_full_run()
        r = self.run_script('--final')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.root / 'started.txt').exists())
        self.assertTrue(answers(url))

    def test_server_that_does_not_answer_is_started_and_stopped(self):
        port = free_port()
        url = f'http://127.0.0.1:{port}/'
        probe = f'{PY} -c "import urllib.request;urllib.request.urlopen(\'{url}\')"'
        self.settings(deferred=[{'match': 'urlopen', 'server': {
            'start': f'{PY} -m http.server {port} --bind 127.0.0.1', 'url': url}}])
        (self.base / 'PBI-001.md').write_text(pbi(probe), encoding='utf-8')
        r = self.run_script('PBI-001', '--deferred')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.results('PBI-001'), {1: 0})
        self.assertFalse(answers(url))


if __name__ == '__main__':
    unittest.main()
