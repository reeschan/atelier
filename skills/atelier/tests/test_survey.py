import json
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

