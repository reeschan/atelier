"""task_profile.py のテスト (taxonomy の段階・profile の測った値・埋め込み・backfill・--run)。"""
import contextlib
import io
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import events
import task_profile

NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

PBI_TEXT = '''# PBI-1: sample task

- Model: test

## Goal
Add a small cli command.

## Targets
- `src/a.py`

## Acceptance criteria
1. The command works.
2. Tests pass.

## Verify commands
1. `python -m unittest`

## Refs
- none
'''


class FakeEmbedding:
    """POST /api/embed に、受けた input の数だけ決まったベクトルを返す偽のサーバー。"""

    def __init__(self, dim=4, delay=0.0):
        self.dim = dim
        self.delay = delay
        self.inputs = []
        self.paths = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, fmt, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get('Content-Length') or 0)
                document = json.loads(self.rfile.read(length).decode('utf-8'))
                outer.paths.append(self.path)
                outer.inputs.append(list(document.get('input') or []))
                if outer.delay:
                    time.sleep(outer.delay)
                vectors = [outer.vector(text) for text in (document.get('input') or [])]
                body = json.dumps({'embeddings': vectors}).encode('utf-8')
                try:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass

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


def l2(vector):
    return math.sqrt(sum(value * value for value in vector))


def make_temp():
    """一時フォルダ。tempfile.mkdtemp は使わない。

    DSH の Windows サンドボックス (windows-acl) では、os.mkdir(path, 0o700) が
    DACL の空のフォルダを作り、作成者も中に書けなくなる (Python 3.13 の 0o700 の
    扱いとサンドボックスの制限トークンの組み合わせ。CPython #157876 / DSH #4290)。
    tempfile.mkdtemp は 0o700 を渡すので、既定の mode で作る。
    """
    path = Path(tempfile.gettempdir()) / f'pbi3-task-profile-{os.getpid()}-{uuid.uuid4().hex}'
    os.mkdir(path)
    return path


class TaxonomyTest(unittest.TestCase):
    def test_buckets_are_valid_ids_and_cover_every_count(self):
        tax = task_profile.Taxonomy.load()
        self.assertIsNotNone(events.ID_RE.match(tax.version))
        for name, entries in tax.buckets.items():
            labels = [label for _, _, label in entries]
            with self.subTest(quantity=name):
                self.assertEqual(len(labels), len(set(labels)))
                for label in labels:
                    self.assertIsNotNone(events.ID_RE.match(label), label)
                top = max((hi if hi is not None else lo + 200) for lo, hi, _ in entries)
                for n in range(0, min(top, 400) + 1):
                    hits = [label for lo, hi, label in entries if lo <= n and (hi is None or n <= hi)]
                    self.assertEqual(len(hits), 1, f'{name}: {n} {hits}')
        # 0 件の段がある (plan の確定時は差分が 0)
        self.assertEqual(tax.bucket('files', 0), '0')
        self.assertEqual(tax.bucket('files', 3), '2-3')

    def test_vocabulary_languages_frameworks_and_anchors(self):
        tax = task_profile.Taxonomy.load()
        schema = json.loads((HERE / 'harness.schema.json').read_text(encoding='utf-8'))
        fields = schema['types']['task.profiled']['fields']
        for key in ('task_type', 'domain', 'difficulty_hint'):
            self.assertEqual(list(tax.vocab[key]), fields[key]['values'])
        for extension, language in tax.languages.items():
            self.assertTrue(extension.startswith('.'), extension)
            self.assertIsNotNone(events.ID_RE.match(language), language)
        for dependency, framework in tax.frameworks.items():
            self.assertEqual(dependency, dependency.lower())
            self.assertIsNotNone(events.ID_RE.match(framework), framework)
        self.assertGreaterEqual(len(tax.anchors), 12)
        ids = [anchor['anchor_id'] for anchor in tax.anchors]
        self.assertEqual(len(ids), len(set(ids)))
        for anchor_id in ids:
            self.assertIsNotNone(events.ID_RE.match(anchor_id), anchor_id)
        self.assertGreaterEqual(len({anchor['task_type'] for anchor in tax.anchors}), 5)
        self.assertGreaterEqual(len({anchor['domain'] for anchor in tax.anchors}), 5)


class ProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp()
        self.cwd = os.getcwd()
        self._env = dict(os.environ)
        sender = self.tmp / 'no-sender'
        sender.mkdir()
        os.environ['HARNESS_LOG_PATH'] = str(sender)
        os.environ['HARNESS_LOG_CONFIG'] = str(self.tmp / 'no-config.json')
        os.environ['PBI_RUNS_ARCHIVE'] = str(self.tmp / 'archive')
        os.environ['HOME'] = str(self.tmp / 'home')
        os.environ['USERPROFILE'] = str(self.tmp / 'home')
        (self.tmp / 'home').mkdir()
        self.tax = task_profile.Taxonomy.load()

    def tearDown(self):
        os.chdir(self.cwd)
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers ----

    def git(self, cwd, *args):
        done = subprocess.run(['git', '-C', str(cwd), *args], capture_output=True, text=True,
                              encoding='utf-8', errors='replace', creationflags=NO_WINDOW)
        if done.returncode != 0:
            self.fail(f'git {args}: {done.stderr}')
        return done.stdout

    def make_repo(self):
        root = self.tmp / 'repo'
        root.mkdir()
        self.git(root, 'init', '-q')
        self.git(root, 'config', 'user.email', 't@example.com')
        self.git(root, 'config', 'user.name', 't')
        self.git(root, 'config', 'commit.gpgsign', 'false')
        base_dir = root / 'docs' / 'pbi'
        base_dir.mkdir(parents=True)
        (base_dir / 'PBI-1.md').write_text(PBI_TEXT, encoding='utf-8')
        (base_dir / 'PBI-1.brief.md').write_text('brief\n', encoding='utf-8')
        (base_dir / 'PBI-2.md').write_text(PBI_TEXT.replace('PBI-1', 'PBI-2'), encoding='utf-8')
        (base_dir / 'waves.json').write_text(json.dumps(
            {'waves': [{'n': 1, 'pbis': ['PBI-1']}, {'n': 2, 'pbis': ['PBI-2']}]}), encoding='utf-8')
        (root / 'src').mkdir()
        (root / 'src' / 'a.py').write_text('a = 1\n', encoding='utf-8')
        (root / 'lib').mkdir()
        (root / 'lib' / 'b.ts').write_text('export const b = 1;\n', encoding='utf-8')
        (root / 'tests').mkdir()
        (root / 'tests' / 'test_a.py').write_text('def test_a(): pass\n', encoding='utf-8')
        self.git(root, 'add', '-A')
        self.git(root, 'commit', '-qm', 'base')
        base = self.git(root, 'rev-parse', 'HEAD').strip()
        (root / 'src' / 'a.py').write_text('a = 1\nb = 2\n', encoding='utf-8')
        (root / 'lib' / 'b.ts').write_text('export const b = 1;\nexport const c = 2;\n', encoding='utf-8')
        (root / 'tests' / 'test_a.py').write_text('def test_a(): pass\ndef test_b(): pass\n', encoding='utf-8')
        return root, base, base_dir

    def write_config(self, document):
        path = Path(os.environ['HARNESS_LOG_CONFIG'])
        path.write_text(json.dumps(document), encoding='utf-8')
        return path

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = task_profile.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def rows(self, base_dir):
        path = Path(base_dir) / 'events.jsonl'
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]

    def vectors(self):
        path = Path(os.environ['PBI_RUNS_ARCHIVE']) / 'repo' / 'no-work-key' / 'vectors.jsonl'
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]

    def profile(self, base, *extra):
        return self.run_main('profile', '--dir', str(base), '--pbi', 'PBI-1',
                             '--task-type', 'feature', '--domain', 'cli', '--difficulty', 'mid', *extra)

    # ---- profile ----

    def test_profile_emits_measured_buckets(self):
        root, base, base_dir = self.make_repo()
        code, out, err = self.profile(base_dir, '--base', base)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, '')
        rows = self.rows(base_dir)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['type'], 'task.profiled')
        self.assertEqual(row['pbi'], 'PBI-1')
        self.assertNotIn('dropped', row)
        data = row['data']
        self.assertEqual(data['scope'], 'pbi')
        self.assertEqual(data['taxonomy_version'], self.tax.version)
        self.assertEqual(data['task_type'], 'feature')
        self.assertEqual(data['domain'], 'cli')
        self.assertEqual(data['difficulty_hint'], 'mid')
        # PBI-1 の Targets は src/a.py だけ。作業ツリー全体の 3 ファイルではない
        self.assertEqual(data['size']['files'], '1')
        self.assertEqual(data['size']['files'], self.tax.bucket('files', 1))
        # 1 行の追加 (足し 1 + 引き 0)
        self.assertEqual(data['size']['lines'], self.tax.bucket('lines', 1))
        self.assertEqual(data['size']['lines'], '1-20')
        self.assertEqual(data['size']['pbis'], self.tax.bucket('pbis', 2))
        self.assertEqual(data['size']['pbis'], '2-3')
        self.assertEqual(data['size']['waves'], self.tax.bucket('waves', 2))
        pbi_chars = len((base_dir / 'PBI-1.md').read_text(encoding='utf-8'))
        self.assertEqual(data['size']['input_chars'], self.tax.bucket('input_chars', pbi_chars))
        self.assertEqual(data['languages'], ['python'])
        self.assertEqual(data['spread']['languages'], self.tax.bucket('languages', 1))
        self.assertIs(data['spread']['tests_touched'], False)
        self.assertEqual(data['spread']['dirs'], self.tax.bucket('dirs', 1))
        self.assertIsNone(data['anchors'])
        self.assertEqual(data['case_id'], 'repo::PBI-1')
        self.assertNotIn('vector', json.dumps(row))

    def test_profile_keeps_derived_pbi_files_out_of_the_count(self):
        root, base, base_dir = self.make_repo()
        (base_dir / 'PBI-1.rework-1.md').write_text('rework\n', encoding='utf-8')
        code, _, err = self.profile(base_dir, '--base', base)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rows(base_dir)[0]['data']['size']['pbis'], self.tax.bucket('pbis', 2))

    def test_target_paths_reads_files_after_a_japanese_label(self):
        text = ('## Targets\n'
                '- 新規 `skills/atelier/a.py`: 説明\n'
                '- `docs/guide.md`: 説明\n'
                '- `episode.ended` を出す\n')
        self.assertEqual(task_profile.target_paths(text, self.tax.languages),
                         ['docs/guide.md', 'skills/atelier/a.py'])

    def test_profile_measures_each_pbi_from_its_own_targets(self):
        root, base, base_dir = self.make_repo()
        text = PBI_TEXT.replace('PBI-1', 'PBI-2')
        text = text.replace('- `src/a.py`', '- `src/a.py`\n- `lib/b.ts`\n'
                                             '- `planned/new_file.py`\n- `planned/other.py`\n'
                                             '- `tests/test_new.py`')
        (base_dir / 'PBI-2.md').write_text(text, encoding='utf-8')
        code, _, err = self.profile(base_dir, '--base', base)
        self.assertEqual(code, 0, err)
        code, _, err = self.run_main('profile', '--dir', str(base_dir), '--pbi', 'PBI-2',
                                     '--task-type', 'feature', '--domain', 'cli',
                                     '--difficulty', 'mid', '--base', base)
        self.assertEqual(code, 0, err)
        first, second = (row['data'] for row in self.rows(base_dir))
        # PBI-1 は src/a.py 1 つ、PBI-2 は 5 つ (未だ作っていない計画の 3 つも数える)
        self.assertEqual(first['size']['files'], '1')
        self.assertEqual(second['size']['files'], self.tax.bucket('files', 5))
        self.assertEqual(second['size']['files'], '4-8')
        self.assertNotEqual(first['size'], second['size'])
        # 行数は base からの足し+引き (src/a.py と lib/b.ts が 1 追加ずつ = 2)。
        # 計画のファイルは 0 行
        self.assertEqual(first['size']['lines'], self.tax.bucket('lines', 1))
        self.assertEqual(second['size']['lines'], self.tax.bucket('lines', 2))
        self.assertEqual(first['languages'], ['python'])
        self.assertEqual(second['languages'], ['python', 'typescript'])
        self.assertIs(first['spread']['tests_touched'], False)
        self.assertIs(second['spread']['tests_touched'], True)
        self.assertEqual(second['spread']['dirs'], self.tax.bucket('dirs', 4))

    def test_pbi_size_lines_is_the_churn_of_the_targets(self):
        root = self.tmp / 'churn-repo'
        root.mkdir()
        self.git(root, 'init', '-q')
        self.git(root, 'config', 'user.email', 't@example.com')
        self.git(root, 'config', 'user.name', 't')
        self.git(root, 'config', 'commit.gpgsign', 'false')
        (root / 'big.py').write_text('line = 1\n' * 2000, encoding='utf-8')
        self.git(root, 'add', '-A')
        self.git(root, 'commit', '-qm', 'base')
        base = self.git(root, 'rev-parse', 'HEAD').strip()
        changed = ['line = 1\n'] * 2000
        changed[-1] = 'line = 2\n'
        (root / 'big.py').write_text(''.join(changed), encoding='utf-8')
        base_dir = root / 'docs' / 'pbi'
        base_dir.mkdir(parents=True)
        text = PBI_TEXT.replace('- `src/a.py`', '- `big.py`')
        (base_dir / 'PBI-1.md').write_text(text, encoding='utf-8')
        target = task_profile.pbi_target(base_dir, 'PBI-1', self.tax.languages)
        measured = task_profile.measure(base_dir, target, base, self.tax)
        # 2000 行のファイルの 1 行の変更は 1 追加 + 1 削除。今の行数 (2000) ではない
        self.assertEqual(measured['files'], 1)
        self.assertEqual(measured['lines'], 2)
        self.assertEqual(self.tax.bucket('lines', measured['lines']), self.tax.bucket('lines', 2))
        self.assertNotEqual(self.tax.bucket('lines', measured['lines']),
                            self.tax.bucket('lines', 2000))
        code, _, err = self.profile(base_dir, '--base', base)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rows(base_dir)[0]['data']['size']['lines'],
                         self.tax.bucket('lines', 2))

    def test_pbi_size_lines_counts_untracked_targets_by_their_lines(self):
        root = self.tmp / 'untracked-target-repo'
        root.mkdir()
        self.git(root, 'init', '-q')
        self.git(root, 'config', 'user.email', 't@example.com')
        self.git(root, 'config', 'user.name', 't')
        self.git(root, 'config', 'commit.gpgsign', 'false')
        base_dir = root / 'docs' / 'pbi'
        base_dir.mkdir(parents=True)
        text = PBI_TEXT.replace('- `src/a.py`', '- `new_module.py`\n- `planned/missing.py`')
        (base_dir / 'PBI-1.md').write_text(text, encoding='utf-8')
        self.git(root, 'add', '-A')
        self.git(root, 'commit', '-qm', 'base')
        base = self.git(root, 'rev-parse', 'HEAD').strip()
        (root / 'new_module.py').write_text('line = 1\n' * 40, encoding='utf-8')
        target = task_profile.pbi_target(base_dir, 'PBI-1', self.tax.languages)
        measured = task_profile.measure(base_dir, target, base, self.tax)
        # 未追跡のファイルは全体の 40 行、まだ無い計画のファイルは 0 行 (ファイル数には数える)
        self.assertEqual(measured['files'], 2)
        self.assertEqual(measured['lines'], 40)

    def test_work_key_reads_status_md_first_then_the_last_run(self):
        base_dir = self.tmp / 'keys' / 'docs' / 'pbi'
        base_dir.mkdir(parents=True)
        (base_dir / 'runs.jsonl').write_text(
            json.dumps({'work_key': 'r:old:W-1'}) + '\n'
            + json.dumps({'work_key': 'r:last:W-3'}) + '\n', encoding='utf-8')
        (base_dir / 'STATUS.md').write_text('Work key: r:new:W-2\n', encoding='utf-8')
        self.assertEqual(task_profile.work_key(base_dir), 'r:new:W-2')
        (base_dir / 'STATUS.md').unlink()
        # STATUS.md が無ければ runs.jsonl の最後の行 (最初の行ではない)
        self.assertEqual(task_profile.work_key(base_dir), 'r:last:W-3')

    def test_default_base_and_repo_name_use_the_current_work_rows(self):
        root = self.tmp / 'current-work'
        root.mkdir()
        self.git(root, 'init', '-q')
        base_dir = root / 'docs' / 'pbi'
        base_dir.mkdir(parents=True)
        (base_dir / 'runs.jsonl').write_text(
            json.dumps({'work_key': 'r:old:W-1', 'base_commit': 'aaaa111', 'repo': 'oldname'}) + '\n'
            + json.dumps({'work_key': 'r:new:W-2', 'base_commit': 'bbbb222', 'repo': 'r'}) + '\n',
            encoding='utf-8')
        (base_dir / 'STATUS.md').write_text('Work key: r:new:W-2\n', encoding='utf-8')
        # 前の作業の行 (最初の行) ではなく、今の作業キーの行
        self.assertEqual(task_profile.default_base(base_dir), 'bbbb222')
        self.assertEqual(task_profile.repo_name(base_dir), 'r')
        # 同じ作業キーの行が増えても、最初の行の値
        (base_dir / 'runs.jsonl').write_text(
            (base_dir / 'runs.jsonl').read_text(encoding='utf-8')
            + json.dumps({'work_key': 'r:new:W-2', 'base_commit': 'cccc333', 'repo': 'other'}) + '\n',
            encoding='utf-8')
        self.assertEqual(task_profile.default_base(base_dir), 'bbbb222')
        self.assertEqual(task_profile.repo_name(base_dir), 'r')
        # STATUS.md が無ければ runs.jsonl の最後の作業キー (r:new:W-2) の行
        (base_dir / 'STATUS.md').unlink()
        self.assertEqual(task_profile.default_base(base_dir), 'bbbb222')
        self.assertEqual(task_profile.repo_name(base_dir), 'r')
        # 作業キーが無ければどの行も合わず、HEAD とリポジトリのフォルダ名に落ちる
        (base_dir / 'runs.jsonl').write_text('', encoding='utf-8')
        self.assertEqual(task_profile.default_base(base_dir), 'HEAD')
        self.assertEqual(task_profile.repo_name(base_dir), 'current-work')

    def test_run_profile_keeps_the_whole_diff(self):
        root, base, base_dir = self.make_repo()
        conductor = base_dir / 'conductor'
        conductor.mkdir()
        (conductor / 'goal.md').write_text('Ship the whole run.\n', encoding='utf-8')
        (conductor / 'run.json').write_text(json.dumps(
            {'run_id': 'r1', 'seq': 2, 'goal': 'docs/pbi/conductor/goal.md'}), encoding='utf-8')
        os.chdir(root)
        code, _, err = self.run_main('profile', '--dir', str(base_dir), '--run', '--task-type',
                                     'feature', '--domain', 'skill', '--difficulty', 'high',
                                     '--base', base)
        self.assertEqual(code, 0, err)
        data = self.rows(base_dir)[0]['data']
        changed = task_profile.changed_files(root, base)
        self.assertEqual(data['size']['files'], self.tax.bucket('files', len(changed)))
        self.assertEqual(data['size']['files'], '2-3')
        self.assertEqual(data['size']['lines'], self.tax.bucket('lines', sum(n for _, n in changed)))
        self.assertEqual(data['languages'], ['python', 'typescript'])
        self.assertIs(data['spread']['tests_touched'], True)

    def test_out_of_vocabulary_tags_exit_two_and_write_nothing(self):
        root, base, base_dir = self.make_repo()
        cases = [
            ('poem', 'cli', 'mid'),
            ('feature', 'poem', 'mid'),
            ('feature', 'cli', 'extreme'),
        ]
        for task_type, domain, difficulty in cases:
            with self.subTest(task_type=task_type, domain=domain, difficulty=difficulty):
                code, out, err = self.run_main('profile', '--dir', str(base_dir), '--pbi', 'PBI-1',
                                               '--task-type', task_type, '--domain', domain,
                                               '--difficulty', difficulty, '--base', base)
                self.assertEqual(code, 2)
                self.assertEqual(out, '')
                self.assertTrue(err.strip())
                self.assertFalse((base_dir / 'events.jsonl').exists())
                self.assertEqual(self.vectors(), [])

    def test_pbi_and_run_are_exclusive(self):
        root, base, base_dir = self.make_repo()
        code, out, err = self.run_main('profile', '--dir', str(base_dir), '--pbi', 'PBI-1', '--run',
                                       '--task-type', 'feature', '--domain', 'cli', '--difficulty', 'mid')
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertTrue(err.strip())

    def test_changed_files_counts_untracked_files_and_skips_records(self):
        root = self.tmp / 'untracked-repo'
        root.mkdir()
        self.git(root, 'init', '-q')
        self.git(root, 'config', 'user.email', 't@example.com')
        self.git(root, 'config', 'user.name', 't')
        self.git(root, 'config', 'commit.gpgsign', 'false')
        (root / 'base.py').write_text('x = 1\n', encoding='utf-8')
        self.git(root, 'add', '-A')
        self.git(root, 'commit', '-qm', 'base')
        (root / 'new_module.py').write_text('line = 1\n' * 50, encoding='utf-8')
        (root / 'docs' / 'pbi').mkdir(parents=True)
        (root / 'docs' / 'pbi' / 'events.jsonl').write_text('{}\n', encoding='utf-8')
        self.assertEqual(task_profile.changed_files(root, 'HEAD'), [('new_module.py', 50)])

    def test_fake_embedding_server_gives_anchors_and_writes_vectors(self):
        root, base, base_dir = self.make_repo()
        server = FakeEmbedding(dim=6)
        self.addCleanup(server.close)
        self.write_config({'embedding': {'url': server.url, 'model': 'fake-embed', 'normalize': True}})
        code, _, err = self.profile(base_dir, '--base', base)
        self.assertEqual(code, 0, err)
        row = self.rows(base_dir)[0]
        data = row['data']
        self.assertNotIn('dropped', row)
        self.assertEqual(len(data['anchors']), 3)
        anchor_ids = {anchor['anchor_id'] for anchor in self.tax.anchors}
        for anchor in data['anchors']:
            self.assertIn(anchor['anchor_id'], anchor_ids)
            self.assertIsInstance(anchor['score'], float)
            self.assertEqual(anchor['score'], round(anchor['score'], 2))
        scores = [anchor['score'] for anchor in data['anchors']]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertNotIn('vector', data)
        self.assertNotIn('vector', (base_dir / 'events.jsonl').read_text(encoding='utf-8'))
        self.assertTrue(server.paths and all(path == '/api/embed' for path in server.paths))
        # アンカーは 1 回目の POST に本文と一緒に載る
        self.assertEqual(server.inputs[0][0], 'Add a small cli command.\nThe command works.\nTests pass.')
        self.assertEqual(len(server.inputs[0]), 1 + len(self.tax.anchors))
        cache = list((Path(os.environ['PBI_RUNS_ARCHIVE']) / '_anchors').glob('*.json'))
        self.assertEqual(len(cache), 1)
        vectors = self.vectors()
        self.assertEqual(len(vectors), 1)
        self.assertEqual(vectors[0]['case_id'], data['case_id'])
        self.assertEqual(vectors[0]['model'], 'fake-embed')
        self.assertEqual(vectors[0]['dim'], 6)
        self.assertEqual(len(vectors[0]['vector']), 6)
        # 2 回目はキャッシュを使い、ベクトルの行は増えない
        code, _, err = self.profile(base_dir, '--base', base)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(server.inputs[1]), 1)
        self.assertEqual(len(self.vectors()), 1)
        self.assertEqual(len(self.rows(base_dir)), 2)

    def test_unreachable_embedding_server_leaves_anchors_null(self):
        root, base, base_dir = self.make_repo()
        self.write_config({'embedding': {'url': f'http://127.0.0.1:{free_port()}', 'model': 'fake',
                                         'timeout': 3}})
        code, _, err = self.profile(base_dir, '--base', base)
        self.assertEqual(code, 0, err)
        self.assertIsNone(self.rows(base_dir)[0]['data']['anchors'])
        self.assertEqual(self.vectors(), [])

    def test_no_embedding_config_does_not_call_anything(self):
        root, base, base_dir = self.make_repo()
        self.write_config({'enabled': False})
        code, _, err = self.profile(base_dir, '--base', base)
        self.assertEqual(code, 0, err)
        self.assertIsNone(self.rows(base_dir)[0]['data']['anchors'])
        self.assertEqual(self.vectors(), [])

    def test_run_scope_uses_the_goal_file(self):
        root, base, base_dir = self.make_repo()
        conductor = base_dir / 'conductor'
        conductor.mkdir()
        goal_text = 'Ship the whole run.\n'
        (conductor / 'goal.md').write_text(goal_text, encoding='utf-8')
        (conductor / 'run.json').write_text(json.dumps(
            {'run_id': 'r1', 'seq': 2, 'goal': 'docs/pbi/conductor/goal.md'}), encoding='utf-8')
        os.chdir(root)
        code, _, err = self.run_main('profile', '--dir', str(base_dir), '--run', '--task-type', 'feature',
                                     '--domain', 'skill', '--difficulty', 'high', '--base', base)
        self.assertEqual(code, 0, err)
        row = self.rows(base_dir)[0]
        self.assertIsNone(row['pbi'])
        self.assertEqual(row['data']['scope'], 'run')
        self.assertEqual(row['data']['size']['input_chars'], self.tax.bucket('input_chars', len(goal_text)))
        self.assertIsNone(row['data']['context']['refs'])
        self.assertTrue(row['data']['case_id'].endswith(':run'))

    def test_events_py_delegates_profile(self):
        root, base, base_dir = self.make_repo()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = events.main(['profile', '--dir', str(base_dir), '--pbi', 'PBI-1', '--task-type', 'feature',
                                '--domain', 'cli', '--difficulty', 'mid', '--base', base])
        self.assertEqual(code, 0, err.getvalue())
        rows = self.rows(base_dir)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['type'], 'task.profiled')

    # ---- backfill ----

    def test_backfill_embeds_null_rows_from_cases_and_does_not_repeat(self):
        root, base, base_dir = self.make_repo()
        code, _, err = self.profile(base_dir, '--base', base)
        self.assertEqual(code, 0, err)
        first = self.rows(base_dir)[0]
        self.assertIsNone(first['data']['anchors'])
        case = {'schema': 'atelier.case/1', 'id': first['data']['case_id'], 'pbi': 'PBI-1',
                'task': {'goal': 'Add a small cli command.',
                         'acceptance': [{'n': 1, 'text': 'The command works.'}]}}
        (base_dir / 'cases.jsonl').write_text(json.dumps(case) + '\n', encoding='utf-8')
        server = FakeEmbedding(dim=5)
        self.addCleanup(server.close)
        self.write_config({'embedding': {'url': server.url, 'model': 'fake-embed'}})
        code, out, err = self.run_main('backfill', '--dir', str(base_dir))
        self.assertEqual(code, 0, err)
        rows = self.rows(base_dir)
        self.assertEqual(len(rows), 2)
        latest = rows[-1]
        self.assertEqual(latest['type'], 'task.profiled')
        self.assertEqual(latest['data']['case_id'], first['data']['case_id'])
        self.assertEqual(len(latest['data']['anchors']), 3)
        self.assertEqual(server.inputs[0][0], 'Add a small cli command.\nThe command works.')
        self.assertEqual(len(self.vectors()), 1)
        # 2 回目は出し直さない
        code, out, err = self.run_main('backfill', '--dir', str(base_dir))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.rows(base_dir)), 2)
        self.assertEqual(len(self.vectors()), 1)

    def test_backfill_without_a_case_skips_and_stays_at_zero(self):
        root, base, base_dir = self.make_repo()
        code, _, err = self.profile(base_dir, '--base', base)
        self.assertEqual(code, 0, err)
        server = FakeEmbedding(dim=4)
        self.addCleanup(server.close)
        self.write_config({'embedding': {'url': server.url, 'model': 'fake-embed'}})
        code, out, err = self.run_main('backfill', '--dir', str(base_dir))
        self.assertEqual(code, 0)
        self.assertEqual(len(self.rows(base_dir)), 1)
        self.assertIn('no case', err)

    def test_backfill_measures_the_pbi_size_again_at_finish(self):
        root = self.tmp / 'size-backfill'
        root.mkdir()
        self.git(root, 'init', '-q')
        self.git(root, 'config', 'user.email', 't@example.com')
        self.git(root, 'config', 'user.name', 't')
        self.git(root, 'config', 'commit.gpgsign', 'false')
        (root / 'big.py').write_text('line = 1\n' * 2000, encoding='utf-8')
        self.git(root, 'add', '-A')
        self.git(root, 'commit', '-qm', 'base')
        base = self.git(root, 'rev-parse', 'HEAD').strip()
        base_dir = root / 'docs' / 'pbi'
        base_dir.mkdir(parents=True)
        (base_dir / 'PBI-1.md').write_text(
            PBI_TEXT.replace('- `src/a.py`', '- `big.py`\n- `new_mod.py`'), encoding='utf-8')
        # plan の確定時は、まだ何も作っていないので行数は 0
        code, _, err = self.profile(base_dir, '--base', base)
        self.assertEqual(code, 0, err)
        first = self.rows(base_dir)[0]
        self.assertEqual(first['data']['size']['lines'], '0')
        # finish: big.py の 10 行を変え (10 追加 + 10 削除)、new_mod.py を 5 行作る
        changed = ['line = 1\n'] * 2000
        for index in range(10):
            changed[index] = 'line = 2\n'
        (root / 'big.py').write_text(''.join(changed), encoding='utf-8')
        (root / 'new_mod.py').write_text('line = 1\n' * 5, encoding='utf-8')
        # 埋め込みの設定が無くても測り直す
        code, out, err = self.run_main('backfill', '--dir', str(base_dir))
        self.assertEqual(code, 0, err)
        rows = self.rows(base_dir)
        self.assertEqual(len(rows), 2)
        latest = rows[-1]
        self.assertEqual(latest['type'], 'task.profiled')
        self.assertEqual(latest['pbi'], 'PBI-1')
        self.assertEqual(latest['data']['case_id'], first['data']['case_id'])
        self.assertEqual(latest['data']['task_type'], 'feature')
        self.assertEqual(latest['data']['size']['files'], self.tax.bucket('files', 2))
        self.assertEqual(latest['data']['size']['lines'], self.tax.bucket('lines', 25))
        self.assertEqual(latest['data']['size']['lines'], '21-100')
        self.assertIsNone(latest['data']['anchors'])
        # 2 回目は出し直さない
        code, out, err = self.run_main('backfill', '--dir', str(base_dir))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.rows(base_dir)), 2)

    def test_backfill_does_not_remeasure_an_earlier_works_pbi(self):
        # rework-4 repro: W-2 の backfill が W-1 の PBI-4 を W-2 の base で測り、
        # W-1 の case_id のまま size 0 を出し直して W-1 の本当の大きさを壊していた。
        root = self.tmp / 'old-work'
        root.mkdir()
        self.git(root, 'init', '-q')
        self.git(root, 'config', 'user.email', 't@example.com')
        self.git(root, 'config', 'user.name', 't')
        self.git(root, 'config', 'commit.gpgsign', 'false')
        (root / '.gitignore').write_text('docs/\n', encoding='utf-8')
        (root / 'big.py').write_text('x = 1\n' * 10, encoding='utf-8')
        self.git(root, 'add', '-A')
        self.git(root, 'commit', '-qm', 'c0')
        c0 = self.git(root, 'rev-parse', 'HEAD').strip()
        base_dir = root / 'docs' / 'pbi'
        base_dir.mkdir(parents=True)
        # W-1: PBI-4 が big.py を 50 行増やす。PBI-4.md は W-2 でも残る。
        (base_dir / 'STATUS.md').write_text('Work key: r:main:W-1\n', encoding='utf-8')
        (base_dir / 'PBI-4.md').write_text(
            PBI_TEXT.replace('PBI-1', 'PBI-4').replace('src/a.py', 'big.py'), encoding='utf-8')
        (root / 'big.py').write_text('x = 1\n' * 10 + 'y = 2\n' * 50, encoding='utf-8')
        (base_dir / 'runs.jsonl').write_text(json.dumps(
            {'repo': 'r', 'work_key': 'r:main:W-1', 'pbi': 'PBI-4', 'base_commit': c0}) + '\n', encoding='utf-8')
        code, _, err = self.run_main('profile', '--dir', str(base_dir), '--pbi', 'PBI-4',
                                     '--task-type', 'feature', '--domain', 'cli', '--difficulty', 'low')
        self.assertEqual(code, 0, err)
        code, _, err = self.run_main('backfill', '--dir', str(base_dir))
        self.assertEqual(code, 0, err)
        w1_rows = [row for row in self.rows(base_dir)
                   if (row.get('data') or {}).get('case_id') == 'r:r:main:W-1:PBI-4']
        self.assertEqual(len(w1_rows), 1)
        self.assertEqual(w1_rows[-1]['data']['size']['lines'], '21-100')
        self.git(root, 'add', 'big.py')
        self.git(root, 'commit', '-qm', 'w1')
        c1 = self.git(root, 'rev-parse', 'HEAD').strip()
        # W-2: PBI-1 が small.py を作る。
        (base_dir / 'STATUS.md').write_text('Work key: r:main:W-2\n', encoding='utf-8')
        (base_dir / 'PBI-1.md').write_text(PBI_TEXT.replace('src/a.py', 'small.py'), encoding='utf-8')
        (root / 'small.py').write_text('z = 3\n' * 5, encoding='utf-8')
        with open(base_dir / 'runs.jsonl', 'a', encoding='utf-8', newline='\n') as handle:
            handle.write(json.dumps(
                {'repo': 'r', 'work_key': 'r:main:W-2', 'pbi': 'PBI-1', 'base_commit': c1}) + '\n')
        code, _, err = self.run_main('profile', '--dir', str(base_dir), '--pbi', 'PBI-1',
                                     '--task-type', 'feature', '--domain', 'cli', '--difficulty', 'low')
        self.assertEqual(code, 0, err)
        before = len(self.rows(base_dir))
        code, out, err = self.run_main('backfill', '--dir', str(base_dir))
        self.assertEqual(code, 0, err)
        rows = self.rows(base_dir)
        # W-2 の backfill は W-1 の case_id の行を 1 つも足さない。
        self.assertEqual(len([row for row in rows[before:]
                              if 'W-1' in ((row.get('data') or {}).get('case_id') or '')]), 0)
        w1_rows = [row for row in rows if (row.get('data') or {}).get('case_id') == 'r:r:main:W-1:PBI-4']
        self.assertEqual(len(w1_rows), 1)
        self.assertEqual(w1_rows[-1]['data']['size']['lines'], '21-100')

    def test_backfill_remeasures_only_the_current_works_pbi_with_a_reused_number(self):
        # W-1 と W-2 が同じ PBI-1 を持つ。W-1 の行が W-2 の行より後ろに届いても、
        # W-2 の backfill は W-2 の case_id の行だけを測り直す。
        root = self.tmp / 'reused-pbi'
        root.mkdir()
        self.git(root, 'init', '-q')
        self.git(root, 'config', 'user.email', 't@example.com')
        self.git(root, 'config', 'user.name', 't')
        self.git(root, 'config', 'commit.gpgsign', 'false')
        (root / '.gitignore').write_text('docs/\n', encoding='utf-8')
        (root / 'big.py').write_text('x = 1\n' * 10, encoding='utf-8')
        self.git(root, 'add', '-A')
        self.git(root, 'commit', '-qm', 'c0')
        c0 = self.git(root, 'rev-parse', 'HEAD').strip()
        base_dir = root / 'docs' / 'pbi'
        base_dir.mkdir(parents=True)
        # W-1: PBI-1 を持つ。profile 行は W-2 の profile 行の後ろに遅れて届く。
        (base_dir / 'STATUS.md').write_text('Work key: r:main:W-1\n', encoding='utf-8')
        (root / 'big.py').write_text('x = 1\n' * 10 + 'y = 2\n' * 50, encoding='utf-8')
        (base_dir / 'runs.jsonl').write_text(json.dumps(
            {'repo': 'r', 'work_key': 'r:main:W-1', 'pbi': 'PBI-1', 'base_commit': c0}) + '\n', encoding='utf-8')
        self.git(root, 'add', 'big.py')
        self.git(root, 'commit', '-qm', 'w1')
        c1 = self.git(root, 'rev-parse', 'HEAD').strip()
        # W-2: 同じ PBI-1 が small.py を作る。
        (base_dir / 'STATUS.md').write_text('Work key: r:main:W-2\n', encoding='utf-8')
        (base_dir / 'PBI-1.md').write_text(PBI_TEXT.replace('src/a.py', 'small.py'), encoding='utf-8')
        (root / 'small.py').write_text('z = 3\n' * 5, encoding='utf-8')
        with open(base_dir / 'runs.jsonl', 'a', encoding='utf-8', newline='\n') as handle:
            handle.write(json.dumps(
                {'repo': 'r', 'work_key': 'r:main:W-2', 'pbi': 'PBI-1', 'base_commit': c1}) + '\n')
        code, _, err = self.run_main('profile', '--dir', str(base_dir), '--pbi', 'PBI-1',
                                     '--task-type', 'feature', '--domain', 'cli', '--difficulty', 'low')
        self.assertEqual(code, 0, err)
        # W-1 の profile 行が W-2 の profile 行の後ろに遅れて届いた状態を作る。
        (base_dir / 'STATUS.md').write_text('Work key: r:main:W-1\n', encoding='utf-8')
        code, _, err = self.run_main('profile', '--dir', str(base_dir), '--pbi', 'PBI-1',
                                     '--task-type', 'feature', '--domain', 'cli', '--difficulty', 'low')
        self.assertEqual(code, 0, err)
        (base_dir / 'STATUS.md').write_text('Work key: r:main:W-2\n', encoding='utf-8')
        # finish: small.py が 5 行から 30 行になる (W-2 の PBI-1 の大きさが変わる)。
        (root / 'small.py').write_text('z = 3\n' * 30, encoding='utf-8')
        before = len(self.rows(base_dir))
        code, out, err = self.run_main('backfill', '--dir', str(base_dir))
        self.assertEqual(code, 0, err)
        rows = self.rows(base_dir)
        # 出し直すのは W-2 の PBI-1 の 1 行だけ。
        self.assertEqual(len(rows), before + 1)
        latest = rows[-1]
        self.assertEqual(latest['type'], 'task.profiled')
        self.assertEqual(latest['pbi'], 'PBI-1')
        self.assertEqual(latest['data']['case_id'], 'r:r:main:W-2:PBI-1')
        self.assertEqual(latest['data']['size']['lines'], '21-100')
        # W-1 の行は測り直されない。
        w1_rows = [row for row in rows if (row.get('data') or {}).get('case_id') == 'r:r:main:W-1:PBI-1']
        self.assertEqual(len(w1_rows), 1)
        self.assertEqual(w1_rows[0]['data']['size']['lines'], '1-20')


