import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parents[1] / 'setup.py'
FAKE = Path(__file__).parent / 'fakes' / 'cli.py'

class SetupTests(unittest.TestCase):
    def test_registration_and_fetch(self):
        with tempfile.TemporaryDirectory() as folder:
            env = dict(os.environ, FAKE_OUTPUT='{"models":[{"slug":"example","display_name":"Example","supported_reasoning_levels":[{"effort":"high"}]}]}')
            def run(*args):
                return subprocess.run([sys.executable, str(SETUP), *args, '--home', folder], env=env, capture_output=True, text=True, encoding='utf-8')
            result = run('runners', '--codex', str(FAKE))
            self.assertEqual(result.returncode, 0, result.stderr)
            registration = json.loads((Path(folder) / 'agents.json').read_text(encoding='utf-8'))
            self.assertEqual(registration['runners']['codex']['path'], str(FAKE))
            self.assertTrue(registration['runners']['agent']['enabled'])
            self.assertNotIn('roles', registration)
            result = run('models', 'codex')
            self.assertEqual(result.returncode, 0, result.stderr)
            models = json.loads((Path(folder) / 'models' / 'codex.json').read_text(encoding='utf-8'))
            self.assertEqual(models['models'][0]['id'], 'example')
            self.assertEqual(models['models'][0]['efforts'], ['high'])

    def test_failure_does_not_replace_models(self):
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run([sys.executable, str(SETUP), 'models', 'codex', '--home', folder], capture_output=True, env=dict(os.environ, PATH=''))
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((Path(folder) / 'models' / 'codex.json').exists())
