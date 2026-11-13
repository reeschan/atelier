"""tools_check.py を CLI から、実際の PC の道具を見ない環境で動かし、有無・キーの無さ・入れ方と代わりの列を確かめる。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tools_check  # noqa: E402
SCRIPT = os.path.join(HERE, 'tools_check.py')
FAKE = os.path.join(HERE, 'testdata', 'fake_jevlint.py')
FIXTURES = os.path.join(HERE, 'testdata', 'present')
sys.path.insert(0, FIXTURES)
import fakes  # noqa: E402
TOOLS = ['omm', 'omm-scan', 'jevlint', 'jev', 'sac-strategic-compact',
         'principle-test-behavior-not-implementation', 'answer-me-with-any', 'node']
SECRET = 'super-secret-jev-key'
CONFIRM_OFF = 'confirm: off (the question file in chat, no reply check)'


class ToolsCheckTest(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.join(tempfile.gettempdir(), f'tools-check-{uuid.uuid4().hex}')
        for name in ('home', 'plugins', 'skills', 'empty'):
            os.makedirs(os.path.join(self.dir, name))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        empty = os.path.join(self.dir, 'empty')
        self.env = dict(os.environ, PATH=empty, FNM_DIR=empty, NVM_DIR=empty, HOME=empty, APPDATA=empty,
                        LOCALAPPDATA=empty, USERPROFILE=empty, PBI_PLUGINS_DIR=os.path.join(self.dir, 'plugins'))
        self.env.pop('TYPESAFE_API_KEY', None)
        self.env['TYPESAFE_CREDENTIALS_FILE'] = os.path.join(self.dir, 'no-such-credentials.json')

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
                self.assertTrue(row[2] and row[3])
                if tool == 'jev':
                    self.assertTrue(row[1].startswith('no-key ファイル無し: '), row[1])
                    self.assertIn('no-such-credentials.json', row[1])
                else:
                    self.assertEqual(row[1], 'missing')
        self.assertIn('https://github.com/reeschan/sac-strategic-compact', rows['sac-strategic-compact'][2])
        self.assertIn('prompts/lint-fallback.md', rows['jevlint'][3])
        install = rows['answer-me-with-any'][2]
        for part in ('https://github.com/reeschan/answer-me-with-any', 'skills/answer-me-with-any',
                     '~/.claude/skills/answer-me-with-any', 'only after the user says yes'):
            self.assertIn(part, install)
        self.assertIn(CONFIRM_OFF, rows['answer-me-with-any'][3])
        self.assertEqual(rows['node'][1], 'missing')
        self.assertIn('fnm install 22', rows['node'][2])
        self.assertIn(CONFIRM_OFF, rows['node'][3])
        self.assertEqual(os.listdir(os.path.join(self.dir, 'skills')), [])

    def test_found_tools_and_a_missing_key(self):
        with open(os.path.join(self.dir, 'home', 'agents.json'), 'w', encoding='utf-8') as f:
            json.dump({'tools': {'jevlint': {'path': FAKE}}}, f)
        self.touch('plugins', 'oh-my-mermaid', '0.2.0', 'skills', 'omm-scan', 'SKILL.md')
        self.touch('plugins', 'pstack', '0.15.32', 'skills', 'principle-test-behavior-not-implementation', 'SKILL.md')
        self.touch('skills', 'sac-strategic-compact', 'SKILL.md')
        self.touch('skills', 'answer-me-with-any', 'SKILL.md')
        self.touch('skills', 'answer-me-with-any', 'scripts', 'am.mjs')
        rows = self.rows()
        self.assertTrue(rows['jevlint'][1].startswith('ok ') and rows['jevlint'][1].endswith('0.0.0-fake'))
        for tool in ('omm-scan', 'sac-strategic-compact', 'principle-test-behavior-not-implementation',
                     'answer-me-with-any'):
            self.assertTrue(rows[tool][1].startswith('ok '), rows[tool])
        self.assertEqual(rows['omm'][1], 'missing')
        self.assertTrue(self.rows(FAKE_JEVLINT_KEYS='')['jevlint'][1].startswith('no-key '))


    def test_confirm_without_am_mjs_is_missing(self):
        """SKILL.md があっても scripts/am.mjs が無ければ ok にしない (present.py は am.mjs で見せる)。"""
        self.touch('skills', 'answer-me-with-any', 'SKILL.md')
        status = self.rows()['answer-me-with-any'][1]
        self.assertTrue(status.startswith('missing scripts/am.mjs (found '), status)

    def test_confirm_old_name_is_ok_with_a_rename_note(self):
        """古い名前のスキルしか無いときも ok にし、新しい名前に変えるよう添える。"""
        self.touch('skills', 'answer-me-with-html', 'SKILL.md')
        self.touch('skills', 'answer-me-with-html', 'scripts', 'am.mjs')
        status = self.rows()['answer-me-with-any'][1]
        self.assertTrue(status.startswith('ok '), status)
        self.assertIn('old name answer-me-with-html; install answer-me-with-any and remove it', status)

    def test_jev_row_names_the_source_and_hides_the_key(self):
        cred = os.path.join(self.dir, 'credentials.json')
        with open(cred, 'w', encoding='utf-8') as f:
            json.dump({'api_key': SECRET}, f)
        from_file = self.rows(TYPESAFE_CREDENTIALS_FILE=cred)
        self.assertEqual(from_file['jev'][1], f'ok {cred}')
        self.assertNotIn(SECRET, '\t'.join(from_file['jev']))
        from_env = self.rows(TYPESAFE_API_KEY=SECRET)
        self.assertEqual(from_env['jev'][1], 'ok env')
        self.assertNotIn(SECRET, '\t'.join(from_env['jev']))
        self.assertIn('facts only', from_env['jev'][3])

    def test_jev_probe_reports_http_status_without_the_key_and_default_does_not_connect(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                auth = self.headers.get('Authorization') or ''
                if SECRET not in auth:
                    self.send_response(401)
                else:
                    self.send_response(401)
                self.send_header('Content-Length', '0')
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        url = f'http://127.0.0.1:{server.server_address[1]}/v1/models'
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            quiet = tools_check.jev_status({'TYPESAFE_API_KEY': SECRET, 'HOME': self.dir}, probe=False,
                                           models_url=url)
            self.assertEqual(quiet, 'ok env')
            self.assertEqual(seen, [])
            probed = tools_check.jev_status({'TYPESAFE_API_KEY': SECRET, 'HOME': self.dir}, probe=True,
                                            models_url=url)
            self.assertEqual(probed, 'http-401 env')
            self.assertNotIn(SECRET, probed)
            self.assertEqual(seen, ['/v1/models'])
        finally:
            server.shutdown()
            server.server_close()

    def test_node_row_ok_with_a_fake_node(self):
        """偽の node (v22.1.0) だけを PATH に載せると、node の行は ok <場所> v22.1.0 になる。"""
        self.env['PATH'] = fakes.install_node(os.path.join(self.dir, 'nodebin'))
        status = self.rows(FAKE_NODE_VERSION='v22.1.0')['node'][1]
        self.assertTrue(status.startswith('ok '), status)
        self.assertTrue(status.endswith('v22.1.0'), status)

    def test_node_row_old_below_20(self):
        """版が 20 未満の偽の node (v18.19.0) は old。install と fallback の列は埋まっている。"""
        self.env['PATH'] = fakes.install_node(os.path.join(self.dir, 'nodebin'))
        row = self.rows(FAKE_NODE_VERSION='v18.19.0')['node']
        self.assertEqual(row[1], 'old v18.19.0')
        self.assertIn('fnm install 22', row[2])
        self.assertIn(CONFIRM_OFF, row[3])


if __name__ == '__main__':
    unittest.main()
