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
                self.assertTrue(r['reasons'][0].endswith('（優先テンプレート deepseek-all を当てた）'))

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
        self.assertEqual(r['template']['note'], 'deepseek-all を期限なしで当てた')
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
            self.assertIn('| 優先テンプレート |', lines[0])
            rows = [line for line in lines if line.startswith('| PBI-')]
            self.assertEqual(len(rows), 2)
            self.assertIn(f'deepseek-all を当てた ({UNTIL} まで)', rows[0])
            self.assertIn('sensitive のため当てない', rows[1])
            with open(os.path.join(d, 'PBI-001.route.json'), encoding='utf-8') as f:
                self.assertTrue(json.load(f)['template']['applied'])


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
        list_model = {'tiers': dict(PERF['tiers'], opus=[{'runner': 'agent', 'model': ['opus']}])}
        for table in (no_model, bad_until, list_model):
            with tempfile.TemporaryDirectory() as d:
                code, _ = run(SMALL, ALL_ZERO, '--routing', self.write(d, 'r.json', table))
            self.assertNotEqual(code, 0)

    def test_all_picks_routing_json_in_dir(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d, 'routing.json', PERF)
            self.write(d, 'PBI-001.facts.json', SMALL)
            out = subprocess.run([sys.executable, SCRIPT, '--all', d, '--today', BEFORE],
                                 capture_output=True, text=True, encoding='utf-8', env=empty_home_env(d))
            self.assertEqual(out.returncode, 0, out.stderr)
            with open(os.path.join(d, 'PBI-001.route.json'), encoding='utf-8') as f:
                r = json.load(f)
        self.assertEqual(r['tier'], 'sonnet')
        self.assertEqual([s['runner'] for s in r['route']], ['codex', 'agent'])


class RoutingHomeTest(unittest.TestCase):
    """共通の routing.json は優先テンプレートと同じ home (templates.resolve_home) から探す。"""

    def write(self, d, table):
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'routing.json'), 'w', encoding='utf-8') as f:
            json.dump(table, f)

    def runners(self, r):
        return [s['runner'] for s in r['route']]

    def test_home_option_routing_json_is_used(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d, PERF)
            code, r = run(SMALL, ALL_ZERO, '--today', BEFORE, '--home', d)
        self.assertEqual(code, 0)
        self.assertEqual(self.runners(r), ['agy', 'agent'])
        self.assertIn('performance', r['reasons'][0])

    def test_env_home_routing_json_is_used(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d, PERF)
            fp = os.path.join(d, 'facts.json')
            with open(fp, 'w', encoding='utf-8') as f:
                json.dump(SMALL, f)
            out = subprocess.run([sys.executable, SCRIPT, fp, '--today', BEFORE], capture_output=True, text=True,
                                 encoding='utf-8', env={**os.environ, 'PBI_TEMPLATES_HOME': d})
        self.assertEqual(out.returncode, 0, out.stderr)
        # Jev の答えが無いので sonnet の段。PERF の sonnet は codex → agent
        self.assertEqual(self.runners(json.loads(out.stdout)), ['codex', 'agent'])

    def test_all_dir_routing_json_wins_over_home(self):
        dir_table = {'name': 'dir', 'tiers': dict(PERF['tiers'], sonnet=[{'runner': 'agent', 'model': 'sonnet'}])}
        with tempfile.TemporaryDirectory() as d:
            home, pbi = os.path.join(d, 'home'), os.path.join(d, 'pbi')
            self.write(home, PERF)
            self.write(pbi, dir_table)
            with open(os.path.join(pbi, 'PBI-001.facts.json'), 'w', encoding='utf-8') as f:
                json.dump({**SMALL, 'files_changed': 4}, f)
            out = subprocess.run([sys.executable, SCRIPT, '--all', pbi, '--today', BEFORE, '--home', home],
                                 capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(out.returncode, 0, out.stderr)
            with open(os.path.join(pbi, 'PBI-001.route.json'), encoding='utf-8') as f:
                r = json.load(f)
        self.assertEqual(self.runners(r), ['agent'])

    def run_copied(self, script_dir, facts_dir):
        """select_model.py と templates.py を script_dir に写し、--home も PBI_TEMPLATES_HOME も無しで動かす。
        Jev の答えを渡さないので sonnet の段になる。"""
        os.makedirs(script_dir, exist_ok=True)
        for name in ('select_model.py', 'templates.py'):
            with open(os.path.join(HERE, name), encoding='utf-8') as src,                     open(os.path.join(script_dir, name), 'w', encoding='utf-8') as dst:
                dst.write(src.read())
        fp = os.path.join(facts_dir, 'facts.json')
        with open(fp, 'w', encoding='utf-8') as f:
            json.dump(SMALL, f)
        env = {k: v for k, v in os.environ.items() if k != 'PBI_TEMPLATES_HOME'}
        out = subprocess.run([sys.executable, os.path.join(script_dir, 'select_model.py'), fp, '--today', BEFORE],
                             capture_output=True, text=True, encoding='utf-8', env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_versioned_layout_reads_skill_root(self):
        """版管理の配置 (versions/<版>/) では、版のディレクトリでなくスキルのルートの routing.json を読む。"""
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, 'skill')
            self.write(root, PERF)
            self.write(os.path.join(root, 'versions', 'v1'), {'name': 'version-dir', 'tiers': {
                t: [{'runner': 'agent', 'model': 'opus'}] for t in select_model.TIERS}})
            r = self.run_copied(os.path.join(root, 'versions', 'v1'), d)
        self.assertEqual(self.runners(r), ['codex', 'agent'])
        self.assertIn('performance', r['reasons'][0])

    def test_flat_layout_reads_script_dir(self):
        """版の無い配置 (公開リポジトリ) では、スクリプトのディレクトリの routing.json を読む。"""
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, 'skill')
            self.write(root, PERF)
            r = self.run_copied(root, d)
        self.assertEqual(self.runners(r), ['codex', 'agent'])
        self.assertIn('performance', r['reasons'][0])


