import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from atelier import survey


class SurveyTests(unittest.TestCase):
    def facts(self):
        return {'schema': 'atelier.facts/1', **{k: [] for k in survey.FIELDS}}

    def test_validation_checks_shape_and_sources(self):
        obj = self.facts()
        obj['modules'] = [{'id': 'm', 'text': 'module', 'source': 'src/a.py:12'}, {'id': 'n', 'text': 'node', 'source': 'omm:architecture/node'}]
        self.assertEqual([], survey.validate_facts(obj))
        obj['modules'][0]['source'] = ''
        self.assertTrue(survey.validate_facts(obj))
        obj['modules'][0]['source'] = 'src/a.py'
        self.assertTrue(survey.validate_facts(obj))
        self.assertTrue(survey.validate_facts({'schema': 'wrong'}))
        self.assertTrue(survey.validate_facts([]))

    def test_prompt_includes_paths_and_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            path = survey.survey_prompt(root, root / 'goal.txt', None, {'source': 'none', 'folder': None})
            body = path.read_text(encoding='utf-8')
            self.assertIn(str(root / 'goal.txt'), body)
            self.assertIn(str(root / 'docs' / 'pbi' / 'results' / 'survey.json'), body)
            self.assertIn('fallback', body.lower())
            self.assertIn('atelier.facts/1', body)

    def test_missing_tools_or_cache_returns_none(self):
        with tempfile.TemporaryDirectory() as d, patch.object(survey, '_cache_script', return_value=None):
            self.assertEqual({'source': 'none', 'folder': None, 'reason': 'tools'}, survey.omm_context(d, Path(d) / 'cache'))

    def test_hit_and_near_paths_with_spaces(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root = base / 'repo'
            root.mkdir()
            cache = base / 'outside cache'
            cache.mkdir()
            hit = cache / 'hit folder'
            (hit / '.omm').mkdir(parents=True)
            script = root / 'cache.mjs'
            script.touch()
            with patch.object(survey, '_cache_script', return_value=script), patch.object(survey.shutil, 'which', return_value='tool'), patch.object(survey, '_run', return_value=f'hit {hit}'):
                self.assertEqual({'source': 'cache-hit', 'folder': str(hit / '.omm')}, survey.omm_context(root, cache))
            work = cache / 'derived folder'
            (work / '.omm').mkdir(parents=True)
            with patch.object(survey, '_cache_script', return_value=script), patch.object(survey.shutil, 'which', return_value='tool'), patch.object(survey, '_run', side_effect=[f'near {hit} abc123 2', f'derived {work} abc123..def456']):
                self.assertEqual({'source': 'cache-delta', 'folder': str(work / '.omm')}, survey.omm_context(root, cache))

    def test_rejects_cache_inside_worktree(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            script = root / 'cache.mjs'
            script.touch()
            with patch.object(survey, '_cache_script', return_value=script), patch.object(survey.shutil, 'which', return_value='tool'), patch.object(survey, '_run') as run:
                self.assertEqual({'source': 'none', 'folder': None, 'reason': 'cache-inside-repo'}, survey.omm_context(root, root / 'cache'))
                run.assert_not_called()


def git(root, *args, input=None):
    return subprocess.run(['git', '-c', 'commit.gpgsign=false', '-c', 'user.name=t', '-c', 'user.email=t@t', *args], cwd=root, check=True,
                          capture_output=True, text=True, encoding='utf-8', input=input).stdout.strip()


class CommittedOmmTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / 'repo'
        (self.root / '.omm' / 'arch').mkdir(parents=True)
        (self.root / '.omm' / 'arch' / 'description.md').write_text('arch\n', encoding='utf-8')
        (self.root / 'a.py').write_text('print(1)\n', encoding='utf-8')
        (self.root / 'b.py').write_text('print(2)\nprint(3)\nprint(4)\n', encoding='utf-8')
        (self.root / 'c.py').write_text('gone\n', encoding='utf-8')
        git(self.root, 'init', '-q')
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-q', '-m', 'base')
        self.base = git(self.root, 'rev-parse', 'HEAD')

    def commit_code(self):
        (self.root / 'a.py').write_text('print(10)\n', encoding='utf-8')
        git(self.root, 'mv', 'b.py', 'moved.py')
        git(self.root, 'rm', '-q', 'c.py')
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-q', '-m', 'code')

    def test_unchanged_code_gives_committed_with_external_copy(self):
        with patch.object(survey, '_run') as run:
            out = survey.omm_context(self.root)
            run.assert_not_called()
        self.assertEqual('committed', out['source'])
        self.assertEqual(0, out['count'])
        self.assertEqual(self.base, out['commit'])
        folder = Path(out['folder'])
        self.assertTrue(folder.is_relative_to(self.root / '.git'))
        self.assertEqual('', git(self.root, 'status', '--porcelain', '--', '.omm'))
        common = Path(git(self.root, 'rev-parse', '--absolute-git-dir'))
        self.assertEqual(common / 'atelier' / 'omm' / self.base[:12] / '.omm', folder)
        self.assertEqual('arch\n', (folder / 'arch' / 'description.md').read_text(encoding='utf-8'))
        self.assertEqual('', Path(out['changes']).read_text(encoding='utf-8'))

    def test_later_code_changes_are_listed_with_status(self):
        self.commit_code()
        out = survey.omm_context(self.root)
        self.assertEqual(self.base, out['commit'])
        lines = Path(out['changes']).read_text(encoding='utf-8').splitlines()
        self.assertEqual(out['count'], len(lines))
        self.assertEqual(3, len(lines))
        self.assertTrue(any(l.startswith('M\t') and 'a.py' in l for l in lines))
        self.assertTrue(any(l.startswith('R') and 'moved.py' in l for l in lines))
        self.assertTrue(any(l.startswith('D\t') and 'c.py' in l for l in lines))
        self.assertFalse(any('.omm' in l for l in lines))

    def test_omm_changing_commit_is_the_base_and_omm_rows_are_excluded(self):
        (self.root / '.omm' / 'arch' / 'extra.md').write_text('x\n', encoding='utf-8')
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-q', '-m', 'omm')
        newer = git(self.root, 'rev-parse', 'HEAD')
        (self.root / 'a.py').write_text('print(11)\n', encoding='utf-8')
        git(self.root, 'commit', '-q', '-am', 'code')
        out = survey.omm_context(self.root)
        self.assertEqual(newer, out['commit'])
        self.assertEqual(1, out['count'])
        self.assertTrue((Path(out['folder']) / 'arch' / 'extra.md').is_file())

    def test_uncommitted_omm_edit_does_not_leak_and_worktree_is_untouched(self):
        (self.root / '.omm' / 'arch' / 'description.md').write_text('dirty\n', encoding='utf-8')
        before = git(self.root, 'status', '--porcelain', '--', '.omm')
        out = survey.omm_context(self.root)
        self.assertEqual('arch\n', (Path(out['folder']) / 'arch' / 'description.md').read_text(encoding='utf-8'))
        self.assertEqual('dirty\n', (self.root / '.omm' / 'arch' / 'description.md').read_text(encoding='utf-8'))
        self.assertEqual(before, git(self.root, 'status', '--porcelain', '--', '.omm'))

    def test_second_call_reuses_the_copy(self):
        first = survey.omm_context(self.root)
        marker = Path(first['folder']) / 'arch' / 'description.md'
        os.utime(marker, (1_000_000_000, 1_000_000_000))
        second = survey.omm_context(self.root)
        self.assertEqual(first['folder'], second['folder'])
        self.assertEqual(1_000_000_000, int(marker.stat().st_mtime))

    def test_fake_tools_and_cache_dir_still_give_committed(self):
        with patch.dict(os.environ, {'ATELIER_OMM_SCRIPT': str(self.root / 'missing.mjs')}):
            self.assertEqual('committed', survey.omm_context(self.root)['source'])
            self.assertEqual('committed', survey.omm_context(self.root, Path(self.tmp.name) / 'cache')['source'])

    def test_git_failure_after_detection_gives_committed_failed(self):
        with patch.object(survey, '_copy_omm', side_effect=subprocess.CalledProcessError(1, 'git')):
            self.assertEqual({'source': 'none', 'folder': None, 'reason': 'committed-failed'}, survey.omm_context(self.root))

    def test_symbolic_link_in_omm_gives_committed_failed_and_no_copy(self):
        target = git(self.root, 'hash-object', '-w', '--stdin', input='arch/description.md')
        git(self.root, 'update-index', '--add', '--cacheinfo', f'120000,{target},.omm/link.md')
        git(self.root, 'commit', '-q', '-m', 'link')
        head = git(self.root, 'rev-parse', 'HEAD')
        self.assertEqual({'source': 'none', 'folder': None, 'reason': 'committed-failed'}, survey.omm_context(self.root))
        common = Path(git(self.root, 'rev-parse', '--absolute-git-dir'))
        self.assertFalse((common / 'atelier' / 'omm' / head[:12]).exists())

    def test_shallow_boundary_is_not_taken_as_the_omm_commit(self):
        self.commit_code()
        clone = Path(self.tmp.name).resolve() / 'clone'
        git(Path(self.tmp.name), 'clone', '-q', '--depth', '1', self.root.as_uri(), str(clone))
        self.assertEqual({'source': 'none', 'folder': None, 'reason': 'committed-failed'}, survey.omm_context(clone))
        full = Path(self.tmp.name).resolve() / 'full'
        git(Path(self.tmp.name), 'clone', '-q', self.root.as_uri(), str(full))
        out = survey.omm_context(full)
        self.assertEqual((self.base, 3), (out['commit'], out['count']))

    def test_no_omm_in_head_uses_cache_flow(self):
        git(self.root, 'rm', '-rq', '.omm')
        git(self.root, 'commit', '-q', '-m', 'drop omm')
        with patch.object(survey, '_cache_script', return_value=None):
            self.assertEqual('tools', survey.omm_context(self.root)['reason'])

    def test_prompt_writes_four_committed_lines(self):
        self.commit_code()
        out = survey.omm_context(self.root)
        body = survey.survey_prompt(self.root, self.root / 'goal.txt', None, out).read_text(encoding='utf-8')
        self.assertIn('OMM source: committed\n', body)
        self.assertIn(f"OMM folder: {out['folder']}\n", body)
        self.assertIn(f"OMM commit: {self.base}\n", body)
        self.assertIn(f"OMM changes: {out['changes']} (3 files)\n", body)
        self.assertEqual(Path(out['changes']).parent, self.root / 'docs' / 'pbi' / 'prompts')
        self.assertNotIn('moved.py', body)
        other = survey.survey_prompt(self.root, self.root / 'goal.txt', None, {'source': 'none', 'folder': None}).read_text(encoding='utf-8')
        self.assertIn('OMM source: none\nOMM folder: none\n', other)
        self.assertNotIn('OMM commit:', other)
