"""component.py の add・remove・list を、偽のプラグインと自分のスキルの置き場で確かめる。

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
        self.plugin('mattpocock-skills', {'grilling': False, 'code-review': False},
                    {'license': 'MIT', 'homepage': 'https://github.com/mattpocock/skills'})
        self.plugin('pstack', {'how': True}, {'license': 'MIT', 'repository': {'url': 'https://github.com/cursor/plugins'}})
        self.plugin('yomiyasu', {'yomiyasu': False}, {'license': 'MIT', 'repository': 'https://github.com/nanaism/yomiyasu'})
        write(os.path.join(self.skills, 'my-review', 'SKILL.md'), skill_md('my-review'))
        write(os.path.join(self.skills, 'my-hidden', 'SKILL.md'), skill_md('my-hidden', disabled=True))

    def plugin(self, name, skills, meta):
        root = os.path.join(self.plugins, 'cache', 'market', name, '1.0.0')
        write(os.path.join(root, '.claude-plugin', 'plugin.json'), json.dumps(dict(meta, name=name)))
        for skill, disabled in skills.items():
            write(os.path.join(root, 'skills', skill, 'SKILL.md'), skill_md(skill, disabled))

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


if __name__ == '__main__':
    unittest.main()
