'''read_first.json の表と read_first.py の入口・まとめたファイルを確かめる。

表の検査は同梱の read_first.json を、切り出しの検査は一時のスキルのフォルダを使う。
どちらも、まとめたファイル (build() が書いたもの) の中身と、entry_of() の戻り値だけを見る。
上限 (limits) を超えたら、読む量が増えすぎた印。中身を episode のファイルへ寄せ直すか、理由があれば上限を上げる。
'''
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import read_first

HERE = Path(__file__).resolve().parent
TABLE = json.loads((HERE / 'read_first.json').read_text(encoding='utf-8'))
DESIGNER_EPISODES = ('design', 'build', 'test', 'review', 'fix', 'finish')
HEADING = re.compile(r'^(#{1,6}) (.+?)\s*$')


def headings_of(path: Path) -> set:
    '''ファイルの見出しの名前の集合 (``` の囲みの中は除く)。'''
    names, fenced = set(), False
    for line in path.read_text(encoding='utf-8').splitlines():
        if line.lstrip().startswith('```'):
            fenced = not fenced
            continue
        match = None if fenced else HEADING.match(line)
        if match:
            names.add(match.group(2))
    return names


def entries():
    '''(episode, entry, 並び) をすべて返す。'''
    return [(episode, entry, items)
            for episode, table in TABLE['episodes'].items() for entry, items in table.items()]


class TableTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='read-first-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_every_designer_episode_has_a_first_entry(self):
        for episode in DESIGNER_EPISODES:
            self.assertIn('first', TABLE['episodes'].get(episode, {}), episode)
            self.assertIn(episode, TABLE['limits'], episode)

    def test_every_file_and_heading_exists(self):
        missing = []
        for episode, entry, items in entries():
            for item in items:
                if item.startswith('docs/'):
                    continue
                name, _, heading = item.partition('#')
                path = HERE / name
                if not path.is_file():
                    missing.append(f'{episode}/{entry}: {name}')
                elif heading and heading not in headings_of(path):
                    missing.append(f'{episode}/{entry}: {item}')
        self.assertEqual([], missing, '\n'.join(missing))

    def test_work_root_files_are_under_docs_pbi(self):
        for episode, entry, items in entries():
            for item in items:
                if item.startswith('docs/'):
                    self.assertTrue(item.startswith('docs/pbi/'), f'{episode}/{entry}: {item}')

    def test_every_entry_starts_with_the_core_and_ends_with_kiln_and_question(self):
        for episode, entry, items in entries():
            self.assertEqual('designer.md', items[0], f'{episode}/{entry}')
            self.assertEqual(['episode/_kiln.md', 'episode/_question.md'], items[-2:], f'{episode}/{entry}')

    def test_each_bundle_stays_within_the_limit_of_its_episode(self):
        over = []
        for episode, entry, _items in entries():
            out = read_first.build(episode, entry, self.tmp / f'{episode}-{entry}.md'.replace(':', '_'))
            size = len(out.read_text(encoding='utf-8'))
            if size > TABLE['limits'][episode]:
                over.append(f'{episode}/{entry}: {size} > {TABLE["limits"][episode]}')
        self.assertEqual([], over, '\n'.join(over))

    def test_build_test_and_fix_read_one_pbi(self):
        for episode in ('build', 'test', 'fix'):
            self.assertIn('episode/one-pbi.md', TABLE['episodes'][episode]['first'], episode)

    def test_design_and_build_read_approve(self):
        self.assertIn('episode/approve.md', TABLE['episodes']['design']['first'])
        for entry in ('answer:approve', 'answer:rework-limit'):
            self.assertIn('episode/approve.md', TABLE['episodes']['build'][entry], entry)

    def test_build_test_fix_and_review_point_at_the_run_dead_ends(self):
        for episode in ('build', 'test', 'review', 'fix'):
            self.assertIn('docs/pbi/dead-ends.md', TABLE['episodes'][episode]['first'], episode)

    def test_bundle_names_the_episode_steps_and_the_kiln_part(self):
        out = read_first.build('build', 'first', self.tmp / 'build.md')
        text = out.read_text(encoding='utf-8')
        self.assertIn('# Episode: build', text)
        self.assertIn('## One PBI', text)
        self.assertIn('kiln_log.py tablet-check', text)
        self.assertIn('--boundary start', text)
        self.assertIn('design_record.py lint --draft', text)
        self.assertIn('docs/pbi/dead-ends.md', text)


