"""events.py と harness.schema.json の振る舞い (atelier.event/1)。"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import agent_stats
import events

HERE = Path(__file__).resolve().parent
EVENTS_PY = HERE / 'events.py'
SCHEMA_PATH = HERE / 'harness.schema.json'
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

SAMPLES = {
    'run.started': {
        'workflow': 'jev-routed', 'launch': 'agent',
        'roles_from': {'designer': 'home', 'tester': 'default'}},
    'run.ended': {
        'result': 'finished', 'episodes': 5, 'waves': 2, 'pbis': 9, 'seconds': 1234.5},
    'episode.started': {
        'episode': 'build', 'wave': 1, 'launcher': 'agent', 'designer': 'installed',
        'roles': {'worker': {'model': 'opus', 'effort': 'high', 'from': 'repo'}}},
    'episode.ended': {
        'episode': 'build', 'status': 'done', 'reason': 'other', 'seconds': 12.5,
        'failures': 0, 'next_episode': 'test'},
    'episode.cost': {
        'episode': 'build', 'method': 'transcript',
        'roles': {'designer': {'requests': 3, 'input': 10, 'output': 20,
                               'cache_read': 30, 'cache_write': 40, 'usd': 0.12}}},
    'stage.decided': {
        'component': 'map.omm', 'decision': 'cache', 'by': 'user', 'reason': 'when-true'},
    'pbi.attempted': {
        'attempt': 1, 'step': 2, 'rework': 0, 'runner': 'claude', 'model': 'opus',
        'tier': 'opus', 'effort': 'high', 'outcome': 'pass', 'causes': ['grader', 'spec'],
        'findings': {'critical': 0, 'major': 1, 'minor': 2}, 'unchanged': False,
        'seconds': 10.5, 'input': 1, 'output': 2, 'cache_read': 3, 'cache_write': 4,
        'usd': 0.5, 'case_id': 'repo:W-1:PBI-1',
        'signature': {
            'sig_id': 'abc123def456',
            'parts': {'outcome': 'pass', 'causes': ['grader', 'spec'],
                      'verify_failed_n': '2-3',
                      'findings': {'critical': '0', 'major': '1', 'minor': '2-up'},
                      'unchanged': False, 'memory_violation': False,
                      'test_failing': ['intended'], 'fallback_any': False,
                      'runner': 'claude', 'tier': 'opus'}}},
    'review.judged': {
        'report': 'PBI-1.rework-1', 'recommendation': 'rework',
        'findings': {'critical': 1, 'major': 2, 'minor': 3},
        'criteria': {'pass': 3, 'fail': 1, 'unknown': 0}},
    'test.judged': {
        'kind': 'wave', 'passed': 10, 'failed': 2,
        'failing': {'intended': 1, 'regression': 1, 'unknown': 0}},
    'question.asked': {'kind': 'decision', 'topic': 'omm', 'items': 2},
    'question.answered': {
        'kind': 'approve', 'topic': 'approve-ask', 'choice': 'b', 'wait_s': 30,
        'revise': False},
    'task.profiled': {
        'scope': 'pbi', 'taxonomy_version': 'tax-1', 'task_type': 'feature',
        'domain': 'cli', 'difficulty_hint': 'mid',
        'size': {'files': '2-3', 'lines': '4-8', 'pbis': '1', 'waves': '1',
                 'input_chars': '9-20'},
        'spread': {'dirs': '1', 'languages': '1', 'tests_touched': True},
        'context': {'repo_files': '21-up', 'rules_lines': '4-8', 'refs': '2-3'},
        'languages': ['python'], 'frameworks': ['pytest'],
        'anchors': [{'anchor_id': 'a1', 'score': 0.87}],
        'case_id': 'repo:W-1:PBI-1'},
    'context.compacted': {
        'boundary': 'phase', 'role': 'designer', 'phase': 'build', 'regime': 'EoCC',
        'tokens_before': 55000, 'action': 'compact'},
    'split.assigned': {
        'case_id': 'repo:W-1:PBI-1', 'split': 'train',
        'split_set_id': 'default-1234abcd', 'reason': 'seeded'},
    'split.retired': {
        'case_id': 'repo:W-1:PBI-1', 'split': 'regression',
        'split_set_id': 'default-1234abcd', 'reason': 'read-heldout'},
    'change.applied': {
        'change_id': 'ch-1', 'parent': None, 'kind': 'model', 'target': 'roles',
        'hypothesis': 'lin-1', 'multi': False},
    'change.checked': {
        'change_id': 'ch-1', 'verdict': 'warn', 'overlap_bucket': 'low',
        'vs': ['heldout', 'failure']},
    'round.judged': {
        'change_id': 'ch-1', 'base_change_id': 'ch-0', 'verdict': 'accept',
        'd_train': 'up2', 'd_heldout': 'up', 'ci_lo': 0.01, 'ci_hi': 0.12, 'trials': 3},
    'triage.labeled': {'lineage_id': 'lin-1', 'root_cause': 'harness', 'by': 'human'},
    'cluster.assigned': {
        'unit_id': 'at:r1:PBI-1:1', 'clusterset_id': 'cs-1', 'cluster_id': 'c-3',
        'lineage_id': None, 'dist_bucket': 'near', 'how': 'nearest'},
}

SENDER_SOURCE = '''"""fake sender: 受け取った引数を記録するだけ"""
import json
CALLS = __CALLS__


def _record(row):
    with open(CALLS, 'a', encoding='utf-8') as h:
        h.write(json.dumps(row, ensure_ascii=False, default=str) + '\\n')


def emit(**kw):
    _record({'call': 'emit',
             'schema_path': str(kw.get('schema_path')),
             'type': kw.get('type'),
             'data': kw.get('data'),
             'run_id': kw.get('run_id'),
             'seq': kw.get('seq'),
             'stage': kw.get('stage'),
             'pbi': kw.get('pbi'),
             'skill_version': kw.get('skill_version'),
             'envelope': kw.get('envelope')})


def flush(**kw):
    _record({'call': 'flush'})
__EXTRA__
'''


class EventsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.dir = self.tmp / 'pbi'
        self.dir.mkdir()
        self.home = self.tmp / 'home'
        self.home.mkdir()
        self.calls = self.tmp / 'calls.jsonl'
        self.empty_sender = self.tmp / 'empty'
        self.empty_sender.mkdir()
        self._env = dict(os.environ)
        os.environ['HARNESS_LOG_CONFIG'] = str(self.tmp / 'no-config.json')
        os.environ['HARNESS_LOG_PATH'] = str(self.empty_sender)
        os.environ['HOME'] = str(self.home)
        os.environ['USERPROFILE'] = str(self.home)
        os.environ.pop('HARNESS_TRIAL', None)
        events._reset_caches()

    def tearDown(self):
        events._reset_caches()
        for name in ('changes', 'splits'):
            sys.modules.pop(name, None)
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers ----

    def rows(self, base=None):
        path = Path(base or self.dir) / 'events.jsonl'
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]

    def write_run(self, run_id='r1', seq=3, extra=None, base=None):
        folder = Path(base or self.dir) / 'conductor'
        folder.mkdir(parents=True, exist_ok=True)
        data = {'run_id': run_id, 'seq': seq}
        if extra:
            data.update(extra)
        (folder / 'run.json').write_text(json.dumps(data), encoding='utf-8')

    def make_sender(self, extra=''):
        folder = self.tmp / 'sender'
        folder.mkdir(exist_ok=True)
        source = SENDER_SOURCE.replace('__CALLS__', repr(str(self.calls))).replace('__EXTRA__', extra)
        (folder / 'harness_log.py').write_text(source, encoding='utf-8')
        os.environ['HARNESS_LOG_PATH'] = str(folder)
        events._reset_caches()
        return folder

    def sender_calls(self):
        if not self.calls.is_file():
            return []
        return [json.loads(line) for line in self.calls.read_text(encoding='utf-8').splitlines() if line.strip()]

    def run_cli(self, *argv):
        env = dict(os.environ)
        env.pop('PYTHONPATH', None)
        return subprocess.run([sys.executable, str(EVENTS_PY), *argv], capture_output=True, text=True,
                              encoding='utf-8', errors='replace', env=env, cwd=str(self.tmp),
                              creationflags=NO_WINDOW)

    def isolated_copy(self):
        folder = self.tmp / 'isolated'
        folder.mkdir(exist_ok=True)
        shutil.copy(EVENTS_PY, folder / 'events.py')
        shutil.copy(SCHEMA_PATH, folder / 'harness.schema.json')
        return folder

    # ---- 手元の行 ----

    def test_line_has_run_keys_and_stage_and_pbi(self):
        self.write_run('r1', 3)
        self.assertTrue(events.emit(self.dir, 'run.started', SAMPLES['run.started'],
                                    stage='build', pbi='PBI-2'))
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['schema'], 'atelier.event/1')
        self.assertEqual(row['run_id'], 'r1')
        self.assertEqual(row['seq'], 3)
        self.assertEqual(row['stage'], 'build')
        self.assertEqual(row['pbi'], 'PBI-2')
        self.assertEqual(row['type'], 'run.started')
        self.assertEqual(row['data'], SAMPLES['run.started'])
        self.assertNotIn('dropped', row)
        self.assertRegex(row['ts'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')

    def test_missing_run_json_gives_null_keys(self):
        events.emit(self.dir, 'question.asked', SAMPLES['question.asked'])
        row = self.rows()[0]
        self.assertIsNone(row['run_id'])
        self.assertIsNone(row['seq'])

    def test_broken_run_json_gives_null_keys(self):
        folder = self.dir / 'conductor'
        folder.mkdir()
        (folder / 'run.json').write_text('{not json', encoding='utf-8')
        events.emit(self.dir, 'question.asked', SAMPLES['question.asked'])
        row = self.rows()[0]
        self.assertIsNone(row['run_id'])
        self.assertIsNone(row['seq'])

    def test_missing_dir_writes_nothing(self):
        missing = self.tmp / 'nope'
        self.assertFalse(events.emit(missing, 'question.asked', SAMPLES['question.asked']))
        self.assertFalse(missing.exists())

    def test_unknown_type_is_not_written(self):
        self.assertFalse(events.emit(self.dir, 'nope.happened', {'x': 1}))
        self.assertFalse((self.dir / 'events.jsonl').exists())

    def test_dropped_fields_keep_only_their_names(self):
        data = {'workflow': 'x' * 65, 'launch': 'agent',
                'roles_from': {'designer': 'home', 'x y': 'home'},
                'note': 'free text', 'extra': 1}
        events.emit(self.dir, 'run.started', data)
        row = self.rows()[0]
        self.assertEqual(row['data'], {'launch': 'agent', 'roles_from': {'designer': 'home'}})
        self.assertIn('workflow', row['dropped'])
        self.assertIn('note', row['dropped'])
        self.assertIn('extra', row['dropped'])
        self.assertIn('roles_from.*', row['dropped'])
        text = (self.dir / 'events.jsonl').read_text(encoding='utf-8')
        self.assertNotIn('free text', text)
        self.assertNotIn('x' * 65, text)

    def test_bad_enum_and_nested_value_are_dropped(self):
        events.emit(self.dir, 'episode.started', {
            'episode': 'build', 'wave': True, 'launcher': 'desktop',
            'designer': 'installed', 'roles': {'worker': {'model': 'opus', 'effort': 'high'}}})
        row = self.rows()[0]
        self.assertNotIn('wave', row['data'])
        self.assertNotIn('launcher', row['data'])
        self.assertIn('wave', row['dropped'])
        self.assertIn('launcher', row['dropped'])
        self.assertEqual(row['data']['roles'], {'worker': {'model': 'opus', 'effort': 'high'}})

    def test_same_once_is_written_only_once(self):
        data = SAMPLES['question.asked']
        self.assertTrue(events.emit(self.dir, 'question.asked', data, once='k1'))
        self.assertFalse(events.emit(self.dir, 'question.asked', data, once='k1'))
        self.assertEqual(len(self.rows()), 1)
        self.assertTrue(events.emit(self.dir, 'question.asked', data, once='k2'))
        self.assertEqual(len(self.rows()), 2)

    def test_once_is_not_consumed_when_nothing_was_written(self):
        self.assertFalse(events.emit(self.dir, 'nope.happened', {}, once='k3'))
        self.assertTrue(events.emit(self.dir, 'question.asked', SAMPLES['question.asked'], once='k3'))
        self.assertEqual(len(self.rows()), 1)

    def test_every_sample_passes_the_allowlist(self):
        for type_, data in SAMPLES.items():
            with self.subTest(type=type_):
                base = self.tmp / type_.replace('.', '_')
                base.mkdir()
                self.assertTrue(events.emit(base, type_, data))
                row = self.rows(base)[0]
                self.assertNotIn('dropped', row)
                self.assertEqual(row['data'], data)

    def test_round_verdict_and_signature_note_are_dropped(self):
        base = self.tmp / 'hill'
        base.mkdir()
        bad = dict(SAMPLES['round.judged'])
        bad['verdict'] = 'great'
        events.emit(base, 'round.judged', bad)
        row = self.rows(base)[0]
        self.assertNotIn('verdict', row['data'])
        self.assertIn('verdict', row['dropped'])

        attempt = dict(SAMPLES['pbi.attempted'])
        parts = dict(attempt['signature']['parts'])
        parts['note'] = 'free text'
        attempt['signature'] = dict(attempt['signature'], parts=parts)
        events.emit(base, 'pbi.attempted', attempt)
        row = self.rows(base)[1]
        self.assertEqual(row['data']['signature']['parts']['tier'], 'opus')
        self.assertNotIn('note', row['data']['signature']['parts'])
        self.assertIn('signature.parts.note', row['dropped'])

    def test_hash_value_over_64_characters_survives_check(self):
        case_id = 'atelier-metrics:atelier-metrics:feature/harness-metrics:W-1:PBI-12'
        self.assertGreater(len(case_id), 64)
        schema = events._load_schema(SCHEMA_PATH)
        clean, dropped = events.check(schema, 'pbi.attempted',
                                      {'attempt': 1, 'case_id': case_id})
        self.assertEqual(dropped, [])
        self.assertEqual(clean, {'attempt': 1, 'case_id': case_id})
        clean, dropped = events.check(schema, 'pbi.attempted', {'attempt': 1, 'case_id': 'x\n'})
        self.assertEqual(clean, {'attempt': 1})
        self.assertEqual(dropped, ['case_id'])

    def test_long_case_id_is_written_and_sent(self):
        self.make_sender()
        case_id = 'atelier-metrics:atelier-metrics:feature/harness-metrics:W-1:PBI-12'
        data = dict(SAMPLES['pbi.attempted'], case_id=case_id)
        self.assertTrue(events.emit(self.dir, 'pbi.attempted', data))
        row = self.rows()[0]
        self.assertEqual(row['data']['case_id'], case_id)
        self.assertNotIn('dropped', row)
        self.assertEqual(self.sender_calls()[0]['data']['case_id'], case_id)

    # ---- 送り手 ----

    def test_sender_receives_the_call(self):
        self.make_sender()
        self.write_run('r1', 3)
        events.emit(self.dir, 'stage.decided', SAMPLES['stage.decided'],
                    stage='build', pbi='PBI-2')
        calls = self.sender_calls()
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call['call'], 'emit')
        self.assertTrue(call['schema_path'].endswith('harness.schema.json'))
        self.assertEqual(call['type'], 'stage.decided')
        self.assertEqual(call['data'], SAMPLES['stage.decided'])
        self.assertEqual(call['run_id'], 'r1')
        self.assertEqual(call['seq'], 3)
        self.assertEqual(call['stage'], 'build')
        self.assertEqual(call['pbi'], 'PBI-2')

    def test_sender_failure_keeps_the_line_and_is_silent(self):
        cases = (
            ('raise', '\ndef emit(**kw):\n    raise RuntimeError("boom")\n', 1),
            ('print', '\ndef emit(**kw):\n    print("noise")\n', 0),
            ('exit', '\ndef emit(**kw):\n    raise SystemExit(3)\n', 1),
            ('load', '\nraise RuntimeError("load boom")\n', 1),
        )
        for name, extra, expected_errors in cases:
            with self.subTest(name=name):
                if self.calls.exists():
                    self.calls.unlink()
                shutil.rmtree(self.dir / '.events', ignore_errors=True)
                self.make_sender(extra)
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    self.assertTrue(events.emit(self.dir, 'question.asked', SAMPLES['question.asked']))
                self.assertEqual(out.getvalue(), '')
                self.assertEqual(err.getvalue(), '')
                log = self.dir / '.events' / 'errors.log'
                errors = log.read_text(encoding='utf-8').splitlines() if log.is_file() else []
                self.assertEqual(len(errors), expected_errors)
                if errors:
                    self.assertNotIn('boom', errors[0])

    def test_sender_is_not_taken_from_beside_when_env_points_nowhere(self):
        os.environ['HARNESS_LOG_PATH'] = str(self.empty_sender)
        events._reset_caches()
        self.assertIsNone(events._sender_path())
        self.assertTrue(events.emit(self.dir, 'question.asked', SAMPLES['question.asked']))
        self.assertEqual(len(self.rows()), 1)

    # ---- 外枠 ----

    def test_envelope_top_level_and_bad_value(self):
        self.make_sender()
        events.emit(self.dir, 'question.asked', SAMPLES['question.asked'],
                    envelope={'unit_id': 'at:r1:PBI-1:1', 'split': 'poem'})
        row = self.rows()[0]
        self.assertEqual(row['unit_id'], 'at:r1:PBI-1:1')
        self.assertNotIn('split', row)
        self.assertIn('envelope.split', row['dropped'])
        call = self.sender_calls()[0]
        # config_fp / change_id are added by changes.current_envelope (PBI-13); compare the rest.
        sent = {k: v for k, v in call['envelope'].items() if k not in ('config_fp', 'change_id')}
        self.assertEqual(sent, {'unit_id': 'at:r1:PBI-1:1'})

    def test_id_values_with_a_trailing_newline_are_dropped(self):
        value = 'a' * 64 + '\n'
        events.emit(self.dir, 'stage.decided',
                    {'component': value, 'decision': 'run', 'by': 'user'},
                    envelope={'unit_id': 'at:r1:PBI-1:1\n'})
        row = self.rows()[0]
        self.assertNotIn('component', row['data'])
        self.assertIn('component', row['dropped'])
        self.assertNotIn('unit_id', row)
        self.assertIn('envelope.unit_id', row['dropped'])

    def test_harness_trial_is_added(self):
        os.environ['HARNESS_TRIAL'] = '3'
        events.emit(self.dir, 'question.asked', SAMPLES['question.asked'])
        row = self.rows()[0]
        self.assertEqual(row['trial'], 3)

    def test_changes_envelope_is_added(self):
        fake = types.ModuleType('changes')
        fake.current_envelope = lambda base: {'change_id': 'ch-x', 'config_fp': 'abc'}
        with mock.patch.dict(sys.modules, {'changes': fake}):
            events.emit(self.dir, 'question.asked', SAMPLES['question.asked'])
        row = self.rows()[0]
        self.assertEqual(row['change_id'], 'ch-x')
        self.assertEqual(row['config_fp'], 'abc')

    def test_emit_writes_when_changes_cannot_be_imported(self):
        with mock.patch.dict(sys.modules, {'changes': None}):
            self.assertTrue(events.emit(self.dir, 'question.asked', SAMPLES['question.asked']))
        row = self.rows()[0]
        self.assertNotIn('change_id', row)

    def test_explicit_envelope_wins_over_auto(self):
        fake = types.ModuleType('changes')
        fake.current_envelope = lambda base: {'change_id': 'ch-auto', 'config_fp': 'abc'}
        with mock.patch.dict(sys.modules, {'changes': fake}):
            events.emit(self.dir, 'question.asked', SAMPLES['question.asked'],
                        envelope={'change_id': 'ch-explicit'})
        row = self.rows()[0]
        self.assertEqual(row['change_id'], 'ch-explicit')
        self.assertEqual(row['config_fp'], 'abc')

    # ---- CLI ----

    def test_cli_stage_writes_the_row(self):
        self.assertEqual(events.main(['stage', '--dir', str(self.dir), '--component', 'map.omm',
                                      '--decision', 'cache', '--by', 'user', '--reason', 'when-true',
                                      '--stage', 'build', '--pbi', 'PBI-2']), 0)
        row = self.rows()[0]
        self.assertEqual(row['type'], 'stage.decided')
        self.assertEqual(row['data']['decision'], 'cache')
        self.assertEqual(row['data']['reason'], 'when-true')
        self.assertEqual(row['stage'], 'build')
        self.assertEqual(row['pbi'], 'PBI-2')

    def test_cli_stage_rejects_unknown_vocabulary(self):
        for argv in (
                ['stage', '--dir', str(self.dir), '--component', 'c', '--decision', 'poem', '--by', 'user'],
                ['stage', '--dir', str(self.dir), '--component', 'c', '--decision', 'cache', '--by', 'poet'],
                ['stage', '--dir', str(self.dir), '--component', 'c', '--decision', 'cache',
                 '--by', 'user', '--reason', 'because'],
                ['stage', '--dir', str(self.dir), '--component', 'a b', '--decision', 'cache', '--by', 'user']):
            with self.subTest(argv=argv):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertEqual(events.main(argv), 2)
                self.assertEqual(len(err.getvalue().strip().splitlines()), 1)
        self.assertFalse((self.dir / 'events.jsonl').exists())

    def test_cli_answer_writes_the_row_and_rejects_bad_values(self):
        self.assertEqual(events.main(['answer', '--dir', str(self.dir), '--kind', 'approve',
                                      '--topic', 'approve-ask', '--choice', 'b',
                                      '--wait-s', '5', '--revise']), 0)
        row = self.rows()[0]
        self.assertEqual(row['type'], 'question.answered')
        self.assertEqual(row['data'], {'kind': 'approve', 'topic': 'approve-ask', 'choice': 'b',
                                       'wait_s': 5, 'revise': True})
        for argv in (
                ['answer', '--dir', str(self.dir), '--kind', 'maybe', '--topic', 'omm'],
                ['answer', '--dir', str(self.dir), '--kind', 'decision', '--topic', 'poem'],
                ['answer', '--dir', str(self.dir), '--kind', 'decision', '--topic', 'omm', '--choice', 'A']):
            with self.subTest(argv=argv):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertEqual(events.main(argv), 2)
                self.assertTrue(err.getvalue().strip())
        self.assertEqual(len(self.rows()), 1)

    def test_cli_answer_choice_may_be_missing(self):
        self.assertEqual(events.main(['answer', '--dir', str(self.dir), '--kind', 'approve',
                                      '--topic', 'approve-ask']), 0)
        self.assertIsNone(self.rows()[0]['data']['choice'])

    def test_cli_process_exit_code_is_two_for_bad_decision(self):
        proc = self.run_cli('stage', '--dir', str(self.dir), '--component', 'c',
                            '--decision', 'poem', '--by', 'user')
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, '')
        self.assertEqual(len(proc.stderr.strip().splitlines()), 1)

    def test_delegated_commands_without_the_module_exit_two(self):
        folder = self.isolated_copy()
        for argv in (['split', 'init'], ['change', 'apply'], ['profile', '--dir', '.'],
                     ['backfill', '--dir', '.'], ['ingest', '--dir', '.'], ['leakcheck', '--dir', '.'],
                     ['failure-embed', '--dir', '.']):
            with self.subTest(argv=argv):
                env = dict(os.environ)
                env.pop('PYTHONPATH', None)
                proc = subprocess.run([sys.executable, str(folder / 'events.py'), *argv],
                                      capture_output=True, text=True, encoding='utf-8',
                                      errors='replace', env=env, cwd=str(folder),
                                      creationflags=NO_WINDOW)
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(proc.stdout, '')
                self.assertEqual(len(proc.stderr.strip().splitlines()), 1)

    def test_leakcheck_crash_exits_two_with_one_stderr_line(self):
        import leakcheck
        cases = (
            (MemoryError('x'), 2, 'events: leakcheck failed: MemoryError'),
            (SystemExit(3), 3, ''),
            (SystemExit(None), 2, 'events: leakcheck failed: SystemExit'),
            (SystemExit('stop'), 2, 'events: leakcheck failed: SystemExit'),
        )
        for exc, code, line in cases:
            with self.subTest(exc=repr(exc)):
                with mock.patch.object(leakcheck, 'main',
                                       lambda argv, e=exc: (_ for _ in ()).throw(e)):
                    err = io.StringIO()
                    with contextlib.redirect_stderr(err):
                        rc = events.main(['leakcheck', '--dir', str(self.dir)])
                self.assertEqual(rc, code)
                self.assertEqual(err.getvalue().strip(), line)

    def test_other_delegate_crash_still_exits_zero(self):
        import ingest
        with mock.patch.object(ingest, 'main',
                               lambda argv: (_ for _ in ()).throw(MemoryError('x'))):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = events.main(['ingest', '--dir', str(self.dir)])
        self.assertEqual(rc, 0)
        self.assertEqual(err.getvalue(), '')

    # ---- settle ----

    def test_settle_without_episode_cost_is_unmeasured(self):
        with mock.patch.object(agent_stats, 'episode_cost', None, create=True):
            self.assertEqual(events.main(['settle', '--dir', str(self.dir), '--seq', '3']), 0)
        row = self.rows()[0]
        self.assertEqual(row['type'], 'episode.cost')
        self.assertEqual(row['data']['method'], 'unmeasured')
        self.assertEqual(row['data']['roles'], {})
        self.assertEqual(row['seq'], 3)

    def test_settle_uses_episode_cost_and_flushes(self):
        self.make_sender()
        self.write_run('r1', 4, {'running': {'seq': 4, 'episode': 'test'},
                                 'history': [{'seq': 3, 'episode': 'design'}]})
        cost = {'method': 'transcript',
                'roles': {'designer': {'requests': 1, 'input': 2, 'output': 3,
                                       'cache_read': 4, 'cache_write': 5, 'usd': 0.1}}}
        with mock.patch.object(agent_stats, 'episode_cost', lambda run, seq: cost, create=True):
            self.assertEqual(events.main(['settle', '--dir', str(self.dir), '--seq', '3']), 0)
        row = self.rows()[0]
        self.assertEqual(row['seq'], 3)
        self.assertEqual(row['data']['episode'], 'design')
        self.assertEqual(row['data']['method'], 'transcript')
        self.assertEqual(row['data']['roles'], cost['roles'])
        calls = self.sender_calls()
        self.assertEqual([c['call'] for c in calls], ['emit', 'flush'])

    def test_failed_flush_keeps_pending_events_and_logs_one_line(self):
        pending = self.tmp / 'sender-spool.jsonl'
        delivered = self.tmp / 'delivered.jsonl'
        fail = self.tmp / 'fail-flag'
        fail.touch()
        self.make_sender(
            '\nPENDING = ' + repr(str(pending)) + '\n'
            'DELIVERED = ' + repr(str(delivered)) + '\n'
            'FAIL = ' + repr(str(fail)) + '\n'
            'import os\n'
            'def load_config(path=None):\n'
            "    return {'enabled': True, 'remote': 'origin', 'spool_dir': " + repr(str(self.tmp)) + '}\n'
            'def flush(**kw):\n'
            "    _record({'call': 'flush'})\n"
            '    if os.path.exists(FAIL):\n'
            '        with open(PENDING, "a", encoding="utf-8") as h:\n'
            '            h.write("pending\\n")\n'
            '        return False\n'
            '    if os.path.exists(PENDING):\n'
            '        with open(DELIVERED, "a", encoding="utf-8") as h:\n'
            '            h.write(open(PENDING, encoding="utf-8").read())\n'
            '        os.remove(PENDING)\n'
            '    return True\n')
        self.write_run('r1', 2)
        self.assertEqual(events.main(['settle', '--dir', str(self.dir), '--seq', '2']), 0)
        self.assertEqual(len(self.rows()), 1)
        self.assertTrue(pending.is_file())
        self.assertEqual(pending.read_text(encoding='utf-8').splitlines(), ['pending'])
        log = self.dir / '.events' / 'errors.log'
        self.assertEqual(len(log.read_text(encoding='utf-8').splitlines()), 1)
        # 復旧すると次の flush が未送の行を送り、errors.log は増えない。
        fail.unlink()
        self.assertEqual(events.main(['settle', '--dir', str(self.dir), '--seq', '2']), 0)
        self.assertFalse(pending.exists())
        self.assertEqual(delivered.read_text(encoding='utf-8'), 'pending\n')
        self.assertEqual(len(log.read_text(encoding='utf-8').splitlines()), 1)

    def test_flush_does_not_log_when_sending_is_off(self):
        self.make_sender(
            '\ndef load_config(path=None):\n'
            "    return {'enabled': False, 'remote': 'origin'}\n"
            'def flush(**kw):\n'
            '    return False\n')
        self.assertEqual(events.main(['settle', '--dir', str(self.dir), '--seq', '1']), 0)
        self.assertEqual(len(self.rows()), 1)
        self.assertFalse((self.dir / '.events' / 'errors.log').exists())

    # ---- skill_version ----

    def test_skill_version(self):
        changelog = self.tmp / 'CHANGELOG.md'
        changelog.write_text('# atelier CHANGELOG\n\n## Unreleased\n', encoding='utf-8')
        self.assertEqual(events.skill_version(changelog), 'unreleased')
        changelog.write_text('## Unreleased\n\n## v7.4\n', encoding='utf-8')
        self.assertEqual(events.skill_version(changelog), 'v7.4')
        changelog.write_text('## 2026-11-11T11:43:32+09:00 v7.2 created\n', encoding='utf-8')
        self.assertEqual(events.skill_version(changelog), 'v7.2')
        (self.tmp / 'ACTIVE').write_text('v7.3\n', encoding='utf-8')
        self.assertEqual(events.skill_version(changelog), 'v7.3')

    # ---- 並ぶ PBI の送り手との整合 ----

    def test_real_sender_check_accepts_every_sample(self):
        sender_path = HERE.parent / 'harness-log' / 'harness_log.py'
        if not sender_path.is_file():
            self.skipTest('harness-log is not here')
        spec = importlib.util.spec_from_file_location('pbi2_sender_check', sender_path)
        sender = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sender)
        schema = json.loads(SCHEMA_PATH.read_text(encoding='utf-8'))
        for type_, data in SAMPLES.items():
            with self.subTest(type=type_):
                clean, dropped = sender.check(schema, type_, data)
                self.assertEqual(dropped, [])
                self.assertEqual(clean, data)


if __name__ == '__main__':
    unittest.main()
