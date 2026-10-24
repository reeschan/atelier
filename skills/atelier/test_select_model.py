import contextlib
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'select_model.py')


@contextlib.contextmanager
def temp_dir():
    """一時ディレクトリ。tempfile の 0o700 は権限を絞った環境で読めなくなるので、既定の権限で作る。"""
    path = os.path.join(tempfile.gettempdir(), f'pbi-select-{uuid.uuid4().hex}')
    os.mkdir(path)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


sys.path.insert(0, HERE)
import agents  # noqa: E402
import select_model  # noqa: E402

DEADLINE = datetime.date(2026, 10, 23)
BEFORE = DEADLINE.isoformat()
AFTER = (DEADLINE + datetime.timedelta(days=1)).isoformat()
ZERO = {'probabilities': {'0': 0.9, '1': 0.1, '2': 0.0}, 'confidence': 0.9}
TWO = {'probabilities': {'0': 0.0, '1': 0.1, '2': 0.9}, 'confidence': 0.9}
NOW = '2026-10-20T09:00:00+09:00'
PAST = '2026-10-23T09:00:00+09:00'
UNTIL = '2026-10-22T23:59:00+09:00'
OPENCODE_MODEL = 'opencode-go/deepseek-v4.1-flash'
FLASH = [{'runner': 'opencode', 'model': OPENCODE_MODEL, 'effort': 'max'}]
AGENT_STEPS = {
    'haiku': {'runner': 'agent', 'model': 'haiku', 'effort': 'max'},
    'sonnet': {'runner': 'agent', 'model': 'sonnet', 'effort': 'high'},
    'opus': {'runner': 'agent', 'model': 'opus', 'effort': None},
}
ALL_TIERS = {'name': 'all-tiers', 'description': '全段に当てる', 'tiers': {t: FLASH for t in select_model.TIERS}}
HAIKU_ONLY = {'name': 'haiku-only', 'description': 'haiku だけ', 'tiers': {'haiku': FLASH}}


def multi_runner_routing(name='multi-runner', until=DEADLINE.isoformat()):
    """外部の実行先を含み、codex に until を付けた振り分けテンプレート。"""
    def dated(step, day):
        return {**step, 'until': day}
    return {'name': name, 'tiers': {
        'haiku': [FLASH[0],
                  dated({'runner': 'codex', 'model': 'gpt-6-astra', 'effort': 'medium'}, until),
                  {'runner': 'agy', 'model': 'claude-sonnet-5-5-low', 'effort': None},
                  AGENT_STEPS['haiku']],
        'sonnet': [FLASH[0],
                   dated({'runner': 'codex', 'model': 'gpt-6-astra', 'effort': 'high'}, until),
                   {'runner': 'agy', 'model': 'claude-sonnet-5-5-high', 'effort': None},
                   AGENT_STEPS['sonnet']],
        'opus': [AGENT_STEPS['opus']],
    }}


def home_env(d):
    """PBI_TEMPLATES_HOME を d にした環境 (PBI_HOME と XDG_CONFIG_HOME は消す)。"""
    env = {k: v for k, v in os.environ.items() if k not in ('ATELIER_HOME', 'PBI_HOME', 'XDG_CONFIG_HOME')}
    env['PBI_TEMPLATES_HOME'] = d
    return env


def empty_home_env(d):
    """PBI_TEMPLATES_HOME を d の下の空のディレクトリにした環境。--home を渡さない実行が、
    実際のユーザーの home の overrides.json を読まないようにする。"""
    home = os.path.join(d, 'empty-home')
    os.makedirs(home, exist_ok=True)
    return home_env(home)


EXTERNAL = ('opencode', 'codex', 'agy', 'dsh')


def write_agents(home, runners=EXTERNAL):
    """home の agents.json に偽の CLI の path を書く。

    テストを動かす PC に CLI があるかどうかで route が変わらないようにする。
    """
    os.makedirs(os.path.join(home, 'bin'), exist_ok=True)
    table = {'runners': {}}
    for runner in runners:
        path = os.path.join(home, 'bin', runner)
        with open(path, 'w', encoding='utf-8') as f:
            f.write('')
        table['runners'][runner] = {'path': path}
    with open(os.path.join(home, 'agents.json'), 'w', encoding='utf-8') as f:
        json.dump(table, f)


def env_with_agents(d, runners=EXTERNAL):
    """empty_home_env の home に agents.json を置いた環境。"""
    env = empty_home_env(d)
    write_agents(env['PBI_TEMPLATES_HOME'], runners)
    return env


def run(facts, answers=None, *extra):
    with temp_dir() as d:
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
                             errors='replace', env=env_with_agents(d))
        return out.returncode, (json.loads(out.stdout) if out.returncode == 0 else None)


def application(name='all-tiers', until=UNTIL, on_expire='ask'):
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
    with temp_dir() as d:
        write_home(d, templates_, app)
        write_agents(d)
        return run(facts, answers, '--home', d, *extra)


SMALL = {'files_changed': 1, 'modules': 1, 'sensitive': False}
ALL_ZERO = {k: ZERO for k in select_model.KEYS}


