import datetime
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'select_model.py')
sys.path.insert(0, HERE)
import select_model  # noqa: E402

BEFORE = select_model.CODEX_UNTIL.isoformat()
AFTER = (select_model.CODEX_UNTIL + datetime.timedelta(days=1)).isoformat()
ZERO = {'probabilities': {'0': 0.9, '1': 0.1, '2': 0.0}, 'confidence': 0.9}


def run(facts, answers=None, *extra):
    with tempfile.TemporaryDirectory() as d:
        fp = os.path.join(d, 'facts.json')
        with open(fp, 'w', encoding='utf-8') as f:
            json.dump(facts, f)
        args = [sys.executable, SCRIPT, fp]
        if answers is not None:
            bp = os.path.join(d, 'brief.json')
            with open(bp, 'w', encoding='utf-8') as f:
                json.dump({'answers': answers}, f)
            args.append(bp)
        out = subprocess.run(args + list(extra), capture_output=True, text=True, encoding='utf-8')
        return out.returncode, (json.loads(out.stdout) if out.returncode == 0 else None)


SMALL = {'files_changed': 1, 'modules': 1, 'sensitive': False}
ALL_ZERO = {k: ZERO for k in select_model.KEYS}


class SelectModelTest(unittest.TestCase):
    def runners(self, result):
        return [(s['runner'], s['model'], s['effort']) for s in result['route']]

    def test_haiku_before_deadline_starts_with_codex_medium(self):
        _, r = run(SMALL, ALL_ZERO, '--today', BEFORE)
        self.assertEqual(r['tier'], 'haiku')
        self.assertEqual(self.runners(r), [
            ('codex', 'gpt-6-astra', 'medium'),
            ('agy', 'claude-sonnet-5-5-low', None),
            ('agent', 'haiku', 'max'),
        ])

    def test_missing_jev_answer_means_sonnet_with_codex_high(self):
        _, r = run(SMALL, None, '--today', BEFORE)
        self.assertEqual(r['tier'], 'sonnet')
        self.assertEqual(r['route'][0], {'runner': 'codex', 'model': 'gpt-6-astra', 'effort': 'high'})

    def test_after_deadline_drops_codex(self):
        _, r = run(SMALL, ALL_ZERO, '--today', AFTER)
        self.assertEqual([s['runner'] for s in r['route']], ['agy', 'agent'])

    def test_sensitive_is_opus_without_codex(self):
        _, r = run({**SMALL, 'sensitive': True}, ALL_ZERO, '--today', BEFORE)
        self.assertEqual(r['tier'], 'opus')
        self.assertEqual(self.runners(r), [
            ('agy', 'claude-opus-5-5-high', None),
            ('agent', 'opus', None),
        ])

    def test_many_files_is_sonnet(self):
        _, r = run({**SMALL, 'files_changed': 4}, ALL_ZERO, '--today', AFTER)
        self.assertEqual(r['tier'], 'sonnet')

    def test_low_confidence_lowers_one_step(self):
        answers = dict(ALL_ZERO, coupling={'probabilities': {'0': 0.1, '1': 0.2, '2': 0.7}, 'confidence': 0.5})
        _, r = run(SMALL, answers, '--today', AFTER)
        self.assertEqual(r['scores']['coupling'], 1)
        self.assertEqual(r['tier'], 'sonnet')

    def test_forced_tier(self):
        _, r = run(SMALL, ALL_ZERO, '--today', BEFORE, '--tier', 'sonnet')
        self.assertEqual(r['tier'], 'sonnet')
        self.assertEqual(r['route'][0]['effort'], 'high')

    def test_invalid_tier_fails(self):
        code, _ = run(SMALL, ALL_ZERO, '--tier', 'xx')
        self.assertNotEqual(code, 0)


if __name__ == '__main__':
    unittest.main()
