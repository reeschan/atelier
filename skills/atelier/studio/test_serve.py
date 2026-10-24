import copy
import http.client
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import common  # noqa: E402
import serve  # noqa: E402

SKILL = Path(HERE).parent
DEFAULT = json.loads((SKILL / 'routing.default.json').read_text(encoding='utf-8'))
CHEAP = json.loads((SKILL / 'examples' / 'templates' / 'cheap-all-tiers.json').read_text(encoding='utf-8'))
# options の一致 (opencode の deepseek・agent の opus) と aa_slug の一致 (codex/claude-sonnet-5-5-low) を確かめる表
CATALOG_FIXTURE = {
    'models': [
        {'id': 'deepseek-flash', 'aa_slug': 'deepseek-v4-1-flash', 'quality': 66,
         'price_in': 0.15, 'price_out': 0.6, 'sample': True,
         'options': [{'runner': 'opencode', 'model': 'opencode-go/deepseek-v4.1-flash'}]},
        {'id': 'claude-sonnet', 'aa_slug': 'claude-sonnet-5-5', 'quality': 80,
         'price_in': 3.0, 'price_out': 15.0, 'sample': True,
         'options': [{'runner': 'agent', 'model': 'sonnet'}]},
        {'id': 'claude-opus', 'aa_slug': 'claude-opus-5-5', 'quality': 90,
         'price_in': 15.0, 'price_out': 75.0, 'sample': True,
         'options': [{'runner': 'agent', 'model': 'opus'}]},
    ],
}


