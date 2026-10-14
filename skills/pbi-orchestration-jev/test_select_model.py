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
TWO = {'probabilities': {'0': 0.0, '1': 0.1, '2': 0.9}, 'confidence': 0.9}


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

    def test_haiku_before_deadline_starts_with_opencode(self):
        _, r = run(SMALL, ALL_ZERO, '--today', BEFORE)
        self.assertEqual(r['tier'], 'haiku')
        self.assertEqual(r['review'], 'normal')
        self.assertEqual(self.runners(r), [
            ('opencode', select_model.DEEPSEEK, None),
            ('codex', 'gpt-6-astra', 'medium'),
            ('agy', 'claude-sonnet-5-5-low', None),
            ('agent', 'haiku', 'max'),
        ])

    def test_missing_jev_answer_means_sonnet_starting_with_opencode(self):
        _, r = run(SMALL, None, '--today', BEFORE)
        self.assertEqual(r['tier'], 'sonnet')
        self.assertEqual([s['runner'] for s in r['route']], ['opencode', 'codex', 'agy', 'agent'])
        self.assertEqual(r['route'][1]['effort'], 'high')

    def test_after_deadline_drops_codex(self):
        _, r = run(SMALL, ALL_ZERO, '--today', AFTER)
        self.assertEqual([s['runner'] for s in r['route']], ['opencode', 'agy', 'agent'])

    def test_sensitive_is_sonnet_agent_only_with_deep_review(self):
        _, r = run({**SMALL, 'sensitive': True}, ALL_ZERO, '--today', BEFORE)
        self.assertEqual(r['tier'], 'sonnet')
        self.assertEqual(r['review'], 'deep')
        self.assertEqual(self.runners(r), [('agent', 'sonnet', 'high')])

    def test_coupling_two_is_sonnet_with_deep_review(self):
        _, r = run(SMALL, dict(ALL_ZERO, coupling=TWO), '--today', AFTER)
        self.assertEqual(r['tier'], 'sonnet')
        self.assertEqual(r['review'], 'deep')
        self.assertEqual(r['route'][0]['runner'], 'opencode')

    def test_novelty_and_ambiguity_two_is_opus_agent_only(self):
        _, r = run(SMALL, dict(ALL_ZERO, novelty=TWO, ambiguity=TWO), '--today', BEFORE)
        self.assertEqual(r['tier'], 'opus')
        self.assertEqual(self.runners(r), [('agent', 'opus', None)])

    def test_many_files_is_sonnet(self):
        _, r = run({**SMALL, 'files_changed': 4}, ALL_ZERO, '--today', AFTER)
        self.assertEqual(r['tier'], 'sonnet')

    def test_low_confidence_lowers_one_step(self):
        answers = dict(ALL_ZERO, coupling={'probabilities': {'0': 0.1, '1': 0.2, '2': 0.7}, 'confidence': 0.5})
        _, r = run(SMALL, answers, '--today', AFTER)
        self.assertEqual(r['scores']['coupling'], 1)
        self.assertEqual(r['tier'], 'sonnet')
        self.assertEqual(r['review'], 'normal')

    def test_skip_removes_runners_but_keeps_agent(self):
        _, r = run(SMALL, ALL_ZERO, '--today', BEFORE, '--skip', 'opencode,agy')
        self.assertEqual([s['runner'] for s in r['route']], ['codex', 'agent'])

    def test_forced_tier_with_skip_opencode(self):
        _, r = run(SMALL, ALL_ZERO, '--today', BEFORE, '--tier', 'sonnet', '--skip', 'opencode')
        self.assertEqual(r['tier'], 'sonnet')
        self.assertEqual(r['route'][0], {'runner': 'codex', 'model': 'gpt-6-astra', 'effort': 'high'})

    def test_invalid_tier_fails(self):
        code, _ = run(SMALL, ALL_ZERO, '--tier', 'xx')
        self.assertNotEqual(code, 0)

    def test_invalid_skip_fails(self):
        code, _ = run(SMALL, ALL_ZERO, '--skip', 'agent')
        self.assertNotEqual(code, 0)

    def test_all_writes_route_files_and_prints_table(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, 'jev'))
            for pbi, facts in [('PBI-001', SMALL), ('PBI-002', {**SMALL, 'sensitive': True})]:
                with open(os.path.join(d, f'{pbi}.facts.json'), 'w', encoding='utf-8') as f:
                    json.dump(facts, f)
                with open(os.path.join(d, 'jev', f'{pbi}.brief.json'), 'w', encoding='utf-8') as f:
                    json.dump({'answers': ALL_ZERO}, f)
            out = subprocess.run([sys.executable, SCRIPT, '--all', d, '--today', AFTER],
                                 capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(out.returncode, 0)
            rows = [line for line in out.stdout.splitlines() if line.startswith('| PBI-')]
            self.assertEqual(len(rows), 2)
            with open(os.path.join(d, 'PBI-002.route.json'), encoding='utf-8') as f:
                r2 = json.load(f)
            self.assertEqual(r2['review'], 'deep')
            self.assertEqual([s['runner'] for s in r2['route']], ['agent'])


PERF = {'name': 'performance', 'tiers': {
    'haiku': [{'runner': 'agy', 'model': 'claude-sonnet-5-5-high', 'effort': None},
              {'runner': 'agent', 'model': 'sonnet', 'effort': 'high'}],
    'sonnet': [{'runner': 'codex', 'model': 'gpt-6-astra', 'effort': 'high', 'until': BEFORE},
               {'runner': 'agent', 'model': 'opus', 'effort': None}],
    'opus': [{'runner': 'agent', 'model': 'opus', 'effort': None}],
}}


class RoutingTemplateTest(unittest.TestCase):
    def write(self, d, name, table):
        path = os.path.join(d, name)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(table, f)
        return path

    def test_default_file_matches_builtin_table(self):
        with open(os.path.join(HERE, 'routing.default.json'), encoding='utf-8') as f:
            table = json.load(f)
        self.assertEqual(table['tiers'], select_model.default_routing()['tiers'])

    def test_routing_option_replaces_route(self):
        with tempfile.TemporaryDirectory() as d:
            _, r = run(SMALL, ALL_ZERO, '--today', BEFORE, '--routing', self.write(d, 'r.json', PERF))
        self.assertEqual(r['tier'], 'haiku')
        self.assertEqual([s['runner'] for s in r['route']], ['agy', 'agent'])
        self.assertIn('performance', r['reasons'][0])

    def test_template_until_drops_step(self):
        with tempfile.TemporaryDirectory() as d:
            _, r = run(SMALL, None, '--today', AFTER, '--routing', self.write(d, 'r.json', PERF))
        self.assertEqual(r['route'], [{'runner': 'agent', 'model': 'opus', 'effort': None}])

    def test_sensitive_uses_template_agent_only(self):
        with tempfile.TemporaryDirectory() as d:
            _, r = run({**SMALL, 'sensitive': True}, ALL_ZERO, '--today', BEFORE,
                       '--routing', self.write(d, 'r.json', PERF))
        self.assertEqual([s['runner'] for s in r['route']], ['agent'])

    def test_template_not_ending_with_agent_fails(self):
        bad = {'tiers': dict(PERF['tiers'], opus=[{'runner': 'codex', 'model': 'x'}])}
        with tempfile.TemporaryDirectory() as d:
            code, _ = run(SMALL, ALL_ZERO, '--routing', self.write(d, 'r.json', bad))
        self.assertNotEqual(code, 0)

    def test_agent_without_model_or_bad_until_fails(self):
        no_model = {'tiers': dict(PERF['tiers'], opus=[{'runner': 'agent'}])}
        bad_until = {'tiers': dict(PERF['tiers'], sonnet=[{'runner': 'codex', 'model': 'x', 'until': 'soon'},
                                                          {'runner': 'agent', 'model': 'sonnet'}])}
        for table in (no_model, bad_until):
            with tempfile.TemporaryDirectory() as d:
                code, _ = run(SMALL, ALL_ZERO, '--routing', self.write(d, 'r.json', table))
            self.assertNotEqual(code, 0)

    def test_all_picks_routing_json_in_dir(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d, 'routing.json', PERF)
            self.write(d, 'PBI-001.facts.json', SMALL)
            out = subprocess.run([sys.executable, SCRIPT, '--all', d, '--today', BEFORE],
                                 capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(out.returncode, 0, out.stderr)
            with open(os.path.join(d, 'PBI-001.route.json'), encoding='utf-8') as f:
                r = json.load(f)
        self.assertEqual(r['tier'], 'sonnet')
        self.assertEqual([s['runner'] for s in r['route']], ['codex', 'agent'])


if __name__ == '__main__':
    unittest.main()
