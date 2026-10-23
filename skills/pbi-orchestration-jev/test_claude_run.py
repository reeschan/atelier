"""claude_run.py (実行先 claude のラッパー) のテスト。本物の claude は呼ばず、PBI_CLAUDE_BIN で渡す偽の claude で呼び出し方を確かめる。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'claude_run.py')
sys.path.insert(0, HERE)
import claude_run  # noqa: E402

# 偽の claude: 受け取った引数・標準入力・外すはずの環境変数が残っていたかを書いて、結果の json を出す
FAKE_BIN = r'''
import json, os, sys
with open(os.environ['FAKE_CLAUDE_LOG'], 'w', encoding='utf-8') as f:
    json.dump({'args': sys.argv[1:], 'stdin': sys.stdin.buffer.read().decode('utf-8'),
               'cwd': os.getcwd(),
               'env': {k: k in os.environ for k in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN',
                                                      'ANTHROPIC_BASE_URL', 'ANTHROPIC_BEDROCK_BASE_URL',
                                                      'CLAUDE_CODE_USE_BEDROCK', 'PATH')}}, f)
print(json.dumps({'type': 'result', 'is_error': False, 'num_turns': 3, 'total_cost_usd': 0.5,
                  'usage': {'input_tokens': 1, 'output_tokens': 2}, 'result': 'OK'}))
sys.stderr.write('diag\n')
sys.exit(int(os.environ.get('FAKE_CLAUDE_EXIT', '0')))
'''

STRIPPED = ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL', 'ANTHROPIC_BEDROCK_BASE_URL',
            'CLAUDE_CODE_USE_BEDROCK')


def make_wrapper(directory: Path, script: Path) -> Path:
    """偽の claude を呼ぶ実行ファイル。Windows は .cmd、それ以外は実行権を付けたシェルスクリプト。"""
    if os.name == 'nt':
        path = directory / 'claude.cmd'
        path.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding='utf-8')
    else:
        path = directory / 'claude'
        path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding='utf-8')
        path.chmod(0o755)
    return path


class PureFunctionsTest(unittest.TestCase):
    def test_settings_table(self):
        self.assertEqual(set(claude_run.SETTINGS), {'default', 'edit-only'})
        self.assertEqual(claude_run.SETTINGS['edit-only'], ('Read', 'Edit', 'Write', 'Glob', 'Grep'))
        self.assertNotIn('Bash', claude_run.SETTINGS['edit-only'])
        self.assertNotIn('PowerShell', claude_run.SETTINGS['edit-only'])
        for tool in claude_run.SETTINGS['default']:
            self.assertFalse(tool.startswith('mcp__'), tool)

    def test_build_args_default_and_edit_only(self):
        args = claude_run.build_args('sonnet', 'high', 'default')
        self.assertEqual(args[:5], ['-p', '--output-format', 'json', '--model', 'sonnet'])
        self.assertEqual(args[args.index('--effort') + 1], 'high')
        self.assertEqual(args[args.index('--tools') + 1], ','.join(claude_run.SETTINGS['default']))
        self.assertEqual(args[args.index('--permission-mode') + 1], 'acceptEdits')
        self.assertEqual(args[args.index('--permission-prompts') + 1], 'none')
        self.assertIn('--strict-mcp-config', args)
        self.assertNotIn('--restricted', args)
        self.assertNotIn('--add-dir', args)
        edit = claude_run.build_args('sonnet', None, 'edit-only')
        self.assertNotIn('--effort', edit)
        self.assertEqual(edit[edit.index('--tools') + 1], 'Read,Edit,Write,Glob,Grep')
        self.assertIn('--restricted', edit)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / 'repo'
        self.dir = self.repo / 'docs' / 'pbi' / 'claude'
        self.dir.mkdir(parents=True)
        (self.dir / 'PBI-001.prompt.md').write_text('依頼', encoding='utf-8')
        self.fake = self.tmp / 'fake_claude.py'
        self.fake.write_text(FAKE_BIN, encoding='utf-8')
        self.bin = make_wrapper(self.tmp, self.fake)
        self.log = self.tmp / 'fake.log'

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args, exit_code=0):
        env = {k: v for k, v in os.environ.items() if k != 'PBI_CLAUDE_BIN'}
        for name in STRIPPED:
            env[name] = 'secret-value'
        env.update(PBI_CLAUDE_BIN=str(self.bin), FAKE_CLAUDE_LOG=str(self.log), FAKE_CLAUDE_EXIT=str(exit_code))
        return subprocess.run([sys.executable, SCRIPT, *args], cwd=self.repo, capture_output=True, text=True,
                              encoding='utf-8', env=env)

    def logged(self):
        return json.loads(self.log.read_text(encoding='utf-8'))

    def test_default_setting_arguments_stdin_and_outputs(self):
        r = self.run_cli('PBI-001', '--model', 'sonnet', '--effort', 'high')
        self.assertEqual(r.returncode, 0, r.stderr)
        log = self.logged()
        args = log['args']
        self.assertEqual(args[:5], ['-p', '--output-format', 'json', '--model', 'sonnet'])
        self.assertEqual(args[args.index('--effort') + 1], 'high')
        self.assertEqual(args[args.index('--tools') + 1], 'Read,Write,Edit,Glob,Grep,Bash,PowerShell')
        self.assertEqual(args[args.index('--permission-mode') + 1], 'acceptEdits')
        self.assertEqual(args[args.index('--permission-prompts') + 1], 'none')
        self.assertIn('--strict-mcp-config', args)
        self.assertNotIn('--restricted', args)
        self.assertNotIn('--add-dir', args)
        self.assertIn('docs/pbi/claude/PBI-001.prompt.md', log['stdin'])
        self.assertIn('その指示どおりに作業してください', log['stdin'])
        self.assertEqual(Path(log['cwd']).resolve(), self.repo.resolve())
        self.assertEqual(json.loads((self.dir / 'PBI-001.json').read_text(encoding='utf-8'))['result'], 'OK')
        self.assertEqual((self.dir / 'PBI-001.err').read_text(encoding='utf-8').strip(), 'diag')
        run = json.loads((self.dir / 'PBI-001.run.json').read_text(encoding='utf-8'))
        self.assertEqual((run['exit'], run['model'], run['effort'], run['setting']), (0, 'sonnet', 'high', 'default'))
        self.assertIn('start', run)
        self.assertIn('end', run)

    def test_edit_only_setting_has_restricted_and_no_command_tools(self):
        r = self.run_cli('PBI-001', '--model', 'opus', '--setting', 'edit-only')
        self.assertEqual(r.returncode, 0, r.stderr)
        args = self.logged()['args']
        self.assertEqual(args[args.index('--tools') + 1], 'Read,Edit,Write,Glob,Grep')
        self.assertIn('--restricted', args)
        self.assertNotIn('--effort', args)
        run = json.loads((self.dir / 'PBI-001.run.json').read_text(encoding='utf-8'))
        self.assertEqual(run['setting'], 'edit-only')

    def test_null_effort_omits_effort(self):
        r = self.run_cli('PBI-001', '--model', 'haiku', '--effort', 'null')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('--effort', self.logged()['args'])

    def test_api_and_auth_environment_variables_are_removed(self):
        r = self.run_cli('PBI-001', '--model', 'sonnet')
        self.assertEqual(r.returncode, 0, r.stderr)
        env = self.logged()['env']
        for name in STRIPPED:
            self.assertFalse(env[name], name)
        self.assertTrue(env['PATH'])
        self.assertNotIn('secret-value', r.stdout + r.stderr)

    def test_exit_code_is_passed_through(self):
        r = self.run_cli('PBI-001', '--model', 'sonnet', exit_code=1)
        self.assertEqual(r.returncode, 1)
        run = json.loads((self.dir / 'PBI-001.run.json').read_text(encoding='utf-8'))
        self.assertEqual(run['exit'], 1)

    def test_unknown_setting_is_rejected(self):
        r = self.run_cli('PBI-001', '--model', 'sonnet', '--setting', 'full')
        self.assertEqual(r.returncode, 2)
        self.assertFalse(self.log.exists())

    def test_rejects_values_that_could_break_the_command_line(self):
        for model in ('x y', 'a&b', '"x"', ''):
            r = self.run_cli('PBI-001', '--model', model)
            self.assertEqual(r.returncode, 2, model)
        self.assertEqual(self.run_cli('PBI-001', '--model', 'sonnet', '--effort', 'a;b').returncode, 2)
        self.assertEqual(self.run_cli('../evil', '--model', 'sonnet').returncode, 2)
        self.assertFalse(self.log.exists())

    def test_missing_prompt_stops_with_2(self):
        r = self.run_cli('PBI-009', '--model', 'sonnet')
        self.assertEqual(r.returncode, 2)
        self.assertIn('依頼文がありません', r.stderr)

    def test_missing_bin_stops_with_2(self):
        env = {k: v for k, v in os.environ.items() if k != 'PBI_CLAUDE_BIN'}
        env['PBI_CLAUDE_BIN'] = str(self.tmp / 'nope')
        r = subprocess.run([sys.executable, SCRIPT, 'PBI-001', '--model', 'sonnet'], cwd=self.repo,
                           capture_output=True, text=True, encoding='utf-8', env=env)
        self.assertEqual(r.returncode, 2)
        self.assertIn('claude が見つかりません', r.stderr)

    def test_without_bin_and_without_claude_on_path_stops_with_2(self):
        env = {k: v for k, v in os.environ.items() if k != 'PBI_CLAUDE_BIN'}
        env['PATH'] = str(self.tmp / 'empty')
        r = subprocess.run([sys.executable, SCRIPT, 'PBI-001', '--model', 'sonnet'], cwd=self.repo,
                           capture_output=True, text=True, encoding='utf-8', env=env)
        self.assertEqual(r.returncode, 2)
        self.assertIn('claude が見つかりません', r.stderr)
        self.assertFalse((self.dir / 'PBI-001.run.json').exists())


if __name__ == '__main__':
    unittest.main()
