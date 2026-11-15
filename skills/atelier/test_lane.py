"""lane.py (作業を始める前に Atelier か軽い道かを決め、軽い道の開始と終了を記録する) を CLI で確かめる。

一時ディレクトリの git リポジトリ (main に src/a/x.py を commit、作業は work ブランチ) で、lane.py を別のプロセスで動かす。
見るのは出る行 (lane: と reason:)・書いたファイル (light.json・events.jsonl)・終了コードだけ。
home・プラグイン・ハーネスのログは一時ディレクトリに切り離し、main の Claude Code セッションの環境
(CLAUDE_CODE_SESSION_ID あり・ATELIER_EPISODE なし) を既定にする。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'lane.py')

RUN_JSON = 'docs/pbi/conductor/run.json'


class LaneTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, 'repo')
        self.home = os.path.join(self.tmp, 'home')
        os.makedirs(self.repo)
        os.makedirs(os.path.join(self.home, 'components'))
        os.makedirs(os.path.join(self.tmp, 'plugins'))
        self.env = {k: v for k, v in os.environ.items() if k != 'ATELIER_EPISODE'}
        self.env.update(ATELIER_HOME=self.home, PBI_PLUGINS_DIR=os.path.join(self.tmp, 'plugins'),
                        HARNESS_LOG_CONFIG=os.path.join(self.tmp, 'no-harness-log.json'),
                        ATELIER_EVENTS_SETTLE='off', ATELIER_EVENTS_QUIET='1',
                        CLAUDE_CODE_SESSION_ID='s', PYTHONIOENCODING='utf-8')
        self.git('init', '-q', '-b', 'main')
        self.write('README.md', '# app\n')
        self.write('src/a/x.py', ''.join(f'value_{i} = {i}\n' for i in range(10)))
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'base')
        self.git('checkout', '-q', '-b', 'work')

    def git(self, *words):
        subprocess.run(['git', '-c', 'user.email=t@example.com', '-c', 'user.name=t', '-c', 'core.autocrlf=false',
                        *words], cwd=self.repo, check=True, capture_output=True)

    def path(self, rel):
        return os.path.join(self.repo, *rel.split('/'))

    def write(self, rel, text):
        path = self.path(rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)

    def write_json(self, rel, data):
        self.write(rel, json.dumps(data))

    def lane(self, *argv, env=None):
        result = subprocess.run([sys.executable, SCRIPT, *argv], cwd=self.repo, capture_output=True,
                                env=env or self.env)
        return (result.returncode, result.stdout.decode('utf-8', errors='replace'),
                result.stderr.decode('utf-8', errors='replace'))

    def events(self):
        path = self.path('docs/pbi/events.jsonl')
        if not os.path.isfile(path):
            return []
        with open(path, encoding='utf-8') as f:
            return [json.loads(line) for line in f if line.strip()]


class LaneDecideTest(LaneTestBase):
    def decide(self, paths, goal='small refactor', *flags, new=None, env=None):
        self.write('goal.md', goal)
        self.write('paths.txt', ''.join(f'{p}\n' for p in paths))
        argv = ['--goal-file', 'goal.md', '--paths', 'paths.txt', '--pbi-dir', 'docs/pbi']
        if new is not None:
            self.write('new.txt', ''.join(f'{p}\n' for p in new))
            argv += ['--new', 'new.txt']
        rc, out, err = self.lane(*argv, *flags, env=env)
        self.assertEqual(rc, 0, out + err)
        return out.splitlines()

    def test_C1(self):
        lines = self.decide(['src/a/x.py', 'src/a/y.py'])
        self.assertEqual(lines[0], 'lane: light')
        self.assertIn('reason: small (2 files, 1 modules)', lines)

    def test_C2(self):
        self.write_json('docs/pbi/tests-at-end.json', {'test_paths': ['skills/atelier/testdata/**']})
        lines = self.decide(['src/a/x.py', 'skills/atelier/testdata/x.json'])
        self.assertEqual(lines[0], 'lane: light')
        self.assertIn('reason: small (1 files, 1 modules)', lines)

    def test_C3(self):
        lines = self.decide([f'src/a/f{i}.py' for i in range(9)])
        self.assertEqual(lines[0], 'lane: atelier')
        self.assertIn('reason: files 9 > 8', lines)

    def test_C4(self):
        lines = self.decide([f'src/a/f{i}.py' for i in range(8)])
        self.assertEqual(lines[0], 'lane: light')

    def test_C5(self):
        lines = self.decide(['src/a/x.py', 'src/b/y.py', 'src/c/z.py'])
        self.assertEqual(lines[0], 'lane: atelier')
        for reason in ('modules 3 > 2', 'new module src/b', 'new module src/c'):
            self.assertIn(f'reason: {reason}', lines)

    def test_C6(self):
        files = ['db/migrations/001.sql', 'api/routes.py', 'package.json', 'Dockerfile']
        lines = self.decide(files, 'small refactor', new=files)
        self.assertEqual(lines[0], 'lane: atelier')
        for reason in ('new data-model file db/migrations/001.sql', 'new interface file api/routes.py',
                       'new manifest file package.json', 'new deploy file Dockerfile'):
            self.assertEqual(lines.count(f'reason: {reason}'), 1, lines)

    def test_C7(self):
        lines = self.decide(['src/a/x.py'], 'Update Auth flow')
        self.assertEqual(lines[0], 'lane: atelier')
        self.assertIn('reason: sensitive word auth (goal)', lines)
        lines = self.decide(['src/a/auth.py'], 'tidy')
        self.assertEqual(lines[0], 'lane: atelier')
        self.assertIn('reason: sensitive word auth (path src/a/auth.py)', lines)

    def test_C8(self):
        lines = self.decide([f'src/a/f{i}.py' for i in range(9)], 'small refactor', '--light')
        self.assertEqual(lines[0], 'lane: light')
        self.assertIn('reason: override --light', lines)

    def test_C9(self):
        lines = self.decide(['src/a/x.py', 'src/a/y.py'], 'small refactor', '--full')
        self.assertEqual(lines[0], 'lane: atelier')
        self.assertIn('reason: override --full', lines)

    def test_C10(self):
        lines = self.decide([], 'small refactor', '--light')
        self.assertEqual(lines[0], 'lane: atelier')
        self.assertIn('reason: empty path list', lines)

    def test_C11(self):
        self.write_json(RUN_JSON, {'state': 'running'})
        lines = self.decide(['src/a/x.py', 'src/a/y.py'], 'small refactor', '--light')
        self.assertEqual(lines[0], 'lane: atelier')
        self.assertIn(f'reason: open run {RUN_JSON} (state running)', lines)

    def test_C12(self):
        episode_env = {**self.env, 'ATELIER_EPISODE': '1'}
        no_session_env = {k: v for k, v in self.env.items() if k != 'CLAUDE_CODE_SESSION_ID'}
        for name, env in (('ATELIER_EPISODE', episode_env), ('no session id', no_session_env)):
            with self.subTest(env=name):
                lines = self.decide(['src/a/x.py', 'src/a/y.py'], 'small refactor', '--light', env=env)
                self.assertEqual(lines[0], 'lane: atelier')
                self.assertIn('reason: not the main Claude Code session', lines)

    def test_C13(self):
        self.write(RUN_JSON, '{invalid json')
        lines = self.decide(['src/a/x.py', 'src/a/y.py'], 'small refactor', '--light')
        self.assertEqual(lines[0], 'lane: atelier')
        self.assertIn(f'reason: open run {RUN_JSON} (state unreadable)', lines)

    def check_diff(self):
        rc, out, err = self.lane('check', '--diff', '--base', 'HEAD', '--pbi-dir', 'docs/pbi')
        self.assertEqual(rc, 0, out + err)
        return out.splitlines()

    def edit_and_add_files(self):
        self.write('src/a/x.py', ''.join(f'value_{i} = {i + 1}\n' for i in range(10)))
        first = self.check_diff()
        for i in range(8):
            self.write(f'src/a/new{i}.py', 'value = 1\n')
        self.git('add', '-N', '.')
        return first, self.check_diff()

    def test_C14(self):
        first, second = self.edit_and_add_files()
        self.assertEqual(first[0], 'lane: light')
        self.assertIn('reason: small (1 files, 1 modules)', first)
        self.assertEqual(second[0], 'lane: atelier')
        self.assertIn('reason: files 9 > 8', second)

    def tree(self):
        found = {}
        for folder, _, names in os.walk(self.path('docs/pbi')):
            for name in names:
                path = os.path.join(folder, name)
                with open(path, 'rb') as f:
                    found[os.path.relpath(path, self.repo)] = f.read()
        return found

    def test_C15(self):
        self.write_json('docs/pbi/tests-at-end.json', {'test_paths': []})
        before = self.tree()
        self.decide(['src/a/x.py', 'src/a/y.py'])
        self.edit_and_add_files()
        self.assertEqual(self.tree(), before)
        for name in before:
            self.assertNotIn('question', os.path.basename(name))
        self.assertFalse(any('question' in name for name in self.tree()))


class LaneRecordTest(LaneTestBase):
    def set_reviewer(self, model):
        self.write_json_home({'roles': {'reviewer': {'other': {'model': model, 'effort': 'high'}}}})

    def write_json_home(self, data):
        with open(os.path.join(self.home, 'agents.json'), 'w', encoding='utf-8') as f:
            json.dump(data, f)

    def line(self, out, key):
        return next((row for row in out.splitlines() if row.startswith(f'{key}:')), None)

    def record_start(self, builder, *extra):
        rc, out, err = self.lane('record', 'start', '--builder-model', builder, '--pbi-dir', 'docs/pbi', *extra)
        self.assertEqual(rc, 0, out + err)
        return out

    def test_C16(self):
        self.set_reviewer('opus')
        self.write('goal.md', 'small refactor')
        out = self.record_start('claude-opus-5-5', '--goal-file', 'goal.md')
        with open(self.path('docs/pbi/light.json'), encoding='utf-8') as f:
            state = json.load(f)
        self.assertEqual(state['builder_model'], 'claude-opus-5-5')
        for key in ('run_id', 'started_at', 'base'):
            self.assertTrue(state.get(key), key)
        rows = {row['type']: row for row in self.events()}
        self.assertIn('run.started', rows)
        self.assertIn('task.profiled', rows)
        self.assertEqual(rows['run.started']['data'].get('lane'), 'light')
        for row in rows.values():
            self.assertEqual((row['run_id'], row['seq']), (state['run_id'], 0))
        reviewer = self.line(out, 'reviewer')
        self.assertIsNotNone(reviewer, out)
        self.assertNotIn('opus', reviewer)
        self.assertIsNotNone(self.line(out, 'review-skill'), out)

    def test_C17(self):
        self.set_reviewer('sonnet')
        out = self.record_start('claude-sonnet-4-6')
        reviewer = self.line(out, 'reviewer')
        self.assertIsNotNone(reviewer, out)
        self.assertIn('opus', reviewer)
        self.assertNotIn('sonnet', reviewer)

    def test_C18(self):
        out = self.record_start('claude-opus-5-5')
        self.assertIn('warn: no goal file, task.profiled not written', out)
        types = [row['type'] for row in self.events()]
        self.assertIn('run.started', types)
        self.assertNotIn('task.profiled', types)

    def test_C19(self):
        self.set_reviewer('opus')
        self.write('goal.md', 'small refactor')
        self.record_start('claude-opus-5-5', '--goal-file', 'goal.md')
        self.write('src/a/x.py', ''.join(f'value_{i} = {i + 1}\n' for i in range(10)))
        rc, out, err = self.lane('record', 'end', '--result', 'escalated', '--findings', '1,2,3',
                                 '--context-before', '1000', '--context-after', '500', '--pbi-dir', 'docs/pbi')
        self.assertEqual(rc, 0, out + err)
        ended = [row for row in self.events() if row['type'] == 'run.ended']
        self.assertEqual(len(ended), 1)
        row = ended[0]
        data = row['data']
        self.assertEqual(data['result'], 'escalated')
        self.assertIsInstance(data['files'], int)
        self.assertIsInstance(data['lines'], int)
        self.assertEqual(data['context_delta'], -500)
        self.assertEqual(data['findings'], {'critical': 1, 'major': 2, 'minor': 3})
        self.assertNotIn('dropped', row)
        first = out.splitlines()[0]
        self.assertTrue(first.startswith('run.ended: result=escalated'), out)
        self.assertIn('context_delta=-500 findings=1,2,3', first)

    def test_C20(self):
        empty = os.path.join(self.tmp, 'empty-pbi')
        os.makedirs(empty)
        rc, out, err = self.lane('record', 'end', '--result', 'finished', '--pbi-dir', empty)
        self.assertEqual(rc, 2, out + err)
        self.assertIn('missing', err)
        self.assertIn('light.json', err)
        self.assertFalse(os.path.exists(os.path.join(empty, 'events.jsonl')))


if __name__ == '__main__':
    unittest.main()
