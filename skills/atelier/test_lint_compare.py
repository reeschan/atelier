"""lint_compare.py を CLI から動かし、前後の jevlint の報告から悪化の行と件数が出ることを確かめる。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'lint_compare.py')


def entry(path, score, lines=(), answers=None):
    data = {'path': path, 'score': score,
            'ast': {'findings': [{'rule': 'function-lines', 'value': n, 'limit': 60, 'line': 1} for n in lines]}}
    if answers is not None:
        data['model'] = {'answers': [{'id': k, 'compliance': v, 'fix': f'fix {k}'} for k, v in answers.items()]}
    return data


class LintCompareTest(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.join(tempfile.gettempdir(), f'lint-compare-{uuid.uuid4().hex}')
        os.mkdir(self.dir)
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def write(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        return path

    def compare(self, before, after, config=None):
        args = [sys.executable, SCRIPT, self.write('before.json', {'files': before}),
                self.write('after.json', {'files': after}), '--out', os.path.join(self.dir, 'recheck.md')]
        if config is not None:
            args += ['--config', self.write('lint.json', config)]
        result = subprocess.run(args, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(os.path.join(self.dir, 'recheck.md'), encoding='utf-8') as f:
            return result.stdout.strip(), f.read()

    def test_lists_each_kind_of_regression(self):
        before = [entry('src/a.ts', 90, [120], {'bug-hunt.swallowed-error': 0.9, 'readable-code.naming': 0.8})]
        after = [entry('src/a.ts', 80, [140, 101, 70], {'bug-hunt.swallowed-error': 0.4, 'readable-code.naming': 0.5})]
        out, table = self.compare(before, after)
        self.assertTrue(out.startswith('worse 5 (bug-hunt 1, ast 2, question 1, score 1)'), out)
        self.assertIn('| src/a.ts | bug-hunt | bug-hunt.swallowed-error 0.9 -> 0.4; fix: fix bug-hunt.swallowed-error '
                      '| rework-candidate |', table)
        self.assertIn('| src/a.ts | ast | function-lines 140 (was 120) | report |', table)
        self.assertIn('| src/a.ts | ast | function-lines 101 (new) | rework-candidate |', table)
        self.assertIn('| src/a.ts | question | readable-code.naming 0.8 -> 0.5', table)
        self.assertIn('| src/a.ts | score | score 90 -> 80 | report |', table)
        self.assertNotIn('function-lines 70', table)

    def test_small_changes_and_improvements_are_not_regressions(self):
        before = [entry('src/a.ts', 90, [150], {'readable-code.naming': 0.5})]
        after = [entry('src/a.ts', 87, [140], {'readable-code.naming': 0.4})]
        out, table = self.compare(before, after)
        self.assertTrue(out.startswith('worse 0 '), out)
        self.assertIn('悪くなった所は無い。', table)

    def test_slight_growth_of_an_existing_finding_is_not_listed(self):
        out, _table = self.compare([entry('src/a.ts', 90, [297])], [entry('src/a.ts', 90, [298])])
        self.assertTrue(out.startswith('worse 0 '), out)

    def test_report_only_paths_never_ask_for_rework(self):
        after = [entry('e2e/a.spec.ts', 90, [300], {'bug-hunt.swallowed-error': 0.2})]
        _out, table = self.compare([], after, {'report_only_paths': ['e2e/**']})
        self.assertIn('| e2e/a.spec.ts | ast | function-lines 300 (new) | report |', table)
        self.assertIn('| e2e/a.spec.ts | bug-hunt | bug-hunt.swallowed-error', table)
        self.assertNotIn('rework-candidate', table)

    def test_new_files_count_weak_questions_but_not_scores(self):
        after = [entry('src/new.ts', 40, [], {'bug-hunt.swallowed-error': 0.3,
                                              'documentation.contracts-stated': 0.1})]
        out, table = self.compare([], after)
        self.assertTrue(out.startswith('worse 1 (bug-hunt 1, ast 0, question 0, score 0)'), out)
        self.assertNotIn('contracts-stated', table)

    def test_ast_only_reports_skip_questions(self):
        out, _table = self.compare([entry('src/a.ts', 90)], [entry('src/a.ts', 90)])
        self.assertTrue(out.startswith('worse 0 '), out)

    def test_config_changes_the_lines(self):
        before = [entry('src/a.ts', 90, [], {'readable-code.naming': 0.8})]
        after = [entry('src/a.ts', 88, [80], {'readable-code.naming': 0.75})]
        out, _table = self.compare(before, after, {'score_drop': 2, 'function_lines': 60, 'compliance': 0.78})
        self.assertTrue(out.startswith('worse 3 (bug-hunt 0, ast 1, question 1, score 1)'), out)

    def test_unreadable_report_exits_2(self):
        bad = self.write('bad.json', {'nothing': True})
        result = subprocess.run([sys.executable, SCRIPT, bad, bad], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 2)
        self.assertIn('not a jevlint audit report', result.stderr)


if __name__ == '__main__':
    unittest.main()
