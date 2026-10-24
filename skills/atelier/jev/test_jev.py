"""jev.py のテスト。127.0.0.1 のモックサーバーだけを相手にする (本物の Jev は呼ばない)。"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import jev  # noqa: E402

DUMMY_KEY = 'dummy-test-key'
OK_BODY = {
    'model': 'jev-1.13.0',
    'answers': {'k1': {'type': 'choice', 'choice': 'blue', 'confidence': 0.82}},
    'usage': {'input_tokens': 1, 'output_tokens': 1},
}
QUESTIONS = {'k1': {'type': 'choice', 'instructions': 'Which?', 'criteria': {'blue': 'b', 'red': 'r'}}}
MISSING_CRED = os.path.join(tempfile.gettempdir(), 'jev-test-no-such-dir', 'credentials.json')


class MockJev:
    """応答の列 [(status, headers, body)] を順に返す。尽きたら最後の応答を繰り返す。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        mock_self = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get('Content-Length') or 0)
                raw = self.rfile.read(length)
                mock_self.requests.append({'headers': dict(self.headers), 'body': raw.decode('utf-8')})
                idx = min(len(mock_self.requests) - 1, len(mock_self.responses) - 1)
                status, headers, body = mock_self.responses[idx]
                if isinstance(body, str):
                    data = body.encode('utf-8')
                else:
                    data = json.dumps(body).encode('utf-8')
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}/v1/systemone'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def sleep_recorder():
    calls = []
    return calls, calls.append


class CallJevTest(unittest.TestCase):
    def call(self, mock_server, **kw):
        kw.setdefault('api_key', DUMMY_KEY)
        kw.setdefault('sleep', lambda s: None)
        return jev.call_jev('some text', QUESTIONS, 'jev-1.13.0', endpoint=mock_server.url, **kw)

    def test_1_ok_returns_json_and_sends_body(self):
        with MockJev([(200, {}, OK_BODY)]) as m:
            result = self.call(m)
        self.assertEqual(result, OK_BODY)
        self.assertEqual(len(m.requests), 1)
        sent = json.loads(m.requests[0]['body'])
        self.assertEqual(sent['state'], 'some text')
        self.assertEqual(sent['model'], 'jev-1.13.0')
        self.assertEqual(sent['questions'], QUESTIONS)
        self.assertEqual(m.requests[0]['headers'].get('Authorization'), f'Bearer {DUMMY_KEY}')

    def test_2_429_then_200(self):
        waits, sleep = sleep_recorder()
        with MockJev([(429, {'retry-after': '0'}, {'error': 'rate'}), (200, {}, OK_BODY)]) as m:
            result = self.call(m, sleep=sleep)
        self.assertEqual(result, OK_BODY)
        self.assertEqual(len(m.requests), 2)
        self.assertEqual(waits, [0])

    def test_3_401_no_retry(self):
        with MockJev([(401, {}, {'error': 'unauthorized'})]) as m:
            with self.assertRaises(jev.JevError) as cm:
                self.call(m)
        self.assertEqual(len(m.requests), 1)
        self.assertEqual(cm.exception.code, 'http')
        self.assertEqual(cm.exception.status, 401)
        self.assertNotIn(DUMMY_KEY, str(cm.exception))

    def test_4_529_retries_three_times(self):
        waits, sleep = sleep_recorder()
        with MockJev([(529, {}, {'error': 'overloaded'})]) as m:
            with self.assertRaises(jev.JevError) as cm:
                self.call(m, sleep=sleep)
        self.assertEqual(len(m.requests), 4)
        self.assertEqual(cm.exception.status, 529)
        self.assertEqual(waits, [1, 2, 4])

    def test_5_500_retries_once(self):
        waits, sleep = sleep_recorder()
        with MockJev([(500, {}, {'error': 'boom'})]) as m:
            with self.assertRaises(jev.JevError) as cm:
                self.call(m, sleep=sleep)
        self.assertEqual(len(m.requests), 2)
        self.assertEqual(cm.exception.code, 'http')
        self.assertEqual(cm.exception.status, 500)
        self.assertEqual(waits, [])

    def test_6_no_key_does_not_call_http(self):
        with mock.patch.dict(os.environ, {'TYPESAFE_CREDENTIALS_FILE': MISSING_CRED}):
            os.environ.pop('TYPESAFE_API_KEY', None)
            self.assertIsNone(jev.get_api_key())
            with MockJev([(200, {}, OK_BODY)]) as m:
                with self.assertRaises(jev.JevError) as cm:
                    jev.call_jev('t', QUESTIONS, 'jev-1.13.0', endpoint=m.url, sleep=lambda s: None)
        self.assertEqual(cm.exception.code, 'no-key')
        self.assertEqual(len(m.requests), 0)

    def test_6b_key_from_credentials_file_and_env_priority(self):
        with tempfile.TemporaryDirectory() as d:
            cred = os.path.join(d, 'credentials.json')
            with open(cred, 'w', encoding='utf-8') as f:
                json.dump({'api_key': 'file-dummy'}, f)
            with mock.patch.dict(os.environ, {'TYPESAFE_CREDENTIALS_FILE': cred}):
                os.environ.pop('TYPESAFE_API_KEY', None)
                with MockJev([(200, {}, OK_BODY)]) as m:
                    jev.call_jev('t', QUESTIONS, 'jev-1.13.0', endpoint=m.url, sleep=lambda s: None)
                self.assertEqual(m.requests[0]['headers'].get('Authorization'), 'Bearer file-dummy')
                os.environ.update({'TYPESAFE_API_KEY': 'env-dummy'})
                with MockJev([(200, {}, OK_BODY)]) as m2:
                    jev.call_jev('t', QUESTIONS, 'jev-1.13.0', endpoint=m2.url, sleep=lambda s: None)
                self.assertEqual(m2.requests[0]['headers'].get('Authorization'), 'Bearer env-dummy')

    def test_6c_get_api_key_with_explicit_env(self):
        self.assertEqual(jev.get_api_key({'TYPESAFE_API_KEY': 'x'}), 'x')
        self.assertIsNone(jev.get_api_key({'TYPESAFE_CREDENTIALS_FILE': MISSING_CRED}))

    def test_7_missing_answers_is_bad_response(self):
        with MockJev([(200, {}, {'x': 1})]) as m:
            with self.assertRaises(jev.JevError) as cm:
                self.call(m)
        self.assertEqual(cm.exception.code, 'bad-response')
        self.assertEqual(len(m.requests), 1)

    def test_7b_non_json_is_bad_response(self):
        with MockJev([(200, {}, 'not json')]) as m:
            with self.assertRaises(jev.JevError) as cm:
                self.call(m)
        self.assertEqual(cm.exception.code, 'bad-response')


