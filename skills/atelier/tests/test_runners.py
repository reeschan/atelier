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


if __name__ == '__main__':
    unittest.main()
