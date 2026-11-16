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

    def test_garbled_changes_detects_fffd_q_runs_and_cjk_split_ignoring_base_binary_and_fakes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            def git(*args):
                return subprocess.run(['git', '-C', d, *args], check=True, capture_output=True).stdout.decode().strip()
            git('init'); git('config', 'user.email', 'test@example.com'); git('config', 'user.name', 'Test')
            (root / 'README.md').write_text('# 見出し\n調べる → 詰める\n', encoding='utf-8')
            (root / 'base_q.txt').write_text('keep = "???"\nok = 1\n', encoding='utf-8')
            (root / 'bin.dat').write_bytes(b'\x00\x01\x02')
            git('add', '.'); git('commit', '-m', 'base')
            base = git('rev-parse', 'HEAD')
            before = guards.snapshot(root)

            (root / 'README.md').write_text('# ??????\n調べる?詰める\n', encoding='utf-8')
            (root / 'added.txt').write_text('壊れ\ufffd文字\n', encoding='utf-8')
            (root / 'base_q.txt').write_text('keep = "???"  # unchanged run\nok = 2\n', encoding='utf-8')
            (root / 'bin.dat').write_bytes(b'\x00???\xef\xbf\xbd')
            (root / 'tests' / 'fakes').mkdir(parents=True)
            (root / 'tests' / 'fakes' / 'fake.txt').write_text('## ??????\nあ?い\n\ufffd\n', encoding='utf-8')

            findings = guards.garbled_changes(root, before, base=base)
            by_path = {(f['path'], f['line']): f for f in findings}
            self.assertIn(('README.md', 1), by_path)
            self.assertIn(('README.md', 2), by_path)
            self.assertIn(('added.txt', 1), by_path)
            self.assertTrue(all(f['path'] not in ('base_q.txt', 'bin.dat', 'tests/fakes/fake.txt') for f in findings))