class SelectModelTest(unittest.TestCase):
    def runners(self, result):
        return [(s['runner'], s['model'], s['effort']) for s in result['route']]

    def test_default_without_any_routing_is_agent_only(self):
        for tier in select_model.TIERS:
            with self.subTest(tier=tier):
                code, r = run(SMALL, ALL_ZERO, '--tier', tier)
                self.assertEqual(code, 0)
                self.assertEqual(r['route'], [AGENT_STEPS[tier]])
                self.assertEqual(r['dropped'], [])

    def test_no_jev_small_facts_is_haiku(self):
        code, r = run(SMALL, None)
        self.assertEqual(code, 0)
        self.assertEqual(r['tier'], 'haiku')
        self.assertEqual(r['review'], 'normal')
        self.assertEqual(r['route'], [AGENT_STEPS['haiku']])
        self.assertTrue(r['reasons'][0].startswith('Jev なし: facts だけで判定'))

    def test_no_jev_many_files_is_sonnet(self):
        code, r = run({**SMALL, 'files_changed': 4}, None)
        self.assertEqual(code, 0)
        self.assertEqual(r['tier'], 'sonnet')
        self.assertTrue(r['reasons'][0].startswith('Jev なし: facts だけで判定'))

    def test_no_jev_two_modules_is_sonnet(self):
        code, r = run({**SMALL, 'modules': 2}, None)
        self.assertEqual(code, 0)
        self.assertEqual(r['tier'], 'sonnet')

    def test_no_jev_sensitive_is_sonnet_with_deep_review_and_agent_only(self):
        code, r = run({**SMALL, 'sensitive': True}, None)
        self.assertEqual(code, 0)
        self.assertEqual(r['tier'], 'sonnet')
        self.assertEqual(r['review'], 'deep')
        self.assertEqual(r['route'], [AGENT_STEPS['sonnet']])

    def test_no_jev_never_opus(self):
        cases = [SMALL, {**SMALL, 'files_changed': 4}, {**SMALL, 'modules': 2},
                 {**SMALL, 'sensitive': True}, {**SMALL, 'files_changed': 100, 'modules': 100}]
        for facts in cases:
            with self.subTest(facts=facts):
                code, r = run(facts, None)
                self.assertEqual(code, 0)
                self.assertNotEqual(r['tier'], 'opus')

    def test_partial_jev_answer_treats_missing_keys_as_one(self):
        code, r = run(SMALL, {'novelty': ZERO})
        self.assertEqual(code, 0)
        self.assertEqual(r['scores'], {'novelty': 0, 'coupling': 1, 'ambiguity': 1})
        self.assertEqual(r['tier'], 'sonnet')
        self.assertTrue(r['reasons'][0].startswith('決定: sonnet。'))

    def test_sensitive_is_sonnet_agent_only_with_deep_review(self):
        _, r = run({**SMALL, 'sensitive': True}, ALL_ZERO)
        self.assertEqual(r['tier'], 'sonnet')
        self.assertEqual(r['review'], 'deep')
        self.assertEqual(r['route'], [AGENT_STEPS['sonnet']])

    def test_coupling_two_is_sonnet_with_deep_review(self):
        _, r = run(SMALL, dict(ALL_ZERO, coupling=TWO))
        self.assertEqual(r['tier'], 'sonnet')
        self.assertEqual(r['review'], 'deep')
        self.assertEqual(r['route'], [AGENT_STEPS['sonnet']])

    def test_novelty_and_ambiguity_two_is_opus_agent_only(self):
        _, r = run(SMALL, dict(ALL_ZERO, novelty=TWO, ambiguity=TWO))
        self.assertEqual(r['tier'], 'opus')
        self.assertEqual(r['route'], [AGENT_STEPS['opus']])

    def test_many_files_is_sonnet(self):
        _, r = run({**SMALL, 'files_changed': 4}, ALL_ZERO)
        self.assertEqual(r['tier'], 'sonnet')

    def test_low_confidence_lowers_one_step(self):
        answers = dict(ALL_ZERO, coupling={'probabilities': {'0': 0.1, '1': 0.2, '2': 0.7}, 'confidence': 0.5})
        _, r = run(SMALL, answers)
        self.assertEqual(r['scores']['coupling'], 1)
        self.assertEqual(r['tier'], 'sonnet')
        self.assertEqual(r['review'], 'normal')

    def test_invalid_tier_fails(self):
        code, _ = run(SMALL, ALL_ZERO, '--tier', 'xx')
        self.assertNotEqual(code, 0)

    def test_invalid_skip_fails(self):
        code, _ = run(SMALL, ALL_ZERO, '--skip', 'agent')
        self.assertNotEqual(code, 0)

    def test_all_writes_route_files_and_prints_table(self):
        with temp_dir() as d:
            os.makedirs(os.path.join(d, 'jev'))
            for pbi, facts in [('PBI-001', SMALL), ('PBI-002', {**SMALL, 'sensitive': True})]:
                with open(os.path.join(d, f'{pbi}.facts.json'), 'w', encoding='utf-8') as f:
                    json.dump(facts, f)
                with open(os.path.join(d, 'jev', f'{pbi}.brief.json'), 'w', encoding='utf-8') as f:
                    json.dump({'answers': ALL_ZERO}, f)
            out = subprocess.run([sys.executable, SCRIPT, '--all', d],
                                 capture_output=True, text=True, encoding='utf-8', env=empty_home_env(d))
            self.assertEqual(out.returncode, 0)
            rows = [line for line in out.stdout.splitlines() if line.startswith('| PBI-')]
            self.assertEqual(len(rows), 2)
            with open(os.path.join(d, 'PBI-002.route.json'), encoding='utf-8') as f:
                r2 = json.load(f)
            self.assertEqual(r2['review'], 'deep')
            self.assertEqual([s['runner'] for s in r2['route']], ['agent'])

    def test_template_applies_to_every_tier(self):
        for tier, agent in AGENT_STEPS.items():
            with self.subTest(tier=tier):
                code, r = run_home(SMALL, ALL_ZERO, {'all-tiers': ALL_TIERS}, application(),
                                   '--now', NOW, '--tier', tier)
                self.assertEqual(code, 0)
                self.assertTrue(r['template']['applied'])
                self.assertEqual(r['template']['name'], 'all-tiers')
                note = f'all-tiers を当てた ({UNTIL} まで)'
                if tier == 'opus':
                    note += '（opus の段を上書き）'
                self.assertEqual(r['template']['note'], note)
                self.assertEqual(self.runners(r), [
                    ('opencode', OPENCODE_MODEL, 'max'),
                    (agent['runner'], agent['model'], agent['effort']),
                ])
                self.assertTrue(r['reasons'][0].endswith('（優先テンプレート all-tiers を当てた）'))

    def test_template_without_that_tier_keeps_normal_route(self):
        code, r = run_home({**SMALL, 'files_changed': 4}, ALL_ZERO, {'haiku-only': HAIKU_ONLY},
                           application('haiku-only'), '--now', NOW)
        self.assertEqual(code, 0)
        self.assertEqual(r['tier'], 'sonnet')
        self.assertFalse(r['template']['applied'])
        self.assertEqual(r['template']['note'], 'この段の上書きなし')
        self.assertEqual(r['route'], [AGENT_STEPS['sonnet']])

    def test_sensitive_keeps_agent_sonnet_only(self):
        code, r = run_home({**SMALL, 'sensitive': True}, ALL_ZERO, {'all-tiers': ALL_TIERS},
                           application(), '--now', NOW)
        self.assertEqual(code, 0)
        self.assertFalse(r['template']['applied'])
        self.assertEqual(r['template']['note'], 'sensitive のため当てない')
        self.assertEqual(self.runners(r), [('agent', 'sonnet', 'high')])

    def test_expired_ask_is_not_applied_and_asks_the_user(self):
        code, r = run_home(SMALL, ALL_ZERO, {'all-tiers': ALL_TIERS}, application(), '--now', PAST)
        self.assertEqual(code, 0)
        self.assertFalse(r['template']['applied'])
        self.assertEqual(r['template']['name'], 'all-tiers')
        self.assertEqual(r['template']['note'], '期限切れ: 延長・やめる・期限なしの確認が要る')
        self.assertEqual(r['route'], [AGENT_STEPS['haiku']])

    def test_expired_drop_is_not_applied(self):
        code, r = run_home(SMALL, ALL_ZERO, {'all-tiers': ALL_TIERS}, application(on_expire='drop'),
                           '--now', PAST)
        self.assertEqual(code, 0)
        self.assertFalse(r['template']['applied'])
        self.assertEqual(r['template']['note'], '期限切れ (drop) のため当てない')
        self.assertEqual(r['route'], [AGENT_STEPS['haiku']])

    def test_until_null_applies_without_deadline(self):
        code, r = run_home(SMALL, ALL_ZERO, {'all-tiers': ALL_TIERS}, application(until=None),
                           '--now', PAST)
        self.assertEqual(code, 0)
        self.assertTrue(r['template']['applied'])
        self.assertEqual(r['template']['note'], 'all-tiers を期限なしで当てた')
        self.assertEqual(self.runners(r), [
            ('opencode', OPENCODE_MODEL, 'max'),
            ('agent', 'haiku', 'max'),
        ])

    def test_skip_opencode_leaves_only_agent(self):
        code, r = run_home(SMALL, ALL_ZERO, {'all-tiers': ALL_TIERS}, application(),
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
        self.assertEqual(r['route'], [AGENT_STEPS['haiku']])

    def test_without_overrides_keeps_the_same_route(self):
        code, r = run_home(SMALL, ALL_ZERO, {'all-tiers': ALL_TIERS}, None, '--now', NOW)
        self.assertEqual(code, 0)
        self.assertFalse(r['template']['applied'])
        self.assertIsNone(r['template']['name'])
        self.assertEqual(r['template']['note'], '適用なし')
        _, plain = run(SMALL, ALL_ZERO)
        self.assertEqual(self.runners(r), self.runners(plain))

    def test_today_and_now_are_used_separately(self):
        code, r = run_home(SMALL, ALL_ZERO, {'all-tiers': ALL_TIERS}, application(),
                           '--today', AFTER, '--now', NOW)
        self.assertEqual(code, 0)
        self.assertTrue(r['template']['applied'])
        code, r = run_home(SMALL, ALL_ZERO, {'all-tiers': ALL_TIERS}, application(),
                           '--today', BEFORE, '--now', PAST)
        self.assertEqual(code, 0)
        self.assertFalse(r['template']['applied'])
        self.assertEqual(r['template']['note'], '期限切れ: 延長・やめる・期限なしの確認が要る')

    def test_all_table_has_template_column(self):
        with temp_dir() as d:
            os.makedirs(os.path.join(d, 'jev'))
            home = os.path.join(d, 'home')
            write_home(home, {'all-tiers': ALL_TIERS}, application())
            write_agents(home)
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
            self.assertIn('| 外した手順 |', lines[0])
            rows = [line for line in lines if line.startswith('| PBI-')]
            self.assertEqual(len(rows), 2)
            self.assertIn(f'all-tiers を当てた ({UNTIL} まで)', rows[0])
            self.assertIn('sensitive のため当てない', rows[1])
            with open(os.path.join(d, 'PBI-001.route.json'), encoding='utf-8') as f:
                self.assertTrue(json.load(f)['template']['applied'])


class AgentFilterTest(unittest.TestCase):
    """route を組んだあと agents.filter_steps で照合し、外した手順を dropped に並べる。"""

    def decide(self, routing, found, home, agents_table=None, skip=frozenset(), today=BEFORE):
        return select_model.decide(SMALL, ALL_ZERO, datetime.date.fromisoformat(today),
                                   skip=skip, routing=routing, home=home,
                                   now=datetime.datetime.fromisoformat(NOW),
                                   agents=agents_table if agents_table is not None else {}, found=found)

    def test_missing_cli_is_dropped_and_the_rest_stays(self):
        found = {'opencode': 'x', 'codex': None, 'agy': None, 'dsh': None}
        with temp_dir() as d:
            r = self.decide(multi_runner_routing(), found, Path(d))
        self.assertEqual(r['route'], [FLASH[0], AGENT_STEPS['haiku']])
        self.assertEqual([(x['runner'], x['model'], x['effort'], x['reason']) for x in r['dropped']], [
            ('codex', 'gpt-6-astra', 'medium', agents.REASON_NOT_FOUND),
            ('agy', 'claude-sonnet-5-5-low', None, agents.REASON_NOT_FOUND),
        ])

    def test_disabled_and_unselected_models_are_dropped(self):
        table = {'runners': {'codex': {'enabled': False},
                             'opencode': {'models': ['opencode-go/other']}}}
        found = {'opencode': 'x', 'codex': 'x', 'agy': 'x', 'dsh': 'x'}
        with temp_dir() as d:
            r = self.decide(multi_runner_routing(), found, Path(d), agents_table=table)
        self.assertEqual([s['runner'] for s in r['route']], ['agy', 'agent'])
        self.assertEqual([(x['runner'], x['reason']) for x in r['dropped']], [
            ('opencode', agents.REASON_MODEL_NOT_SELECTED),
            ('codex', agents.REASON_DISABLED),
        ])

    def test_skip_and_until_are_dropped_with_their_reasons(self):
        found = {'opencode': 'x', 'codex': 'x', 'agy': 'x', 'dsh': 'x'}
        with temp_dir() as d:
            r = self.decide(multi_runner_routing(), found, Path(d), skip=frozenset({'opencode'}),
                            today=AFTER)
        self.assertEqual([(x['runner'], x['reason']) for x in r['dropped']], [
            ('opencode', select_model.REASON_SKIP),
            ('codex', select_model.REASON_UNTIL),
        ])
        self.assertEqual([s['runner'] for s in r['route']], ['agy', 'agent'])

    def test_priority_template_steps_are_filtered_and_fall_back_to_the_routing_agent(self):
        template = {'name': 'codex-first', 'description': '', 'tiers': {'haiku': [
            {'runner': 'codex', 'model': 'gpt-6-astra', 'effort': 'high'},
            {'runner': 'opencode', 'model': OPENCODE_MODEL, 'effort': 'max'},
        ]}}
        routing = {'name': 'routing', 'tiers': {t: [AGENT_STEPS[t]] for t in select_model.TIERS}}
        found = {'opencode': 'x', 'codex': None, 'agy': 'x', 'dsh': 'x'}
        with temp_dir() as d:
            write_home(d, {'codex-first': template}, application('codex-first'))
            r = self.decide(routing, found, Path(d))
        self.assertTrue(r['template']['applied'])
        self.assertEqual([(s['runner'], s['model']) for s in r['route']],
                         [('opencode', OPENCODE_MODEL), ('agent', 'haiku')])
        self.assertEqual([(x['runner'], x['reason']) for x in r['dropped']],
                         [('codex', agents.REASON_NOT_FOUND)])

    def test_claude_is_not_in_the_default_routing_so_it_never_reaches_the_route(self):
        default = select_model.default_routing()
        for tier in select_model.TIERS:
            self.assertNotIn('claude', [s['runner'] for s in default['tiers'][tier]])
        found = {runner: 'x' for runner in select_model.RUNNERS}
        with temp_dir() as d:
            r = self.decide(default, found, Path(d), agents_table={'runners': {'claude': {'enabled': True}}})
        self.assertNotIn('claude', [s['runner'] for s in r['route']])
        self.assertEqual(r['dropped'], [])

    def test_claude_step_appears_in_route_only_when_enabled_in_agents_json(self):
        claude = {'runner': 'claude', 'model': 'sonnet', 'effort': 'high'}
        template = {'name': 'claude-first', 'description': '', 'tiers': {'haiku': [claude]}}
        routing = {'name': 'routing', 'tiers': {t: [AGENT_STEPS[t]] for t in select_model.TIERS}}
        found = {runner: 'x' for runner in select_model.RUNNERS}
        with temp_dir() as d:
            write_home(d, {'claude-first': template}, application('claude-first'))
            on = self.decide(routing, found, Path(d), agents_table={'runners': {'claude': {'enabled': True}}})
            off = self.decide(routing, found, Path(d), agents_table={'runners': {'claude': {'enabled': False}}})
            missing = self.decide(routing, {**found, 'claude': None}, Path(d))
        self.assertEqual([(s['runner'], s['model']) for s in on['route']],
                         [('claude', 'sonnet'), ('agent', 'haiku')])
        self.assertEqual([s['runner'] for s in off['route']], ['agent'])
        self.assertEqual([(x['runner'], x['reason']) for x in off['dropped']],
                         [('claude', agents.REASON_DISABLED)])
        self.assertEqual([x['reason'] for x in missing['dropped']], [agents.REASON_NOT_FOUND])

    def test_opus_template_note_has_the_override_warning(self):
        for tier in select_model.TIERS:
            with self.subTest(tier=tier):
                code, r = run_home(SMALL, ALL_ZERO, {'all-tiers': ALL_TIERS}, application(),
                                   '--now', NOW, '--tier', tier)
                self.assertEqual(code, 0)
                if tier == 'opus':
                    self.assertIn('opus の段を上書き', r['template']['note'])
                else:
                    self.assertNotIn('opus の段を上書き', r['template']['note'])


class AllOnlyTest(unittest.TestCase):
    """--all の --only と --tier。指定した PBI の route.json だけを書き直す。"""

    def write_pbi(self, base, name, facts, answers=ALL_ZERO):
        os.makedirs(os.path.join(base, 'jev'), exist_ok=True)
        with open(os.path.join(base, f'{name}.facts.json'), 'w', encoding='utf-8') as f:
            json.dump(facts, f)
        with open(os.path.join(base, 'jev', f'{name}.brief.json'), 'w', encoding='utf-8') as f:
            json.dump({'answers': answers}, f)

    def run_all(self, base, *extra):
        return subprocess.run([sys.executable, SCRIPT, '--all', base, '--now', NOW, *extra],
                              capture_output=True, text=True, encoding='utf-8', errors='replace',
                              env=env_with_agents(base))

    def test_only_and_tier_rewrite_only_that_route(self):
        with temp_dir() as d:
            for name in ('PBI-001', 'PBI-002'):
                self.write_pbi(d, name, SMALL)
            first = self.run_all(d)
            self.assertEqual(first.returncode, 0, first.stderr)
            with open(os.path.join(d, 'PBI-001.route.json'), 'rb') as f:
                before = f.read()
            out = self.run_all(d, '--only', 'PBI-002', '--tier', 'sonnet')
            self.assertEqual(out.returncode, 0, out.stderr)
            rows = [line for line in out.stdout.splitlines() if line.startswith('| PBI-')]
            self.assertEqual(len(rows), 1)
            self.assertIn('PBI-002', rows[0])
            with open(os.path.join(d, 'PBI-002.route.json'), encoding='utf-8') as f:
                r2 = json.load(f)
            self.assertEqual(r2['tier'], 'sonnet')
            self.assertTrue(r2['reasons'][0].startswith('決定: sonnet。--tier sonnet で指定'))
            with open(os.path.join(d, 'PBI-001.route.json'), 'rb') as f:
                self.assertEqual(f.read(), before)

    def test_tier_without_only_fails(self):
        with temp_dir() as d:
            self.write_pbi(d, 'PBI-001', SMALL)
            self.assertNotEqual(self.run_all(d, '--tier', 'sonnet').returncode, 0)

    def test_only_without_facts_fails(self):
        with temp_dir() as d:
            self.write_pbi(d, 'PBI-001', SMALL)
            self.assertNotEqual(self.run_all(d, '--only', 'PBI-999').returncode, 0)


class FindCliOnceTest(unittest.TestCase):
    """--all は agents.json を 1 回だけ読み、CLI の場所も runner ごとに 1 回だけ求める。"""

    @mock.patch.object(agents, 'load_agents', return_value=None)
    @mock.patch.object(agents, 'find_cli', return_value=None)
    def test_agents_are_read_once_and_each_cli_is_found_once(self, find_cli, load_agents):
        with temp_dir() as d:
            for name in ('PBI-001', 'PBI-002', 'PBI-003'):
                with open(os.path.join(d, f'{name}.facts.json'), 'w', encoding='utf-8') as f:
                    json.dump(SMALL, f)
            with mock.patch('builtins.print'):
                select_model.run_all(Path(d), datetime.date.fromisoformat(BEFORE), frozenset(),
                                     routing=select_model.default_routing(), home=Path(d))
        self.assertEqual(load_agents.call_count, 1)
        self.assertEqual(find_cli.call_count, len(select_model.RUNNERS))
        self.assertEqual({c.args[0] for c in find_cli.call_args_list}, set(select_model.RUNNERS))


PERF = {'name': 'performance', 'tiers': {
    'haiku': [{'runner': 'agy', 'model': 'claude-sonnet-5-5-high', 'effort': None},
              AGENT_STEPS['sonnet']],
    'sonnet': [{'runner': 'codex', 'model': 'gpt-6-astra', 'effort': 'high', 'until': BEFORE},
               AGENT_STEPS['opus']],
    'opus': [AGENT_STEPS['opus']],
}}


class RoutingTemplateTest(unittest.TestCase):
    def write(self, d, name, table):
        path = os.path.join(d, name)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(table, f)
        return path

    def test_default_file_is_read_and_all_agent(self):
        with open(os.path.join(HERE, 'routing.default.json'), encoding='utf-8') as f:
            table = json.load(f)
        self.assertEqual(table['name'], 'default')
        for tier in select_model.TIERS:
            self.assertEqual(table['tiers'][tier], [AGENT_STEPS[tier]])
        self.assertIn('examples/routing/multi-runner.json', table['$schema_note'])
        self.assertEqual(select_model.default_routing(), table)

    def test_default_routing_json_next_to_the_script_is_used(self):
        with temp_dir() as d:
            script = os.path.join(d, 'skill')
            os.makedirs(script)
            for name in ('select_model.py', 'templates.py', 'common.py', 'agents.py', 'routing.default.json'):
                shutil.copy(os.path.join(HERE, name), os.path.join(script, name))
            custom = {'name': 'custom-default', 'tiers': {
                'haiku': [AGENT_STEPS['sonnet']],
                'sonnet': [AGENT_STEPS['sonnet']],
                'opus': [AGENT_STEPS['opus']]}}
            with open(os.path.join(script, 'routing.default.json'), 'w', encoding='utf-8') as f:
                json.dump(custom, f)
            fp = os.path.join(d, 'facts.json')
            with open(fp, 'w', encoding='utf-8') as f:
                json.dump(SMALL, f)
            out = subprocess.run([sys.executable, os.path.join(script, 'select_model.py'), fp, '--tier', 'haiku'],
                                 capture_output=True, text=True, encoding='utf-8', errors='replace',
                                 env=empty_home_env(d))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)['route'], [AGENT_STEPS['sonnet']])

    def test_routing_option_replaces_route(self):
        with temp_dir() as d:
            _, r = run(SMALL, ALL_ZERO, '--routing', self.write(d, 'r.json', PERF))
        self.assertEqual(r['tier'], 'haiku')
        self.assertEqual([s['runner'] for s in r['route']], ['agy', 'agent'])
        self.assertIn('performance', r['reasons'][0])

    def test_until_before_deadline_keeps_codex(self):
        with temp_dir() as d:
            _, r = run(SMALL, ALL_ZERO, '--today', BEFORE,
                       '--routing', self.write(d, 'r.json', multi_runner_routing()))
        self.assertEqual(r['tier'], 'haiku')
        self.assertEqual([(s['runner'], s['model'], s['effort']) for s in r['route']], [
            ('opencode', OPENCODE_MODEL, 'max'),
            ('codex', 'gpt-6-astra', 'medium'),
            ('agy', 'claude-sonnet-5-5-low', None),
            ('agent', 'haiku', 'max'),
        ])

    def test_after_deadline_drops_codex(self):
        with temp_dir() as d:
            _, r = run(SMALL, ALL_ZERO, '--today', AFTER,
                       '--routing', self.write(d, 'r.json', multi_runner_routing()))
        self.assertEqual([s['runner'] for s in r['route']], ['opencode', 'agy', 'agent'])

    def test_template_until_drops_step(self):
        with temp_dir() as d:
            _, r = run({**SMALL, 'files_changed': 4}, None, '--today', AFTER,
                       '--routing', self.write(d, 'r.json', PERF))
        self.assertEqual(r['tier'], 'sonnet')
        self.assertEqual(r['route'], [AGENT_STEPS['opus']])

    def test_sensitive_uses_template_agent_only(self):
        with temp_dir() as d:
            _, r = run({**SMALL, 'sensitive': True}, ALL_ZERO, '--today', BEFORE,
                       '--routing', self.write(d, 'r.json', PERF))
        self.assertEqual([s['runner'] for s in r['route']], ['agent'])

    def test_skip_removes_runners_but_keeps_agent(self):
        with temp_dir() as d:
            _, r = run(SMALL, ALL_ZERO, '--today', BEFORE, '--skip', 'opencode,agy',
                       '--routing', self.write(d, 'r.json', multi_runner_routing()))
        self.assertEqual([s['runner'] for s in r['route']], ['codex', 'agent'])

    def test_forced_tier_with_skip_opencode(self):
        with temp_dir() as d:
            _, r = run(SMALL, ALL_ZERO, '--today', BEFORE, '--tier', 'sonnet', '--skip', 'opencode',
                       '--routing', self.write(d, 'r.json', multi_runner_routing()))
        self.assertEqual(r['tier'], 'sonnet')
        self.assertEqual(r['route'][0], {'runner': 'codex', 'model': 'gpt-6-astra', 'effort': 'high'})

    def test_template_not_ending_with_agent_fails(self):
        bad = {'tiers': dict(PERF['tiers'], opus=[{'runner': 'codex', 'model': 'x'}])}
        with temp_dir() as d:
            code, _ = run(SMALL, ALL_ZERO, '--routing', self.write(d, 'r.json', bad))
        self.assertNotEqual(code, 0)

    def test_agent_without_model_or_bad_until_fails(self):
        no_model = {'tiers': dict(PERF['tiers'], opus=[{'runner': 'agent'}])}
        bad_until = {'tiers': dict(PERF['tiers'], sonnet=[{'runner': 'codex', 'model': 'x', 'until': 'soon'},
                                                          {'runner': 'agent', 'model': 'sonnet'}])}
        list_model = {'tiers': dict(PERF['tiers'], opus=[{'runner': 'agent', 'model': ['opus']}])}
        for table in (no_model, bad_until, list_model):
            with temp_dir() as d:
                code, _ = run(SMALL, ALL_ZERO, '--routing', self.write(d, 'r.json', table))
            self.assertNotEqual(code, 0)

    def test_all_picks_routing_json_in_dir(self):
        with temp_dir() as d:
            self.write(d, 'routing.json', PERF)
            self.write(d, 'PBI-001.facts.json', {**SMALL, 'files_changed': 4})
            out = subprocess.run([sys.executable, SCRIPT, '--all', d, '--today', BEFORE],
                                 capture_output=True, text=True, encoding='utf-8', env=env_with_agents(d))
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
        with temp_dir() as d:
            self.write(d, PERF)
            write_agents(d)
            code, r = run(SMALL, ALL_ZERO, '--today', BEFORE, '--home', d)
        self.assertEqual(code, 0)
        self.assertEqual(self.runners(r), ['agy', 'agent'])
        self.assertIn('performance', r['reasons'][0])

    def test_env_home_routing_json_is_used(self):
        with temp_dir() as d:
            self.write(d, PERF)
            write_agents(d)
            fp = os.path.join(d, 'facts.json')
            with open(fp, 'w', encoding='utf-8') as f:
                json.dump({**SMALL, 'files_changed': 4}, f)
            out = subprocess.run([sys.executable, SCRIPT, fp, '--today', BEFORE], capture_output=True, text=True,
                                 encoding='utf-8', env=home_env(d))
        self.assertEqual(out.returncode, 0, out.stderr)
        # Jev の答えが無いので facts だけで sonnet の段。PERF の sonnet は codex → agent
        self.assertEqual(self.runners(json.loads(out.stdout)), ['codex', 'agent'])

    def test_all_dir_routing_json_wins_over_home(self):
        dir_table = {'name': 'dir', 'tiers': dict(PERF['tiers'], sonnet=[{'runner': 'agent', 'model': 'sonnet'}])}
        with temp_dir() as d:
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

    def run_copied(self, script_dir, facts_dir, env=None):
        """select_model.py・templates.py・common.py・agents.py を script_dir に写し、--home も PBI_HOME も無しで動かす。
        Jev の答えを渡さず files_changed=4 なので、facts だけで sonnet の段になる。"""
        os.makedirs(script_dir, exist_ok=True)
        for name in ('select_model.py', 'templates.py', 'common.py', 'agents.py'):
            with open(os.path.join(HERE, name), encoding='utf-8') as src, \
                    open(os.path.join(script_dir, name), 'w', encoding='utf-8') as dst:
                dst.write(src.read())
        fp = os.path.join(facts_dir, 'facts.json')
        with open(fp, 'w', encoding='utf-8') as f:
            json.dump({**SMALL, 'files_changed': 4}, f)
        copied_env = {k: v for k, v in os.environ.items()
                      if k not in ('ATELIER_HOME', 'PBI_HOME', 'PBI_TEMPLATES_HOME', 'XDG_CONFIG_HOME')}
        copied_env.update(env or {})
        out = subprocess.run([sys.executable, os.path.join(script_dir, 'select_model.py'), fp, '--today', BEFORE],
                             capture_output=True, text=True, encoding='utf-8', env=copied_env)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_home_does_not_depend_on_the_script_location(self):
        """スクリプトの置き場所 (版の有無) で home は変わらず、XDG_CONFIG_HOME の下を読む。"""
        with temp_dir() as d:
            config = os.path.join(d, 'config')
            self.write(os.path.join(config, 'atelier'), PERF)
            write_agents(os.path.join(config, 'atelier'))
            for layout in ('flat', 'versioned'):
                root = os.path.join(d, 'skill-' + layout)
                script_dir = os.path.join(root, 'versions', 'v1') if layout == 'versioned' else root
                self.write(root, {'name': 'layout-dir', 'tiers': {
                    t: [{'runner': 'agent', 'model': 'opus'}] for t in select_model.TIERS}})
                r = self.run_copied(script_dir, d, {'XDG_CONFIG_HOME': config})
                self.assertEqual(self.runners(r), ['codex', 'agent'], layout)
                self.assertIn('performance', r['reasons'][0], layout)


