"""lint_run.py を CLI から、一時の git リポジトリと偽の jevlint (testdata/fake_jevlint.py) で動かして確かめる。

見ること: 送るファイルの一覧 (秘密・sensitive・拡張子の除外)、許可の記録が無いとモデルで送らないこと、
許可は 1 回で使い切ること、dry-run は送らないこと、見直しの「前」を一時的な worktree で判定して後片付けすること、
下調べのメモの中身。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'lint_run.py')
FAKE = os.path.join(HERE, 'testdata', 'fake_jevlint.py')
GIT_ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.com',
           'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.com'}


class LintRunTest(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.join(tempfile.gettempdir(), f'lint-run-{uuid.uuid4().hex}')
        self.repo = os.path.join(self.dir, 'repo')
        self.home = os.path.join(self.dir, 'home')
        os.makedirs(os.path.join(self.repo, 'src'))
        os.makedirs(self.home)
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        with open(os.path.join(self.home, 'agents.json'), 'w', encoding='utf-8') as f:
            json.dump({'tools': {'jevlint': {'path': FAKE}}}, f)
        self.log = os.path.join(self.dir, 'calls.jsonl')
        self.env = dict(os.environ, FAKE_JEVLINT_LOG=self.log, **GIT_ENV)
        self.git('init', '-q', '-b', 'main')
        self.put('src/a.ts', 'score: 90\nfn: 50\n')
        self.put('src/b.ts', 'score: 90\n')
        self.git('add', '.')
        self.git('commit', '-q', '-m', 'base')
        self.git('checkout', '-q', '-b', 'work')
        self.put('src/a.ts', 'score: 80\nfn: 120\nbug: 0.3\n')
        self.put('src/b.ts', 'score: 89\n')
        self.put('src/c.ts', 'score: 70\n')
        self.put('src/secrets.ts', 'token\n')
        self.put('src/notes.txt', 'text\n')
        self.pbi = os.path.join(self.repo, 'docs', 'pbi')
        os.makedirs(self.pbi)

    def put(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.repo, check=True, capture_output=True, text=True,
                              env=dict(os.environ, **GIT_ENV)).stdout

    def run_cli(self, *args):
        return subprocess.run([sys.executable, SCRIPT, *args[:1], self.pbi, *args[1:], *(
            ['--home', self.home] if args[0] in ('survey', 'recheck') else [])],
            cwd=self.repo, capture_output=True, text=True, encoding='utf-8', env=self.env)

    def lines(self, name):
        with open(os.path.join(self.pbi, 'jevlint', name), encoding='utf-8') as f:
            return f.read().splitlines()

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding='utf-8') as f:
            return [json.loads(line) for line in f]

    def model_calls(self):
        return [c for c in self.calls() if c['args'][:1] == ['audit'] and '--ast-only' not in c['args']
                and '--dry-run' not in c['args']]

    def make_recheck_list(self):
        result = self.run_cli('files', '--name', 'recheck', '--base', 'main', '--new',
                              'src/c.ts,src/secrets.ts,src/notes.txt', '--sensitive', 'src/b.ts')
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_files_lists_what_may_be_sent(self):
        result = self.make_recheck_list()
        self.assertIn('files 3 send 2 excluded 3', result.stdout)
        self.assertEqual(self.lines('recheck.files.txt'), ['src/a.ts', 'src/b.ts', 'src/c.ts'])
        self.assertEqual(self.lines('recheck.send.txt'), ['src/a.ts', 'src/c.ts'])
        self.assertEqual(sorted(self.lines('recheck.excluded.txt')),
                         ['src/b.ts\tsensitive (ast only)', 'src/notes.txt\textension', 'src/secrets.ts\tsecret'])

    def test_jevlint_runs_with_its_own_directory_first_on_path(self):
        # セッションの PATH に別の版の Node しか無くても、見つけた jevlint の版の Node で動かすため
        self.make_recheck_list()
        done = self.run_cli('recheck', '--name', 'recheck', '--base', 'main', '--mode', 'ast-only')
        self.assertEqual(done.returncode, 0, done.stderr)
        heads = {os.path.normcase(c['path0']) for c in self.calls()}
        self.assertEqual(heads, {os.path.normcase(os.path.dirname(os.path.abspath(FAKE)))})

    def test_model_needs_an_unused_approval(self):
        self.make_recheck_list()
        refused = self.run_cli('recheck', '--name', 'recheck', '--base', 'main', '--mode', 'model')
        self.assertEqual(refused.returncode, 3, refused.stdout + refused.stderr)
        self.assertEqual(self.model_calls(), [])
        approved = self.run_cli('approve', '--name', 'recheck', '--answer', 'send')
        self.assertEqual(approved.returncode, 0, approved.stderr)
        done = self.run_cli('recheck', '--name', 'recheck', '--base', 'main', '--mode', 'model')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('worse ', done.stdout)
        sent = sorted(f for c in self.model_calls() for f in c['args'] if f.startswith('src/'))
        self.assertEqual(sent, ['src/a.ts', 'src/a.ts', 'src/c.ts'])  # 前の a と、後の a・c。b は送らない
        with open(os.path.join(self.pbi, 'jevlint', 'recheck.md'), encoding='utf-8') as f:
            table = f.read()
        self.assertIn('| src/a.ts | bug-hunt | bug-hunt.swallowed-error 0.9 -> 0.3', table)
        self.assertIn('| src/a.ts | ast | function-lines 120 (new) | rework-candidate |', table)
        again = self.run_cli('recheck', '--name', 'recheck', '--base', 'main', '--mode', 'model')
        self.assertEqual(again.returncode, 3)

    def test_an_ast_only_answer_does_not_allow_a_send(self):
        self.make_recheck_list()
        self.run_cli('approve', '--name', 'recheck', '--answer', 'ast-only')
        self.assertEqual(self.run_cli('recheck', '--name', 'recheck', '--base', 'main', '--mode', 'model').returncode, 3)
        self.assertEqual(self.model_calls(), [])

    def test_a_changed_list_needs_a_new_approval(self):
        self.make_recheck_list()
        self.run_cli('approve', '--name', 'recheck', '--answer', 'send')
        self.run_cli('files', '--name', 'recheck', '--base', 'main', '--new', 'src/c.ts')
        self.assertEqual(self.run_cli('recheck', '--name', 'recheck', '--base', 'main', '--mode', 'model').returncode, 3)

    def test_ast_only_recheck_uses_a_temporary_worktree_and_cleans_it_up(self):
        self.make_recheck_list()
        result = self.run_cli('recheck', '--name', 'recheck', '--base', 'main', '--mode', 'ast-only')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.model_calls(), [])
        cwds = {os.path.normcase(os.path.realpath(c['cwd'])) for c in self.calls()}
        self.assertEqual(len(cwds), 2)
        self.assertIn(os.path.normcase(os.path.realpath(self.repo)), cwds)
        self.assertEqual(len(self.git('worktree', 'list').splitlines()), 1)
        for other in cwds - {os.path.normcase(os.path.realpath(self.repo))}:
            self.assertFalse(os.path.exists(other))
        with open(os.path.join(self.pbi, 'jevlint', 'recheck.before.json'), encoding='utf-8') as f:
            before = json.load(f)
        self.assertEqual(sorted(x['path'] for x in before['files']), ['src/a.ts', 'src/b.ts'])

    def test_dry_run_sends_nothing(self):
        self.make_recheck_list()
        result = self.run_cli('recheck', '--name', 'recheck', '--base', 'main', '--mode', 'dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('dry-run recheck before: 1 files, about 1,000 tokens (about $0.0100), provider typesafe', result.stdout)
        self.assertIn('dry-run recheck after: 2 files', result.stdout)
        self.assertEqual(self.model_calls(), [])
        self.assertFalse(os.path.exists(os.path.join(self.pbi, 'jevlint', 'recheck.after.json')))

    def test_survey_writes_the_note(self):
        self.git('add', 'src/c.ts')
        self.assertEqual(self.run_cli('files', '--name', 'survey', '--paths', 'src').returncode, 0)
        self.run_cli('approve', '--name', 'survey', '--answer', 'send')
        result = self.run_cli('survey', '--name', 'survey', '--mode', 'model')
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(os.path.join(self.pbi, 'notes', 'survey.md'), encoding='utf-8') as f:
            note = f.read()
        self.assertIn('| src/c.ts | 70 |', note)
        self.assertIn('- src/a.ts:1 120 行', note)
        self.assertIn('- src/a.ts `bug-hunt.swallowed-error` 0.3: rethrow or report the error', note)
        self.assertNotIn('contracts-stated', note)

    def test_a_sensitive_directory_without_wildcards_covers_its_files(self):
        self.put('src/auth/login.ts', 'score: 90\n')
        result = self.run_cli('files', '--name', 'recheck', '--base', 'main', '--new', 'src/auth/login.ts',
                              '--sensitive', 'src/auth')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('src/auth/login.ts', self.lines('recheck.send.txt'))
        self.assertIn('src/auth/login.ts\tsensitive (ast only)', self.lines('recheck.excluded.txt'))

    def test_paths_also_take_new_files(self):
        result = self.run_cli('files', '--name', 'recheck-1', '--paths', 'src/a.ts', '--new', 'src/c.ts')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.lines('recheck-1.files.txt'), ['src/a.ts', 'src/c.ts'])

    def test_dry_run_keeps_its_numbers_for_the_report(self):
        self.make_recheck_list()
        self.assertEqual(self.run_cli('recheck', '--name', 'recheck', '--base', 'main', '--mode', 'dry-run').returncode, 0)
        with open(os.path.join(self.pbi, 'jevlint', 'recheck.dry-run.json'), encoding='utf-8') as f:
            totals = json.load(f)
        self.assertEqual((totals['before']['files'], totals['after']['files'], totals['after']['provider']),
                         (1, 2, 'typesafe'))

    def test_files_needs_paths_or_base(self):
        result = self.run_cli('files', '--name', 'x')
        self.assertEqual(result.returncode, 2)


if __name__ == '__main__':
    unittest.main()
