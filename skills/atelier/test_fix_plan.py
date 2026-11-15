"""fix_plan.py: 直しの一覧から直しの PBI と波を書き、直しの差分を出す (修正の段、appendix/fix.md)。"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / 'fix_plan.py'


def item(id_, file, what='wrong', fix='right', **more):
    return {'id': id_, 'severity': 'major', 'file': file, 'line': 3, 'what': what, 'fix': fix,
            'decision': 'fix', 'from': 'review', **more}


class FixPlanTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='fix-plan-'))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.base = self.root / 'docs' / 'pbi'
        self.base.mkdir(parents=True)
        for rel, text in {'src/a.ts': 'a1\na2\na3\n', 'src/b.ts': 'b\n', 'src/c.ts': 'c\n', 'src/d.ts': 'd\n',
                          'docs/adr/0001.md': '# ADR 1\n'}.items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8', newline='\n')
        (self.base / 'PBI-001.md').write_text('# PBI-001\n\n## Targets\n- `src/a.ts`\n', encoding='utf-8')
        (self.base / 'PBI-002.md').write_text('# PBI-002\n\n## Targets\n- `src/b.ts`\n', encoding='utf-8')
        (self.base / 'waves.json').write_text(json.dumps({'waves': [{'n': 1, 'pbis': ['PBI-001', 'PBI-002']}]}),
                                              encoding='utf-8')
        (self.base / 'checks.json').write_text(json.dumps({'format': 'npx prettier --check', 'lint': 'npx eslint'}),
                                               encoding='utf-8')

    def fixes(self, items, number=1, **more):
        data = {'schema': 'atelier.fixes/1', 'round': number, 'source': 'review', 'tier': 'sonnet',
                'checks': ['pnpm tsc --noEmit', 'pnpm run test:unit'], 'items': items, **more}
        path = self.base / 'fix' / f'round-{number}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding='utf-8')

    def run_plan(self, *args, command='plan'):
        proc = subprocess.run([sys.executable, str(SCRIPT), command, 'docs/pbi', *args], cwd=self.root,
                              capture_output=True, text=True, encoding='utf-8')
        return proc.returncode, proc.stdout + proc.stderr

    def read(self, rel):
        return (self.base / rel).read_text(encoding='utf-8')

    def waves(self):
        return json.loads(self.read('waves.json'))['waves']

    def test_items_sharing_files_go_to_one_worker_and_one_wave_holds_every_worker(self):
        self.fixes([item('F1', 'src/a.ts'), item('F2', 'src/b.ts', files=['src/a.ts']),
                    item('F3', 'src/c.ts', check='node scripts/check-c.mjs'),
                    item('F4', 'docs/adr/0001.md', severity='critical', what='contradicts the code',
                         fix='write ADR 2 that supersedes it'),
                    item('F5', 'src/d.ts'), item('F6', 'src/d.ts', decision='skip')])
        code, out = self.run_plan()
        self.assertEqual(code, 0, out)
        self.assertIn('skipped F6 (skip)', out)
        # F1 と F2 は src/a.ts を共にするので同じ人。4 つの塊を 3 人に分ける
        self.assertIn('wrote PBI-003 (2 items: F1,F2; files: src/a.ts, src/b.ts)', out)
        self.assertEqual(self.waves()[-1], {'n': 2, 'pbis': ['PBI-003', 'PBI-004', 'PBI-005'], 'kind': 'fix',
                                            'round': 1})
        self.assertIn('wave 2: PBI-003,PBI-004,PBI-005', out)
        self.assertIn('--only PBI-003,PBI-004,PBI-005 --tier sonnet', out)
        self.assertIn('design_record.py work PBI-003', out)
        planned = {name: self.read(f'{name}.md') for name in ('PBI-003', 'PBI-004', 'PBI-005')}
        owners = {fid: name for name, text in planned.items() for fid in ('F1', 'F2', 'F3', 'F4', 'F5')
                  if f'- Fix items: ' in text and fid in text.split('- Fix items: ')[1].splitlines()[0]}
        self.assertEqual(owners['F1'], owners['F2'])
        self.assertEqual(len(set(owners.values())), 3)
        targets = [set(line[3:-1] for line in text.split('## Targets\n')[1].split('\n\n')[0].splitlines())
                   for text in planned.values()]
        for i, one in enumerate(targets):
            for other in targets[i + 1:]:
                self.assertFalse(one & other, 'two workers of one wave never share a file')
        text = planned['PBI-003']
        self.assertIn('- Fix round: 1', text)
        self.assertIn('1. `npx prettier --check src/a.ts src/b.ts`', text)
        self.assertIn('3. `pnpm tsc --noEmit`', text)
        self.assertIn('F1 (src/a.ts:3, major): wrong — fixed as: right.', text)
        self.assertIn('fix_plan.py diff docs/pbi PBI-003', text)
        check_owner = planned[owners['F3']]
        self.assertIn('5. `node scripts/check-c.mjs`', check_owner)
        self.assertIn('; verify 5 exits 0.', check_owner)
        # PBI に属さないファイル (ADR) も直せる
        self.assertIn('`docs/adr/0001.md`', planned[owners['F4']])
        facts = json.loads(self.read('PBI-003.facts.json'))
        self.assertEqual((facts['files_changed'], facts['criteria']), (2, 4))
        self.assertTrue((self.base / 'fix/round-1/base/src/a.ts').is_file())

    def test_ask_left_writes_nothing(self):
        self.fixes([item('F1', 'src/a.ts'), item('F2', 'src/b.ts', severity='minor', decision='ask',
                                                 recommend='fix')])
        code, out = self.run_plan()
        self.assertEqual(code, 4, out)
        self.assertIn('exception: F2: decision is still ask', out)
        self.assertIn('nothing written', out)
        self.assertEqual(len(self.waves()), 1)
        self.assertFalse((self.base / 'PBI-003.md').exists())

    def test_exceptions_name_the_item_and_its_problem(self):
        self.fixes([item('F1', '../outside.ts'), item('F1', 'src/b.ts'), item('F3', 'docs/pbi/STATUS.md'),
                    item('F4', 'src/c.ts', fix=''), {'id': 'F5', 'severity': 'odd', 'decision': 'maybe'},
                    item('F6', 'src/d.ts')])
        code, out = self.run_plan()
        self.assertEqual(code, 4, out)
        self.assertIn('exception: F1: ../outside.ts: has ..', out)
        self.assertIn('exception: F1: the id is used twice', out)
        self.assertIn('exception: F3: docs/pbi/STATUS.md: inside docs/pbi', out)
        self.assertIn('exception: F4: fix is empty', out)
        self.assertIn("exception: F5: decision 'maybe' is not one of fix, skip, ask", out)
        self.assertIn('wrote PBI-003 (1 items: F6; files: src/d.ts)', out)

    def test_planning_again_keeps_the_planned_round_and_refuses_new_items(self):
        self.fixes([item('F1', 'src/a.ts')])
        self.assertEqual(self.run_plan()[0], 0)
        code, out = self.run_plan()
        self.assertEqual(code, 0, out)
        self.assertIn('kept PBI-003 (F1; already planned)', out)
        self.assertEqual(len(self.waves()), 2)
        self.fixes([item('F1', 'src/a.ts'), item('F2', 'src/b.ts')])
        code, out = self.run_plan()
        self.assertEqual(code, 4, out)
        self.assertIn('write the new items as round 2', out)

    def test_a_round_written_but_not_in_waves_is_rewritten_with_the_same_number(self):
        self.fixes([item('F1', 'src/a.ts')])
        self.run_plan()
        (self.base / 'waves.json').write_text(json.dumps({'waves': [{'n': 1, 'pbis': ['PBI-001']}]}),
                                              encoding='utf-8')
        code, out = self.run_plan()
        self.assertEqual(code, 0, out)
        self.assertIn('wrote PBI-003', out)
        self.assertEqual(self.waves()[-1]['pbis'], ['PBI-003'])

    def test_diff_shows_only_the_fix(self):
        self.fixes([item('F1', 'src/a.ts')])
        self.run_plan()
        # 直す前の写しは流し直しても変わらない
        (self.root / 'src/a.ts').write_text('a1\nA2\na3\n', encoding='utf-8', newline='\n')
        self.run_plan()
        code, out = self.run_plan('PBI-003', command='diff')
        self.assertEqual(code, 0, out)
        self.assertIn('--- a/src/a.ts (before the fix)', out)
        self.assertIn('-a2\n+A2', out)
        changed = [line for line in out.splitlines() if line[:1] in '+-' and not line.startswith(('+++', '---'))]
        self.assertEqual(changed, ['-a2', '+A2'])
        code, out = self.run_plan('PBI-001', command='diff')
        self.assertEqual(code, 2, out)
        self.assertIn('not a fix PBI', out)

    def test_nothing_to_fix(self):
        self.fixes([item('F1', 'src/a.ts', decision='skip')])
        code, out = self.run_plan()
        self.assertEqual(code, 0, out)
        self.assertIn('nothing to fix in round 1', out)
        code, out = self.run_plan('--round', '9')
        self.assertEqual(code, 2, out)


if __name__ == '__main__':
    unittest.main()
