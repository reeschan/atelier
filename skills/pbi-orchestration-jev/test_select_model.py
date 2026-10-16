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
NOW = '2026-10-20T09:00:00+09:00'
PAST = '2026-10-23T09:00:00+09:00'
UNTIL = '2026-10-22T23:59:00+09:00'
FLASH = [{'runner': 'opencode', 'model': select_model.DEEPSEEK, 'effort': 'max'}]
ALL_TIERS = {'name': 'deepseek-all', 'description': '全段に当てる', 'tiers': {t: FLASH for t in select_model.TIERS}}
HAIKU_ONLY = {'name': 'light-only', 'description': 'haiku だけ', 'tiers': {'haiku': FLASH}}


def empty_home_env(d):
    """PBI_TEMPLATES_HOME を d の下の空のディレクトリにした環境。--home を渡さない実行が、
    版配置 (versions/<版>/) でスキルのホームの overrides.json を読まないようにする。"""
    home = os.path.join(d, 'empty-home')
    os.makedirs(home, exist_ok=True)
    return {**os.environ, 'PBI_TEMPLATES_HOME': home}


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
        out = subprocess.run(args + list(extra), capture_output=True, text=True, encoding='utf-8',
                             errors='replace', env=empty_home_env(d))
        return out.returncode, (json.loads(out.stdout) if out.returncode == 0 else None)


def application(name='deepseek-all', until=UNTIL, on_expire='ask'):
    return {'id': 'a-20261016T090000', 'template': name, 'until': until, 'on_expire': on_expire,
            'reason': '一時措置', 'started_at': '2026-10-16T09:00:00+09:00', 'started_via': 'chat'}


def write_home(d, templates_, app):
    os.makedirs(os.path.join(d, 'templates'), exist_ok=True)
    for name, template in templates_.items():
        with open(os.path.join(d, 'templates', f'{name}.json'), 'w', encoding='utf-8') as f:
            json.dump(template, f)
    if app is not None:
        with open(os.path.join(d, 'overrides.json'), 'w', encoding='utf-8') as f:
            json.dump({'synced_at': '2026-10-16T09:00:00+09:00', 'application': app}, f)


