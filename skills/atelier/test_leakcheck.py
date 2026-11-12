"""leakcheck.py のテスト: diff の切り方・比べ方・閾値・sources の組み方・change.checked・events.py の委譲。"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import events
import leakcheck

NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
EVENTS_PY = HERE / 'events.py'


def make_temp() -> Path:
    """一時フォルダを既定の mode で作る。tempfile.mkdtemp は mode 0700 の DACL を作るので、
    DSH の Windows sandbox (workspace-write) ではその中に書けない (CI の Python 3.10 では問題ない)。"""
    base = Path(tempfile.gettempdir())
    for _ in range(10):
        path = base / f'leakcheck-{os.getpid()}-{uuid.uuid4().hex[:8]}'
        try:
            path.mkdir()
            return path
        except FileExistsError:
            continue
    path.mkdir()
    return path

CASE_A = 'repo:work:PBI-7'
CASE_B = 'repo:work:PBI-8'
GOAL_A = ('held-out の case の目的は、失敗の本文が改善の指示に写されていないことを、採用の前に確かめることにある。'
          'ここでは六十文字より長い文をわざと使い、長い一致でも拾えることを確かめるために、さらに言葉を足しておく。')
REWORK_B = 'train だけの作り直し依頼として、境界値の丸め方を直してほしい。'
REWORK_A = 'held-out の case の rework の Expected の本文。'
WHY = 'レビューの指摘の理由は、held-out の本文を写すと採点の基準そのものが漏れるからである。'
UNRELATED = '今日は雨が降っているので、散歩には行かないことにする。'


def unified_diff(added=(), removed=()):
    head = ('--- a/skills/atelier/worker.md\n'
            '+++ b/skills/atelier/worker.md\n'
            '@@ -1,2 +1,2 @@\n')
    return head + ''.join('-' + line + '\n' for line in removed) + ''.join('+' + line + '\n' for line in added)


class LeakcheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp()
        self.dir = self.tmp / 'pbi'
        (self.dir / 'reviews').mkdir(parents=True)
        self.home = self.tmp / 'home'
        (self.home / 'splits').mkdir(parents=True)
        self.archive = self.tmp / 'archive'
        self._env = dict(os.environ)
        os.environ['ATELIER_HOME'] = str(self.home)
        os.environ['HARNESS_LOG_CONFIG'] = str(self.tmp / 'no-config.json')
        os.environ['HARNESS_LOG_PATH'] = str(self.tmp / 'no-sender')
        os.environ['PBI_RUNS_ARCHIVE'] = str(self.archive)
        os.environ.pop('HARNESS_TRIAL', None)
        events._reset_caches()
        self.write_cases()
        self.write_review()
        self.write_manifest()

    def tearDown(self):
        events._reset_caches()
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 偽の docs/pbi と home ----

    def write_cases(self):
        case_a = {
            'schema': 'atelier.case/1', 'id': CASE_A, 'pbi': 'PBI-7',
            'task': {
                'goal': GOAL_A,
                'targets': '`skills/atelier/heldout_only_marker.py`',
                'do_not_touch': '既存のファイルは触らない。',
                'acceptance': [{'n': 1, 'text': 'held-out の受け入れ条件は、識別子だけでも重なりとして扱うこと。'}],
                'notes': 'held-out の注意として、本文はイベントにも標準出力にも出さない。',
                'verify': [{'n': 1, 'command': 'python -m unittest discover -s skills/atelier'}],
                'depends_on': [],
                'title': 'held-out の case',
            },
            'private': {'rework': [{'note': 'PBI-7.rework-1.md', 'items': [
                {'kind': 'expected', 'text': REWORK_A}]}]},
        }
        case_b = {
            'schema': 'atelier.case/1', 'id': CASE_B, 'pbi': 'PBI-8',
            'task': {
                'goal': 'train の case の目的は、ふつうの実装を進めることにある。',
                'targets': '`skills/atelier/train_only_marker.py`',
                'do_not_touch': '既存のファイルは触らない。',
                'acceptance': [{'n': 1, 'text': 'train の受け入れ条件。'}],
                'notes': 'train の注意。',
                'verify': [],
                'depends_on': [],
                'title': 'train の case',
            },
            'private': {'rework': [{'note': 'PBI-8.rework-1.md', 'items': [
                {'kind': 'expected', 'text': REWORK_B}]}]},
        }
        text = ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in (case_a, case_b))
        (self.dir / 'cases.jsonl').write_text(text, encoding='utf-8')

    def write_review(self):
        doc = {'schema': 'atelier.review/1', 'report': 'PBI-2.md', 'pbi': 'PBI-2',
               'findings': [{'severity': 'major', 'file': 'skills/atelier/worker.md', 'line': 1,
                             'why': WHY, 'repro': 'rep'}]}
        (self.dir / 'reviews' / 'PBI-2.review.json').write_text(json.dumps(doc, ensure_ascii=False),
                                                                encoding='utf-8')

    def write_manifest(self):
        manifest = {
            'schema': 'atelier.split/1', 'name': 'default', 'split_set_id': 'default-00000001',
            'seed': '1', 'heldout_ratio': 0.5, 'strata': ['task_type', 'domain', 'size_bucket'],
            'frozen': True,
            'cases': {CASE_A: {'split': 'heldout', 'strata': {}},
                      CASE_B: {'split': 'train', 'strata': {}}},
        }
        (self.home / 'splits' / 'default.json').write_text(json.dumps(manifest, ensure_ascii=False),
                                                           encoding='utf-8')

    # ---- 呼び出し ----

    def write_diff(self, text, name='change.diff'):
        path = self.tmp / name
        path.write_text(text, encoding='utf-8')
        return path

    def run_main(self, change, diff_text, *, split_name=None, diff_name='change.diff'):
        diff = self.write_diff(diff_text, diff_name)
        argv = ['--dir', str(self.dir), '--change', change, '--diff', str(diff), '--home', str(self.home)]
        if split_name is not None:
            argv += ['--split-name', split_name]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = leakcheck.main(argv)
        return code, out.getvalue(), err.getvalue()

    def result(self, code, out):
        self.assertEqual(code in (0, 3), True, out)
        return json.loads(out.strip())

    def events_rows(self):
        path = self.dir / 'events.jsonl'
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]

    def assert_one_checked(self, body=''):
        rows = [row for row in self.events_rows() if row.get('type') == 'change.checked']
        self.assertEqual(len(rows), 1)
        data = rows[0]['data']
        self.assertEqual(sorted(data), ['change_id', 'overlap_bucket', 'verdict', 'vs'])
        if body:
            text = (self.dir / 'events.jsonl').read_text(encoding='utf-8')
            self.assertNotIn(body, text)
            self.assertNotIn(body[:20], text)

    # ---- 受け入れ条件 1 の各行 ----

    def test_multiline_source_with_and_without_filler(self):
        words = ['the retry loop must stop after three attempts',
                 'and then report the final failure to caller',
                 'without swallowing the original exception',
                 'so the log line shows the real cause here',
                 'and the exit code is two for every failure',
                 'instead of the zero that it returns now',
                 'Repro: run the script with a bad host name',
                 'and look at the exit code that comes back',
                 'it prints nothing and returns success now',
                 'that is wrong because the guard is silent']
        lines = ['Expected: ' + word for word in words]
        source = [leakcheck.Source(vs='failure', text='\n'.join(lines))]
        mixed = [line for n, line in enumerate(lines)
                 for line in [line] + [f'unrelated filler {n}-{i} about weather' for i in range(3)]]
        unrelated = [f'gardening and sunshine fill the afternoon number {n}' for n in range(40)]
        for added in (lines, mixed, lines[:5]):
            with self.subTest(added=added):
                result = leakcheck.check(added, source)
                self.assertEqual(result['verdict'], 'warn')
                self.assertEqual(result['vs'], ['failure'])
        self.assertEqual(leakcheck.check(unrelated, source),
                         {'verdict': 'pass', 'overlap_bucket': '0', 'vs': []})

    def test_heldout_goal_is_blocked(self):
        self.assertGreaterEqual(len(GOAL_A), 60)
        code, out, err = self.run_main('ch-block', unified_diff(added=[GOAL_A]))
        self.assertEqual(code, 3, err)
        result = self.result(code, out)
        self.assertEqual(result['verdict'], 'block')
        self.assertIn('heldout', result['vs'])
        self.assertNotIn(GOAL_A, out)
        self.assert_one_checked(GOAL_A)

    def test_heldout_goal_is_blocked_with_other_added_lines(self):
        # 足した行が増えても、元の 1 行をそのまま写したら被覆率が 1 になる。
        copied = 'Returns 404 when the user id is unknown'
        filler = [f'filler {n} about something else entirely here' for n in range(40)]
        result = leakcheck.check(filler + [copied], [leakcheck.Source(vs='heldout', text=copied)])
        self.assertEqual(result['verdict'], 'block')
        self.assertEqual(result['vs'], ['heldout'])

    def test_short_heldout_goal_is_blocked_after_filler_lines(self):
        # 60 字未満の日本語の goal でも、10 行の足した行のあとで見つける。
        goal = 'ログインの失敗は、三回までなら同じ画面に留めて、四回目で閉じる。'
        self.assertLess(len(goal), 60)
        filler = [f'関係のない行 {n} をここに置く。' for n in range(10)]
        result = leakcheck.check(filler + [goal], [leakcheck.Source(vs='heldout', text=goal)])
        self.assertEqual(result['verdict'], 'block')
        self.assertEqual(result['vs'], ['heldout'])

    def test_train_rework_is_warn(self):
        code, out, err = self.run_main('ch-warn', unified_diff(added=[REWORK_B]))
        self.assertEqual(code, 0, err)
        result = self.result(code, out)
        self.assertEqual(result['verdict'], 'warn')
        self.assertEqual(result['vs'], ['failure'])
        self.assertNotIn(REWORK_B, out)
        self.assert_one_checked(REWORK_B)

    def test_review_why_is_blocked(self):
        code, out, err = self.run_main('ch-rubric', unified_diff(added=[WHY]))
        self.assertEqual(code, 3, err)
        result = self.result(code, out)
        self.assertEqual(result['verdict'], 'block')
        self.assertIn('rubric', result['vs'])
        self.assert_one_checked(WHY)

    def test_unrelated_lines_pass(self):
        code, out, err = self.run_main('ch-pass', unified_diff(added=[UNRELATED]))
        self.assertEqual(code, 0, err)
        result = self.result(code, out)
        self.assertEqual(result, {'verdict': 'pass', 'overlap_bucket': '0', 'vs': []})
        self.assert_one_checked(UNRELATED)

    def test_pbi_identifier_is_overlap(self):
        code, out, err = self.run_main('ch-ident-pbi', unified_diff(added=['PBI-7']))
        self.assertEqual(code, 3, err)
        result = self.result(code, out)
        self.assertEqual(result['verdict'], 'block')
        self.assertEqual(result['overlap_bucket'], 'low')
        self.assertEqual(result['vs'], ['heldout'])
        self.assert_one_checked('PBI-7')

    def test_case_file_name_identifier_is_overlap(self):
        code, out, err = self.run_main(
            'ch-ident-file', unified_diff(added=['skills/atelier/heldout_only_marker.py']))
        self.assertEqual(code, 3, err)
        result = self.result(code, out)
        self.assertEqual(result['verdict'], 'block')
        self.assertEqual(result['overlap_bucket'], 'low')
        self.assertEqual(result['vs'], ['heldout'])
        self.assert_one_checked('heldout_only_marker.py')

    def test_pbi_prefix_is_not_an_identifier(self):
        code, out, err = self.run_main('ch-ident-prefix', unified_diff(added=['PBI-70']))
        self.assertEqual(code, 0, err)
        result = self.result(code, out)
        self.assertEqual(result, {'verdict': 'pass', 'overlap_bucket': '0', 'vs': []})
        self.assert_one_checked('PBI-70')

    def test_removed_lines_are_not_compared(self):
        code, out, err = self.run_main('ch-removed', unified_diff(removed=[GOAL_A]))
        self.assertEqual(code, 0, err)
        result = self.result(code, out)
        self.assertEqual(result, {'verdict': 'pass', 'overlap_bucket': '0', 'vs': []})
        self.assert_one_checked(GOAL_A)

    def test_added_lines_skips_headers_context_and_removed(self):
        text = ('--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n context\n\\ No newline at end of file\n')
        self.assertEqual(leakcheck.added_lines(text), ['new'])
        self.assertEqual(leakcheck.added_lines(unified_diff(added=['+ itself'])), ['+ itself'])

    # ---- once・委譲・誤り ----

    def test_second_run_with_the_same_diff_writes_one_event(self):
        diff = unified_diff(added=[GOAL_A])
        first = self.run_main('ch-once', diff)
        second = self.run_main('ch-once', diff, diff_name='change-2.diff')
        self.assertEqual(first[0], 3)
        self.assertEqual(second[0], 3)
        self.assertEqual(first[1], second[1])
        self.assert_one_checked(GOAL_A)

    def test_changed_verdict_writes_a_second_event(self):
        # 同じ --change・同じ diff。1 回目は manifest 無し (全 case train) で train の失敗に重なり warn、
        # 2 回目は held-out の manifest ありで held-out に重なり block。再び同じ打ち方をしても増えない。
        diff = unified_diff(added=[GOAL_A, REWORK_A])
        (self.home / 'splits' / 'default.json').unlink()
        first = self.run_main('ch-changed', diff)
        self.assertEqual(first[0], 0, first[2])
        self.assertEqual(self.result(*first[:2])['verdict'], 'warn')
        self.write_manifest()
        second = self.run_main('ch-changed', diff, diff_name='change-2.diff')
        self.assertEqual(second[0], 3, second[2])
        self.assertEqual(self.result(*second[:2])['verdict'], 'block')
        third = self.run_main('ch-changed', diff, diff_name='change-3.diff')
        self.assertEqual(third[0], 3, third[2])
        self.assertEqual(third[1], second[1])
        rows = [row['data'] for row in self.events_rows() if row.get('type') == 'change.checked']
        self.assertEqual([row['verdict'] for row in rows], ['warn', 'block'])
        for row in rows:
            self.assertEqual(sorted(row), ['change_id', 'overlap_bucket', 'verdict', 'vs'])

    def test_missing_dir_is_usage(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = leakcheck.main(['--dir', str(self.tmp / 'nope'), '--change', 'ch-x',
                                   '--diff', str(self.write_diff(''))])
        self.assertEqual(code, 2)
        self.assertEqual(out.getvalue(), '')
        self.assertTrue(err.getvalue().strip())

    def test_missing_manifest_treats_every_case_as_train(self):
        (self.home / 'splits' / 'default.json').unlink()
        goal = 'Autumn gardens bloom under gentle sunshine while migrating birds cross the horizon.'
        cases_path = self.dir / 'cases.jsonl'
        cases = [json.loads(line) for line in cases_path.read_text(encoding='utf-8').splitlines()]
        cases[0]['task']['goal'] = goal
        cases_path.write_text(''.join(json.dumps(case, ensure_ascii=False) + '\n' for case in cases),
                              encoding='utf-8')
        code, out, err = self.run_main('ch-train-fallback', unified_diff(added=[goal]))
        self.assertEqual(code, 0, err)  # train の goal は比べる元ではないので pass
        result = self.result(code, out)
        self.assertEqual(result, {'verdict': 'pass', 'overlap_bucket': '0', 'vs': []})
        self.assertIn('leakcheck: no split manifest; held-out not checked', err)
        self.assert_one_checked(goal)

    def init_manifest_without_assign(self, ratio):
        """splits init だけで manifest を作る (assign しないので cases は空)。"""
        import splits
        (self.home / 'splits' / 'default.json').unlink()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = splits.main(['init', '--seed', 's1', '--heldout-ratio', ratio, '--home', str(self.home)])
        self.assertEqual(code, 0, err.getvalue())
        manifest = splits.load_manifest('default', self.home)
        self.assertEqual(manifest['cases'], {})

    def test_unassigned_case_predicted_heldout_is_blocked(self):
        # split assign の前でも、split_of が heldout を予測する case は held-out として比べる。
        self.init_manifest_without_assign('1.0')
        code, out, err = self.run_main('ch-unassigned', unified_diff(added=[GOAL_A]))
        self.assertEqual(code, 3, err)
        result = self.result(code, out)
        self.assertEqual(result['verdict'], 'block')
        self.assertIn('heldout', result['vs'])
        self.assert_one_checked(GOAL_A)

    def test_unassigned_case_predicted_train_is_not_heldout(self):
        self.init_manifest_without_assign('0.0')
        code, out, err = self.run_main('ch-unassigned-train', unified_diff(added=[GOAL_A]))
        result = self.result(code, out)
        self.assertNotIn('heldout', result['vs'])
        self.assertNotEqual(result['verdict'], 'block', err)

    def test_run_pseudo_case_is_train(self):
        manifest = leakcheck._manifest('default', self.home)
        self.assertFalse(leakcheck._is_heldout(manifest, 'repo:work:run'))
        self.assertFalse(leakcheck._is_heldout(None, CASE_A))
        self.assertTrue(leakcheck._is_heldout(manifest, CASE_A))

    def test_load_sources_failure_is_exit_2(self):
        # 読めないときに黙って pass にしない (仕様 §297・§343 の門)。
        with mock.patch.object(leakcheck, 'load_sources', side_effect=RuntimeError('boom')):
            code, out, err = self.run_main('ch-load-fail', unified_diff(added=[UNRELATED]))
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertIn('leakcheck: cannot load sources: boom', err)

    def test_archived_heldout_case_is_compared(self):
        # export_cases が保全先に写した cases.jsonl だけにある held-out の case も比べる。
        archived_goal = '保全先にだけある held-out の目的は、三回連続の失敗を同じ画面にまとめて示すことにある。'
        case_id = 'repo:old-work:PBI-9'
        manifest = json.loads((self.home / 'splits' / 'default.json').read_text(encoding='utf-8'))
        manifest['cases'][case_id] = {'split': 'heldout', 'strata': {}}
        (self.home / 'splits' / 'default.json').write_text(json.dumps(manifest, ensure_ascii=False),
                                                           encoding='utf-8')
        row = {'schema': 'atelier.case/1', 'id': case_id, 'pbi': 'PBI-9',
               'task': {'goal': archived_goal, 'targets': '', 'acceptance': [], 'notes': '',
                        'verify': [], 'depends_on': [], 'title': ''},
               'private': {'rework': []}}
        group = self.archive / 'repo' / 'old-work__W-1'
        group.mkdir(parents=True)
        (group / 'cases.jsonl').write_text(json.dumps(row, ensure_ascii=False) + '\n', encoding='utf-8')
        code, out, err = self.run_main('ch-archive', unified_diff(added=[archived_goal]))
        self.assertEqual(code, 3, err)
        result = self.result(code, out)
        self.assertEqual(result['verdict'], 'block')
        self.assertEqual(result['vs'], ['heldout'])
        self.assert_one_checked(archived_goal)

    def run_unassigned_archived(self, ratio, case_id='repo:repo:main:W-1:PBI-1'):
        """manifest に未登録で保全先にだけある case の goal を写した diff を、新しい home で比べる。"""
        import splits
        goal = ('Add a resumable progress bar to the export command that survives a crash '
                'and restarts at the last chunk.')
        home = self.tmp / 'home-fresh'
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(splits.main(['init', '--seed', 's1', '--heldout-ratio', ratio,
                                          '--home', str(home)]), 0)
        row = {'schema': 'atelier.case/1', 'id': case_id, 'pbi': 'PBI-1',
               'task': {'goal': goal}, 'private': {'rework': []}}
        group = self.archive / 'repo' / 'main__W-1'
        group.mkdir(parents=True)
        (group / 'cases.jsonl').write_text(json.dumps(row) + '\n', encoding='utf-8')
        (self.dir / 'cases.jsonl').write_text('', encoding='utf-8')
        diff = self.write_diff(unified_diff(added=[goal]))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = leakcheck.main(['--dir', str(self.dir), '--change', 'ch-unassigned',
                                   '--diff', str(diff), '--home', str(home)])
        manifest = json.loads((home / 'splits' / 'default.json').read_text(encoding='utf-8'))
        self.assertNotIn(case_id, manifest['cases'])  # leakcheck は manifest を書かない
        return code, out.getvalue(), err.getvalue(), goal

    def test_unassigned_archived_case_predicted_heldout_is_blocked(self):
        # split assign を飛ばした前の作業の case: 保全先にだけあり、manifest に未登録。
        code, out, err, goal = self.run_unassigned_archived('1.0')
        self.assertEqual(code, 3, err)
        result = self.result(code, out)
        self.assertEqual(result['verdict'], 'block')
        self.assertIn('heldout', result['vs'])
        self.assert_one_checked(goal)

    def test_unassigned_archived_case_predicted_train_is_not_heldout(self):
        code, out, err, goal = self.run_unassigned_archived('0.0')
        self.assertEqual(code, 0, err)
        result = self.result(code, out)
        self.assertNotEqual(result['verdict'], 'block')
        self.assertNotIn('heldout', result['vs'])

    def test_unassigned_archived_run_case_is_not_loaded_as_heldout(self):
        code, out, err, goal = self.run_unassigned_archived('1.0', case_id='repo:main__W-1:run')
        self.assertEqual(code, 0, err)
        result = self.result(code, out)
        self.assertNotIn('heldout', result['vs'])

    # ---- held-out を比べられないときに pass にしない ----

    def assert_refused(self, code, out, err, message):
        self.assertEqual(code, 2, err)
        self.assertEqual(out.strip(), '')
        self.assertIn(message, err)
        self.assertEqual([row for row in self.events_rows() if row.get('type') == 'change.checked'], [])

    def test_unreadable_manifest_is_exit_2(self):
        manifest = self.home / 'splits' / 'default.json'
        manifest.write_text('{not json', encoding='utf-8')
        diff = self.write_diff(unified_diff(added=[UNRELATED]), 'broken.diff')
        proc = subprocess.run(
            [sys.executable, str(HERE / 'leakcheck.py'), '--dir', str(self.dir), '--change', 'ch-1',
             '--diff', str(diff), '--home', str(self.home)],
            capture_output=True, text=True, encoding='utf-8', errors='replace', env=dict(os.environ),
            cwd=str(self.tmp), creationflags=NO_WINDOW)
        self.assert_refused(proc.returncode, proc.stdout, proc.stderr,
                            f'leakcheck: cannot read split manifest: {manifest}')
        self.assertNotIn('no split manifest', proc.stderr)

    def test_wrong_shape_manifest_is_exit_2(self):
        manifest = self.home / 'splits' / 'default.json'
        manifest.write_text(json.dumps({'schema': 'other', 'cases': {}}), encoding='utf-8')
        code, out, err = self.run_main('ch-shape', unified_diff(added=[UNRELATED]))
        self.assert_refused(code, out, err, f'leakcheck: cannot read split manifest: {manifest}')

    def test_missing_manifest_file_still_passes_with_note(self):
        (self.home / 'splits' / 'default.json').unlink()
        code, out, err = self.run_main('ch-no-manifest', unified_diff(added=[UNRELATED]))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.result(code, out)['verdict'], 'pass')
        self.assertIn('leakcheck: no split manifest; held-out not checked', err)
        self.assert_one_checked()

    def test_heldout_case_found_nowhere_is_exit_2(self):
        path = self.home / 'splits' / 'default.json'
        manifest = json.loads(path.read_text(encoding='utf-8'))
        manifest['cases']['case-x'] = {'split': 'heldout', 'strata': {}}
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
        self.archive.mkdir(parents=True, exist_ok=True)  # 空の保全先
        code, out, err = self.run_main('ch-lost', unified_diff(added=[UNRELATED]))
        self.assert_refused(code, out, err, 'leakcheck: held-out case not found: case-x')

    def test_heldout_missing_lists_up_to_five_ids(self):
        path = self.home / 'splits' / 'default.json'
        manifest = json.loads(path.read_text(encoding='utf-8'))
        ids = [f'case-{n}' for n in range(7)]
        for case_id in ids:
            manifest['cases'][case_id] = {'split': 'heldout', 'strata': {}}
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
        code, out, err = self.run_main('ch-lost-many', unified_diff(added=[UNRELATED]))
        self.assert_refused(code, out, err, 'leakcheck: held-out case not found: ' + ', '.join(ids[:5]))
        self.assertNotIn('case-5', err)

    def test_check_failure_is_exit_2(self):
        with mock.patch.object(leakcheck, 'check', side_effect=MemoryError()):
            code, out, err = self.run_main('ch-check-fail', unified_diff(added=[UNRELATED]))
        self.assert_refused(code, out, err, 'leakcheck: check failed: MemoryError')

    def test_added_lines_failure_is_exit_2(self):
        with mock.patch.object(leakcheck, 'added_lines', side_effect=ValueError('x')):
            code, out, err = self.run_main('ch-added-fail', unified_diff(added=[UNRELATED]))
        self.assert_refused(code, out, err, 'leakcheck: check failed: ValueError')


    def test_events_py_delegates(self):
        diff = self.write_diff(unified_diff(added=[GOAL_A]), 'delegate.diff')
        env = dict(os.environ)
        proc = subprocess.run(
            [sys.executable, str(EVENTS_PY), 'leakcheck', '--dir', str(self.dir), '--change', 'ch-del',
             '--diff', str(diff), '--home', str(self.home)],
            capture_output=True, text=True, encoding='utf-8', errors='replace', env=env,
            cwd=str(self.tmp), creationflags=NO_WINDOW)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertNotIn(GOAL_A, proc.stdout)
        self.assert_one_checked(GOAL_A)

    # ---- 閾値 ----

    def test_thresholds_are_a_parameter(self):
        source = [leakcheck.Source(vs='heldout', text=GOAL_A)]
        chunk = GOAL_A[:70]
        self.assertEqual(leakcheck.check([chunk], source)['verdict'], 'block')
        strict = leakcheck.Thresholds(jaccard=(0.9, 0.95, 0.99), longest=(10 ** 6, 10 ** 6))
        self.assertEqual(leakcheck.check([chunk], source, strict)['verdict'], 'pass')

    def test_from_mapping_and_load_thresholds(self):
        path = self.tmp / 'taxonomy.json'
        path.write_text(json.dumps({'taxonomy_version': '1',
                                    'leakcheck': {'jaccard': [0.2, 0.4, 0.8], 'longest': [30, 100]}}),
                        encoding='utf-8')
        loaded = leakcheck.load_thresholds(path)
        self.assertEqual(loaded.jaccard, (0.2, 0.4, 0.8))
        self.assertEqual(loaded.longest, (30, 100))
        self.assertIs(leakcheck.load_thresholds(self.tmp / 'none.json'), leakcheck.DEFAULT_THRESHOLDS)
        with self.assertRaises(ValueError):
            leakcheck.Thresholds.from_mapping({'jaccard': [0.3, 0.2, 0.6]})
        with self.assertRaises(ValueError):
            leakcheck.Thresholds.from_mapping({'unknown': 1})

    def test_containment_longest_from_mapping(self):
        self.assertEqual(leakcheck.DEFAULT_THRESHOLDS.containment_longest, 40)
        loaded = leakcheck.Thresholds.from_mapping({'containment_longest': 25})
        self.assertEqual(loaded.containment_longest, 25)
        path = self.tmp / 'taxonomy.json'
        path.write_text(json.dumps({'leakcheck': {'containment_longest': 55}}), encoding='utf-8')
        self.assertEqual(leakcheck.load_thresholds(path).containment_longest, 55)
        for bad in (0, -1, 1.5, True, '40'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                leakcheck.Thresholds.from_mapping({'containment_longest': bad})

    # ---- 全体の包含率の門 ----

    def test_long_unrelated_prose_in_same_vocabulary_passes(self):
        import random
        pool = ('worker designer prompt appendix rework review harness verify failure criteria '
                'changes events source split manifest overlap rubric threshold command acceptance '
                'report emitted recorded checked written stage result memory model runner').split()
        rng = random.Random(7)

        def sentence(n):
            return ' '.join(rng.choice(pool) for _ in range(n))

        why = sentence(40)
        added = [sentence(10) for _ in range(200)]
        source = [leakcheck.Source(vs='rubric', text=why)]
        self.assertEqual(leakcheck.check(added, source),
                         {'verdict': 'pass', 'overlap_bucket': '0', 'vs': []})
        # 門を下げれば包含率が数えられて block になる (門が効いていることの確認)。
        loose = leakcheck.Thresholds(containment_longest=1)
        self.assertEqual(leakcheck.check(added, source, loose)['verdict'], 'block')

    def test_multiline_source_pasted_line_by_line_is_warn(self):
        words = ['the retry loop must stop after three attempts',
                 'and then report the final failure to caller',
                 'without swallowing the original exception',
                 'so the log line shows the real cause here',
                 'and the exit code is two for every failure',
                 'instead of the zero that it returns now',
                 'Repro: run the script with a bad host name',
                 'and look at the exit code that comes back',
                 'it prints nothing and returns success now',
                 'that is wrong because the guard is silent']
        lines = ['Expected: ' + word for word in words]
        result = leakcheck.check(lines, [leakcheck.Source(vs='failure', text='\n'.join(lines))])
        self.assertEqual(result['verdict'], 'warn')
        self.assertEqual(result['vs'], ['failure'])


if __name__ == '__main__':
    unittest.main()
