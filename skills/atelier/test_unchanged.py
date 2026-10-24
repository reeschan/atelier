import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'unchanged.py')


class UnchangedTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / 'src').mkdir()
        (self.root / 'src' / 'a.py').write_text('a = 1\n', encoding='utf-8')
        (self.root / 'src' / 'b.py').write_text('b = 1\n', encoding='utf-8')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_script(self, *args):
        return subprocess.run([sys.executable, SCRIPT, *args], cwd=self.root,
                              capture_output=True, text=True, encoding='utf-8')

    def save(self, *files):
        r = self.run_script('save', 'PBI-001', *files)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def test_check_passes_when_nothing_changed(self):
        self.save('src/a.py', 'src/b.py')
        r = self.run_script('check', 'PBI-001')
        self.assertEqual((r.returncode, r.stdout), (0, ''))
        self.assertTrue((self.root / 'docs/pbi/verify/PBI-001.unchanged.json').is_file())

    def test_check_lists_only_changed_files(self):
        self.save('src/a.py', 'src/b.py')
        (self.root / 'src' / 'b.py').write_text('b = 2\n', encoding='utf-8')
        r = self.run_script('check', 'PBI-001')
        self.assertEqual((r.returncode, r.stdout), (1, 'changed src/b.py\n'))

    def test_untracked_and_unrelated_files_do_not_count(self):
        # git status なら ?? や他の PBI の変更で誤検知する場面
        self.save('src/a.py')
        (self.root / 'src' / 'new.py').write_text('x = 1\n', encoding='utf-8')
        (self.root / 'src' / 'b.py').write_text('b = 2\n', encoding='utf-8')
        r = self.run_script('check', 'PBI-001')
        self.assertEqual((r.returncode, r.stdout), (0, ''))

    def test_deleted_and_created_files_count_as_changed(self):
        self.save('src/a.py', 'src/missing.py')
        (self.root / 'src' / 'a.py').unlink()
        (self.root / 'src' / 'missing.py').write_text('', encoding='utf-8')
        r = self.run_script('check', 'PBI-001')
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout.splitlines(), ['changed src/a.py', 'changed src/missing.py'])

    def test_check_without_save_exits_2(self):
        r = self.run_script('check', 'PBI-009')
        self.assertEqual(r.returncode, 2)
        self.assertIn('run save first', r.stderr)

    def test_dir_option(self):
        r = self.run_script('--dir', 'work', 'save', 'PBI-001', 'src/a.py')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.root / 'work/verify/PBI-001.unchanged.json').is_file())
        self.assertEqual(self.run_script('--dir', 'work', 'check', 'PBI-001').returncode, 0)


if __name__ == '__main__':
    unittest.main()
