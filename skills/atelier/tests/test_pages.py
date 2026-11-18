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
 'plan_checks': ['npm run type-check'], 'plan_e2e': [], 'final_review': {'skill': 'repo-review', 'args': 'full'},
 'candidates': [{'id': '指摘-2', 'kind': '確信度が 80 未満', 'title': '名前を直す', 'reason': 'minor・確信度 55・読みにくい'}],
 'estimate': '約 2 時間', 'decisions': [{'question': '通知しますか', 'options': [{'label': '通知する', 'note': '結果を知らせる', 'recommended': True}, {'label': '通知しない', 'note': '画面で確認する'}]}],
 'spec': '注文の内容を確認します。', 'fix_list': [{'id': '修正-1', 'title': '表示を直す', 'reason': '金額が読みにくい'}]
}

class PageTests(unittest.TestCase):
 def test_expected_drafts(self):
  for kind in ('plan', 'spec', 'fix_list'):
   with self.subTest(kind=kind):
    result = pages.draft(kind, RUN)
    self.assertEqual(result['text'], (EXPECTED / (kind + '.md')).read_text(encoding='utf-8'))
    self.assertTrue(result['summary'])
    self.assertIn('決めること', '\n'.join(result['summary']))
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

