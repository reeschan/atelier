import datetime
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'templates.py')
sys.path.insert(0, HERE)
import templates  # noqa: E402

HOME_KEYS = ('PBI_HOME', 'PBI_TEMPLATES_HOME', 'XDG_CONFIG_HOME')

NOW = datetime.datetime.fromisoformat('2026-10-22T09:00:00+09:00')
UNTIL = '2026-10-22T23:59:00+09:00'
APPLICATION = {
    'id': 'a-20261016T000000', 'template': 'cheap-all-tiers', 'until': UNTIL, 'on_expire': 'ask',
    'reason': '一時措置', 'started_at': '2026-10-16T09:00:00+09:00', 'started_via': 'chat',
}


def template(name='cheap-all-tiers', **step_overrides):
    step = {'runner': 'opencode', 'model': 'opencode-go/deepseek-v4.1-flash', 'effort': 'max'}
    step.update(step_overrides)
    return {'name': name, 'description': '1 行の説明',
            'tiers': {'haiku': [step], 'opus': [{'runner': 'agent', 'model': 'opus'}]}}


def application(**overrides):
    app = dict(APPLICATION)
    app.update(overrides)
    return app


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def run(*args, env=None, script=SCRIPT):
    e = {k: v for k, v in os.environ.items() if k not in HOME_KEYS}
    e.update(env or {})
    return subprocess.run([sys.executable, script, *args], capture_output=True, text=True, encoding='utf-8', env=e)


def read_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def read_history(home):
    path = os.path.join(home, 'history.jsonl')
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]


class ValidateTest(unittest.TestCase):
    def test_good_template_has_no_problems(self):
        self.assertEqual(templates.validate(template(), 'cheap-all-tiers'), [])
        self.assertEqual(templates.validate(template()), [])

    def test_bad_runner(self):
        problems = templates.validate(template(runner='gemini'), 'cheap-all-tiers')
        self.assertTrue(any('runner' in p for p in problems), problems)

    def test_dsh_runner_is_valid(self):
        t = template(runner='dsh', model='deepseek-official/deepseek-flash', effort='high')
        self.assertEqual(templates.validate(t, 'cheap-all-tiers'), [])

    def test_unknown_tier_key(self):
        t = template()
        t['tiers']['mini'] = [{'runner': 'agy', 'model': 'claude-sonnet-5-5-low'}]
        problems = templates.validate(t, 'cheap-all-tiers')
        self.assertTrue(any('mini' in p for p in problems), problems)

    def test_name_and_filename_mismatch(self):
        problems = templates.validate(template(), 'other')
        self.assertTrue(any('ファイル名' in p for p in problems), problems)

    def test_invalid_name(self):
        self.assertTrue(templates.validate(template(name='Bad')))

    def test_extra_step_key(self):
        t = template()
        t['tiers']['haiku'][0]['note'] = 'x'
        problems = templates.validate(t, 'cheap-all-tiers')
        self.assertTrue(any('余計なキー' in p for p in problems), problems)

    def test_routing_json_shape_is_accepted(self):
        t = template()
        t['$schema_note'] = 'routing.json と同じ形'
        t['tiers']['haiku'][0]['until'] = '2026-10-23'
        self.assertEqual(templates.validate(t, 'cheap-all-tiers'), [])

    def test_step_until_must_be_a_date(self):
        t = template()
        t['tiers']['haiku'][0]['until'] = 'next week'
        problems = templates.validate(t, 'cheap-all-tiers')
        self.assertTrue(any('until' in p for p in problems), problems)

    def test_extra_top_key(self):
        t = template()
        t['synced_at'] = '2026-10-16T09:00:00+09:00'
        problems = templates.validate(t, 'cheap-all-tiers')
        self.assertTrue(any('余計なキー' in p for p in problems), problems)

    def test_empty_tiers_and_empty_steps(self):
        t = template()
        t['tiers'] = {}
        self.assertTrue(templates.validate(t, 'cheap-all-tiers'))
        t = template()
        t['tiers']['haiku'] = []
        self.assertTrue(templates.validate(t, 'cheap-all-tiers'))

    def test_missing_effort_is_ok(self):
        t = {'name': 'cheap-all-tiers', 'description': '',
             'tiers': {'opus': [{'runner': 'agent', 'model': 'opus'}]}}
        self.assertEqual(templates.validate(t, 'cheap-all-tiers'), [])

    def test_bad_effort_type(self):
        self.assertTrue(templates.validate(template(effort=3), 'cheap-all-tiers'))


FREE = {'runner': 'opencode', 'model': 'opencode/deepseek-v4.1-flash-free', 'effort': 'max'}
GLM = {'runner': 'opencode', 'model': 'opencode/glm-5.3-flash-free', 'effort': 'max'}


def rotate_template(*steps):
    return {'name': 'rotate-free-then-paid', 'description': '', 'tiers': {'haiku': list(steps)}}


