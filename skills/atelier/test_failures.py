"""failures.py のテスト (失敗の文・space_id・差し戻しの埋め込み・冪等・外に出さないこと)。"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import events
import failures
import record_run

WORK_KEY = 'repo:main:W-1'


class FakeEmbedding:
    """POST /api/embed にベクトルを返し、POST /api/show に digest を返す偽のサーバー。"""

    def __init__(self, dim=4, digest='sha256:aaa', show=True):
        self.dim = dim
        self.digest = digest
        self.show = show
        self.inputs = []
        self.paths = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, fmt, *args):
                pass

            def reply(self, body, status=200):
                try:
                    self.send_response(status)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass

            def do_POST(self):
                length = int(self.headers.get('Content-Length') or 0)
                raw = self.rfile.read(length) if length else b'{}'
                try:
                    document = json.loads(raw.decode('utf-8'))
                except ValueError:
                    document = {}
                outer.paths.append(self.path)
                if self.path == '/api/show':
                    if not outer.show:
                        self.reply(b'{}', status=404)
                        return
                    self.reply(json.dumps({'digest': outer.digest}).encode('utf-8'))
                    return
                if self.path != '/api/embed':
                    self.reply(b'{}', status=404)
                    return
                texts = [str(text) for text in (document.get('input') or [])]
                outer.inputs.append(texts)
                self.reply(json.dumps({'embeddings': [outer.vector(text) for text in texts]}).encode('utf-8'))

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.port = self.server.server_address[1]
        self.url = f'http://127.0.0.1:{self.port}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def vector(self, text):
        seed = 7
        for char in text:
            seed = (seed * 31 + ord(char)) % 1000003
        return [float((seed >> (index * 3)) % 13 + 1) for index in range(self.dim)]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def make_temp():
    """一時フォルダ。tempfile.mkdtemp は 0o700 を渡すので、既定の mode で作る。"""
    path = Path(tempfile.gettempdir()) / f'pbi16-failures-{os.getpid()}-{uuid.uuid4().hex}'
    os.mkdir(path)
    return path


def sha12(parts):
    blob = json.dumps(parts, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(blob).hexdigest()[:12]


def run_row(report, attempt=1, rework=0, outcome='差し戻し', causes=('spec',), findings=None,
            work_key=WORK_KEY, repo='repo', run_id=None):
    pbi = report.split('.rework-')[0]
    row = {
        'schema': 1, 'repo': repo, 'work_key': work_key, 'pbi': pbi, 'report': report,
        'attempt': attempt, 'rework': rework, 'tier': 'sonnet', 'runner': 'dsh', 'model': 'm',
        'judged': {'outcome': outcome, 'findings': findings,
                   'cause': list(causes) if causes is not None else None},
    }
    if run_id is not None:
        row['run_id'] = run_id
    return row


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp()
        self._env = dict(os.environ)
        sender = self.tmp / 'no-sender'
        sender.mkdir()
        os.environ['HARNESS_LOG_PATH'] = str(sender)
        os.environ['HARNESS_LOG_CONFIG'] = str(self.tmp / 'config.json')
        os.environ['PBI_RUNS_ARCHIVE'] = str(self.tmp / 'archive')
        os.environ['HOME'] = str(self.tmp / 'home')
        os.environ['USERPROFILE'] = str(self.tmp / 'home')
        (self.tmp / 'home').mkdir()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers ----

    def make_base(self):
        base = self.tmp / 'docs' / 'pbi'
        (base / 'conductor').mkdir(parents=True)
        (base / 'conductor' / 'run.json').write_text(json.dumps({'run_id': 'r1'}), encoding='utf-8')
        (base / 'STATUS.md').write_text(f'Work key: {WORK_KEY}\n', encoding='utf-8')
        (base / 'reviews').mkdir()
        return base

    def write_rows(self, base, rows):
        (base / 'runs.jsonl').write_text(
            ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows), encoding='utf-8')

    def write_config(self, embedding):
        Path(os.environ['HARNESS_LOG_CONFIG']).write_text(json.dumps({'embedding': embedding}),
                                                          encoding='utf-8')

    def vectors_path(self, repo='repo', work_key=WORK_KEY):
        return Path(os.environ['PBI_RUNS_ARCHIVE']) / repo / record_run.key_dir(work_key) / 'vectors.jsonl'

    def vectors(self):
        path = self.vectors_path()
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = failures.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    # ---- failure_text ----

    def test_failure_text_order_cut_and_clean(self):
        row = run_row('PBI-9', causes=['model', 'spec'],
                      findings={'重大': 1, '中': 0, '軽微': 2})
        review = {'findings': [{'why': 'a' * 500}, {'why': 'b\nline | x'}]}
        note = '## Rework 1\n\n- Now: nope\n- Expected: ' + 'c' * 500 + '\n- Repro: not this\n'
        text = failures.failure_text(row, note, review)
        parts = text.split(' | ')
        self.assertEqual(len(parts), 5)
        self.assertEqual(parts[0], 'spec,model')
        self.assertEqual(parts[1], 'critical=1,major=0,minor=2')
        self.assertEqual(parts[2], 'a' * 400)
        self.assertEqual(parts[3], 'unknown')
        self.assertEqual(parts[4], 'c' * 400)
        # 無い項は none、Repro は拾わない、自由文の区切りは崩さない
        self.assertEqual(failures.failure_text({}, None, None), 'none | none | none | unknown | none')
        self.assertNotIn('not this', failures.failure_text(row, note, None))
        self.assertNotIn(' | ', failures._clean('b\nline | x'))  # noqa: SLF001

    def test_failure_text_joins_why_and_expected_in_order(self):
        row = run_row('PBI-9', causes=None, findings=None)
        review = {'findings': [{'why': 'first'}, {'why': 'second'}, {'no_why': True}]}
        note = '- Expected: one\n  - Expected: two\n'
        parts = failures.failure_text(row, note, review).split(' | ')
        self.assertEqual(parts[0], 'none')
        self.assertEqual(parts[1], 'none')
        self.assertEqual(parts[2], 'first; second')
        self.assertEqual(parts[4], 'one; two')

    # ---- space_id ----

    def test_space_id_is_sha12_of_model_digest_dims_template_and_kind(self):
        server = FakeEmbedding(dim=3, digest='sha256:one')
        self.addCleanup(server.close)
        config = {'url': server.url, 'model': 'fake-embed', 'dims': 3, 'timeout': 5}
        expected = sha12(['fake-embed', 'sha256:one', 3, 'failure/1', 'failure'])
        self.assertEqual(failures.space_id(config, 'failure/1'), expected)
        server.digest = 'sha256:two'
        self.assertNotEqual(failures.space_id(config, 'failure/1'), expected)
        self.assertEqual(failures.space_id(config, 'failure/2'),
                         sha12(['fake-embed', 'sha256:two', 3, 'failure/2', 'failure']))

    def test_space_id_uses_none_when_show_does_not_answer(self):
        server = FakeEmbedding(dim=3, show=False)
        self.addCleanup(server.close)
        config = {'url': server.url, 'model': 'fake-embed', 'dims': 3, 'timeout': 5}
        self.assertEqual(failures.space_id(config, 'failure/1'),
                         sha12(['fake-embed', 'none', 3, 'failure/1', 'failure']))
        self.assertIn('/api/show', server.paths)

    # ---- embed_failures ----

    def test_embeds_only_sent_back_rows_and_does_not_repeat(self):
        base = self.make_base()
        self.write_rows(base, [
            run_row('PBI-2', outcome='合格', causes=None,
                    findings={'重大': 0, '中': 0, '軽微': 0}),
            run_row('PBI-1', attempt=1, rework=0, causes=['scope', 'spec'],
                    findings={'重大': 1, '中': 2, '軽微': 0}),
            run_row('PBI-1.rework-1', attempt=1, rework=1, causes=['model'],
                    findings={'重大': 0, '中': 1, '軽微': 0}),
        ])
        (base / 'PBI-1.rework-1.md').write_text(
            '## Rework 1\n\n- Now: x\n- Expected: fixed the thing\n', encoding='utf-8')
        (base / 'reviews' / 'PBI-1.review.json').write_text(
            json.dumps({'findings': [{'severity': 'major', 'why': 'the why of it'}]}), encoding='utf-8')
        # 先に task のベクトル行を置いても消さない (R6)
        task_row = {'kind': 'task', 'case_id': 'keep-me', 'model': 'fake-embed', 'dim': 3, 'vector': [1, 2, 3]}
        self.vectors_path().parent.mkdir(parents=True, exist_ok=True)
        self.vectors_path().write_text(json.dumps(task_row) + '\n', encoding='utf-8')

        server = FakeEmbedding(dim=6, digest='sha256:aaa')
        self.addCleanup(server.close)
        self.write_config({'url': server.url, 'model': 'fake-embed'})

        code, out, err = self.run_main('failure-embed', '--dir', str(base))
        self.assertEqual(code, 0, err)
        self.assertEqual(out, '')
        self.assertEqual(err, '')

        vectors = self.vectors()
        self.assertEqual(len(vectors), 3)
        self.assertEqual(vectors[0], task_row)
        found = [row for row in vectors if row.get('kind') == 'failure']
        by_unit = {row['unit_id']: row for row in found}
        # PBI-1 と PBI-1.rework-1 は別の行 (report を unit_id に入れる)
        self.assertEqual(set(by_unit), {'at:r1:PBI-1:1', 'at:r1:PBI-1.rework-1:1'})
        expected_space = sha12(['fake-embed', 'sha256:aaa', 6, 'failure/1', 'failure'])
        for row in found:
            self.assertEqual(row['space_id'], expected_space)
            self.assertEqual(row['case_id'], f'repo:{WORK_KEY}:PBI-1')
            self.assertEqual(row['model'], 'fake-embed')
            self.assertEqual(row['dim'], 6)
            self.assertEqual(len(row['vector']), 6)
        # 差し戻し 2 行だけを埋め込む (合格の行は入らない)
        self.assertEqual(len(server.inputs), 1)
        self.assertEqual(len(server.inputs[0]), 2)
        text = '\n'.join(server.inputs[0])
        self.assertIn('spec,scope | critical=1,major=2,minor=0 | the why of it | unknown | fixed the thing',
                      text)
        self.assertIn('model | critical=0,major=1,minor=0 | none | unknown | none', text)
        # 受けた文は保全先の行にも events.jsonl にも入らない
        raw = self.vectors_path().read_text(encoding='utf-8')
        for item in server.inputs[0]:
            self.assertNotIn(item, raw)
        self.assertNotIn('the why of it', raw)
        self.assertNotIn('fixed the thing', raw)
        self.assertFalse((base / 'events.jsonl').exists())

        # 2 回目は同じ (space_id, unit_id) の行を足さない
        code, out, err = self.run_main('failure-embed', '--dir', str(base))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.vectors()), 3)

    def test_unit_id_uses_the_rows_own_run_id_not_the_current_run(self):
        # 記録した run (run-A) と failure-embed を回す run が違っても、行の run_id を使う
        base = self.make_base()
        self.write_rows(base, [run_row('PBI-1', attempt=1, run_id='run-A')])
        (base / 'conductor' / 'run.json').write_text(json.dumps({'run_id': 'run-B'}), encoding='utf-8')
        server = FakeEmbedding(dim=4)
        self.addCleanup(server.close)
        self.write_config({'url': server.url, 'model': 'fake-embed'})

        self.assertEqual(failures.embed_failures(base), 1)
        self.assertEqual([row['unit_id'] for row in self.vectors()], ['at:run-A:PBI-1:1'])

        # 今の run が変わっても、同じ unit_id の行は足さない
        (base / 'conductor' / 'run.json').write_text(json.dumps({'run_id': 'run-C'}), encoding='utf-8')
        self.assertEqual(failures.embed_failures(base), 0)
        self.assertEqual([row['unit_id'] for row in self.vectors()], ['at:run-A:PBI-1:1'])

    def test_show_failure_still_embeds_with_digest_none(self):
        base = self.make_base()
        self.write_rows(base, [run_row('PBI-1')])
        server = FakeEmbedding(dim=4, show=False)
        self.addCleanup(server.close)
        self.write_config({'url': server.url, 'model': 'fake-embed'})
        code, out, err = self.run_main('failure-embed', '--dir', str(base))
        self.assertEqual(code, 0, err)
        rows = self.vectors()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['space_id'],
                         sha12(['fake-embed', 'none', 4, 'failure/1', 'failure']))

    def test_rows_of_another_work_key_are_not_embedded(self):
        base = self.make_base()
        self.write_rows(base, [
            run_row('PBI-3', work_key='repo:other:W-9'),
            run_row('PBI-1'),
        ])
        server = FakeEmbedding(dim=4)
        self.addCleanup(server.close)
        self.write_config({'url': server.url, 'model': 'fake-embed'})
        code, out, err = self.run_main('failure-embed', '--dir', str(base))
        self.assertEqual(code, 0, err)
        self.assertEqual(out, '')
        self.assertEqual([row['unit_id'] for row in self.vectors()], ['at:r1:PBI-1:1'])
        self.assertIn('別の作業の差し戻し 1 行', err)
        self.assertEqual(len(server.inputs[0]), 1)

    def test_unreachable_url_exits_zero_with_one_line(self):
        base = self.make_base()
        self.write_rows(base, [run_row('PBI-1')])
        self.write_config({'url': f'http://127.0.0.1:{free_port()}', 'model': 'fake-embed',
                           'timeout': 2})
        code, out, err = self.run_main('failure-embed', '--dir', str(base))
        self.assertEqual(code, 0)
        self.assertEqual(out, '')
        lines = [line for line in err.strip().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        self.assertIn('0 件', lines[0])
        self.assertEqual(self.vectors(), [])

    def test_nothing_to_embed_writes_nothing(self):
        base = self.make_base()
        self.write_rows(base, [run_row('PBI-1', outcome='合格', causes=None, findings=None)])
        server = FakeEmbedding(dim=4)
        self.addCleanup(server.close)
        self.write_config({'url': server.url, 'model': 'fake-embed'})
        code, out, err = self.run_main('failure-embed', '--dir', str(base))
        self.assertEqual(code, 0, err)
        self.assertEqual(server.inputs, [])
        self.assertFalse(self.vectors_path().exists())

    def test_bad_argument_exits_two(self):
        code, out, err = self.run_main('failure-embed', '--nope', 'x')
        self.assertEqual(code, 2)
        self.assertNotEqual(err, '')

    def test_events_py_delegates_failure_embed(self):
        base = self.make_base()
        self.write_rows(base, [run_row('PBI-1')])
        server = FakeEmbedding(dim=4)
        self.addCleanup(server.close)
        self.write_config({'url': server.url, 'model': 'fake-embed'})
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = events.main(['failure-embed', '--dir', str(base)])
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual([row['unit_id'] for row in self.vectors()], ['at:r1:PBI-1:1'])


if __name__ == '__main__':
    unittest.main()