def run_home(facts, answers, templates_, app, *extra):
    with tempfile.TemporaryDirectory() as d:
        write_home(d, templates_, app)
        return run(facts, answers, '--home', d, *extra)


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
                                 capture_output=True, text=True, encoding='utf-8', env=empty_home_env(d))
            self.assertEqual(out.returncode, 0)
            rows = [line for line in out.stdout.splitlines() if line.startswith('| PBI-')]
            self.assertEqual(len(rows), 2)
            with open(os.path.join(d, 'PBI-002.route.json'), encoding='utf-8') as f:
                r2 = json.load(f)
            self.assertEqual(r2['review'], 'deep')
            self.assertEqual([s['runner'] for s in r2['route']], ['agent'])

    def test_template_applies_to_every_tier(self):
        for tier, agent in select_model.AGENT.items():
            with self.subTest(tier=tier):
                code, r = run_home(SMALL, ALL_ZERO, {'deepseek-all': ALL_TIERS}, application(),
                                   '--now', NOW, '--tier', tier)
                self.assertEqual(code, 0)
                self.assertTrue(r['template']['applied'])
                self.assertEqual(r['template']['name'], 'deepseek-all')
                self.assertEqual(r['template']['note'], f'deepseek-all を当てた ({UNTIL} まで)')
                self.assertEqual(self.runners(r), [
                    ('opencode', select_model.DEEPSEEK, 'max'),
                    (agent['runner'], agent['model'], agent['effort']),
                ])
                self.assertTrue(r['reasons'][0].endswith('（テンプレート deepseek-all を当てた）'))

    def test_template_without_that_tier_keeps_normal_route(self):
        code, r = run_home({**SMALL, 'files_changed': 4}, ALL_ZERO, {'light-only': HAIKU_ONLY},
                           application('light-only'), '--now', NOW)
        self.assertEqual(code, 0)
        self.assertEqual(r['tier'], 'sonnet')
        self.assertFalse(r['template']['applied'])
        self.assertEqual(r['template']['note'], 'この段の上書きなし')
        self.assertEqual([s['runner'] for s in r['route']], ['opencode', 'codex', 'agy', 'agent'])

    def test_sensitive_keeps_agent_sonnet_only(self):
        code, r = run_home({**SMALL, 'sensitive': True}, ALL_ZERO, {'deepseek-all': ALL_TIERS},
                           application(), '--now', NOW)
        self.assertEqual(code, 0)
        self.assertFalse(r['template']['applied'])
        self.assertEqual(r['template']['note'], 'sensitive のため当てない')
        self.assertEqual(self.runners(r), [('agent', 'sonnet', 'high')])

    def test_expired_ask_is_not_applied_and_asks_the_user(self):
        code, r = run_home(SMALL, ALL_ZERO, {'deepseek-all': ALL_TIERS}, application(), '--now', PAST)
        self.assertEqual(code, 0)
        self.assertFalse(r['template']['applied'])
        self.assertEqual(r['template']['name'], 'deepseek-all')
        self.assertEqual(r['template']['note'], '期限切れ: 延長・やめる・期限なしの確認が要る')
        self.assertEqual(r['route'][0], select_model.OPENCODE['haiku'])

    def test_expired_drop_is_not_applied(self):
        code, r = run_home(SMALL, ALL_ZERO, {'deepseek-all': ALL_TIERS}, application(on_expire='drop'),
                           '--now', PAST)
        self.assertEqual(code, 0)
        self.assertFalse(r['template']['applied'])
        self.assertEqual(r['template']['note'], '期限切れ (drop) のため当てない')
        self.assertEqual(r['route'][0], select_model.OPENCODE['haiku'])

    def test_until_null_applies_without_deadline(self):
        code, r = run_home(SMALL, ALL_ZERO, {'deepseek-all': ALL_TIERS}, application(until=None),
                           '--now', PAST)
        self.assertEqual(code, 0)
        self.assertTrue(r['template']['applied'])
        self.assertEqual(r['template']['note'], '期限なしで当てた')
        self.assertEqual(self.runners(r), [
            ('opencode', select_model.DEEPSEEK, 'max'),
            ('agent', 'haiku', 'max'),
        ])

    def test_skip_opencode_leaves_only_agent(self):
        code, r = run_home(SMALL, ALL_ZERO, {'deepseek-all': ALL_TIERS}, application(),
                           '--now', NOW, '--skip', 'opencode')
        self.assertEqual(code, 0)
        self.assertTrue(r['template']['applied'])
        self.assertEqual(self.runners(r), [('agent', 'haiku', 'max')])

    def test_missing_template_is_not_applied(self):
        code, r = run_home(SMALL, ALL_ZERO, {}, application('ghost'), '--now', NOW)
        self.assertEqual(code, 0)
        self.assertFalse(r['template']['applied'])
        self.assertEqual(r['template']['name'], 'ghost')
        self.assertEqual(r['template']['note'], 'テンプレートが見つからないか不正')
        self.assertEqual([s['runner'] for s in r['route']], ['opencode', 'codex', 'agy', 'agent'])

    def test_without_overrides_keeps_the_same_route(self):
        code, r = run_home(SMALL, ALL_ZERO, {'deepseek-all': ALL_TIERS}, None, '--now', NOW)
        self.assertEqual(code, 0)
        self.assertFalse(r['template']['applied'])
        self.assertIsNone(r['template']['name'])
        self.assertEqual(r['template']['note'], '適用なし')
        _, plain = run(SMALL, ALL_ZERO, '--today', BEFORE)
        self.assertEqual(self.runners(r), self.runners(plain))

    def test_today_and_now_are_used_separately(self):
        code, r = run_home(SMALL, ALL_ZERO, {'deepseek-all': ALL_TIERS}, application(),
                           '--today', AFTER, '--now', NOW)
        self.assertEqual(code, 0)
        self.assertTrue(r['template']['applied'])
        code, r = run_home(SMALL, ALL_ZERO, {'deepseek-all': ALL_TIERS}, application(),
                           '--today', BEFORE, '--now', PAST)
        self.assertEqual(code, 0)
        self.assertFalse(r['template']['applied'])
        self.assertEqual(r['template']['note'], '期限切れ: 延長・やめる・期限なしの確認が要る')

    def test_all_table_has_template_column(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, 'jev'))
            home = os.path.join(d, 'home')
            write_home(home, {'deepseek-all': ALL_TIERS}, application())
            for pbi, facts in [('PBI-001', SMALL), ('PBI-002', {**SMALL, 'sensitive': True})]:
                with open(os.path.join(d, f'{pbi}.facts.json'), 'w', encoding='utf-8') as f:
                    json.dump(facts, f)
                with open(os.path.join(d, 'jev', f'{pbi}.brief.json'), 'w', encoding='utf-8') as f:
                    json.dump({'answers': ALL_ZERO}, f)
            out = subprocess.run([sys.executable, SCRIPT, '--all', d, '--now', NOW, '--home', home],
                                 capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(out.returncode, 0)
            lines = out.stdout.splitlines()
            self.assertIn('| テンプレート |', lines[0])
            rows = [line for line in lines if line.startswith('| PBI-')]
            self.assertEqual(len(rows), 2)
            self.assertIn(f'deepseek-all を当てた ({UNTIL} まで)', rows[0])
            self.assertIn('sensitive のため当てない', rows[1])
            with open(os.path.join(d, 'PBI-001.route.json'), encoding='utf-8') as f:
                self.assertTrue(json.load(f)['template']['applied'])


if __name__ == '__main__':
    unittest.main()
