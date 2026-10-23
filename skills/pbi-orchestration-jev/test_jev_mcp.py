import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
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
        env = mock.patch.dict(os.environ, {'PBI_HOME': str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.write_routing()
        self.write_agents()

    def write_routing(self):
        (self.home / 'routing.json').write_text(json.dumps(ROUTING), encoding='utf-8')

    def write_agents(self, runners=('opencode', 'codex')):
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
        arguments.setdefault('role', 'executor')
        arguments.setdefault('runner', 'opencode')
        arguments.setdefault('model', 'opencode/model-a')
        return self.ok('record', **arguments)

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
        got = self.ok('claim_task', repo=str(self.repo), report='PBI-001', runner='harness2')
        self.assertEqual(got['prompt'], 'PBI-001 を実装する\n')
        self.assertEqual(got['sections']['対象'], '- src/log.py')
        self.assertEqual(got['verify_commands'], [{'n': 1, 'command': 'test -f src/log.py'},
                                                  {'n': 2, 'command': 'exit 3'}])
        self.assertEqual(got['facts'], {'files': ['src/log.py']})
        self.assertIsNone(got['rework'])
        claim = json.loads((self.repo / 'docs/pbi/mcp/PBI-001.claim.json').read_text(encoding='utf-8'))
        self.assertEqual(claim['runner'], 'harness2')
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
            '状態: DONE',
            '結果: 受け入れ条件 pass 1/1',
            '変更: src/log.py',
            '未解決: なし',
            '詳細: docs/pbi/reports/PBI-001.md',
        ])
        self.assertEqual(out['summary'], last)
        details = (self.repo / 'docs/pbi/reports/PBI-001.md').read_text(encoding='utf-8')
        self.assertTrue(details.startswith('状態: DONE\n'))
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
        self.assertEqual(last[:4], ['状態: NEEDS_CONTEXT', '結果: 未実施', '変更: なし', '未解決: ログの形式の指定が無い'])
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

    def test_record_writes_a_harness2_row_with_record_run_columns(self):
        out = self.record(effort='max', outcome={
            'ok': True, 'cost_usd': 0.0123, 'tokens': {'input': 100, 'output': 20}, 'wall_s': 12.5,
            'run_id': 'run-1', 'label': 'try-1'})
        self.assertEqual(out, {'ok': True, 'path': 'docs/pbi/runs.jsonl', 'attempt': 1})
        [row] = self.rows()
        self.assertEqual(set(row), {'schema', 'recorded_at', 'repo', 'pbi', 'report', 'attempt', 'rework',
                                    'tier', 'runner', 'model', 'effort', 'time', 'tokens', 'cost_reported',
                                    'outcome', 'source', 'role', 'run_id', 'label'})
        self.assertEqual(row['schema'], 1)
        self.assertTrue(row['recorded_at'])
        self.assertEqual((row['repo'], row['pbi'], row['report'], row['attempt'], row['rework']),
                         ('app', 'PBI-001', 'PBI-001', 1, 0))
        self.assertEqual((row['tier'], row['runner'], row['model'], row['effort']),
                         (None, 'opencode', 'opencode/model-a', 'max'))
        self.assertEqual(row['time'], {'seconds': 12.5})
        self.assertEqual(row['tokens'], {'input': 100, 'output': 20})
        self.assertEqual(row['cost_reported'], 0.0123)
        self.assertEqual(row['outcome'], '合格')
        self.assertEqual((row['source'], row['role'], row['run_id'], row['label']),
                         ('harness2', 'executor', 'run-1', 'try-1'))

    def test_record_second_try_is_attempt_2(self):
        self.record(outcome={'ok': False})
        out = self.record(outcome={'ok': True})
        self.assertEqual(out['attempt'], 2)
        self.assertEqual([r['attempt'] for r in self.rows()], [1, 2])

    def test_record_outcome_three_values(self):
        cases = [({'ok': True}, '合格'),
                 ({'ok': False, 'unavailable': True}, '使えず'),
                 ({'ok': False}, '不合格')]
        for i, (outcome, _) in enumerate(cases):
            self.record(model=f'model-{i}', outcome=outcome)
        self.assertEqual([r['outcome'] for r in self.rows()], ['合格', '使えず', '不合格'])

    def test_record_rework_report_name_sets_pbi_and_rework(self):
        out = self.record(report='PBI-001.rework-2', outcome={'ok': True})
        self.assertEqual(out['attempt'], 1)
        [row] = self.rows()
        self.assertEqual((row['pbi'], row['report'], row['rework']), ('PBI-001', 'PBI-001.rework-2', 2))

    def test_record_missing_values_are_null(self):
        self.record(effort='', outcome={'ok': False})
        [row] = self.rows()
        self.assertIsNone(row['tier'])
        self.assertIsNone(row['effort'])
        self.assertEqual(row['time'], {'seconds': None})
        self.assertEqual(row['tokens'], {'input': None, 'output': None})
        self.assertIsNone(row['cost_reported'])
        self.assertNotIn('run_id', row)
        self.assertNotIn('label', row)
        self.assertNotIn('error', row)

    def test_record_error_is_kept_up_to_500_characters(self):
        self.record(outcome={'ok': False, 'error': 'あ' * 600})
        [row] = self.rows()
        self.assertEqual((row['outcome'], len(row['error'])), ('不合格', 500))

    def test_record_rejects_bad_outcome_types(self):
        base = {'repo': str(self.repo), 'report': 'PBI-001', 'role': 'executor',
                'runner': 'opencode', 'model': 'm'}
        for outcome in ('合格', {'ok': 'yes'}, {'ok': True, 'cost_usd': 'x'},
                        {'ok': True, 'tokens': {'input': 'x'}}, {'ok': True, 'wall_s': None, 'error': 1}):
            with self.subTest(outcome=outcome):
                self.assertTrue(self.call('record', **base, outcome=outcome)['isError'])
        self.assertTrue(self.call('record', **base)['isError'])
        self.assertFalse((self.repo / 'docs/pbi/runs.jsonl').exists())

    def test_record_rejects_bad_role_and_repo_outside_roots(self):
        r = self.call('record', repo=str(self.repo), report='PBI-001', role='reviewer',
                      runner='opencode', model='m', outcome={'ok': True})
        self.assertTrue(r['isError'])
        outside = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / 'docs' / 'pbi').mkdir(parents=True)
        r = self.call('record', repo=str(outside), report='PBI-001', role='executor',
                      runner='opencode', model='m', outcome={'ok': True})
        self.assertTrue(r['isError'])
        self.assertIn('外', r['content'][0]['text'])
        self.assertFalse((outside / 'docs/pbi/runs.jsonl').exists())

    def test_recorded_row_can_be_read_by_model_report(self):
        import model_report
        self.record(outcome={'ok': True, 'tokens': {'input': 1, 'output': 2}, 'wall_s': 3})
        [row] = model_report.load([self.repo / 'docs/pbi/runs.jsonl'])
        self.assertEqual((row['source'], row['role'], row['outcome'], row['attempt']),
                         ('harness2', 'executor', '合格', 1))

    def test_record_is_listed_with_required_fields(self):
        tools = self.server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})['result']['tools']
        found = [t for t in tools if t['name'] == 'record']
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['inputSchema']['required'],
                         ['repo', 'report', 'role', 'runner', 'model', 'outcome'])
        result = self.call('record', repo=str(self.repo), report='PBI-001', role='executor', runner='opencode',
                           model='opencode/model-a', outcome={'ok': True})
        self.assertFalse(result['isError'], result['content'][0]['text'])

    def test_route_executor_keeps_only_substrate_steps(self):
        out = self.route(role='executor', substrate='opencode')
        self.assertEqual(out['role'], 'executor')
        self.assertEqual(out['tier'], 'haiku')
        self.assertEqual(out['route'], [
            {'runner': 'opencode', 'model': 'opencode/model-a', 'effort': 'max'},
            {'runner': 'opencode', 'model': 'opencode/model-b', 'effort': 'max'},
        ])
        self.assertEqual([(d['runner'], d['model'], d['reason']) for d in out['dropped']],
                         [('agent', 'haiku', 'substrate opencode で動かない')])
        self.assertTrue(out['reasons'][0].startswith('決定: haiku'))
        self.assertIn('template', out)

    def test_route_improver_default_is_agent_only(self):
        out = self.route(role='improver')
        self.assertEqual(out['tier'], 'sonnet')
        self.assertEqual(out['route'], [{'runner': 'agent', 'model': 'sonnet', 'effort': 'high'}])
        self.assertEqual([(d['runner'], d['reason']) for d in out['dropped']],
                         [('opencode', '柵を申告できない実行先')])

    def test_route_improver_runners_include_both(self):
        out = self.route(role='improver', runners=['agent', 'opencode'])
        self.assertEqual([s['runner'] for s in out['route']], ['opencode', 'agent'])
        self.assertEqual(out['dropped'], [])

    def test_route_tier_overrides_the_role_default(self):
        out = self.route(role='executor', substrate='opencode', tier='sonnet')
        self.assertEqual((out['tier'], out['route']),
                         ('sonnet', [{'runner': 'opencode', 'model': 'opencode/model-c', 'effort': 'max'}]))
        out = self.route(role='improver', tier='opus')
        self.assertEqual((out['tier'], out['route']),
                         ('opus', [{'runner': 'agent', 'model': 'opus', 'effort': None}]))

    def test_route_rejects_bad_role_tier_and_missing_substrate(self):
        for arguments in ({'role': 'reviewer'},
                          {'role': 'executor', 'substrate': 'opencode', 'tier': 'medium'},
                          {'role': 'executor'}):
            with self.subTest(arguments=arguments):
                r = self.call('route', repo=str(self.repo), report='PBI-001', **arguments)
                self.assertTrue(r['isError'])

    def test_route_sensitive_facts_do_not_apply_the_priority_template(self):
        self.write_priority_template()
        self.write_facts(files_changed=1, modules=1, sensitive=True)
        out = self.route(role='executor', substrate='codex')
        self.assertFalse(out['template']['applied'])
        self.assertIn('sensitive', out['template']['note'])
        self.assertEqual(out['route'], [])
        # sensitive でなければ優先テンプレートの codex の手順が当たる (比べるための対照)
        self.write_facts(files_changed=1, modules=1, sensitive=False)
        out = self.route(role='executor', substrate='codex')
        self.assertTrue(out['template']['applied'])
        self.assertEqual(out['route'], [{'runner': 'codex', 'model': 'model-x', 'effort': 'high'}])

    def test_route_without_facts_works(self):
        (self.repo / 'docs/pbi/PBI-001.facts.json').unlink()
        out = self.route(role='executor', substrate='opencode')
        self.assertEqual([s['model'] for s in out['route']], ['opencode/model-a', 'opencode/model-b'])

    def test_route_rework_report_uses_the_original_facts(self):
        self.write_facts(sensitive=True)
        (self.repo / 'docs/pbi/PBI-001.rework-1.facts.json').write_text(json.dumps({'sensitive': False}),
                                                                       encoding='utf-8')
        out = self.ok('route', repo=str(self.repo), report='PBI-001.rework-1', role='executor', substrate='opencode')
        # PBI-001 の facts が sensitive なので agent だけになり、substrate opencode では全部外れる
        self.assertEqual(out['route'], [])
        self.assertEqual([(d['runner'], d['model']) for d in out['dropped']], [('agent', 'haiku')])

    def test_route_is_listed_and_callable_over_mcp(self):
        tools = self.server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})['result']['tools']
        found = [t for t in tools if t['name'] == 'route']
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['inputSchema']['required'], ['repo', 'report', 'role'])
        result = self.call('route', repo=str(self.repo), report='PBI-001', role='improver')
        self.assertFalse(result['isError'], result['content'][0]['text'])
        self.assertEqual(result['structuredContent']['route'][0]['runner'], 'agent')

    def test_protocol(self):
        init = self.server.handle({'jsonrpc': '2.0', 'id': 0, 'method': 'initialize',
                                   'params': {'protocolVersion': '2025-03-26'}})
        self.assertEqual(init['result']['protocolVersion'], '2025-03-26')
        self.assertEqual(init['result']['serverInfo']['name'], 'jev-mcp')
        self.assertIsNone(self.server.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'}))
        names = [t['name'] for t in self.server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
                 ['result']['tools']]
        self.assertEqual(names, ['list_tasks', 'claim_task', 'submit_result', 'fail_task', 'verify', 'jev_gate',
                                 'route', 'run_session', 'record'])
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

        def post(body, host=None):
            req = urllib.request.Request(f'http://127.0.0.1:{port}/mcp', data=json.dumps(body).encode(),
                                         headers={'Content-Type': 'application/json'})
            if host:
                req.add_header('Host', host)
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


FAKE_CLAUDE = '''import json, os, sys, time
record = {'argv': sys.argv[1:], 'stdin': sys.stdin.buffer.read().decode('utf-8'), 'cwd': os.getcwd(),
          'env': {k: os.environ.get(k) for k in json.loads(os.environ['FAKE_CLAUDE_KEYS'])}}
with open(os.environ['FAKE_CLAUDE_RECORD'], 'w', encoding='utf-8') as f:
    json.dump(record, f)
if os.environ.get('FAKE_CLAUDE_SLEEP'):
    time.sleep(float(os.environ['FAKE_CLAUDE_SLEEP']))
with open(os.environ['FAKE_CLAUDE_REPLY'], encoding='utf-8') as f:
    sys.stdout.write(f.read())
sys.exit(int(os.environ.get('FAKE_CLAUDE_EXIT', '0')))
'''
DROPPED = ['ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL', 'ANTHROPIC_BEDROCK_BASE_URL',
           'CLAUDE_CODE_USE_BEDROCK']
GOOD_REPLY = {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': '直した',
              'total_cost_usd': 0.25, 'usage': {'input_tokens': 11, 'output_tokens': 22}}


class RunSessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.allowed = self.tmp / 'allowed'
        self.work = self.allowed / 'work'
        self.work.mkdir(parents=True)
        self.outside = self.tmp / 'outside'
        self.outside.mkdir()
        (self.tmp / 'fake_claude.py').write_text(FAKE_CLAUDE, encoding='utf-8')
        if os.name == 'nt':
            fake = self.tmp / 'claude.cmd'
            fake.write_text(f'@"{sys.executable}" "{self.tmp / "fake_claude.py"}" %*\r\n', encoding='utf-8')
        else:
            fake = self.tmp / 'claude'
            fake.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{self.tmp / "fake_claude.py"}" "$@"\n',
                            encoding='utf-8')
            fake.chmod(0o755)
        self.record = self.tmp / 'record.json'
        self.reply = self.tmp / 'reply.json'
        self.set_reply(GOOD_REPLY)
        patch = mock.patch.dict(os.environ, {
            'PBI_CLAUDE_BIN': str(fake), 'FAKE_CLAUDE_RECORD': str(self.record), 'FAKE_CLAUDE_REPLY': str(self.reply),
            'FAKE_CLAUDE_KEYS': json.dumps(DROPPED + ['KEEP_ME']), 'KEEP_ME': 'yes',
            **{k: 'secret' for k in DROPPED}})
        patch.start()
        self.addCleanup(patch.stop)
        self.tasks = jev_mcp.Tasks([self.tmp], [self.allowed])
        self.server = jev_mcp.Server(self.tasks)

    def set_reply(self, value):
        self.reply.write_text(json.dumps(value) if not isinstance(value, str) else value, encoding='utf-8')

    def call(self, **arguments):
        r = self.server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                'params': {'name': 'run_session', 'arguments': arguments}})
        return r['result']

    def ok(self, **arguments):
        result = self.call(**arguments)
        self.assertFalse(result['isError'], result['content'][0]['text'])
        return result['structuredContent']

    def recorded(self):
        return json.loads(self.record.read_text(encoding='utf-8'))

    def test_fence_arguments_cwd_and_env(self):
        out = self.ok(cwd=str(self.work), mission='木を直して')
        argv = self.recorded()['argv']
        self.assertEqual(argv[:argv.index('-p') + 3],
                         ['--restricted', '--tools', 'Read,Edit,Write,Glob,Grep', '--permission-mode', 'acceptEdits',
                          '--permission-prompts', 'none', '--strict-mcp-config', '-p', '--output-format', 'json'])
        self.assertEqual(len(argv), 11)
        self.assertNotIn('--add-dir', argv)
        rec = self.recorded()
        self.assertEqual(Path(rec['cwd']).resolve(), self.work)
        self.assertEqual(rec['stdin'], '木を直して')
        self.assertEqual(rec['env']['KEEP_ME'], 'yes')
        for key in DROPPED:
            self.assertIsNone(rec['env'][key], key)
        self.assertTrue(out['ok'])

    def test_model_effort_and_system_change_arguments(self):
        self.ok(cwd=str(self.work), mission='m', model='sonnet', effort='high', system='短い指示')
        argv = self.recorded()['argv']
        self.assertEqual(argv[-6:], ['--model', 'sonnet', '--effort', 'high', '--append-system-prompt', '短い指示'])
        self.assertEqual(self.recorded()['stdin'], 'm')

    def test_long_system_goes_to_stdin(self):
        system = 'あ' * (jev_mcp.SYSTEM_ARG_LIMIT + 1)
        self.ok(cwd=str(self.work), mission='本題', system=system)
        rec = self.recorded()
        self.assertNotIn('--append-system-prompt', rec['argv'])
        self.assertTrue(rec['stdin'].startswith('# SYSTEM INSTRUCTIONS'))
        self.assertIn(system, rec['stdin'])
        self.assertTrue(rec['stdin'].endswith('本題'))

    def test_result_cost_tokens_and_fence(self):
        out = self.ok(cwd=str(self.work), mission='m')
        self.assertEqual((out['ok'], out['text'], out['cost_usd'], out['tokens'], out['unavailable']),
                         (True, '直した', 0.25, {'input': 11, 'output': 22}, False))
        self.assertEqual(out['fence'], {'tools': ['Read', 'Edit', 'Write', 'Glob', 'Grep'], 'cwd_only': True,
                                        'strict_mcp': True, 'no_prompts': True, 'restricted': True,
                                        'permission_mode': 'acceptEdits', 'dropped_env': DROPPED})
        self.assertIsNone(out['error'])
        self.assertIn('wall_s', out)

    def test_refusals_are_errors(self):
        r = self.call(cwd=str(self.outside), mission='m')
        self.assertTrue(r['isError'])
        self.assertIn('--session-root', r['content'][0]['text'])
        r = self.call(cwd=str(self.work / '..' / '..' / 'outside'), mission='m')
        self.assertTrue(r['isError'])
        self.assertTrue(self.call(cwd=str(self.work / 'nothing'), mission='m')['isError'])
        r = self.call(cwd=str(self.work), mission='m', runner='opencode')
        self.assertTrue(r['isError'])
        self.assertIn('agent', r['content'][0]['text'])
        self.assertTrue(self.call(cwd=str(self.work))['isError'])
        self.assertFalse(self.record.exists())

    def test_without_session_root_always_errors(self):
        self.server = jev_mcp.Server(jev_mcp.Tasks([self.tmp]))
        r = self.call(cwd=str(self.work), mission='m')
        self.assertTrue(r['isError'])
        self.assertIn('--session-root', r['content'][0]['text'])

    def test_is_error_gives_ok_false(self):
        self.set_reply({**GOOD_REPLY, 'is_error': True, 'result': '途中で失敗した'})
        out = self.ok(cwd=str(self.work), mission='m')
        self.assertEqual((out['ok'], out['unavailable'], out['text']), (False, False, '途中で失敗した'))
        self.set_reply({**GOOD_REPLY, 'subtype': 'error_max_turns'})
        self.assertFalse(self.ok(cwd=str(self.work), mission='m')['ok'])

    def test_usage_limit_is_unavailable(self):
        self.set_reply({**GOOD_REPLY, 'is_error': True, 'result': 'Claude AI usage limit reached'})
        out = self.ok(cwd=str(self.work), mission='m')
        self.assertEqual((out['ok'], out['unavailable']), (False, True))

    def test_unreadable_json_with_nonzero_exit_is_unavailable(self):
        self.set_reply('これは JSON ではない')
        with mock.patch.dict(os.environ, {'FAKE_CLAUDE_EXIT': '3'}):
            out = self.ok(cwd=str(self.work), mission='m')
        self.assertEqual((out['ok'], out['unavailable']), (False, True))

    def test_missing_cli_is_unavailable_not_an_error(self):
        with mock.patch.dict(os.environ, {'PBI_CLAUDE_BIN': str(self.tmp / 'no-such-claude')}):
            out = self.ok(cwd=str(self.work), mission='m')
        self.assertEqual((out['ok'], out['unavailable']), (False, True))
        self.assertIn('fence', out)

    def test_timeout_is_ok_false_but_not_unavailable(self):
        with mock.patch.dict(os.environ, {'FAKE_CLAUDE_SLEEP': '30'}):
            out = self.ok(cwd=str(self.work), mission='m', wall=2)
        self.assertEqual((out['ok'], out['unavailable']), (False, False))
        self.assertIn('timeout', out['error'])
        self.assertLess(out['wall_s'], 20)

    def test_listed_over_mcp_and_session_roots_helpers(self):
        tools = self.server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})['result']['tools']
        found = [t for t in tools if t['name'] == 'run_session']
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['inputSchema']['required'], ['cwd', 'mission'])
        env = {'PBI_MCP_SESSION_ROOTS': os.pathsep.join([str(self.allowed), str(self.outside)])}
        self.assertEqual(jev_mcp.session_roots_from([], env), [self.allowed, self.outside])
        self.assertEqual(jev_mcp.session_roots_from([str(self.work)], env), [self.work])
        self.assertEqual(jev_mcp.session_roots_from([], {}), [])


if __name__ == '__main__':
    unittest.main()
