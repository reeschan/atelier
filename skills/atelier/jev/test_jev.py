"""jev.py のテスト。127.0.0.1 のモックサーバーだけを相手にする (本物の Jev は呼ばない)。"""
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
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


def reset_endpoint():
    """受けた接続をすぐ RST する。閉じたポートは Windows でタイムアウトになることがある。"""
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.listen(8)
    url = f'http://127.0.0.1:{sock.getsockname()[1]}/v1/systemone'
    stop = threading.Event()

    def serve():
        sock.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
                conn.close()
            except OSError:
                pass

    threading.Thread(target=serve, daemon=True).start()
    return sock, stop, url


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

    def test_3_401_redacts_a_key_that_crosses_the_excerpt(self):
        key = 'cross-boundary-secret-key'
        body = 'x' * 490 + key + '-tail'
        with MockJev([(401, {}, body)]) as m:
            with self.assertRaises(jev.JevError) as cm:
                self.call(m, api_key=key)
        message = str(cm.exception)
        self.assertEqual(cm.exception.code, 'http')
        self.assertNotIn(key, message)
        self.assertNotIn('cross-boundary', message)
        self.assertIn('[redacted]', message)
        self.assertLessEqual(len(message), 500 + len('HTTP 401: '))

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

    def test_6d_timeout_and_network_codes(self):
        class Hang(BaseHTTPRequestHandler):
            def do_POST(self):
                time.sleep(5)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Hang)
        server.daemon_threads = True
        url = f'http://127.0.0.1:{server.server_address[1]}/v1/systemone'
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(jev.JevError) as cm:
                jev.call_jev('t', QUESTIONS, 'jev-1.13.0', endpoint=url, api_key=DUMMY_KEY,
                             timeout=0.2, sleep=lambda s: None)
            self.assertEqual(cm.exception.code, 'timeout')
            self.assertNotIn(DUMMY_KEY, str(cm.exception))
        finally:
            server.shutdown()
            server.server_close()
        sock, stop, url = reset_endpoint()
        try:
            with self.assertRaises(jev.JevError) as cm:
                jev.call_jev('t', QUESTIONS, 'jev-1.13.0', endpoint=url,
                             api_key=DUMMY_KEY, timeout=1, sleep=lambda s: None)
            self.assertEqual(cm.exception.code, 'network')
        finally:
            stop.set()
            sock.close()

    def test_6e_public_code_splits_http_status(self):
        self.assertEqual(jev.public_code(jev.JevError('x', 'http', 401)), 'http-401')
        self.assertEqual(jev.public_code(jev.JevError('x', 'http', 403)), 'http-403')
        self.assertEqual(jev.public_code(jev.JevError('x', 'http', 500)), 'http-500')
        self.assertEqual(jev.public_code(jev.JevError('x', 'network')), 'network')
        self.assertEqual(jev.public_code(jev.JevError('x', 'timeout')), 'timeout')
        self.assertEqual(jev.public_code(jev.JevError('x', 'no-key')), 'no-key')

    def test_7_missing_answers_is_bad_response(self):
        with MockJev([(200, {}, {'x': 1})]) as m:
            with self.assertRaises(jev.JevError) as cm:
                self.call(m)
        self.assertEqual(cm.exception.code, 'bad-response')
        self.assertEqual(len(m.requests), 1)

    def test_7b_non_json_redacts_a_key_before_the_excerpt(self):
        key = 'cross-boundary-secret-key'
        with MockJev([(200, {}, 'x' * 490 + key + '-tail')]) as m:
            with self.assertRaises(jev.JevError) as cm:
                self.call(m, api_key=key)
        message = str(cm.exception)
        self.assertEqual(cm.exception.code, 'bad-response')
        self.assertNotIn(key, message)
        self.assertNotIn('cross-boundary', message)

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
        body = json.loads(p.stdout.decode('utf-8'))
        self.assertEqual(set(body), {'error', 'code', 'where'})
        self.assertEqual(body['code'], 'http-401')
        self.assertEqual(body['where'], 'env')
        self.assertNotIn(DUMMY_KEY, p.stdout.decode('utf-8'))

    def test_9d_403_is_http_403(self):
        with tempfile.TemporaryDirectory() as d, MockJev([(403, {}, {'error': 'forbidden'})]) as m:
            req = os.path.join(d, 'request.json')
            with open(req, 'w', encoding='utf-8') as f:
                json.dump({'state': 's', 'model': 'jev-1.13.0', 'questions': QUESTIONS, 'endpoint': m.url}, f)
            p = self.run_cli(['--request', req])
        self.assertEqual(p.returncode, 1)
        body = json.loads(p.stdout.decode('utf-8'))
        self.assertEqual(body['code'], 'http-403')
        self.assertEqual(body['where'], 'env')
        self.assertNotIn(DUMMY_KEY, body['error'])

    def test_9e_network_and_timeout_codes(self):
        sock, stop, url = reset_endpoint()
        try:
            with tempfile.TemporaryDirectory() as d:
                req = os.path.join(d, 'request.json')
                with open(req, 'w', encoding='utf-8') as f:
                    json.dump({'state': 's', 'model': 'jev-1.13.0', 'questions': QUESTIONS,
                               'endpoint': url, 'timeout': 1}, f)
                p = self.run_cli(['--request', req])
        finally:
            stop.set()
            sock.close()
        body = json.loads(p.stdout.decode('utf-8'))
        self.assertEqual(p.returncode, 1)
        self.assertEqual(body['code'], 'network')
        self.assertEqual(body['where'], 'env')

        class Hang(BaseHTTPRequestHandler):
            def do_POST(self):
                time.sleep(5)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Hang)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as d:
                req = os.path.join(d, 'request.json')
                with open(req, 'w', encoding='utf-8') as f:
                    json.dump({'state': 's', 'model': 'jev-1.13.0', 'questions': QUESTIONS,
                               'endpoint': f'http://127.0.0.1:{server.server_address[1]}/v1/systemone',
                               'timeout': 0.2}, f)
                p = self.run_cli(['--request', req])
        finally:
            server.shutdown()
            server.server_close()
        body = json.loads(p.stdout.decode('utf-8'))
        self.assertEqual(p.returncode, 1, body)
        self.assertEqual(body['code'], 'timeout')
        self.assertEqual(body['where'], 'env')
        self.assertNotIn(DUMMY_KEY, p.stdout.decode('utf-8'))

    def test_9c_no_args_exit_2(self):
        p = self.run_cli([])
        self.assertEqual(p.returncode, 2)
        self.assertIn(b'usage', p.stderr)


