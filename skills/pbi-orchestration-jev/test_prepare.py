"""prepare.py の表 (10 列)・sensitive のゲート・jev-log の変化だけの記録・--tier を確かめる。

Jev を呼ばせないため、応答 (jev/<PBI>.brief.json・.pbi_quality.json) を先に置いてから
prepare.py を別プロセスで起動する。テンプレートの home は別の一時ディレクトリに作り --home で渡す。
sensitive の PBI には brief の応答だけを置く (pbi_quality は送られないので Jev は呼ばれない)。
--only は応答があっても Jev を呼び直すので、資格情報を無いことにして即座に失敗させる。
"""
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'prepare.py')
sys.path.insert(0, HERE)
import prepare  # noqa: E402

NOW = '2026-10-20T09:00:00+09:00'
PAST = '2026-10-23T09:00:00+09:00'
UNTIL = '2026-10-22T23:59:00+09:00'
SYNCED = '2026-10-16T09:00:00+09:00'
FLASH = [{'runner': 'opencode', 'model': 'opencode-go/fast-model', 'effort': 'max'}]
ALL_TIERS = {'name': 'all-tiers', 'description': '全段に当てる',
             'tiers': {t: FLASH for t in ('haiku', 'sonnet', 'opus')}}
ZERO = {'probabilities': {'0': 0.9, '1': 0.1, '2': 0.0}, 'confidence': 0.9}
LOW_TWO = {'probabilities': {'0': 0.0, '1': 0.1, '2': 0.9}, 'confidence': 0.5}
ALL_ZERO = {k: ZERO for k in ('novelty', 'coupling', 'ambiguity')}
SMALL = {'files_changed': 1, 'modules': 1, 'sensitive': False}
SENSITIVE = {'files_changed': 1, 'modules': 1, 'sensitive': True}
HEADER = ['PBI', '段', 'route の先頭', 'review', '補正後の値 (新規性/結合度/あいまいさ)', '理由',
          '優先テンプレート', '外した手順', '品質の警告', 'メモ']
MULTI = {'name': 'multi-runner', 'tiers': {
    t: [FLASH[0], {'runner': 'agent', 'model': t, 'effort': 'max'}] for t in ('haiku', 'sonnet', 'opus')}}
EMPTY = {}


def application(until=UNTIL, on_expire='ask'):
    return {'id': 'a-20261016T090000', 'template': 'all-tiers', 'until': until, 'on_expire': on_expire,
            'reason': '一時措置', 'started_at': SYNCED, 'started_via': 'chat'}


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def write_home(d, templates_, app):
    """home に templates/ と (app があれば) 写し overrides.json を置く。"""
    for name, template in templates_.items():
        write_json(os.path.join(d, 'templates', f'{name}.json'), template)
    if app is not None:
        write_json(os.path.join(d, 'overrides.json'), {'synced_at': SYNCED, 'application': app})


def write_pbi(d, name, facts, brief, quality):
    """1 件の PBI 本文・facts と、先に置く Jev の応答 (None なら置かない) を書く。"""
    write_json(os.path.join(d, f'{name}.facts.json'), facts)
    if brief is not None:
        write_json(os.path.join(d, 'jev', f'{name}.brief.json'), {'answers': brief})
    if quality is not None:
        write_json(os.path.join(d, 'jev', f'{name}.pbi_quality.json'), {'answers': quality})
    write_text(os.path.join(d, f'{name}.md'), f'# {name}: ダミー\n\n中身は適当でよい。\n')
    write_text(os.path.join(d, f'{name}.brief.md'), '目的: ダミー\n')


def rows(out):
    return [line for line in out.stdout.splitlines() if line.startswith('| PBI-')]


def cells(row):
    return [c.strip() for c in row.split('|')[1:-1]]


