"""prepare.py の「優先テンプレート: …」の 1 行、表の列「優先テンプレート」、jev-log の template を確かめる。

Jev を呼ばせないため、応答 (jev/<PBI>.brief.json・.pbi_quality.json) を先に置いてから
prepare.py を別プロセスで起動する。テンプレートの home は別の一時ディレクトリに作り --home で渡す。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'prepare.py')
sys.path.insert(0, HERE)
import prepare  # noqa: E402

NOW = '2026-10-20T09:00:00+09:00'
PAST = '2026-10-23T09:00:00+09:00'
UNTIL = '2026-10-22T23:59:00+09:00'
SYNCED = '2026-10-16T09:00:00+09:00'
FLASH = [{'runner': 'opencode', 'model': 'opencode-go/deepseek-v4.1-flash', 'effort': 'max'}]
ALL_TIERS = {'name': 'deepseek-all', 'description': '全段に当てる',
             'tiers': {t: FLASH for t in ('haiku', 'sonnet', 'opus')}}
ZERO = {'probabilities': {'0': 0.9, '1': 0.1, '2': 0.0}, 'confidence': 0.9}
ALL_ZERO = {k: ZERO for k in ('novelty', 'coupling', 'ambiguity')}
SMALL = {'files_changed': 1, 'modules': 1, 'sensitive': False}


def application(until=UNTIL, on_expire='ask'):
    return {'id': 'a-20261016T090000', 'template': 'deepseek-all', 'until': until, 'on_expire': on_expire,
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


def write_base(d):
    """PBI 本文・facts・brief と、先に置く Jev の応答 (これがあれば prepare.py は Jev を呼ばない)。"""
    write_json(os.path.join(d, 'PBI-001.facts.json'), SMALL)
    write_json(os.path.join(d, 'jev', 'PBI-001.brief.json'), {'answers': ALL_ZERO})
    write_json(os.path.join(d, 'jev', 'PBI-001.pbi_quality.json'), {'answers': {}})
    write_text(os.path.join(d, 'PBI-001.md'), '# PBI-001: ダミー\n\n中身は適当でよい。\n')
    write_text(os.path.join(d, 'PBI-001.brief.md'), '目的: ダミー\n')


def run_prepare(base, home, *extra):
    cmd = [sys.executable, SCRIPT, base, '--home', home, *extra]
    return subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')


def rows(out):
    return [line for line in out.stdout.splitlines() if line.startswith('| PBI-')]


class PrepareTemplateTest(unittest.TestCase):
    def run_case(self, templates_, app, *extra):
        with tempfile.TemporaryDirectory() as d:
            home, base = os.path.join(d, 'home'), os.path.join(d, 'pbi')
            write_home(home, templates_, app)
            write_base(base)
            out = run_prepare(base, home, *extra)
            log = []
            log_path = os.path.join(base, 'jev-log.jsonl')
            if os.path.exists(log_path):
                with open(log_path, encoding='utf-8') as f:
                    log = [json.loads(line) for line in f if line.strip()]
            return out, log

    def test_active_template_shows_name_and_deadline(self):
        out, log = self.run_case({'deepseek-all': ALL_TIERS}, application(), '--now', NOW)
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = out.stdout.splitlines()
        self.assertEqual(lines[0], f'優先テンプレート: deepseek-all を適用中 ({UNTIL} まで、期限切れは ask) / 写し: {SYNCED} 時点')
        self.assertIn('| 優先テンプレート |', lines[1])
        self.assertIn(f'deepseek-all を当てた ({UNTIL} まで)', rows(out)[0])
        brief = [line for line in log if line['gate'] == 'brief'][0]
        self.assertEqual(brief['template']['name'], 'deepseek-all')
        self.assertTrue(brief['template']['applied'])

    def test_expired_ask_asks_confirmation(self):
        out, _ = self.run_case({'deepseek-all': ALL_TIERS}, application(), '--now', PAST)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.splitlines()[0],
                         f'優先テンプレート: deepseek-all は期限切れ。承認で「延長 / やめる / 期限なし」を確認する / 写し: {SYNCED} 時点')
        self.assertIn('期限切れ: 延長・やめる・期限なしの確認が要る', rows(out)[0])

    def test_expired_drop_says_not_applied(self):
        out, _ = self.run_case({'deepseek-all': ALL_TIERS}, application(on_expire='drop'), '--now', PAST)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.splitlines()[0],
                         f'優先テンプレート: deepseek-all は期限切れ (drop)。当てない / 写し: {SYNCED} 時点')
        self.assertIn('期限切れ (drop) のため当てない', rows(out)[0])

    def test_without_overrides_says_no_application(self):
        out, log = self.run_case({'deepseek-all': ALL_TIERS}, None, '--now', NOW)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.splitlines()[0], '優先テンプレート: 適用なし / 写し: なし')
        self.assertIn('適用なし', rows(out)[0])
        brief = [line for line in log if line['gate'] == 'brief'][0]
        self.assertFalse(brief['template']['applied'])

    def test_until_null_shows_no_deadline(self):
        out, _ = self.run_case({'deepseek-all': ALL_TIERS}, application(until=None), '--now', PAST)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.splitlines()[0], f'優先テンプレート: deepseek-all を適用中 (期限なし) / 写し: {SYNCED} 時点')
        self.assertIn('期限なしで当てた', rows(out)[0])

    def test_template_note_falls_back_to_dash(self):
        self.assertEqual(prepare.template_note({}), '—')
        self.assertEqual(prepare.template_note({'template': None}), '—')
        self.assertEqual(prepare.template_note({'template': {'name': 'x', 'applied': True, 'note': '当てた'}}), '当てた')


if __name__ == '__main__':
    unittest.main()
