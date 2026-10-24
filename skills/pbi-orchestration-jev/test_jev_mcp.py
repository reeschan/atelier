import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import jev_mcp  # noqa: E402

SCRIPT = os.path.join(HERE, 'jev_mcp.py')

ROUTING = {
    'name': 'test-routing',
    'tiers': {
        'haiku': [
            {'runner': 'opencode', 'model': 'opencode/model-a', 'effort': 'max'},
            {'runner': 'opencode', 'model': 'opencode/model-b', 'effort': 'max'},
            {'runner': 'agent', 'model': 'haiku', 'effort': 'max'},
        ],
        'sonnet': [
            {'runner': 'opencode', 'model': 'opencode/model-c', 'effort': 'max'},
            {'runner': 'agent', 'model': 'sonnet', 'effort': 'high'},
        ],
        'opus': [
            {'runner': 'agent', 'model': 'opus', 'effort': None},
        ],
    },
}
PRIORITY = {
    'name': 'prio',
    'description': '優先テンプレートのテスト',
    'tiers': {'haiku': [{'runner': 'codex', 'model': 'model-x', 'effort': 'high'}]},
}
APPLICATION = {'id': 'a-1', 'template': 'prio', 'until': '2999-12-31T00:00:00+09:00', 'on_expire': 'ask',
               'reason': 'テスト', 'started_at': '2026-01-01T00:00:00+09:00', 'started_via': 'chat'}

PBI = '''# PBI-001 ログの日付を ISO 形式にする

## 目的
ログを機械で読めるようにする。

## 対象
- src/log.py

## 受け入れ条件
1. 日付が ISO 8601 で出る

## 検証コマンド
1. `test -f src/log.py`
2. `exit 3`
'''


class JevMcpTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.repo = self.root / 'app'
        pbi = self.repo / 'docs' / 'pbi'
        (pbi / 'mcp').mkdir(parents=True)
        (self.repo / 'src').mkdir()
        (self.repo / 'src' / 'log.py').write_text('x = 1\n', encoding='utf-8')
        (pbi / 'PBI-001.md').write_text(PBI, encoding='utf-8')
        (pbi / 'PBI-001.facts.json').write_text('{"files": ["src/log.py"]}', encoding='utf-8')
        (pbi / 'mcp' / 'PBI-001.prompt.md').write_text('PBI-001 を実装する\n', encoding='utf-8')
        self.tasks = jev_mcp.Tasks([self.root])
        self.server = jev_mcp.Server(self.tasks)
        # route は実際の home や CLI の検出に頼らない (home も CLI も一時ディレクトリに置く)
        self.home = self.root / 'home'
        self.home.mkdir()
        env = mock.patch.dict(os.environ, {'PBI_HOME': str(self.home),
                                           'PBI_RUNS_ARCHIVE': str(self.root / 'archive')})
        env.start()
        self.addCleanup(env.stop)
        self.write_routing()
        self.write_agents()

    def write_routing(self):
        (self.home / 'routing.json').write_text(json.dumps(ROUTING), encoding='utf-8')

    def write_agents(self, runners=('opencode', 'codex', 'agy', 'dsh')):
        (self.home / 'bin').mkdir(exist_ok=True)
        table = {'runners': {}}
        for runner in runners:
            path = self.home / 'bin' / runner
            path.write_text('', encoding='utf-8')
            table['runners'][runner] = {'path': str(path)}
        (self.home / 'agents.json').write_text(json.dumps(table), encoding='utf-8')

    def write_facts(self, **values):
        (self.repo / 'docs/pbi/PBI-001.facts.json').write_text(json.dumps(values), encoding='utf-8')

    def write_priority_template(self):
        (self.home / 'templates').mkdir(exist_ok=True)
        (self.home / 'templates' / 'prio.json').write_text(json.dumps(PRIORITY), encoding='utf-8')
        (self.home / 'overrides.json').write_text(json.dumps({'synced_at': None, 'application': APPLICATION}),
                                                 encoding='utf-8')

    def route(self, **arguments):
        return self.ok('route', repo=str(self.repo), report='PBI-001', **arguments)

    def record(self, **arguments):
        arguments.setdefault('repo', str(self.repo))
        arguments.setdefault('report', 'PBI-001')
        arguments.setdefault('attempt', 1)
        arguments.setdefault('outcome', '合格')
        arguments.setdefault('runner', 'opencode')
        arguments.setdefault('model', 'opencode/model-a')
        return self.ok('record', **arguments)

    def write_runner_log(self, report='PBI-001', cost=0.0123, input_tokens=100, output_tokens=20):
        base = self.repo / 'docs' / 'pbi' / 'opencode'
        base.mkdir(parents=True, exist_ok=True)
        event = {'type': 'step_finish', 'timestamp': 1700000000000,
                 'part': {'tokens': {'input': input_tokens, 'output': output_tokens}, 'cost': cost}}
        (base / f'{report}.jsonl').write_text(json.dumps(event) + '\n', encoding='utf-8')

    def rows(self):
        path = self.repo / 'docs' / 'pbi' / 'runs.jsonl'
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def call(self, tool, **arguments):
        r = self.server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                'params': {'name': tool, 'arguments': arguments}})
        return r['result']

    def ok(self, tool, **arguments):
        result = self.call(tool, **arguments)
        self.assertFalse(result['isError'], result['content'][0]['text'])
        return result['structuredContent']

    def test_list_and_claim(self):
        tasks = self.ok('list_tasks', repo=str(self.repo))['tasks']
        self.assertEqual([(t['report'], t['state'], t['title']) for t in tasks],
                         [('PBI-001', 'queued', 'PBI-001 ログの日付を ISO 形式にする')])
        got = self.ok('claim_task', repo=str(self.repo), report='PBI-001', runner='my-client')
        self.assertEqual(got['prompt'], 'PBI-001 を実装する\n')
        # 旧い PBI の日本語の見出しも、英語の見出しのキーで返す
        self.assertEqual(got['sections']['Targets'], '- src/log.py')
        self.assertEqual(got['verify_commands'], [{'n': 1, 'command': 'test -f src/log.py'},
                                                  {'n': 2, 'command': 'exit 3'}])
        self.assertEqual(got['facts'], {'files': ['src/log.py']})
        self.assertIsNone(got['rework'])
        claim = json.loads((self.repo / 'docs/pbi/mcp/PBI-001.claim.json').read_text(encoding='utf-8'))
        self.assertEqual(claim['runner'], 'my-client')
        tasks = self.ok('list_tasks', repo=str(self.repo))['tasks']
        self.assertEqual(tasks[0]['state'], 'claimed')

    def test_double_claim_is_refused(self):
        self.ok('claim_task', repo=str(self.repo), report='PBI-001')
        result = self.call('claim_task', repo=str(self.repo), report='PBI-001')
        self.assertTrue(result['isError'])
        self.assertIn('取得済み', result['content'][0]['text'])

    def test_submit_writes_five_line_summary_and_details(self):
        self.ok('claim_task', repo=str(self.repo), report='PBI-001')
        out = self.ok('submit_result', repo=str(self.repo), report='PBI-001', status='DONE', passed=1, total=1,
                      changed=['src/log.py'], details='## 変更\n- 日付を ISO にした',
                      artifacts={'summary': {'score': 0.9}})
        last = (self.repo / 'docs/pbi/mcp/PBI-001.last.md').read_text(encoding='utf-8')
        self.assertEqual(last.splitlines(), [
            'Status: DONE',
            'Result: acceptance criteria pass 1/1',
            'Changed: src/log.py',
            'Open: none',
            'Details: docs/pbi/reports/PBI-001.md',
        ])
        self.assertEqual(out['summary'], last)
        details = (self.repo / 'docs/pbi/reports/PBI-001.md').read_text(encoding='utf-8')
        self.assertTrue(details.startswith('Status: DONE\n'))
        artifacts = json.loads((self.repo / 'docs/pbi/mcp/PBI-001.artifacts.json').read_text(encoding='utf-8'))
        self.assertEqual(artifacts, {'summary': {'score': 0.9}})
        self.assertEqual(self.ok('list_tasks', repo=str(self.repo))['tasks'], [])
        self.assertEqual(self.ok('list_tasks', repo=str(self.repo), include_done=True)['tasks'][0]['state'], 'done')
        again = self.call('submit_result', repo=str(self.repo), report='PBI-001', status='DONE', passed=1, total=1)
        self.assertTrue(again['isError'])

    def test_submit_without_claim_and_bad_counts_are_refused(self):
        r = self.call('submit_result', repo=str(self.repo), report='PBI-001', status='DONE', passed=1, total=1)
        self.assertIn('claim_task', r['content'][0]['text'])
        self.ok('claim_task', repo=str(self.repo), report='PBI-001')
        r = self.call('submit_result', repo=str(self.repo), report='PBI-001', status='DONE', passed=2, total=1)
        self.assertTrue(r['isError'])
        r = self.call('submit_result', repo=str(self.repo), report='PBI-001', status='OK', passed=1, total=1)
        self.assertTrue(r['isError'])
        self.assertFalse((self.repo / 'docs/pbi/mcp/PBI-001.last.md').exists())

    def test_fail_task(self):
        self.ok('claim_task', repo=str(self.repo), report='PBI-001')
        self.ok('fail_task', repo=str(self.repo), report='PBI-001', status='NEEDS_CONTEXT',
                reason='ログの形式の指定が無い')
        last = (self.repo / 'docs/pbi/mcp/PBI-001.last.md').read_text(encoding='utf-8').splitlines()
        self.assertEqual(last[:4], ['Status: NEEDS_CONTEXT', 'Result: not run', 'Changed: none', 'Open: ログの形式の指定が無い'])
        r = self.call('fail_task', repo=str(self.repo), report='PBI-001', status='DONE', reason='x')
        self.assertTrue(r['isError'])

    def test_rework_report_returns_rework_text(self):
        (self.repo / 'docs/pbi/PBI-001.rework-1.md').write_text('差し戻し: 秒まで出す\n', encoding='utf-8')
        (self.repo / 'docs/pbi/mcp/PBI-001.rework-1.prompt.md').write_text('差し戻し対応\n', encoding='utf-8')
        got = self.ok('claim_task', repo=str(self.repo), report='PBI-001.rework-1')
        self.assertEqual((got['pbi'], got['rework']), ('PBI-001', '差し戻し: 秒まで出す\n'))

    def test_paths_outside_roots_and_bad_names_are_refused(self):
        outside = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / 'docs' / 'pbi').mkdir(parents=True)
        r = self.call('list_tasks', repo=str(outside))
        self.assertIn('外', r['content'][0]['text'])
        r = self.call('list_tasks', repo=str(self.repo / '..' / '..' / outside.name))
        self.assertTrue(r['isError'])
        r = self.call('claim_task', repo=str(self.repo), report='../PBI-001')
        self.assertTrue(r['isError'])
        r = self.call('verify', repo=str(self.repo), report='PBI-001', workdir=str(outside))
        self.assertTrue(r['isError'])
        r = self.call('list_tasks', repo=str(self.root))
        self.assertIn('docs/pbi', r['content'][0]['text'])

    def test_verify_runs_commands_in_workdir(self):
        out = self.ok('verify', repo=str(self.repo), report='PBI-001')
        self.assertEqual(out['code'], 0, out['stderr'])
        self.assertEqual([(r['n'], r['code']) for r in out['results']], [(1, 0), (2, 3)])
        self.assertTrue((self.repo / 'docs/pbi/verify/PBI-001.mcp.json').is_file())
        tree = self.root / 'worktree'
        tree.mkdir()
        out = self.ok('verify', repo=str(self.repo), report='PBI-001', workdir=str(tree), name='PBI-001.wt')
        self.assertEqual([(r['n'], r['code']) for r in out['results']], [(1, 1), (2, 3)])

    def test_jev_gate_needs_config(self):
        r = self.call('jev_gate', repo=str(self.repo), gate='report', text='報告の要約')
        self.assertIn('jev.config.json', r['content'][0]['text'])

    def test_record_writes_the_row_record_run_makes(self):
        self.write_runner_log()
        out = self.record(effort='max', findings=[0, 1, 2])
        self.assertEqual(set(out), {'code', 'stdout', 'stderr'})
        self.assertEqual((out['code'], out['stderr']), (0, ''))
        self.assertIn('記録: PBI-001 attempt 1 opencode opencode/model-a 合格', out['stdout'])
        [row] = self.rows()
        # jev-mcp が組み立てた行ではなく、record_run.py の列がそのまま入る
        self.assertEqual(set(row), {'schema', 'recorded_at', 'repo', 'work_key', 'pbi', 'report', 'attempt',
                                    'rework', 'tier', 'runner', 'model', 'effort', 'models_seen', 'time', 'steps',
                                    'tokens', 'cost_reported', 'cost_api_equiv', 'prices_checked_at',
                                    'prices_home_checked_at', 'verify', 'unchanged', 'judged', 'archive'})
        self.assertEqual(row['schema'], 1)
        self.assertTrue(row['recorded_at'])
        self.assertEqual((row['repo'], row['pbi'], row['report'], row['attempt'], row['rework']),
                         ('app', 'PBI-001', 'PBI-001', 1, 0))
        self.assertEqual((row['runner'], row['model'], row['effort'], row['tier']),
                         ('opencode', 'opencode/model-a', 'max', None))
        self.assertEqual({k: row['tokens'][k] for k in ('input', 'output', 'total')},
                         {'input': 100, 'output': 20, 'total': 120})
        self.assertEqual(row['cost_reported'], 0.0123)
        self.assertEqual(row['judged'], {'outcome': '合格', 'findings': {'重大': 0, '中': 1, '軽微': 2}})
        self.assertTrue(row['archive']['ok'])
        for column in ('role', 'source', 'label'):
            self.assertNotIn(column, row)

    def test_record_timeout_is_a_tool_error(self):
        def slow(*args, **kwargs):
            raise subprocess.TimeoutExpired(args[0], kwargs.get('timeout'))
        with unittest.mock.patch.object(jev_mcp.subprocess, 'run', side_effect=slow):
            r = self.call('record', repo=str(self.repo), report='PBI-001', attempt=1, outcome='合格')
        self.assertTrue(r.get('isError'))
        self.assertIn(str(jev_mcp.RECORD_TIMEOUT), r['content'][0]['text'])

    def test_record_second_same_attempt_is_exit_code_2(self):
        self.write_runner_log()
        self.assertEqual(self.record()['code'], 0)
        result = self.call('record', repo=str(self.repo), report='PBI-001', attempt=1, outcome='合格',
                           runner='opencode', model='opencode/model-a')
        self.assertFalse(result['isError'])
        out = result['structuredContent']
        self.assertEqual(out['code'], 2)
        self.assertIn('記録済み', out['stderr'])
        self.assertEqual(len(self.rows()), 1)

    def test_record_bad_outcome_is_record_run_result(self):
        self.write_runner_log()
        result = self.call('record', repo=str(self.repo), report='PBI-001', attempt=1, outcome='不合格',
                           runner='opencode', model='opencode/model-a')
        self.assertFalse(result['isError'])
        out = result['structuredContent']
        self.assertEqual(out['code'], 2)
        self.assertIn('--outcome', out['stderr'])
        self.assertFalse((self.repo / 'docs/pbi/runs.jsonl').exists())

    def test_record_outside_roots_is_an_error(self):
        outside = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / 'docs' / 'pbi').mkdir(parents=True)
        result = self.call('record', repo=str(outside), report='PBI-001', attempt=1, outcome='合格')
        self.assertTrue(result['isError'])
        self.assertIn('外', result['content'][0]['text'])
        self.assertFalse((outside / 'docs/pbi/runs.jsonl').exists())

    def test_concurrent_records_keep_each_line(self):
        results = []
        lock = threading.Lock()

        def go(n):
            r = self.call('record', repo=str(self.repo), report='PBI-001', attempt=n,
                          outcome='合格', runner='opencode', model='opencode/model-a')
            with lock:
                results.append((n, r))

        threads = [threading.Thread(target=go, args=(n,)) for n in range(1, 9)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 8)
        for n, r in results:
            with self.subTest(attempt=n):
                self.assertFalse(r['isError'], r['content'][0]['text'])
                self.assertEqual(r['structuredContent']['code'], 0, r['structuredContent']['stderr'])
        text = (self.repo / 'docs/pbi/runs.jsonl').read_text(encoding='utf-8')
        self.assertEqual(len([line for line in text.splitlines() if line.strip()]), 8)
        self.assertEqual(sorted(row['attempt'] for row in self.rows()), list(range(1, 9)))

    def test_record_uses_the_route_json_when_runner_and_model_are_omitted(self):
        self.write_runner_log()
        (self.repo / 'docs/pbi/PBI-001.route.json').write_text(json.dumps({
            'tier': 'haiku',
            'route': [{'runner': 'opencode', 'model': 'opencode/model-b', 'effort': 'max'}]}), encoding='utf-8')
        out = self.ok('record', repo=str(self.repo), report='PBI-001', attempt=1, outcome='昇格')
        self.assertEqual(out['code'], 0, out['stderr'])
        [row] = self.rows()
        self.assertEqual((row['runner'], row['model'], row['effort'], row['tier']),
                         ('opencode', 'opencode/model-b', 'max', 'haiku'))
        self.assertEqual(row['judged']['outcome'], '昇格')

    def test_record_rework_report_name_is_the_same_as_record_run(self):
        self.write_runner_log(report='PBI-001.rework-2')
        out = self.record(report='PBI-001.rework-2', attempt=3)
        self.assertEqual(out['code'], 0, out['stderr'])
        [row] = self.rows()
        self.assertEqual((row['pbi'], row['report'], row['attempt'], row['rework']),
                         ('PBI-001', 'PBI-001.rework-2', 3, 2))
        self.assertIsNone(row['judged']['findings'])

    def test_record_is_listed_with_the_record_run_arguments(self):
        tools = self.server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})['result']['tools']
        found = [t for t in tools if t['name'] == 'record']
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['inputSchema']['required'], ['repo', 'report', 'attempt', 'outcome'])
        self.assertEqual(list(found[0]['inputSchema']['properties']),
                         ['repo', 'report', 'attempt', 'outcome', 'findings', 'runner', 'model', 'effort', 'tier'])
        base = {'repo': str(self.repo), 'report': 'PBI-001'}
        for arguments in ({'attempt': 1, 'outcome': '合格', 'role': 'executor'},
                          {'attempt': 1, 'outcome': {'ok': True}},
                          {'attempt': 0, 'outcome': '合格'},
                          {'attempt': 1, 'outcome': '合格', 'findings': [0, 1]},
                          {'attempt': 1, 'outcome': '合格', 'findings': [0, 1, '2']}):
            with self.subTest(arguments=arguments):
                self.assertTrue(self.call('record', **base, **arguments)['isError'])
        self.assertFalse((self.repo / 'docs/pbi/runs.jsonl').exists())

    def test_route_is_the_single_select_model_decision(self):
        out = self.route()
        self.assertEqual(out['tier'], 'haiku')
        self.assertEqual(out['route'], [
            {'runner': 'opencode', 'model': 'opencode/model-a', 'effort': 'max'},
            {'runner': 'opencode', 'model': 'opencode/model-b', 'effort': 'max'},
            {'runner': 'agent', 'model': 'haiku', 'effort': 'max'},
        ])
        self.assertEqual(out['dropped'], [])
        self.assertEqual(set(out), {'tier', 'route', 'dropped', 'review', 'scores', 'reasons', 'template'})
        self.assertTrue(out['reasons'][0].startswith('Jev なし: facts だけで判定。決定: haiku。'))

    def test_route_tier_is_the_same_as_the_tier_option(self):
        out = self.route(tier='sonnet')
        self.assertEqual((out['tier'], out['route']), ('sonnet', [
            {'runner': 'opencode', 'model': 'opencode/model-c', 'effort': 'max'},
            {'runner': 'agent', 'model': 'sonnet', 'effort': 'high'},
        ]))
        self.assertTrue(out['reasons'][0].startswith('決定: sonnet。--tier sonnet で指定'))

    def test_route_skip_runner_moves_its_steps_to_dropped(self):
        out = self.route(skip=['opencode'])
        self.assertEqual(out['route'], [{'runner': 'agent', 'model': 'haiku', 'effort': 'max'}])
        self.assertEqual([(d['runner'], d['model'], d['reason']) for d in out['dropped']], [
            ('opencode', 'opencode/model-a', '--skip で外した'),
            ('opencode', 'opencode/model-b', '--skip で外した'),
        ])
        self.assertIn('opencode を外した', out['reasons'][0])

    def test_route_skip_model_id_moves_only_that_step(self):
        out = self.route(skip=['opencode/model-b'])
        self.assertEqual([s['model'] for s in out['route']], ['opencode/model-a', 'haiku'])
        self.assertEqual([(d['model'], d['reason']) for d in out['dropped']],
                         [('opencode/model-b', '--skip で外した')])

    def test_route_rejects_bad_tier_skip_and_the_old_arguments(self):
        base = {'repo': str(self.repo), 'report': 'PBI-001'}
        for arguments in ({'tier': 'medium'}, {'skip': 'opencode'}, {'skip': ['nonsense']},
                          {'skip': ['opencode', 1]}, {'role': 'executor'}, {'substrate': 'opencode'},
                          {'runners': ['agent']}):
            with self.subTest(arguments=arguments):
                self.assertTrue(self.call('route', **base, **arguments)['isError'])

    def test_route_without_facts_is_an_error(self):
        (self.repo / 'docs/pbi/PBI-001.facts.json').unlink()
        r = self.call('route', repo=str(self.repo), report='PBI-001')
        self.assertTrue(r['isError'])
        self.assertIn('facts', r['content'][0]['text'])

    def test_route_sensitive_facts_do_not_apply_the_priority_template(self):
        self.write_priority_template()
        self.write_facts(files_changed=1, modules=1, sensitive=True)
        out = self.route(tier='haiku')
        self.assertFalse(out['template']['applied'])
        self.assertIn('sensitive', out['template']['note'])
        self.assertEqual(out['route'], [{'runner': 'agent', 'model': 'haiku', 'effort': 'max'}])
        self.assertEqual(out['review'], 'deep')
        # sensitive でなければ優先テンプレートの codex の手順が当たる (比べるための対照)
        self.write_facts(files_changed=1, modules=1, sensitive=False)
        out = self.route(tier='haiku')
        self.assertTrue(out['template']['applied'])
        self.assertEqual(out['route'], [
            {'runner': 'codex', 'model': 'model-x', 'effort': 'high'},
            {'runner': 'agent', 'model': 'haiku', 'effort': 'max'},
        ])

    def test_route_rework_report_uses_the_original_facts(self):
        self.write_facts(sensitive=True)
        (self.repo / 'docs/pbi/PBI-001.rework-1.facts.json').write_text(json.dumps({'sensitive': False}),
                                                                       encoding='utf-8')
        out = self.ok('route', repo=str(self.repo), report='PBI-001.rework-1')
        self.assertEqual(out['tier'], 'sonnet')
        self.assertEqual(out['route'], [{'runner': 'agent', 'model': 'sonnet', 'effort': 'high'}])
        self.assertNotEqual(out['template']['name'], 'prio')

    def test_route_reads_the_jev_brief_answers(self):
        brief = self.repo / 'docs' / 'pbi' / 'jev' / 'PBI-001.brief.json'
        brief.parent.mkdir()
        brief.write_text(json.dumps({'answers': {
            'novelty': {'probabilities': {'0': 0.0, '1': 0.0, '2': 1.0}, 'confidence': 0.9},
            'coupling': {'probabilities': {'0': 1.0, '1': 0.0, '2': 0.0}, 'confidence': 0.9},
            'ambiguity': {'probabilities': {'0': 0.0, '1': 0.0, '2': 1.0}, 'confidence': 0.9},
        }}), encoding='utf-8')
        out = self.route()
        self.assertEqual(out['tier'], 'opus')
        self.assertEqual(out['route'], [{'runner': 'agent', 'model': 'opus', 'effort': None}])
        self.assertTrue(out['reasons'][0].startswith('決定: opus。'))

    def test_route_does_not_write_a_route_json(self):
        self.route()
        self.assertFalse((self.repo / 'docs/pbi/PBI-001.route.json').exists())

    def test_route_is_listed_with_only_the_select_model_arguments(self):
        tools = self.server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})['result']['tools']
        found = [t for t in tools if t['name'] == 'route']
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['inputSchema']['required'], ['repo', 'report'])
        self.assertEqual(list(found[0]['inputSchema']['properties']), ['repo', 'report', 'tier', 'skip'])
        result = self.call('route', repo=str(self.repo), report='PBI-001')
        self.assertFalse(result['isError'], result['content'][0]['text'])
        self.assertEqual(result['structuredContent']['tier'], 'haiku')

    def test_protocol(self):
        init = self.server.handle({'jsonrpc': '2.0', 'id': 0, 'method': 'initialize',
                                   'params': {'protocolVersion': '2025-03-26'}})
        self.assertEqual(init['result']['protocolVersion'], '2025-03-26')
        self.assertEqual(init['result']['serverInfo']['name'], 'jev-mcp')
        self.assertIsNone(self.server.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'}))
        names = [t['name'] for t in self.server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
                 ['result']['tools']]
        self.assertEqual(names, ['list_tasks', 'claim_task', 'submit_result', 'fail_task', 'verify', 'jev_gate',
                                 'route', 'run', 'record'])
        self.assertEqual(self.server.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'nope'})['error']['code'], -32601)
        self.assertTrue(self.call('nope')['isError'])
        self.assertTrue(self.call('list_tasks', repo=str(self.repo), extra=1)['isError'])

    def test_stdio(self):
        lines = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
             'params': {'name': 'list_tasks', 'arguments': {'repo': str(self.repo)}}},
        ]
        stdin = '\n'.join(json.dumps(m) for m in lines) + '\nnot json\n'
        r = subprocess.run([sys.executable, SCRIPT, '--root', str(self.root)], input=stdin,
                           capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        replies = [json.loads(line) for line in r.stdout.splitlines()]
        self.assertEqual([m.get('id') for m in replies], [1, 2, None])
        self.assertEqual(replies[1]['result']['structuredContent']['tasks'][0]['report'], 'PBI-001')
        self.assertEqual(replies[2]['error']['code'], -32700)

    def test_serve_stdio_in_process(self):
        out = io.StringIO()
        jev_mcp.serve_stdio(self.server, io.StringIO('{"jsonrpc": "2.0", "id": 7, "method": "ping"}\n'), out)
        self.assertEqual(json.loads(out.getvalue()), {'jsonrpc': '2.0', 'id': 7, 'result': {}})

    def test_http(self):
        httpd = jev_mcp.http.server.ThreadingHTTPServer(('127.0.0.1', 0), None)
        port = httpd.server_address[1]
        httpd.RequestHandlerClass = jev_mcp.make_handler(self.server, port)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)

        def post(body, host=None, content_type='application/json', origin=None):
            req = urllib.request.Request(f'http://127.0.0.1:{port}/mcp', data=json.dumps(body).encode(),
                                         headers={'Content-Type': content_type})
            if host:
                req.add_header('Host', host)
            if origin:
                req.add_header('Origin', origin)
            try:
                with urllib.request.urlopen(req, timeout=10) as res:
                    return res.status, res.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()

        status, data = post({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(data)['result']['tools']), 9)
        self.assertEqual(post({'jsonrpc': '2.0', 'method': 'notifications/initialized'})[0], 202)
        self.assertEqual(post({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}, host='evil.example')[0], 403)
        ping = {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}
        # A simple cross-site POST from a browser page (text/plain, or a foreign Origin) is refused.
        self.assertEqual(post(ping, content_type='text/plain')[0], 415)
        self.assertEqual(post(ping, content_type='application/x-www-form-urlencoded')[0], 415)
        self.assertEqual(post(ping, origin='https://evil.example')[0], 403)
        self.assertEqual(post(ping, origin='null')[0], 403)
        # A charset parameter and the server's own origin are fine.
        self.assertEqual(post(ping, content_type='application/json; charset=utf-8')[0], 200)
        self.assertEqual(post(ping, origin=f'http://127.0.0.1:{port}')[0], 200)