class EmbedTest(unittest.TestCase):
    def test_prefix_normalize_dims_and_provider(self):
        server = FakeEmbedding(dim=2)
        self.addCleanup(server.close)
        config = {'url': server.url, 'model': 'm', 'provider': 'ollama', 'normalize': True,
                  'prefix': 'q: ', 'timeout': 5, 'dims': 2}
        vectors = task_profile.embed(['a', 'b'], config)
        self.assertIsNotNone(vectors)
        self.assertEqual(len(vectors), 2)
        self.assertEqual(server.inputs[0], ['q: a', 'q: b'])
        for vector in vectors:
            self.assertEqual(round(l2(vector), 6), 1.0)
        # normalize: false はそのまま返す (prefix は付く)
        raw = task_profile.embed(['a'], dict(config, normalize=False, prefix=''))
        self.assertEqual(raw, [server.vector('a')])
        # 次元が合わない・provider が違う・つながらない → None (例外を投げない)
        before = len(server.inputs)
        self.assertIsNone(task_profile.embed(['a'], dict(config, dims=3)))
        self.assertIsNone(task_profile.embed(['a'], dict(config, provider='openai')))
        self.assertEqual(len(server.inputs), before + 1)
        self.assertIsNone(task_profile.embed(['a'], dict(config, url=f'http://127.0.0.1:{free_port()}')))
        self.assertIsNone(task_profile.embed([], config))

    def test_timeout_is_handed_to_the_socket(self):
        server = FakeEmbedding(dim=2, delay=2.0)
        self.addCleanup(server.close)
        start = time.monotonic()
        vectors = task_profile.embed(['a'], {'url': server.url, 'model': 'm', 'timeout': 1})
        elapsed = time.monotonic() - start
        self.assertIsNone(vectors)
        self.assertLess(elapsed, 1.9)


