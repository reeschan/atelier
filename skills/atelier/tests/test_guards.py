import subprocess
import tempfile
import unittest
from pathlib import Path
from atelier import guards


class GuardTests(unittest.TestCase):
    def test_sends_require_approved_pbi_runner_pair(self):
        run = {'approvals': {'plan': True, 'plan_sends': {'PBI-1': ['codex']}}}
        self.assertTrue(guards.sends_approved(run, 'PBI-1', {'runner': 'codex'}))
        self.assertFalse(guards.sends_approved(run, 'PBI-2', {'runner': 'codex'}))
        self.assertFalse(guards.sends_approved(run, 'PBI-1', {'runner': 'agy'}))
        run['approvals']['plan'] = False
        self.assertFalse(guards.sends_approved(run, 'PBI-1', {'runner': 'codex'}))
        self.assertTrue(guards.sends_approved(run, 'PBI-1', {'runner': 'agent'}))

    def test_secrets_are_detected_without_values(self):
        values = ['sk-' + 'a' * 32, 'ghp_' + 'B' * 36, 'hunter2-secret', 'private material']
        text = f'{values[0]}\n{values[1]}\nDB_PASSWORD={values[2]}\n-----BEGIN PRIVATE KEY-----\n{values[3]}\n-----END PRIVATE KEY-----'
        findings = guards.scan_secrets(text)
        self.assertGreaterEqual(len(findings), 4)
        for value in values:
            self.assertNotIn(value, repr(findings))
        self.assertEqual(guards.scan_secrets('PORT=1234\nhello world'), [])

    def test_dependency_snapshot_changes(self):
        before = {'files': {'package-lock.json': 'a', 'src/x': 'a'}}
        after = {'files': {'package-lock.json': 'b', 'requirements.txt': 'c', 'src/x': 'b'}}
        self.assertEqual(set(guards.install_attempts(before, after)), {'package-lock.json', 'requirements.txt'})
        self.assertEqual(guards.install_attempts(before, before), [])

    def test_component_forbidden_arguments(self):
        component = {'forbidden_args': ['--paid', '--unsafe']}
        self.assertEqual(guards.forbidden_args(component, ['cmd', '--paid=yes', '--safe']), ['--paid'])
        self.assertEqual(guards.forbidden_args(component, ['--paidness']), [])

    def test_diff_deletions_migrations_and_schema(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            def git(*args):
                return subprocess.run(['git', '-C', d, *args], check=True, capture_output=True).stdout.decode().strip()
            git('init'); git('config', 'user.email', 'test@example.com'); git('config', 'user.name', 'Test')
            (root / 'old.txt').write_text('x')
            (root / 'db.sql').write_text('select 1;\n')
            git('add', '.'); git('commit', '-m', 'base')
            base = git('rev-parse', 'HEAD')
            (root / 'old.txt').unlink()
            (root / 'db.sql').write_text('CREATE TABLE foo (id int);\n')
            (root / 'migrations').mkdir(); (root / 'migrations' / '001.sql').write_text('x')
            findings = guards.destructive_changes(root, base)
            self.assertIn({'kind': 'deleted', 'path': 'old.txt'}, findings)
            self.assertIn({'kind': 'migration', 'path': 'migrations/001.sql'}, findings)
            self.assertIn({'kind': 'schema', 'path': 'db.sql'}, findings)
            with self.assertRaises(RuntimeError):
                guards.destructive_changes(root, 'invalid-ref')