class KeyLookupTest(unittest.TestCase):
    def env(self, home, **extra):
        found = {'HOME': home, 'USERPROFILE': home}
        found.update(extra)
        return found

    def test_env_missing_file_missing_bad_json_and_no_api_key(self):
        with tempfile.TemporaryDirectory() as d:
            missing_home = self.env(d)
            none = jev.resolve_api_key(missing_home)
            self.assertIsNone(none.key)
            self.assertEqual(none.where, 'env無し')
            self.assertIsNone(jev.get_api_key(missing_home))
            self.assertEqual(jev.JevError('Jev の API キーが見つからない', 'no-key', where=none.where).code, 'no-key')

            missing = os.path.join(d, 'no-such.json')
            file_env = self.env(d, TYPESAFE_CREDENTIALS_FILE=missing)
            found = jev.resolve_api_key(file_env)
            self.assertIsNone(found.key)
            self.assertEqual(found.where, f'ファイル無し: {missing}')
            self.assertEqual(jev.get_api_key(file_env), None)

            broken = os.path.join(d, 'broken.json')
            with open(broken, 'w', encoding='utf-8') as f:
                f.write('{')
            broken_env = self.env(d, TYPESAFE_CREDENTIALS_FILE=broken)
            found = jev.resolve_api_key(broken_env)
            self.assertIsNone(found.key)
            self.assertEqual(found.where, f'JSONが壊れている: {broken}')

            nokey = os.path.join(d, 'nokey.json')
            with open(nokey, 'w', encoding='utf-8') as f:
                json.dump({'token': 'not-the-field'}, f)
            nokey_env = self.env(d, TYPESAFE_CREDENTIALS_FILE=nokey)
            found = jev.resolve_api_key(nokey_env)
            self.assertIsNone(found.key)
            self.assertEqual(found.where, f'api_keyが無い: {nokey}')

            bom = os.path.join(d, 'bom.json')
            with open(bom, 'wb') as f:
                f.write(b'\xef\xbb\xbf{"api_key": "bom-key"}')
            bom_env = self.env(d, TYPESAFE_CREDENTIALS_FILE=bom)
            found = jev.resolve_api_key(bom_env)
            self.assertEqual(found.key, 'bom-key')
            self.assertEqual(found.where, bom)
            self.assertEqual(jev.get_api_key(bom_env), 'bom-key')

            from_env = jev.resolve_api_key(self.env(d, TYPESAFE_API_KEY='env-key', TYPESAFE_CREDENTIALS_FILE=missing))
            self.assertEqual((from_env.key, from_env.where), ('env-key', 'env'))

    def test_cli_reports_each_no_key_reason(self):
        def run(env, req_path):
            return subprocess.run(
                [sys.executable, os.path.join(HERE, 'jev.py'), '--request', req_path],
                capture_output=True, env=env, timeout=30,
            )

        with tempfile.TemporaryDirectory() as d:
            req = os.path.join(d, 'request.json')
            with open(req, 'w', encoding='utf-8') as f:
                json.dump({'state': 's', 'model': 'm', 'questions': QUESTIONS,
                           'endpoint': 'http://127.0.0.1:9/unused'}, f)
            cases = {
                'env無し': {'HOME': d, 'USERPROFILE': d},
            }
            missing = os.path.join(d, 'missing.json')
            broken = os.path.join(d, 'broken.json')
            nokey = os.path.join(d, 'nokey.json')
            with open(broken, 'w', encoding='utf-8') as f:
                f.write('{')
            with open(nokey, 'w', encoding='utf-8') as f:
                json.dump({}, f)
            cases[f'ファイル無し: {missing}'] = {'HOME': d, 'USERPROFILE': d, 'TYPESAFE_CREDENTIALS_FILE': missing}
            cases[f'JSONが壊れている: {broken}'] = {'HOME': d, 'USERPROFILE': d, 'TYPESAFE_CREDENTIALS_FILE': broken}
            cases[f'api_keyが無い: {nokey}'] = {'HOME': d, 'USERPROFILE': d, 'TYPESAFE_CREDENTIALS_FILE': nokey}
            for where, extra in cases.items():
                with self.subTest(where=where):
                    env = dict(os.environ)
                    env.pop('TYPESAFE_API_KEY', None)
                    env.pop('TYPESAFE_CREDENTIALS_FILE', None)
                    env.update(extra)
                    p = run(env, req)
                    self.assertEqual(p.returncode, 1, p.stderr)
                    body = json.loads(p.stdout.decode('utf-8'))
                    self.assertEqual(body['code'], 'no-key')
                    self.assertEqual(body['where'], where)
                    joined = p.stdout.decode('utf-8') + p.stderr.decode('utf-8', 'replace')
                    self.assertNotIn('bom-key', joined)
                    self.assertNotIn('env-key', joined)


if __name__ == '__main__':
    unittest.main()
