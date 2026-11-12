"""splits.py のテスト (manifest の形・決定的な割当・2 回目の assign・retire・split_of / split_in・events.py の委譲)。"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import events
import splits

NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
EVENTS_PY = HERE / 'events.py'
SEED = '42'
CASES = 20


def case_ids(count=CASES):
    """本物に近い case_id (events.py の id/hash は 64 字までなので、その内側に収める)。"""
    return [f'atelier-metrics:atelier-metrics:claude/x:W-11:PBI-{100 + n}' for n in range(count)]


class SplitsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.dir = self.tmp / 'pbi'
        self.dir.mkdir()
        self.home = self.tmp / 'home-a'
        self.other = self.tmp / 'home-b'
        self._env = dict(os.environ)
        os.environ['ATELIER_HOME'] = str(self.home)  # events.emit の自動の split_of も一時の home を見る
        os.environ['HARNESS_LOG_CONFIG'] = str(self.tmp / 'no-config.json')
        os.environ['HARNESS_LOG_PATH'] = str(self.tmp / 'no-sender')
        os.environ.pop('HARNESS_TRIAL', None)
        events._reset_caches()

    def tearDown(self):
        events._reset_caches()
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers ----

    def manifest(self, home=None, name='default'):
        return json.loads((Path(home or self.home) / 'splits' / f'{name}.json').read_text(encoding='utf-8'))

    def manifest_path(self, home=None, name='default'):
        return Path(home or self.home) / 'splits' / f'{name}.json'

    def run_splits(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            return splits.main(list(argv))

    def init(self, home=None, seed=SEED, name='default'):
        return self.run_splits('init', '--name', name, '--seed', seed, '--home', str(home or self.home))

    def assign(self, home=None, name='default'):
        return self.run_splits('assign', '--dir', str(self.dir), '--name', name,
                               '--home', str(home or self.home))

    def write_cases(self, ids):
        rows = [{'schema': 'atelier.case/1', 'id': cid, 'pbi': cid.rsplit(':', 1)[-1]} for cid in ids]
        text = ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows)
        (self.dir / 'cases.jsonl').write_text(text, encoding='utf-8')

    def profile(self, cid, task_type='feature', domain='cli', files='2-3'):
        return {'schema': 'atelier.event/1', 'ts': '2026-11-11T00:00:00Z', 'type': 'task.profiled',
                'pbi': cid.rsplit(':', 1)[-1],
                'data': {'scope': 'pbi', 'case_id': cid, 'task_type': task_type, 'domain': domain,
                         'size': {'files': files}}}

    def write_events(self, rows):
        text = ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows)
        (self.dir / 'events.jsonl').write_text(text, encoding='utf-8')

    def rows(self, type_=None):
        path = self.dir / 'events.jsonl'
        if not path.is_file():
            return []
        rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
        return [row for row in rows if type_ is None or row.get('type') == type_]

    def run_events(self, *argv):
        env = dict(os.environ)
        env.pop('PYTHONPATH', None)
        return subprocess.run([sys.executable, str(EVENTS_PY), *argv], capture_output=True, text=True,
                              encoding='utf-8', errors='replace', env=env, cwd=str(self.tmp),
                              creationflags=NO_WINDOW)

    # ---- init ----

    def test_init_writes_the_manifest_in_the_refs_shape(self):
        self.assertEqual(self.init(), 0)
        path = self.manifest_path()
        self.assertTrue(path.is_file())
        data = self.manifest()
        self.assertEqual(data['schema'], 'atelier.split/1')
        self.assertEqual(data['name'], 'default')
        self.assertEqual(data['seed'], SEED)
        self.assertEqual(data['heldout_ratio'], 0.2)
        self.assertEqual(data['strata'], ['task_type', 'domain', 'size_bucket'])
        self.assertIs(data['frozen'], True)
        self.assertEqual(data['cases'], {})
        self.assertEqual(data['split_set_id'],
                         'default-' + hashlib.sha256(SEED.encode('utf-8')).hexdigest()[:8])

    def test_second_init_exits_2_and_keeps_the_file(self):
        self.assertEqual(self.init(), 0)
        path = self.manifest_path()
        before = path.read_bytes()
        self.assertEqual(self.init(), 2)
        self.assertEqual(path.read_bytes(), before)

    # ---- assign ----

    def test_assign_is_deterministic_and_records_strata(self):
        ids = case_ids()
        self.write_cases(ids)
        profiled = ids[:15]
        expected_strata = {}
        rows = []
        for index, cid in enumerate(profiled):
            task_type = 'feature' if index % 2 else 'bugfix'
            domain = 'cli' if index % 3 else 'skill'
            rows.append(self.profile(cid, task_type, domain))
            expected_strata[cid] = {'task_type': task_type, 'domain': domain, 'size_bucket': '2-3'}
        self.write_events(rows)

        self.assertEqual(self.init(self.home), 0)
        self.assertEqual(self.init(self.other), 0)
        self.assertEqual(self.assign(self.home), 0)
        self.assertEqual(self.assign(self.other), 0)

        first = self.manifest(self.home)
        second = self.manifest(self.other)
        splits_a = {cid: entry['split'] for cid, entry in first['cases'].items()}
        splits_b = {cid: entry['split'] for cid, entry in second['cases'].items()}
        self.assertEqual(splits_a, splits_b)
        self.assertEqual(set(splits_a), set(ids))

        heldout = [cid for cid, split in splits_a.items() if split == 'heldout']
        self.assertGreaterEqual(len(heldout), round(len(ids) * 0.2) - 3)
        self.assertLessEqual(len(heldout), round(len(ids) * 0.2) + 3)

        for cid in ids:
            strata = first['cases'][cid]['strata']
            self.assertEqual(set(strata), {'task_type', 'domain', 'size_bucket'})
            if cid in expected_strata:
                self.assertEqual(strata, expected_strata[cid])
            else:
                self.assertEqual(strata, {'task_type': 'unknown', 'domain': 'unknown',
                                          'size_bucket': 'unknown'})

        emitted = self.rows('split.assigned')
        self.assertEqual(len(emitted), len(ids))
        for row in emitted:
            self.assertNotIn('dropped', row)
            self.assertEqual(row['split'], row['data']['split'])
            self.assertEqual(row['split_set_id'], first['split_set_id'])
            self.assertEqual(row['data']['split_set_id'], first['split_set_id'])
            self.assertEqual(row['data']['reason'], 'seeded')
            self.assertEqual(row['pbi'], row['data']['case_id'].rsplit(':', 1)[-1])
            self.assertEqual(row['split'], first['cases'][row['data']['case_id']]['split'])

    def test_second_assign_keeps_old_cases_and_emits_only_new_ones(self):
        first_ids = case_ids(5)
        self.write_cases(first_ids)
        self.assertEqual(self.init(), 0)
        self.assertEqual(self.assign(), 0)
        before = json.loads(json.dumps(self.manifest()['cases']))
        self.assertEqual(len(self.rows('split.assigned')), 5)

        all_ids = case_ids(7)
        self.write_cases(all_ids)
        self.assertEqual(self.assign(), 0)
        after = self.manifest()['cases']
        self.assertEqual(len(after), 7)
        for cid, entry in before.items():
            self.assertEqual(after[cid], entry)

        emitted = self.rows('split.assigned')
        self.assertEqual(len(emitted), 7)
        self.assertEqual({row['data']['case_id'] for row in emitted[5:]}, set(all_ids[5:]))

    def test_assign_without_a_manifest_exits_2_and_writes_nothing(self):
        self.write_cases(case_ids(3))
        self.assertEqual(self.assign(), 2)
        self.assertFalse(self.manifest_path().exists())
        self.assertEqual(self.rows('split.assigned'), [])

    # ---- retire ----

    def test_retire_moves_a_heldout_case_to_regression(self):
        ids = case_ids()
        self.write_cases(ids)
        self.assertEqual(self.init(), 0)
        self.assertEqual(self.assign(), 0)
        data = self.manifest()
        heldout = sorted(cid for cid, entry in data['cases'].items() if entry['split'] == 'heldout')
        self.assertTrue(heldout)
        cid = heldout[0]

        self.assertEqual(self.run_splits('retire', '--case', cid, '--reason', 'read-heldout',
                                         '--dir', str(self.dir), '--home', str(self.home)), 0)
        retired = self.manifest()
        self.assertEqual(retired['cases'][cid]['split'], 'regression')
        emitted = self.rows('split.retired')
        self.assertEqual(len(emitted), 1)
        row = emitted[0]
        self.assertEqual(row['data'], {'case_id': cid, 'split': 'regression',
                                       'split_set_id': retired['split_set_id'],
                                       'reason': 'read-heldout'})
        self.assertNotIn('dropped', row)
        self.assertEqual(row['split'], 'regression')
        self.assertEqual(row['split_set_id'], retired['split_set_id'])
        self.assertEqual(row['pbi'], cid.rsplit(':', 1)[-1])

    def test_retire_of_a_train_case_exits_2_and_changes_nothing(self):
        ids = case_ids()
        self.write_cases(ids)
        self.assertEqual(self.init(), 0)
        self.assertEqual(self.assign(), 0)
        data = self.manifest()
        train = sorted(cid for cid, entry in data['cases'].items() if entry['split'] == 'train')
        self.assertTrue(train)
        path = self.manifest_path()
        before = path.read_bytes()
        self.assertEqual(self.run_splits('retire', '--case', train[0], '--reason', 'manual',
                                         '--dir', str(self.dir), '--home', str(self.home)), 2)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.rows('split.retired'), [])

    def test_retire_without_dir_still_updates_the_manifest(self):
        ids = case_ids()
        self.write_cases(ids)
        self.assertEqual(self.init(), 0)
        self.assertEqual(self.assign(), 0)
        data = self.manifest()
        heldout = sorted(cid for cid, entry in data['cases'].items() if entry['split'] == 'heldout')
        self.assertTrue(heldout)
        self.assertEqual(self.run_splits('retire', '--case', heldout[0],
                                         '--home', str(self.home)), 0)
        self.assertEqual(self.manifest()['cases'][heldout[0]]['split'], 'regression')
        self.assertEqual(self.rows('split.retired'), [])

    def test_retire_before_assign_moves_a_predicted_heldout_case_to_regression(self):
        # rework-3: assign 前でも、予測が heldout の PBI-<n> は regression に移せる
        cid = 'repo:repo:main:W-1:PBI-1'
        self.assertEqual(self.run_splits('init', '--name', 'default', '--seed', 's1',
                                         '--heldout-ratio', '1.0', '--home', str(self.home)), 0)
        self.assertEqual(splits.split_in(self.manifest(), cid), 'heldout')
        self.assertEqual(self.run_splits('retire', '--case', cid, '--reason', 'read-heldout',
                                         '--home', str(self.home)), 0)
        retired = self.manifest()
        self.assertEqual(retired['cases'][cid],
                         {'split': 'regression',
                          'strata': {'task_type': 'unknown', 'domain': 'unknown',
                                     'size_bucket': 'unknown'},
                          'pbi': 'PBI-1'})
        self.assertEqual(splits.split_in(retired, cid), 'regression')
        self.assertEqual(splits.split_of(cid, home=self.home),
                         ('regression', retired['split_set_id']))
        self.assertEqual(self.rows('split.retired'), [])  # --dir が無いのでイベントは出さない

        # あとで assign しても、振り済みなので regression のまま
        self.write_cases([cid])
        self.assertEqual(self.assign(), 0)
        self.assertEqual(self.manifest()['cases'][cid]['split'], 'regression')
        self.assertEqual(self.rows('split.assigned'), [])

    def test_retire_before_assign_takes_pbi_and_strata_from_dir(self):
        cid = 'repo:repo:main:W-1:PBI-1'
        case = {'schema': 'atelier.case/1', 'id': cid, 'pbi': 'PBI-9'}
        (self.dir / 'cases.jsonl').write_text(json.dumps(case) + '\n', encoding='utf-8')
        self.write_events([self.profile(cid, 'bugfix', 'skill', '4-up')])
        self.assertEqual(self.run_splits('init', '--name', 'default', '--seed', 's1',
                                         '--heldout-ratio', '1.0', '--home', str(self.home)), 0)
        self.assertEqual(self.run_splits('retire', '--case', cid, '--reason', 'read-heldout',
                                         '--dir', str(self.dir), '--home', str(self.home)), 0)
        entry = self.manifest()['cases'][cid]
        self.assertEqual(entry, {'split': 'regression',
                                 'strata': {'task_type': 'bugfix', 'domain': 'skill',
                                            'size_bucket': '4-up'},
                                 'pbi': 'PBI-9'})
        rows = self.rows('split.retired')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['data'], {'case_id': cid, 'split': 'regression',
                                           'split_set_id': self.manifest()['split_set_id'],
                                           'reason': 'read-heldout'})
        self.assertEqual(rows[0]['pbi'], 'PBI-9')

        self.assertEqual(self.assign(), 0)
        self.assertEqual(self.manifest()['cases'][cid]['split'], 'regression')
        self.assertEqual(self.rows('split.assigned'), [])

    def test_retire_before_assign_of_a_predicted_train_case_exits_2(self):
        cid = 'repo:repo:main:W-1:PBI-1'
        self.assertEqual(self.run_splits('init', '--name', 'default', '--seed', 's1',
                                         '--heldout-ratio', '0.0', '--home', str(self.home)), 0)
        path = self.manifest_path()
        before = path.read_bytes()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = splits.main(['retire', '--case', cid, '--reason', 'read-heldout',
                                '--home', str(self.home)])
        self.assertEqual(code, 2)
        self.assertIn(f'not heldout: {cid}', err.getvalue())
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.rows('split.retired'), [])

    def test_retire_before_assign_rejects_pseudo_and_malformed_ids(self):
        self.assertEqual(self.run_splits('init', '--name', 'default', '--seed', 's1',
                                         '--heldout-ratio', '1.0', '--home', str(self.home)), 0)
        path = self.manifest_path()
        before = path.read_bytes()
        for cid in ('repo:repo:main:W-1:run', 'not-a-case', 'repo:repo:main:W-1:PBI-1 x'):
            with self.subTest(cid=cid):
                self.assertEqual(self.run_splits('retire', '--case', cid, '--reason',
                                                 'read-heldout', '--home', str(self.home)), 2)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.rows('split.retired'), [])

    # ---- split_of ----

    def test_split_of_returns_the_stored_split_and_the_seeded_split_for_unknown(self):
        ids = case_ids()
        self.write_cases(ids)
        self.assertEqual(self.init(), 0)
        self.assertEqual(self.assign(), 0)
        data = self.manifest()
        cid = ids[0]
        self.assertEqual(splits.split_of(cid, home=self.home),
                         (data['cases'][cid]['split'], data['split_set_id']))

        # manifest にまだ無い case にも、assign が入れるのと同じ split を先に返す (manifest は書かない)
        unknown = 'atelier-metrics:atelier-metrics:x:W-9:PBI-999'
        digest = hashlib.sha256(f'{data["seed"]}:{unknown}'.encode('utf-8')).hexdigest()
        seeded = 'heldout' if int(digest, 16) / 16 ** 64 < data['heldout_ratio'] else 'train'
        self.assertEqual(splits.split_of(unknown, home=self.home),
                         (seeded, data['split_set_id']))
        self.assertNotIn(unknown, self.manifest()['cases'])

        self.write_cases(ids + [unknown])
        self.assertEqual(self.assign(), 0)
        self.assertEqual(self.manifest()['cases'][unknown]['split'], seeded)
        self.assertEqual(splits.split_of(unknown, home=self.home),
                         (seeded, data['split_set_id']))

        self.assertIsNone(splits.split_of(cid, home=self.other))  # manifest が無い

    def test_split_of_gives_none_for_the_run_pseudo_case(self):
        # rework-2 の短い repro: heldout_ratio 1.0 でも `…:run` は None、`PBI-<n>` は予測のまま
        self.assertEqual(self.run_splits('init', '--name', 'default', '--seed', 's1',
                                         '--heldout-ratio', '1.0', '--home', str(self.home)), 0)
        set_id = self.manifest()['split_set_id']
        self.assertIsNone(splits.split_of('repo:repo:main:W-1:run', home=self.home))
        self.assertEqual(splits.split_of('repo:repo:main:W-1:PBI-1', home=self.home),
                         ('heldout', set_id))

    def test_split_in_is_the_single_rule_for_stored_and_unassigned_cases(self):
        ids = case_ids(5)
        new = 'atelier-metrics:atelier-metrics:claude/x:W-11:PBI-999'
        self.write_cases(ids + [new])
        self.assertEqual(self.run_splits('init', '--name', 'default', '--seed', 's1',
                                         '--heldout-ratio', '1.0', '--home', str(self.home)), 0)
        before = self.manifest()
        self.assertEqual(before['cases'], {})
        # manifest に無い `PBI-<n>` の id は assign が後で入れるのと同じ split
        self.assertEqual(splits.split_in(before, new), 'heldout')
        self.assertIsNone(splits.split_in(before, 'r:main:W-1:run'))
        self.assertIsNone(splits.split_in(before, 'not-a-case'))

        self.assertEqual(self.assign(), 0)
        after = self.manifest()
        for cid in ids + [new]:
            self.assertEqual(splits.split_in(after, cid), 'heldout')
            self.assertEqual(after['cases'][cid]['split'], 'heldout')

        # retire した case は regression のまま
        self.assertEqual(self.run_splits('retire', '--case', ids[0],
                                         '--home', str(self.home)), 0)
        retired = self.manifest()
        self.assertEqual(splits.split_in(retired, ids[0]), 'regression')

        # 壊れた manifest dict でも投げず None
        self.assertIsNone(splits.split_in(None, new))
        self.assertIsNone(splits.split_in('broken', new))
        self.assertIsNone(splits.split_in({}, new))

    def test_is_heldout_agrees_with_split_in(self):
        ids = case_ids(5)
        self.write_cases(ids)
        self.assertEqual(self.run_splits('init', '--name', 'default', '--seed', 's1',
                                         '--heldout-ratio', '1.0', '--home', str(self.home)), 0)
        self.assertEqual(self.assign(), 0)
        manifest = self.manifest()
        for cid in ids:
            self.assertEqual(splits.is_heldout(manifest, cid),
                             splits.split_in(manifest, cid) == 'heldout')
            self.assertTrue(splits.is_heldout(manifest, cid))
        # ratio 1.0 でも `…:run` は held-out ではない
        self.assertFalse(splits.is_heldout(manifest, 'r:main:W-1:run'))
        self.assertFalse(splits.is_heldout({}, ids[0]))

    def test_pbi_attempted_carries_the_split_that_assign_later_stores(self):
        cid = 'r:main:W-1:PBI-1'
        self.assertEqual(self.run_splits('init', '--name', 'default', '--seed', 's',
                                         '--heldout-ratio', '0.5', '--home', str(self.home)), 0)
        created = events.emit(self.dir, 'pbi.attempted',
                              {'case_id': cid, 'attempt': 1, 'outcome': 'pass'}, pbi='PBI-1')
        self.assertTrue(created)
        self.write_cases([cid])
        self.assertEqual(self.assign(), 0)

        attempted = self.rows('pbi.attempted')
        assigned = self.rows('split.assigned')
        self.assertEqual(len(attempted), 1)
        self.assertEqual(len(assigned), 1)
        self.assertNotIn('dropped', attempted[0])
        self.assertNotIn('dropped', assigned[0])
        self.assertEqual(attempted[0]['split'], assigned[0]['split'])
        self.assertEqual(attempted[0]['split_set_id'], assigned[0]['split_set_id'])
        self.assertEqual(assigned[0]['data']['reason'], 'seeded')

    def test_split_of_returns_none_for_a_broken_manifest(self):
        path = self.manifest_path(self.other)
        path.parent.mkdir(parents=True)
        path.write_text('{not json', encoding='utf-8')
        self.assertIsNone(splits.load_manifest('default', self.other))
        self.assertIsNone(splits.split_of(case_ids(1)[0], home=self.other))

    # ---- events.py の委譲 ----

    def test_events_cli_delegates_split_init_and_assign(self):
        proc = self.run_events('split', 'init', '--name', 'default', '--seed', SEED,
                               '--home', str(self.home))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        path = self.manifest_path()
        before = path.read_bytes()
        self.assertEqual(self.manifest()['split_set_id'],
                         'default-' + hashlib.sha256(SEED.encode('utf-8')).hexdigest()[:8])

        again = self.run_events('split', 'init', '--name', 'default', '--seed', SEED,
                                '--home', str(self.home))
        self.assertEqual(again.returncode, 2)
        self.assertEqual(path.read_bytes(), before)

        ids = case_ids(3)
        self.write_cases(ids)
        assigned = self.run_events('split', 'assign', '--dir', str(self.dir),
                                   '--home', str(self.home))
        self.assertEqual(assigned.returncode, 0, assigned.stderr)
        self.assertEqual(set(self.manifest()['cases']), set(ids))
        self.assertEqual(len(self.rows('split.assigned')), 3)


if __name__ == '__main__':
    unittest.main()
