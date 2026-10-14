import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import serve  # noqa: E402

with open(os.path.join(HERE, '..', 'routing.default.json'), encoding='utf-8') as f:
    GOOD = json.load(f)


class ServeTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.target = Path(self.dir.name)
        self.applied = []
        self.server = serve.make_server(self.target, 0, 'tok', self.applied.append)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.dir.cleanup()

    def request(self, method, path, body=None, token='tok', host=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        headers = {'Host': host or f'127.0.0.1:{self.port}', 'Content-Type': 'application/json'}
        if token:
            headers['X-Studio-Token'] = token
        data = json.dumps(body).encode('utf-8') if body is not None else None
        conn.request(method, path, body=data, headers=headers)
        r = conn.getresponse()
        out = r.status, r.read()
        conn.close()
        return out

    def test_page_embeds_target_and_token(self):
        code, body = self.request('GET', '/')
        self.assertEqual(code, 200)
        self.assertIn(b'"token": "tok"', body)
        self.assertIn(b'routing.json', body)

    def test_wrong_token_or_host_is_refused(self):
        self.assertEqual(self.request('POST', '/apply', GOOD, token='nope')[0], 403)
        self.assertEqual(self.request('POST', '/apply', GOOD, host='evil.example:80')[0], 403)
        self.assertEqual(self.request('GET', '/', host='evil.example:80')[0], 403)
        self.assertFalse((self.target / 'docs' / 'pbi' / 'routing.json').exists())

    def test_invalid_template_is_refused(self):
        bad = {'tiers': dict(GOOD['tiers'], opus=[{'runner': 'codex', 'model': 'x'}])}
        code, body = self.request('POST', '/apply', bad)
        self.assertEqual(code, 400)
        self.assertIn('agent', json.loads(body)['error'])
        self.assertEqual(self.applied, [])

    def test_symlinked_destination_is_refused(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        (self.target / 'docs').mkdir()
        (self.target / 'docs' / 'pbi').symlink_to(outside.name, target_is_directory=True)
        code, body = self.request('POST', '/apply', GOOD)
        self.assertEqual(code, 400)
        self.assertEqual(os.listdir(outside.name), [])
        self.assertEqual(self.applied, [])

    def test_apply_writes_and_keeps_previous(self):
        out = self.target / 'docs' / 'pbi' / 'routing.json'
        out.parent.mkdir(parents=True)
        out.write_text('{"old": true}', encoding='utf-8')
        code, body = self.request('POST', '/apply', dict(GOOD, name='mine'))
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(out.read_text(encoding='utf-8'))['name'], 'mine')
        self.assertEqual(json.loads((out.parent / 'routing.prev.json').read_text(encoding='utf-8')), {'old': True})
        self.assertEqual(self.applied, [out.resolve()])
        self.assertEqual(sorted(p.name for p in out.parent.iterdir()), ['routing.json', 'routing.prev.json'])


if __name__ == '__main__':
    unittest.main()
