"""role_run.py (レビュー担当・テスト担当を agent 以外の実行先で動かす) を CLI で確かめる。

本物の CLI は呼ばない。codex・opencode・agy は ATELIER_<RUNNER>_CMD で、claude は PBI_CLAUDE_BIN の包みで、
dsh は PBI_DSH_NODE と PBI_DSH_BIN で偽物を差す。偽物は受け取った引数と標準入力を書き、FAKE_EXIT で終わる。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / 'role_run.py'
sys.path.insert(0, str(HERE))
from test_claude_run import make_wrapper  # noqa: E402

FAKE = r'''
import json, os, sys
stdin = '' if sys.stdin is None or sys.stdin.closed else sys.stdin.buffer.read().decode('utf-8', 'replace')
with open(os.environ['FAKE_LOG'], 'a', encoding='utf-8') as f:
    f.write(json.dumps({'args': sys.argv[1:], 'stdin': stdin,
                        'opencode_config': os.environ.get('OPENCODE_CONFIG')}) + '\n')
args = sys.argv[1:]
if '-o' in args:
    open(args[args.index('-o') + 1], 'w', encoding='utf-8').write('done\n')
print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1}}))
sys.exit(int(os.environ.get('FAKE_EXIT', '0')))
'''

PROMPT = 'Work root: x\nReview PBI-001.\n'


class RoleRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='atelier-role-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / 'repo'
        self.roles = self.repo / 'docs' / 'pbi' / 'roles'
        self.roles.mkdir(parents=True)
        self.fake = self.tmp / 'fake.py'
        self.fake.write_text(FAKE, encoding='utf-8')
        self.log = self.tmp / 'fake.log'
        self.home = self.tmp / 'home'
        (self.home / '.config' / 'deepseek').mkdir(parents=True)
        (self.home / '.config' / 'deepseek' / 'deepseek.key').write_text('sk-test\n', encoding='utf-8')
        self.cmd = json.dumps([sys.executable, str(self.fake)])
        self.codex_home = self.tmp / 'codex-home'
        self.codex_home.mkdir()
        (self.codex_home / 'auth.json').write_text('{}', encoding='utf-8')
        (self.codex_home / 'config.toml').write_text('[mcp_servers.shared-memory]\nurl = "x"\n', encoding='utf-8')

    def prompt(self, name='PBI-001.review'):
        (self.roles / f'{name}.prompt.md').write_text(PROMPT, encoding='utf-8')

    def run_cli(self, *args, exit_code=0, extra_env=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith('ATELIER_') and k not in ('PBI_CLAUDE_BIN', 'OPENAI_API_KEY')}
        env.update(FAKE_LOG=str(self.log), FAKE_EXIT=str(exit_code), ATELIER_HOME=str(self.home / 'atelier'),
                   ATELIER_CODEX_CMD=self.cmd, ATELIER_OPENCODE_CMD=self.cmd, ATELIER_AGY_CMD=self.cmd,
                   CODEX_HOME=str(self.codex_home),
                   PBI_CLAUDE_BIN=str(make_wrapper(self.tmp, self.fake)), FAKE_CLAUDE_LOG=str(self.log),
                   PBI_DSH_NODE=sys.executable, PBI_DSH_BIN=str(self.fake), FAKE_DSH_LOG=str(self.log),
                   USERPROFILE=str(self.home), HOME=str(self.home))
        env.update(extra_env or {})
        return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=self.repo, capture_output=True, text=True,
                              encoding='utf-8', errors='replace', env=env, timeout=120, check=False)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text(encoding='utf-8').splitlines() if line.strip()]

    def test_codex_reviewer_runs_codex_exec_with_the_definition_first(self):
        self.prompt()
        r = self.run_cli('PBI-001.review', '--role', 'reviewer', '--runner', 'codex', '--model', 'gpt-6.1-terra',
                         '--effort', 'high')
        self.assertEqual(r.returncode, 0, r.stderr)
        call = self.calls()[0]
        args = call['args']
        self.assertEqual(args[:3], ['-a', 'never', 'exec'])
        self.assertEqual(args[args.index('-m') + 1], 'gpt-6.1-terra')
        self.assertEqual(args[args.index('-s') + 1], 'danger-full-access')
        self.assertIn('model_reasoning_effort="high"', args)
        self.assertIn('mcp_servers.shared-memory.default_tools_approval_mode="auto"', args)
        self.assertEqual(args[-1], '-')
        first, second = call['stdin'].splitlines()[:2]
        self.assertTrue(first.startswith('Writing files: On Windows'), first)
        self.assertTrue(second.startswith('Read ') and second.endswith('agents/atelier-reviewer.md and follow it '
                                                                        'as your instructions.'), second)
        self.assertIn('Review PBI-001.', call['stdin'])
        for suffix in ('jsonl', 'err', 'last.md', 'run.json'):
            self.assertTrue((self.roles / f'PBI-001.review.{suffix}').is_file(), suffix)
        run = json.loads((self.roles / 'PBI-001.review.run.json').read_text(encoding='utf-8'))
        self.assertEqual((run['role'], run['runner'], run['model'], run['exit']),
                         ('reviewer', 'codex', 'gpt-6.1-terra', 0))
        self.assertIn('role_run: exit=0 role=reviewer runner=codex', r.stdout)

    def test_definition_line_is_added_once(self):
        self.prompt()
        for _ in range(2):
            self.run_cli('PBI-001.review', '--role', 'reviewer', '--runner', 'codex', '--model', 'gpt-6.1-terra')
        text = (self.roles / 'PBI-001.review.prompt.md').read_text(encoding='utf-8')
        self.assertEqual(text.count('atelier-reviewer.md'), 1)

    def test_runners_md_carries_the_same_text_rule(self):
        """Claude の設計者が codex の作業者に渡す決まり (runners.md) は、role_run と設計者の依頼文の決まりと同じ文。"""
        import runner_cli
        text = (HERE / 'runners.md').read_text(encoding='utf-8')
        self.assertIn(f'Writing files: {runner_cli.CODEX_TEXT_RULE}', text)

    def test_sandbox_can_be_narrowed(self):
        self.prompt()
        r = self.run_cli('PBI-001.review', '--role', 'reviewer', '--runner', 'codex', '--model', 'gpt-6.1-terra',
                         '--sandbox', 'workspace-write')
        self.assertEqual(r.returncode, 0, r.stderr)
        args = self.calls()[0]['args']
        self.assertEqual(args[args.index('-s') + 1], 'workspace-write')
        r = self.run_cli('PBI-001.review', '--role', 'reviewer', '--runner', 'codex', '--model', 'gpt-6.1-terra',
                         '--sandbox', 'open')
        self.assertEqual(r.returncode, 2, r.stderr)
        # read-only では結果の JSON を書けないので断る
        r = self.run_cli('PBI-001.review', '--role', 'reviewer', '--runner', 'codex', '--model', 'gpt-6.1-terra',
                         '--sandbox', 'read-only')
        self.assertEqual(r.returncode, 2, r.stderr)

    def test_exit_code_is_passed_through(self):
        self.prompt('test-final')
        r = self.run_cli('test-final', '--role', 'tester', '--runner', 'codex', '--model', 'gpt-6.1-terra',
                         exit_code=3)
        self.assertEqual(r.returncode, 3, r.stderr)

    def test_claude_tester_goes_through_claude_run_in_roles(self):
        self.prompt('test-wave-1')
        r = self.run_cli('test-wave-1', '--role', 'tester', '--runner', 'claude', '--model', 'sonnet')
        self.assertEqual(r.returncode, 0, r.stderr)
        call = json.loads(self.log.read_text(encoding='utf-8'))
        self.assertIn('-p', call['args'])
        self.assertEqual(call['args'][call['args'].index('--model') + 1], 'sonnet')
        self.assertIn('docs/pbi/roles/test-wave-1.prompt.md', call['stdin'])
        self.assertTrue((self.roles / 'test-wave-1.json').is_file())
        text = (self.roles / 'test-wave-1.prompt.md').read_text(encoding='utf-8')
        self.assertIn('atelier-tester.md', text.splitlines()[0])

    def test_dsh_reviewer_goes_through_dsh_run_in_roles(self):
        self.prompt()
        r = self.run_cli('PBI-001.review', '--role', 'reviewer', '--runner', 'dsh',
                         '--model', 'deepseek-official/deepseek-flash', '--effort', 'high')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.roles / 'PBI-001.review.patch.yml').is_file())
        self.assertTrue((self.roles / 'PBI-001.review.run.json').is_file())

    def test_opencode_and_agy_commands(self):
        self.prompt()
        r = self.run_cli('PBI-001.review', '--role', 'reviewer', '--runner', 'opencode',
                         '--model', 'opencode-go/glm-5.3', '--effort', 'max')
        self.assertEqual(r.returncode, 0, r.stderr)
        call = self.calls()[-1]
        self.assertEqual(call['args'][:5], ['run', '-m', 'opencode-go/glm-5.3#max', '--format', 'json'])
        self.assertTrue(call['opencode_config'].endswith('opencode.config.json'))
        r = self.run_cli('PBI-001.review', '--role', 'reviewer', '--runner', 'agy', '--model', 'gemini-x')
        self.assertEqual(r.returncode, 0, r.stderr)
        args = self.calls()[-1]['args']
        self.assertEqual(args[0], '-p')
        self.assertEqual(args[args.index('--mode') + 1], 'accept-edits')
        self.assertTrue(args[args.index('--settings') + 1].endswith('agy.settings.json'))

    def test_agy_settings_allow_commands_and_deny_destructive_ones(self):
        settings = json.loads((HERE / 'agy.settings.json').read_text(encoding='utf-8'))
        self.assertEqual(settings['permissions']['allow'], ['command(*)'])
        rules = [re.compile(r[len('command(regex:'):-1]) for r in settings['permissions']['deny']]

        def denied(command):
            return any(rule.fullmatch(command) for rule in rules)
        for command in ('git commit -m x', 'Set-Content b.txt hi; git add b.txt; git commit -m t',
                        'Write-Output start; git push', 'git checkout -- .', 'rm -rf node_modules',
                        'Remove-Item -Path dist -Recurse -Force'):
            self.assertTrue(denied(command), command)
        for command in ('rg foo src', 'git status --short; git diff', 'pnpm run check',
                        'Get-Content a.txt | Select-String foo', 'git log --oneline', 'rm a.txt'):
            self.assertFalse(denied(command), command)

    def test_refusals(self):
        self.prompt()
        base = ['--role', 'reviewer', '--model', 'gpt-6.1-terra']
        for args in (['PBI-001.review', *base, '--runner', 'agent'],
                     ['PBI-001.review', *base, '--runner', 'mcp'],
                     ['../x', *base, '--runner', 'codex'],
                     ['missing', *base, '--runner', 'codex'],
                     ['PBI-001.review', '--role', 'reviewer', '--model', 'a b', '--runner', 'codex']):
            r = self.run_cli(*args)
            self.assertEqual(r.returncode, 2, f'{args}: {r.stderr}')
        r = self.run_cli('PBI-001.review', *base, '--runner', 'codex',
                         extra_env={'ATELIER_CODEX_CMD': '', 'PATH': os.path.dirname(sys.executable)})
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn('codex not found', r.stderr)


    def test_codex_home_with_only_a_config_is_allowed(self):
        """auth.json が無くても config.toml があれば断らない (サインインは keyring にも置ける)。"""
        self.prompt()
        keyring_home = self.tmp / 'keyring-home'
        keyring_home.mkdir()
        (keyring_home / 'config.toml').write_text('cli_auth_credentials_store = "keyring"\n', encoding='utf-8')
        r = self.run_cli('PBI-001.review', '--role', 'reviewer', '--runner', 'codex', '--model', 'gpt-6.1-terra',
                         extra_env={'CODEX_HOME': str(keyring_home)})
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_codex_home_allowed_when_the_command_picks_the_credentials_store(self):
        """ATELIER_CODEX_CMD が -c cli_auth_credentials_store で置き場を選んでいれば、空のホームでも断らない。"""
        self.prompt()
        empty = self.tmp / 'empty-keyring-home'
        empty.mkdir()
        cmd = json.dumps([sys.executable, str(self.fake), '-c', 'cli_auth_credentials_store="keyring"'])
        r = self.run_cli('PBI-001.review', '--role', 'reviewer', '--runner', 'codex', '--model', 'gpt-6.1-terra',
                         extra_env={'CODEX_HOME': str(empty), 'ATELIER_CODEX_CMD': cmd})
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_codex_home_without_sign_in_is_refused(self):
        self.prompt()
        empty = self.tmp / 'empty-home'
        empty.mkdir()
        r = self.run_cli('PBI-001.review', '--role', 'reviewer', '--runner', 'codex', '--model', 'gpt-6.1-terra',
                         extra_env={'CODEX_HOME': str(empty)})
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn('no sign-in', r.stderr)
        self.assertFalse(self.log.exists())


if __name__ == '__main__':
    unittest.main()
