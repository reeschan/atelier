import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from types import SimpleNamespace
from atelier import pages

EXPECTED = Path(__file__).parent / 'expected'
RUN = {
 'conclusion': '注文を確認できるようにします。', 'before': '注文を受ける', 'after': '注文を確認する',
 'template': {'name': '標準', 'roles': {'planner': {'runner': 'agent', 'model': 'opus', 'effort': None}}, 'routes': {'L2': [{'runner': 'codex', 'model': 'モデルA', 'effort': 'high'}]}, 'limits': {'auto_fix': 1, 'e2e_rounds': 2, 'budget_tokens': None, 'budget_usd': 5}},
 'pbis': [{'id': 'PBI-1', 'title': '確認画面', 'level': 'L2', 'level_reason': '画面を追加', 'sensitive_reason': '個人情報を扱う', 'data_needs': [{'source': 'D:/data/order.bin', 'dest': 'tests/fixtures/order.bin', 'why': '実データで確かめる'}]}],
 'acceptance_first': False, 'acceptance_reason': 'テンプレートで切った', 'plan_checks': ['npm run type-check'], 'plan_e2e': [], 'final_review': {'skill': 'repo-review', 'args': 'full'},
 'candidates': [{'id': '指摘-2', 'kind': '確信度が 80 未満', 'title': '名前を直す', 'reason': 'minor・確信度 55・読みにくい'}],
 'estimate': '約 2 時間', 'decisions': [{'question': '通知しますか', 'options': [{'label': '通知する', 'note': '結果を知らせる', 'recommended': True}, {'label': '通知しない', 'note': '画面で確認する'}]}],
 'spec': '注文の内容を確認します。',
 'fix_list': [{'id': '修正-1', 'kind': '見直しの指摘', 'title': '表示を直す', 'what': 'app.py:3 の正しさの指摘（major）', 'why': '金額が読みにくい', 'tried': '最後の見直しで見つかった。まだ直していない', 'state': '未修正', 'interpretation': 'なし', 'options': ['直す: 表示を直す', '見送る（backlog に残す）'], 'recommendation': '直す', 'pbis': ['PBI-1'], 'files': ['app.py'], 'reason': '仕様の金額の書き方に反する'}]
}

class PageTests(unittest.TestCase):
 def test_expected_drafts(self):
  for kind in ('plan', 'spec', 'fix_list'):
   with self.subTest(kind=kind):
    result = pages.draft(kind, RUN)
    self.assertEqual(result['text'], (EXPECTED / (kind + '.md')).read_text(encoding='utf-8'))
    self.assertTrue(result['summary'])
    self.assertIn('決めること', '\n'.join(result['summary']))
  # The plan page has no E2E limit even when the template still names one.
  self.assertNotIn('E2E の回数', pages.draft('plan', RUN)['text'])
 def test_render_refuses_corruption_before_launch(self):
  with TemporaryDirectory() as d:
   path = Path(d) / 'draft.md'
   for text in ('壊れ???た', '壊れ\ufffdた'):
    path.write_text(text, encoding='utf-8')
    with patch('atelier.pages.subprocess.run') as run:
     with self.assertRaises(pages.PageError): pages.render(path)
     run.assert_not_called()
 def test_render_uses_node_and_parses_path(self):
  with TemporaryDirectory() as d:
   path = Path(d) / 'draft.md'; path.write_text('正常', encoding='utf-8')
   script = Path(d) / 'custom.mjs'; script.write_text('', encoding='utf-8')
   with patch('atelier.pages.shutil.which', return_value='node-test'), patch('atelier.pages.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='✓ C:/page.html\n', stderr='')) as run:
    self.assertEqual(pages.render(path, script=script, node='node-test'), Path('C:/page.html'))
    self.assertEqual(run.call_args.args[0], ['node-test', str(script), 'render', str(path), '--no-open'])
 def test_render_failure_is_not_success(self):
  with TemporaryDirectory() as d:
   path = Path(d) / 'draft.md'; path.write_text('正常', encoding='utf-8')
   script = Path(d) / 'am.mjs'; script.write_text('', encoding='utf-8')
   with patch('atelier.pages.shutil.which', return_value='node'), patch('atelier.pages.subprocess.run', return_value=SimpleNamespace(returncode=1, stdout='', stderr='失敗')):
    with self.assertRaises(pages.PageError): pages.render(path, script=script)
 def test_render_without_answer_me_with_html_returns_no_page(self):
  with TemporaryDirectory() as d:
   path = Path(d) / 'draft.md'; path.write_text('正常', encoding='utf-8')
   with patch('atelier.pages.subprocess.run') as run:
    self.assertIsNone(pages.render(path, script=Path(d) / 'missing.mjs'))
    run.assert_not_called()
 def test_missing_information_is_visible(self):
  result = pages.draft('plan', {'pbis': [{'id': 'PBI-1'}]})
  self.assertIn('未確認', result['text'])
  self.assertNotIn('なし | なし | なし', result['text'])
 def test_acceptance_toggle_table(self):
  for enabled, reason, cells in ((True, None, '| 入り | なし |'), (False, 'git のリポジトリでない', '| 切り | git のリポジトリでない |'), (None, None, '| 未確認 | 未確認 |')):
   with self.subTest(enabled=enabled):
    text = pages.draft('plan', {**RUN, 'acceptance_first': enabled, 'acceptance_reason': reason})['text']
    self.assertIn('## 部品の入り切り', text)
    self.assertIn(cells, text)

 def test_scenarios_mode_and_kind_columns(self):
  scenarios = [
   {'id': 'R1', 'text': '二つの数を足せる', 'mode': 'normal', 'gherkin': 'Scenario Outline: add\n  Given <a>', 'cases': [{'id': 'R1-1', 'values': {'a': 1, 'sum': 3}, 'pbi': 'PBI-2'}]},
   {'id': 'R2', 'text': '依存を更新する', 'mode': 'light', 'cases': [{'id': 'R2-1', 'values': {}, 'pbi': 'PBI-1'}]}]
  pbis = [{'id': 'PBI-1', 'title': '土台', 'level': 'L2', 'kind': 'stub'}, {'id': 'PBI-2', 'title': '本体', 'level': 'L2'}, {'id': 'PBI-3', 'title': '通し', 'level': 'L3', 'kind': 'e2e'}]
  text = pages.draft('plan', {**RUN, 'pbis': pbis, 'acceptance_first': True, 'acceptance_reason': None, 'scenarios': scenarios})['text']
  self.assertIn('## シナリオ', text)
  self.assertIn('```gherkin\nScenario Outline: add', text)
  self.assertIn('| ケース | a | sum |\n| --- | --- | --- |\n| R1-1 | 1 | 3 |', text)
  self.assertIn('| ケース | テスト名 |\n| --- | --- |\n| R2-1 | 依存を更新する |', text)
  self.assertIn('| モード | 種類 |', text)
  self.assertIn('| light | stub |', text)
  self.assertIn('| normal | build |', text)
  self.assertIn('| — | e2e |', text)
  self.assertIn('（受け入れ 0、stub 1、build 1、E2E 1）', text)
 def test_no_scenario_section_when_acceptance_first_is_off(self):
  text = pages.draft('plan', {**RUN, 'acceptance_first': False, 'scenarios': [{'id': 'R1', 'text': 'x', 'mode': 'normal', 'cases': []}]})['text']
  self.assertNotIn('## シナリオ', text)