class PrepareTest(unittest.TestCase):
    def setUp(self):
        # tempfile の 0o700 は権限を絞った環境で読めなくなるので、既定の権限で作る。
        self.dir = os.path.join(tempfile.gettempdir(), f'pbi-prepare-{uuid.uuid4().hex}')
        os.mkdir(self.dir)
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.home = os.path.join(self.dir, 'home')
        self.base = os.path.join(self.dir, 'pbi')
        os.makedirs(os.path.join(self.home, 'templates'))
        os.makedirs(self.base)

    def use_home(self, templates_=None, app=None, agents=None):
        write_json_home(self.home, templates_, app, agents)

    def add_pbi(self, name='PBI-001', facts=None, brief=ALL_ZERO, quality=EMPTY):
        write_pbi(self.base, name, facts or SMALL, brief, quality)

    def run_prepare(self, *extra):
        env = dict(os.environ)
        env.pop('TYPESAFE_API_KEY', None)  # 応答の無いゲートを呼んでも、ネットワークに出ずに失敗させる
        env['TYPESAFE_CREDENTIALS_FILE'] = os.path.join(self.dir, 'no-credentials.json')
        cmd = [sys.executable, SCRIPT, self.base, '--home', self.home, *extra]
        return subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', env=env)

    def log(self):
        path = os.path.join(self.base, 'jev-log.jsonl')
        if not os.path.exists(path):
            return []
        with open(path, encoding='utf-8') as f:
            return [json.loads(line) for line in f if line.strip()]

    def route(self, name='PBI-001'):
        with open(os.path.join(self.base, f'{name}.route.json'), encoding='utf-8') as f:
            return json.load(f)

    # --- 優先テンプレートの行と列 ---

    def test_active_template_shows_name_and_deadline(self):
        self.use_home(app=application())
        self.add_pbi()
        out = self.run_prepare('--now', NOW)
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = out.stdout.splitlines()
        self.assertEqual(lines[0], f'優先テンプレート: all-tiers を適用中 ({UNTIL} まで、期限切れは ask) / 写し: {SYNCED} 時点')
        self.assertEqual(cells(lines[1]), HEADER)
        self.assertIn(f'all-tiers を当てた ({UNTIL} まで)', rows(out)[0])
        brief = [line for line in self.log() if line['gate'] == 'brief'][0]
        self.assertEqual(brief['template']['name'], 'all-tiers')
        self.assertTrue(brief['template']['applied'])

    def test_expired_ask_asks_confirmation(self):
        self.use_home(app=application())
        self.add_pbi()
        out = self.run_prepare('--now', PAST)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.splitlines()[0],
                         f'優先テンプレート: all-tiers は期限切れ。承認で「延長 / やめる / 期限なし」を確認する / 写し: {SYNCED} 時点')
        self.assertIn('期限切れ: 延長・やめる・期限なしの確認が要る', rows(out)[0])

    def test_expired_drop_says_not_applied(self):
        self.use_home(app=application(on_expire='drop'))
        self.add_pbi()
        out = self.run_prepare('--now', PAST)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.splitlines()[0],
                         f'優先テンプレート: all-tiers は期限切れ (drop)。当てない / 写し: {SYNCED} 時点')
        self.assertIn('期限切れ (drop) のため当てない', rows(out)[0])

    def test_without_overrides_says_no_application(self):
        self.use_home(app=None)
        self.add_pbi()
        out = self.run_prepare('--now', NOW)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.splitlines()[0], '優先テンプレート: 適用なし / 写し: なし')
        self.assertIn('適用なし', rows(out)[0])
        brief = [line for line in self.log() if line['gate'] == 'brief'][0]
        self.assertFalse(brief['template']['applied'])

    def test_until_null_shows_no_deadline(self):
        self.use_home(app=application(until=None))
        self.add_pbi()
        out = self.run_prepare('--now', PAST)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.splitlines()[0], f'優先テンプレート: all-tiers を適用中 (期限なし) / 写し: {SYNCED} 時点')
        self.assertIn('期限なしで当てた', rows(out)[0])

    def test_template_note_falls_back_to_dash(self):
        self.assertEqual(prepare.template_note({}), '—')
        self.assertEqual(prepare.template_note({'template': None}), '—')
        self.assertEqual(prepare.template_note({'template': {'name': 'x', 'applied': True, 'note': '当てた'}}), '当てた')

    # --- 表の列 ---

    def test_table_shows_corrected_scores_and_reason(self):
        """生の score が 2 でも確信度が低ければ、表には補正後の 1 が出る。"""
        self.use_home(app=None)
        self.add_pbi(brief={'novelty': LOW_TWO, 'coupling': ZERO, 'ambiguity': ZERO})
        out = self.run_prepare('--now', NOW)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(cells(out.stdout.splitlines()[1]), HEADER)
        row = cells(rows(out)[0])
        route = self.route()
        self.assertEqual(row[4], '/'.join(str(route['scores'][k]) for k in ('novelty', 'coupling', 'ambiguity')))
        self.assertEqual(row[4], '1/0/0')
        self.assertEqual(row[1], route['tier'])
        self.assertEqual(row[2], f"{route['route'][0]['runner']} {route['route'][0]['model']} {route['route'][0]['effort']}")
        self.assertEqual(row[5], route['reasons'][0].removeprefix('決定: '))
        self.assertNotIn('決定: ', row[5])

    def test_dropped_step_is_shown_with_reason(self):
        self.use_home(app=None, agents={'runners': {'opencode': {'enabled': False}}})
        write_json(os.path.join(self.base, 'routing.json'), MULTI)
        self.add_pbi()
        out = self.run_prepare('--now', NOW)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(cells(rows(out)[0])[7], 'opencode opencode-go/fast-model (未使用 (agents.json))')
        self.assertEqual(self.route()['dropped'][0]['reason'], '未使用 (agents.json)')

    def test_missing_cli_is_shown_as_dropped(self):
        """外部の実行先の CLI が無いとき、外した手順に「見つからない」が出る。"""
        self.use_home(app=None)
        write_json(os.path.join(self.base, 'routing.json'), MULTI)
        self.add_pbi()
        env = dict(os.environ)
        env.pop('TYPESAFE_API_KEY', None)
        env['TYPESAFE_CREDENTIALS_FILE'] = os.path.join(self.dir, 'no-credentials.json')
        for name in ('empty-bin', 'empty-appdata', 'empty-home'):
            os.makedirs(os.path.join(self.dir, name), exist_ok=True)
        env['PATH'] = os.path.join(self.dir, 'empty-bin')
        env['APPDATA'] = os.path.join(self.dir, 'empty-appdata')
        env['HOME'] = os.path.join(self.dir, 'empty-home')
        for key in ('FNM_DIR', 'NVM_HOME', 'NVM_DIR', 'PBI_DSH_BIN'):
            env.pop(key, None)
        cmd = [sys.executable, SCRIPT, self.base, '--home', self.home, '--now', NOW]
        out = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('opencode opencode-go/fast-model (見つからない)', cells(rows(out)[0])[7])

    # --- sensitive ---

    def test_sensitive_sends_only_brief(self):
        self.use_home(app=None)
        self.add_pbi(facts=SENSITIVE, quality=None)
        out = self.run_prepare('--now', NOW)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.base, 'jev', 'PBI-001.pbi_quality.json')))
        self.assertIn('sensitive のため pbi_quality を送らない', cells(rows(out)[0])[9])
        self.assertEqual(self.route()['tier'], 'sonnet')

    # --- jev-log ---

    def test_log_is_written_only_when_decision_changes(self):
        self.use_home(app=None)
        self.add_pbi()
        first = self.run_prepare('--now', NOW)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(len(self.log()), 1)
        second = self.run_prepare('--now', NOW)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(len(self.log()), 1)
        write_json(os.path.join(self.base, 'PBI-001.facts.json'),
                   {'files_changed': 5, 'modules': 1, 'sensitive': False})
        third = self.run_prepare('--now', NOW)
        self.assertEqual(third.returncode, 0, third.stderr)
        log = self.log()
        self.assertEqual(len(log), 2)
        self.assertEqual(log[1]['tier'], 'sonnet')
        for line in log:
            self.assertIsNotNone(datetime.datetime.fromisoformat(line['ts']).utcoffset())

    # --- --tier ---

    def test_only_with_tier_changes_that_pbi(self):
        self.use_home(app=None)
        self.add_pbi('PBI-001')
        self.add_pbi('PBI-002')
        first = self.run_prepare('--now', NOW)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(self.route('PBI-001')['tier'], 'haiku')
        self.assertEqual(self.route('PBI-002')['tier'], 'haiku')
        out = self.run_prepare('--now', NOW, '--only', 'PBI-002', '--tier', 'sonnet')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.route('PBI-002')['tier'], 'sonnet')
        self.assertEqual(self.route('PBI-001')['tier'], 'haiku')
        self.assertEqual([cells(r)[1] for r in rows(out) if cells(r)[0] == 'PBI-002'], ['sonnet'])

    def test_tier_without_only_exits_nonzero(self):
        self.use_home(app=None)
        self.add_pbi()
        out = self.run_prepare('--now', NOW, '--tier', 'sonnet')
        self.assertNotEqual(out.returncode, 0)


def write_json_home(d, templates_, app, agents):
    """home に templates/ と (あれば) overrides.json・agents.json を置く。"""
    write_home(d, {'all-tiers': ALL_TIERS} if templates_ is None else templates_, app)
    if agents is not None:
        write_json(os.path.join(d, 'agents.json'), agents)


if __name__ == '__main__':
    unittest.main()
