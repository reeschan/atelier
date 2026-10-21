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

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import jev_mcp  # noqa: E402

SCRIPT = os.path.join(HERE, 'jev_mcp.py')

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

    def test_protocol(self):
        init = self.server.handle({'jsonrpc': '2.0', 'id': 0, 'method': 'initialize',
                                   'params': {'protocolVersion': '2025-03-26'}})
        self.assertEqual(init['result']['protocolVersion'], '2025-03-26')
        self.assertEqual(init['result']['serverInfo']['name'], 'jev-mcp')
        self.assertIsNone(self.server.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'}))
        names = [t['name'] for t in self.server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
                 ['result']['tools']]
        self.assertEqual(names, ['list_tasks', 'claim_task', 'submit_result', 'fail_task', 'verify', 'jev_gate'])
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
        self.assertEqual(len(json.loads(data)['result']['tools']), 6)
        self.assertEqual(post({'jsonrpc': '2.0', 'method': 'notifications/initialized'})[0], 202)
        self.assertEqual(post({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}, host='evil.example')[0], 403)


if __name__ == '__main__':
    unittest.main()