class PriorityOverRoutingTest(unittest.TestCase):
    """優先テンプレートは振り分けテンプレート (routing) より先に当たり、最後は routing のその段の agent。"""

    def test_priority_template_wins_and_falls_back_to_routing_agent(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'routing.json')
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(PERF, f)
            code, r = run_home({**SMALL, 'files_changed': 4}, ALL_ZERO, {'deepseek-all': ALL_TIERS},
                               application(), '--now', NOW, '--routing', path)
        self.assertEqual(code, 0)
        self.assertEqual(r['tier'], 'sonnet')
        self.assertTrue(r['template']['applied'])
        self.assertEqual([(s['runner'], s['model']) for s in r['route']],
                         [('opencode', select_model.DEEPSEEK), ('agent', 'opus')])

    def test_routing_is_used_when_no_application(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'routing.json')
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(PERF, f)
            code, r = run_home({**SMALL, 'files_changed': 4}, ALL_ZERO, {'deepseek-all': ALL_TIERS},
                               None, '--now', NOW, '--today', BEFORE, '--routing', path)
        self.assertEqual(code, 0)
        self.assertFalse(r['template']['applied'])
        self.assertEqual([s['runner'] for s in r['route']], ['codex', 'agent'])

    def test_priority_step_past_its_until_is_dropped(self):
        dated = {'name': 'dated', 'description': '', 'tiers': {'haiku': [
            {'runner': 'codex', 'model': 'gpt-6-astra', 'effort': 'medium', 'until': '2026-10-19'},
            {'runner': 'opencode', 'model': select_model.DEEPSEEK, 'effort': 'max'}]}}
        code, r = run_home(SMALL, ALL_ZERO, {'dated': dated}, application('dated'), '--now', NOW)
        self.assertEqual(code, 0)
        self.assertTrue(r['template']['applied'])
        self.assertEqual([s['runner'] for s in r['route']], ['opencode', 'agent'])


DS_FREE = {'runner': 'opencode', 'model': 'opencode/deepseek-v4.1-flash-free', 'effort': 'max'}
GLM_FREE = {'runner': 'opencode', 'model': 'opencode/glm-5.3-flash-free', 'effort': 'max'}
DS_GO = {'runner': 'opencode', 'model': 'opencode-go/deepseek-v4.1-flash', 'effort': 'max'}
GLM_GO = {'runner': 'opencode', 'model': 'opencode-go/glm-5.3-flash', 'effort': 'max'}
ROTATE_STEPS = [{'rotate': [DS_FREE, GLM_FREE]}, {'rotate': [DS_GO, GLM_GO]}]
FLASH_ROTATE = {'name': 'flash-rotate', 'description': '交互', 'tiers': {t: ROTATE_STEPS for t in select_model.TIERS}}


