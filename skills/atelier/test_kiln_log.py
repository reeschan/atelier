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
          'soft_budget', 'handoff_at', 'action', 'state_path', 'task_id', 'note', 'atelier_run_id', 'seq',
          'tokens_source']
COMPACTED = ['boundary', 'role', 'phase', 'regime', 'tokens_before', 'action']
sys.path.insert(0, HERE)

import kiln_log  # noqa: E402

TABLET = '''# State

## Objective
Finish PBI-003.

## 2. Facts
- `flow.py` resolves the template.

## Changes
- flow.py (verified)

## 4. Verification
- python run_tests.py -p test_flow.py: OK

## Dead ends
- none

## Remaining plan
1. Run the review.
'''


def request(rid, ts, inp=0, read=0, write=0):
    """会話記録の assistant の 1 行 (文脈 = inp + read + write)。"""
    return {'type': 'assistant', 'requestId': rid, 'timestamp': ts,
            'message': {'model': 'claude-sonnet-4-5', 'usage': {
                'input_tokens': inp, 'output_tokens': 1, 'cache_read_input_tokens': read,
                'cache_creation_input_tokens': write,
                'cache_creation': {'ephemeral_5m_input_tokens': write, 'ephemeral_1h_input_tokens': 0}}}}


def write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(''.join(json.dumps(r) + '\n' for r in rows))


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
        # 会話記録は一時の CLAUDE_CONFIG_DIR/projects だけを読ませる
        self.claude = os.path.join(self.pbi, 'claude')
        self.env['CLAUDE_CONFIG_DIR'] = self.claude

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

    def test_seq_and_start_boundary_are_on_the_line(self):
        self.log_once('designer', 10)
        result = self.run_cli('--role', 'designer', '--boundary', 'start', '--phase', 'design', '--tokens', '64000',
                              '--seq', '4')
        self.assertEqual(result.returncode, 0, result.stderr)
        first, start = self.events()
        self.assertEqual((first['seq'], first['tokens_source']), (None, 'given'))
        self.assertEqual((start['boundary'], start['seq'], start['tokens_before'], start['action'],
                          start['tokens_source']), ('start', 4, 64000, 'noop', 'given'))

    def transcript_agent(self, description, rows):
        sub = os.path.join(self.claude, 'projects', 'proj', 'caller-session', 'subagents')
        write_jsonl(os.path.join(sub, 'agent-a1.jsonl'), rows)
        with open(os.path.join(sub, 'agent-a1.meta.json'), 'w', encoding='utf-8') as f:
            json.dump({'description': description}, f)

    def test_probe_agent_measures_the_context_itself(self):
        self.transcript_agent('ATELIER.abcd1234.4 design', [
            request('r1', '2026-11-15T00:00:00Z', inp=10, read=20000, write=1000),
            request('r2', '2026-11-15T00:00:05Z', inp=5, read=125000, write=2000)])
        result = self.run_cli('--role', 'designer', '--boundary', 'start', '--phase', 'design', '--seq', '4',
                              '--probe-agent', 'ATELIER.abcd1234.4 ')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith('action suggest (role designer, tokens 127005,'), result.stdout)
        event = self.events()[0]
        self.assertEqual((event['tokens_before'], event['seq'], event['tokens_source']), (127005, 4, 'probe'))

    def test_probe_session_measures_a_headless_designer(self):
        write_jsonl(os.path.join(self.claude, 'projects', 'proj', 'sess-1.jsonl'),
                    [request('r1', '2026-11-15T00:00:00Z', inp=100, read=30000)])
        result = self.run_cli('--role', 'designer', '--boundary', 'phase', '--phase', 'plan',
                              '--probe-session', 'sess-1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events()[0]['tokens_before'], 30100)

    def test_probe_without_a_transcript_is_unknown(self):
        self.write_run()
        result = self.run_cli('--role', 'designer', '--boundary', 'start', '--phase', 'design', '--seq', '2',
                              '--probe-agent', 'ATELIER.nothing.2 ')
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertTrue(result.stdout.startswith('context=unknown'), result.stdout)
        event = self.events()[0]
        self.assertEqual((event['tokens_before'], event['action'], event['seq'], event['tokens_source']),
                         (None, 'noop', 2, 'unknown'))
        # 線と比べていないので、判断の event は出さない
        self.assertEqual(self.atelier_events(), [])

    def test_tokens_and_probe_are_exclusive_and_one_is_needed(self):
        both = self.run_cli('--role', 'designer', '--boundary', 'phase', '--phase', 'plan', '--tokens', '1',
                            '--probe-session', 'sess-1')
        self.assertEqual(both.returncode, 2)
        neither = self.run_cli('--role', 'designer', '--boundary', 'phase', '--phase', 'plan')
        self.assertEqual(neither.returncode, 2)
        self.assertFalse(os.path.exists(self.log))

    def tablet(self, text, *extra):
        path = os.path.join(self.pbi, 'CONTEXT_STATE.md')
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        return subprocess.run([sys.executable, SCRIPT, 'tablet-check', path, *extra], capture_output=True, text=True,
                              encoding='utf-8', env=self.env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def test_tablet_check_accepts_the_six_headings(self):
        result = self.tablet(TABLET)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(result.stdout.startswith('ok'), result.stdout)
        # H1 は無くてもよく、行数の下限は見ない。コードの囲みの中の ## は見出しに数えない
        short = ('## 1. Objective\nx\n## Facts\n```\n## not a heading\n```\n## Changes\n## Verification\n'
                 '## Dead ends\n## Remaining plan\n')
        self.assertEqual(self.tablet(short).returncode, 0)

    def test_tablet_check_reports_problems(self):
        missing = self.tablet(TABLET.replace('## Dead ends\n- none\n\n', ''))
        self.assertEqual(missing.returncode, 1)
        self.assertIn('problem: missing ## Dead ends', missing.stdout.splitlines())
        extra = self.tablet(TABLET + '\n## Notes\nmore\n')
        self.assertEqual(extra.returncode, 1)
        self.assertIn('problem: unexpected ## Notes', extra.stdout.splitlines())
        twice = self.tablet(TABLET + '\n## Objective\nagain\n')
        self.assertIn('problem: ## Objective appears 2 times', twice.stdout.splitlines())

    def test_tablet_check_reads_the_limit_from_kiln_json(self):
        long_text = TABLET + '\n'.join(['- more facts'] * 600) + '\n'
        result = self.tablet(long_text)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f'problem: {len(long_text)} chars > 6000', result.stdout.splitlines())
        with open(os.path.join(self.pbi, 'kiln.json'), 'w', encoding='utf-8') as f:
            json.dump({'tablet_max_chars': 100}, f)
        self.assertIn(f'problem: {len(TABLET)} chars > 100', self.tablet(TABLET).stdout.splitlines())
        self.assertEqual(self.tablet(TABLET, '--max-chars', '9000').returncode, 0)

    def test_tablet_problems_for_the_conductor(self):
        path = os.path.join(self.pbi, 'tablet.md')
        self.assertEqual(kiln_log.tablet_problems(path), ['missing file'])
        with open(path, 'w', encoding='utf-8') as f:
            f.write(TABLET)
        self.assertEqual(kiln_log.tablet_problems(path), [])
        self.assertEqual(kiln_log.tablet_problems(path, max_chars=50), [f'{len(TABLET)} chars > 50'])
        self.assertEqual(kiln_log.tablet_max_chars(self.pbi), 6000)

    def test_bundled_templates_set_the_tablet_limit(self):
        for name in ('jev-routed', 'high-model'):
            with open(os.path.join(HERE, 'workflows', f'{name}.json'), encoding='utf-8') as f:
                self.assertEqual(json.load(f)['kiln']['tablet_max_chars'], 6000, name)

    def test_bad_settings_exit_2(self):
        with open(os.path.join(self.pbi, 'kiln.json'), 'w', encoding='utf-8') as f:
            json.dump({'designer_peak': 'big'}, f)
        result = self.run_cli('--role', 'designer', '--boundary', 'phase', '--phase', 'plan', '--tokens', '1')
        self.assertEqual(result.returncode, 2)
        self.assertIn('designer_peak', result.stderr)


if __name__ == '__main__':
    unittest.main()
