"""export_cases.py (判定を終えた PBI を正規化した case にして書く) の CLI テスト。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import export_cases
import record_run

HERE = os.path.dirname(os.path.abspath(__file__))
EXPORT = os.path.join(HERE, 'export_cases.py')

PBI = """# PBI-002: 一覧に並べ替えを足す

- Model: sonnet
- Depends on: PBI-001

## Goal
一覧を日付で並べ替えられるようにする。

## Targets
- `src/list/sort.ts` の sortByDate
- src/list/view.tsx

## Do not touch
- src/api/

## Acceptance criteria
1. 検証 1 が 0 で終わる
2. 検証 2 が 1 以上を出す

## Verify commands
1. `pnpm vitest run src/list`
2. `grep -c sortByDate src/list/view.tsx`

## Refs
- mem-1: 日付は UTC で持つ
- `mem-2`: 並べ替えは安定ソート

## Notes
none
"""

REWORK = """## Rework 1: PBI-002

- Criterion 1: fail
  - Now: 同じ日付の順が入れ替わる
  - Expected: 同じ日付は元の順のまま
  - Repro: `node docs/pbi/tools/stable.mjs`
"""

PBI_B = """# PBI-003: 一覧の余白をそろえる

## Goal
一覧の余白を 8px にそろえる。

## Targets
- `src/list/view.tsx`

## Acceptance criteria
1. 検証 1 が 0 で終わる

## Verify commands
1. `pnpm vitest run src/list`

