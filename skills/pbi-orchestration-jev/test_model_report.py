"""model_report.py (runs.jsonl のモデル別の集計) の CLI テスト。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'model_report.py')
DS = 'opencode/deepseek-v4.1-flash-free'
GLM = 'opencode/glm-5.3-flash-free'
GO = 'opencode-go/deepseek-v4.1-flash'


def row(pbi, report, attempt, rework, model, outcome, total=100, cost=0.0, api=None, seconds=60, repo='r1',
        runner='opencode', effort='max'):
    return {'schema': 1, 'repo': repo, 'pbi': pbi, 'report': report, 'attempt': attempt, 'rework': rework,
            'tier': 'haiku', 'runner': runner, 'model': model, 'effort': effort,
            'tokens': {'total': total}, 'cost_reported': cost, 'cost_api_equiv': api,
            'time': {'seconds': seconds}, 'judged': {'outcome': outcome, 'findings': None}}


ROWS = [
    # PBI-001: GLM が一発合格 → GLM に A
    row('PBI-001', 'PBI-001', 1, 0, GLM, '合格'),
    # PBI-002: DS は使えず、GLM が差し戻し 1 回で合格 → GLM に B。DS は評価なし
    row('PBI-002', 'PBI-002', 1, 0, DS, '使えず', total=None, cost=None, seconds=None),
    row('PBI-002', 'PBI-002', 2, 0, GLM, '差し戻し'),
    row('PBI-002', 'PBI-002.rework-1', 1, 1, GLM, '合格'),
    # PBI-003: DS が差し戻し 2 回で合格 → DS に C
    row('PBI-003', 'PBI-003', 1, 0, DS, '差し戻し'),
    row('PBI-003', 'PBI-003.rework-1', 1, 1, DS, '差し戻し'),
    row('PBI-003', 'PBI-003.rework-2', 1, 2, DS, '合格'),
    # PBI-004: GO が昇格 → GO に D (昇格後の agent は評価を受けない)
    row('PBI-004', 'PBI-004', 1, 0, GO, '昇格', cost=0.01),
    row('PBI-004', 'PBI-004.rework-1', 1, 1, 'sonnet', '合格', runner='agent', effort='high', cost=None,
        api=0.5),
]


def run(paths, *extra):
    r = subprocess.run([sys.executable, SCRIPT, *map(str, paths), *extra], capture_output=True, text=True,
                       encoding='utf-8')
    return r


class ModelReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / 'runs.jsonl'
        self.path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in ROWS), encoding='utf-8')

    def tearDown(self):
        self.tmp.cleanup()

    def report(self, *paths):
        r = run(paths or [self.path], '--json')
        self.assertEqual(r.returncode, 0, r.stderr)
        return {m['label']: m for m in json.loads(r.stdout)['models']}

    def test_grades_go_to_the_first_model_that_delivered(self):
        m = self.report()
        self.assertEqual(m[f'opencode {GLM} max']['grades'], {'A': 1, 'B': 1, 'C': 0, 'D': 0})
        self.assertEqual(m[f'opencode {DS} max']['grades'], {'A': 0, 'B': 0, 'C': 1, 'D': 0})
        self.assertEqual(m[f'opencode {GO} max']['grades'], {'A': 0, 'B': 0, 'C': 0, 'D': 1})
        self.assertEqual(m['agent sonnet high']['grades'], {'A': 0, 'B': 0, 'C': 0, 'D': 0})

    def test_counts_unavailable_rate_and_rework_average(self):
        m = self.report()
        ds = m[f'opencode {DS} max']
        self.assertEqual(ds['attempts'], 4)
        self.assertEqual(ds['outcomes'], {'使えず': 1, '差し戻し': 2, '合格': 1})
        self.assertAlmostEqual(ds['unavailable_rate'], 0.25)
        self.assertEqual(m[f'opencode {GLM} max']['rework_avg'], 0.5)
        self.assertEqual(ds['rework_avg'], 2.0)

    def test_tokens_costs_and_time_skip_nulls(self):
        m = self.report()
        ds = m[f'opencode {DS} max']
        self.assertEqual(ds['tokens_total'], 300)
        self.assertEqual(ds['tokens_known'], 3)
        self.assertEqual(ds['cost_reported'], 0.0)
        self.assertEqual(ds['seconds_avg'], 60)
        self.assertEqual(m['agent sonnet high']['cost_api_equiv'], 0.5)
        self.assertIsNone(m[f'opencode {GLM} max']['cost_api_equiv'])

    def test_archive_reads_every_runs_file(self):
        other = self.dir / 'archive' / 'r2' / 'main__W-001'
        other.mkdir(parents=True)
        (other / 'runs.jsonl').write_text(json.dumps(row('PBI-001', 'PBI-001', 1, 0, GLM, '合格', repo='r2'))
                                          + '\n', encoding='utf-8')
        first = self.dir / 'archive' / 'r1' / 'main__W-001'
        first.mkdir(parents=True)
        (first / 'runs.jsonl').write_text(self.path.read_text(encoding='utf-8'), encoding='utf-8')
        r = run([], '--archive', str(self.dir / 'archive'), '--json')
        self.assertEqual(r.returncode, 0, r.stderr)
        models = {m['label']: m for m in json.loads(r.stdout)['models']}
        self.assertEqual(models[f'opencode {GLM} max']['grades']['A'], 2)

    def test_markdown_table(self):
        r = run([self.path])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('| モデル |', r.stdout)
        self.assertIn(GLM, r.stdout)
        self.assertIn('仮の評価', r.stdout)


if __name__ == '__main__':
    unittest.main()
