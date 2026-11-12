"""ingest.py の振る舞い: analyzer の判定の取り込みと、停滞の判定。"""
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

import events
import ingest

ROUND = {
    'change_id': 'ch-1', 'base_change_id': 'ch-0', 'verdict': 'accept',
    'd_train': 'up2', 'd_heldout': 'up', 'ci_lo': 0.01, 'ci_hi': 0.12, 'trials': 3,
}
TRIAGE = {'lineage_id': 'lin-1', 'root_cause': 'harness', 'by': 'human'}
CLUSTER = {
    'unit_id': 'at:r1:PBI-1:1', 'clusterset_id': 'cs-1', 'cluster_id': 'c-3',
    'lineage_id': None, 'dist_bucket': 'near', 'how': 'nearest',
}


class IngestTest(unittest.TestCase):
    def setUp(self):
        # mkdtemp の 0o700 は、この環境の sandbox では子を作れない。既定の mode で作る。
        self.tmp = Path(tempfile.gettempdir()) / f'test_ingest_{os.getpid()}_{uuid.uuid4().hex[:12]}'
        self.tmp.mkdir()
        self.dir = self.tmp / 'pbi'
        self.dir.mkdir()
        self.home = self.tmp / 'home'
        self.home.mkdir()
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

    def write_source(self, rows):
        """1 行 1 イベントの JSONL を書く。dict は JSON にし、str はそのまま 1 行として書く。"""
        path = self.tmp / 'in.jsonl'
        lines = [row if isinstance(row, str) else json.dumps(row, ensure_ascii=False) for row in rows]
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        return path

    def rows(self):
        path = self.dir / 'events.jsonl'
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]

    def judge(self, *verdicts):
        for verdict in verdicts:
            events.emit(self.dir, 'round.judged', dict(ROUND, verdict=verdict))

    # ---- 取り込み ----

    def test_four_valid_rows_are_appended(self):
        path = self.write_source([
            {'type': 'round.judged', 'data': ROUND},
            {'type': 'triage.labeled', 'data': TRIAGE},
            {'type': 'round.judged', 'data': dict(ROUND, verdict='regress')},
            {'type': 'cluster.assigned', 'data': CLUSTER},
        ])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            got = ingest.ingest(self.dir, path)
        self.assertEqual(got, (4, 0))
        self.assertEqual(err.getvalue(), '')
        rows = self.rows()
        self.assertEqual([row['type'] for row in rows],
                         ['round.judged', 'triage.labeled', 'round.judged', 'cluster.assigned'])
        for row in rows:
            self.assertEqual(row['schema'], 'atelier.event/1')
            self.assertIsNone(row['stage'])
            self.assertIsNone(row['pbi'])
        self.assertEqual(rows[0]['data'], ROUND)
        self.assertEqual(rows[1]['data'], TRIAGE)
        self.assertEqual(rows[3]['data'], CLUSTER)

    def test_unknown_type_non_json_and_bad_required_are_dropped(self):
        path = self.write_source([
            {'type': 'run.started', 'data': {'workflow': 'w'}},
            '{not json',
            {'type': 'round.judged', 'data': dict(ROUND, verdict='great')},
            {'type': 'triage.labeled', 'data': {'lineage_id': 'lin-1', 'by': 'human'}},
            {'type': 'cluster.assigned', 'data': dict(CLUSTER, cluster_id=7)},
            {'type': 'round.judged', 'data': dict(ROUND, verdict='noise')},
        ])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            got = ingest.ingest(self.dir, path)
        self.assertEqual(got, (1, 5))
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['data']['verdict'], 'noise')
        text = err.getvalue()
        self.assertEqual(text.count('\n'), 1)
        self.assertIn('5', text)
        for body in ('great', 'run.started', 'not json', 'lin-1'):
            self.assertNotIn(body, text)

    def test_data_must_be_an_object(self):
        path = self.write_source([
            {'type': 'round.judged', 'data': [ROUND]},
            {'type': 'triage.labeled', 'data': 'lin-1'},
        ])
        with contextlib.redirect_stderr(io.StringIO()):
            got = ingest.ingest(self.dir, path)
        self.assertEqual(got, (0, 2))
        self.assertEqual(self.rows(), [])

    def test_cluster_unit_id_is_also_in_the_envelope(self):
        path = self.write_source([{'type': 'cluster.assigned', 'data': CLUSTER}])
        self.assertEqual(ingest.ingest(self.dir, path), (1, 0))
        row = self.rows()[0]
        self.assertEqual(row['unit_id'], 'at:r1:PBI-1:1')
        self.assertEqual(row['data']['unit_id'], 'at:r1:PBI-1:1')

    def test_invalid_optional_field_drops_only_that_field(self):
        path = self.write_source([{'type': 'round.judged', 'data': dict(ROUND, d_train='sideways')}])
        self.assertEqual(ingest.ingest(self.dir, path), (1, 0))
        row = self.rows()[0]
        self.assertNotIn('d_train', row['data'])
        self.assertIn('d_train', row['dropped'])

    # ---- 停滞 ----

    def test_stalled_when_three_non_accept_at_the_tail(self):
        self.judge('accept', 'regress', 'noise', 'overfit_suspect')
        self.assertTrue(ingest.stalled(self.dir))
        self.assertTrue(ingest.stalled(self.dir, 3))

    def test_not_stalled_when_last_is_accept(self):
        self.judge('regress', 'noise', 'accept')
        self.assertFalse(ingest.stalled(self.dir))

    def test_not_stalled_below_three_judgements(self):
        self.judge('regress', 'noise')
        self.assertFalse(ingest.stalled(self.dir))

    def test_other_events_between_judgements_do_not_reset_the_streak(self):
        self.judge('accept', 'regress')
        events.emit(self.dir, 'triage.labeled', TRIAGE)
        self.judge('noise', 'invalid')
        self.assertTrue(ingest.stalled(self.dir))

    def test_short_streak_with_larger_k_is_not_stalled(self):
        self.judge('regress', 'noise', 'invalid')
        self.assertFalse(ingest.stalled(self.dir, 4))

    def test_missing_events_file_is_not_stalled(self):
        self.assertFalse(ingest.stalled(self.tmp / 'nope'))

    def test_reingesting_the_same_file_is_silent_and_does_not_stall(self):
        """repro: 2 件 (noise, regress) を取り込み、同じファイルをもう一度取り込んでも増えない。"""
        path = self.write_source([
            {'type': 'round.judged', 'data': dict(ROUND, verdict='noise')},
            {'type': 'round.judged', 'data': dict(ROUND, verdict='regress')},
        ])
        self.assertEqual(ingest.ingest(self.dir, path), (2, 0))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ingest.main(['stalled', '--dir', str(self.dir)])
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), 'ok')

        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = ingest.main(['ingest', '--dir', str(self.dir), '--file', str(path)])
        self.assertEqual(rc, 0)
        self.assertEqual(err.getvalue(), '')
        self.assertEqual(len(self.rows()), 2)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ingest.main(['stalled', '--dir', str(self.dir)])
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), 'ok')

    # ---- CLI ----

    def test_missing_file_exits_2(self):
        missing = self.tmp / 'nope.jsonl'
        self.assertEqual(ingest.main(['ingest', '--dir', str(self.dir), '--file', str(missing)]), 2)

    def test_delegated_form_without_subcommand(self):
        path = self.write_source([{'type': 'round.judged', 'data': ROUND}])
        self.assertEqual(ingest.main(['--dir', str(self.dir), '--file', str(path)]), 0)
        self.assertEqual(len(self.rows()), 1)

    def test_events_delegates_ingest(self):
        path = self.write_source([{'type': 'triage.labeled', 'data': TRIAGE}])
        self.assertEqual(events.main(['ingest', '--dir', str(self.dir), '--file', str(path)]), 0)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['type'], 'triage.labeled')

    def test_stalled_cli_prints_one_word(self):
        self.judge('regress', 'noise', 'invalid')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ingest.main(['stalled', '--dir', str(self.dir)])
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), 'stalled')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ingest.main(['stalled', '--dir', str(self.dir), '--k', '5'])
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), 'ok')


if __name__ == '__main__':
    unittest.main()
