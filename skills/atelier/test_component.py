"""component.py の add・remove・list・example を、偽のプラグインと自分のスキルの置き場で確かめる。

入ったスキルの SKILL.md から部品の定義を作ること (Skill ツールで呼べるものは skill、disable-model-invocation は
read-skill)、ライセンスとリンクを plugin.json から取ること、home に無いテンプレートは同梱のものを写して差すこと、
工程では先頭・関所では after の工程の関所に入ること、検証に通らなければ何も残さないこと、
同梱の部品は消せないこと。home とプラグインの置き場は必ず一時ディレクトリにする。
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import component  # noqa: E402


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def skill_md(name, disabled=False):
    extra = 'disable-model-invocation: true\n' if disabled else ''
    return f'---\nname: {name}\ndescription: Does {name}. More text.\n{extra}---\n\n# {name}\n'


class ComponentCliTest(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.join(tempfile.gettempdir(), f'pbi-component-{uuid.uuid4().hex}')
        os.mkdir(self.dir)
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.home = os.path.join(self.dir, 'home')
        self.plugins = os.path.join(self.dir, 'plugins')
        self.skills = os.path.join(self.dir, 'skills')
        # mattpocock-skills は skills/<分類>/<名前>/ の構成 (1.3.1 で確認)
        self.plugin('mattpocock-skills', {'grilling': False, 'code-review': False},
                    {'license': 'MIT', 'homepage': 'https://github.com/mattpocock/skills'}, group='engineering')
        self.plugin('grouped', {'deep': True}, {'license': 'MIT'}, group='productivity')
        self.plugin('pstack', {'how': True, 'blast-radius': True, 'architect': True, 'interrogate': True, 'statechart': True}, {'license': 'MIT', 'repository': {'url': 'https://github.com/cursor/plugins'}})
        self.plugin('yomiyasu', {'yomiyasu': False}, {'license': 'MIT', 'repository': 'https://github.com/nanaism/yomiyasu'})
        self.plugin('founder-skill', {'founder-consumer': False}, {'license': 'MIT'})
        write(os.path.join(self.skills, 'my-review', 'SKILL.md'), skill_md('my-review'))
        for name in ('design-studio', 'ui-video', 'security-audit'):
            write(os.path.join(self.skills, name, 'SKILL.md'), skill_md(name))
        write(os.path.join(self.skills, 'my-hidden', 'SKILL.md'), skill_md('my-hidden', disabled=True))

    def plugin(self, name, skills, meta, group=''):
        root = os.path.join(self.plugins, 'cache', 'market', name, '1.0.0')
        write(os.path.join(root, '.claude-plugin', 'plugin.json'), json.dumps(dict(meta, name=name)))
        for skill, disabled in skills.items():
            write(os.path.join(root, 'skills', group, skill, 'SKILL.md'), skill_md(skill, disabled))

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        env = {'PBI_PLUGINS_DIR': self.plugins, 'PBI_SKILLS_DIR': self.skills}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = component.main(['--home', self.home, *args])
        return code, out.getvalue(), err.getvalue()

    def load(self, *parts):
        with open(os.path.join(self.home, *parts), encoding='utf-8') as f:
            return json.load(f)

    def test_add_a_plugin_skill_to_a_stage(self):
        code, out, err = self.run_cli('add', 'mattpocock-skills:grilling', '--stage', 'design')
        self.assertEqual(code, 0, out + err)
        data = self.load('components', 'design.mattpocock-skills-grilling.json')
        self.assertEqual(data['invoke'], {'kind': 'skill', 'name': 'mattpocock-skills:grilling'})
        self.assertEqual((data['where'], data['memory'], data['interactive']), ('main', 'full', True))
        self.assertEqual(data['requires'], {'skills': ['mattpocock-skills:grilling']})
        self.assertEqual((data['license'], data['link']), ('MIT', 'https://github.com/mattpocock/skills'))
        self.assertEqual(data['title']['en'], 'Does grilling')
        self.assertEqual(self.load('workflows', 'jev-routed.json')['stages']['design'], ['design.mattpocock-skills-grilling', 'design.builtin'])

    def test_read_skill_under_a_group_folder_keeps_its_path(self):
        code, out, err = self.run_cli('add', 'grouped:deep', '--stage', 'review')
        self.assertEqual(code, 0, out + err)
        data = self.load('components', 'review.grouped-deep.json')
        self.assertEqual(data['invoke'], {'kind': 'read-skill', 'plugin': 'grouped', 'path': 'skills/productivity/deep/SKILL.md'})

    def test_add_twice_keeps_one_entry(self):
        self.run_cli('add', 'my-review', '--stage', 'review')
        code, out, err = self.run_cli('add', 'my-review', '--stage', 'review')
        self.assertEqual(code, 0, out + err)
        review = self.load('workflows', 'jev-routed.json')['stages']['review']
        self.assertEqual(review, ['review.my-review', 'review.code-review', 'review.builtin'])

    def test_plugin_ids_do_not_clash_with_built_ins(self):
        # mattpocock-skills の code-review は、同梱の built-in の review.code-review とは別の id になる
        code, out, err = self.run_cli('add', 'mattpocock-skills:code-review', '--stage', 'review')
        self.assertEqual(code, 0, out + err)
        review = self.load('workflows', 'jev-routed.json')['stages']['review']
        self.assertEqual(review, ['review.mattpocock-skills-code-review', 'review.code-review', 'review.builtin'])

    def test_bundled_id_is_refused(self):
        write(os.path.join(self.skills, 'builtin', 'SKILL.md'), skill_md('builtin'))
        code, _, err = self.run_cli('add', 'builtin', '--stage', 'review')
        self.assertEqual(code, 2)
        self.assertIn('review.builtin: a bundled component already has this id', err)
        self.assertFalse(os.path.exists(os.path.join(self.home, 'components')))

    def test_add_a_read_skill_and_a_gate(self):
        code, out, err = self.run_cli('add', 'pstack:how', '--stage', 'understand', '--workflow', 'high-model',
                                      '--forbid', 'do not run Phase D')
        self.assertEqual(code, 0, out + err)
        data = self.load('components', 'understand.pstack-how.json')
        self.assertEqual(data['invoke'], {'kind': 'read-skill', 'plugin': 'pstack', 'path': 'skills/how/SKILL.md'})
        self.assertEqual((data['where'], data['memory'], data['doc']), ('subagent-readonly', 'none', 'appendix/engine.md'))
        self.assertEqual(data['link'], 'https://github.com/cursor/plugins')
        self.assertEqual(data['forbid'], component.READ_SKILL_FORBID + ['do not run Phase D'])
        code, out, err = self.run_cli('add', 'yomiyasu:yomiyasu', '--stage', 'gate', '--after', 'finish',
                                      '--workflow', 'high-model')
        self.assertEqual(code, 0, out + err)
        workflow = self.load('workflows', 'high-model.json')
        self.assertEqual(workflow['stages']['understand'], ['understand.pstack-how', 'understand.builtin'])
        self.assertEqual(workflow['gates'][-1], {'after': 'finish', 'components': ['gate.yomiyasu']})
        self.assertEqual(self.load('components', 'gate.yomiyasu.json')['doc'], 'appendix/finish.md')

    def test_gate_is_placed_in_stage_order(self):
        self.run_cli('add', 'yomiyasu:yomiyasu', '--stage', 'gate', '--after', 'route')
        gates = self.load('workflows', 'jev-routed.json')['gates']
        self.assertEqual([g['after'] for g in gates], ['plan', 'route', 'review'])

    def test_add_my_own_skill_to_review(self):
        code, out, err = self.run_cli('add', 'my-review', '--stage', 'review')
        self.assertEqual(code, 0, out + err)
        data = self.load('components', 'review.my-review.json')
        self.assertEqual(data['invoke'], {'kind': 'skill', 'name': 'my-review'})
        self.assertEqual(data['where'], 'subagent')
        self.assertEqual(data['forbid'], component.REVIEW_FORBID)

    def test_add_to_optional_stages(self):
        code, out, err = self.run_cli('add', 'my-review', '--stage', 'ui-design', '--when', 'ui-change')
        self.assertEqual(code, 0, out + err)
        self.assertIn('(skill, ui-design after design)', out)
        data = self.load('components', 'ui-design.my-review.json')
        self.assertEqual((data['doc'], data['where'], data['interactive']), ('appendix/optional.md', 'main', True))
        self.assertEqual(data['forbid'], [])
        self.run_cli('add', 'mattpocock-skills:grilling', '--stage', 'ui-design')
        workflow = self.load('workflows', 'jev-routed.json')
        self.assertEqual(workflow['optional'], [{'stage': 'ui-design', 'after': 'design', 'components': [
            {'id': 'ui-design.my-review', 'when': 'ui-change'}, 'ui-design.mattpocock-skills-grilling']}])
        # 置き場所はテンプレートで 1 つ。違う after は断る
        code, _, err = self.run_cli('add', 'yomiyasu:yomiyasu', '--stage', 'ui-design', '--after', 'plan')
        self.assertEqual(code, 2)
        self.assertIn('ui-design is already placed after design', err)
        code, _, err = self.run_cli('add', 'yomiyasu:yomiyasu', '--stage', 'marketing', '--after', 'finish')
        self.assertEqual(code, 2)
        self.assertIn('cannot run after finish', err)
        code, out, err = self.run_cli('add', 'yomiyasu:yomiyasu', '--stage', 'marketing', '--after', 'review')
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.load('workflows', 'jev-routed.json')['optional'][-1]['after'], 'review')
        # remove は最後の部品を外すとその工程ごと消す
        self.assertEqual(self.run_cli('remove', 'marketing.yomiyasu')[0], 0)
        self.assertEqual([s['stage'] for s in self.load('workflows', 'jev-routed.json')['optional']], ['ui-design'])
        code, _, err = self.run_cli('add', 'my-review', '--stage', 'review', '--after', 'design')
        self.assertEqual(code, 2)
        self.assertIn('--after is only for --stage gate or an optional stage', err)

    def test_errors_leave_nothing(self):
        cases = [
            (('add', 'nobody:nothing', '--stage', 'design'), 'skill not found; install the plugin first'),
            (('add', 'my-hidden', '--stage', 'design'), 'cannot be invoked by the model and is not a plugin skill'),
            (('add', 'yomiyasu:yomiyasu', '--stage', 'gate'), '--stage gate needs --after'),
            (('add', 'my-review', '--stage', 'review', '--when', 'tier-opus'), ''),
            (('add', 'my-review', '--stage', 'review', '--workflow', 'no-such'), 'workflow not found: no-such'),
        ]
        for args, message in cases:
            with self.subTest(args=args):
                code, out, err = self.run_cli(*args)
                self.assertEqual(code, 2, out + err)
                self.assertIn(message, out + err)
                self.assertFalse(os.path.exists(os.path.join(self.home, 'components', 'review.my-review.json')))
                self.assertFalse(os.path.exists(os.path.join(self.home, 'workflows', 'jev-routed.json')))

    def test_per_pbi_when_outside_a_route_gate_is_an_error(self):
        _, out, _ = self.run_cli('add', 'my-review', '--stage', 'review', '--when', 'tier-opus')
        self.assertIn('error review: review.my-review uses when tier-opus, which only a gate after route can decide', out)

    def test_when_lives_on_the_workflow_entry(self):
        self.run_cli('add', 'pstack:how', '--stage', 'understand', '--when', 'clearly-complex')
        code, out, err = self.run_cli('add', 'pstack:how', '--stage', 'understand', '--workflow', 'high-model')
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.load('workflows', 'jev-routed.json')['stages']['understand'][0],
                         {'id': 'understand.pstack-how', 'when': 'clearly-complex'})
        self.assertEqual(self.load('workflows', 'high-model.json')['stages']['understand'][0], 'understand.pstack-how')
        self.assertEqual(self.load('components', 'understand.pstack-how.json')['when'], 'always')

    def test_models_live_on_the_workflow_entry(self):
        code, out, err = self.run_cli('add', 'pstack:interrogate', '--stage', 'review', '--when', 'deep',
                                      '--where', 'main', '--models', 'opus xhigh', 'sonnet high')
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.load('workflows', 'jev-routed.json')['stages']['review'][0],
                         {'id': 'review.pstack-interrogate', 'when': 'deep', 'where': 'main',
                          'models': ['opus xhigh', 'sonnet high']})
        # where と models は共有の部品の定義に書かないので、登録済みの部品のまま別のテンプレートで変えられる
        component_file = self.load('components', 'review.pstack-interrogate.json')
        self.assertEqual(component_file['where'], 'subagent-readonly')
        self.assertNotIn('models', component_file)
        code, out, err = self.run_cli('add', 'pstack:interrogate', '--stage', 'review', '--workflow', 'high-model',
                                      '--where', 'main', '--models', 'sonnet high')
        self.assertEqual(code, 0, out + err)
        # 登録し直して --when だけ変えても、where と models は残り、--no-models で消える
        self.run_cli('add', 'pstack:interrogate', '--stage', 'review', '--when', 'always')
        self.assertEqual(self.load('workflows', 'jev-routed.json')['stages']['review'][0],
                         {'id': 'review.pstack-interrogate', 'when': 'always', 'where': 'main',
                          'models': ['opus xhigh', 'sonnet high']})
        self.run_cli('add', 'pstack:interrogate', '--stage', 'review', '--when', 'always', '--no-models')
        self.assertEqual(self.load('workflows', 'jev-routed.json')['stages']['review'][0],
                         {'id': 'review.pstack-interrogate', 'when': 'always', 'where': 'main'})
        # サブエージェントで動く部品は、さらにサブエージェントを起こせないので models を持てない
        _, out, _ = self.run_cli('add', 'pstack:how', '--stage', 'understand', '--models', 'opus')
        self.assertIn('error understand: understand.pstack-how has models but does not run in main '
                      '(a subagent cannot start subagents)', out)

    def test_other_settings_for_a_registered_component_are_refused(self):
        self.run_cli('add', 'my-review', '--stage', 'review')
        code, _, err = self.run_cli('add', 'my-review', '--stage', 'review', '--memory', 'full', '--workflow', 'high-model')
        self.assertEqual(code, 2)
        self.assertIn('already registered with other settings', err)
        self.assertFalse(os.path.exists(os.path.join(self.home, 'workflows', 'high-model.json')))

    def test_unreadable_home_workflow_is_not_replaced(self):
        path = os.path.join(self.home, 'workflows', 'jev-routed.json')
        write(path, '{broken')
        code, _, err = self.run_cli('add', 'my-review', '--stage', 'review')
        self.assertEqual(code, 2)
        self.assertIn('not a readable workflow', err)
        with open(path, encoding='utf-8') as f:
            self.assertEqual(f.read(), '{broken')

    def test_remove_rejects_ids_that_leave_components(self):
        self.run_cli('add', 'my-review', '--stage', 'review')
        for bad in ('../workflows/jev-routed', 'review/../x', 'nothing'):
            with self.subTest(bad=bad):
                code, _, err = self.run_cli('remove', bad)
                self.assertEqual(code, 2)
                self.assertIn('not a component id', err)
        self.assertTrue(os.path.exists(os.path.join(self.home, 'workflows', 'jev-routed.json')))

    def test_remove_keeps_everything_while_another_workflow_uses_it(self):
        self.run_cli('add', 'my-review', '--stage', 'review')
        self.run_cli('add', 'my-review', '--stage', 'review', '--workflow', 'high-model')
        code, _, err = self.run_cli('remove', 'review.my-review')
        self.assertEqual(code, 2)
        self.assertIn('still used by workflows high-model', err)
        self.assertTrue(os.path.exists(os.path.join(self.home, 'components', 'review.my-review.json')))
        self.assertEqual(self.load('workflows', 'jev-routed.json')['stages']['review'][0], 'review.my-review')

    def test_remove(self):
        self.run_cli('add', 'my-review', '--stage', 'review')
        code, out, err = self.run_cli('remove', 'review.my-review')
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.load('workflows', 'jev-routed.json')['stages']['review'], ['review.code-review', 'review.builtin'])
        self.assertFalse(os.path.exists(os.path.join(self.home, 'components', 'review.my-review.json')))
        code, _, err = self.run_cli('remove', 'review.builtin')
        self.assertEqual(code, 2)
        self.assertIn('a bundled component cannot be removed', err)

    def test_list_marks_the_source(self):
        self.run_cli('add', 'my-review', '--stage', 'review')
        code, out, _ = self.run_cli('list')
        self.assertEqual(code, 0)
        rows = {line.split('\t')[0]: line.split('\t') for line in out.splitlines()[1:]}
        self.assertEqual(rows['review.my-review'][1], 'my-review')
        self.assertEqual(rows['review.builtin'][1], 'bundled')


    # --- example ---

    def test_bundled_examples_are_valid(self):
        for path in sorted(component.EXAMPLES.glob('*.json')):
            data, problem = component.load_example(str(path))
            self.assertIsNotNone(data, problem)
            self.assertEqual(data['workflow']['name'], data['name'], path.name)

    def test_example_check_lists_missing_skills_and_install_commands(self):
        shutil.rmtree(os.path.join(self.plugins, 'cache', 'market', 'yomiyasu'))
        code, out, err = self.run_cli('example', 'check', 'recommended')
        self.assertEqual(code, component.MISSING, out + err)
        self.assertIn('yomiyasu:yomiyasu\tgate after finish\tmissing', out)
        self.assertIn('pstack:how\tunderstand\tinstalled\tnot registered', out)
        self.assertIn('install claude plugin marketplace add nanaism/yomiyasu', out)
        self.assertIn('install claude plugin install yomiyasu@yomiyasu --scope user', out)
        self.assertNotIn('install claude plugin install pstack', out)
        self.assertIn('design-studio\tui-design after design\tinstalled', out)
        self.assertIn('founder-skill:founder-consumer\tmarketing after understand\tinstalled', out)
        self.assertIn('security-audit\tsecurity after review\tinstalled', out)
        # 自分のスキルは見本の install.command をそのまま出す
        shutil.rmtree(os.path.join(self.skills, 'ui-video'))
        _code, out, _err = self.run_cli('example', 'check', 'recommended')
        self.assertIn('install git clone https://github.com/reeschan/atelier.git ~/dev/atelier && '
                      'mkdir -p ~/.claude/skills && cp -r ~/dev/atelier/skills/ui-video ~/.claude/skills/', out)
        code, out, err = self.run_cli('example', 'use', 'recommended')
        self.assertEqual(code, component.MISSING)
        self.assertFalse(os.path.exists(os.path.join(self.home, 'workflows')))

    def test_example_use_registers_components_and_writes_the_template(self):
        code, out, err = self.run_cli('example', 'use', 'recommended', '--as', 'jev-routed')
        self.assertEqual(code, 0, out + err)
        workflow = self.load('workflows', 'jev-routed.json')
        self.assertEqual(workflow['name'], 'jev-routed')
        self.assertEqual(workflow['stages']['understand'][0], {'id': 'understand.pstack-how', 'when': 'clearly-complex'})
        architect = self.load('components', 'gate.pstack-architect.json')
        self.assertEqual(architect['invoke']['kind'], 'read-skill')
        self.assertIn('do not run Phase D (implementation) or Phase E', architect['forbid'])
        self.assertEqual(self.load('components', 'gate.yomiyasu.json')['doc'], 'appendix/finish.md')
        self.assertEqual([s['stage'] for s in workflow['optional']], ['marketing', 'ui-design', 'model-check', 'security'])
        self.assertEqual(workflow['optional'][2]['components'], [{'id': 'model-check.pstack-statechart', 'when': 'ask-user'}])
        self.assertEqual(workflow['optional'][3]['components'], [{'id': 'security.security-audit', 'when': 'ask-user'}])
        # interrogate は敵役を起こすので設計者が動かし、敵役のモデルはテンプレートの候補に書く
        self.assertEqual(workflow['stages']['review'][0], {'id': 'review.pstack-interrogate', 'when': 'deep', 'where': 'main',
                                                         'models': ['opus xhigh', 'sonnet high']})
        self.assertEqual(self.load('components', 'ui-design.ui-video.json')['doc'], 'appendix/optional.md')
        code, out, err = self.run_cli('example', 'check', 'recommended')
        self.assertEqual(code, 0, out + err)
        self.assertNotIn('not registered', out)
        # 2 回目は --replace が要り、付ければ登録済みの部品はそのまま使う
        self.assertEqual(self.run_cli('example', 'use', 'recommended', '--as', 'jev-routed')[0], 2)
        self.assertEqual(self.run_cli('example', 'use', 'recommended', '--as', 'jev-routed', '--replace')[0], 0)

    def test_example_use_refuses_a_component_registered_with_other_settings(self):
        self.run_cli('add', 'pstack:how', '--stage', 'understand', '--memory', 'full')
        code, out, err = self.run_cli('example', 'use', 'recommended')
        self.assertEqual(code, 2)
        self.assertIn('understand.pstack-how', err)
        self.assertIn('pstack:how\tunderstand\tinstalled\tdiffers', out)
        self.assertFalse(os.path.exists(os.path.join(self.home, 'workflows', 'recommended.json')))

    def test_example_use_rolls_back_on_a_workflow_error(self):
        bad = component.load_example('recommended')[0]
        bad['workflow']['stages']['review'] = ['review.pstack-interrogate']
        path = os.path.join(self.dir, 'bad.json')
        write(path, json.dumps(bad))
        code, out, err = self.run_cli('example', 'use', path)
        self.assertEqual(code, 2)
        self.assertIn('the last component must be builtin', out)
        self.assertEqual(os.listdir(os.path.join(self.home, 'components')), [])
        self.assertFalse(os.path.exists(os.path.join(self.home, 'workflows', 'recommended.json')))

    def test_example_shape_errors(self):
        path = os.path.join(self.dir, 'shape.json')
        write(path, json.dumps({'name': 'x', 'workflow': {}, 'skills': [{'skill': 'a:b', 'stage': 'gate'}]}))
        code, _out, err = self.run_cli('example', 'check', path)
        self.assertEqual(code, 2)
        self.assertIn('stage gate needs after (a stage)', err)
        write(path, json.dumps({'name': 'x', 'workflow': {}, 'skills': [{'skill': 'a:b', 'stage': 'review', 'link': None}]}))
        code, _out, err = self.run_cli('example', 'check', path)
        self.assertEqual(code, 2)
        self.assertIn('link must be a string', err)
        self.assertEqual(self.run_cli('example', 'check', 'no-such-example')[0], 2)


if __name__ == '__main__':
    unittest.main()
