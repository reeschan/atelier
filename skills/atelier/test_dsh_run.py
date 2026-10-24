"""dsh_run.py (実行先 dsh のラッパー) のテスト。dsh そのものは動かさず、偽の node スクリプトで呼び出し方を確かめる。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'dsh_run.py')
sys.path.insert(0, HERE)
import dsh_run  # noqa: E402

# 偽の dsh: 受け取った引数と、キーの環境変数が入っていたか (値は書かない) を書いて、イベントを 3 行出す
FAKE_BIN = r'''
import json, os, sys
args = sys.argv[1:]
with open(os.environ['FAKE_DSH_LOG'], 'w', encoding='utf-8') as f:
    json.dump({'args': args, 'has_deepseek': bool(os.environ.get('DEEPSEEK_API_KEY')),
               'mode': os.environ.get('DSH_PERMISSION_MODE'), 'home': os.environ.get('DSH_HOME')}, f)
print(json.dumps({'type': 'session', 'sessionId': 's'}))
print(json.dumps({'type': 'status', 'usage': {'inputTokens': 1, 'outputTokens': 2}}))
print(json.dumps({'type': 'final', 'text': 'OK'}))
sys.exit(int(os.environ.get('FAKE_DSH_EXIT', '0')))
'''


class PureFunctionsTest(unittest.TestCase):
    def test_patch_yaml_with_and_without_effort(self):
        self.assertIn('    reasoningEffort: high\n', dsh_run.patch_yaml('deepseek-official', 'deepseek-flash', 'high'))
        self.assertNotIn('reasoningEffort', dsh_run.patch_yaml('deepseek-official', 'deepseek-flash', None))

    def test_final_text_takes_last_final_event(self):
        d = Path(tempfile.mkdtemp())
        try:
            p = d / 'x.jsonl'
            p.write_text('not json\n{"type":"final","text":"a"}\n{"type":"final","text":"b"}\n', encoding='utf-8')
            self.assertEqual(dsh_run.final_text(p), 'b')
            self.assertIsNone(dsh_run.final_text(d / 'missing.jsonl'))
        finally:
            shutil.rmtree(d, ignore_errors=True)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / 'repo'
        (self.repo / 'docs' / 'pbi' / 'dsh').mkdir(parents=True)
        (self.repo / 'docs' / 'pbi' / 'dsh' / 'PBI-001.prompt.md').write_text('依頼', encoding='utf-8')
        self.fake = self.tmp / 'fake_bin.py'
        self.fake.write_text(FAKE_BIN, encoding='utf-8')
        self.log = self.tmp / 'fake.log'
        self.home = self.tmp / 'home'
        (self.home / '.config' / 'deepseek').mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args, key_file=True, env_key=None, exit_code=0):
        if key_file:
            (self.home / '.config' / 'deepseek' / 'deepseek.key').write_text('﻿sk-test-value\r\n', encoding='utf-8')
        env = {k: v for k, v in os.environ.items()
               if k not in ('DEEPSEEK_API_KEY', 'OPENCODE_GO_API_KEY', 'DSH_HOME')}
        env.update(PBI_DSH_NODE=sys.executable, PBI_DSH_BIN=str(self.fake), FAKE_DSH_LOG=str(self.log),
                   FAKE_DSH_EXIT=str(exit_code), USERPROFILE=str(self.home), HOME=str(self.home))
        if env_key:
            env['DEEPSEEK_API_KEY'] = env_key
        return subprocess.run([sys.executable, SCRIPT, *args], cwd=self.repo, capture_output=True, text=True,
                              encoding='utf-8', env=env)

    def test_runs_headless_with_patch_and_json_and_writes_outputs(self):
        r = self.run_cli('PBI-001', '--model', 'deepseek-official/deepseek-flash', '--effort', 'high')
        self.assertEqual(r.returncode, 0, r.stderr)
        log = json.loads(self.log.read_text(encoding='utf-8'))
        args = log['args']
        self.assertEqual(args[:2], ['--profile', 'headless'])
        self.assertEqual(args[2], '--patch')
        self.assertTrue(args[3].endswith('PBI-001.patch.yml'))
        self.assertEqual(args[4], '--json')
        self.assertIn('docs/pbi/dsh/PBI-001.prompt.md', args[5])
        self.assertTrue(log['has_deepseek'])
        self.assertEqual(log['mode'], 'workspace-write')
        d = self.repo / 'docs' / 'pbi' / 'dsh'
        patch = (d / 'PBI-001.patch.yml').read_text(encoding='utf-8')
        self.assertIn('provider: deepseek-official', patch)
        self.assertIn('model: deepseek-flash', patch)
        self.assertIn('reasoningEffort: high', patch)
        self.assertEqual((d / 'PBI-001.final.md').read_text(encoding='utf-8'), 'OK')
        run = json.loads((d / 'PBI-001.run.json').read_text(encoding='utf-8'))
        self.assertEqual(run['exit'], 0)
        self.assertEqual(run['model'], 'deepseek-official/deepseek-flash')
        # キーの値はどこにも出さない
        for p in d.iterdir():
            self.assertNotIn('sk-test-value', p.read_text(encoding='utf-8'), p.name)
        self.assertNotIn('sk-test-value', r.stdout + r.stderr)

    def test_null_effort_omits_reasoning_effort(self):
        r = self.run_cli('PBI-001', '--model', 'deepseek-official/deepseek-flash', '--effort', 'null')
        self.assertEqual(r.returncode, 0, r.stderr)
        patch = (self.repo / 'docs' / 'pbi' / 'dsh' / 'PBI-001.patch.yml').read_text(encoding='utf-8')
        self.assertNotIn('reasoningEffort', patch)

    def test_exit_code_is_passed_through(self):
        r = self.run_cli('PBI-001', '--model', 'deepseek-official/deepseek-flash', exit_code=1)
        self.assertEqual(r.returncode, 1)
        run = json.loads((self.repo / 'docs' / 'pbi' / 'dsh' / 'PBI-001.run.json').read_text(encoding='utf-8'))
        self.assertEqual(run['exit'], 1)

    def test_missing_key_for_route_provider_stops_with_2(self):
        r = self.run_cli('PBI-001', '--model', 'deepseek-official/deepseek-flash', key_file=False)
        self.assertEqual(r.returncode, 2)
        self.assertIn('DEEPSEEK_API_KEY', r.stderr)
        self.assertFalse(self.log.exists())

    def test_env_key_is_used_without_key_file(self):
        r = self.run_cli('PBI-001', '--model', 'deepseek-official/deepseek-flash', key_file=False, env_key='sk-env')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(json.loads(self.log.read_text(encoding='utf-8'))['has_deepseek'])

    def test_rejects_values_that_could_inject_yaml(self):
        for model in ('deepseek-official', 'a/!!js process.exit()', 'a/b\nc', '/b'):
            r = self.run_cli('PBI-001', '--model', model)
            self.assertEqual(r.returncode, 2, model)
        r = self.run_cli('PBI-001', '--model', 'deepseek-official/deepseek-flash', '--effort', '!!js x')
        self.assertEqual(r.returncode, 2)
        r = self.run_cli('../evil', '--model', 'deepseek-official/deepseek-flash')
        self.assertEqual(r.returncode, 2)

    def test_without_bin_and_without_dsh_on_path_stops_with_2(self):
        (self.home / '.config' / 'deepseek' / 'deepseek.key').write_text('sk-test-value', encoding='utf-8')
        env = {k: v for k, v in os.environ.items() if k not in ('PBI_DSH_BIN', 'PBI_DSH_NODE', 'DEEPSEEK_API_KEY')}
        env.update(PATH=str(self.tmp / 'empty'), USERPROFILE=str(self.home), HOME=str(self.home))
        r = subprocess.run([sys.executable, SCRIPT, 'PBI-001', '--model', 'deepseek-official/deepseek-flash'],
                           cwd=self.repo, capture_output=True, text=True, encoding='utf-8', env=env)
        self.assertEqual(r.returncode, 2)
        self.assertIn('dsh が見つかりません', r.stderr)

    def test_missing_prompt_stops_with_2(self):
        r = self.run_cli('PBI-009', '--model', 'deepseek-official/deepseek-flash')
        self.assertEqual(r.returncode, 2)
        self.assertIn('依頼文がありません', r.stderr)


if __name__ == '__main__':
    unittest.main()