class PriorityOverRoutingTest(unittest.TestCase):
    """優先テンプレートは振り分けテンプレート (routing) より先に当たり、最後は routing のその段の agent。"""

    def test_priority_template_wins_and_falls_back_to_routing_agent(self):
        with temp_dir() as d:
            path = os.path.join(d, 'routing.json')
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(PERF, f)
            code, r = run_home({**SMALL, 'files_changed': 4}, ALL_ZERO, {'all-tiers': ALL_TIERS},
                               application(), '--now', NOW, '--routing', path)
        self.assertEqual(code, 0)
        self.assertEqual(r['tier'], 'sonnet')
        self.assertTrue(r['template']['applied'])
        self.assertEqual([(s['runner'], s['model']) for s in r['route']],
                         [('opencode', OPENCODE_MODEL), ('agent', 'opus')])

    def test_routing_is_used_when_no_application(self):
        with temp_dir() as d:
            path = os.path.join(d, 'routing.json')
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(PERF, f)
            code, r = run_home({**SMALL, 'files_changed': 4}, ALL_ZERO, {'all-tiers': ALL_TIERS},
                               None, '--now', NOW, '--today', BEFORE, '--routing', path)
        self.assertEqual(code, 0)
        self.assertFalse(r['template']['applied'])
        self.assertEqual([s['runner'] for s in r['route']], ['codex', 'agent'])

    def test_priority_step_past_its_until_is_dropped(self):
        dated = {'name': 'dated', 'description': '', 'tiers': {'haiku': [
            {'runner': 'codex', 'model': 'gpt-6-astra', 'effort': 'medium', 'until': '2026-10-19'},
            {'runner': 'opencode', 'model': OPENCODE_MODEL, 'effort': 'max'}]}}
        code, r = run_home(SMALL, ALL_ZERO, {'dated': dated}, application('dated'), '--now', NOW)
        self.assertEqual(code, 0)
        self.assertTrue(r['template']['applied'])
        self.assertEqual([s['runner'] for s in r['route']], ['opencode', 'agent'])