class CliTest(unittest.TestCase):
    def run_cli(self, args):
        env = dict(os.environ)
        env.update({'TYPESAFE_API_KEY': DUMMY_KEY, 'TYPESAFE_CREDENTIALS_FILE': MISSING_CRED})
        return subprocess.run(
            [sys.executable, os.path.join(HERE, 'jev.py'), *args],
            capture_output=True, env=env, timeout=60,
        )

    def test_8_config_mode(self):
        with tempfile.TemporaryDirectory() as d, \
                MockJev([(429, {'retry-after': '0'}, {}), (200, {}, OK_BODY)]) as m:
            config = {
                'endpoint': m.url,
                'model': 'jev-1.13.0',
                'gates': {'g1': {'k1': {**QUESTIONS['k1'], 'extra': 'ignored'}}},
            }
            cfg = os.path.join(d, 'config.json')
            txt = os.path.join(d, 'text.txt')
            with open(cfg, 'w', encoding='utf-8') as f:
                json.dump(config, f)
            with open(txt, 'w', encoding='utf-8') as f:
                f.write('日本語の文章')
            p = self.run_cli([cfg, 'g1', txt])
        self.assertEqual(p.returncode, 0, p.stderr.decode('utf-8', 'replace'))
        self.assertEqual(json.loads(p.stdout.decode('utf-8')), OK_BODY)
        sent = json.loads(m.requests[-1]['body'])
        self.assertEqual(sent['state'], '日本語の文章')
        self.assertEqual(sent['questions'], QUESTIONS)

    def test_9_request_mode(self):
        with tempfile.TemporaryDirectory() as d, MockJev([(200, {}, OK_BODY)]) as m:
            req = os.path.join(d, 'request.json')
            with open(req, 'w', encoding='utf-8') as f:
                json.dump({'state': {'product': 'p'}, 'model': 'jev-1.13.0',
                           'questions': QUESTIONS, 'endpoint': m.url}, f)
            p = self.run_cli(['--request', req])
        self.assertEqual(p.returncode, 0, p.stderr.decode('utf-8', 'replace'))
        self.assertEqual(json.loads(p.stdout.decode('utf-8')), OK_BODY)
        self.assertEqual(json.loads(m.requests[0]['body'])['state'], {'product': 'p'})

    def test_9b_jev_failure_exit_1(self):
        with tempfile.TemporaryDirectory() as d, MockJev([(401, {}, {'error': 'no'})]) as m:
            req = os.path.join(d, 'request.json')
            with open(req, 'w', encoding='utf-8') as f:
                json.dump({'state': 's', 'model': 'jev-1.13.0', 'questions': QUESTIONS, 'endpoint': m.url}, f)
            p = self.run_cli(['--request', req])
        self.assertEqual(p.returncode, 1)
        self.assertIn('error', json.loads(p.stdout.decode('utf-8')))

    def test_9c_no_args_exit_2(self):
        p = self.run_cli([])
        self.assertEqual(p.returncode, 2)
        self.assertIn(b'usage', p.stderr)


if __name__ == '__main__':
    unittest.main()
