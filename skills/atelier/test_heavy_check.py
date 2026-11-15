"""heavy_check.py を CLI として一時ディレクトリの git リポジトリで動かし、判定・原稿・記録・断りを確かめる。

基準ごとの境目 (new-app の行数・new-subsystem のモジュール数・new-architecture の配置先と依存の入れ替え)、
<pbi-dir> の下を数えないこと、原稿が問いの点検 (design_record.lint_text の decision) に通ること、
費用の見込みの式、record の断り (ページを見せていない run・auto の run・理由の無い auto の skip) と、
記録のあとは聞き直さないことを見る。home・プラグイン・ハーネスのログは一時ディレクトリに切り離す。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'heavy_check.py')
sys.path.insert(0, HERE)

import common  # noqa: E402
import design_record  # noqa: E402
import heavy_check  # noqa: E402

HEAVY_ROW = {'id': 'review.fake-heavy', 'when': 'heavy-ask', 'where': 'main', 'models': ['opus xhigh', 'sonnet high']}


def code_lines(n: int) -> str:
    return ''.join(f'value_{i} = {i}\n' for i in range(n))


class HeavyCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmp, 'repo')
        self.home = os.path.join(self.tmp, 'home')
        os.makedirs(self.repo)
        os.makedirs(os.path.join(self.home, 'components'))
        os.makedirs(os.path.join(self.tmp, 'plugins'))
        self.env = {**os.environ, 'ATELIER_HOME': self.home, 'PBI_PLUGINS_DIR': os.path.join(self.tmp, 'plugins'),
                    'HARNESS_LOG_CONFIG': os.path.join(self.tmp, 'no-harness-log.json'),
                    'ATELIER_EVENTS_SETTLE': 'off', 'ATELIER_EVENTS_QUIET': '1'}
        self.pbi = os.path.join(self.repo, 'docs', 'pbi')
        self.git('init', '-q', '-b', 'main')
        self.write('README.md', '# app\n')
        self.write('src/a/x.py', code_lines(10))
        self.write('src/b/y.py', code_lines(10))
        self.write('pyproject.toml', '[project]\nname = "app"\ndependencies = ["flask"]\n')
        self.commit()
        self.git('checkout', '-q', '-b', 'work')
        # 重い見直しの部品 (home の外部部品) と、それを review の先頭に置いたテンプレート
        component = {'id': 'review.fake-heavy', 'stages': ['review'], 'title': {'ja': 'heavy', 'en': 'heavy'},
                     'invoke': {'kind': 'read-skill', 'plugin': 'fake', 'path': 'skills/heavy/SKILL.md'},
                     'doc': 'appendix/engine.md', 'where': 'subagent-readonly', 'memory': 'none', 'when': 'always',
                     'cost': 'mid', 'requires': {'plugins': ['fake']}, 'forbid': [], 'interactive': False,
                     'calls_back': False}
        common.write_json(os.path.join(self.home, 'components', 'review.fake-heavy.json'), component)
        self.workflow([HEAVY_ROW, 'review.code-review', 'review.builtin'])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def git(self, *words):
        subprocess.run(['git', '-c', 'user.email=t@example.com', '-c', 'user.name=t', '-c', 'core.autocrlf=false',
                        *words], cwd=self.repo, check=True, capture_output=True)

    def write(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)

    def commit(self):
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'base')

    def intend(self):
        """新しいファイルを差分に出す (appendix/review.md の git add -N と同じ)。"""
        self.git('add', '-N', '.')

    def workflow(self, review):
        data = common.load_json(os.path.join(HERE, 'workflows', 'jev-routed.json'))
        data['name'] = 'test'
        data['stages']['review'] = review
        os.makedirs(self.pbi, exist_ok=True)
        common.write_json(os.path.join(self.pbi, 'workflow.json'), data)

    def run_cli(self, *args):
        result = subprocess.run([sys.executable, SCRIPT, *args], cwd=self.repo, env=self.env, capture_output=True,
                                text=True, encoding='utf-8')
        return result.returncode, result.stdout, result.stderr

    def check(self, *extra):
        return self.run_cli('check', 'docs/pbi', '--base', 'main', *extra)

    def result(self):
        return common.load_json(os.path.join(self.pbi, 'verify', 'heavy.json'))

    def held(self):
        return [c['id'] for c in self.result()['criteria'] if c['holds']]

    def test_a_small_change_is_light_and_writes_no_draft(self):
        self.write('src/a/x.py', code_lines(40))
        code, out, err = self.check()
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines()[0], 'verdict light')
        result = self.result()
        self.assertEqual((result['verdict'], result['draft']), ('light', None))
        self.assertEqual(result['numbers']['files'], 1)
        self.assertFalse(os.path.exists(os.path.join(self.pbi, 'notes', 'heavy-review.question.md')))

    def test_new_app_needs_a_new_manifest_and_enough_lines(self):
        self.write('apps/web/package.json', '{"name": "web"}\n')
        self.write('apps/web/src/main.py', code_lines(heavy_check.NEW_APP_LINES - 2))
        self.intend()
        self.check()
        self.assertEqual(self.held(), [])  # 299 行は届かない
        self.write('apps/web/src/main.py', code_lines(heavy_check.NEW_APP_LINES))
        code, out, err = self.check()
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines()[0], 'verdict heavy')
        self.assertEqual(self.held(), ['new-app'])

    def test_a_manifest_in_a_folder_that_had_one_is_not_a_new_app(self):
        self.write('src/c/z.py', code_lines(10))
        self.write('src/c/package.json', '{}\n')
        self.rebase_here()
        self.write('requirements.txt', 'flask\n')  # マニフェストに数えない
        self.write('setup.cfg', '[metadata]\n')    # ルートにはもう pyproject.toml がある
        self.write('src/c/tsconfig.json', '{}\n')
        self.write('src/a/more.py', code_lines(400))
        self.intend()
        self.check()
        self.assertEqual(self.result()['numbers']['added_files'], 4)
        self.assertNotIn('new-app', self.held())

    def test_new_subsystem_needs_three_modules_a_new_one_and_a_data_model_or_interface(self):
        self.write('src/a/x.py', code_lines(20))
        self.write('src/billing/service.py', code_lines(20))
        self.intend()
        self.check()
        self.assertEqual(self.held(), [])  # 2 モジュールで、データモデルも入出力も無い
        self.write('src/b/y.py', code_lines(20))
        self.check()
        self.assertEqual(self.held(), [])  # 3 モジュールでも、データモデルも入出力も無い
        self.write('src/billing/models/invoice.py', code_lines(5))
        self.intend()
        self.check()
        self.assertEqual(self.held(), ['new-subsystem'])
        numbers = self.result()['numbers']
        self.assertEqual((numbers['modules'], numbers['new_modules']), (['src/a', 'src/b', 'src/billing'], ['src/billing']))

    def test_a_module_with_only_docs_in_the_base_is_new(self):
        self.write('src/c/README.md', '# c\n')
        self.write('tests/c/test_c.py', code_lines(5))
        self.rebase_here()
        self.write('src/a/x.py', code_lines(20))
        self.write('src/b/y.py', code_lines(20))
        self.write('src/c/models.py', code_lines(20))
        self.intend()
        self.check()
        self.assertEqual(self.result()['numbers']['new_modules'], ['src/c'])
        self.assertEqual(self.held(), ['new-subsystem'])

    def test_tests_and_docs_do_not_count_as_modules(self):
        self.write('src/a/x.py', code_lines(20))
        self.write('tests/test_x.py', code_lines(20))
        self.write('docs/guide/howto.md', 'text\n')
        self.write('src/a/api/routes.py', code_lines(5))
        self.intend()
        self.check()
        self.assertEqual(self.result()['numbers']['modules'], ['src/a'])
        self.assertEqual(self.held(), [])

    def test_new_architecture_from_a_deploy_target(self):
        self.write('Dockerfile', 'FROM python\n')
        self.write('src/a/x.py', code_lines(heavy_check.ARCH_LINES - 10))
        self.intend()
        self.check()
        self.assertEqual(self.held(), [])
        self.write('src/a/x.py', code_lines(heavy_check.ARCH_LINES + 10))
        self.check()
        self.assertEqual(self.held(), ['new-architecture'])

    def rebase_here(self):
        """今の作業ツリーをコミットし、main (マージ基点) をそこへ動かす。"""
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'more base')
        self.git('branch', '-f', 'main', 'HEAD')

    def test_new_architecture_from_a_dependency_swap(self):
        self.write('src/old/legacy.py', code_lines(heavy_check.ARCH_DELETED_LINES + 10))
        self.rebase_here()
        os.remove(os.path.join(self.repo, 'src', 'old', 'legacy.py'))
        self.check()
        self.assertEqual(self.held(), [])  # 消しただけで、依存は入れ替えていない
        self.write('pyproject.toml', '[project]\nname = "app"\ndependencies = ["fastapi"]\n')
        self.check()
        self.assertEqual(self.held(), ['new-architecture'])

    def test_the_pbi_folder_is_not_counted(self):
        self.write('docs/pbi/notes/huge.py', code_lines(2000))
        self.write('docs/pbi/package.json', '{}\n')
        self.intend()
        self.check()
        self.assertEqual(self.result()['numbers']['files'], 0)
        self.assertEqual(self.held(), [])

    def test_heavy_draft_passes_the_question_check_and_carries_the_numbers(self):
        self.write('apps/web/package.json', '{"name": "web"}\n')
        self.write('apps/web/src/main.py', code_lines(400))
        self.intend()
        for lang in ('ja', 'en'):
            with self.subTest(lang=lang):
                code, out, err = self.check('--lang', lang)
                self.assertEqual(code, 0, err)
                draft = os.path.join(self.pbi, 'notes', 'heavy-review.question.md')
                self.assertIn('draft ' + draft.replace(os.sep, '/').split('/repo/')[-1], out.replace('\\', '/'))
                with open(draft, encoding='utf-8') as f:
                    text = f.read()
                errors, _ = design_record.lint_text(text, 'decision')
                self.assertEqual(errors, [])
                self.assertIn('review.fake-heavy', text)
                self.assertIn('opus xhigh, sonnet high', text)
                self.assertIn('| 2 | 2 | 0 | 401 | 0 | 1 | 1 |', text)
                self.assertIn('`review.code-review`', text)  # ふつうの見直しは heavy-ask でない次の行

    def test_cost_follows_the_estimate(self):
        self.write('apps/web/package.json', '{"name": "web"}\n')
        self.write('apps/web/src/main.py', code_lines(400))
        self.intend()
        self.workflow([{**HEAVY_ROW, 'models': ['opus xhigh', 'inherit']}, 'review.code-review', 'review.builtin'])
        self.check('--designer-model', 'sonnet', '--usual-model', 'haiku')
        result = self.result()
        content = result['content_tokens']
        prices = common.load_prices(self.home)['models']
        people = result['rows'][0]['reviewers']
        self.assertEqual([(p['spec'], p['model']) for p in people],
                         [('opus xhigh', 'opus'), ('inherit', 'sonnet'), ('lead (designer)', 'sonnet')])
        shape = heavy_check.ESTIMATE['reviewer']
        tokens_in = shape['base_in'] + shape['reads'] * content
        price = prices['claude-opus-5-5']
        self.assertEqual(people[0]['usd'], round((tokens_in * price['input'] + shape['out'] * price['output']) / 1e6, 2))
        self.assertEqual(result['rows'][0]['total_usd'], round(sum(p['usd'] for p in people), 2))
        self.assertEqual((result['usual']['model'], result['usual']['spec']), ('haiku', 'review.code-review'))

    def test_no_heavy_row_means_none(self):
        self.workflow(['review.code-review', 'review.builtin'])
        code, out, _ = self.check()
        self.assertEqual((code, out.strip()), (0, 'verdict none (no heavy-ask row in the review stage)'))
        self.assertIsNone(self.result())

    def shown(self):
        row = {'schema': 'atelier.presentation/1', 'event': 'shown', 'ts': common.now().isoformat(timespec='seconds'),
               'run_id': None, 'seq': 0, 'kind': 'decision', 'format': 'html'}
        with open(os.path.join(self.pbi, 'presentations.jsonl'), 'a', encoding='utf-8') as f:
            f.write(json.dumps(row) + '\n')

    def heavy(self):
        self.write('apps/web/package.json', '{"name": "web"}\n')
        self.write('apps/web/src/main.py', code_lines(400))
        self.intend()
        self.check()

    def record(self, *args):
        return self.run_cli('record', 'docs/pbi', *args)

    def adapters(self):
        return common.load_json(os.path.join(self.pbi, 'adapters.json'), {})

    def test_run_needs_the_user_and_the_page(self):
        self.heavy()
        code, _, err = self.record('--decision', 'run', '--by', 'auto')
        self.assertEqual(code, 3)
        self.assertIn('only the user can say run', err)
        code, _, err = self.record('--decision', 'run', '--by', 'user')
        self.assertEqual(code, 3)
        self.assertIn('the question page was not shown after the check', err)
        self.assertNotIn('heavy-review', self.adapters())
        self.shown()
        code, out, err = self.record('--decision', 'run', '--by', 'user')
        self.assertEqual(code, 0, err)
        record = self.adapters()['heavy-review']
        self.assertEqual((record['decision'], record['asked'], record['heavy'], record['criteria']),
                         ('run', True, True, ['new-app']))
        self.assertEqual(record['diff_sha256'], self.result()['diff_sha256'])
        # 記録があれば、check は聞き直さず、差分を測り直さない
        code, out, _ = self.check()
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith('recorded run by user ('), out)

    def test_a_page_shown_before_the_check_does_not_count(self):
        os.makedirs(self.pbi, exist_ok=True)
        with open(os.path.join(self.pbi, 'presentations.jsonl'), 'w', encoding='utf-8') as f:
            f.write(json.dumps({'event': 'shown', 'kind': 'decision', 'ts': '2000-01-01T00:00:00+00:00'}) + '\n')
        self.heavy()
        code, _, err = self.record('--decision', 'run', '--by', 'user')
        self.assertEqual(code, 3, err)

    def test_auto_skip_of_a_heavy_diff_needs_a_reason(self):
        self.heavy()
        code, _, err = self.record('--decision', 'skip', '--by', 'auto')
        self.assertEqual(code, 3)
        self.assertIn('needs --reason', err)
        code, _, err = self.record('--decision', 'skip', '--by', 'auto', '--reason', 'the new folder is only fixtures')
        self.assertEqual(code, 0, err)
        record = self.adapters()['heavy-review']
        self.assertEqual((record['decision'], record['asked'], record['heavy'], record['reason']),
                         ('skip', False, False, 'the new folder is only fixtures'))

    def test_light_skip_and_user_skip(self):
        self.write('src/a/x.py', code_lines(40))
        self.check()
        code, _, err = self.record('--decision', 'skip', '--by', 'auto')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.adapters()['heavy-review']['reason'], 'no criterion holds')
        os.remove(os.path.join(self.pbi, 'adapters.json'))
        self.heavy()
        code, _, err = self.record('--decision', 'skip', '--by', 'user')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.adapters()['heavy-review']['asked'], True)

    def test_record_without_a_check_is_an_error(self):
        os.makedirs(self.pbi, exist_ok=True)
        code, _, err = self.record('--decision', 'skip', '--by', 'auto')
        self.assertEqual(code, 2)
        self.assertIn('run heavy_check.py check first', err)

    def test_other_adapters_entries_are_kept(self):
        self.write('src/a/x.py', code_lines(40))
        self.check()
        common.write_json(os.path.join(self.pbi, 'adapters.json'), {'map': {'decision': 'repo', 'asked': True}})
        self.record('--decision', 'skip', '--by', 'auto')
        self.assertEqual(self.adapters()['map'], {'decision': 'repo', 'asked': True})


class ParseTest(unittest.TestCase):
    def test_numstat_and_name_status_with_renames(self):
        numstat = '3\t1\tsrc/a.py\x002\t0\t\x00old/b.py\x00new/b.py\x00-\t-\timg.png\x00'
        self.assertEqual(heavy_check.parse_numstat_z(numstat),
                         {'src/a.py': (3, 1), 'new/b.py': (2, 0), 'img.png': (0, 0)})
        status = 'M\x00src/a.py\x00R087\x00old/b.py\x00new/b.py\x00A\x00c.py\x00'
        self.assertEqual(heavy_check.parse_name_status_z(status), {'src/a.py': 'M', 'new/b.py': 'R', 'c.py': 'A'})

    def test_module_of(self):
        self.assertEqual(heavy_check.module_of('src/auth/x/y.ts'), 'src/auth')
        self.assertEqual(heavy_check.module_of('main.py'), '.')


if __name__ == '__main__':
    unittest.main()
