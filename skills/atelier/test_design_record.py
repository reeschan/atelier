"""design_record.py を CLI から動かし、正本の形 (schema 1)・封筒の文字種・フェンスの切り出し・未選択の ask・照合の結果を確かめる。

testdata/design/ の返信 (reply-*.md) は answer-me-with-any (8856dde) の replyText と withReplyMeta で作ったもの。
同じフォルダの pbi/design-record.json と draft.md に対する封筒が付いている。HTML の生成や skill の手順は起動しない。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'design_record.py')
FIXTURES = os.path.join(HERE, 'testdata', 'design')
DESIGN_ID = 'workspace:cursor/handoff-design-a5f0:W-001:atelier-handoff'
RECORD = {'schema': 'atelier.design-record/1', 'design_id': DESIGN_ID, 'revision': 1,
          'targets': ['PBI-012', 'PBI-013', 'PBI-014'], 'concerns': ['notes/c1.slice.json']}
META_NAME = re.compile(r'^[a-z][a-z0-9_]{0,31}$')
META_VALUE = re.compile(r'^[A-Za-z0-9._:+/=-]{1,128}$')


class DesignRecordTest(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.join(tempfile.gettempdir(), f'design-record-{uuid.uuid4().hex}')
        shutil.copytree(FIXTURES, self.dir)
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.pbi = os.path.join(self.dir, 'pbi')

    def run_cli(self, *args, stdin=None):
        """stdin はバイト列で渡す (Windows の改行の変換と文字コードを通さない)。出力は UTF-8 で読む。"""
        result = subprocess.run([sys.executable, SCRIPT, *args, '--dir', self.pbi],
                                input=None if stdin is None else stdin.encode('utf-8'), capture_output=True)
        return subprocess.CompletedProcess(result.args, result.returncode, result.stdout.decode('utf-8'),
                                           result.stderr.decode('utf-8', errors='replace'))

    def path(self, *parts):
        return os.path.join(self.dir, *parts)

    def read_record(self):
        with open(os.path.join(self.pbi, 'design-record.json'), encoding='utf-8') as f:
            return json.load(f)

    def approvals(self):
        path = os.path.join(self.pbi, 'design-approvals.jsonl')
        if not os.path.exists(path):
            return []
        with open(path, encoding='utf-8') as f:
            return [json.loads(line) for line in f if line.strip()]

    def check(self, reply_name, *extra):
        return self.run_cli('check-reply', '--reply', self.path(reply_name), *extra)

    # 正本

    def test_fixture_record_is_schema_1(self):
        """schema 1 の形 (キーと値の型) が変わっていない。"""
        self.assertEqual(self.read_record(), RECORD)

    def test_write_keeps_schema_and_bumps_revision_only_on_change(self):
        same = self.run_cli('write', '--id', DESIGN_ID, '--target', 'PBI-012', '--target', 'PBI-013',
                            '--target', 'PBI-014', '--concern', 'notes/c1.slice.json')
        self.assertEqual(same.returncode, 0, same.stderr)
        self.assertEqual(self.read_record(), RECORD)
        changed = self.run_cli('write', '--id', DESIGN_ID, '--target', 'PBI-012')
        self.assertEqual(changed.returncode, 0, changed.stderr)
        self.assertEqual(self.read_record(), {**RECORD, 'revision': 2, 'targets': ['PBI-012'], 'concerns': []})
        other = self.run_cli('write', '--id', 'D-2', '--target', 'PBI-012')
        self.assertEqual(other.returncode, 0, other.stderr)
        self.assertEqual(self.read_record()['revision'], 1)
        self.assertEqual(sorted(self.read_record()), sorted(RECORD))

    def test_write_refuses_missing_concern_and_bad_id(self):
        missing = self.run_cli('write', '--id', 'D-1', '--target', 'PBI-012', '--concern', 'notes/none.slice.json')
        self.assertEqual(missing.returncode, 2)
        outside = self.run_cli('write', '--id', 'D-1', '--target', 'PBI-012', '--concern', '../draft.md')
        self.assertEqual(outside.returncode, 2)
        for bad in ('D 1', 'D-1あ', 'x' * 129):
            with self.subTest(design_id=bad):
                self.assertEqual(self.run_cli('write', '--id', bad, '--target', 'PBI-012').returncode, 2)
        self.assertEqual(self.read_record(), RECORD)

    def test_slice_holds_question_reads_and_unknown_only(self):
        result = self.run_cli('slice', 'c2', '--question', 'どこに置くか', '--read', 'a/b.py:3-9',
                              '--read', 'c.md:4', '--unknown', '利用者の選択')
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(os.path.join(self.pbi, 'notes', 'c2.slice.json'), encoding='utf-8') as f:
            data = json.load(f)
        self.assertEqual(data, {'schema': 'atelier.design-slice/1', 'id': 'c2', 'question': 'どこに置くか',
                                'reads': [{'path': 'a/b.py', 'lines': [3, 9]}, {'path': 'c.md', 'lines': [4, 4]}],
                                'unknown': ['利用者の選択']})
        self.assertEqual(self.run_cli('slice', 'c3', '--question', 'q', '--read', 'a.py:9-3').returncode, 2)

    # Markdown の封筒

    def test_markdown_envelope_matches_the_renderer_rules(self):
        result = self.run_cli('markdown', '--draft', self.path('approve-good.md'))
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.split('\n')
        meta = [line for line in lines if line.startswith('reply-meta-')]
        self.assertEqual([line.split(':', 1)[0] for line in meta],
                         ['reply-meta-design_id', 'reply-meta-revision', 'reply-meta-record_hash',
                          'reply-meta-draft_hash'])
        for line in meta:
            name, value = line[len('reply-meta-'):].split(': ', 1)
            self.assertRegex(name, META_NAME)
            self.assertRegex(value, META_VALUE)
        self.assertEqual(lines[0], '---')
        self.assertLess(lines.index(meta[-1]), lines.index('---', 1))
        self.assertIn('title: 設計の受け渡しの承認', lines)

    def test_markdown_is_the_same_for_its_own_output_and_crlf(self):
        """封筒を付けた Markdown や \\r\\n の下書きを渡しても draft_hash は変わらない。"""
        first = self.run_cli('markdown', '--draft', self.path('approve-good.md')).stdout
        with open(self.path('again.md'), 'w', encoding='utf-8', newline='') as f:
            f.write(first.replace('\n', '\r\n'))
        second = self.run_cli('markdown', '--draft', self.path('again.md'))
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.stdout, first)

    def test_markdown_without_frontmatter_gets_one(self):
        with open(self.path('approve-good.md'), encoding='utf-8') as f:
            body = f.read().split('---\n', 2)[2]
        with open(self.path('plain.md'), 'w', encoding='utf-8') as f:
            f.write(body)
        out = self.run_cli('markdown', '--draft', self.path('plain.md')).stdout.split('\n')
        self.assertEqual(out[0], '---')
        self.assertTrue(out[1].startswith('reply-meta-design_id: '))
        self.assertEqual(out[5:7], ['---', '## 結論'])

    def test_markdown_draft_hash_matches_its_own_output(self):
        """frontmatter も末尾の改行も無い下書きでも、出力の draft_hash はその出力から取った値と同じ。"""
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import design_record
        with open(self.path('approve-good.md'), encoding='utf-8') as f:
            body = f.read().split('---\n', 2)[2]
        for name, text in (('plain.md', body), ('plain-no-newline.md', body.rstrip('\n'))):
            with self.subTest(draft=name):
                with open(self.path(name), 'w', encoding='utf-8', newline='') as f:
                    f.write(text)
                out = self.run_cli('markdown', '--draft', self.path(name)).stdout
                given = next(line.split(': ', 1)[1] for line in out.split('\n')
                             if line.startswith('reply-meta-draft_hash: '))
                self.assertEqual(given, design_record.draft_hash(out))

    def test_markdown_stops_on_a_draft_lint_rejects(self):
        """承認の原稿の形でない下書き (段ごとの記録がある・節が足りない) には封筒を付けず、6 で止める。"""
        result = self.run_cli('markdown', '--draft', self.path('draft.md'))
        self.assertEqual(result.returncode, 6, result.stdout + result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertIn('✗ L', result.stderr)

    # 返信の照合

    def test_matching_reply_is_saved(self):
        result = self.check('reply-answered.md', '--draft', self.path('draft.md'))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('ask\t1\tanswered\t**リポジトリ外のキャッシュ**\tOMM の置き場所はどちらですか', result.stdout)
        self.assertTrue(result.stdout.splitlines()[-1].startswith('match\t'))
        rows = self.approvals()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['design_id'], rows[0]['revision'], rows[0]['draft_checked']), (DESIGN_ID, 1, True))
        self.assertEqual(rows[0]['answers'], [{'question': 'OMM の置き場所はどちらですか',
                                               'answer': '**リポジトリ外のキャッシュ**'}])

    def test_crlf_reply_and_stdin(self):
        with open(self.path('reply-answered.md'), encoding='utf-8') as f:
            text = f.read().replace('\n', '\r\n')
        with open(self.path('reply-crlf.md'), 'w', encoding='utf-8', newline='') as f:
            f.write(text)
        self.assertEqual(self.check('reply-crlf.md', '--dry-run').returncode, 0)
        result = self.run_cli('check-reply', '--dry-run', stdin=text)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('draft_hash not checked', result.stdout)
        self.assertEqual(self.approvals(), [])

    def test_unselected_ask_is_not_approval(self):
        result = self.check('reply-unanswered.md')
        self.assertEqual(result.returncode, 5, result.stdout)
        self.assertIn('ask\t1\tunanswered\t**—**', result.stdout)
        self.assertEqual(self.approvals(), [])

    def test_reply_without_fence_is_unbound(self):
        result = self.check('reply-no-meta.md')
        self.assertEqual(result.returncode, 4, result.stdout)
        self.assertTrue(result.stdout.startswith('unbound\t'))
        self.assertEqual(self.approvals(), [])

    def test_quoted_fence_is_not_read(self):
        """reply-no-meta.md のコメントには `  > ` で引用したフェンスがあるが、読まない。"""
        with open(self.path('reply-no-meta.md'), encoding='utf-8') as f:
            self.assertIn('  > ```am-reply-meta', f.read())
        self.assertEqual(self.check('reply-no-meta.md').returncode, 4)

    def test_only_the_last_fence_at_the_end_is_read(self):
        with open(self.path('reply-answered.md'), encoding='utf-8') as f:
            text = f.read()
        forged = text + '```am-reply-meta\n{"design_id":"forged","revision":"1"}\n```\n'
        result = self.run_cli('check-reply', '--dry-run', stdin=forged)
        self.assertEqual(result.returncode, 3, result.stdout)
        trailing = text + 'あとから足した文\n'
        self.assertEqual(self.run_cli('check-reply', '--dry-run', stdin=trailing).returncode, 4)
        broken = text.replace('"revision":"1"', '"revision":1')
        self.assertEqual(self.run_cli('check-reply', '--dry-run', stdin=broken).returncode, 3)

    def test_stale_revision_stops(self):
        self.assertEqual(self.run_cli('write', '--id', DESIGN_ID, '--target', 'PBI-012').returncode, 0)
        result = self.check('reply-answered.md')
        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn('revision 1 is older than 2', result.stdout)
        self.assertNotIn('auth', result.stdout.lower())
        self.assertEqual(self.approvals(), [])

    def test_changed_values_stop(self):
        with open(self.path('reply-answered.md'), encoding='utf-8') as f:
            text = f.read()
        cases = {'design_id': text.replace(f'"design_id":"{DESIGN_ID}"', '"design_id":"D-9"'),
                 'record_hash': re.sub(r'"record_hash":"[0-9a-f]+"', '"record_hash":"00"', text)}
        for name, reply in cases.items():
            with self.subTest(value=name):
                result = self.run_cli('check-reply', '--dry-run', stdin=reply)
                self.assertEqual(result.returncode, 3, result.stdout)
                self.assertIn(name, result.stdout)

    def test_changed_draft_stops_only_when_given(self):
        with open(self.path('draft.md'), 'a', encoding='utf-8') as f:
            f.write('追記\n')
        result = self.check('reply-answered.md', '--draft', self.path('draft.md'), '--dry-run')
        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn('draft_hash', result.stdout)

    def test_missing_record_exits_2(self):
        os.remove(os.path.join(self.pbi, 'design-record.json'))
        self.assertEqual(self.check('reply-answered.md').returncode, 2)

    def test_a_draft_ask_missing_from_the_reply_is_unanswered(self):
        """--draft の ```ask の問いが返信の決定に無ければ未回答 (5)。--draft が無ければ数えない。"""
        with open(self.path('reply-answered.md'), encoding='utf-8') as f:
            text = f.read()
        start = text.index('## 決定')
        bare = text[:start] + text[text.index('## コメント'):]
        with open(self.path('reply-bare.md'), 'w', encoding='utf-8', newline='\n') as f:
            f.write(bare)
        result = self.check('reply-bare.md', '--draft', self.path('draft.md'))
        self.assertEqual(result.returncode, 5, result.stdout)
        self.assertIn('ask\t-\tmissing\t-\tOMM の置き場所はどちらですか', result.stdout)
        self.assertEqual(self.approvals(), [])
        self.assertEqual(self.check('reply-bare.md', '--dry-run').returncode, 0)



EXAMPLE = os.path.join(HERE, 'examples', 'approve.md')


class ApprovalDraftLintTest(unittest.TestCase):
    """design_record.py lint: 承認の原稿の 7 つの節・図・表・選択肢の補足を点検し、内部の言葉に警告を出す。"""

    def setUp(self):
        self.dir = os.path.join(tempfile.gettempdir(), f'approval-lint-{uuid.uuid4().hex}')
        os.makedirs(self.dir)
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        with open(os.path.join(FIXTURES, 'approve-good.md'), encoding='utf-8') as f:
            self.good = f.read()

    def lint(self, text):
        path = os.path.join(self.dir, 'approve.md')
        with open(path, 'w', encoding='utf-8', newline='') as f:
            f.write(text)
        result = subprocess.run([sys.executable, SCRIPT, 'lint', '--draft', path], capture_output=True)
        return subprocess.CompletedProcess(result.args, result.returncode, result.stdout.decode('utf-8'),
                                           result.stderr.decode('utf-8', errors='replace'))

    def line_of(self, text, needle):
        return text.split('\n').index(needle) + 1

    def assert_error(self, text, line, word):
        result = self.lint(text)
        self.assertEqual(result.returncode, 6, result.stdout + result.stderr)
        errors = [row for row in result.stderr.splitlines() if row.startswith('✗ L')]
        self.assertTrue(any(row.startswith(f'✗ L{line} ') and word in row for row in errors),
                        f'no "✗ L{line} … {word}" in:\n{result.stderr}')
        return result

    def replace_section(self, heading, next_heading, body):
        start, end = self.good.index(heading), self.good.index(next_heading)
        return self.good[:start] + body + self.good[end:]

    def test_good_draft_passes(self):
        result = self.lint(self.good)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('✗', result.stderr)
        self.assertNotIn('! L', result.stderr)
        self.assertTrue(result.stdout.startswith('ok\t'), result.stdout)

    def test_crlf_draft_passes(self):
        self.assertEqual(self.lint(self.good.replace('\n', '\r\n')).returncode, 0)

    def test_extra_section_is_an_error(self):
        text = self.good.replace('## 作る人と見直し', '## 段ごとの記録\n- 計画: 済み\n\n## 作る人と見直し')
        result = self.assert_error(text, self.line_of(text, '## 段ごとの記録'), '段ごとの記録')
        self.assertIn('例:', result.stderr)

    def test_missing_section_is_an_error(self):
        text = self.replace_section('## 影響範囲', '## 作る人と見直し', '')
        self.assert_error(text, self.line_of(text, '## 作る人と見直し'), '影響範囲')

    def test_decisions_at_the_end_is_an_error(self):
        start, end = self.good.index('## 決めること'), self.good.index('## PBI')
        text = self.good[:start] + self.good[end:] + '\n' + self.good[start:end]
        self.assert_error(text, self.line_of(text, '## 決めること'), '順')

    def test_before_without_a_diagram_is_an_error(self):
        text = self.replace_section('## 今の流れ', '## この run のあとの流れ',
                                    '## 今の流れ\nチャットに一覧を出している。\n\n')
        self.assert_error(text, self.line_of(text, '## 今の流れ'), '図')

    def test_after_without_change_marks_is_an_error(self):
        text = self.replace_section('## この run のあとの流れ', '## 決めること',
                                    '## この run のあとの流れ\n```flow\n(設計者が問いを書く) -> ページにする\n```\n\n')
        self.assert_error(text, self.line_of(text, '## この run のあとの流れ'), '+')

    def test_machine_pbi_table_is_an_error(self):
        text = self.good.replace('| PBI | 何をするか | 終わると何ができるか | 先に要る PBI |',
                                 '| PBI | 題 | 段 | route の先頭 |')
        self.assert_error(text, self.line_of(text, '## PBI'), '何をするか')

    def test_option_without_a_note_is_an_error(self):
        text = self.good.replace('- リポジトリの中 | 誰でも同じ図を見られる。差分が毎回出る', '- リポジトリの中')
        self.assert_error(text, self.line_of(text, '- リポジトリの中'), '|')

    def test_blank_note_after_the_separator_is_an_error(self):
        text = self.good.replace('- リポジトリの中 | 誰でも同じ図を見られる。差分が毎回出る', '- リポジトリの中 |  ')
        self.assert_error(text, self.line_of(text, '- リポジトリの中 |  '), '|')

    def test_ask_fence_the_reply_check_cannot_read_is_an_error(self):
        """返事の照合 (check-reply --draft) が読むのは ```ask だけ。~~~ask などは答えを求められずに通るので止める。"""
        for opener, closer in (('~~~ask', '~~~'), (':::ask', ':::'), ('````ask', '````'), ('```ask multi', '```')):
            with self.subTest(opener=opener):
                ask = '```ask\n図の置き場所はどちらにしますか'
                text = self.good.replace(ask, ask.replace('```ask', opener))
                text = text.replace('差分が毎回出る\n```', '差分が毎回出る\n' + closer)
                self.assert_error(text, self.line_of(text, opener), opener)

    def test_empty_ask_block_is_an_error(self):
        empty = self.replace_section('## 決めること', '## PBI', '## 決めること\n```ask\n```\n\n')
        self.assert_error(empty, self.line_of(empty, '```ask'), '選択肢')
        question_only = self.replace_section('## 決めること', '## PBI', '## 決めること\n```ask\n置き場所は?\n```\n\n')
        self.assert_error(question_only, self.line_of(question_only, '```ask'), '選択肢')

    def test_two_ask_blocks_are_checked_apart(self):
        second = '\n```ask\n点検しますか\n* する | 時間がかかる\n- しない\n```\n'
        text = self.good.replace('## PBI\n', second + '\n## PBI\n')
        result = self.assert_error(text, self.line_of(text, '- しない'), '|')
        self.assertEqual(len([r for r in result.stderr.splitlines() if r.startswith('✗ L')]), 1, result.stderr)

    def test_empty_diagram_is_an_error(self):
        text = self.replace_section('## 今の流れ', '## この run のあとの流れ', '## 今の流れ\n```flow\n```\n\n')
        self.assert_error(text, self.line_of(text, '## 今の流れ'), '図')

    def test_empty_decisions_is_an_error_and_none_passes(self):
        empty = self.replace_section('## 決めること', '## PBI', '## 決めること\n\n')
        self.assert_error(empty, self.line_of(empty, '## 決めること'), 'なし')
        none = self.replace_section('## 決めること', '## PBI', '## 決めること\nなし\n\n')
        result = self.lint(none)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ask_outside_decisions_is_an_error(self):
        ask = '```ask\n点検しますか\n* する | 時間がかかる\n- しない | 速い\n```\n'
        text = self.good.replace('## 影響範囲\n', '## 影響範囲\n' + ask)
        self.assert_error(text, self.line_of(text, '## 影響範囲') + 1, '決めること')

    def test_every_error_is_listed_at_once(self):
        text = self.good.replace('| PBI | 何をするか | 終わると何ができるか | 先に要る PBI |', '| PBI | 題 |')
        text = text.replace('- リポジトリの中 | 誰でも同じ図を見られる。差分が毎回出る', '- リポジトリの中')
        result = self.lint(text)
        self.assertEqual(result.returncode, 6)
        self.assertEqual(len([row for row in result.stderr.splitlines() if row.startswith('✗ L')]), 2, result.stderr)

    def test_internal_term_is_a_warning_with_a_rewording(self):
        row = '| PBI-012 | route と tier を変える | ページで設計を読める | なし |'
        text = self.good.replace('| PBI-012 | 承認の一覧をページにする | ページで設計を読める | なし |', row)
        result = self.lint(text)
        self.assertEqual(result.returncode, 0, result.stderr)
        warnings = [w for w in result.stderr.splitlines() if w.startswith(f'! L{self.line_of(text, row)} ')]
        self.assertEqual(len(warnings), 2, result.stderr)
        self.assertTrue(all('→' in w for w in warnings), warnings)

    def test_explained_term_is_not_warned(self):
        text = self.good.replace('| PBI-012 | 承認の一覧をページにする |',
                                 '| PBI-012 | route (どのモデルにどの順で頼むか) を変える |')
        self.assertNotIn('! L', self.lint(text).stderr)

    def test_common_words_with_the_same_kanji_are_not_warned(self):
        text = self.good.replace('設計の受け渡しを、', '段階を分け、階段のように手段をそろえ、波線を引き、設計の受け渡しを、')
        self.assertNotIn('! L', self.lint(text).stderr)

    def test_english_headings_pass(self):
        names = {'## 結論': '## Summary', '## 今の流れ': '## Before', '## この run のあとの流れ': '## After',
                 '## 決めること': '## Decisions', '## PBI\n': '## PBIs\n', '## 影響範囲': '## Impact',
                 '## 作る人と見直し': '## Who builds',
                 '| PBI | 何をするか | 終わると何ができるか | 先に要る PBI |': '| PBI | What it does | What works after | Needs |'}
        text = self.good
        for ja, en in names.items():
            text = text.replace(ja, en)
        result = self.lint(text)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_panel_id_and_attributes_are_ignored(self):
        text = self.good.replace('## 結論', '## A 結論 {span=2}')
        result = self.lint(text)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_example_draft_passes_without_warnings(self):
        result = subprocess.run([sys.executable, SCRIPT, 'lint', '--draft', EXAMPLE], capture_output=True)
        stderr = result.stderr.decode('utf-8', errors='replace')
        self.assertEqual(result.returncode, 0, stderr)
        self.assertNotIn('! L', stderr)

if __name__ == '__main__':
    unittest.main()
