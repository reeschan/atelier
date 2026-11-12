"""changes.py のテスト (config_fp・change apply・current_envelope)。"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import changes
import events

PART_NAMES = {'skill_version', 'worker', 'designer', 'roles', 'workflow', 'routing'}


def make_temp():
    """一時フォルダ。tempfile.mkdtemp は 0o700 を渡し、DSH のサンドボックスでは中に書けない。"""
    path = Path(tempfile.gettempdir()) / f'pbi13-changes-{os.getpid()}-{uuid.uuid4().hex}'
    os.mkdir(path)
    return path


class ChangesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp()
        self.dir = self.tmp / 'docs' / 'pbi'
        self.dir.mkdir(parents=True)
        (self.dir / 'worker.md').write_text('# worker\n', encoding='utf-8')
        self.write_run()
        self.archive = self.tmp / 'archive'
        (self.tmp / 'home').mkdir()
        sender = self.tmp / 'no-sender'
        sender.mkdir()
        self._env = dict(os.environ)
        os.environ['HARNESS_LOG_PATH'] = str(sender)
        os.environ['HARNESS_LOG_CONFIG'] = str(self.tmp / 'no-config.json')
        os.environ['PBI_RUNS_ARCHIVE'] = str(self.archive)
        os.environ['HOME'] = str(self.tmp / 'home')
        os.environ['USERPROFILE'] = str(self.tmp / 'home')
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

    def write_run(self, workflow='default', roles=None):
        folder = self.dir / 'conductor'
        folder.mkdir(parents=True, exist_ok=True)
        data = {'schema': 'atelier.run/1', 'run_id': 'r1', 'seq': 1}
        if workflow is not None:
            data['workflow'] = workflow
        if roles is not None:
            data['roles'] = roles
        (folder / 'run.json').write_text(json.dumps(data), encoding='utf-8')

    def change_rows(self):
        path = self.archive / 'changes.jsonl'
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]

    def event_rows(self):
        path = self.dir / 'events.jsonl'
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]

    # ---- config_parts / config_fp ----

    def test_parts_have_six_keys_and_fp_is_twelve_hex(self):
        parts = changes.config_parts(self.dir)
        self.assertEqual(set(parts), PART_NAMES)
        for name, value in parts.items():
            self.assertIsInstance(value, str, name)
        fp = changes.config_fp(self.dir)
        self.assertRegex(fp, r'^[0-9a-f]{12}$')
        self.assertRegex(parts['worker'], r'^[0-9a-f]{12}$')

    def test_worker_change_moves_only_worker_part_and_fp(self):
        before_parts = changes.config_parts(self.dir)
        before_fp = changes.config_fp(self.dir)
        (self.dir / 'worker.md').write_text('# worker!\n', encoding='utf-8')
        after_parts = changes.config_parts(self.dir)
        after_fp = changes.config_fp(self.dir)
        self.assertNotEqual(before_parts['worker'], after_parts['worker'])
        self.assertNotEqual(before_fp, after_fp)
        for name in PART_NAMES - {'worker'}:
            self.assertEqual(before_parts[name], after_parts[name], name)

    def test_roles_and_workflow_parts_come_from_run_json(self):
        before = changes.config_parts(self.dir)
        self.assertEqual(before['workflow'], 'default')
        self.assertNotEqual(before['roles'], 'none')
        self.write_run(workflow='jev-routed',
                       roles={'reviewer': {'deep': {'model': 'fable'}}})
        after = changes.config_parts(self.dir)
        self.assertEqual(after['workflow'], 'jev-routed')
        self.assertNotEqual(before['roles'], after['roles'])

    def test_repo_roles_model_change_moves_config_fp(self):
        """run.json に roles が無くても、repo の roles.json の model の変更が config_fp に映る (rework 1)。"""
        roles_path = self.dir / 'roles.json'
        roles_path.write_text(json.dumps({'roles': {'reviewer': {'deep': {'model': 'sonnet'}}}}),
                              encoding='utf-8')
        before_parts = changes.config_parts(self.dir)
        before = changes.config_fp(self.dir)
        roles_path.write_text(json.dumps({'roles': {'reviewer': {'deep': {'model': 'opus'}}}}),
                              encoding='utf-8')
        after_parts = changes.config_parts(self.dir)
        self.assertNotEqual(before_parts['roles'], after_parts['roles'])
        self.assertNotEqual(before, changes.config_fp(self.dir))

    def test_workflow_falls_back_to_status_md(self):
        self.write_run(workflow=None)
        (self.dir / 'STATUS.md').write_text('State: build\nWorkflow: jev-routed (x)\n',
                                            encoding='utf-8')
        self.assertEqual(changes.config_parts(self.dir)['workflow'], 'jev-routed')

    def test_designer_part_includes_appendix(self):
        skill = self.tmp / 'skill'
        (skill / 'agents').mkdir(parents=True)
        (skill / 'appendix').mkdir()
        (skill / 'designer.md').write_text('designer', encoding='utf-8')
        (skill / 'agents' / 'a.md').write_text('agent', encoding='utf-8')
        (skill / 'appendix' / 'x.md').write_text('one', encoding='utf-8')
        (skill / 'routing.default.json').write_text('{}', encoding='utf-8')
        with mock.patch.object(changes, 'HERE', skill):
            before = changes.config_parts(self.dir)['designer']
            (skill / 'appendix' / 'x.md').write_text('two', encoding='utf-8')
            after = changes.config_parts(self.dir)['designer']
        self.assertNotEqual(before, after)

    def test_kinds_match_the_schema_and_the_part_table(self):
        schema = json.loads((HERE / 'harness.schema.json').read_text(encoding='utf-8'))
        values = schema['types']['change.applied']['fields']['kind']['values']
        self.assertEqual(list(changes.KINDS), values)
        self.assertEqual(set(changes.KINDS),
                         {kind for kinds in changes.PART_KINDS.values() for kind in kinds})

    # ---- apply ----

    def test_apply_writes_one_row_and_change_applied(self):
        row = changes.apply(self.dir, 'worker-prompt', 'worker.md', 'L1')
        rows = self.change_rows()
        self.assertEqual(len(rows), 1)
        saved = rows[0]
        self.assertEqual(saved['schema'], 'harness.change/1')
        self.assertEqual(saved['change_id'], row['change_id'])
        self.assertIsNone(saved['parent'])
        self.assertEqual(saved['kind'], 'worker-prompt')
        self.assertEqual(saved['target'], 'worker.md')
        self.assertEqual(saved['hypothesis'], 'L1')
        self.assertEqual(saved['config_fp'], changes.config_fp(self.dir))
        self.assertEqual(set(saved['config_parts']), PART_NAMES)
        self.assertIs(saved['multi'], False)
        applied = [r for r in self.event_rows() if r['type'] == 'change.applied']
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]['data']['change_id'], row['change_id'])
        self.assertIsNone(applied[0]['data']['parent'])
        self.assertEqual(applied[0]['data']['kind'], 'worker-prompt')
        self.assertEqual(applied[0]['data']['target'], 'worker.md')
        self.assertEqual(applied[0]['data']['hypothesis'], 'L1')
        self.assertIs(applied[0]['data']['multi'], False)
        self.assertEqual(applied[0]['change_id'], row['change_id'])

    def test_second_apply_parent_is_the_first_change_id(self):
        first = changes.apply(self.dir, 'worker-prompt', 'worker.md', 'L1')
        (self.dir / 'worker.md').write_text('# worker 2\n', encoding='utf-8')
        second = changes.apply(self.dir, 'worker-prompt', 'worker.md', 'L2')
        self.assertEqual(second['parent'], first['change_id'])
        self.assertNotEqual(second['change_id'], first['change_id'])
        self.assertEqual(len(self.change_rows()), 2)

    def test_worker_only_is_not_multi_and_worker_plus_roles_is_multi(self):
        changes.apply(self.dir, 'worker-prompt', 'worker.md', 'L1')
        (self.dir / 'worker.md').write_text('# worker 2\n', encoding='utf-8')
        one = changes.apply(self.dir, 'worker-prompt', 'worker.md', 'L1')
        self.assertIs(one['multi'], False)
        (self.dir / 'worker.md').write_text('# worker 3\n', encoding='utf-8')
        self.write_run(roles={'reviewer': {'deep': {'model': 'fable'}}})
        two = changes.apply(self.dir, 'worker-prompt', 'worker.md', 'L1')
        self.assertIs(two['multi'], True)

    def test_apply_refuses_bad_kind_and_writes_nothing(self):
        with contextlib.redirect_stderr(io.StringIO()):
            rc = changes.main(['apply', '--dir', str(self.dir), '--kind', 'poem',
                               '--target', 'worker.md', '--hypothesis', 'L1'])
        self.assertEqual(rc, 2)
        self.assertFalse((self.archive / 'changes.jsonl').exists())
        self.assertFalse((self.dir / 'events.jsonl').exists())

    def test_apply_refuses_bad_target_and_writes_nothing(self):
        with contextlib.redirect_stderr(io.StringIO()):
            rc = changes.main(['apply', '--dir', str(self.dir), '--kind', 'worker-prompt',
                               '--target', 'a b', '--hypothesis', 'L1'])
        self.assertEqual(rc, 2)
        self.assertFalse((self.archive / 'changes.jsonl').exists())
        self.assertFalse((self.dir / 'events.jsonl').exists())

    def test_apply_reads_only_the_tail_of_changes_jsonl(self):
        path = self.archive / 'changes.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        prefix = {'schema': 'harness.change/1', 'change_id': 'ch-old', 'pad': 'x' * 9000}
        path.write_text(json.dumps(prefix) + '\n', encoding='utf-8')
        row = changes.apply(self.dir, 'worker-prompt', 'worker.md', 'L1')
        self.assertEqual(row['parent'], 'ch-old')
        self.assertIs(row['multi'], False)
        self.assertEqual(changes.current_envelope(self.dir)['change_id'], row['change_id'])

    # ---- current_envelope ----

    def test_current_envelope_is_the_last_change_and_the_current_fp(self):
        empty = changes.current_envelope(self.dir)
        self.assertEqual(empty, {'config_fp': changes.config_fp(self.dir)})
        row = changes.apply(self.dir, 'worker-prompt', 'worker.md', 'L1')
        env = changes.current_envelope(self.dir)
        self.assertEqual(env, {'change_id': row['change_id'],
                               'config_fp': changes.config_fp(self.dir)})
        (self.dir / 'worker.md').write_text('# worker changed by hand\n', encoding='utf-8')
        env = changes.current_envelope(self.dir)
        self.assertEqual(env['change_id'], row['change_id'])
        self.assertEqual(env['config_fp'], changes.config_fp(self.dir))
        self.assertNotEqual(env['config_fp'], row['config_fp'])

    def test_current_envelope_returns_empty_dict_on_failure(self):
        with mock.patch.object(changes, 'config_fp', side_effect=RuntimeError('boom')):
            self.assertEqual(changes.current_envelope(self.dir), {})
        path = self.archive / 'changes.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{not json\n', encoding='utf-8')
        self.assertEqual(changes.current_envelope(self.dir), {})

    # ---- ordinary events ----

    def test_emit_after_apply_carries_the_change_id(self):
        row = changes.apply(self.dir, 'worker-prompt', 'worker.md', 'L1')
        self.assertTrue(events.emit(self.dir, 'run.started',
                                    {'workflow': 'jev-routed', 'launch': 'agent',
                                     'roles_from': {'designer': 'home'}}))
        last = self.event_rows()[-1]
        self.assertEqual(last['type'], 'run.started')
        self.assertEqual(last['change_id'], row['change_id'])
        self.assertEqual(last['config_fp'], changes.config_fp(self.dir))


if __name__ == '__main__':
    unittest.main()