class RotateValidateTest(unittest.TestCase):
    """交互グループ {"rotate": [手順, ...]} の検査。"""

    def problems(self, *steps):
        return templates.validate(rotate_template(*steps), 'rotate-free-then-paid')

    def test_good_group_is_ok(self):
        self.assertEqual(self.problems({'rotate': [FREE, GLM]}, {'rotate': [FREE, {**GLM, 'until': '2026-10-23'}]}), [])

    def test_group_with_plain_step_is_ok(self):
        self.assertEqual(self.problems(FREE, {'rotate': [FREE, GLM]}), [])

    def test_fewer_than_two_members(self):
        problems = self.problems({'rotate': [FREE]})
        self.assertTrue(any('2 つ以上' in p for p in problems), problems)

    def test_not_a_list(self):
        self.assertTrue(self.problems({'rotate': FREE}))

    def test_agent_member_is_rejected(self):
        problems = self.problems({'rotate': [FREE, {'runner': 'agent', 'model': 'haiku'}]})
        self.assertTrue(any('agent' in p for p in problems), problems)

    def test_nested_group_is_rejected(self):
        problems = self.problems({'rotate': [FREE, {'rotate': [FREE, GLM]}]})
        self.assertTrue(any('入れ子' in p for p in problems), problems)

    def test_bad_member_is_reported_with_its_place(self):
        problems = self.problems({'rotate': [FREE, {**GLM, 'model': ''}]})
        self.assertTrue(any('haiku[0].rotate[1]' in p for p in problems), problems)

    def test_extra_key_on_group(self):
        problems = self.problems({'rotate': [FREE, GLM], 'note': 'x'})
        self.assertTrue(any('余計なキー' in p for p in problems), problems)

    def test_check_command_reports_ng(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'rotate-free-then-paid.json'), rotate_template({'rotate': [FREE]}))
            out = run('check', '--home', d)
        self.assertEqual(out.returncode, 1)
        self.assertIn('ng rotate-free-then-paid', out.stdout)

    def test_check_command_reports_ok(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'rotate-free-then-paid.json'), rotate_template({'rotate': [FREE, GLM]}))
            out = run('check', '--home', d)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn('ok rotate-free-then-paid', out.stdout)


class StateOfTest(unittest.TestCase):
    def test_none(self):
        self.assertEqual(templates.state_of(None, NOW), 'none')

    def test_active_before_until(self):
        self.assertEqual(templates.state_of(application(until='2026-10-22T10:00:00+09:00'), NOW), 'active')

    def test_active_without_until(self):
        self.assertEqual(templates.state_of(application(until=None), NOW), 'active')

    def test_expired_ask(self):
        self.assertEqual(templates.state_of(application(until='2026-10-22T08:59:00+09:00'), NOW), 'expired_ask')

    def test_expired_drop(self):
        app = application(until='2026-10-22T08:59:00+09:00', on_expire='drop')
        self.assertEqual(templates.state_of(app, NOW), 'expired_drop')

    def test_now_equals_until_is_expired(self):
        self.assertEqual(templates.state_of(application(until=NOW.isoformat()), NOW), 'expired_ask')


class ResolveHomeTest(unittest.TestCase):
    """--home → PBI_HOME → PBI_TEMPLATES_HOME → XDG_CONFIG_HOME → ~/.config。"""

    def resolve(self, arg=None, **env):
        cleared = {k: v for k, v in os.environ.items() if k not in HOME_KEYS}
        cleared.update(env)
        with mock.patch.dict(os.environ, cleared, clear=True):
            return templates.resolve_home(arg)

    def test_arg_wins_over_env(self):
        self.assertEqual(self.resolve('arg-home', PBI_HOME='pbi-home', PBI_TEMPLATES_HOME='old-home'),
                         Path('arg-home'))

    def test_pbi_home_is_used(self):
        self.assertEqual(self.resolve(None, PBI_HOME='pbi-home'), Path('pbi-home'))

    def test_old_name_is_still_read(self):
        self.assertEqual(self.resolve(None, PBI_TEMPLATES_HOME='old-home'), Path('old-home'))

    def test_pbi_home_wins_over_old_name(self):
        self.assertEqual(self.resolve(None, PBI_HOME='pbi-home', PBI_TEMPLATES_HOME='old-home'),
                         Path('pbi-home'))

    def test_xdg_config_home(self):
        self.assertEqual(self.resolve(None, XDG_CONFIG_HOME='xdg'),
                         Path('xdg') / 'pbi-orchestration-jev')

    def test_default_is_config_under_home(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(Path, 'home', return_value=Path(d)):
            self.assertEqual(self.resolve(None), Path(d) / '.config' / 'pbi-orchestration-jev')

    def test_home_option_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            out = run('check', '--home', d)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(out.stdout.strip(), 'ok cheap-all-tiers')

    def test_env_home_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            out = run('check', env={'PBI_HOME': d})
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(out.stdout.strip(), 'ok cheap-all-tiers')

    def test_old_env_name_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            out = run('check', env={'PBI_TEMPLATES_HOME': d})
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(out.stdout.strip(), 'ok cheap-all-tiers')


class LoadTest(unittest.TestCase):
    def test_load_template(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            self.assertEqual(templates.load_template(Path(d), 'cheap-all-tiers')['name'], 'cheap-all-tiers')
            self.assertIsNone(templates.load_template(Path(d), 'nope'))
            write_text(os.path.join(d, 'templates', 'broken.json'), '{oops')
            self.assertIsNone(templates.load_template(Path(d), 'broken'))
            write_json(os.path.join(d, 'templates', 'other.json'), template())
            self.assertIsNone(templates.load_template(Path(d), 'other'))

    def test_load_all_keeps_only_valid(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            write_json(os.path.join(d, 'templates', 'bad.json'), template(name='bad', runner='gemini'))
            write_text(os.path.join(d, 'templates', 'broken.json'), 'not json')
            self.assertEqual(list(templates.load_all(Path(d))), ['cheap-all-tiers'])

    def test_load_overrides_missing_or_broken_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            empty = {'synced_at': None, 'updated_at': None, 'application': None}
            self.assertEqual(templates.load_overrides(Path(d)), empty)
            write_text(os.path.join(d, 'overrides.json'), '{oops')
            self.assertEqual(templates.load_overrides(Path(d)), empty)

    def test_load_overrides_reads_application(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'overrides.json'),
                       {'synced_at': '2026-10-16T09:00:00+09:00', 'application': application()})
            overrides = templates.load_overrides(Path(d))
            self.assertEqual(overrides['application']['template'], 'cheap-all-tiers')

    def test_load_overrides_reads_updated_at(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'overrides.json'),
                       {'synced_at': '2026-10-16T09:00:00+09:00',
                        'updated_at': '2026-10-20T09:00:00+09:00', 'application': application()})
            self.assertEqual(templates.load_overrides(Path(d))['updated_at'], '2026-10-20T09:00:00+09:00')

    def test_load_overrides_old_form_has_no_updated_at(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'overrides.json'),
                       {'synced_at': '2026-10-16T09:00:00+09:00', 'application': application()})
            self.assertIsNone(templates.load_overrides(Path(d))['updated_at'])


