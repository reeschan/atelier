"""run_tests.py (テストを複数のプロセスで流す) の試験。一時フォルダに小さなテストを置いて、別のプロセスで流す。"""
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'run_tests.py')

PASSING = '''
import os
import unittest


class A(unittest.TestCase):
    def test_one(self):
        self.assertEqual(1, 1)

    def test_two(self):
        os.environ['RUN_TESTS_SAMPLE'] = 'x'  # 別のプロセスの束には漏れない

    @unittest.skip('skipped on purpose')
    def test_skip(self):
        pass


class B(unittest.TestCase):
    def test_three(self):
        self.assertTrue(True)
'''

FAILING = '''
import unittest


class C(unittest.TestCase):
    def test_fails(self):
        self.assertEqual(1, 2)

    def test_errors(self):
        raise RuntimeError('boom')
'''


class RunTestsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='run-tests-')
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def write(self, name, text):
        with open(os.path.join(self.dir, name), 'w', encoding='utf-8') as stream:
            stream.write(textwrap.dedent(text))

    def run_tests(self, *extra):
        p = subprocess.run([sys.executable, SCRIPT, '-s', self.dir, '-j', '2', '--chunk', '1', *extra],
                           capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)
        return p.returncode, p.stderr

    def test_all_passing_is_ok(self):
        self.write('test_pass.py', PASSING)
        code, err = self.run_tests()
        self.assertEqual(code, 0, err)
        self.assertIn('Ran 4 tests in ', err)
        self.assertIn('OK (skipped=1)', err)

    def test_failures_and_errors_are_reported(self):
        self.write('test_pass.py', PASSING)
        self.write('test_fail.py', FAILING)
        code, err = self.run_tests()
        self.assertEqual(code, 1, err)
        self.assertIn('Ran 6 tests in ', err)
        self.assertIn('FAILED (failures=1, errors=1, skipped=1)', err)
        self.assertIn('FAIL: test_fails (test_fail.C', err)
        self.assertIn('ERROR: test_errors (test_fail.C', err)
        self.assertIn('RuntimeError: boom', err)

    def test_a_module_that_cannot_be_imported_is_an_error(self):
        self.write('test_pass.py', PASSING)
        self.write('test_broken.py', 'import no_such_module_here\n')
        code, err = self.run_tests()
        self.assertEqual(code, 1, err)
        self.assertIn('Ran 5 tests in ', err)
        self.assertIn('no_such_module_here', err)

    def test_no_tests_is_a_failure(self):
        code, err = self.run_tests()
        self.assertEqual(code, 1, err)
        self.assertIn('no tests ran', err)

    def test_bad_arguments_exit_2(self):
        code, err = self.run_tests('--chunk', '0')
        self.assertEqual(code, 2, err)


if __name__ == '__main__':
    unittest.main()