class FindCaseTest(unittest.TestCase):
    """backfill が埋め込み直す本文を cases.jsonl から探すときの選び方。"""

    def test_known_case_id_does_not_fall_back_to_another_works_row(self):
        cases = [{'id': 'repo:wk1:PBI-3', 'pbi': 'PBI-3', 'task': 'W-1 の本文'}]
        self.assertIsNone(task_profile._find_case(cases, 'PBI-3', 'repo:wk2:PBI-3'))

    def test_known_case_id_picks_its_own_row(self):
        cases = [{'id': 'repo:wk2:PBI-3', 'pbi': 'PBI-3'}, {'id': 'repo:wk1:PBI-3', 'pbi': 'PBI-3'}]
        self.assertEqual(task_profile._find_case(cases, 'PBI-3', 'repo:wk2:PBI-3')['id'], 'repo:wk2:PBI-3')

    def test_without_case_id_the_last_row_of_the_pbi_is_used(self):
        cases = [{'id': 'a', 'pbi': 'PBI-3'}, {'id': 'b', 'pbi': 'PBI-3'}, {'id': 'c', 'pbi': 'PBI-4'}]
        self.assertEqual(task_profile._find_case(cases, 'PBI-3', None)['id'], 'b')


if __name__ == '__main__':
    unittest.main()
