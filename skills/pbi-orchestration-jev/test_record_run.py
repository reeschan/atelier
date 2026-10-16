"""record_run.py (1 回の実行の記録と生ログの保全) と verify.py の終了コードの json の CLI テスト。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
RECORD = os.path.join(HERE, 'record_run.py')
VERIFY = os.path.join(HERE, 'verify.py')

DS_FREE = {'runner': 'opencode', 'model': 'opencode/deepseek-v4.1-flash-free', 'effort': 'max'}
GLM_FREE = {'runner': 'opencode', 'model': 'opencode/glm-5.3-flash-free', 'effort': 'max'}
CODEX = {'runner': 'codex', 'model': 'gpt-6-astra', 'effort': 'high'}
AGENT = {'runner': 'agent', 'model': 'sonnet', 'effort': 'high'}


def project_dir_name(cwd):
    return ''.join(c if c.isalnum() else '-' for c in str(cwd))


def step_finish(ts, inp, out, reasoning, read, write, cost):
    return {'type': 'step_finish', 'timestamp': ts, 'part': {
        'type': 'step-finish', 'cost': cost,
        'tokens': {'input': inp, 'output': out, 'reasoning': reasoning, 'cache': {'read': read, 'write': write}}}}


def assistant(ts, rid, model, inp, out, read, w5, w1):
    return {'type': 'assistant', 'timestamp': ts, 'requestId': rid, 'message': {
        'model': model, 'usage': {'input_tokens': inp, 'output_tokens': out, 'cache_read_input_tokens': read,
                                  'cache_creation': {'ephemeral_5m_input_tokens': w5,
                                                     'ephemeral_1h_input_tokens': w1}}}}


class RecordRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / 'myrepo'
        self.pbi = self.repo / 'docs' / 'pbi'
        (self.pbi / 'opencode').mkdir(parents=True)
        (self.pbi / 'verify').mkdir()
        (self.pbi / 'reports').mkdir()
        (self.pbi / 'jev').mkdir()
        self.archive = self.tmp / 'archive'
        self.projects = self.tmp / 'projects'
        (self.pbi / 'STATUS.md').write_text('作業キー: myrepo:feature/x:W-002 / 作業空間: project:work\n',
                                            encoding='utf-8')
        (self.pbi / 'PBI-007.md').write_text('# PBI-007\n', encoding='utf-8')
        (self.pbi / 'PBI-007.facts.json').write_text('{}', encoding='utf-8')
        (self.pbi / 'jev' / 'PBI-007.brief.json').write_text('{}', encoding='utf-8')
        (self.pbi / 'reports' / 'PBI-007.md').write_text('報告', encoding='utf-8')
        self.write_route('PBI-007', [GLM_FREE, DS_FREE, AGENT], tier='haiku')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_route(self, pbi, route, tier='haiku'):
        (self.pbi / f'{pbi}.route.json').write_text(json.dumps({'tier': tier, 'route': route}), encoding='utf-8')

    def write_opencode(self, name, rows):
        with open(self.pbi / 'opencode' / f'{name}.jsonl', 'w', encoding='utf-8') as f:
            f.write('{"type":"step_start","timestamp":1792117900000}\n')
            for r in rows:
                f.write(json.dumps(r) + '\n')
        (self.pbi / 'opencode' / f'{name}.prompt.md').write_text('依頼', encoding='utf-8')
        (self.pbi / 'opencode' / f'{name}.err').write_text('', encoding='utf-8')

    def write_verify(self, name, results):
        (self.pbi / 'verify' / f'{name}.json').write_text(json.dumps({'name': name, 'results': results}),
                                                         encoding='utf-8')
        for r in results:
            (self.pbi / 'verify' / f"{name}.{r['n']}.log").write_text('$ x\n', encoding='utf-8')

    def write_agent(self, description, rows, agent_id='a1', project=None, root=None):
        """project を渡すと別のプロジェクト (フォルダを選ばずに始めたセッションなど) の下に置く。
        root を渡すと、依頼文の先頭に「作業ルート: <root>」を書く。"""
        proj = self.projects / (project or project_dir_name(self.repo.resolve()))
        sub = proj / 'sess1' / 'subagents'
        sub.mkdir(parents=True, exist_ok=True)
        (proj / 'sess1.jsonl').write_text('', encoding='utf-8')
        with open(sub / f'agent-{agent_id}.jsonl', 'w', encoding='utf-8') as f:
            if root is not None:
                f.write(json.dumps({'type': 'user', 'timestamp': '2026-10-16T09:59:00Z', 'message': {
                    'role': 'user', 'content': f'作業ルート: {root}。ここをルートとして作業し、外には書き込まない\n'
                                               'PBI: docs/pbi/PBI-007.md'}}, ensure_ascii=False) + '\n')
            for r in rows:
                f.write(json.dumps(r) + '\n')
        (sub / f'agent-{agent_id}.meta.json').write_text(json.dumps({'description': description}), encoding='utf-8')

    def record(self, name, attempt, outcome, *extra, archive=True, home=None):
        env = {k: v for k, v in os.environ.items() if k != 'PBI_RUNS_ARCHIVE'}
        if home is not None:
            env['HOME'] = env['USERPROFILE'] = str(home)
        elif archive:
            env['PBI_RUNS_ARCHIVE'] = str(self.archive)
        else:
            env['PBI_RUNS_ARCHIVE'] = str(self.tmp / 'missing' / 'x')
        args = [sys.executable, RECORD, name, '--attempt', str(attempt), '--outcome', outcome,
                '--projects', str(self.projects), *extra]
        return subprocess.run(args, cwd=self.repo, capture_output=True, text=True, encoding='utf-8', env=env)

    def rows(self):
        path = self.pbi / 'runs.jsonl'
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]

    def test_opencode_tokens_cost_steps_and_time(self):
        self.write_opencode('PBI-007', [
            step_finish(1792117910000, 100, 20, 5, 1000, 50, 0.0),
            step_finish(1792117970000, 200, 30, 0, 2000, 0, 0.0)])
        self.write_verify('PBI-007', [{'n': 1, 'command': 'pnpm test', 'code': 0},
                                      {'n': 2, 'command': 'python docs/pbi/tools/unchanged.py check PBI-007',
                                       'code': 0},
                                      {'n': 3, 'command': 'pnpm lint', 'code': 1}])
        r = self.record('PBI-007', 1, '合格', '--findings', '0,1,2')
        self.assertEqual(r.returncode, 0, r.stderr)
        [row] = self.rows()
        self.assertEqual((row['runner'], row['model'], row['effort'], row['tier']),
                         ('opencode', GLM_FREE['model'], 'max', 'haiku'))
        self.assertEqual((row['pbi'], row['report'], row['attempt'], row['rework']), ('PBI-007', 'PBI-007', 1, 0))
        self.assertEqual(row['repo'], 'myrepo')
        self.assertEqual(row['work_key'], 'myrepo:feature/x:W-002')
        t = row['tokens']
        self.assertEqual((t['input'], t['output'], t['reasoning'], t['cache_read'], t['cache_write']),
                         (300, 50, 5, 3000, 50))
        self.assertIsNone(t['cache_write_5m'])
        self.assertEqual(t['total'], 3405)
        self.assertEqual(row['steps'], 2)
        self.assertEqual(row['cost_reported'], 0.0)
        self.assertIsNone(row['cost_api_equiv'])
        self.assertEqual(row['time']['seconds'], 70)
        self.assertEqual(row['verify'], {'passed': 2, 'total': 3, 'codes': [0, 0, 1]})
        self.assertEqual(row['unchanged'], {'checked': True, 'violation': False})
        self.assertEqual(row['judged'], {'outcome': '合格', 'findings': {'重大': 0, '中': 1, '軽微': 2}})
        self.assertTrue(row['archive']['ok'])

    def test_raw_logs_are_archived_by_repo_branch_and_work_number(self):
        self.write_opencode('PBI-007', [step_finish(1792117910000, 1, 1, 0, 0, 0, 0.0)])
        self.write_verify('PBI-007', [{'n': 1, 'command': 'x', 'code': 0}])
        r = self.record('PBI-007', 1, '合格')
        self.assertEqual(r.returncode, 0, r.stderr)
        base = self.archive / 'myrepo' / 'feature-x__W-002'
        got = sorted(p.relative_to(base / 'PBI-007' / 'attempt-1').as_posix()
                     for p in (base / 'PBI-007' / 'attempt-1').rglob('*') if p.is_file())
        for expected in ('opencode/PBI-007.jsonl', 'opencode/PBI-007.prompt.md', 'opencode/PBI-007.err',
                         'verify/PBI-007.json', 'verify/PBI-007.1.log', 'PBI-007.md', 'PBI-007.route.json',
                         'PBI-007.facts.json', 'jev/PBI-007.brief.json', 'reports/PBI-007.md'):
            self.assertIn(expected, got)
        self.assertTrue((base / 'runs.jsonl').is_file())
        self.assertEqual(self.rows()[0]['archive']['dir'], (base / 'PBI-007' / 'attempt-1').as_posix())

    def test_unavailable_attempt_then_next_route_step(self):
        self.write_opencode('PBI-007', [])
        self.assertEqual(self.record('PBI-007', 1, '使えず').returncode, 0)
        self.write_opencode('PBI-007', [step_finish(1792117910000, 1, 1, 0, 0, 0, 0.0)])
        self.assertEqual(self.record('PBI-007', 2, '合格').returncode, 0)
        rows = self.rows()
        self.assertEqual([(r['attempt'], r['model'], r['judged']['outcome']) for r in rows],
                         [(1, GLM_FREE['model'], '使えず'), (2, DS_FREE['model'], '合格')])
        self.assertEqual(rows[0]['steps'], 0)

    def test_duplicate_record_is_rejected(self):
        self.write_opencode('PBI-007', [])
        self.assertEqual(self.record('PBI-007', 1, '使えず').returncode, 0)
        r = self.record('PBI-007', 1, '使えず')
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(len(self.rows()), 1)

    def test_agent_rework_transcript_is_matched_by_report_name(self):
        self.write_route('PBI-007', [AGENT], tier='sonnet')
        self.write_agent('PBI-007 実装', [assistant('2026-10-16T10:00:00Z', 'r0', 'claude-sonnet-5-5',
                                                    999, 999, 999, 999, 999)], agent_id='a0')
        self.write_agent('PBI-007.rework-1 差し戻し', [
            assistant('2026-10-16T10:10:00Z', 'r1', 'claude-sonnet-5-5', 10, 20, 100, 0, 50),
            assistant('2026-10-16T10:11:40Z', 'r2', 'claude-sonnet-5-5', 0, 10, 100, 0, 0)], agent_id='a1')
        (self.pbi / 'PBI-007.rework-1.md').write_text('差し戻し文', encoding='utf-8')
        r = self.record('PBI-007.rework-1', 1, '合格')
        self.assertEqual(r.returncode, 0, r.stderr)
        [row] = self.rows()
        self.assertEqual((row['report'], row['rework'], row['runner'], row['model']),
                         ('PBI-007.rework-1', 1, 'agent', 'sonnet'))
        t = row['tokens']
        self.assertEqual((t['input'], t['output'], t['cache_read'], t['cache_write_5m'], t['cache_write_1h']),
                         (10, 30, 200, 0, 50))
        self.assertIsNone(t['reasoning'])
        self.assertEqual(row['steps'], 2)
        self.assertEqual(row['time']['seconds'], 100)
        self.assertIsNone(row['cost_reported'])
        # sonnet 5.5: input 10*2 + output 30*10 + read 200*0.10 + 1h 50*4 = 540 → 0.00054 USD
        self.assertAlmostEqual(row['cost_api_equiv'], 0.00054, places=9)
        self.assertEqual(row['prices_checked_at'], '2026-10-16')
        arch = Path(row['archive']['dir'])
        self.assertTrue((arch / 'claude' / 'agent-a1.jsonl').is_file())
        self.assertFalse((arch / 'claude' / 'agent-a0.jsonl').exists())
        self.assertTrue((arch / 'PBI-007.rework-1.md').is_file())

    def test_review_and_design_agents_are_not_the_attempt(self):
        self.write_route('PBI-007', [AGENT], tier='sonnet')
        self.write_agent('PBI-007 実装', [assistant('2026-10-16T10:00:00Z', 'r1', 'claude-sonnet-5-5',
                                                    1, 2, 0, 0, 0)], agent_id='impl')
        self.write_agent('PBI-007 review', [assistant('2026-10-16T11:00:00Z', 'r2', 'claude-opus-5-5',
                                                      500, 500, 0, 0, 0)], agent_id='rev')
        self.write_agent('PBI-007.design', [assistant('2026-10-16T12:00:00Z', 'r3', 'claude-opus-5-5',
                                                      700, 700, 0, 0, 0)], agent_id='des')
        self.write_agent('PBI-007 rework-1 以前の書き方', [assistant('2026-10-16T13:00:00Z', 'r4',
                                                                        'claude-sonnet-5-5', 800, 800, 0, 0, 0)],
                         agent_id='old')
        r = self.record('PBI-007', 1, '合格')
        self.assertEqual(r.returncode, 0, r.stderr)
        [row] = self.rows()
        self.assertEqual((row['tokens']['input'], row['tokens']['output']), (1, 2))

    def test_agent_in_another_project_is_found_by_work_root(self):
        self.write_route('PBI-007', [AGENT], tier='sonnet')
        self.write_agent('PBI-007 実装', [assistant('2026-10-16T10:00:00Z', 'r1', 'claude-sonnet-5-5',
                                                    3, 4, 0, 0, 0)], agent_id='mine', project='C--scratch-a',
                         root=str(self.repo.resolve()))
        self.write_agent('PBI-007 実装', [assistant('2026-10-16T11:00:00Z', 'r2', 'claude-sonnet-5-5',
                                                    900, 900, 0, 0, 0)], agent_id='other', project='C--scratch-b',
                         root='C:\\Users\\someone\\other-repo')
        r = self.record('PBI-007', 1, '合格')
        self.assertEqual(r.returncode, 0, r.stderr)
        [row] = self.rows()
        self.assertEqual((row['tokens']['input'], row['tokens']['output']), (3, 4))
        self.assertTrue((Path(row['archive']['dir']) / 'claude' / 'agent-mine.jsonl').is_file())

    def test_codex_has_total_only(self):
        self.write_route('PBI-007', [CODEX, AGENT], tier='sonnet')
        (self.pbi / 'codex').mkdir()
        (self.pbi / 'codex' / 'PBI-007.log').write_text('...\ntokens used\n12,345\n', encoding='utf-8')
        r = self.record('PBI-007', 1, '差し戻し', '--findings', '1,0,0')
        self.assertEqual(r.returncode, 0, r.stderr)
        [row] = self.rows()
        self.assertEqual(row['tokens']['total'], 12345)
        self.assertIsNone(row['tokens']['input'])
        self.assertIsNone(row['cost_api_equiv'])
        self.assertIsNone(row['verify'])
        self.assertEqual(row['unchanged'], {'checked': False, 'violation': None})

    def test_missing_log_gives_nulls_not_zeros(self):
        self.write_route('PBI-007', [{'runner': 'agy', 'model': 'claude-sonnet-5-5-high', 'effort': None}, AGENT])
        r = self.record('PBI-007', 1, '使えず')
        self.assertEqual(r.returncode, 0, r.stderr)
        [row] = self.rows()
        self.assertIsNone(row['tokens']['total'])
        self.assertIsNone(row['steps'])
        self.assertIsNone(row['time']['seconds'])

    def test_archive_defaults_to_home_without_env(self):
        self.write_opencode('PBI-007', [])
        home = self.tmp / 'home'
        r = self.record('PBI-007', 1, '使えず', home=home)
        self.assertEqual(r.returncode, 0, r.stderr)
        [row] = self.rows()
        dest = home / '.claude' / 'pbi-runs' / 'myrepo' / 'feature-x__W-002' / 'PBI-007' / 'attempt-1'
        self.assertEqual(row['archive']['dir'], dest.as_posix())
        self.assertTrue((dest / 'PBI-007.md').is_file())

    def test_archive_failure_does_not_stop(self):
        self.write_opencode('PBI-007', [])
        (self.tmp / 'missing').write_text('ファイルなのでディレクトリを作れない', encoding='utf-8')
        r = self.record('PBI-007', 1, '使えず', archive=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        [row] = self.rows()
        self.assertFalse(row['archive']['ok'])
        self.assertIn('保全', r.stdout)

    def test_failed_copy_of_runs_is_kept_in_the_row(self):
        self.write_opencode('PBI-007', [])
        (self.archive / 'myrepo' / 'feature-x__W-002' / 'runs.jsonl').mkdir(parents=True)
        r = self.record('PBI-007', 1, '使えず')
        self.assertEqual(r.returncode, 0, r.stderr)
        [row] = self.rows()
        self.assertFalse(row['archive']['ok'])
        self.assertIsNotNone(row['archive']['error'])
        self.assertIn('保全に失敗', r.stdout)

    def test_archived_runs_has_only_this_work_key(self):
        self.write_opencode('PBI-007', [])
        self.assertEqual(self.record('PBI-007', 1, '使えず').returncode, 0)
        (self.pbi / 'STATUS.md').write_text('作業キー: myrepo:feature/x:W-003\n', encoding='utf-8')
        self.assertEqual(self.record('PBI-007', 2, '使えず').returncode, 0)

        def archived(group):
            text = (self.archive / 'myrepo' / group / 'runs.jsonl').read_text(encoding='utf-8')
            return [(r['work_key'], r['attempt']) for r in map(json.loads, text.splitlines())]
        self.assertEqual(archived('feature-x__W-002'), [('myrepo:feature/x:W-002', 1)])
        self.assertEqual(archived('feature-x__W-003'), [('myrepo:feature/x:W-003', 2)])
        self.assertEqual(len(self.rows()), 2)

    def test_runner_override_for_promotion(self):
        self.write_agent('PBI-007.rework-1 昇格', [
            assistant('2026-10-16T10:10:00Z', 'r1', 'claude-sonnet-5-5', 1, 1, 0, 0, 0)])
        r = self.record('PBI-007.rework-1', 1, '合格', '--runner', 'agent', '--model', 'sonnet', '--effort', 'high',
                        '--tier', 'sonnet')
        self.assertEqual(r.returncode, 0, r.stderr)
        [row] = self.rows()
        self.assertEqual((row['runner'], row['model'], row['tier']), ('agent', 'sonnet', 'sonnet'))

    def test_bad_outcome_is_rejected(self):
        r = self.record('PBI-007', 1, 'よかった')
        self.assertNotEqual(r.returncode, 0)


class VerifyJsonTest(unittest.TestCase):
    def test_exit_codes_are_written_as_json(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / 'docs' / 'pbi'
            base.mkdir(parents=True)
            (base / 'PBI-003.md').write_text('# PBI-003\n\n## 検証コマンド\n\n1. `exit 0`\n2. `exit 3`\n',
                                             encoding='utf-8')
            r = subprocess.run([sys.executable, VERIFY, 'PBI-003', '--name', 'PBI-003.rework-1'], cwd=d,
                               capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(r.returncode, 0, r.stderr)
            data = json.loads((base / 'verify' / 'PBI-003.rework-1.json').read_text(encoding='utf-8'))
        self.assertEqual(data['name'], 'PBI-003.rework-1')
        self.assertEqual([(x['n'], x['command'], x['code']) for x in data['results']],
                         [(1, 'exit 0', 0), (2, 'exit 3', 3)])

    def test_only_rerun_is_merged_by_number(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / 'docs' / 'pbi'
            base.mkdir(parents=True)
            pbi = base / 'PBI-003.md'

            def verify(codes, *extra):
                cmds = ''.join(f'{i}. `exit {c}`\n' for i, c in enumerate(codes, 1))
                pbi.write_text(f'# PBI-003\n\n## 検証コマンド\n\n{cmds}', encoding='utf-8')
                r = subprocess.run([sys.executable, VERIFY, 'PBI-003', *extra], cwd=d,
                                   capture_output=True, text=True, encoding='utf-8')
                self.assertEqual(r.returncode, 0, r.stderr)
                return json.loads((base / 'verify' / 'PBI-003.json').read_text(encoding='utf-8'))['results']
            verify((0, 3, 0))
            only = verify((0, 0, 0), '--only', '2')
            full = verify((0, 0))
        self.assertEqual([(x['n'], x['code']) for x in only], [(1, 0), (2, 0), (3, 0)])
        self.assertEqual([(x['n'], x['code']) for x in full], [(1, 0), (2, 0)])


if __name__ == '__main__':
    unittest.main()