class ServeTest(unittest.TestCase):
    def setUp(self):
        # tempfile の 0o700 は権限を絞った環境で書けなくなるので、既定の権限で作る
        self.home = Path(tempfile.gettempdir()) / f'pbi-serve-{uuid.uuid4().hex}'
        self.home.mkdir()
        (self.home / 'models').mkdir()
        (self.home / 'templates').mkdir()
        self.write_json('agents.json', {
            'runners': {
                'opencode': {'enabled': True, 'path': 'C:/tools/opencode.exe', 'version': '1.2.3',
                             'checked_at': '2026-10-20T00:00:00+00:00',
                             'models': ['opencode-go/deepseek-v4.1-flash', 'gone-model'],
                             'billing': 'subscription'},
                'codex': {'enabled': True, 'path': 'C:/tools/codex.exe', 'version': '2.0.0',
                          'checked_at': '2026-10-20T00:00:00+00:00', 'models': ['gpt-6-astra'],
                          'billing': 'subscription'},
                'agy': {'enabled': False, 'path': None, 'version': None,
                        'checked_at': '2026-10-20T00:00:00+00:00'},
                'dsh': {'enabled': True, 'path': None, 'version': None,
                        'checked_at': '2026-10-20T00:00:00+00:00'},
                'agent': {'enabled': True, 'models': ['opus', 'sonnet', 'haiku']},
            },
        })
        self.write_json('models/opencode.json', {
            'runner': 'opencode', 'fetched_at': '2026-10-20T00:00:00+00:00', 'method': 'cli',
            'error': None, 'failed_at': None,
            'models': [
                {'id': 'opencode-go/deepseek-v4.1-flash', 'name': 'DeepSeek V4.1 Flash',
                 'efforts': ['low', 'high']},
                {'id': 'opencode-go/other', 'name': 'Other', 'efforts': []},
            ],
        })
        self.write_json('models/codex.json', {
            'runner': 'codex', 'fetched_at': '2026-10-20T00:00:00+00:00', 'method': 'cli',
            'error': None, 'failed_at': None,
            'models': [
                {'id': 'gpt-6-astra', 'name': 'GPT-6 Astra', 'efforts': ['high']},
                {'id': 'codex/claude-sonnet-5-5-low', 'name': 'Sonnet (low)', 'efforts': []},
            ],
        })
        self.write_json('models/agent.json', {
            'runner': 'agent', 'fetched_at': '2026-10-20T00:00:00+00:00', 'method': 'builtin',
            'error': None, 'failed_at': None,
            'models': [{'id': model, 'name': None, 'efforts': ['low', 'high']}
                       for model in ('opus', 'sonnet', 'haiku')],
        })
        # 品質・価格の参考は、同梱の catalog.json の値が更新で変わってもテストが揺れないよう、決まった表で確かめる
        catalog = self.home.parent / f'{self.home.name}-catalog.json'
        catalog.write_text(json.dumps(CATALOG_FIXTURE), encoding='utf-8')
        self.addCleanup(catalog.unlink, missing_ok=True)
        patcher = mock.patch.object(serve, 'CATALOG', catalog)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.saved = []
        self.closed = []
        self.closed_event = threading.Event()

        def on_close():
            self.closed.append(True)
            self.closed_event.set()

        self.server = serve.make_server(
            self.home, 0, 'tok',
            lambda kind, path: self.saved.append((kind, str(path))),
            on_close)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.home, ignore_errors=True)

    def write_json(self, name, data):
        path = self.home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')

    def read_json(self, name):
        return json.loads((self.home / name).read_text(encoding='utf-8'))

    def request(self, method, path, body=None, token='tok', host=None,
                content_type='application/json', raw=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        headers = {'Host': host or f'127.0.0.1:{self.port}'}
        if content_type:
            headers['Content-Type'] = content_type
        if token:
            headers['X-Studio-Token'] = token
        data = raw if raw is not None else (json.dumps(body).encode('utf-8') if body is not None else None)
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        out = response.status, response.read()
        conn.close()
        return out

    def get_state(self):
        code, body = self.request('GET', '/api/state')
        self.assertEqual(code, 200)
        return json.loads(body)

    # --- 画面とセキュリティ ---

    def test_page_embeds_boot_token_and_home(self):
        code, body = self.request('GET', '/')
        self.assertEqual(code, 200)
        self.assertIn(b'studio-boot', body)
        match = re.search(rb'<script type="application/json" id="studio-boot">(.*?)</script>',
                          body, re.S)
        self.assertIsNotNone(match)
        boot = json.loads(match.group(1))
        self.assertEqual(boot['token'], 'tok')
        self.assertEqual(boot['home'], str(self.home))

    def test_wrong_token_and_host_are_refused(self):
        self.assertEqual(self.request('GET', '/api/state', token=None)[0], 403)
        self.assertEqual(self.request('GET', '/api/state', token='nope')[0], 403)
        self.assertEqual(self.request('PUT', '/api/agents/opencode', {'selected': []},
                                      token='nope')[0], 403)
        self.assertEqual(self.request('GET', '/api/state', host='evil.example:80')[0], 403)
        self.assertEqual(self.request('GET', '/', host='evil.example:80')[0], 403)
        self.assertEqual(self.request('GET', '/api/state',
                                      host=f'localhost:{self.port}')[0], 200)

    def test_json_body_rules(self):
        code, _ = self.request('PUT', '/api/agents/opencode', {'selected': []},
                               content_type='text/plain')
        self.assertEqual(code, 415)
        code, _ = self.request('PUT', '/api/agents/opencode',
                               raw=b'{' + b' ' * serve.MAX_BODY + b'}')
        self.assertEqual(code, 413)
        code, _ = self.request('PUT', '/api/agents/opencode', raw=b'not json')
        self.assertEqual(code, 400)

    # --- state ---

    def test_state_reports_runners_routing_templates_and_refs(self):
        state = self.get_state()
        self.assertEqual(set(state['runners']), set(common.RUNNERS))
        opencode = state['runners']['opencode']
        self.assertTrue(opencode['enabled'])
        self.assertEqual(opencode['path'], 'C:/tools/opencode.exe')
        self.assertEqual(opencode['selected'],
                         ['opencode-go/deepseek-v4.1-flash', 'gone-model'])
        self.assertEqual(opencode['missing_selected'], ['gone-model'])
        refs = {item['id']: item['ref'] for item in opencode['models']['list']}
        self.assertEqual(refs['opencode-go/deepseek-v4.1-flash']['quality'], 66)
        self.assertEqual(refs['opencode-go/deepseek-v4.1-flash']['source'], 'catalog')
        self.assertTrue(refs['opencode-go/deepseek-v4.1-flash']['sample'])
        codex_refs = {item['id']: item['ref']
                      for item in state['runners']['codex']['models']['list']}
        self.assertEqual(codex_refs['codex/claude-sonnet-5-5-low']['quality'], 80)
        self.assertEqual(codex_refs['codex/claude-sonnet-5-5-low']['source'], 'catalog')
        agent_refs = {item['id']: item['ref']
                      for item in state['runners']['agent']['models']['list']}
        self.assertEqual(agent_refs['opus']['quality'], 90)
        self.assertIsNone(state['runners']['agy']['models'])
        self.assertEqual(state['routing']['source'], 'default')
        self.assertEqual(state['routing']['table']['name'], 'default')
        self.assertIn('cheap-all-tiers', state['examples'])
        self.assertIsNone(state['application'])
        self.assertEqual(state['application_status'], 'none')
        self.assertTrue(any('gone-model' in warning for warning in state['warnings']))

    def test_state_uses_a_manual_model_without_a_ref(self):
        self.write_json('models/codex.json', {
            'runner': 'codex', 'fetched_at': None, 'method': 'manual',
            'error': None, 'failed_at': None,
            'models': [{'id': 'codex/hand-made', 'name': None, 'efforts': [],
                        'manual': True}],
        })
        item = self.get_state()['runners']['codex']['models']['list'][0]
        self.assertTrue(item['manual'])
        self.assertIsNone(item['ref'])

    # --- agents / models ---

    def test_put_agents_changes_selected_and_keeps_the_rest(self):
        code, body = self.request('PUT', '/api/agents/opencode',
                                  {'selected': ['opencode-go/other'], 'billing': 'api'})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['runner']['selected'], ['opencode-go/other'])
        config = self.read_json('agents.json')['runners']['opencode']
        self.assertEqual(config['models'], ['opencode-go/other'])
        self.assertEqual(config['path'], 'C:/tools/opencode.exe')
        self.assertEqual(config['version'], '1.2.3')
        self.assertEqual(config['billing'], 'api')
        self.assertIn(('agents', str(self.home / 'agents.json')), self.saved)

        code, _ = self.request('PUT', '/api/agents/opencode', {'selected': []})
        self.assertEqual(code, 200)
        self.assertEqual(self.read_json('agents.json')['runners']['opencode']['models'], [])

        code, body = self.request('PUT', '/api/agents/opencode', {'selected': None})
        self.assertEqual(code, 200)
        self.assertIsNone(json.loads(body)['runner']['selected'])
        self.assertNotIn('models', self.read_json('agents.json')['runners']['opencode'])

    def test_put_agents_refuses_selected_items_that_are_not_strings(self):
        before = self.read_json('agents.json')
        for selected in ([{}], [['a']], [''], 'opencode-go/other'):
            code, _ = self.request('PUT', '/api/agents/opencode', {'selected': selected})
            self.assertEqual(code, 400, selected)
        self.assertEqual(self.read_json('agents.json'), before)
        self.assertEqual(self.get_state()['runners']['opencode']['missing_selected'], ['gone-model'])

    def test_agent_cannot_be_disabled(self):
        code, body = self.request('PUT', '/api/agents/agent', {'enabled': False})
        self.assertEqual(code, 400)
        self.assertFalse(json.loads(body)['ok'])
        self.assertTrue(self.read_json('agents.json')['runners']['agent']['enabled'])
        self.assertEqual(self.saved, [])

    def test_unknown_runner_is_refused(self):
        self.assertEqual(self.request('PUT', '/api/agents/nope', {'selected': []})[0], 400)
        self.assertEqual(self.request('POST', '/api/models/nope', {'id': 'x'})[0], 400)

    def test_post_models_adds_a_manual_model(self):
        code, body = self.request('POST', '/api/models/codex',
                                  {'id': 'codex/custom-model', 'efforts': ['low', 'high']})
        self.assertEqual(code, 200)
        data = self.read_json('models/codex.json')
        added = [item for item in data['models'] if item['id'] == 'codex/custom-model']
        self.assertEqual(len(added), 1)
        self.assertTrue(added[0]['manual'])
        self.assertEqual(added[0]['efforts'], ['low', 'high'])
        self.assertIn(('models', str(self.home / 'models' / 'codex.json')), self.saved)

        listed = [item for item in self.get_state()['runners']['codex']['models']['list']
                  if item['id'] == 'codex/custom-model']
        self.assertTrue(listed[0]['manual'])

    # --- routing ---

    def test_put_routing_validates_writes_and_keeps_the_previous(self):
        bad = copy.deepcopy(DEFAULT)
        bad['tiers']['haiku'] = [{'runner': 'codex', 'model': 'gpt-6-astra', 'effort': 'high'}]
        self.assertEqual(self.request('PUT', '/api/routing', bad)[0], 400)
        self.assertFalse((self.home / 'routing.json').exists())

        not_selected = copy.deepcopy(DEFAULT)
        not_selected['name'] = 'not-selected'
        not_selected['tiers']['haiku'] = [
            {'runner': 'opencode', 'model': 'opencode-go/other', 'effort': None},
            {'runner': 'agent', 'model': 'haiku', 'effort': 'max'}]
        self.assertEqual(self.request('PUT', '/api/routing', not_selected)[0], 400)

        disabled = copy.deepcopy(DEFAULT)
        disabled['tiers']['haiku'] = [
            {'runner': 'agy', 'model': 'claude-sonnet-5-5-high', 'effort': None},
            {'runner': 'agent', 'model': 'haiku', 'effort': 'max'}]
        self.assertEqual(self.request('PUT', '/api/routing', disabled)[0], 400)
        self.assertFalse((self.home / 'routing.json').exists())

        good = copy.deepcopy(DEFAULT)
        good['name'] = 'mine'
        code, _ = self.request('PUT', '/api/routing', good)
        self.assertEqual(code, 200)
        self.assertEqual(self.read_json('routing.json')['name'], 'mine')
        self.assertIn(('routing', str(self.home / 'routing.json')), self.saved)
        state = self.get_state()
        self.assertEqual(state['routing']['source'], 'home')
        self.assertEqual(state['routing']['table']['name'], 'mine')

        second = copy.deepcopy(DEFAULT)
        second['name'] = 'second'
        self.assertEqual(self.request('PUT', '/api/routing', second)[0], 200)
        self.assertEqual(self.read_json('routing.prev.json')['name'], 'mine')
        self.assertEqual(self.read_json('routing.json')['name'], 'second')

    # --- templates / examples ---

    def test_put_and_delete_templates(self):
        code, _ = self.request('PUT', '/api/templates/mini', dict(CHEAP, name='mini'))
        self.assertEqual(code, 200)
        self.assertEqual(self.read_json('templates/mini.json')['name'], 'mini')
        self.assertIn('mini', self.get_state()['templates'])
        self.assertIn(('template', str(self.home / 'templates' / 'mini.json')), self.saved)

        self.assertEqual(self.request('PUT', '/api/templates/mini',
                                      dict(CHEAP, name='other'))[0], 400)
        self.assertEqual(self.request('PUT', '/api/templates/broken',
                                      {'name': 'broken', 'description': 'x',
                                       'tiers': {'haiku': []}})[0], 400)
        self.assertEqual(self.request('PUT', '/api/templates/bad/name', CHEAP)[0], 400)

        self.assertEqual(self.request('DELETE', '/api/templates/mini')[0], 200)
        self.assertFalse((self.home / 'templates' / 'mini.json').exists())
        self.assertNotIn('mini', self.get_state()['templates'])

    def test_examples_copy_writes_home_and_refuses_a_second_copy(self):
        code, body = self.request('POST', '/api/examples/cheap-all-tiers/copy', {})
        self.assertEqual(code, 200)
        copied = self.read_json('templates/cheap-all-tiers.json')
        self.assertEqual(copied['name'], 'cheap-all-tiers')
        self.assertIn(('template', str(self.home / 'templates' / 'cheap-all-tiers.json')),
                      self.saved)
        self.assertEqual(self.request('POST', '/api/examples/cheap-all-tiers/copy', {})[0], 409)

        code, _ = self.request('POST', '/api/examples/cheap-all-tiers/copy',
                               {'as': 'cheap-copy'})
        self.assertEqual(code, 200)
        self.assertEqual(self.read_json('templates/cheap-copy.json')['name'], 'cheap-copy')
        self.assertEqual(self.request('POST', '/api/examples/nope/copy', {})[0], 404)

    # --- application ---

    def test_application_apply_and_answer(self):
        self.request('POST', '/api/examples/cheap-all-tiers/copy', {})
        code, body = self.request('PUT', '/api/application', {
            'template': 'cheap-all-tiers', 'until': None, 'on_expire': 'ask',
            'reason': 'test'})
        self.assertEqual(code, 200)
        result = json.loads(body)
        self.assertTrue(result['opus_override'])
        self.assertEqual(self.read_json('overrides.json')['application']['template'],
                         'cheap-all-tiers')
        self.assertIn(('application', str(self.home / 'overrides.json')), self.saved)
        state = self.get_state()
        self.assertEqual(state['application_status'], 'active')
        self.assertEqual(state['application']['template'], 'cheap-all-tiers')

        no_opus = {'name': 'no-opus', 'description': 'only two tiers',
                   'tiers': {'haiku': [{'runner': 'agent', 'model': 'haiku', 'effort': 'max'}],
                             'sonnet': [{'runner': 'agent', 'model': 'sonnet', 'effort': 'high'}]}}
        self.assertEqual(self.request('PUT', '/api/templates/no-opus', no_opus)[0], 200)
        code, body = self.request('PUT', '/api/application', {
            'template': 'no-opus', 'until': None, 'on_expire': 'drop', 'reason': 'switch'})
        self.assertEqual(code, 200)
        self.assertFalse(json.loads(body)['opus_override'])

        code, body = self.request('POST', '/api/application/answer', {'choice': 'stop'})
        self.assertEqual(code, 200)
        self.assertIsNone(json.loads(body)['application'])
        self.assertIsNone(self.read_json('overrides.json')['application'])
        history = (self.home / 'history.jsonl').read_text(encoding='utf-8').splitlines()
        self.assertTrue(any(json.loads(line)['ended_how'] == 'stopped' for line in history))
        self.assertEqual(self.get_state()['application_status'], 'none')

        self.assertEqual(self.request('POST', '/api/application/answer',
                                      {'choice': 'stop'})[0], 400)

    def test_application_is_refused_for_a_bad_template_or_time(self):
        self.request('POST', '/api/examples/cheap-all-tiers/copy', {})
        self.assertEqual(self.request('PUT', '/api/application', {
            'template': 'missing', 'until': None, 'on_expire': 'ask',
            'reason': 'x'})[0], 400)
        self.assertEqual(self.request('PUT', '/api/application', {
            'template': 'cheap-all-tiers', 'until': '2000-01-01T00:00:00+00:00',
            'on_expire': 'ask', 'reason': 'x'})[0], 400)
        self.assertEqual(self.request('PUT', '/api/application', {
            'template': 'cheap-all-tiers', 'until': None, 'on_expire': 'nope',
            'reason': 'x'})[0], 400)

    def test_delete_a_template_in_use_is_refused(self):
        self.request('POST', '/api/examples/cheap-all-tiers/copy', {})
        self.request('PUT', '/api/application', {
            'template': 'cheap-all-tiers', 'until': None, 'on_expire': 'ask',
            'reason': 'x'})
        self.assertEqual(self.request('DELETE', '/api/templates/cheap-all-tiers')[0], 409)
        self.assertTrue((self.home / 'templates' / 'cheap-all-tiers.json').is_file())

    # --- 開発フローのテンプレート ---

    def bundled_workflow(self, name='jev-routed'):
        return json.loads((SKILL / 'workflows' / f'{name}.json').read_text(encoding='utf-8'))

    def test_state_reports_flow_components_and_bundled_workflows(self):
        flow_state = self.get_state()['flow']
        self.assertEqual(flow_state['stages'][0], 'understand')
        self.assertIn('deep', flow_state['when'])
        self.assertEqual(flow_state['optional'], ['ui-design', 'marketing'])
        self.assertEqual(flow_state['optional_after'], {'ui-design': 'design', 'marketing': 'understand'})
        self.assertNotIn('finish', flow_state['optional_places'])
        self.assertEqual(flow_state['default'], 'jev-routed')
        self.assertEqual(flow_state['errors'], [])
        review = flow_state['components']['review.code-review']
        self.assertEqual(review['source'], 'bundled')
        self.assertFalse(review['fallback'])
        self.assertTrue(flow_state['components']['review.builtin']['fallback'])
        routed = flow_state['workflows']['jev-routed']
        self.assertEqual(routed['source'], 'bundled')
        self.assertEqual(routed['errors'], [])
        self.assertEqual(routed['data']['fix']['max_rework'], 2)

    def test_put_workflow_writes_home_and_delete_falls_back_to_bundled(self):
        workflow = self.bundled_workflow()
        workflow['fix']['max_rework'] = 3
        workflow['stages']['review'] = ['review.builtin']
        code, body = self.request('PUT', '/api/workflows/jev-routed', workflow)
        self.assertEqual(code, 200, body)
        written = self.read_json('workflows/jev-routed.json')
        self.assertEqual(written['fix']['max_rework'], 3)
        self.assertEqual(self.saved[-1][0], 'workflow')
        routed = self.get_state()['flow']['workflows']['jev-routed']
        self.assertEqual(routed['source'], 'home')
        self.assertTrue(routed['bundled'])
        code, body = self.request('DELETE', '/api/workflows/jev-routed')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['fallback'], 'bundled')
        self.assertFalse((self.home / 'workflows' / 'jev-routed.json').exists())
        self.assertEqual(self.get_state()['flow']['workflows']['jev-routed']['source'], 'bundled')

    def test_put_workflow_with_a_new_name_and_delete_leaves_no_fallback(self):
        workflow = self.bundled_workflow()
        del workflow['name']
        code, body = self.request('PUT', '/api/workflows/mine', workflow)
        self.assertEqual(code, 200, body)
        self.assertEqual(self.read_json('workflows/mine.json')['name'], 'mine')
        code, body = self.request('DELETE', '/api/workflows/mine')
        self.assertEqual(json.loads(body)['fallback'], None)
        self.assertNotIn('mine', self.get_state()['flow']['workflows'])

    def test_put_workflow_checks_optional_stages(self):
        workflow = self.bundled_workflow()
        workflow['optional'] = [{'stage': 'ui-design', 'after': 'design', 'components': ['review.builtin']}]
        code, body = self.request('PUT', '/api/workflows/jev-routed', workflow)
        self.assertEqual(code, 400)
        self.assertIn('ui-design after design: review.builtin does not serve ui-design', body.decode('utf-8'))
        self.assertFalse((self.home / 'workflows' / 'jev-routed.json').exists())

    def test_put_workflow_refuses_invalid_ones_and_writes_nothing(self):
        workflow = self.bundled_workflow()
        workflow['stages']['review'] = ['review.code-review']
        code, body = self.request('PUT', '/api/workflows/jev-routed', workflow)
        self.assertEqual(code, 400)
        self.assertIn('the last component must be builtin', json.loads(body)['error'])
        workflow = self.bundled_workflow()
        workflow['build']['external_memory'] = 'full'
        self.assertEqual(self.request('PUT', '/api/workflows/jev-routed', workflow)[0], 400)
        workflow = self.bundled_workflow()
        self.assertEqual(self.request('PUT', '/api/workflows/other', workflow)[0], 400)
        self.assertEqual(self.request('PUT', '/api/workflows/Bad_Name', workflow)[0], 400)
        self.assertEqual(self.request('PUT', '/api/workflows/jev-routed', ['x'])[0], 400)
        self.assertFalse((self.home / 'workflows').exists())
        self.assertEqual(self.saved, [])
        self.assertEqual(self.request('DELETE', '/api/workflows/jev-routed')[0], 404)
        self.assertEqual(self.request('DELETE', '/api/workflows/..')[0], 400)

    def test_home_components_can_be_placed_in_a_workflow(self):
        self.write_json('components/review.mine.json', {
            'id': 'review.mine', 'stages': ['review'],
            'title': {'ja': '自分のレビュー', 'en': 'My review'},
            'invoke': {'kind': 'skill', 'name': 'mine'}, 'doc': 'appendix/review.md',
            'where': 'subagent', 'memory': 'none', 'when': 'always', 'cost': 'mid',
            'requires': {'skills': ['mine']}, 'forbid': [], 'interactive': False,
            'calls_back': False, 'license': 'MIT', 'link': 'local'})
        components = self.get_state()['flow']['components']
        self.assertEqual(components['review.mine']['source'], 'home')
        workflow = self.bundled_workflow()
        workflow['stages']['review'] = [{'id': 'review.mine', 'when': 'deep'}, 'review.builtin']
        code, body = self.request('PUT', '/api/workflows/jev-routed', workflow)
        self.assertEqual(code, 200, body)
        # 同梱の id を使う home の部品は誤りとして返り、その間は保存も断る
        self.write_json('components/review.builtin.json',
                        json.loads((SKILL / 'components' / 'review.builtin.json').read_text(encoding='utf-8')))
        flow_state = self.get_state()['flow']
        self.assertIn('a bundled component already has this id', ' '.join(flow_state['errors']))
        self.assertEqual(self.request('PUT', '/api/workflows/jev-routed', workflow)[0], 400)

    # --- close ---

    def test_close_calls_the_callback_after_the_response(self):
        code, body = self.request('POST', '/api/close')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body), {'ok': True})
        # The handler calls the callback after it has flushed the response, so the client can
        # see the response first; wait for the callback instead of checking at once.
        self.assertTrue(self.closed_event.wait(5), 'the close callback was not called')
        self.assertEqual(self.closed, [True])


if __name__ == '__main__':
    unittest.main()
