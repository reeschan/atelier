import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from atelier import template


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.agents = self.root / 'agents.json'
        self.agents.write_text(json.dumps({'runners': {'agent': {'enabled': True}, 'codex': {'enabled': True}, 'off': {'enabled': False}}}), encoding='utf-8')
        self.default = template.load_template(agents_path=self.agents)

    def test_default_and_choice(self):
        levels = template.load_levels()
        self.assertEqual([x['id'] for x in levels], ['L1', 'L2', 'L3', 'L4', 'L5'])
        self.assertTrue(all(len(x['examples']) == 2 for x in levels))
        choice = template.choose_route(self.default, {'level': 'L3', 'level_reason': '複雑'})
        self.assertEqual(choice['inputs'], {'level': 'L3', 'reason': '複雑'})
        self.assertEqual(choice['rule'], 'routes.L3')
        self.assertEqual(choice['template'], self.default['name'])
        self.assertTrue(all(route[-1]['runner'] == 'agent' for route in self.default['routes'].values()))
        self.assertTrue(template.should_read(self.default, 'missing'))

    def test_external_route_and_roles(self):
        value = copy.deepcopy(self.default)
        value['routes']['L2'] = [{'runner': 'codex', 'model': 'same', 'effort': 'high'}]
        value['routes']['L3'] = [{'runner': 'codex', 'model': 'same', 'effort': 'low'}]
        value['roles']['interviewer']['model'] = 'different'
        value['reading']['G1'] = False
        template.validate_template(value, self.agents)
        self.assertEqual(template.role_for(value, 'interviewer')['model'], 'different')
        self.assertFalse(template.should_read(value, 'G1'))

    def test_invalid_values(self):
        changes = (lambda x: x['routes'].pop('L1'), lambda x: x['limits'].update(auto_fix=2), lambda x: x['routes']['L1'][0].update(runner='off'), lambda x: x['parts'].update(foo='yes'), lambda x: x['reading'].update(G1=1))
        for change in changes:
            value = copy.deepcopy(self.default)
            change(value)
            with self.subTest(value=value), self.assertRaises(template.TemplateError) as caught:
                template.validate_template(value, self.agents)
            self.assertNotIn('\n', str(caught.exception))

    def test_switch(self):
        alternate = copy.deepcopy(self.default)
        alternate['name'] = 'alternate'
        alternate['limits']['e2e_rounds'] = 7
        alternate['roles']['reviewer']['model'] = 'other'
        path = self.root / 'alternate.json'
        path.write_text(json.dumps(alternate), encoding='utf-8')
        now = datetime(2026, 1, 2, tzinfo=timezone.utc)
        switch = {'template': str(path), 'until': '2026-01-03', 'on_expire': 'ask', 'reason': '期間限定'}
        selected = template.resolve_template(None, switch, agents_path=self.agents, now=now)
        self.assertEqual(selected['template']['limits']['e2e_rounds'], 7)
        self.assertEqual(selected['template']['roles']['reviewer']['model'], 'other')
        switch['until'] = '2026-01-01'
        self.assertTrue(template.resolve_template(None, switch, agents_path=self.agents, now=now)['ask'])
        switch['on_expire'] = 'drop'
        selected = template.resolve_template(None, switch, agents_path=self.agents, now=now)
        self.assertFalse(selected['ask'])
        self.assertEqual(selected['template']['name'], self.default['name'])

    def test_an_older_e2e_limit_is_read_but_not_used(self):
        # E2E runs once and a failure goes to the retrospective list: the default has no E2E limit any more.
        self.assertNotIn('e2e_rounds', json.loads(template.DEFAULT_TEMPLATE.read_text(encoding='utf-8'))['limits'])
        self.assertNotIn('e2e_rounds', self.default['limits'])
        for rounds in (2, 0, 7):
            value = copy.deepcopy(self.default)
            value['limits']['e2e_rounds'] = rounds
            with self.subTest(rounds=rounds):
                self.assertEqual(template.validate_template(value, self.agents)['limits']['e2e_rounds'], rounds)

    def test_bad_inputs(self):
        with self.assertRaises(template.TemplateError):
            template.resolve_template(None, [], agents_path=self.agents)

    def test_missing_registration_allows_only_agent(self):
        missing = self.root / 'missing' / 'agents.json'
        self.assertEqual(template.load_template(agents_path=missing)['name'], self.default['name'])
        external = json.loads(json.dumps(self.default))
        external['routes']['L1'] = [{'runner': 'codex', 'model': 'gpt', 'effort': None}]
        with self.assertRaises(template.TemplateError):
            template.validate_template(external, agents_path=missing)