FAKE_RUNNER = '''import json, os, sys, time
args = sys.argv[1:]
opencode = bool(args) and args[0] == 'run'
claude = '--setting' in args
with open(os.environ['FAKE_RECORD'], 'w', encoding='utf-8') as f:
    json.dump({'kind': 'opencode' if opencode else 'claude' if claude else 'dsh', 'argv': args, 'cwd': os.getcwd(),
               'opencode_config': os.environ.get('OPENCODE_CONFIG'),
               'prompt': None if opencode or claude
               else open('docs/pbi/dsh/%s.prompt.md' % args[0], encoding='utf-8').read()}, f)
report = os.environ['FAKE_REPORT']
base = os.path.join('docs', 'pbi', 'opencode' if opencode else 'claude' if claude else 'dsh')
os.makedirs(base, exist_ok=True)
if os.environ.get('FAKE_SLEEP'):
    time.sleep(float(os.environ['FAKE_SLEEP']))
with open(os.path.join(base, report + ('.json' if claude else '.jsonl')), 'w', encoding='utf-8') as f:
    f.write(os.environ.get('FAKE_JSONL', '{"type":"step_finish"}\\n'))
with open(os.path.join(base, report + '.err'), 'w', encoding='utf-8') as f:
    f.write(os.environ.get('FAKE_ERR', ''))
if 'FAKE_LAST' in os.environ:
    with open(os.path.join(base, report + '.last.md'), 'w', encoding='utf-8') as f:
        f.write('' if os.environ['FAKE_LAST'] == '@empty' else os.environ['FAKE_LAST'])
if os.environ.get('FAKE_REPORT_MD'):
    os.makedirs(os.path.join('docs', 'pbi', 'reports'), exist_ok=True)
    with open(os.path.join('docs', 'pbi', 'reports', report + '.md'), 'w', encoding='utf-8') as f:
        f.write('状態: DONE\\n')
if os.environ.get('FAKE_STDERR'):
    sys.stderr.buffer.write(os.environ['FAKE_STDERR'].encode('utf-8'))
sys.exit(int(os.environ.get('FAKE_EXIT', '0')))
'''
LAST = '状態: DONE\n結果: 受け入れ条件 pass 1/1\n変更: a.py\n未解決: なし\n詳細: docs/pbi/reports/PBI-001.md\n'


