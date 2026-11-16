import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from atelier import guards, runners


class RunnerTests(unittest.TestCase):
    def test_external_usage_and_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / 'prompt.txt'
            prompt.write_text('work', encoding='utf-8')
            fake = Path(__file__).parent / 'fakes' / 'cli.py'
            with patch.dict(os.environ, {'ATELIER_CODEX_CMD': json.dumps([sys.executable, str(fake)])}):
                spec = runners.launch_spec('worker', {'runner': 'codex', 'model': 'test'}, prompt, root / 'result.json', root)
                self.assertIn('sandbox_workspace_write.network_access=false', spec)
                result = runners.run_external(spec, 10)
            self.assertEqual(result['tokens'], {'input': 7, 'output': 3, 'total': 10})
            self.assertIsNone(result['unusable_reason'])

    def test_guard_detects_changed_existing_untracked_and_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / 'old.txt').write_text('before')
            before = guards.snapshot(root, allowed_paths=['allowed'], report_path='result.json')
            (root / 'old.txt').write_text('after')
            (root / 'package-lock.json').write_text('{}')
            violations = guards.compare(root, before)
            self.assertTrue(any(v['rule'] == 'G1' and 'old.txt' in v['detail'] for v in violations))
            self.assertTrue(any(v['rule'] == 'G5' for v in violations))
            self.assertTrue(any(v['rule'] == 'G14' for v in violations))

    def test_other_runners_and_denied_or_missing_output(self):
        fake = Path(__file__).parent / 'fakes' / 'cli.py'
        samples = {
            'agy': {'response': 'done', 'usage': {'input_tokens': 4, 'output_tokens': 2}},
            'opencode': {'type': 'text', 'part': {'text': 'done', 'tokens': {'input': 4, 'output': 2}}},
            'dsh': {'type': 'final', 'text': 'done', 'usage': {'input_tokens': 4, 'output_tokens': 2}},
            'claude': {'result': 'done', 'total_cost_usd': 0.25, 'modelUsage': {'m': {'inputTokens': 4, 'outputTokens': 2}}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, sample in samples.items():
                with self.subTest(runner=name), patch.dict(os.environ, {'ATELIER_' + name.upper() + '_CMD': json.dumps([sys.executable, str(fake)]), 'FAKE_OUTPUT': json.dumps(sample)}):
                    spec = runners.launch_spec('worker', {'runner': name, 'model': 'provider/model'}, root / 'prompt', root / (name + '.json'), root)
                    result = runners.run_external(spec, 10)
                    self.assertEqual(result['tokens'], {'input': 4, 'output': 2, 'total': 6})
                    self.assertIsNone(result['unusable_reason'])
            with patch.dict(os.environ, {'ATELIER_AGY_CMD': json.dumps([sys.executable, str(fake)]), 'FAKE_OUTPUT': '{"response":"done","denied_actions":["command"]}'}):
                result = runners.run_external(runners.launch_spec('worker', {'runner': 'agy'}, root / 'prompt', root / 'deny', root), 10)
                self.assertIsNotNone(result['unusable_reason'])
                self.assertEqual(result['tokens'], 'unknown')

    def test_registered_cli_path_used_outside_path_and_env_wins(self):
        fake = Path(__file__).parent / 'fakes' / 'cli.py'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / 'prompt.txt'
            prompt.write_text('work', encoding='utf-8')
            tool_dir = root / 'outside_path'
            tool_dir.mkdir()
            if os.name == 'nt':
                cli_path = tool_dir / 'my-codex.cmd'
                cli_path.write_text(f'@echo off\n"{sys.executable}" "{fake}" %*\n', encoding='utf-8')
            else:
                cli_path = tool_dir / 'my-codex'
                cli_path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n', encoding='utf-8')
                cli_path.chmod(0o755)
            agents_file = root / 'agents.json'
            agents_file.write_text(json.dumps({
                'runners': {
                    'agent': {'enabled': True},
                    'codex': {'enabled': True, 'path': str(cli_path)},
                }
            }), encoding='utf-8')
            clean_env = {k: v for k, v in os.environ.items() if k != 'ATELIER_CODEX_CMD'}
            clean_env['PATH'] = ''
            with patch.dict(os.environ, clean_env, clear=True):
                spec = runners.launch_spec('worker', {'runner': 'codex', 'model': 'test'}, prompt, root / 'r1.json', root, agents_path=agents_file)
                self.assertEqual(spec[0], str(cli_path))
                res = runners.run_external(spec, 10)
                self.assertIsNone(res['unusable_reason'])
                self.assertEqual(res['tokens'], {'input': 7, 'output': 3, 'total': 10})

            # Default ~/.config/atelier/agents.json lookup when agents_path is omitted
            fake_home = root / 'home'
            default_agents = fake_home / '.config' / 'atelier' / 'agents.json'
            default_agents.parent.mkdir(parents=True)
            default_agents.write_text(agents_file.read_text(encoding='utf-8'), encoding='utf-8')
            with patch.dict(os.environ, clean_env, clear=True), patch('atelier.runners.Path.home', return_value=fake_home):
                spec_default = runners.launch_spec('worker', {'runner': 'codex', 'model': 'test'}, prompt, root / 'r2.json', root)
                self.assertEqual(spec_default[0], str(cli_path))

            # ATELIER_<RUNNER>_CMD still wins over agents.json
            with patch.dict(os.environ, {**clean_env, 'ATELIER_CODEX_CMD': json.dumps([sys.executable, str(fake)])}, clear=True):
                spec_env = runners.launch_spec('worker', {'runner': 'codex', 'model': 'test'}, prompt, root / 'r3.json', root, agents_path=agents_file)
                self.assertEqual(spec_env[:2], [sys.executable, str(fake)])


if __name__ == '__main__':
    unittest.main()
