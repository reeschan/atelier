"""tests_plan.py: survey の表とケースからテスト PBI と波を書き、例外だけを出す。"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / 'tests_plan.py'

SURVEY = """# Test survey

| file:test | verdict | reason | action |
|---|---|---|---|
| tests/price.test.ts:rounds half up | fix | the rule moved to round() | assert round(1.25) |
| tests/price.test.ts:calls helper | delete | covered by tests/price.test.ts:rounds half up | remove it |
| tests/price.test.ts | add | Tests for the end: tax is added | assert the total with tax |
| tests/cart.test.ts:adds | keep | catches a broken add | |
| tests/login.test.ts:shows error | rewrite | asserts a CSS class | assert the message |
| tests/login.test.ts:old | delete | it was old | remove |

### Cases: tests/price.test.ts
Exemplar: tests/cart.test.ts
| id | behaviour | input | expected | mock |
|---|---|---|---|---|
| C1 | rounds half up (req 2) | round(1.25) | 1.3 | none |
| C2 | tax is added (Tests for the end) | total([100]) | 110 | clock returns 2026-01-01 |

### Cases: tests/login.test.ts
Exemplar: tests/cart.test.ts
| id | behaviour | input | expected | mock |
|---|---|---|---|---|
| C1 | shows the error | submit with no password | "Password is required" | none |
| C2 | locks after 3 tries | submit 3 bad passwords | ? | none |
"""


class TestsPlanTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='tests-plan-'))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.base = self.root / 'docs' / 'pbi'
        (self.base / 'notes').mkdir(parents=True)
        (self.base / 'notes' / 'test-survey.md').write_text(SURVEY, encoding='utf-8')
        (self.base / 'PBI-001.md').write_text('# PBI-001: price\n\n## Targets\n- `src/price.ts`: round\n\n'
                                              '## Do not touch\nnone\n', encoding='utf-8')
        (self.base / 'PBI-002.md').write_text('# PBI-002\n\n## Targets\n- src/login.ts\n', encoding='utf-8')
        (self.base / 'waves.json').write_text(json.dumps({'waves': [{'n': 1, 'pbis': ['PBI-001', 'PBI-002']}]}),
                                              encoding='utf-8')
        self.settings({'test_paths': ['tests/**'], 'test_command': 'pnpm vitest run {file}',
                       'test_typecheck': 'pnpm tsc -p tests', 'implement_tier': 'haiku'})

    def settings(self, data):
        (self.base / 'tests-at-end.json').write_text(json.dumps(data), encoding='utf-8')

    def run_plan(self, *args):
        proc = subprocess.run([sys.executable, str(SCRIPT), 'docs/pbi', *args], cwd=self.root, capture_output=True,
                              text=True, encoding='utf-8')
        return proc.returncode, proc.stdout + proc.stderr

    def test_writes_one_pbi_per_clean_file_and_lists_only_the_exceptions(self):
        code, out = self.run_plan()
        self.assertEqual(code, 4, out)
        self.assertIn('wrote PBI-003 tests/price.test.ts (2 cases, 1 deletes)', out)
        # login: ? のあるケースと、理由の形が違う delete。PBI は書かない
        self.assertIn('exception: tests/login.test.ts: case C2: expected has ?', out)
        self.assertIn("exception: tests/login.test.ts: delete old: the reason starts with neither", out)
        self.assertFalse((self.base / 'PBI-004.md').exists())
        self.assertNotIn('cart.test.ts', out)  # keep だけの file
        self.assertIn('wave 2: PBI-003', out)
        self.assertIn('--only PBI-003 --tier haiku', out)
        waves = json.loads((self.base / 'waves.json').read_text(encoding='utf-8'))['waves']
        self.assertEqual(waves[-1], {'n': 2, 'pbis': ['PBI-003'], 'kind': 'test'})

        pbi = (self.base / 'PBI-003.md').read_text(encoding='utf-8')
        self.assertIn('- Test file: `tests/price.test.ts`', pbi)
        self.assertIn('## Targets\n- `tests/price.test.ts`', pbi)
        self.assertIn('`src/price.ts`, `src/login.ts`', pbi)
        self.assertIn('1. C1 (rounds half up (req 2)): verify 1 runs the test named C1 and it passes.', pbi)
        self.assertIn('3. Delete tests/price.test.ts:calls helper:', pbi)
        self.assertIn('1. `pnpm vitest run tests/price.test.ts`\n2. `pnpm tsc -p tests`', pbi)
        self.assertIn('Exemplar: tests/cart.test.ts', pbi)
        self.assertIn('| C2 | tax is added (Tests for the end) | total([100]) | 110 | clock returns 2026-01-01 |', pbi)
        self.assertIn('Delete: tests/price.test.ts:calls helper (covered by', pbi)
        self.assertIn('- fix rounds half up: assert round(1.25)', pbi)
        facts = json.loads((self.base / 'PBI-003.facts.json').read_text(encoding='utf-8'))
        self.assertEqual((facts['criteria'], facts['files_new'], facts['sensitive']), (3, 1, False))
        self.assertTrue((self.base / 'PBI-003.brief.md').is_file())

    def test_format_and_lint_come_first_when_checks_json_names_them(self):
        (self.base / 'checks.json').write_text(json.dumps({'format': 'pnpm prettier --write', 'lint': 'pnpm eslint --fix'}),
                                               encoding='utf-8')
        self.run_plan()
        pbi = (self.base / 'PBI-003.md').read_text(encoding='utf-8')
        self.assertIn('1. `pnpm prettier --write tests/price.test.ts`\n2. `pnpm eslint --fix tests/price.test.ts`\n'
                      '3. `pnpm vitest run tests/price.test.ts`', pbi)
        self.assertIn('verify 3 runs the test named C1', pbi)
        self.assertIn('4. Format and lint: verify 1, 2 exit 0.', pbi)

    def test_a_second_run_keeps_the_planned_file(self):
        self.run_plan()
        code, out = self.run_plan()
        self.assertIn('kept PBI-003 tests/price.test.ts (already planned)', out)
        self.assertFalse((self.base / 'PBI-004.md').exists())
        waves = json.loads((self.base / 'waves.json').read_text(encoding='utf-8'))['waves']
        self.assertEqual(len(waves), 2)

    def test_no_test_command_writes_nothing(self):
        self.settings({'test_paths': ['tests/**']})
        code, out = self.run_plan()
        self.assertEqual(code, 4, out)
        self.assertIn('no test_command with {file}', out)
        self.assertFalse((self.base / 'PBI-003.md').exists())

    def test_files_outside_test_paths_and_missing_cases_are_exceptions(self):
        (self.base / 'notes' / 'test-survey.md').write_text(
            '| file:test | verdict | reason | action |\n|---|---|---|---|\n'
            '| src/a.ts:x | add | new | assert y |\n| tests/b.test.ts:z | fix | broke | fix it |\n'
            '| tests/c.test.ts:w | maybe | ? | ? |\n', encoding='utf-8')
        code, out = self.run_plan('--dry-run')
        self.assertEqual(code, 4, out)
        self.assertIn('exception: src/a.ts: not in test_paths', out)
        self.assertIn('exception: tests/b.test.ts: fix, rewrite or add rows but no "### Cases:" section', out)
        self.assertIn("exception: tests/c.test.ts: verdict 'maybe' is not one of", out)
        self.assertFalse(list(self.base.glob('PBI-00[3-9].md')))

    def test_waves_hold_at_most_three_test_pbis(self):
        rows, sections = [], []
        for i in range(4):
            rows.append(f'| tests/t{i}.test.ts | add | new | assert |')
            sections.append(f'### Cases: tests/t{i}.test.ts\nExemplar: none (vitest, expect)\n'
                            '| id | behaviour | input | expected | mock |\n|---|---|---|---|---|\n'
                            '| C1 | b | f(1) | 2 | none |\n')
        (self.base / 'notes' / 'test-survey.md').write_text(
            '| file:test | verdict | reason | action |\n|---|---|---|---|\n' + '\n'.join(rows) + '\n\n'
            + '\n'.join(sections), encoding='utf-8')
        code, out = self.run_plan()
        self.assertEqual(code, 0, out)
        self.assertIn('wave 2: PBI-003,PBI-004,PBI-005', out)
        self.assertIn('wave 3: PBI-006', out)


if __name__ == '__main__':
    unittest.main()