class PbiFormatTest(unittest.TestCase):
    ENGLISH = (
        '# PBI-002: Log dates\n\n- Model: agent sonnet\n\n## Goal\nISO dates.\n\n## Targets\n- src/log.py\n\n'
        '## Do not touch\n- src/api.py\n\n## Acceptance criteria\n1. command 1 exits 0\n\n'
        '## Verify commands\n1. `test -f src/log.py`\n2. `exit 3`\n\n## Refs\nnone\n\n## Notes\nnone\n')

    def test_english_headings_are_read(self):
        sections = jev_mcp.sections_of(self.ENGLISH)
        self.assertEqual(sections['Targets'], '- src/log.py')
        self.assertEqual(sections['Do not touch'], '- src/api.py')
        self.assertEqual(jev_mcp.verify_commands(self.ENGLISH),
                         [{'n': 1, 'command': 'test -f src/log.py'}, {'n': 2, 'command': 'exit 3'}])


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / 'roots'
        self.repo = self.root / 'app'
        (self.repo / 'docs' / 'pbi').mkdir(parents=True)
        self.tree = self.root / 'worktree'
        self.tree.mkdir()
        self.outside = self.tmp / 'outside'
        self.outside.mkdir()
        self.fakes = self.tmp / 'fakes'
        self.fakes.mkdir()
        script = self.fakes / 'fake_runner.py'
        script.write_text(FAKE_RUNNER, encoding='utf-8')
        # 偽の dsh_run.py は run が呼ぶスクリプトの場所 (jev_mcp.DSH_RUN) を差し替える
        patch = mock.patch.object(jev_mcp, 'DSH_RUN', script)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(jev_mcp, 'CLAUDE_RUN', script)
        patch.start()
        self.addCleanup(patch.stop)
        if os.name == 'nt':
            (self.fakes / 'opencode.cmd').write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding='utf-8')
        else:
            fake = self.fakes / 'opencode'
            fake.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding='utf-8')
            fake.chmod(0o755)
        self.record = self.tmp / 'record.json'
        env = mock.patch.dict(os.environ, {
            'PATH': str(self.fakes) + os.pathsep + os.environ.get('PATH', ''),
            'FAKE_RECORD': str(self.record), 'FAKE_REPORT': 'PBI-001', 'FAKE_LAST': LAST})
        env.start()
        self.addCleanup(env.stop)
        self.server = jev_mcp.Server(jev_mcp.Tasks([self.root]))

    def call(self, **arguments):
        arguments.setdefault('repo', str(self.repo))
        arguments.setdefault('report', 'PBI-001')
        arguments.setdefault('prompt', 'PBI-001 を実装する')
        r = self.server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                'params': {'name': 'run', 'arguments': arguments}})
        return r['result']

    def ok(self, **arguments):
        result = self.call(**arguments)
        self.assertFalse(result['isError'], result['content'][0]['text'])
        return result['structuredContent']

    def recorded(self):
        return json.loads(self.record.read_text(encoding='utf-8'))

    def test_dsh_writes_the_prompt_and_calls_dsh_run(self):
        out = self.ok(runner='dsh', model='deepseek-official/deepseek-flash', effort='high')
        rec = self.recorded()
        self.assertEqual(rec['argv'], ['PBI-001', '--model', 'deepseek-official/deepseek-flash', '--effort', 'high'])
        self.assertEqual(Path(rec['cwd']).resolve(), self.repo)
        self.assertEqual(rec['prompt'], 'PBI-001 を実装する\n\nSummary file: `docs/pbi/dsh/PBI-001.last.md`\n')
        self.assertEqual(out, {
            'runner': 'dsh', 'model': 'deepseek-official/deepseek-flash', 'effort': 'high',
            'setting': 'default', 'permission': jev_mcp.DSH_PERMISSION, 'code': 0, 'last': LAST,
            'report_path': None,
            'logs': ['docs/pbi/dsh/PBI-001.jsonl', 'docs/pbi/dsh/PBI-001.err'],
            'unusable': False, 'unusable_reason': None})

    def test_dsh_without_effort_has_no_effort_argument(self):
        for effort in (None, ''):
            with self.subTest(effort=effort):
                arguments = {} if effort is None else {'effort': effort}
                out = self.ok(runner='dsh', model='deepseek-official/deepseek-flash', **arguments)
                self.assertEqual(self.recorded()['argv'], ['PBI-001', '--model', 'deepseek-official/deepseek-flash'])
                self.assertIsNone(out['effort'])

    def test_opencode_command_and_logs(self):
        out = self.ok(runner='opencode', model='opencode-go/deepseek-v4.1-flash', effort='max',
                      prompt='作業してください', report='PBI-001')
        rec = self.recorded()
        self.assertEqual(rec['kind'], 'opencode')
        self.assertEqual(rec['argv'], ['run', '-m', 'opencode-go/deepseek-v4.1-flash#max', '--format', 'json',
                                       'docs/pbi/opencode/PBI-001.prompt.md を読み、その指示どおりに作業してください。'])
        self.assertEqual(Path(rec['opencode_config']), jev_mcp.HERE / 'opencode.config.json')
        self.assertEqual(Path(rec['cwd']).resolve(), self.repo)
        prompt = (self.repo / 'docs/pbi/opencode/PBI-001.prompt.md').read_text(encoding='utf-8')
        self.assertEqual(prompt, '作業してください\n\nSummary file: `docs/pbi/opencode/PBI-001.last.md`\n')
        self.assertEqual((out['code'], out['last'], out['unusable']), (0, LAST, False))
        self.assertEqual(out['logs'], ['docs/pbi/opencode/PBI-001.jsonl', 'docs/pbi/opencode/PBI-001.err'])
        self.assertEqual((self.repo / 'docs/pbi/opencode/PBI-001.jsonl').read_text(encoding='utf-8'),
                         '{"type":"step_finish"}\n')

    def test_opencode_without_effort_has_no_hash(self):
        self.ok(runner='opencode', model='opencode/deepseek-v4.1-flash-free')
        self.assertEqual(self.recorded()['argv'][1:3], ['-m', 'opencode/deepseek-v4.1-flash-free'])

    def test_last_is_missing_or_empty_is_unusable_for_both(self):
        for runner in ('dsh', 'opencode'):
            for last in (None, '@empty', '  \n'):
                with self.subTest(runner=runner, last=last):
                    extra = {'FAKE_LAST': last} if last is not None else {}
                    with mock.patch.dict(os.environ, extra):
                        if last is None:
                            os.environ.pop('FAKE_LAST')
                        out = self.ok(runner=runner, model='a/b')
                    self.assertTrue(out['unusable'])
                    self.assertIn('.last.md が無いか空', out['unusable_reason'])
                    self.assertEqual(out['last'], {None: None, '@empty': ''}.get(last, last))

    def test_nonzero_exit_is_unusable_for_both(self):
        for runner in ('dsh', 'opencode'):
            with self.subTest(runner=runner), mock.patch.dict(os.environ, {'FAKE_EXIT': '2'}):
                out = self.ok(runner=runner, model='a/b')
                self.assertEqual((out['code'], out['unusable']), (2, True))
                self.assertIn('終了コードが 0 以外 (2)', out['unusable_reason'])

    def test_dsh_err_code_line_is_unusable(self):
        with mock.patch.dict(os.environ, {'FAKE_ERR': 'dsh: MISSING_CREDENTIAL: key が無い\n'}):
            out = self.ok(runner='dsh', model='a/b')
        self.assertEqual((out['code'], out['unusable']), (0, True))
        self.assertIn('dsh: <code>:', out['unusable_reason'])
        with mock.patch.dict(os.environ, {'FAKE_ERR': 'warning: 何か\n'}):
            self.assertFalse(self.ok(runner='dsh', model='a/b')['unusable'])

    def test_dsh_run_stderr_is_kept_in_the_reason(self):
        with mock.patch.dict(os.environ, {'FAKE_EXIT': '2', 'FAKE_STDERR': 'dsh_run: dsh が見つかりません'}):
            out = self.ok(runner='dsh', model='a/b')
        self.assertIn('dsh が見つかりません', out['unusable_reason'])

    def test_opencode_error_event_and_database_lock_are_unusable(self):
        cases = ({'FAKE_JSONL': '{"type":"error","error":{"name":"APIError"}}\n'},
                 {'FAKE_ERR': 'Error: database is locked\n'})
        for env in cases:
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                out = self.ok(runner='opencode', model='a/b')
                self.assertEqual((out['code'], out['unusable']), (0, True))
        # dsh の判定の語は opencode には当たらない
        with mock.patch.dict(os.environ, {'FAKE_ERR': 'dsh: TRANSPORT: x\n'}):
            self.assertFalse(self.ok(runner='opencode', model='a/b')['unusable'])

    def test_report_path_and_stale_last(self):
        with mock.patch.dict(os.environ, {'FAKE_REPORT_MD': '1'}):
            self.assertEqual(self.ok(runner='dsh', model='a/b')['report_path'], 'docs/pbi/reports/PBI-001.md')
        # 前の実行の .last.md は、今回 .last.md が来なくても残らない
        os.environ.pop('FAKE_LAST')
        out = self.ok(runner='dsh', model='a/b')
        self.assertIsNone(out['last'])
        self.assertTrue(out['unusable'])

    def test_workdir_is_the_working_root(self):
        out = self.ok(runner='dsh', model='a/b', workdir=str(self.tree))
        self.assertEqual(Path(self.recorded()['cwd']).resolve(), self.tree)
        self.assertTrue((self.tree / 'docs/pbi/dsh/PBI-001.prompt.md').is_file())
        self.assertFalse((self.repo / 'docs/pbi/dsh/PBI-001.prompt.md').exists())
        self.assertEqual(out['logs'], ['docs/pbi/dsh/PBI-001.jsonl', 'docs/pbi/dsh/PBI-001.err'])

    def test_workdir_outside_root_is_an_error(self):
        for runner in ('dsh', 'opencode'):
            with self.subTest(runner=runner):
                r = self.call(runner=runner, model='a/b', workdir=str(self.outside))
                self.assertTrue(r['isError'])
                self.assertIn('外', r['content'][0]['text'])
        self.assertTrue(self.call(runner='dsh', model='a/b', workdir=str(self.root / 'nothing'))['isError'])
        self.assertFalse(self.record.exists())
        self.assertFalse((self.outside / 'docs').exists())

    def test_other_runners_are_errors(self):
        for runner in ('agent', 'codex', 'agy', 'mcp', 'nonsense'):
            with self.subTest(runner=runner):
                r = self.call(runner=runner, model='a/b')
                self.assertTrue(r['isError'])
                self.assertIn('dsh と opencode', r['content'][0]['text'])
        self.assertEqual(list(jev_mcp.RUN_RUNNERS), ['dsh', 'opencode', 'claude'])
        self.assertFalse(self.record.exists())
        self.assertFalse((self.repo / 'docs/pbi/agent').exists())

    def test_bad_arguments_are_errors(self):
        for arguments in ({'model': ''}, {'model': '--bad'}, {'model': 'a/b', 'effort': 'x y'},
                          {'model': 'a/b', 'prompt': ' '}, {'model': 'a/b', 'timeout': 0},
                          {'model': 'a/b', 'report': '../PBI-001'}, {'model': 'a/b', 'repo': str(self.outside)}):
            with self.subTest(arguments=arguments):
                self.assertTrue(self.call(runner='dsh', **arguments)['isError'])
        self.assertTrue(self.call(runner='dsh')['isError'])
        self.assertFalse(self.record.exists())

    def test_missing_opencode_is_an_error(self):
        with mock.patch.dict(os.environ, {'PATH': str(self.outside)}):
            r = self.call(runner='opencode', model='a/b')
        self.assertTrue(r['isError'])
        self.assertIn('opencode', r['content'][0]['text'])

    def test_timeout_stops_the_process_and_is_unusable(self):
        for runner in ('dsh', 'opencode'):
            with self.subTest(runner=runner), mock.patch.dict(os.environ, {'FAKE_SLEEP': '30'}):
                started = time.monotonic()
                out = self.ok(runner=runner, model='a/b', timeout=2)
                self.assertLess(time.monotonic() - started, 20)
                self.assertIsNone(out['code'])
                self.assertTrue(out['unusable'])
                self.assertIn('timeout (2s)', out['unusable_reason'])
                self.assertIsNone(out['last'])

    def test_listed_with_the_run_arguments_and_no_session_tool(self):
        tools = self.server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})['result']['tools']
        found = [t for t in tools if t['name'] == 'run']
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['inputSchema']['required'], ['repo', 'report', 'runner', 'model', 'prompt'])
        self.assertEqual(list(found[0]['inputSchema']['properties']),
                         ['repo', 'report', 'runner', 'model', 'effort', 'setting', 'prompt', 'workdir', 'timeout'])
        self.assertNotIn('run_session', [t['name'] for t in tools])

    def test_opencode_edit_only_uses_the_edit_only_config(self):
        out = self.ok(runner='opencode', model='a/b', setting='edit-only')
        self.assertEqual(Path(self.recorded()['opencode_config']), jev_mcp.HERE / 'opencode.edit-only.config.json')
        self.assertEqual(out['setting'], 'edit-only')
        out = self.ok(runner='opencode', model='a/b', setting='default')
        self.assertEqual(Path(self.recorded()['opencode_config']), jev_mcp.HERE / 'opencode.config.json')
        self.assertEqual(out['setting'], 'default')

    def test_claude_passes_the_setting_to_claude_run(self):
        for setting, given in (('default', {}), ('default', {'setting': 'default'}),
                               ('edit-only', {'setting': 'edit-only'})):
            with self.subTest(setting=setting, given=given):
                out = self.ok(runner='claude', model='sonnet', effort='high', **given)
                rec = self.recorded()
                self.assertEqual(rec['kind'], 'claude')
                self.assertEqual(rec['argv'], ['PBI-001', '--model', 'sonnet', '--effort', 'high',
                                               '--setting', setting])
                self.assertEqual(Path(rec['cwd']).resolve(), self.repo)
                self.assertTrue((self.repo / 'docs/pbi/claude/PBI-001.prompt.md').is_file())
                self.assertEqual((out['setting'], out['unusable']), (setting, False))
                self.assertEqual(out['logs'], ['docs/pbi/claude/PBI-001.json', 'docs/pbi/claude/PBI-001.err'])
        self.ok(runner='claude', model='sonnet')
        self.assertEqual(self.recorded()['argv'], ['PBI-001', '--model', 'sonnet', '--setting', 'default'])

    def test_claude_unusable_conditions(self):
        cases = ({'FAKE_JSONL': '{"is_error": true, "result": "x"}'},
                 {'FAKE_JSONL': '{"is_error": false, "result": "Claude usage limit reached"}'},
                 {'FAKE_ERR': 'Invalid API key - please run /login\n'},
                 {'FAKE_EXIT': '1'})
        for env in cases:
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                self.assertTrue(self.ok(runner='claude', model='sonnet')['unusable'])
        with mock.patch.dict(os.environ, {'FAKE_JSONL': '{"is_error": false, "result": "done"}'}):
            self.assertFalse(self.ok(runner='claude', model='sonnet')['unusable'])

    def test_dsh_edit_only_and_bad_setting_are_errors(self):
        r = self.call(runner='dsh', model='a/b', setting='edit-only')
        self.assertTrue(r['isError'])
        self.assertIn('dsh には読む・書くだけの設定が無い', r['content'][0]['text'])
        for runner in ('dsh', 'opencode', 'claude'):
            for setting in ('readonly', '', 3):
                with self.subTest(runner=runner, setting=setting):
                    self.assertTrue(self.call(runner=runner, model='a/b', setting=setting)['isError'])
        self.assertFalse(self.record.exists())

    def test_permission_matches_the_setting(self):
        out = self.ok(runner='opencode', model='a/b', setting='edit-only')
        self.assertEqual(out['permission'], {'commands': False, 'tools': ['edit'], 'outside_workdir': False})
        out = self.ok(runner='opencode', model='a/b')
        self.assertEqual(out['permission'], {'commands': True, 'tools': ['edit', 'bash'], 'outside_workdir': False})
        out = self.ok(runner='claude', model='a')
        self.assertTrue(out['permission']['commands'])
        self.assertEqual(out['permission']['tools'], list(jev_mcp.claude_run.SETTINGS['default']))
        out = self.ok(runner='claude', model='a', setting='edit-only')
        self.assertEqual(out['permission'], {'commands': False, 'tools': ['Read', 'Edit', 'Write', 'Glob', 'Grep'],
                                             'outside_workdir': False})
        self.assertEqual(self.ok(runner='dsh', model='a/b')['permission'], jev_mcp.DSH_PERMISSION)


class EditOnlyConfigTest(unittest.TestCase):
    def test_config_denies_commands_and_outside(self):
        cfg = json.loads((jev_mcp.HERE / 'opencode.edit-only.config.json').read_text(encoding='utf-8'))
        perm = cfg['permission']
        self.assertEqual(cfg['$schema'], 'https://opencode.ai/config.json')
        self.assertEqual(perm['edit'], 'allow')
        self.assertEqual(perm['bash'], {'*': 'deny'})
        self.assertEqual(perm['external_directory'], 'deny')
        for key in ('webfetch', 'websearch', 'task', 'skill', 'question'):
            self.assertEqual(perm[key], 'deny', key)
        self.assertEqual([k for k, v in perm.items() if k != 'edit' and v not in ('deny', {'*': 'deny'})], [])

    def test_default_config_keeps_allowing_commands(self):
        perm = json.loads((jev_mcp.HERE / 'opencode.config.json').read_text(encoding='utf-8'))['permission']
        self.assertEqual(perm['bash']['*'], 'allow')
        self.assertEqual(perm['external_directory'], 'deny')


if __name__ == '__main__':
    unittest.main()
