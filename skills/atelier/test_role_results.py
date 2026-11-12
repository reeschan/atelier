"""role_results.py (役割の結果の確認と、波のレビューの束) のテスト。

CLI を外から動かし、終了コード・出力の行・`--out` のファイルだけを見る。
期待値は仕様 `docs/specs/atelier-v6.md` の `atelier.review/1`・`atelier.test-result/1`・
`atelier.episode/1` の節と、`role_results.py --help` から取る。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / 'role_results.py'
EXAMPLES = HERE / 'examples' / 'roles'
WAVE = EXAMPLES / 'wave'
# 2000-01-01。報告より verify の JSON を新しくするための固定の時刻 (写した環境の時計に左右されない)
OLD = 946684800
# 2033-05-18。verify より報告を新しくするための固定の時刻
FUTURE = 2000000000
# 本物の ~/.claude/harness-log.json を読ませないための、無いファイル (作らない)
NO_CONFIG = Path(tempfile.gettempdir()) / f'role-results-no-config-{uuid.uuid4().hex}.json'


def make_temp_dir():
    """中に書ける一時ディレクトリ。既定の権限で作る (tempfile の 0o700 は権限を絞った環境で書けなくなる)。"""
    while True:
        path = Path(tempfile.gettempdir()) / f'role-results-{uuid.uuid4().hex}'
        try:
            path.mkdir()
            return path
        except FileExistsError:
            continue


def run(*args):
    """role_results.py を動かして (終了コード, stdout と stderr) を返す。

    HARNESS_LOG_CONFIG は一時の無いファイルにする (本物の ~/.claude の設定を読ませない)。
    """
    env = dict(os.environ, HARNESS_LOG_CONFIG=str(NO_CONFIG))
    proc = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                          encoding='utf-8', env=env)
    return proc.returncode, proc.stdout + proc.stderr


def edit_json(path, fn):
    """JSON を読んで fn で変えてから書き戻す。"""
    path = Path(path)
    data = json.loads(path.read_text(encoding='utf-8'))
    fn(data)
    path.write_text(json.dumps(data), encoding='utf-8')


class CheckTest(unittest.TestCase):
    """`check` が 3 つの schema を確かめること。"""

    def setUp(self):
        self.dir = make_temp_dir()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def write(self, name, data):
        path = self.dir / f'{name}-{uuid.uuid4().hex}.json'
        path.write_text(json.dumps(data), encoding='utf-8')
        return path

    def test_examples_check_clean(self):
        """仕様にある 3 つの schema の正しい例は、どれも ok になる。"""
        code, out = run('check', str(EXAMPLES / 'review.json'), str(EXAMPLES / 'test-result.json'),
                        str(EXAMPLES / 'episode.json'))
        self.assertEqual(code, 0, out)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 3, out)
        for line in lines:
            self.assertTrue(line.endswith(': ok'), line)

    def test_missing_field_is_named(self):
        """必須の項目が欠けたら、その項目の名前を出して終了コード 3。"""
        cases = (
            ('review.json', ('report', 'pbi', 'reviewer.model', 'verify.failed', 'criteria', 'recommendation')),
            ('test-result.json', ('kind', 'commands', 'failing', 'recommendation')),
            ('episode.json', ('episode', 'seq', 'status', 'summary')),
        )
        for name, keys in cases:
            for key in keys:
                with self.subTest(file=name, missing=key):
                    data = json.loads((EXAMPLES / name).read_text(encoding='utf-8'))
                    if key == 'reviewer.model':
                        data['reviewer'].pop('model')
                    elif key == 'verify.failed':
                        data['verify'].pop('failed')
                    else:
                        data.pop(key)
                    path = self.write(name, data)
                    code, out = run('check', str(path))
                    self.assertEqual(code, 3, out)
                    self.assertIn(key, out)

    def test_bad_enum_values(self):
        """決まった値のどれでもない値を書いたら、その項目を名指しして終了コード 3。"""
        def drop_state(d):
            d.update(status='question', question={'kind': 'decision', 'path': 'q.md', 'summary': 'need a call'})

        cases = (
            ('review.json', lambda d: d['criteria'][0].update(verdict='maybe'), 'criteria[0]'),
            ('review.json', lambda d: d['findings'][0].update(severity='blocker'), 'findings[0]'),
            ('review.json', lambda d: d.update(recommendation='maybe'), 'recommendation'),
            ('test-result.json', lambda d: d.update(kind='nope'), 'kind'),
            ('episode.json', lambda d: d.update(episode='nope'), 'episode'),
            ('episode.json', lambda d: d.update(status='nope'), 'status'),
            ('episode.json', lambda d: d.update(status='question',
                                                question={'kind': 'nope', 'path': 'q.md', 'summary': 'x'}), 'question'),
            ('episode.json', drop_state, 'state'),
        )
        for name, mutate, label in cases:
            with self.subTest(file=name, field=label):
                data = json.loads((EXAMPLES / name).read_text(encoding='utf-8'))
                mutate(data)
                path = self.write(name, data)
                code, out = run('check', str(path))
                self.assertEqual(code, 3, out)
                self.assertIn(label, out)

    def test_check_wrong_schema_file(self):
        """3 つの schema のどれでもないファイル (束のファイルを含む) は誤り。"""
        cases = (
            {'wave': 1, 'rows': []},
            {'schema': 'atelier.review/2', 'report': 'PBI-001', 'pbi': 'PBI-001'},
        )
        for data in cases:
            with self.subTest(data=data):
                path = self.write('other', data)
                code, out = run('check', str(path))
                self.assertEqual(code, 3, out)
                self.assertIn('unknown schema', out)

    def test_check_non_string_schema(self):
        """schema がリストでも traceback を出さず、残りのファイルの確かめを続ける。"""
        bad = self.write('bad', {'schema': ['x']})
        code, out = run('check', str(bad), str(EXAMPLES / 'review.json'))
        self.assertEqual(code, 3, out)
        self.assertNotIn('Traceback', out)
        self.assertIn('unknown schema', out)
        self.assertIn(f'{EXAMPLES / "review.json"}: ok', out)


class JudgedEventTest(unittest.TestCase):
    """`check` が通ったレビュー・テストの結果を、イベントとして残すこと。"""

    def setUp(self):
        self.dir = make_temp_dir()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def write(self, *parts, data):
        """<一時の dir>/<parts> に JSON を書いて、そのパスを返す。"""
        path = self.dir.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding='utf-8')
        return path

    def example(self, name):
        return json.loads((EXAMPLES / name).read_text(encoding='utf-8'))

    def events(self):
        """<dir>/events.jsonl の行。無ければ空。"""
        path = self.dir / 'events.jsonl'
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]

    def test_review_judged_once_per_content(self):
        """review の check は review.judged を 1 行出し、同じ中身の 2 回目は出さず、変えたら出す。"""
        review = self.example('review.json')
        path = self.write('reviews', 'PBI-1.review.json', data=review)
        code, out = run('check', str(path))
        self.assertEqual(code, 0, out)
        rows = self.events()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]['type'], 'review.judged')
        self.assertEqual(rows[0]['data'], {
            'report': 'EX-1',
            'recommendation': 'rework',
            'findings': {'critical': 0, 'major': 1, 'minor': 0},
            'criteria': {'pass': 1, 'fail': 1, 'unknown': 1},
        })

        code, out = run('check', str(path))
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.events()), 1, '同じ中身の check の繰り返しで 2 行目が出た')

        review['recommendation'] = 'pass'
        path.write_text(json.dumps(review), encoding='utf-8')
        code, out = run('check', str(path))
        self.assertEqual(code, 0, out)
        rows = self.events()
        self.assertEqual(len(rows), 2, rows)
        self.assertEqual(rows[1]['type'], 'review.judged')
        self.assertEqual(rows[1]['data']['recommendation'], 'pass')

    def test_test_judged_counts_commands_and_causes(self):
        """test の結果は test.judged を出し、passed・failed が commands の code (0 か否か) の数と合う。"""
        result = self.example('test-result.json')
        result['commands'] = [
            {'command': 'python -m pytest -q', 'code': 0},
            {'command': 'python -m unittest', 'code': 1},
            {'command': 'python --version', 'code': 2},
        ]
        result['failing'] = [
            {'test': 'a', 'file': 'b.py', 'pbi': 'PBI-1', 'cause': 'regression', 'why': 'x'},
            {'test': 'c', 'file': 'd.py', 'pbi': 'PBI-1', 'cause': 'unknown', 'why': 'y'},
        ]
        path = self.write('verify', 'tests', 'x.test-result.json', data=result)
        code, out = run('check', str(path))
        self.assertEqual(code, 0, out)
        rows = self.events()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]['type'], 'test.judged')
        self.assertEqual(rows[0]['data'], {
            'kind': 'wave',
            'passed': 1,
            'failed': 2,
            'failing': {'intended': 0, 'regression': 1, 'unknown': 1},
        })

    def test_test_judged_same_body_at_other_path(self):
        """同じ中身でもパスが違えば別の結果として出し、同じファイルの 2 回目は出さない。

        合格した test-result は波を持たず、波ごとに同じ本文が別のパスに書かれる。
        中身だけを once の鍵にすると 2 波目以降が落ちる (final review 8)。
        """
        result = self.example('test-result.json')
        # 全問合格の結果は波・PBI・時刻を持たず、波ごとに同じ本文になる
        result['commands'] = [{'command': 'python -m unittest discover', 'code': 0}]
        result['failing'] = []
        result['recommendation'] = 'pass'
        paths = [self.write('verify', 'tests', f'wave-{n}.test-result.json', data=result) for n in (1, 2, 3)]
        for path in paths:
            code, out = run('check', str(path))
            self.assertEqual(code, 0, out)
        rows = self.events()
        self.assertEqual(len(rows), 3, rows)
        self.assertEqual([row['type'] for row in rows], ['test.judged'] * 3)
        self.assertEqual([row['data'] for row in rows], [
            {'kind': 'wave', 'passed': 1, 'failed': 0, 'failing': {'intended': 0, 'regression': 0, 'unknown': 0}},
        ] * 3)
        code, out = run('check', str(paths[0]))
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.events()), 3, '同じファイルの check の繰り返しで 4 行目が出た')

    def test_test_judged_again_in_another_run(self):
        """同じ合格の結果でも run が変われば出し直し、同じ run の中では 1 行のまま。

        `.events/seen.txt` は実行をまたいで残るので、once の鍵に run_id を入れないと
        次の run の check が落ちる (final review の再現: run-A で 1 行、run-B でもう 1 行)。
        """
        result = self.example('test-result.json')
        result['commands'] = [{'command': 'python -m unittest discover', 'code': 0}]
        result['failing'] = []
        result['recommendation'] = 'pass'
        path = self.write('verify', 'tests', 'final.test-result.json', data=result)
        run_json = self.dir / 'conductor' / 'run.json'
        run_json.parent.mkdir(parents=True, exist_ok=True)

        def set_run(run_id):
            run_json.write_text(json.dumps({'run_id': run_id}), encoding='utf-8')

        set_run('run-A')
        code, out = run('check', str(path))
        self.assertEqual(code, 0, out)
        rows = self.events()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]['run_id'], 'run-A')

        code, out = run('check', str(path))
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.events()), 1, '同じ run の check の繰り返しで 2 行目が出た')

        set_run('run-B')
        code, out = run('check', str(path))
        self.assertEqual(code, 0, out)
        rows = self.events()
        self.assertEqual(len(rows), 2, rows)
        self.assertEqual(rows[1]['run_id'], 'run-B')

        code, out = run('check', str(path))
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.events()), 2, '同じ run の check の繰り返しで 3 行目が出た')

    def test_invalid_results_do_not_emit(self):
        """誤りのある結果ではイベントを出さない。"""
        review = self.example('review.json')
        review['recommendation'] = 'maybe'
        review_path = self.write('reviews', 'PBI-1.review.json', data=review)
        result = self.example('test-result.json')
        result['kind'] = 'nope'
        result_path = self.write('verify', 'tests', 'x.test-result.json', data=result)
        code, out = run('check', str(review_path), str(result_path))
        self.assertEqual(code, 3, out)
        self.assertEqual(self.events(), [])

    def test_outside_known_dirs_do_not_emit(self):
        """reviews/ と verify/tests/ の外のファイルでは events.jsonl を作らない (スキルのフォルダに書かない)。"""
        code, out = run('check', str(EXAMPLES / 'review.json'), str(EXAMPLES / 'test-result.json'))
        self.assertEqual(code, 0, out)
        self.assertFalse((HERE / 'events.jsonl').exists())
        self.assertFalse((self.dir / 'events.jsonl').exists())
        # 一時の dir の中でも、決まった場所の外なら出さない
        path = self.write('review.json', data=self.example('review.json'))
        code, out = run('check', str(path))
        self.assertEqual(code, 0, out)
        self.assertFalse((self.dir / 'events.jsonl').exists())

    def test_episode_question_topic(self):
        """出番の結果の question の topic は決まった値だけを通し、無くても通る。"""
        base = self.example('episode.json')
        base.update(status='question', state='episode.state.md',
                    question={'kind': 'decision', 'path': 'q.md', 'summary': 'pick one'})
        for topic, expected in ((None, 0), ('omm', 0), ('poem', 3)):
            with self.subTest(topic=topic):
                data = json.loads(json.dumps(base))
                if topic is not None:
                    data['question']['topic'] = topic
                path = self.write('episode.json', data=data)
                code, out = run('check', str(path))
                self.assertEqual(code, expected, out)
                if expected:
                    self.assertIn('topic', out)
                else:
                    self.assertIn(': ok', out)


class BundleTest(unittest.TestCase):
    """`bundle` が報告ごとに自分で確かめ、1 報告 1 行の表を出すこと。"""

    def setUp(self):
        self.root = make_temp_dir()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.count = 0
        self.new_wave()

    def new_wave(self):
        """`examples/roles/wave` を写した、新しい作業ディレクトリにする。"""
        self.count += 1
        self.base = self.root / f'wave-{self.count}'
        shutil.copytree(WAVE, self.base)
        # 報告と verify の JSON の新旧は、写した環境の時計に左右されないように固定する
        os.utime(self.base / 'reports' / 'PBI-901.md', (OLD, OLD))

    def bundle(self, wave=1):
        """bundle を動かして (終了コード, 出力, --out のパス) を返す。"""
        out = self.root / 'bundle.json'
        if out.exists():
            out.unlink()
        code, text = run('bundle', '--wave', str(wave), '--dir', str(self.base), '--out', str(out))
        return code, text, out

    def row(self, text, name='PBI-901'):
        """表のその PBI の行。無ければ失敗。"""
        for line in text.splitlines():
            if line.startswith(f'{name}  '):
                return line
        self.fail(f'no row for {name} in:\n{text}')

    def two_pbi_wave(self):
        """PBI-901 と PBI-902 の 2 件の波にする (PBI-902 は 901 の写しで、名前だけ変える)。"""
        files = ('PBI-901.md', 'reports/PBI-901.md', 'reviews/PBI-901.review.json',
                 'verify/PBI-901.json', 'PBI-901.route.json')
        for name in files:
            src = self.base / name
            dst = self.base / name.replace('PBI-901', 'PBI-902')
            dst.write_text(src.read_text(encoding='utf-8').replace('PBI-901', 'PBI-902'), encoding='utf-8')
        (self.base / 'verify' / 'PBI-902.unchanged.json').write_text('{}', encoding='utf-8')
        (self.base / 'waves.json').write_text(
            json.dumps({'waves': [{'n': 1, 'pbis': ['PBI-901', 'PBI-902']}]}), encoding='utf-8')
        os.utime(self.base / 'reports' / 'PBI-902.md', (OLD, OLD))

    def test_bundle_example_wave(self):
        """そろった波は、終了コード 0 で PBI ごとに 1 行になり、束のファイルに残る。"""
        code, out, out_path = self.bundle()
        self.assertEqual(code, 0, out)
        line = self.row(out)
        self.assertIn('verify ok', line)
        self.assertIn('unchanged ok', line)
        self.assertIn('review pass', line)
        self.assertNotIn('error', line)
        self.assertIn(f'bundle: {out_path.as_posix()}', out)
        bundle = json.loads(out_path.read_text(encoding='utf-8'))
        self.assertEqual(bundle['wave'], 1)
        self.assertEqual([r['report'] for r in bundle['rows']], ['PBI-901'])

    def test_bundle_missing_report_review_verify(self):
        """報告・レビュー・verify の JSON が欠けたら、その行に誤りが出て、ほかの行は残る。"""
        cases = (
            ('reports/PBI-901.md', 'no report'),
            ('reviews/PBI-901.review.json', 'no reviews/PBI-901.review.json'),
            ('verify/PBI-901.json', 'no verify/PBI-901.json'),
        )
        for name, needle in cases:
            with self.subTest(missing=name):
                self.new_wave()
                self.two_pbi_wave()
                (self.base / name).unlink()
                code, out, _ = self.bundle()
                self.assertEqual(code, 3, out)
                self.assertIn(needle, self.row(out))
                self.assertNotIn('error', self.row(out, 'PBI-902'))

    def test_bundle_stale_verify(self):
        """verify の JSON が報告より新しくないときは、その行に古さの誤りが出る。"""
        os.utime(self.base / 'reports' / 'PBI-901.md', (FUTURE, FUTURE))
        code, out, _ = self.bundle()
        self.assertEqual(code, 3, out)
        self.assertIn('is not newer than the report', self.row(out))

    def test_bundle_unjudged_criterion(self):
        """PBI の受け入れ基準の番号に判定が無ければ、その番号を名指しする。"""
        pbi = self.base / 'PBI-901.md'
        pbi.write_text(pbi.read_text(encoding='utf-8')
                       + '\n## Acceptance criteria\n1. runs\n2. builds\n3. is documented\n', encoding='utf-8')
        code, out, _ = self.bundle()
        self.assertEqual(code, 3, out)
        self.assertIn('no verdict for criteria 3', self.row(out))

    def test_bundle_review_json_mismatch(self):
        """review.json が報告・verify の JSON と食い違ったら、誤りか mismatch の印を付ける。"""
        def other_report(d):
            d.update(report='PBI-777')

        def other_pbi(d):
            d.update(pbi='PBI-777')

        def fail_without_declaring(d):
            # 表の verify は失敗なのに、review.json の verify.failed は空のまま
            edit_json(self.base / 'verify' / 'PBI-901.json', lambda v: v['results'][0].update(code=1))

        def fail_criterion_but_pass(d):
            d['criteria'][0].update(verdict='fail')
            d.update(recommendation='pass')

        def duplicate_number_hides_failure(d):
            edit_json(self.base / 'verify' / 'PBI-901.json',
                      lambda v: v['results'].insert(0, {'n': 1, 'command': 'python --version', 'code': 1, 'log': ''}))

        cases = (
            ('another report', other_report, 3, ('review.json is for report PBI-777, not PBI-901',)),
            ('another pbi', other_pbi, 3, ('review.json is for PBI-777, not PBI-901',)),
            ('failed verify not declared', fail_without_declaring, 0, ('verify fail 1', 'mismatch')),
            ('fail criterion with pass', fail_criterion_but_pass, 0, ('verify ok', 'mismatch')),
            ('duplicate verify number', duplicate_number_hides_failure, 3, ('number twice',)),
        )
        for label, mutate, expected, needles in cases:
            with self.subTest(case=label):
                self.new_wave()
                edit_json(self.base / 'reviews' / 'PBI-901.review.json', mutate)
                code, out, _ = self.bundle()
                self.assertEqual(code, expected, out)
                line = self.row(out)
                for needle in needles:
                    self.assertIn(needle, line)

    def test_bundle_same_model_reviewer(self):
        """レビュー担当のモデルが、その報告を作ったモデルと同じなら誤り。"""
        for model in ('sonnet', 'claude-sonnet-5-5'):
            with self.subTest(model=model):
                self.new_wave()
                edit_json(self.base / 'reviews' / 'PBI-901.review.json',
                          lambda d: d['reviewer'].update(model=model))
                code, out, _ = self.bundle()
                self.assertEqual(code, 3, out)
                self.assertIn('is the model that built it', self.row(out))

    def test_bundle_built_model_unknown(self):
        """作ったモデルが分からない (built も runs.jsonl も route.json も無い) なら誤り。"""
        edit_json(self.base / 'reviews' / 'PBI-901.review.json', lambda d: d.pop('built', None))
        (self.base / 'PBI-901.route.json').unlink()
        code, out, _ = self.bundle()
        self.assertEqual(code, 3, out)
        self.assertIn('built model unknown', self.row(out))

    def test_bundle_one_row_per_report_latest_attempt(self):
        """表の行は PBI ごとに 1 つで、いちばん新しい試行の報告を使う。"""
        (self.base / 'reports' / 'PBI-901.rework-2.md').write_text('Status: DONE\n', encoding='utf-8')
        # PBI-9010 は PBI-901 ではない別の PBI。試行の番号を PBI-901 のどれより大きくして、
        # 名前に PBI-901 を含むだけで拾う実装なら、この報告が PBI-901 の行を奪うようにする。
        (self.base / 'reports' / 'PBI-9010.rework-5.md').write_text('Status: DONE\n', encoding='utf-8')
        code, out, _ = self.bundle()
        self.assertEqual(code, 3, out)  # やり直しの報告にはレビューがまだ無い
        # 行の名前は PBI-901 の報告のまま (PBI-9010 の報告に乗っ取られない)
        self.assertEqual(self.row(out, 'PBI-901.rework-2').split('  ')[0], 'PBI-901.rework-2')
        self.assertNotIn('PBI-9010', out)
        self.assertNotIn('\nPBI-901  ', '\n' + out)

        self.new_wave()
        self.two_pbi_wave()
        code, out, _ = self.bundle()
        self.assertEqual(code, 0, out)
        rows = [line for line in out.splitlines() if line.startswith('PBI-')]
        self.assertEqual(len(rows), 2, out)
        self.assertIn('PBI-902', '\n'.join(rows))

    def test_bundle_review_round_limit(self):
        """レビューは PBI ごとに 2 回まで。2 回目でまだ通らない行に limit が付き、1 回目や通った行には付かない。"""
        code, out, _ = self.bundle()
        self.assertEqual(code, 0, out)
        self.assertIn('round 1/2', self.row(out))
        self.assertNotIn('limit', self.row(out))

        review_path = self.base / 'reviews' / 'PBI-901.review.json'
        review = json.loads(review_path.read_text(encoding='utf-8'))
        for name, recommendation, limit in (('PBI-901', 'rework', False),
                                            ('PBI-901.rework-1', 'pass', False),
                                            ('PBI-901.rework-1', 'rework', True)):
            with self.subTest(report=name, recommendation=recommendation):
                self.new_wave()
                if name != 'PBI-901':
                    for kind, ext in (('reports', '.md'), ('verify', '.json')):
                        src = self.base / kind / f'PBI-901{ext}'
                        text = src.read_text(encoding='utf-8')
                        dst = self.base / kind / f'{name}{ext}'
                        dst.write_text(text.replace('"name": "PBI-901"', f'"name": "{name}"'), encoding='utf-8')
                    os.utime(self.base / 'reports' / f'{name}.md', (OLD, OLD))
                data = dict(review, report=name, recommendation=recommendation)
                (self.base / 'reviews' / f'{name}.review.json').write_text(json.dumps(data), encoding='utf-8')
                code, out, _ = self.bundle()
                line = self.row(out, name)
                self.assertNotIn('error', line, out)
                self.assertIn(f"round {1 if name == 'PBI-901' else 2}/2", line)
                self.assertEqual('limit' in line.split('  '), limit, line)

    def test_bundle_all_verify_deferred(self):
        """verify のコマンドが全部あとに回すものなら、verify の JSON が無くても誤りにしない。"""
        (self.base / 'PBI-901.md').write_text(
            '# PBI-901: bundle example\n\n'
            '## Verify commands\n'
            '1. `python -m unittest discover`\n', encoding='utf-8')
        # そのコマンドを tests-at-end.json の deferred に合わせる (verify.py --skip-deferred は流さずに終わる)
        (self.base / 'tests-at-end.json').write_text(
            json.dumps({'enabled': True, 'deferred': ['python -m unittest']}), encoding='utf-8')
        (self.base / 'verify' / 'PBI-901.json').unlink()
        code, out, _ = self.bundle()
        self.assertEqual(code, 0, out)
        line = self.row(out)
        self.assertNotIn('no verify', line)
        self.assertIn('verify: all deferred', line)

    def test_bundle_no_numbered_verify_commands(self):
        """番号付きの verify コマンドが無い PBI は、verify の JSON の欠けを誤りにする。"""
        (self.base / 'PBI-901.md').write_text(
            '# PBI-901: bundle example\n\n'
            '## Verify commands\n'
            '- `python -m unittest discover`\n', encoding='utf-8')
        (self.base / 'verify' / 'PBI-901.json').unlink()
        code, out, _ = self.bundle()
        self.assertNotEqual(code, 0, out)
        line = self.row(out)
        self.assertIn('no verify', line)
        self.assertNotIn('verify: all deferred', line)

    def test_bundle_no_wave(self):
        """waves.json に無い波は終了コード 2 で、束のファイルを書かない。"""
        code, out, out_path = self.bundle(wave=9)
        self.assertEqual(code, 2, out)
        self.assertIn('wave 9 is not in', out)
        self.assertFalse(out_path.exists())

        self.new_wave()
        (self.base / 'waves.json').unlink()
        code, out, out_path = self.bundle()
        self.assertEqual(code, 2, out)
        self.assertFalse(out_path.exists())

    def test_bundle_criteria_number_forms(self):
        """受け入れ基準の番号は `1)` `2:` の形も読み、ほかの節の番号は数えない。"""
        (self.base / 'PBI-901.md').write_text(
            '# PBI-901: bundle example\n\n'
            '## Verify commands\n'
            '1. `python --version`\n'
            '2. `python docs/pbi/tools/unchanged.py check PBI-901`\n\n'
            '## Acceptance criteria\n'
            '1) the tool runs\n'
            '2: the wave is built\n'
            '3) the review has a verdict\n\n'
            '## Notes\n'
            '4. this number is not a criterion\n', encoding='utf-8')
        # 例の review.json は基準 1・2 しか判定していないので、3) を読めば 3 が欠けた誤りになる。
        # Notes の 4. まで数えてしまう実装なら、3 に加えて 4 も名指しされてしまう。
        code, out, _ = self.bundle()
        self.assertEqual(code, 3, out)
        line = self.row(out)
        self.assertIn('no verdict for criteria 3', line)
        self.assertNotIn('4', line)


    def test_bundle_bad_pbi_name_in_waves(self):
        """waves.json の PBI 名が絶対パスや glob の文字を含むと、traceback を出さずその行の誤り (終了コード 3) にする。"""
        for bad in ('C:/abs/PBI-001', '/abs/PBI-001', 'PBI-a/b', 'PBI-*', '../PBI-901'):
            with self.subTest(name=bad):
                self.new_wave()
                (self.base / 'waves.json').write_text(
                    json.dumps({'waves': [{'n': 1, 'pbis': ['PBI-901', bad]}]}), encoding='utf-8')
                code, out, _ = self.bundle()
                self.assertEqual(code, 3, out)
                self.assertNotIn('Traceback', out)
                self.assertIn('invalid PBI name', out)
                self.assertIn('review pass', self.row(out))


if __name__ == '__main__':
    unittest.main()