class EntryTest(unittest.TestCase):
    def test_first_launch(self):
        self.assertEqual('first', read_first.entry_of(False, None, None, False, None))

    def test_continue_from_a_tablet(self):
        self.assertEqual('continue', read_first.entry_of(True, None, None, False, None))

    def test_answer_with_and_without_topic(self):
        self.assertEqual('answer:approve', read_first.entry_of(False, 'approve', None, False, None))
        self.assertEqual('answer:decision:omm', read_first.entry_of(True, 'decision', 'omm', False, None))

    def test_escalated_wins_over_lead_and_answer(self):
        self.assertEqual('escalated', read_first.entry_of(True, 'decision', None, True, 'test'))

    def test_lead_wins_over_answer_and_continue(self):
        self.assertEqual('lead:fix', read_first.entry_of(True, 'decision', 'other', False, 'fix'))
        self.assertEqual('lead:test', read_first.entry_of(False, None, None, False, 'test on sonnet high'))


class BuildTest(unittest.TestCase):
    '''一時のスキルのフォルダで、切り出しとつなぎ方を確かめる。'''

    def setUp(self):
        self.skill = Path(tempfile.mkdtemp(prefix='read-first-skill-'))
        self.addCleanup(shutil.rmtree, self.skill, ignore_errors=True)
        (self.skill / 'appendix').mkdir()
        (self.skill / 'core.md').write_text('# Core\n\nCore body.\n', encoding='utf-8')
        (self.skill / 'appendix' / 'a.md').write_text(
            '# Appendix A\n\nPreface of A.\n\n'
            '## One\n\nOne body.\n\n### One child\n\nChild body.\n\n'
            '```text\n## Not a heading\n```\n\nStill one.\n\n'
            '## Two\n\nTwo body.\n\n# Top again\n\nTop body.\n',
            encoding='utf-8')
        table = {
            'limits': {'design': 100000},
            'episodes': {'design': {
                'first': ['core.md', 'appendix/a.md#One', 'docs/pbi/dead-ends.md'],
                'answer:decision': ['core.md', 'appendix/a.md#Two'],
            }},
        }
        (self.skill / 'read_first.json').write_text(json.dumps(table), encoding='utf-8')
        self.out = self.skill / 'out' / '3-read.md'

    def build(self, entry):
        path = read_first.build('design', entry, self.out, self.skill)
        self.assertEqual(self.out, path)
        return path.read_text(encoding='utf-8')

    def test_section_comes_with_the_preface_of_its_file(self):
        text = self.build('first')
        self.assertIn('Core body.', text)
        self.assertIn('Preface of A.', text)
        self.assertIn('## One', text)
        self.assertIn('Child body.', text)
        self.assertIn('## Not a heading', text)
        self.assertIn('Still one.', text)
        self.assertNotIn('Two body.', text)
        self.assertNotIn('Top body.', text)

    def test_each_part_starts_with_its_file_and_heading(self):
        text = self.build('first')
        self.assertRegex(text, r'(?m)^<!-- from core\.md -->$')
        self.assertRegex(text, r'(?m)^<!-- from appendix/a\.md, ## One -->$')

    def test_work_root_files_are_named_not_copied(self):
        text = self.build('first')
        self.assertIn('docs/pbi/dead-ends.md', text)

    def test_section_ends_before_a_higher_heading(self):
        text = self.build('answer:decision')
        self.assertIn('Two body.', text)
        self.assertNotIn('Top body.', text)
        self.assertNotIn('One body.', text)

    def test_answer_with_topic_falls_back_to_its_kind_then_to_first(self):
        self.assertIn('Two body.', self.build('answer:decision:omm'))
        self.assertIn('One body.', self.build('continue'))

    def test_missing_heading_is_an_error(self):
        table = json.loads((self.skill / 'read_first.json').read_text(encoding='utf-8'))
        table['episodes']['design']['first'] = ['appendix/a.md#Three']
        (self.skill / 'read_first.json').write_text(json.dumps(table), encoding='utf-8')
        with self.assertRaises(ValueError) as caught:
            self.build('first')
        self.assertIn('appendix/a.md#Three', str(caught.exception))

    def test_cli_writes_the_file_and_prints_its_path(self):
        result = subprocess.run(
            [sys.executable, str(HERE / 'read_first.py'), 'design', '--entry', 'answer:decision',
             '--out', str(self.out), '--skill-dir', str(self.skill)],
            capture_output=True, text=True, encoding='utf-8',
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(f'read-first: {self.out.as_posix()}', result.stdout.strip())
        self.assertIn('Two body.', self.out.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
