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


def row(report, attempt, outcome, rework=0, cause=None, missed=None, worker='abc123def456'):
    return {'schema': 1, 'recorded_at': f'2026-10-30T10:0{attempt + rework}:00+09:00', 'repo': 'app',
            'work_key': 'app:main:W-003', 'pbi': 'PBI-002', 'report': report, 'attempt': attempt, 'rework': rework,
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

    def run_export(self):
        env = {**os.environ, 'PBI_RUNS_ARCHIVE': str(self.archive)}
        return subprocess.run([sys.executable, EXPORT], cwd=self.repo, capture_output=True, text=True,
                              encoding='utf-8', env=env)

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

    def test_missing_runs_exits_2(self):
        self.assertEqual(self.run_export().returncode, 2)


if __name__ == '__main__':
    unittest.main()