DS_FREE = {'runner': 'opencode', 'model': 'opencode/deepseek-v4.1-flash-free', 'effort': 'max'}
GLM_FREE = {'runner': 'opencode', 'model': 'opencode/glm-5.3-flash-free', 'effort': 'max'}
DS_GO = {'runner': 'opencode', 'model': 'opencode-go/deepseek-v4.1-flash', 'effort': 'max'}
GLM_GO = {'runner': 'opencode', 'model': 'opencode-go/glm-5.3-flash', 'effort': 'max'}
ROTATE_STEPS = [{'rotate': [DS_FREE, GLM_FREE]}, {'rotate': [DS_GO, GLM_GO]}]
FLASH_ROTATE = {'name': 'rotation', 'description': '交互', 'tiers': {t: ROTATE_STEPS for t in select_model.TIERS}}


class RotateTest(unittest.TestCase):
    """交互グループは PBI の番号 mod メンバー数 を先頭にして回し、残りは控えとして後ろに並ぶ。"""

    def run_pbi(self, pbi, templates_=None, *extra, facts=SMALL):
        """facts を <pbi>.facts.json に書いて単体で判定する。pbi が None なら facts.json。"""
        with temp_dir() as d:
            home = os.path.join(d, 'home')
            name = next(iter(templates_)) if templates_ else 'rotation'
            write_home(home, templates_ or {'rotation': FLASH_ROTATE}, application(name))
            write_agents(home)
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
        with temp_dir() as d:
            home = os.path.join(d, 'home')
            write_home(home, {'rotation': FLASH_ROTATE}, application('rotation'))
            write_agents(home)
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
        t = {'name': 'dsh-first', 'description': '', 'tiers': {tier: [ds1017, go] for tier in select_model.TIERS}}
        code, r = run_home(SMALL, ALL_ZERO, {'dsh-first': t}, application('dsh-first'), '--now', NOW)
        self.assertEqual(code, 0)
        self.assertEqual([(s["runner"], s["model"], s["effort"]) for s in r["route"]][:2],
                         [("dsh", "deepseek-official/deepseek-flash", "high"),
                          ('opencode', 'opencode-go/deepseek-v4.1-flash', 'max')])
        code, r = run_home(SMALL, ALL_ZERO, {'dsh-first': t}, application('dsh-first'),
                           '--now', NOW, '--skip', 'dsh')
        self.assertEqual(code, 0)
        self.assertEqual(r['route'][0]['runner'], 'opencode')
        code, r = run_home(SMALL, ALL_ZERO, {'dsh-first': t}, application('dsh-first'), '--now', NOW,
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