class SyncTest(unittest.TestCase):
    def sync(self, d, state, now):
        state_path = os.path.join(d, 'state.json')
        if state is not None:
            write_json(state_path, state)
        out = run('sync', '--home', d, '--state', state_path, '--now', now)
        self.assertEqual(out.returncode, 0, out.stderr)
        with open(os.path.join(d, 'overrides.json'), encoding='utf-8') as f:
            overrides = json.load(f)
        return json.loads(out.stdout), overrides

    def test_active_keeps_application(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            out, overrides = self.sync(d, {'application': application(), 'updated_at': '2026-10-16T09:00:00+09:00'},
                                       '2026-10-22T09:00:00+09:00')
            self.assertEqual(out['status'], 'active')
            self.assertEqual(out['writes'], [])
            self.assertEqual(out['application']['template'], 'cheap-all-tiers')
            self.assertEqual(overrides['synced_at'], '2026-10-22T09:00:00+09:00')
            self.assertEqual(overrides['application']['id'], APPLICATION['id'])

    def test_none_writes_empty_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            out, overrides = self.sync(d, {}, '2026-10-22T09:00:00+09:00')
            self.assertEqual(out['status'], 'none')
            self.assertEqual(out['writes'], [])
            self.assertIsNone(out['application'])
            self.assertIsNone(overrides['application'])
            self.assertEqual(overrides['synced_at'], '2026-10-22T09:00:00+09:00')

    def test_missing_state_file_is_no_application(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            out, _ = self.sync(d, None, '2026-10-22T09:00:00+09:00')
            self.assertEqual(out['status'], 'none')

    def test_expired_ask_keeps_application_and_no_writes(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            out, overrides = self.sync(d, {'application': application(), 'updated_at': 'x'},
                                       '2026-10-23T00:00:00+09:00')
            self.assertEqual(out['status'], 'expired_ask')
            self.assertEqual(out['writes'], [])
            self.assertEqual(overrides['application']['template'], 'cheap-all-tiers')
            self.assertEqual(overrides['synced_at'], '2026-10-23T00:00:00+09:00')

    def test_expired_drop_moves_to_history_and_clears(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            app = application(on_expire='drop')
            out, overrides = self.sync(d, {'application': app, 'updated_at': 'x'}, '2026-10-23T00:00:00+09:00')
            self.assertEqual(out['status'], 'expired_drop')
            self.assertEqual(len(out['writes']), 2)
            history, current = out['writes']
            self.assertEqual(history['op'], 'set')
            self.assertEqual(history['collection'], 'history')
            self.assertEqual(history['doc_id'], app['id'])
            self.assertEqual(history['data']['ended_at'], UNTIL)
            self.assertEqual(history['data']['ended_how'], 'expired')
            self.assertEqual(history['data']['template'], 'cheap-all-tiers')
            self.assertEqual(current['collection'], 'state')
            self.assertEqual(current['doc_id'], 'current')
            self.assertEqual(current['data'], {'application': None, 'updated_at': '2026-10-23T00:00:00+09:00'})
            self.assertIsNone(overrides['application'])

    def test_unknown_template_keeps_application(self):
        with tempfile.TemporaryDirectory() as d:
            out, overrides = self.sync(d, {'application': application(template='ghost'), 'updated_at': 'x'},
                                       '2026-10-23T00:00:00+09:00')
            self.assertEqual(out['status'], 'unknown_template')
            self.assertEqual(out['writes'], [])
            self.assertEqual(overrides['application']['template'], 'ghost')

    def test_unknown_template_when_local_file_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template(runner='gemini'))
            out, overrides = self.sync(d, {'application': application(), 'updated_at': 'x'},
                                       '2026-10-22T09:00:00+09:00')
            self.assertEqual(out['status'], 'unknown_template')
            self.assertEqual(out['writes'], [])
            self.assertEqual(overrides['application']['template'], 'cheap-all-tiers')

    def test_invalid_state_json_is_exit_2(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            state_path = os.path.join(d, 'state.json')
            write_text(state_path, '{oops')
            out = run('sync', '--home', d, '--state', state_path)
            self.assertEqual(out.returncode, 2)
            self.assertTrue(out.stderr.strip())

    def test_naive_now_is_exit_2(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            state_path = os.path.join(d, 'state.json')
            write_json(state_path, {})
            out = run('sync', '--home', d, '--state', state_path, '--now', '2026-10-22')
            self.assertEqual(out.returncode, 2)
            self.assertTrue(out.stderr.strip())

    def test_local_newer_wins_and_pushes_state(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            local = application(id='a-20261021T000000')
            write_json(os.path.join(d, 'overrides.json'),
                       {'synced_at': '2026-10-21T09:00:00+09:00',
                        'updated_at': '2026-10-21T09:00:00+09:00', 'application': local})
            out, overrides = self.sync(d, {'application': application(),
                                           'updated_at': '2026-10-20T09:00:00+09:00'},
                                       '2026-10-22T09:00:00+09:00')
            self.assertEqual(out['status'], 'active')
            self.assertEqual(len(out['writes']), 1)
            write = out['writes'][0]
            self.assertEqual((write['op'], write['collection'], write['doc_id']), ('set', 'state', 'current'))
            self.assertEqual(write['data'],
                             {'application': local, 'updated_at': '2026-10-21T09:00:00+09:00'})
            self.assertEqual(overrides['application']['id'], 'a-20261021T000000')
            self.assertEqual(overrides['updated_at'], '2026-10-21T09:00:00+09:00')
            self.assertEqual(overrides['synced_at'], '2026-10-22T09:00:00+09:00')

    def test_local_newer_with_same_application_has_no_writes(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            write_json(os.path.join(d, 'overrides.json'),
                       {'synced_at': '2026-10-21T09:00:00+09:00',
                        'updated_at': '2026-10-21T09:00:00+09:00', 'application': application()})
            out, overrides = self.sync(d, {'application': application(),
                                           'updated_at': '2026-10-20T09:00:00+09:00'},
                                       '2026-10-22T09:00:00+09:00')
            self.assertEqual(out['writes'], [])
            self.assertEqual(overrides['updated_at'], '2026-10-21T09:00:00+09:00')

    def test_same_updated_at_takes_local(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            local = application(id='a-20261021T000000')
            write_json(os.path.join(d, 'overrides.json'),
                       {'synced_at': '2026-10-21T09:00:00+09:00',
                        'updated_at': '2026-10-21T09:00:00+09:00', 'application': local})
            out, overrides = self.sync(d, {'application': application(),
                                           'updated_at': '2026-10-21T09:00:00+09:00'},
                                       '2026-10-22T09:00:00+09:00')
            self.assertEqual(out['writes'][0]['data']['application'], local)
            self.assertEqual(overrides['application']['id'], 'a-20261021T000000')

    def test_web_newer_wins_and_copies_updated_at(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            write_json(os.path.join(d, 'overrides.json'),
                       {'synced_at': '2026-10-20T09:00:00+09:00',
                        'updated_at': '2026-10-20T09:00:00+09:00', 'application': application(id='a-local')})
            out, overrides = self.sync(d, {'application': application(),
                                           'updated_at': '2026-10-21T09:00:00+09:00'},
                                       '2026-10-22T09:00:00+09:00')
            self.assertEqual(out['writes'], [])
            self.assertEqual(overrides['application']['id'], APPLICATION['id'])
            self.assertEqual(overrides['updated_at'], '2026-10-21T09:00:00+09:00')

    def test_local_without_updated_at_takes_web(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            write_json(os.path.join(d, 'overrides.json'),
                       {'synced_at': '2026-10-20T09:00:00+09:00', 'application': application(id='a-local')})
            out, overrides = self.sync(d, {'application': application(),
                                           'updated_at': '2026-10-21T09:00:00+09:00'},
                                       '2026-10-22T09:00:00+09:00')
            self.assertEqual(out['application']['id'], APPLICATION['id'])
            self.assertEqual(overrides['application']['id'], APPLICATION['id'])
            self.assertEqual(overrides['updated_at'], '2026-10-21T09:00:00+09:00')

    def test_local_none_pushes_empty_state(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            write_json(os.path.join(d, 'overrides.json'),
                       {'synced_at': '2026-10-21T09:00:00+09:00',
                        'updated_at': '2026-10-21T09:00:00+09:00', 'application': None})
            out, overrides = self.sync(d, {'application': application(),
                                           'updated_at': '2026-10-20T09:00:00+09:00'},
                                       '2026-10-22T09:00:00+09:00')
            self.assertEqual(out['status'], 'none')
            self.assertEqual(out['writes'],
                             [{'op': 'set', 'collection': 'state', 'doc_id': 'current',
                               'data': {'application': None, 'updated_at': '2026-10-21T09:00:00+09:00'}}])
            self.assertIsNone(overrides['application'])


class DueTest(unittest.TestCase):
    def due(self, d, state, now, *extra):
        state_path = os.path.join(d, 'state.json')
        if state is not None:
            write_json(state_path, state)
        out = run('due', '--home', d, '--state', state_path, '--now', now, *extra)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_inside_window(self):
        with tempfile.TemporaryDirectory() as d:
            r = self.due(d, {'application': application()}, '2026-10-22T09:00:00+09:00')
            self.assertEqual(r['due']['template'], 'cheap-all-tiers')
            self.assertTrue(14.9 < r['hours_left'] < 15.0, r)

    def test_outside_window(self):
        with tempfile.TemporaryDirectory() as d:
            r = self.due(d, {'application': application()}, '2026-10-21T09:00:00+09:00')
            self.assertEqual(r, {'due': None})

    def test_wider_window_finds_due(self):
        with tempfile.TemporaryDirectory() as d:
            r = self.due(d, {'application': application()}, '2026-10-21T09:00:00+09:00', '--within-hours', '40')
            self.assertEqual(r['due']['template'], 'cheap-all-tiers')

    def test_past_until_is_not_due(self):
        with tempfile.TemporaryDirectory() as d:
            r = self.due(d, {'application': application()}, '2026-10-23T00:00:00+09:00')
            self.assertEqual(r, {'due': None})

    def test_without_until_or_application(self):
        with tempfile.TemporaryDirectory() as d:
            r = self.due(d, {'application': application(until=None)}, '2026-10-22T09:00:00+09:00')
            self.assertEqual(r, {'due': None})
            r = self.due(d, {}, '2026-10-22T09:00:00+09:00')
            self.assertEqual(r, {'due': None})


class CheckTest(unittest.TestCase):
    def test_ok_and_ng_lines_with_exit_1(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'good.json'), template(name='good'))
            write_json(os.path.join(d, 'templates', 'bad.json'), template(name='bad', runner='gemini'))
            out = run('check', '--home', d)
            self.assertEqual(out.returncode, 1)
            lines = out.stdout.splitlines()
            self.assertIn('ok good', lines)
            self.assertTrue(any(line.startswith('ng bad:') for line in lines), lines)

    def test_all_good_is_exit_0(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            out = run('check', '--home', d)
            self.assertEqual(out.returncode, 0)
            self.assertEqual(out.stdout.splitlines(), ['ok cheap-all-tiers'])

    def test_named_check_and_missing(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            out = run('check', 'cheap-all-tiers', '--home', d)
            self.assertEqual(out.returncode, 0)
            self.assertEqual(out.stdout.splitlines(), ['ok cheap-all-tiers'])
            out = run('check', 'nope', '--home', d)
            self.assertEqual(out.returncode, 1)
            self.assertTrue(out.stdout.splitlines()[0].startswith('ng nope:'), out.stdout)

    def test_broken_json_is_ng(self):
        with tempfile.TemporaryDirectory() as d:
            write_text(os.path.join(d, 'templates', 'broken.json'), '{oops')
            out = run('check', '--home', d)
            self.assertEqual(out.returncode, 1)
            self.assertTrue(out.stdout.splitlines()[0].startswith('ng broken:'), out.stdout)


class ApplyTest(unittest.TestCase):
    def apply(self, d, state, template_name='cheap-all-tiers', until='2026-11-05T23:59:00+09:00',
              on_expire='ask', reason='テスト', now='2026-10-22T09:00:00+09:00', via=None):
        state_path = os.path.join(d, 'state.json')
        if state is not None:
            write_json(state_path, state)
        args = ['apply', '--home', d, '--state', state_path, '--now', now,
                '--template', template_name, '--until', until,
                '--on-expire', on_expire, '--reason', reason]
        if via:
            args += ['--via', via]
        return run(*args)

    def test_no_previous_application_writes_only_state(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            out = self.apply(d, {})
            self.assertEqual(out.returncode, 0, out.stderr)
            result = json.loads(out.stdout)
            self.assertEqual(len(result['writes']), 1)
            write = result['writes'][0]
            self.assertEqual((write['op'], write['collection'], write['doc_id']), ('set', 'state', 'current'))
            self.assertEqual(write['data']['updated_at'], '2026-10-22T09:00:00+09:00')
            self.assertEqual(write['data']['application'], result['application'])
            self.assertNotIn('if_version', write)

    def test_new_application_fields(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            out = self.apply(d, {}, reason='一時措置')
            self.assertEqual(json.loads(out.stdout)['application'], {
                'id': 'a-20261022T000000', 'template': 'cheap-all-tiers',
                'until': '2026-11-05T23:59:00+09:00', 'on_expire': 'ask', 'reason': '一時措置',
                'started_at': '2026-10-22T09:00:00+09:00', 'started_via': 'chat'})

    def test_missing_state_file_is_no_previous(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            out = self.apply(d, None)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual([w['collection'] for w in json.loads(out.stdout)['writes']], ['state'])

    def test_active_previous_moves_to_history_replaced(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            app = application()
            out = self.apply(d, {'application': app, 'updated_at': 'x'})
            self.assertEqual(out.returncode, 0, out.stderr)
            history, current = json.loads(out.stdout)['writes']
            self.assertEqual((history['op'], history['collection'], history['doc_id']),
                             ('set', 'history', app['id']))
            self.assertEqual(history['data']['ended_how'], 'replaced')
            self.assertEqual(history['data']['ended_at'], '2026-10-22T09:00:00+09:00')
            self.assertEqual(history['data']['template'], 'cheap-all-tiers')
            self.assertEqual(history['data']['started_via'], 'chat')
            self.assertEqual(current['collection'], 'state')
            self.assertEqual(current['data']['application']['template'], 'cheap-all-tiers')

    def test_expired_ask_previous_moves_to_history_replaced(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            out = self.apply(d, {'application': application(), 'updated_at': 'x'},
                             now='2026-10-23T00:00:00+09:00')
            history = json.loads(out.stdout)['writes'][0]
            self.assertEqual(history['data']['ended_how'], 'replaced')
            self.assertEqual(history['data']['ended_at'], '2026-10-23T00:00:00+09:00')

    def test_expired_drop_previous_moves_to_history_expired(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            app = application(on_expire='drop')
            out = self.apply(d, {'application': app, 'updated_at': 'x'},
                             now='2026-10-23T00:00:00+09:00')
            history = json.loads(out.stdout)['writes'][0]
            self.assertEqual(history['data']['ended_how'], 'expired')
            self.assertEqual(history['data']['ended_at'], UNTIL)

    def test_until_none_has_no_expiry(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            out = self.apply(d, {}, until='none')
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIsNone(json.loads(out.stdout)['application']['until'])

    def test_via_web(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            out = self.apply(d, {}, via='web')
            self.assertEqual(json.loads(out.stdout)['application']['started_via'], 'web')

    def test_missing_template_is_exit_2(self):
        with tempfile.TemporaryDirectory() as d:
            out = self.apply(d, {}, template_name='ghost')
            self.assertEqual(out.returncode, 2)
            self.assertTrue(out.stderr.strip())
            self.assertEqual(out.stdout, '')

    def test_invalid_template_is_exit_2(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template(runner='gemini'))
            out = self.apply(d, {})
            self.assertEqual(out.returncode, 2)
            self.assertTrue(out.stderr.strip())

    def test_past_or_equal_until_is_exit_2(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            for until in ('2026-10-22T08:59:00+09:00', '2026-10-22T09:00:00+09:00'):
                out = self.apply(d, {}, until=until)
                self.assertEqual(out.returncode, 2, until)
                self.assertTrue(out.stderr.strip())

    def test_bad_on_expire_is_exit_2(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            out = self.apply(d, {}, on_expire='later')
            self.assertEqual(out.returncode, 2)
            self.assertTrue(out.stderr.strip())


class AnswerTest(unittest.TestCase):
    def answer(self, d, state, choice, until=None, now='2026-10-22T09:00:00+09:00'):
        state_path = os.path.join(d, 'state.json')
        if state is not None:
            write_json(state_path, state)
        args = ['answer', '--home', d, '--state', state_path, '--now', now, '--choice', choice]
        if until is not None:
            args += ['--until', until]
        return run(*args)

    def test_extend_sets_new_until(self):
        with tempfile.TemporaryDirectory() as d:
            out = self.answer(d, {'application': application()}, 'extend',
                              until='2026-10-25T23:59:00+09:00')
            self.assertEqual(out.returncode, 0, out.stderr)
            result = json.loads(out.stdout)
            self.assertEqual(result['application']['until'], '2026-10-25T23:59:00+09:00')
            self.assertEqual(result['application']['id'], APPLICATION['id'])
            self.assertEqual(len(result['writes']), 1)
            write = result['writes'][0]
            self.assertEqual((write['op'], write['collection'], write['doc_id']), ('set', 'state', 'current'))
            self.assertEqual(write['data'],
                             {'application': result['application'], 'updated_at': '2026-10-22T09:00:00+09:00'})

    def test_forever_clears_until(self):
        with tempfile.TemporaryDirectory() as d:
            out = self.answer(d, {'application': application()}, 'forever')
            self.assertEqual(out.returncode, 0, out.stderr)
            result = json.loads(out.stdout)
            self.assertIsNone(result['application']['until'])
            self.assertEqual(len(result['writes']), 1)
            self.assertEqual(result['writes'][0]['data']['application'], result['application'])

    def test_stop_active_is_stopped(self):
        with tempfile.TemporaryDirectory() as d:
            out = self.answer(d, {'application': application()}, 'stop')
            self.assertEqual(out.returncode, 0, out.stderr)
            result = json.loads(out.stdout)
            self.assertIsNone(result['application'])
            history, current = result['writes']
            self.assertEqual((history['collection'], history['doc_id']), ('history', APPLICATION['id']))
            self.assertEqual(history['data']['ended_how'], 'stopped')
            self.assertEqual(history['data']['ended_at'], '2026-10-22T09:00:00+09:00')
            self.assertEqual((current['collection'], current['doc_id']), ('state', 'current'))
            self.assertEqual(current['data'],
                             {'application': None, 'updated_at': '2026-10-22T09:00:00+09:00'})

    def test_stop_expired_ask_is_declined(self):
        with tempfile.TemporaryDirectory() as d:
            out = self.answer(d, {'application': application()}, 'stop',
                              now='2026-10-23T00:00:00+09:00')
            self.assertEqual(out.returncode, 0, out.stderr)
            result = json.loads(out.stdout)
            self.assertIsNone(result['application'])
            self.assertEqual(result['writes'][0]['data']['ended_how'], 'declined')

    def test_no_application_is_exit_2(self):
        with tempfile.TemporaryDirectory() as d:
            out = self.answer(d, {}, 'stop')
            self.assertEqual(out.returncode, 2)
            self.assertTrue(out.stderr.strip())
            self.assertEqual(out.stdout, '')
            out = self.answer(d, None, 'forever')
            self.assertEqual(out.returncode, 2)

    def test_extend_without_until_is_exit_2(self):
        with tempfile.TemporaryDirectory() as d:
            out = self.answer(d, {'application': application()}, 'extend')
            self.assertEqual(out.returncode, 2)
            self.assertTrue(out.stderr.strip())

    def test_extend_past_or_equal_until_is_exit_2(self):
        with tempfile.TemporaryDirectory() as d:
            for until in ('2026-10-22T08:59:00+09:00', '2026-10-22T09:00:00+09:00'):
                out = self.answer(d, {'application': application()}, 'extend', until=until)
                self.assertEqual(out.returncode, 2, until)
                self.assertTrue(out.stderr.strip())

    def test_bad_choice_is_exit_2(self):
        with tempfile.TemporaryDirectory() as d:
            out = self.answer(d, {'application': application()}, 'later')
            self.assertEqual(out.returncode, 2)
            self.assertTrue(out.stderr.strip())


class LocalApplyTest(unittest.TestCase):
    """--state を省いた apply はローカルの overrides.json と history.jsonl を書き換える。"""

    def apply(self, d, template_name='cheap-all-tiers', until='2026-11-05T23:59:00+09:00',
              on_expire='ask', reason='テスト', now='2026-10-22T09:00:00+09:00', via=None):
        args = ['apply', '--home', d, '--now', now, '--template', template_name,
                '--until', until, '--on-expire', on_expire, '--reason', reason]
        if via:
            args += ['--via', via]
        return run(*args)

    def put_overrides(self, d, app, synced_at='2026-10-16T09:00:00+09:00',
                      updated_at='2026-10-20T09:00:00+09:00'):
        write_json(os.path.join(d, 'overrides.json'),
                   {'synced_at': synced_at, 'updated_at': updated_at, 'application': app})

    def test_writes_application_and_replaces_history(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            self.put_overrides(d, application())
            out = self.apply(d)
            self.assertEqual(out.returncode, 0, out.stderr)
            result = json.loads(out.stdout)
            overrides = read_json(os.path.join(d, 'overrides.json'))
            self.assertEqual(overrides['synced_at'], '2026-10-16T09:00:00+09:00')
            self.assertEqual(overrides['updated_at'], '2026-10-22T09:00:00+09:00')
            self.assertEqual(overrides['application'], result['application'])
            history = read_history(d)
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]['id'], APPLICATION['id'])
            self.assertEqual(history[0]['ended_how'], 'replaced')
            self.assertEqual(history[0]['ended_at'], '2026-10-22T09:00:00+09:00')
            self.assertEqual([w['collection'] for w in result['writes']], ['history', 'state'])
            self.assertEqual(result['writes'][1]['data'],
                             {'application': result['application'],
                              'updated_at': '2026-10-22T09:00:00+09:00'})

    def test_without_previous_writes_only_state(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            out = self.apply(d)
            self.assertEqual(out.returncode, 0, out.stderr)
            result = json.loads(out.stdout)
            self.assertEqual([w['collection'] for w in result['writes']], ['state'])
            overrides = read_json(os.path.join(d, 'overrides.json'))
            self.assertIsNone(overrides['synced_at'])
            self.assertEqual(overrides['updated_at'], '2026-10-22T09:00:00+09:00')
            self.assertEqual(read_history(d), [])

    def test_expired_drop_previous_is_expired(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            self.put_overrides(d, application(on_expire='drop'))
            out = self.apply(d, now='2026-10-23T00:00:00+09:00')
            self.assertEqual(out.returncode, 0, out.stderr)
            history = read_history(d)
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]['ended_how'], 'expired')
            self.assertEqual(history[0]['ended_at'], UNTIL)
            self.assertEqual(json.loads(out.stdout)['writes'][0]['data']['ended_how'], 'expired')

    def test_missing_template_is_exit_2_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            self.put_overrides(d, application())
            out = self.apply(d, template_name='ghost')
            self.assertEqual(out.returncode, 2)
            self.assertTrue(out.stderr.strip())
            self.assertEqual(out.stdout, '')
            self.assertEqual(read_json(os.path.join(d, 'overrides.json'))['application'], application())
            self.assertEqual(read_history(d), [])

    def test_invalid_template_is_exit_2(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template(runner='gemini'))
            out = self.apply(d)
            self.assertEqual(out.returncode, 2)
            self.assertTrue(out.stderr.strip())

    def test_past_until_is_exit_2(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            out = self.apply(d, until='2026-10-22T08:59:00+09:00')
            self.assertEqual(out.returncode, 2)
            self.assertTrue(out.stderr.strip())
            self.assertFalse(os.path.exists(os.path.join(d, 'overrides.json')))


class LocalAnswerTest(unittest.TestCase):
    """--state を省いた answer はローカルの overrides.json を読み書きする。"""

    def answer(self, d, choice, until=None, now='2026-10-22T09:00:00+09:00'):
        args = ['answer', '--home', d, '--now', now, '--choice', choice]
        if until is not None:
            args += ['--until', until]
        return run(*args)

    def put_overrides(self, d, app, synced_at='2026-10-16T09:00:00+09:00',
                      updated_at='2026-10-20T09:00:00+09:00'):
        write_json(os.path.join(d, 'overrides.json'),
                   {'synced_at': synced_at, 'updated_at': updated_at, 'application': app})

    def test_extend_rewrites_local_until(self):
        with tempfile.TemporaryDirectory() as d:
            self.put_overrides(d, application())
            out = self.answer(d, 'extend', until='2026-10-25T23:59:00+09:00')
            self.assertEqual(out.returncode, 0, out.stderr)
            result = json.loads(out.stdout)
            overrides = read_json(os.path.join(d, 'overrides.json'))
            self.assertEqual(overrides['application']['id'], APPLICATION['id'])
            self.assertEqual(overrides['application']['until'], '2026-10-25T23:59:00+09:00')
            self.assertEqual(overrides['updated_at'], '2026-10-22T09:00:00+09:00')
            self.assertEqual(overrides['synced_at'], '2026-10-16T09:00:00+09:00')
            self.assertEqual(read_history(d), [])
            self.assertEqual(result['application'], overrides['application'])
            self.assertEqual(result['writes'],
                             [{'op': 'set', 'collection': 'state', 'doc_id': 'current',
                               'data': {'application': result['application'],
                                        'updated_at': '2026-10-22T09:00:00+09:00'}}])

    def test_forever_clears_local_until(self):
        with tempfile.TemporaryDirectory() as d:
            self.put_overrides(d, application())
            out = self.answer(d, 'forever')
            self.assertEqual(out.returncode, 0, out.stderr)
            overrides = read_json(os.path.join(d, 'overrides.json'))
            self.assertIsNone(overrides['application']['until'])
            self.assertEqual(overrides['updated_at'], '2026-10-22T09:00:00+09:00')
            self.assertEqual(read_history(d), [])

    def test_stop_clears_local_and_appends_stopped(self):
        with tempfile.TemporaryDirectory() as d:
            self.put_overrides(d, application())
            out = self.answer(d, 'stop')
            self.assertEqual(out.returncode, 0, out.stderr)
            result = json.loads(out.stdout)
            self.assertIsNone(result['application'])
            overrides = read_json(os.path.join(d, 'overrides.json'))
            self.assertIsNone(overrides['application'])
            self.assertEqual(overrides['updated_at'], '2026-10-22T09:00:00+09:00')
            self.assertEqual(overrides['synced_at'], '2026-10-16T09:00:00+09:00')
            history = read_history(d)
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]['id'], APPLICATION['id'])
            self.assertEqual(history[0]['ended_how'], 'stopped')
            self.assertEqual(history[0]['ended_at'], '2026-10-22T09:00:00+09:00')
            self.assertEqual([w['collection'] for w in result['writes']], ['history', 'state'])
            self.assertEqual(result['writes'][1]['data'],
                             {'application': None, 'updated_at': '2026-10-22T09:00:00+09:00'})

    def test_stop_expired_ask_is_declined(self):
        with tempfile.TemporaryDirectory() as d:
            self.put_overrides(d, application())
            out = self.answer(d, 'stop', now='2026-10-23T00:00:00+09:00')
            self.assertEqual(out.returncode, 0, out.stderr)
            history = read_history(d)
            self.assertEqual(history[0]['ended_how'], 'declined')
            self.assertEqual(history[0]['ended_at'], '2026-10-23T00:00:00+09:00')

    def test_no_application_is_exit_2(self):
        with tempfile.TemporaryDirectory() as d:
            out = self.answer(d, 'stop')
            self.assertEqual(out.returncode, 2)
            self.assertTrue(out.stderr.strip())
            self.assertEqual(out.stdout, '')
            self.put_overrides(d, None)
            out = self.answer(d, 'forever')
            self.assertEqual(out.returncode, 2)
            self.assertTrue(out.stderr.strip())

    def test_extend_without_until_is_exit_2(self):
        with tempfile.TemporaryDirectory() as d:
            self.put_overrides(d, application())
            out = self.answer(d, 'extend')
            self.assertEqual(out.returncode, 2)
            self.assertTrue(out.stderr.strip())

    def test_extend_past_until_keeps_local_state(self):
        with tempfile.TemporaryDirectory() as d:
            self.put_overrides(d, application())
            before = read_json(os.path.join(d, 'overrides.json'))
            out = self.answer(d, 'extend', until='2026-10-22T08:59:00+09:00')
            self.assertEqual(out.returncode, 2)
            self.assertTrue(out.stderr.strip())
            self.assertEqual(read_json(os.path.join(d, 'overrides.json')), before)
            self.assertEqual(read_history(d), [])


class LocalStatusTest(unittest.TestCase):
    def status(self, d, now='2026-10-22T09:00:00+09:00'):
        out = run('status', '--home', d, '--now', now)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def put_overrides(self, d, app, synced_at='2026-10-16T09:00:00+09:00',
                      updated_at='2026-10-20T09:00:00+09:00'):
        write_json(os.path.join(d, 'overrides.json'),
                   {'synced_at': synced_at, 'updated_at': updated_at, 'application': app})

    def test_none_without_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.status(d), {'status': 'none', 'application': None, 'writes': []})

    def test_active(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            self.put_overrides(d, application())
            result = self.status(d)
            self.assertEqual(result['status'], 'active')
            self.assertEqual(result['application']['id'], APPLICATION['id'])
            self.assertEqual(result['writes'], [])

    def test_expired_ask(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            self.put_overrides(d, application())
            result = self.status(d, now='2026-10-23T00:00:00+09:00')
            self.assertEqual(result['status'], 'expired_ask')
            self.assertEqual(result['application']['id'], APPLICATION['id'])
            self.assertEqual(result['writes'], [])
            self.assertEqual(read_history(d), [])

    def test_expired_drop_clears_local_and_appends_expired(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'cheap-all-tiers.json'), template())
            self.put_overrides(d, application(on_expire='drop'))
            result = self.status(d, now='2026-10-23T00:00:00+09:00')
            self.assertEqual(result['status'], 'expired_drop')
            self.assertIsNone(result['application'])
            overrides = read_json(os.path.join(d, 'overrides.json'))
            self.assertIsNone(overrides['application'])
            self.assertEqual(overrides['updated_at'], '2026-10-23T00:00:00+09:00')
            self.assertEqual(overrides['synced_at'], '2026-10-16T09:00:00+09:00')
            history = read_history(d)
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]['ended_how'], 'expired')
            self.assertEqual(history[0]['ended_at'], UNTIL)
            self.assertEqual([w['collection'] for w in result['writes']], ['history', 'state'])
            self.assertEqual(result['writes'][1]['data'],
                             {'application': None, 'updated_at': '2026-10-23T00:00:00+09:00'})

    def test_unknown_template_keeps_application(self):
        with tempfile.TemporaryDirectory() as d:
            self.put_overrides(d, application(template='ghost'))
            result = self.status(d)
            self.assertEqual(result['status'], 'unknown_template')
            self.assertEqual(result['application']['template'], 'ghost')
            self.assertEqual(result['writes'], [])


class PushTest(unittest.TestCase):
    def push(self, d, *extra, now='2026-10-20T09:00:00+09:00'):
        out = run('push', '--home', d, '--now', now, *extra)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_sets_only_valid_templates_with_synced_at(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'good.json'), template(name='good'))
            write_json(os.path.join(d, 'templates', 'bad.json'), template(name='bad', runner='gemini'))
            write_text(os.path.join(d, 'templates', 'broken.json'), '{oops')
            result = self.push(d)
            self.assertEqual(len(result['writes']), 1)
            write = result['writes'][0]
            self.assertEqual((write['op'], write['collection'], write['doc_id']),
                             ('set', 'templates', 'good'))
            self.assertEqual(write['data'],
                             dict(template(name='good'), synced_at='2026-10-20T09:00:00+09:00'))

    def test_existing_deleted_only_when_not_local(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'good.json'), template(name='good'))
            result = self.push(d, '--existing', 'good,gone')
            self.assertEqual(sorted((w['op'], w['doc_id']) for w in result['writes']),
                             [('delete', 'gone'), ('set', 'good')])
            self.assertTrue(all(w['collection'] == 'templates' for w in result['writes']))

    def test_invalid_local_template_is_deleted(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'bad.json'), template(name='bad', runner='gemini'))
            result = self.push(d, '--existing', 'bad')
            self.assertEqual([(w['op'], w['doc_id']) for w in result['writes']], [('delete', 'bad')])

    def test_without_existing_has_no_deletes(self):
        with tempfile.TemporaryDirectory() as d:
            write_json(os.path.join(d, 'templates', 'good.json'), template(name='good'))
            result = self.push(d)
            self.assertEqual({w['op'] for w in result['writes']}, {'set'})

    def test_empty_home_is_empty_writes(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.push(d), {'writes': []})


if __name__ == '__main__':
    unittest.main()
