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

    def test_system_bash_splits_both_separators_on_any_host(self):
        for path in (r'C:\Windows\System32\bash.exe', 'C:/Windows/System32/bash.exe',
                     r'C:\Users\u\AppData\Local\Microsoft\WindowsApps\bash.exe'):
            with self.subTest(path=path):
                self.assertTrue(verify._system_bash(path))
        self.assertFalse(verify._system_bash(r'C:\Program Files\Git\bin\bash.exe'))
        self.assertFalse(verify._system_bash('/usr/bin/bash'))

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
        r = self.run_script('PBI-001', '--deferred')
        self.assertLess(time.monotonic() - started, 15)
        # 結果は書いたうえで、上限に当たったことを終了コード 3 で伝える
        self.assertEqual(self.results('PBI-001'), {1: 'timeout'})
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn('blocked: limit', r.stdout)

    def test_deferred_with_no_entries_for_the_phase_is_a_no_op(self):
        self.settings(deferred=['echo wave'])
        (self.base / 'PBI-001.md').write_text(pbi('echo wave >> wave.txt'), encoding='utf-8')
        r = self.run_script('PBI-001', '--deferred', '--when', 'end')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('deferred: none for when=end', r.stdout)
        self.assertFalse((self.root / 'wave.txt').exists())

    def test_related_fills_specs_into_investigate(self):
        self.settings(investigate='echo {specs} > specs.txt')
        r = self.run_script('--related', 'a.cy.ts,b.cy.ts')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.root / 'specs.txt').read_text(encoding='utf-8').strip(), 'a.cy.ts,b.cy.ts')
        self.assertEqual(self.results('related'), {1: 0})

    def test_related_without_specs_is_refused(self):
        self.settings(investigate='echo {specs} > ran.txt')
        r = self.run_script('--related', ' , ,')
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn('refused: --related has no spec', r.stdout)
        self.assertFalse((self.root / 'ran.txt').exists())

    def test_related_with_investigate_without_specs_placeholder_is_refused(self):
        self.settings(investigate='echo all > ran.txt')
        r = self.run_script('--related', 'a.cy.ts')
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn('refused: "investigate" in tests-at-end.json has no {specs}', r.stdout)
        self.assertFalse((self.root / 'ran.txt').exists())

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

    def test_related_each_runs_one_spec_at_a_time_and_retries_failures(self):
        # a は通り、b は毎回落ちる。b だけ limits.retries 回流し直す
        self.settings(investigate='echo {specs} >> runs.txt; test {specs} = a.cy.ts', limits={'retries': 1})
        r = self.run_script('--related', 'a.cy.ts,b.cy.ts', '--each', '--name', 'e2e')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        runs = (self.root / 'runs.txt').read_text(encoding='utf-8').split()
        self.assertEqual(runs, ['a.cy.ts', 'b.cy.ts', 'b.cy.ts'])
        data = json.loads((self.base / 'verify' / 'e2e.json').read_text(encoding='utf-8'))
        rows = {row['spec']: (row['code'], row['tries']) for row in data['results']}
        self.assertEqual(rows, {'a.cy.ts': (0, 1), 'b.cy.ts': (1, 2)})

    def test_related_each_keeps_the_results_when_a_spec_hits_the_limit(self):
        self.settings(investigate='test {specs} = a.cy.ts || sleep 30', limits={'per_spec': 1})
        r = self.run_script('--related', 'a.cy.ts,b.cy.ts', '--each', '--name', 'e2e')
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn('blocked: limit (b.cy.ts', r.stdout)
        self.assertEqual(self.results('e2e'), {1: 0, 2: 'timeout'})

    def test_checks_json_requires_format_and_lint_commands(self):
        (self.base / 'checks.json').write_text(json.dumps({'format': 'npx prettier --write', 'lint': 'npx eslint'}),
                                               encoding='utf-8')
        (self.base / 'PBI-001.md').write_text(pbi('npx prettier --write src/a.ts > fmt.txt', 'echo ok > ran.txt'),
                                              encoding='utf-8')
        r = self.run_script('PBI-001')
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn('refused: PBI-001 has no lint command (checks.json)', r.stdout)
        self.assertFalse((self.root / 'ran.txt').exists())
        self.assertEqual(self.run_script('PBI-001', '--list-deferred').returncode, 0)
        # 引数に現れるだけでは数えない
        (self.base / 'PBI-001.md').write_text(pbi('echo npx prettier --write src/a.ts', 'echo npx eslint src/a.ts'),
                                              encoding='utf-8')
        r = self.run_script('PBI-001')
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn('has no format or lint command', r.stdout)
        # コマンドの頭なら、前置き (cd ... &&、VAR=値) があっても数える
        self.assertTrue(verify.runs_command('cd web && CI=1 npx  eslint src/a.ts', 'npx eslint'))
        self.assertFalse(verify.runs_command('npx eslintx src/a.ts', 'npx eslint'))
        self.assertFalse(verify.runs_command('true # npx eslint', 'npx eslint'))

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

    def test_final_counts_output_without_a_newline_as_activity(self):
        self.settings(final='for i in 1 2 3 4 5; do printf .; sleep 1; done', limits={'idle': 3})
        self.approve_full_run()
        r = self.run_script('--final')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.results('final'), {1: 0})

    def test_final_keeps_the_approval_when_the_server_is_not_ready(self):
        url = f'http://127.0.0.1:{free_port()}/'
        self.settings(final='echo ran > ran.txt', server={'url': url})
        self.approve_full_run()
        r = self.run_script('--final')
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn('blocked: server', r.stdout)
        self.assertTrue((self.base / 'full-run.approved.json').exists())
        self.assertFalse((self.root / 'ran.txt').exists())

    def test_final_refuses_when_the_approval_went_during_server_start(self):
        """サーバーを用意するあいだに承認が使われたら (同時に始めた別の --final)、流さずに断る。"""
        port = free_port()
        url = f'http://127.0.0.1:{port}/'
        token = (self.base / 'full-run.approved.json').as_posix()
        start = (f'{PY} -c "import os;os.remove(\'{token}\')" && '
                 f'{PY} -m http.server {port} --bind 127.0.0.1')
        self.settings(final='echo ran > ran.txt', server={'start': start, 'url': url})
        self.approve_full_run()
        r = self.run_script('--final')
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn('refused: the full-run approval was already used', r.stdout)
        self.assertFalse((self.root / 'ran.txt').exists())
        self.assertFalse(answers(url))

    def test_deferred_keeps_finished_results_when_a_later_server_fails(self):
        """先の deferred を流し終えたあと、次のコマンドのサーバーが立たなくても、先の結果は残る。"""
        url = f'http://127.0.0.1:{free_port()}/'
        self.settings(deferred=[{'match': 'echo first'}, {'match': 'echo second', 'server': {'url': url}}])
        (self.base / 'PBI-001.md').write_text(pbi('echo first', 'echo second'), encoding='utf-8')
        r = self.run_script('PBI-001', '--deferred')
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn('blocked: server', r.stdout)
        self.assertEqual(self.results('PBI-001'), {1: 0})

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
        self.settings(final='cd . && echo whole  suite')
        # final そのもの・final を含むもの・cd の前置きを除いた最後のコマンドを含むもの
        for cmd in ('cd . && echo whole suite', 'cd . && echo whole suite >> ran.txt',
                    'echo whole suite >> ran.txt'):
            with self.subTest(cmd=cmd):
                (self.base / 'PBI-004.md').write_text(pbi('echo ok', cmd), encoding='utf-8')
                r = self.run_script('PBI-004')
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn('refused: PBI-004 verify 2 runs the whole suite (final); use related specs', r.stdout)
                self.assertFalse((self.root / 'ran.txt').exists())

    def test_runs_final_compares_the_last_command_of_final(self):
        final = 'cd e2e && pnpm run cy:run:parallel'
        self.assertTrue(verify.runs_final('pnpm run cy:run:parallel', final))
        self.assertTrue(verify.runs_final('cd e2e;  pnpm run  cy:run:parallel', final))
        self.assertFalse(verify.runs_final('cd e2e && pnpm exec cypress run --spec a.cy.ts', final))

    def test_example_settings_defer_nothing_that_runs_final(self):
        settings = json.loads(Path(HERE, 'examples', 'tests-at-end.json').read_text(encoding='utf-8'))
        final = verify.final_command(settings)
        for entry in verify.deferred_entries(settings):
            with self.subTest(match=entry['match']):
                self.assertNotIn(entry['match'], final)

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


@unittest.skipIf(os.name == 'nt', 'プロセスグループで止めるのは Windows 以外')
class KillTreeTest(unittest.TestCase):
    def test_stops_the_group_after_the_launcher_exited(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        pid_file = root / 'pid'
        proc = subprocess.Popen(['bash', '-c', f'sleep 60 >/dev/null 2>&1 & echo $! > "{pid_file}"'],
                                stdin=subprocess.DEVNULL, **verify.NEW_GROUP)
        proc.wait(timeout=10)
        child = int(pid_file.read_text(encoding='utf-8').strip())
        os.kill(child, 0)
        verify.kill_tree(proc)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        self.fail(f'sleep (pid {child}) is still running')


if __name__ == '__main__':
    unittest.main()