## Notes
none
"""


def row(report, attempt, outcome, rework=0, cause=None, missed=None, worker='abc123def456', pbi='PBI-002'):
    return {'schema': 1, 'recorded_at': f'2026-10-30T10:0{attempt + rework}:00+09:00', 'repo': 'app',
            'work_key': 'app:main:W-003', 'pbi': pbi, 'report': report, 'attempt': attempt, 'rework': rework,
            'tier': 'sonnet', 'runner': 'agent', 'model': 'sonnet', 'effort': 'high',
            'tokens': {'total': 100}, 'verify': {'passed': 1, 'total': 2, 'codes': [1, 0]},
            'unchanged': {'checked': False, 'violation': None},
            'memory': {'violation': False, 'missed_refs': missed}, 'prompt': {'worker': worker},
            'base_commit': 'c0ffee', 'judged': {'outcome': outcome, 'findings': None, 'cause': cause},
            'archive': {'dir': '/archive/x', 'ok': True}}


class ExportCasesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / 'app'
        self.pbi = self.repo / 'docs' / 'pbi'
        self.pbi.mkdir(parents=True)
        (self.repo / 'src' / 'list').mkdir(parents=True)
        (self.repo / 'CLAUDE.md').write_text('root rules\n', encoding='utf-8')
        (self.repo / 'src' / 'list' / 'CLAUDE.md').write_text('list rules\n', encoding='utf-8')
        (self.repo / 'src' / 'CLAUDE.md').write_text('src rules\n', encoding='utf-8')
        (self.pbi / 'PBI-002.md').write_text(PBI, encoding='utf-8')
        (self.pbi / 'PBI-002.rework-1.md').write_text(REWORK, encoding='utf-8')
        (self.pbi / 'PBI-002.rework-1.brief.md').write_text('brief', encoding='utf-8')
        (self.pbi / 'worker.md').write_text('# How to work\n', encoding='utf-8')
        self.archive = self.tmp / 'archive'

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_rows(self, rows):
        (self.pbi / 'runs.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows),
                                             encoding='utf-8')

    def run_export(self, *args):
        env = {**os.environ, 'PBI_RUNS_ARCHIVE': str(self.archive)}
        return subprocess.run([sys.executable, EXPORT, *args], cwd=self.repo, capture_output=True, text=True,
                              encoding='utf-8', env=env)

    def write_manifest(self, home, splits, name='default'):
        path = Path(home) / 'splits' / f'{name}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        cases = {case_id: {'split': split,
                           'strata': {'task_type': 'unknown', 'domain': 'unknown', 'size_bucket': 'unknown'}}
                 for case_id, split in splits.items()}
        path.write_text(json.dumps({'schema': 'atelier.split/1', 'name': name, 'split_set_id': 'default-abc123',
                                    'seed': '42', 'heldout_ratio': 0.2,
                                    'strata': ['task_type', 'domain', 'size_bucket'], 'frozen': True,
                                    'cases': cases}, ensure_ascii=False), encoding='utf-8')

    def cases(self, path=None):
        path = path or self.pbi / 'cases.jsonl'
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]

    def test_sent_back_pbi_is_normalized_by_kind(self):
        self.write_rows([row('PBI-002', 1, '差し戻し', cause=['spec'], missed=['mem-2']),
                         row('PBI-002.rework-1', 1, '合格', rework=1, missed=[])])
        r = self.run_export()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('cases: 1 件 (差し戻しあり 1 件)', r.stdout)
        [case] = self.cases()
        self.assertEqual(case['schema'], export_cases.CASE_SCHEMA)
        self.assertEqual((case['id'], case['base_commit']), ('app:app:main:W-003:PBI-002', 'c0ffee'))
        harness = case['harness']
        self.assertEqual(harness['system_prompt']['text'], '# How to work\n')
        self.assertEqual(harness['system_prompt']['version'], record_run.file_version(self.pbi / 'worker.md'))
        self.assertEqual([g['path'] for g in harness['guardrails']],
                         ['CLAUDE.md', 'src/CLAUDE.md', 'src/list/CLAUDE.md'])
        self.assertTrue(all(g['kind'] == 'convention' and len(g['version']) == 12 for g in harness['guardrails']))
        self.assertEqual(harness['memory'], [{'id': 'mem-1', 'gist': '日付は UTC で持つ', 'fetched': True},
                                             {'id': 'mem-2', 'gist': '並べ替えは安定ソート', 'fetched': False}])
        task = case['task']
        self.assertEqual(task['title'], 'PBI-002: 一覧に並べ替えを足す')
        self.assertEqual(task['depends_on'], ['PBI-001'])
        self.assertEqual(task['verify'], [{'n': 1, 'command': 'pnpm vitest run src/list'},
                                          {'n': 2, 'command': 'grep -c sortByDate src/list/view.tsx'}])
        self.assertEqual([a['n'] for a in task['acceptance']], [1, 2])
        self.assertEqual(task['do_not_touch'], '- src/api/')
        result = case['result']
        self.assertEqual((result['first_pass'], result['passed'], result['sent_back']), (False, True, True))
        self.assertEqual([a['outcome'] for a in result['attempts']], ['差し戻し', '合格'])
        self.assertEqual(result['attempts'][0]['cause'], ['spec'])
        self.assertEqual(case['private']['rework'], [{'note': 'PBI-002.rework-1.md', 'items': [
            {'kind': 'expected', 'text': '同じ日付は元の順のまま'},
            {'kind': 'repro', 'text': '`node docs/pbi/tools/stable.mjs`'}]}])
        self.assertEqual(self.cases(self.archive / 'app' / 'main__W-003' / 'cases.jsonl'), [case])

    def test_size_time_and_tokens(self):
        (self.pbi / 'PBI-002.facts.json').write_text(json.dumps(
            {'files_changed': 2, 'files_new': 0, 'files_deleted': 0, 'modules': 1, 'criteria': 2,
             'sensitive': False, 'extra': 'x'}), encoding='utf-8')
        first = row('PBI-002', 1, '差し戻し', cause=['spec'])
        first.update(time={'seconds': 300}, steps=20, cost_api_equiv=0.5, input={'pbi_chars': 1234},
                     tokens={'input': 60, 'output': 30, 'reasoning': None, 'cache_read': 10, 'cache_write': 0,
                             'total': 100})
        second = row('PBI-002.rework-1', 1, '合格', rework=1)
        second.update(time={'seconds': 120}, steps=None, cost_reported=0.2, cost_api_equiv=0.9)
        self.write_rows([first, second])
        self.run_export()
        [case] = self.cases()
        size = case['size']
        self.assertEqual(size['facts'], {'files_changed': 2, 'files_new': 0, 'files_deleted': 0, 'modules': 1,
                                         'criteria': 2, 'sensitive': False})
        self.assertEqual((size['tier'], size['targets'], size['acceptance'], size['verify'], size['refs']),
                         ('sonnet', 2, 2, 2, 2))
        self.assertEqual(size['chars'], {'worker': len('# How to work\n'), 'pbi': 1234,
                                         'refs': len('日付は UTC で持つ') + len('並べ替えは安定ソート')})
        a = case['result']['attempts'][0]
        self.assertEqual((a['seconds'], a['steps'], a['cost'], a['tokens']), (300, 20, 0.5, 100))
        self.assertEqual(a['token_parts'], {'input': 60, 'output': 30, 'reasoning': None, 'cache_read': 10,
                                            'cache_write': 0})
        self.assertEqual(case['result']['attempts'][1]['cost'], 0.2)
        self.assertEqual(case['result']['totals'], {
            'seconds': 420, 'steps': 20, 'tokens': 200, 'cost': 0.7, 'attempts': 2,
            'unknown': {'seconds': 0, 'steps': 1, 'tokens': 0, 'cost': 0}})

    def test_target_paths_are_whole_paths(self):
        self.assertEqual(export_cases.target_paths(
            '- Makefile\n- `src/routes/api/recipes/$id.ts` の loader\n- src/a.ts: 並べ替え\n- src/a.ts\n- none\n'),
            ['Makefile', 'src/a.ts', 'src/routes/api/recipes/$id.ts'])

    def test_unavailable_only_pbi_is_left_out(self):
        self.write_rows([row('PBI-002', 1, '使えず')])
        r = self.run_export()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.cases(), [])

    def test_unknown_fetch_is_null(self):
        self.write_rows([row('PBI-002', 1, '合格', missed=None)])
        self.run_export()
        [case] = self.cases()
        self.assertEqual([m['fetched'] for m in case['harness']['memory']], [None, None])
        self.assertTrue(case['result']['first_pass'])
        self.assertIsNone(case['size']['facts'])
        self.assertEqual(case['size']['chars']['pbi'], len(PBI))
        self.assertEqual(case['result']['totals']['unknown']['seconds'], 1)

    def test_missing_runs_exits_2(self):
        self.assertEqual(self.run_export().returncode, 2)

    def test_improver_view_reduces_only_heldout_cases(self):
        (self.pbi / 'PBI-003.md').write_text(PBI_B, encoding='utf-8')
        self.write_rows([row('PBI-002', 1, '差し戻し', cause=['spec']),
                         row('PBI-003', 1, '合格', pbi='PBI-003')])
        home = self.tmp / 'home'
        self.write_manifest(home, {'app:app:main:W-003:PBI-002': 'heldout',
                                   'app:app:main:W-003:PBI-003': 'train'})
        self.assertEqual(self.run_export().returncode, 0)
        owner = {case['id']: case for case in self.cases()}
        r = self.run_export('--view', 'improver', '--home', str(home))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, '')
        path = self.pbi / 'cases.improver.jsonl'
        text = path.read_text(encoding='utf-8')
        shown = {case['id']: case for case in self.cases(path)}
        heldout = shown['app:app:main:W-003:PBI-002']
        self.assertEqual(set(heldout), {'schema', 'id', 'size', 'result'})
        self.assertEqual(set(heldout['result']), {'passed'})
        self.assertEqual(heldout['result']['passed'],
                         owner['app:app:main:W-003:PBI-002']['result']['passed'])
        self.assertEqual(heldout['size'], owner['app:app:main:W-003:PBI-002']['size'])
        self.assertEqual(text.count('一覧を日付で並べ替えられるようにする。'), 0)
        self.assertEqual(text.count('同じ日付は元の順のまま'), 0)
        self.assertEqual(text.count('node docs/pbi/tools/stable.mjs'), 0)
        self.assertEqual(shown['app:app:main:W-003:PBI-003'], owner['app:app:main:W-003:PBI-003'])
        self.assertEqual(self.cases(self.archive / 'app' / 'main__W-003' / 'cases.improver.jsonl'),
                         list(shown.values()))

    def test_default_view_writes_only_owner_cases(self):
        self.write_rows([row('PBI-002', 1, '合格')])
        r = self.run_export()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.pbi / 'cases.jsonl').is_file())
        self.assertFalse((self.pbi / 'cases.improver.jsonl').exists())

    def test_improver_without_manifest_falls_back_to_owner_rows(self):
        self.write_rows([row('PBI-002', 1, '合格')])
        self.assertEqual(self.run_export().returncode, 0)
        owner = self.cases()
        r = self.run_export('--view', 'improver', '--home', str(self.tmp / 'no-home'))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.cases(self.pbi / 'cases.improver.jsonl'), owner)
        self.assertEqual(len(r.stderr.strip().splitlines()), 1)
        self.assertIn('分割が無い', r.stderr)


    def test_improver_stops_when_the_manifest_exists_but_is_broken(self):
        """manifest のファイルがあるのに読めないときは、held-out の本文を出さないよう exit 2 で止める。"""
        self.write_rows([row('PBI-002', 1, '合格')])
        home = self.tmp / 'home-broken'
        (home / 'splits').mkdir(parents=True)
        (home / 'splits' / 'default.json').write_text('{"schema": "harness.split', encoding='utf-8')
        r = self.run_export('--view', 'improver', '--home', str(home))
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn('manifest', r.stderr)
        self.assertFalse((self.pbi / 'cases.improver.jsonl').exists())

if __name__ == '__main__':
    unittest.main()
