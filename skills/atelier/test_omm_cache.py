"""omm_cache.py を CLI から一時の git リポジトリで動かし、フィンガープリント・当たり外れ・.omm/ の移動を確かめる。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'omm_cache.py')
GIT_ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.com',
           'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.com'}


class OmmCacheTest(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.join(tempfile.gettempdir(), f'omm-cache-{uuid.uuid4().hex}')
        self.repo = os.path.join(self.dir, 'my-repo')
        self.cache = os.path.join(self.dir, 'cache')
        os.makedirs(os.path.join(self.repo, 'src'))
        os.makedirs(os.path.join(self.repo, 'lib'))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.git('init', '-q', '-b', 'main')
        for rel in ('src/a.ts', 'lib/b.ts', 'README.md'):
            with open(os.path.join(self.repo, rel), 'w', encoding='utf-8') as f:
                f.write(rel)
        self.git('add', '.')
        self.git('commit', '-q', '-m', 'base')
        self.pbi = os.path.join(self.repo, 'docs', 'pbi')
        os.makedirs(self.pbi)

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.repo, check=True, capture_output=True, text=True,
                              env=dict(os.environ, **GIT_ENV)).stdout.strip()

    def run_cli(self, command, *args):
        extra = [] if command == 'check' else ['--base', 'main', '--cache-dir', self.cache]
        return subprocess.run([sys.executable, SCRIPT, command, self.pbi, *extra, *args], cwd=self.repo,
                              capture_output=True, text=True, encoding='utf-8')

    def scan(self):
        os.makedirs(os.path.join(self.repo, '.omm', 'overall-architecture'))
        with open(os.path.join(self.repo, '.omm', 'overall-architecture', 'description.md'), 'w') as f:
            f.write('layers')

    def test_miss_store_then_hit(self):
        tree = self.git('rev-parse', 'main:src')
        miss = self.run_cli('lookup')
        self.assertEqual(miss.returncode, 0, miss.stderr)
        folder = os.path.join(self.cache, 'my-repo', tree[:12])
        self.assertEqual(miss.stdout.strip(), f'miss {folder}')
        self.scan()
        self.assertEqual(self.run_cli('check').returncode, 1)
        stored = self.run_cli('store', '--perspectives', 'overall-architecture,dependency-map')
        self.assertEqual(stored.returncode, 0, stored.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.repo, '.omm')))
        self.assertEqual(self.run_cli('check').stdout.strip(), 'none')
        with open(os.path.join(folder, 'fingerprint.json'), encoding='utf-8') as f:
            record = json.load(f)
        self.assertEqual((record['src_tree'], record['trees'], record['perspectives']),
                         (tree, {'src': tree}, ['overall-architecture', 'dependency-map']))
        self.assertTrue(os.path.isfile(os.path.join(folder, '.omm', 'overall-architecture', 'description.md')))
        self.assertEqual(self.run_cli('lookup').stdout.strip(), f'hit {folder}')

    def test_a_record_with_only_src_tree_still_hits(self):
        tree = self.git('rev-parse', 'main:src')
        folder = os.path.join(self.cache, 'my-repo', tree[:12])
        os.makedirs(os.path.join(folder, '.omm'))
        with open(os.path.join(folder, 'fingerprint.json'), 'w', encoding='utf-8') as f:
            json.dump({'src_tree': tree}, f)
        self.assertEqual(self.run_cli('lookup').stdout.strip(), f'hit {folder}')

    def test_a_change_outside_the_scan_dirs_keeps_the_fingerprint(self):
        before = self.run_cli('lookup').stdout
        with open(os.path.join(self.repo, 'README.md'), 'w', encoding='utf-8') as f:
            f.write('changed')
        self.git('commit', '-q', '-am', 'docs')
        self.assertEqual(self.run_cli('lookup').stdout, before)

    def test_scan_dirs_from_map_json(self):
        with open(os.path.join(self.pbi, 'map.json'), 'w', encoding='utf-8') as f:
            json.dump({'scan_dirs': ['src', 'lib']}, f)
        self.scan()
        self.assertEqual(self.run_cli('store').returncode, 0)
        line = self.run_cli('lookup').stdout.strip()
        self.assertTrue(line.startswith('hit '), line)
        self.assertNotIn(self.git('rev-parse', 'main:src')[:12], line)

    def test_a_tracked_omm_is_left_alone(self):
        self.scan()
        self.git('add', '.omm')
        self.git('commit', '-q', '-m', 'omm')
        result = self.run_cli('store')
        self.assertEqual(result.returncode, 2)
        self.assertIn('tracked', result.stderr)
        self.assertTrue(os.path.isdir(os.path.join(self.repo, '.omm')))

    def test_export_copies_the_stored_omm_into_the_working_tree(self):
        """export は当たりの .omm/ を作業ツリーへ写し、置き場所にも残す。外れのときは何も写さず 2。"""
        missed = self.run_cli('export')
        self.assertEqual(missed.returncode, 2)
        self.assertFalse(os.path.exists(os.path.join(self.repo, '.omm')))
        self.scan()
        self.assertEqual(self.run_cli('store').returncode, 0)
        placed = self.run_cli('export')
        self.assertEqual(placed.returncode, 0, placed.stderr)
        word, _, where = placed.stdout.strip().partition(' ')
        # git の出すルートは別名を解いた形 (macOS の /private/var、Windows の長い名前) なので、場所の同じさで比べる
        self.assertEqual(word, 'placed')
        self.assertTrue(os.path.samefile(where, os.path.join(self.repo, '.omm')), where)
        self.assertTrue(os.path.isfile(os.path.join(self.repo, '.omm', 'overall-architecture', 'description.md')))
        self.assertEqual(self.run_cli('lookup').stdout.split()[0], 'hit')

    def test_export_leaves_an_untracked_omm_alone(self):
        """作業ツリーに git が追っていない .omm/ があれば上書きしない。"""
        self.scan()
        self.assertEqual(self.run_cli('store').returncode, 0)
        os.makedirs(os.path.join(self.repo, '.omm', 'mine'))
        result = self.run_cli('export')
        self.assertEqual(result.returncode, 2)
        self.assertIn('not tracked', result.stderr)
        self.assertTrue(os.path.isdir(os.path.join(self.repo, '.omm', 'mine')))
        self.assertFalse(os.path.exists(os.path.join(self.repo, '.omm', 'overall-architecture')))


if __name__ == '__main__':
    unittest.main()
