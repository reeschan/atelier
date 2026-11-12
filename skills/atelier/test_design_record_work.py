"""design_record.py の work と stale を CLI から動かし、作業票 (docs/pbi/PBI-<n>.work.json) の中身と
起こす前の古さの確かめを、終了コード・標準出力・標準エラー・ファイルの中身で確かめる。

仕様は docs/specs/work-json.md。期待値は仕様と git (git rev-parse HEAD、git hash-object) から取る。
work と stale は作業ツリーの相対パスで読み git を呼ぶので、テストごとに一時の git リポジトリを作って cwd に渡す。
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'design_record.py')

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_MISMATCH = 3
EXIT_DRAFT = 6
EXIT_STALE = 7

ROUTE = {'route': [{'runner': 'agent', 'model': 'sonnet', 'effort': 'high'},
                   {'runner': 'codex', 'model': 'gpt-5', 'effort': None}]}


def _make_writable(func, path, _exc):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def pbi_markdown(n, *, goal='Make the thing work.', targets=('src/a.txt', 'src/b.txt'),
                 do_not_touch=('src/other.txt', 'the public API'), depends_on='none', refs=('mem-1', 'mem-2'),
                 notes=('- plain note', '- slice: c1', '- slice: c2'), sections=None):
    """PBI の Markdown。節を渡した引数が None のものは節ごと省く。"""
    def bullets(items):
        return '\n'.join(f'- {item}' for item in items)
    parts = [f'# PBI-{n}: a test PBI', '', f'- Depends on: {depends_on}', '']
    if goal is not None:
        parts += ['## Goal', goal, '']
    if targets is not None:
        parts += ['## Targets', bullets(targets), '']
    if do_not_touch is not None:
        parts += ['## Do not touch', bullets(do_not_touch), '']
    parts += ['## Acceptance criteria', '1. It works.', '']
    if refs is not None:
        parts += ['## Refs', bullets(refs), '']
    if notes is not None:
        parts += ['## Notes', '\n'.join(notes), '']
    return '\n'.join(parts)


class WorkTicketTest(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix='work-ticket-')
        self.addCleanup(shutil.rmtree, self.repo, onerror=_make_writable)
        self.git('init', '-q')
        self.write_file('src/a.txt', 'alpha\n')
        self.write_file('src/b.txt', 'bravo\n')
        self.write_file('src/other.txt', 'other\n')
        self.git('add', '.')
        self.git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-m', 'base')
        self.no_config = os.path.join(self.repo, 'no-harness-log.json')

    # --- helpers ---

    def git(self, *args):
        done = subprocess.run(['git', *args], cwd=self.repo, capture_output=True, stdin=subprocess.DEVNULL)
        self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', errors='replace'))
        return done.stdout.decode('utf-8').strip()

    def path(self, rel):
        return os.path.join(self.repo, *rel.split('/'))

    def write_file(self, rel, content):
        full = self.path(rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'wb') as f:
            f.write(content.encode('utf-8'))

    def run_cli(self, *args):
        """スクリプトをサブプロセスで動かす。cwd は一時の git リポジトリ。--dir は既定の docs/pbi を使う。"""
        result = subprocess.run([sys.executable, SCRIPT, *args], cwd=self.repo, capture_output=True,
                                stdin=subprocess.DEVNULL,
                                env=dict(os.environ, HARNESS_LOG_CONFIG=self.no_config))
        return subprocess.CompletedProcess(result.args, result.returncode, result.stdout.decode('utf-8'),
                                           result.stderr.decode('utf-8', errors='replace'))

    def write_pbi(self, n, **kwargs):
        self.write_file(f'docs/pbi/PBI-{n}.md', pbi_markdown(n, **kwargs))

    def write_route(self, n, route=ROUTE):
        self.write_file(f'docs/pbi/PBI-{n}.route.json', json.dumps(route))

    def write_slice(self, slice_id):
        self.write_file(f'docs/pbi/notes/{slice_id}.slice.json',
                        json.dumps({'schema': 'atelier.design-slice/1', 'id': slice_id, 'question': 'q?'}))

    def make_pbi(self, n, **kwargs):
        """PBI の Markdown・route.json・Notes が名指す slice を置く。"""
        self.write_pbi(n, **kwargs)
        self.write_route(n)
        for slice_id in ('c1', 'c2'):
            self.write_slice(slice_id)

    def ticket_path(self, n):
        return self.path(f'docs/pbi/PBI-{n}.work.json')

    def read_ticket(self, n):
        with open(self.ticket_path(n), encoding='utf-8') as f:
            return json.load(f)

    def work_ok(self, n):
        done = self.run_cli('work', f'PBI-{n}')
        self.assertEqual(done.returncode, EXIT_OK, done.stderr)
        return done

    def hash_object(self, rel):
        return self.git('hash-object', '--', rel)

    # --- Group A: work ---

    def test_work_writes_the_ticket_from_the_pbi_and_route(self):
        self.make_pbi(3, depends_on='PBI-1, PBI-2')
        done = self.work_ok(3)
        self.assertTrue(done.stdout.strip().endswith('docs/pbi/PBI-3.work.json'), done.stdout)
        ticket = self.read_ticket(3)
        self.assertEqual(ticket['schema'], 'atelier.work/1')
        self.assertEqual(ticket['pbi'], 'PBI-3')
        self.assertEqual(ticket['goal'], 'Make the thing work.')
        self.assertEqual(ticket['targets'], ['src/a.txt', 'src/b.txt'])
        self.assertEqual(ticket['do_not_touch'], ['src/other.txt', 'the public API'])
        self.assertEqual(ticket['depends_on'], ['PBI-1', 'PBI-2'])
        self.assertEqual(ticket['base_sha'], self.git('rev-parse', 'HEAD'))
        self.assertEqual(ticket['target_blobs'],
                         {'src/a.txt': self.hash_object('src/a.txt'), 'src/b.txt': self.hash_object('src/b.txt')})
        self.assertEqual(ticket['route'], ROUTE['route'])
        self.assertEqual(ticket['slices'], ['c1', 'c2'])
        self.assertEqual(ticket['refs'], ['mem-1', 'mem-2'])

    def test_work_records_a_new_target_as_null(self):
        self.make_pbi(1, targets=('src/a.txt', 'src/new.txt'))
        self.work_ok(1)
        blobs = self.read_ticket(1)['target_blobs']
        self.assertEqual(blobs, {'src/a.txt': self.hash_object('src/a.txt'), 'src/new.txt': None})

    def test_work_exits_6_and_writes_nothing_when_a_target_is_missing(self):
        causes = {
            'no Goal': ({'goal': None}, 'Goal'),
            'empty Targets': ({'targets': ()}, 'Targets'),
            'no Do not touch': ({'do_not_touch': None}, 'Do not touch'),
            'Targets none': ({'targets': ('none',)}, 'Targets'),
            'no route.json': ({}, 'route'),
            'slice file missing': ({'notes': ('- slice: c9',)}, 'c9'),
        }
        for name, (kwargs, word) in causes.items():
            with self.subTest(name):
                self.make_pbi(5, **kwargs)
                if name == 'no route.json':
                    os.remove(self.path('docs/pbi/PBI-5.route.json'))
                ticket = self.ticket_path(5)
                if os.path.exists(ticket):
                    os.remove(ticket)
                done = self.run_cli('work', 'PBI-5')
                self.assertEqual(done.returncode, EXIT_DRAFT, done.stderr)
                self.assertIn(word, done.stderr)
                self.assertFalse(os.path.exists(ticket))
                # すでにある作業票は変わらない
                with open(ticket, 'wb') as f:
                    f.write(b'{"keep": "me"}')
                done = self.run_cli('work', 'PBI-5')
                self.assertEqual(done.returncode, EXIT_DRAFT, done.stderr)
                with open(ticket, 'rb') as f:
                    self.assertEqual(f.read(), b'{"keep": "me"}')
                os.remove(ticket)

    def test_work_accepts_none_in_do_not_touch_depends_on_and_refs(self):
        self.make_pbi(4, do_not_touch=('none',), depends_on='none', refs=('none',), notes=())
        self.work_ok(4)
        ticket = self.read_ticket(4)
        self.assertEqual(ticket['do_not_touch'], [])
        self.assertEqual(ticket['depends_on'], [])
        self.assertEqual(ticket['refs'], [])
        for name, kwargs in {'Targets none': {'targets': ('none',)}, 'Goal none': {'goal': 'none'}}.items():
            with self.subTest(name):
                self.make_pbi(6, **kwargs)
                done = self.run_cli('work', 'PBI-6')
                self.assertEqual(done.returncode, EXIT_DRAFT, done.stderr)
                self.assertFalse(os.path.exists(self.ticket_path(6)))

    # --- Group B: stale ---

    def test_stale_is_fresh_when_the_targets_match(self):
        self.make_pbi(1)
        self.work_ok(1)
        done = self.run_cli('stale', 'PBI-1')
        self.assertEqual(done.returncode, EXIT_OK, done.stderr)
        self.assertTrue(done.stdout.startswith('fresh'), done.stdout)

    def test_stale_reports_a_changed_or_removed_target(self):
        self.make_pbi(1)
        self.work_ok(1)
        self.write_file('src/a.txt', 'alpha changed\n')
        done = self.run_cli('stale', 'PBI-1')
        self.assertEqual(done.returncode, EXIT_STALE, done.stderr)
        self.assertIn('stale\tchanged\tsrc/a.txt', done.stdout.splitlines())
        self.assertNotIn('src/b.txt', done.stdout)
        os.remove(self.path('src/b.txt'))
        done = self.run_cli('stale', 'PBI-1')
        self.assertEqual(done.returncode, EXIT_STALE, done.stderr)
        self.assertIn('stale\tremoved\tsrc/b.txt', done.stdout.splitlines())

    def test_stale_ignores_a_change_made_by_a_depends_on_pbi(self):
        self.make_pbi(1, targets=('src/a.txt',))
        self.make_pbi(2, targets=('src/a.txt', 'src/b.txt'), depends_on='PBI-1')
        self.work_ok(1)
        self.work_ok(2)
        self.write_file('src/a.txt', 'alpha changed by PBI-1\n')
        done = self.run_cli('stale', 'PBI-2')
        self.assertEqual(done.returncode, EXIT_OK, done.stderr)
        self.assertTrue(done.stdout.startswith('fresh'), done.stdout)
        # PBI-1 の targets に無いファイルの変化は古さに数える
        self.write_file('src/b.txt', 'bravo changed\n')
        done = self.run_cli('stale', 'PBI-2')
        self.assertEqual(done.returncode, EXIT_STALE, done.stderr)
        self.assertIn('stale\tchanged\tsrc/b.txt', done.stdout.splitlines())
        self.assertNotIn('src/a.txt', done.stdout)

    def test_stale_reports_a_target_that_appeared(self):
        self.make_pbi(1, targets=('src/a.txt', 'src/new.txt'))
        self.work_ok(1)
        self.write_file('src/new.txt', 'someone made me\n')
        done = self.run_cli('stale', 'PBI-1')
        self.assertEqual(done.returncode, EXIT_STALE, done.stderr)
        self.assertIn('stale\tappeared\tsrc/new.txt', done.stdout.splitlines())

    def test_stale_refuses_a_missing_ticket_or_an_outside_path(self):
        done = self.run_cli('stale', 'PBI-1')
        self.assertEqual(done.returncode, EXIT_USAGE)
        self.assertEqual(done.stdout, '')
        # depends_on の作業票が無い
        self.make_pbi(2, depends_on='PBI-1')
        self.work_ok(2)
        done = self.run_cli('stale', 'PBI-2')
        self.assertEqual(done.returncode, EXIT_USAGE)
        self.assertEqual(done.stdout, '')
        # リポジトリの外を指す target_blobs
        self.write_file('docs/pbi/PBI-3.work.json', json.dumps({
            'schema': 'atelier.work/1', 'pbi': 'PBI-3', 'goal': 'g', 'targets': ['src/a.txt'], 'do_not_touch': [],
            'depends_on': [], 'base_sha': self.git('rev-parse', 'HEAD'), 'target_blobs': {'../x': None},
            'route': ROUTE['route'], 'slices': [], 'refs': []}))
        done = self.run_cli('stale', 'PBI-3')
        self.assertEqual(done.returncode, EXIT_USAGE)
        self.assertEqual(done.stdout, '')

    def rewrite_ticket(self, n, **changes):
        ticket = self.read_ticket(n)
        ticket.update(changes)
        self.write_file(f'docs/pbi/PBI-{n}.work.json', json.dumps(ticket))

    def test_stale_refuses_a_ticket_changed_after_the_record(self):
        """承認の正本 (/2) があるとき、記録のあとに書き換わった作業票 (依存の作業票も) では起こさない。"""
        self.make_pbi(1, targets=('src/a.txt',))
        self.make_pbi(2, targets=('src/a.txt', 'src/b.txt'), depends_on='PBI-1')
        self.work_ok(1)
        self.work_ok(2)
        done = self.run_cli('write', '--id', 'work-json', '--target', 'PBI-1', '--target', 'PBI-2',
                            '--work', 'PBI-1.work.json', '--work', 'PBI-2.work.json')
        self.assertEqual(done.returncode, EXIT_OK, done.stderr)
        self.assertEqual(self.run_cli('stale', 'PBI-2').returncode, EXIT_OK)
        self.write_file('src/b.txt', 'bravo changed\n')
        # 自分の作業票の target_blobs を今の中身に合わせて書き換えても、記録と違うので通さない
        self.rewrite_ticket(2, target_blobs={'src/a.txt': self.hash_object('src/a.txt'),
                                             'src/b.txt': self.hash_object('src/b.txt')})
        done = self.run_cli('stale', 'PBI-2')
        self.assertEqual(done.returncode, EXIT_MISMATCH, done.stdout + done.stderr)
        self.assertEqual(done.stdout, '')
        self.assertIn('PBI-2.work.json', done.stderr)
        # 依存の作業票の targets を広げて、自分の変化を隠すのも通さない
        self.work_ok(2)
        self.run_cli('write', '--id', 'work-json', '--target', 'PBI-1', '--target', 'PBI-2',
                     '--work', 'PBI-1.work.json', '--work', 'PBI-2.work.json')
        self.rewrite_ticket(1, targets=['src/a.txt', 'src/b.txt'],
                            target_blobs={'src/a.txt': self.hash_object('src/a.txt'), 'src/b.txt': None})
        done = self.run_cli('stale', 'PBI-2')
        self.assertEqual(done.returncode, EXIT_MISMATCH, done.stdout + done.stderr)
        self.assertIn('PBI-1.work.json', done.stderr)

    def test_stale_refuses_a_ticket_whose_blobs_or_dependencies_do_not_fit(self):
        """target_blobs が targets と違う作業票、自分に依存する作業票は形の誤り (終了コード 2)。"""
        self.make_pbi(1)
        self.work_ok(1)
        self.write_file('src/a.txt', 'alpha changed\n')
        cases = {
            'blobs empty': {'target_blobs': {}},
            'blobs missing a target': {'target_blobs': {'src/b.txt': self.hash_object('src/b.txt')}},
            'depends on itself': {'depends_on': ['PBI-1']},
        }
        original = self.read_ticket(1)
        for name, changes in cases.items():
            with self.subTest(name):
                self.write_file('docs/pbi/PBI-1.work.json', json.dumps(original))
                self.rewrite_ticket(1, **changes)
                done = self.run_cli('stale', 'PBI-1')
                self.assertEqual(done.returncode, EXIT_USAGE, done.stdout + done.stderr)
                self.assertEqual(done.stdout, '')


if __name__ == '__main__':
    unittest.main()