class RotateTest(unittest.TestCase):
    """交互グループは PBI の番号 mod メンバー数 を先頭にして回し、残りは控えとして後ろに並ぶ。"""

    def run_pbi(self, pbi, templates_=None, *extra, facts=SMALL):
        """facts を <pbi>.facts.json に書いて単体で判定する。pbi が None なら facts.json。"""
        with tempfile.TemporaryDirectory() as d:
            home = os.path.join(d, 'home')
            name = next(iter(templates_)) if templates_ else 'flash-rotate'
            write_home(home, templates_ or {'flash-rotate': FLASH_ROTATE}, application(name))
            fp = os.path.join(d, f'{pbi}.facts.json' if pbi else 'facts.json')
            with open(fp, 'w', encoding='utf-8') as f:
                json.dump(facts, f)
            bp = os.path.join(d, 'brief.json')
            with open(bp, 'w', encoding='utf-8') as f:
                json.dump({'answers': ALL_ZERO}, f)
            out = subprocess.run([sys.executable, SCRIPT, fp, bp, '--home', home, '--now', NOW, *extra],
                                 capture_output=True, text=True, encoding='utf-8', errors='replace',
                                 env=empty_home_env(d))
            return out.returncode, (json.loads(out.stdout) if out.returncode == 0 else None)

    def models(self, r):
        return [s['model'] for s in r['route']]

    def test_odd_and_even_pbi_swap_the_head(self):
        code, odd = self.run_pbi('PBI-007')
        self.assertEqual(code, 0)
        self.assertTrue(odd['template']['applied'])
        self.assertEqual(self.models(odd), [GLM_FREE['model'], DS_FREE['model'], GLM_GO['model'], DS_GO['model'],
                                            'haiku'])
        code, even = self.run_pbi('PBI-008')
        self.assertEqual(code, 0)
        self.assertEqual(self.models(even), [DS_FREE['model'], GLM_FREE['model'], DS_GO['model'], GLM_GO['model'],
                                             'haiku'])
        self.assertEqual(even['route'][0], DS_FREE)

    def test_three_members_rotate_evenly(self):
        a, b, c = ({'runner': 'opencode', 'model': f'opencode/m{i}', 'effort': None} for i in range(3))
        three = {'name': 'three', 'description': '', 'tiers': {'haiku': [{'rotate': [a, b, c]}]}}
        heads = []
        for n in (3, 4, 5):
            code, r = self.run_pbi(f'PBI-{n:03d}', {'three': three})
            self.assertEqual(code, 0)
            heads.append(r['route'][0]['model'])
        self.assertEqual(heads, ['opencode/m0', 'opencode/m1', 'opencode/m2'])
        code, r = self.run_pbi('PBI-004', {'three': three})
        self.assertEqual(self.models(r), ['opencode/m1', 'opencode/m2', 'opencode/m0', 'haiku'])

    def test_pbi_option_overrides_the_filename(self):
        code, r = self.run_pbi('PBI-007', None, '--pbi', '8')
        self.assertEqual(code, 0)
        self.assertEqual(r['route'][0]['model'], DS_FREE['model'])

    def test_no_number_counts_as_zero(self):
        code, r = self.run_pbi(None)
        self.assertEqual(code, 0)
        self.assertEqual(r['route'][0]['model'], DS_FREE['model'])

    def test_all_matches_single(self):
        with tempfile.TemporaryDirectory() as d:
            home = os.path.join(d, 'home')
            write_home(home, {'flash-rotate': FLASH_ROTATE}, application('flash-rotate'))
            base = os.path.join(d, 'pbi')
            os.makedirs(base)
            for n in (1, 2):
                with open(os.path.join(base, f'PBI-00{n}.facts.json'), 'w', encoding='utf-8') as f:
                    json.dump(SMALL, f)
            out = subprocess.run([sys.executable, SCRIPT, '--all', base, '--home', home, '--now', NOW],
                                 capture_output=True, text=True, encoding='utf-8', errors='replace',
                                 env=empty_home_env(d))
            self.assertEqual(out.returncode, 0, out.stderr)
            routes = {}
            for n in (1, 2):
                with open(os.path.join(base, f'PBI-00{n}.route.json'), encoding='utf-8') as f:
                    routes[n] = json.load(f)['route']
        self.assertEqual(routes[1][0]['model'], GLM_FREE['model'])
        self.assertEqual(routes[2][0]['model'], DS_FREE['model'])
        for n in (1, 2):
            code, r = self.run_pbi(f'PBI-00{n}')
            self.assertEqual(code, 0)
            # --all は Jev の答えが無く段が違いうるので、最後の agent を除いた並びを比べる
            self.assertEqual(self.models(r)[:-1], [s['model'] for s in routes[n]][:-1])

    def test_skip_a_model_keeps_the_partner_and_paid(self):
        code, r = self.run_pbi('PBI-008', None, '--skip', DS_FREE['model'])
        self.assertEqual(code, 0)
        self.assertEqual(self.models(r), [GLM_FREE['model'], DS_GO['model'], GLM_GO['model'], 'haiku'])

    def test_skip_both_free_models_drops_the_group(self):
        code, r = self.run_pbi('PBI-007', None, '--skip', f"{DS_FREE['model']},{GLM_FREE['model']}")
        self.assertEqual(code, 0)
        self.assertEqual(self.models(r), [GLM_GO['model'], DS_GO['model'], 'haiku'])

    def test_skip_runner_still_drops_every_opencode_step(self):
        code, r = self.run_pbi('PBI-007', None, '--skip', 'opencode')
        self.assertEqual(code, 0)
        self.assertEqual([s['runner'] for s in r['route']], ['agent'])

    def test_dsh_step_heads_route_and_skip_dsh_falls_back(self):
        ds1017 = {'runner': 'dsh', 'model': 'deepseek-official/deepseek-flash', 'effort': 'high'}
        go = {'runner': 'opencode', 'model': 'opencode-go/deepseek-v4.1-flash', 'effort': 'max'}
        t = {'name': 'deepseek-all', 'description': '', 'tiers': {tier: [ds1017, go] for tier in select_model.TIERS}}
        code, r = run_home(SMALL, ALL_ZERO, {'deepseek-all': t}, application(), '--now', NOW)
        self.assertEqual(code, 0)
        self.assertEqual([(s["runner"], s["model"], s["effort"]) for s in r["route"]][:2], [("dsh", "deepseek-official/deepseek-flash", "high"),
                                               ('opencode', 'opencode-go/deepseek-v4.1-flash', 'max')])
        code, r = run_home(SMALL, ALL_ZERO, {'deepseek-all': t}, application(), '--now', NOW, '--skip', 'dsh')
        self.assertEqual(code, 0)
        self.assertEqual(r['route'][0]['runner'], 'opencode')
        code, r = run_home(SMALL, ALL_ZERO, {'deepseek-all': t}, application(), '--now', NOW,
                           '--skip', 'deepseek-official/deepseek-flash')
        self.assertEqual(code, 0)
        self.assertEqual(r['route'][0]['runner'], 'opencode')

    def test_unknown_runner_name_is_rejected(self):
        code, _ = self.run_pbi('PBI-007', None, '--skip', 'nosuch')
        self.assertNotEqual(code, 0)

    def test_sensitive_is_not_rotated(self):
        code, r = self.run_pbi('PBI-007', None, facts={**SMALL, 'sensitive': True})
        self.assertEqual(code, 0)
        self.assertFalse(r['template']['applied'])
        self.assertEqual(r['route'], [{'runner': 'agent', 'model': 'sonnet', 'effort': 'high'}])

    def test_member_past_its_until_is_dropped(self):
        dated = {'name': 'dated', 'description': '', 'tiers': {'haiku': [
            {'rotate': [{**DS_FREE, 'until': '2026-10-19'}, GLM_FREE]}]}}
        code, r = self.run_pbi('PBI-008', {'dated': dated})
        self.assertEqual(code, 0)
        self.assertEqual(self.models(r), [GLM_FREE['model'], 'haiku'])


if __name__ == '__main__':
    unittest.main()
