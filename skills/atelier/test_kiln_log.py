"""kiln_log.py を CLI から動かし、線からの手立ての決め方・記録の 1 行・止めたときの動きを確かめる。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'kiln_log.py')
FIELDS = ['ts', 'run_id', 'host', 'regime', 'boundary', 'role', 'phase', 'tokens_before', 'peak_budget',
          'soft_budget', 'handoff_at', 'action', 'state_path', 'task_id', 'note', 'atelier_run_id']
COMPACTED = ['boundary', 'role', 'phase', 'regime', 'tokens_before', 'action']


class KilnLogTest(unittest.TestCase):
    def setUp(self):
        self.pbi = os.path.join(tempfile.gettempdir(), f'kiln-{uuid.uuid4().hex}')
        os.mkdir(self.pbi)
        self.addCleanup(shutil.rmtree, self.pbi, ignore_errors=True)
        self.log = os.path.join(self.pbi, '.kiln', 'compact-events.jsonl')
        # 本物の ~/.claude の設定と送り手を読ませない (無いファイル・空のフォルダ)。
        no_sender = os.path.join(self.pbi, 'no-sender')
        os.mkdir(no_sender)
        self.env = dict(os.environ)
        self.env['HARNESS_LOG_CONFIG'] = os.path.join(self.pbi, 'no-config.json')
        self.env['HARNESS_LOG_PATH'] = no_sender

    def run_cli(self, *args):
        return subprocess.run([sys.executable, SCRIPT, self.pbi, *args], capture_output=True, text=True,
                              encoding='utf-8', env=self.env,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def log_once(self, role, tokens, *extra):
        result = self.run_cli('--role', role, '--boundary', 'wave', '--phase', 'build', '--tokens', str(tokens), *extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def events(self):
        with open(self.log, encoding='utf-8') as f:
            return [json.loads(line) for line in f]

    def atelier_events(self):
        path = os.path.join(self.pbi, 'events.jsonl')
        if not os.path.exists(path):
            return []
        with open(path, encoding='utf-8') as f:
            return [json.loads(line) for line in f if line.strip()]

    def write_run(self, run_id='run-from-conductor', seq=3):
        folder = os.path.join(self.pbi, 'conductor')
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, 'run.json'), 'w', encoding='utf-8') as f:
            json.dump({'run_id': run_id, 'seq': seq}, f)

    def test_decides_the_action_from_the_bundled_lines(self):
        cases = [('designer', 100000, 'noop'), ('designer', 130000, 'suggest'), ('designer', 200000, 'compact'),
                 ('designer', 789971, 'handoff'), ('implementer', 50000, 'noop'), ('implementer', 70000, 'suggest'),
                 ('implementer', 95000, 'compact')]
        for role, tokens, action in cases:
            with self.subTest(role=role, tokens=tokens):
                self.assertTrue(self.log_once(role, tokens).startswith(f'action {action} '))
        events = self.events()
        self.assertEqual([e['action'] for e in events], [c[2] for c in cases])
        self.assertEqual(list(events[0]), FIELDS)
        self.assertEqual(len({e['run_id'] for e in events}), 1)
        self.assertEqual((events[0]['soft_budget'], events[0]['peak_budget'], events[0]['handoff_at']),
                         (120000, 180000, 250000))
        self.assertEqual((events[-1]['soft_budget'], events[-1]['peak_budget']), (60000, 90000))

    def test_run_id_restored_on_resume_is_kept(self):
        self.log_once('designer', 10, '--run-id', 'run-from-handoff')
        self.log_once('designer', 20)
        self.assertEqual([e['run_id'] for e in self.events()], ['run-from-handoff', 'run-from-handoff'])

    def test_explicit_action_and_fields(self):
        self.log_once('designer', 10, '--action', 'compact', '--task-id', 'W-005', '--note', '下書きを待つ')
        event = self.events()[0]
        self.assertEqual((event['action'], event['task_id'], event['note'], event['state_path']),
                         ('compact', 'W-005', '下書きを待つ', 'docs/pbi/CONTEXT_STATE.md'))

    def test_compacted_event_carries_the_conductor_run_id(self):
        self.write_run('run-from-conductor')
        self.log_once('designer', 130000, '--note', '下書きを待つ')
        event = self.events()[0]
        self.assertEqual(event['atelier_run_id'], 'run-from-conductor')
        self.assertNotEqual(event['run_id'], 'run-from-conductor')  # kiln の run_id は今までどおり
        rows = self.atelier_events()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['type'], 'context.compacted')
        self.assertEqual(row['run_id'], 'run-from-conductor')
        self.assertEqual(row['data'], {'boundary': 'wave', 'role': 'designer', 'phase': 'build',
                                       'regime': 'EoCC', 'tokens_before': 130000, 'action': 'suggest'})
        self.assertNotIn('note', row['data'])  # 自由文は入れない
        self.assertNotIn('dropped', row)

    def test_without_run_json_atelier_run_id_is_null(self):
        self.log_once('designer', 100000)
        event = self.events()[0]
        self.assertIsNone(event['atelier_run_id'])
        self.assertTrue(event['run_id'])  # kiln の run_id は今までどおり
        rows = self.atelier_events()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]['run_id'])

    def test_kiln_off_writes_neither_the_line_nor_the_event(self):
        self.write_run()
        with open(os.path.join(self.pbi, 'kiln.json'), 'w', encoding='utf-8') as f:
            json.dump({'enabled': False}, f)
        self.assertEqual(self.log_once('designer', 999999), 'kiln off')
        self.assertFalse(os.path.exists(self.log))
        self.assertFalse(os.path.exists(os.path.join(self.pbi, 'events.jsonl')))

    def test_run_copy_overrides_and_turns_off(self):
        with open(os.path.join(self.pbi, 'kiln.json'), 'w', encoding='utf-8') as f:
            json.dump({'designer_soft': 10, 'designer_peak': 20}, f)
        self.assertTrue(self.log_once('designer', 15).startswith('action suggest '))
        with open(os.path.join(self.pbi, 'kiln.json'), 'w', encoding='utf-8') as f:
            json.dump({'enabled': False}, f)
        self.assertEqual(self.log_once('designer', 999999), 'kiln off')
        self.assertEqual(len(self.events()), 1)

    def test_bad_settings_exit_2(self):
        with open(os.path.join(self.pbi, 'kiln.json'), 'w', encoding='utf-8') as f:
            json.dump({'designer_peak': 'big'}, f)
        result = self.run_cli('--role', 'designer', '--boundary', 'phase', '--phase', 'plan', '--tokens', '1')
        self.assertEqual(result.returncode, 2)
        self.assertIn('designer_peak', result.stderr)


if __name__ == '__main__':
    unittest.main()
