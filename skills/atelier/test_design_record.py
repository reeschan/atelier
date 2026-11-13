"""design_record.py を CLI から動かし、正本の形 (schema 1)・封筒の文字種・フェンスの切り出し・未選択の ask・照合の結果を確かめる。

testdata/design/ の返信 (reply-*.md) は answer-me-with-any (8856dde) の replyText と withReplyMeta で作ったもの。
同じフォルダの pbi/design-record.json と draft.md に対する封筒が付いている。HTML の生成や skill の手順は起動しない。
"""
import contextlib
import hashlib
import io
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
        # 本物の ~/.claude の設定を読ませない (events.emit の送り手)
        self.no_config = os.path.join(self.dir, 'no-harness-log.json')
        # 本物の home・プラグインの yomiyasu を拾わせない
        self.isolated = {'ATELIER_HOME': os.path.join(self.dir, 'home'),
                         'PBI_PLUGINS_DIR': os.path.join(self.dir, 'plugins')}

    def run_cli(self, *args, stdin=None, env=None):
        """stdin はバイト列で渡す (Windows の改行の変換と文字コードを通さない)。出力は UTF-8 で読む。"""
        result = subprocess.run([sys.executable, SCRIPT, *args, '--dir', self.pbi],
                                input=None if stdin is None else stdin.encode('utf-8'), capture_output=True,
                                env=dict(os.environ, HARNESS_LOG_CONFIG=self.no_config, **self.isolated, **(env or {})))
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

    def events(self):
        path = os.path.join(self.pbi, 'events.jsonl')
        if not os.path.exists(path):
            return []
        with open(path, encoding='utf-8') as f:
            return [json.loads(line) for line in f if line.strip()]

    def check(self, reply_name, *extra):
        return self.run_cli('check-reply', '--reply', self.path(reply_name), *extra)

    # 一時 git リポジトリ (work・stale・slice は git のリポジトリのルートで動かす)

    @property
    def repo(self):
        """1 コミットだけある一時 git リポジトリ (src/a.txt と src/b.txt)。使うテストだけが作る。"""
        if not hasattr(self, '_repo'):
            self._repo = tempfile.mkdtemp(prefix='design-record-repo-')
            self.addCleanup(shutil.rmtree, self._repo, **self.rmtree_options())
            self.git('init', '-q')
            os.makedirs(self.repo_path('src'))
            for name in ('a.txt', 'b.txt'):
                with open(self.repo_path('src', name), 'w', encoding='utf-8', newline='\n') as f:
                    f.write(f'{name}\n')
            self.git('add', '.')
            self.git('-c', 'user.name=t', '-c', 'user.email=t@t', '-c', 'commit.gpgsign=false',
                     'commit', '-q', '-m', 'first')
        return self._repo

    @staticmethod
    def rmtree_options():
        """git が読み取り専用で作るファイルも消す (Windows)。"""
        def make_writable(func, path, _):
            os.chmod(path, 0o700)
            func(path)
        return {'onexc' if sys.version_info >= (3, 12) else 'onerror': make_writable}

    def git(self, *args):
        done = subprocess.run(['git', *args], cwd=self.repo, capture_output=True, stdin=subprocess.DEVNULL)
        stdout, stderr = done.stdout.decode('utf-8', errors='replace'), done.stderr.decode('utf-8', errors='replace')
        self.assertEqual(done.returncode, 0, f'git {args}: {stdout}{stderr}')
        return stdout

    def repo_path(self, *parts):
        return os.path.join(self.repo, *parts)

    def run_cli_in(self, cwd, *args, stdin=None, env=None):
        """cwd で動かす。--dir は渡さず、既定の docs/pbi (cwd からの相対) を使う。"""
        result = subprocess.run([sys.executable, SCRIPT, *args], cwd=cwd,
                                input=None if stdin is None else stdin.encode('utf-8'), capture_output=True,
                                env=dict(os.environ, HARNESS_LOG_CONFIG=self.no_config, **self.isolated, **(env or {})))
        return subprocess.CompletedProcess(result.args, result.returncode, result.stdout.decode('utf-8'),
                                           result.stderr.decode('utf-8', errors='replace'))

    @staticmethod
    def read_json(path):
        with open(path, encoding='utf-8') as f:
            return json.load(f)

    def read_repo_record(self):
        return self.read_json(self.repo_path('docs', 'pbi', 'design-record.json'))

    def repo_approvals(self):
        path = self.repo_path('docs', 'pbi', 'design-approvals.jsonl')
        if not os.path.exists(path):
            return []
        with open(path, encoding='utf-8') as f:
            return [json.loads(line) for line in f if line.strip()]

    def work_path(self, pbi):
        return self.repo_path('docs', 'pbi', f'{pbi}.work.json')

    @staticmethod
    def file_hash(path):
        """作業票の中身 (改行を \\n にそろえたもの) の sha256。"""
        with open(path, 'rb') as f:
            text = f.read().decode('utf-8').replace('\r\n', '\n')
        return hashlib.sha256(text.encode('utf-8')).hexdigest()

    @staticmethod
    def record_hash_of(record):
        """正本の hash は、キーを並べ替え、区切りを , と : にし、ASCII にしない JSON の sha256 (design_record.py の docstring)。"""
        text = json.dumps(record, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        return hashlib.sha256(text.encode('utf-8')).hexdigest()

    def write_pbi(self, pbi, goal='目的', targets=None, notes=None):
        """PBI の Markdown と route.json を docs/pbi に書く。"""
        base = self.repo_path('docs', 'pbi')
        os.makedirs(base, exist_ok=True)
        lines = [f'# {pbi}: 題', '', '- Depends on: none', '', '## Goal', goal, '', '## Targets',
                 *[f'- `{t}`' for t in (targets or ['src/a.txt'])], '', '## Do not touch', '- none', '']
        if notes:
            lines += ['## Notes', notes, '']
        lines += ['## Refs', 'none', '']
        with open(os.path.join(base, f'{pbi}.md'), 'w', encoding='utf-8', newline='\n') as f:
            f.write('\n'.join(lines))
        with open(os.path.join(base, f'{pbi}.route.json'), 'w', encoding='utf-8', newline='\n') as f:
            json.dump({'route': [{'runner': 'agent', 'model': 'sonnet', 'effort': None}]}, f)

    def make_work(self, pbi, **pbi_options):
        """PBI の Markdown を書き、design_record.py work に作業票を作らせる。"""
        self.write_pbi(pbi, **pbi_options)
        result = self.run_cli_in(self.repo, 'work', pbi)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def write_reply(self, path, record):
        """reply-answered.md の封筒の revision と record_hash を、record に結び直した返信を書く。"""
        with open(self.path('reply-answered.md'), encoding='utf-8') as f:
            text = f.read()
        text = re.sub(r'"revision":"\d+"', f'"revision":"{record["revision"]}"', text)
        text = re.sub(r'"record_hash":"[0-9a-f]+"', f'"record_hash":"{self.record_hash_of(record)}"', text)
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)

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
        """slice は atelier.design-slice/2 で、設計のときの HEAD (base_sha) を持つ。git のリポジトリの中で動かす。"""
        head = self.git('rev-parse', 'HEAD').strip()
        result = self.run_cli_in(self.repo, 'slice', 'c2', '--question', 'どこに置くか', '--read', 'a/b.py:3-9',
                                 '--read', 'c.md:4', '--unknown', '利用者の選択')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = self.read_json(self.repo_path('docs', 'pbi', 'notes', 'c2.slice.json'))
        self.assertEqual(data, {'schema': 'atelier.design-slice/2', 'id': 'c2', 'question': 'どこに置くか',
                                'reads': [{'path': 'a/b.py', 'lines': [3, 9]}, {'path': 'c.md', 'lines': [4, 4]}],
                                'unknown': ['利用者の選択'], 'base_sha': head})
        bad = self.run_cli_in(self.repo, 'slice', 'c3', '--question', 'q', '--read', 'a.py:9-3')
        self.assertEqual(bad.returncode, 2)
        self.assertFalse(os.path.exists(self.repo_path('docs', 'pbi', 'notes', 'c3.slice.json')))

    # 作業票を正本に入れる (write --work)

    def test_write_with_works_bumps_revision_and_hash_on_content_or_order_change(self):
        self.make_work('PBI-1', goal='最初の目的')
        self.make_work('PBI-2', goal='別の目的', targets=['src/b.txt'])
        args = ('write', '--id', DESIGN_ID, '--target', 'PBI-1', '--target', 'PBI-2')
        both = (*args, '--work', 'PBI-1.work.json', '--work', 'PBI-2.work.json')
        first = self.run_cli_in(self.repo, *both)
        self.assertEqual(first.returncode, 0, first.stderr)
        record = self.read_repo_record()
        self.assertEqual(record['schema'], 'atelier.design-record/2')
        self.assertEqual(record['revision'], 1)
        self.assertEqual(record['works'], [
            {'pbi': 'PBI-1', 'file': 'PBI-1.work.json', 'hash': self.file_hash(self.work_path('PBI-1'))},
            {'pbi': 'PBI-2', 'file': 'PBI-2.work.json', 'hash': self.file_hash(self.work_path('PBI-2'))}])
        hash_1 = self.record_hash_of(record)
        # 作業票の中身が変わる
        self.make_work('PBI-1', goal='書き直した目的')
        changed = self.run_cli_in(self.repo, *both)
        self.assertEqual(changed.returncode, 0, changed.stderr)
        record = self.read_repo_record()
        self.assertEqual(record['revision'], 2)
        self.assertEqual(record['works'][0]['hash'], self.file_hash(self.work_path('PBI-1')))
        hash_2 = self.record_hash_of(record)
        self.assertNotEqual(hash_2, hash_1)
        # --work の順が変わる
        swapped = self.run_cli_in(self.repo, *args, '--work', 'PBI-2.work.json', '--work', 'PBI-1.work.json')
        self.assertEqual(swapped.returncode, 0, swapped.stderr)
        record = self.read_repo_record()
        self.assertEqual([w['pbi'] for w in record['works']], ['PBI-2', 'PBI-1'])
        self.assertEqual(record['revision'], 3)
        self.assertNotIn(self.record_hash_of(record), (hash_1, hash_2))

    def test_write_with_identical_works_keeps_revision_and_hash(self):
        self.make_work('PBI-1')
        self.make_work('PBI-2', targets=['src/b.txt'])
        args = ('write', '--id', DESIGN_ID, '--target', 'PBI-1', '--target', 'PBI-2',
                '--work', 'PBI-1.work.json', '--work', 'PBI-2.work.json')
        self.assertEqual(self.run_cli_in(self.repo, *args).returncode, 0)
        before = self.read_repo_record()
        with open(self.repo_path('docs', 'pbi', 'design-record.json'), 'rb') as f:
            before_bytes = f.read()
        again = self.run_cli_in(self.repo, *args)
        self.assertEqual(again.returncode, 0, again.stderr)
        after = self.read_repo_record()
        self.assertEqual(after['revision'], 1)
        self.assertEqual(self.record_hash_of(after), self.record_hash_of(before))
        with open(self.repo_path('docs', 'pbi', 'design-record.json'), 'rb') as f:
            self.assertEqual(f.read(), before_bytes)

    def test_write_without_work_keeps_schema_1_and_its_hash(self):
        """--work の無い write は /1 のまま。承認済みの返信 (answer-me-with-any が作ったもの) の record_hash が今も合う。"""
        result = self.run_cli('write', '--id', DESIGN_ID, '--target', 'PBI-012', '--target', 'PBI-013',
                              '--target', 'PBI-014', '--concern', 'notes/c1.slice.json')
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.read_record()
        self.assertEqual(record, RECORD)
        self.assertEqual(record['schema'], 'atelier.design-record/1')
        self.assertNotIn('works', record)
        with open(self.path('reply-answered.md'), encoding='utf-8') as f:
            given = re.search(r'"record_hash":"([0-9a-f]+)"', f.read()).group(1)
        self.assertEqual(self.record_hash_of(record), given)
        matched = self.check('reply-answered.md', '--draft', self.path('draft.md'), '--dry-run')
        self.assertEqual(matched.returncode, 0, matched.stdout + matched.stderr)

    def test_check_reply_matches_a_schema_2_record(self):
        self.make_work('PBI-1', goal='最初の目的')
        args = ('write', '--id', DESIGN_ID, '--target', 'PBI-1', '--work', 'PBI-1.work.json')
        self.assertEqual(self.run_cli_in(self.repo, *args).returncode, 0)
        record = self.read_repo_record()
        self.assertEqual(record['schema'], 'atelier.design-record/2')
        reply = self.repo_path('reply-works.md')
        self.write_reply(reply, record)
        matched = self.run_cli_in(self.repo, 'check-reply', '--reply', reply)
        self.assertEqual(matched.returncode, 0, matched.stdout + matched.stderr)
        # 記録を書いたあとに作業票を変え、記録を書き直すと、前の返信は一致しない
        self.make_work('PBI-1', goal='承認のあとで書き直した目的')
        self.assertEqual(self.run_cli_in(self.repo, *args).returncode, 0)
        newer = self.read_repo_record()
        self.assertEqual(newer['revision'], 2)
        stale = self.run_cli_in(self.repo, 'check-reply', '--reply', reply)
        self.assertEqual(stale.returncode, 3, stale.stdout + stale.stderr)
        self.assertTrue(stale.stdout.startswith('mismatch\t'), stale.stdout)
        self.assertEqual(len(self.repo_approvals()), 1)
        # 新しい版に結んだ返信は一致する
        self.write_reply(reply, newer)
        self.assertEqual(self.run_cli_in(self.repo, 'check-reply', '--reply', reply).returncode, 0)
        self.assertEqual(len(self.repo_approvals()), 2)

    def test_check_reply_rechecks_the_work_tickets_on_disk(self):
        """記録を書き直さずに作業票が変わった・消えた・記録に無い作業票が増えたときも、返信は一致しない。"""
        self.make_work('PBI-1', goal='最初の目的')
        args = ('write', '--id', DESIGN_ID, '--target', 'PBI-1', '--work', 'PBI-1.work.json')
        self.assertEqual(self.run_cli_in(self.repo, *args).returncode, 0)
        record = self.read_repo_record()
        reply = self.repo_path('reply-works.md')
        self.write_reply(reply, record)
        ticket = self.repo_path('docs', 'pbi', 'PBI-1.work.json')
        with open(ticket, 'rb') as f:
            original = f.read()

        def expect_mismatch(reason):
            result = self.run_cli_in(self.repo, 'check-reply', '--reply', reply)
            self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
            self.assertTrue(result.stdout.startswith('mismatch\t'), result.stdout)
            self.assertIn(reason, result.stdout + result.stderr)

        self.make_work('PBI-1', goal='記録のあとで書き換えた目的')
        expect_mismatch('changed since write')
        os.remove(ticket)
        expect_mismatch('missing since write')
        with open(ticket, 'wb') as f:
            f.write(original)
        self.make_work('PBI-2')
        expect_mismatch('not in the record')
        os.remove(self.repo_path('docs', 'pbi', 'PBI-2.work.json'))
        matched = self.run_cli_in(self.repo, 'check-reply', '--reply', reply)
        self.assertEqual(matched.returncode, 0, matched.stdout + matched.stderr)
        self.assertEqual(len(self.repo_approvals()), 1)

    def test_write_refuses_a_missing_or_foreign_work_file(self):
        self.make_work('PBI-1')
        good = ('write', '--id', DESIGN_ID, '--target', 'PBI-1', '--work', 'PBI-1.work.json')
        self.assertEqual(self.run_cli_in(self.repo, *good).returncode, 0)
        record_path = self.repo_path('docs', 'pbi', 'design-record.json')
        with open(record_path, 'rb') as f:
            before = f.read()
        shutil.copyfile(self.work_path('PBI-1'), self.repo_path('outside.work.json'))
        with open(self.work_path('PBI-9'), 'w', encoding='utf-8') as f:
            json.dump({'schema': 'atelier.other/1', 'pbi': 'PBI-9'}, f)
        cases = {'missing': 'PBI-5.work.json', 'outside the dir': '../outside.work.json',
                 'not atelier.work/1': 'PBI-9.work.json'}
        for name, work in cases.items():
            with self.subTest(work=name):
                result = self.run_cli_in(self.repo, 'write', '--id', DESIGN_ID, '--target', 'PBI-1',
                                         '--work', 'PBI-1.work.json', '--work', work)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                with open(record_path, 'rb') as f:
                    self.assertEqual(f.read(), before)

    def test_work_accepts_a_schema_1_slice(self):
        notes = self.repo_path('docs', 'pbi', 'notes')
        os.makedirs(notes, exist_ok=True)
        old = {'schema': 'atelier.design-slice/1', 'id': 'c1', 'question': '古い形',
               'reads': [{'path': 'src/a.txt', 'lines': [1, 2]}], 'unknown': []}
        other = {**old, 'schema': 'atelier.design-slice/9', 'id': 'c9'}
        for name, data in (('c1', old), ('c9', other)):
            with open(os.path.join(notes, f'{name}.slice.json'), 'w', encoding='utf-8') as f:
                json.dump(data, f)
        self.write_pbi('PBI-1', notes='slice: c1')
        accepted = self.run_cli_in(self.repo, 'work', 'PBI-1')
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(self.read_json(self.work_path('PBI-1'))['slices'], ['c1'])
        self.write_pbi('PBI-2', notes='slice: c9')
        refused = self.run_cli_in(self.repo, 'work', 'PBI-2')
        self.assertEqual(refused.returncode, 6, refused.stdout + refused.stderr)
        self.assertFalse(os.path.exists(self.work_path('PBI-2')))

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

    # approve_meta と question_meta (CLI ではなく、同じ関数を直接呼ぶ)

    @staticmethod
    def design_record_module():
        """design_record.py を同じフォルダから読む。この試験のプロセスで、CLI と同じ関数を呼ぶため。"""
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import design_record
        return design_record

    def test_approve_meta_equals_the_markdown_output(self):
        """approve_meta の戻り値は、同じ原稿で `markdown --draft` が出す標準出力と一致する (末尾の改行も)。
        呼んでも標準出力・標準エラーには何も出ず、終了もしない。LF と CRLF の原稿、frontmatter の無い原稿も同じ。"""
        design_record = self.design_record_module()
        with open(self.path('approve-good.md'), encoding='utf-8-sig', newline='') as f:
            raw = f.read()
        lf = raw.replace('\r\n', '\n')
        copies = {'approve-lf.md': lf, 'approve-crlf.md': lf.replace('\n', '\r\n'),
                  'approve-plain.md': lf.split('---\n', 2)[2]}
        for name, text in copies.items():
            with open(self.path(name), 'w', encoding='utf-8', newline='') as f:
                f.write(text)
        record = self.read_record()
        for name, text in {'approve-good.md': raw, **copies}.items():
            with self.subTest(draft=name):
                cli = self.run_cli('markdown', '--draft', self.path(name))
                self.assertEqual(cli.returncode, 0, cli.stdout + cli.stderr)
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    result = design_record.approve_meta(text, record)
                self.assertEqual((out.getvalue(), err.getvalue()), ('', ''))
                self.assertEqual(result, cli.stdout)

    def test_approve_meta_raises_draft_error_on_a_lint_error(self):
        """lint の誤りがある原稿では DraftError (ValueError の子) を投げ、SystemExit では止めない。
        errors と warnings は lint_draft が返すものと同じ (draft.md は markdown が 6 で止める原稿)。"""
        design_record = self.design_record_module()
        with open(self.path('draft.md'), encoding='utf-8-sig', newline='') as f:
            text = f.read()
        errors, warnings = design_record.lint_draft(text)
        self.assertTrue(errors)
        try:
            design_record.approve_meta(text, self.read_record())
        except SystemExit:
            self.fail('approve_meta exited instead of raising DraftError')
        except design_record.DraftError as error:
            self.assertEqual(error.errors, errors)
            self.assertEqual(error.warnings, warnings)
        else:
            self.fail('approve_meta returned without raising DraftError')

    def test_question_meta_writes_the_four_items_and_the_draft_hash(self):
        """question_meta は frontmatter に reply-meta の 4 項目 (run_id・seq・kind・draft_hash) だけを足す。
        draft_hash は envelope_base の原稿から取り、出力から取り直しても同じ。渡し直すと同じ出力になる。run_id が None なら none。"""
        design_record = self.design_record_module()
        with open(self.path('approve-good.md'), encoding='utf-8-sig', newline='') as f:
            text = f.read()
        h = design_record.draft_hash(design_record.envelope_base(text))
        out = design_record.question_meta(text, 'run-1234', 3, 'decision')
        lines = out.split('\n')
        front = lines[1:lines.index('---', 1)]
        self.assertEqual([line for line in front if line.startswith('reply-meta-')],
                         ['reply-meta-run_id: run-1234', 'reply-meta-seq: 3', 'reply-meta-kind: decision',
                          f'reply-meta-draft_hash: {h}'])
        self.assertEqual(design_record.draft_hash(out), h)
        self.assertEqual(design_record.question_meta(out, 'run-1234', 3, 'decision'), out)
        without_run = design_record.question_meta(text, None, 3, 'decision')
        self.assertIn('reply-meta-run_id: none', without_run.split('\n'))

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

    def test_saving_an_approval_emits_question_answered(self):
        result = self.check('reply-answered.md', '--draft', self.path('draft.md'))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = [row for row in self.events() if row['type'] == 'question.answered']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['schema'], 'atelier.event/1')
        self.assertEqual(rows[0]['data'], {'kind': 'approve', 'topic': 'design-approval', 'choice': None,
                                           'wait_s': None, 'revise': False})

    def test_a_one_letter_reply_is_the_choice(self):
        with open(self.path('reply-answered.md'), encoding='utf-8') as f:
            text = f.read().replace('**リポジトリ外のキャッシュ**', 'a')
        with open(self.path('reply-letter.md'), 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        self.assertEqual(self.check('reply-letter.md').returncode, 0)
        rows = [row for row in self.events() if row['type'] == 'question.answered']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['data']['choice'], 'a')

    def test_quiet_and_dry_run_emit_no_event(self):
        quiet = self.run_cli('check-reply', '--reply', self.path('reply-answered.md'),
                             '--draft', self.path('draft.md'), env={'ATELIER_EVENTS_QUIET': '1'})
        self.assertEqual(quiet.returncode, 0, quiet.stdout + quiet.stderr)
        self.assertEqual(self.events(), [])
        dry = self.check('reply-answered.md', '--draft', self.path('draft.md'), '--dry-run')
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertEqual(self.events(), [])

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
FAKE_YOMIYASU = os.path.join(HERE, 'testdata', 'fake_yomiyasu.py')
MARKER = 'ヨミヤス候補'


def isolated_env(base, **extra):
    """本物の home・プラグイン・送り手の設定に触れさせない環境。"""
    return dict(os.environ, ATELIER_HOME=os.path.join(base, 'home'), PBI_PLUGINS_DIR=os.path.join(base, 'plugins'),
                HARNESS_LOG_CONFIG=os.path.join(base, 'no-harness-log.json'), ATELIER_EVENTS_SETTLE='off',
                ATELIER_EVENTS_QUIET='1', **extra)


class ApprovalDraftLintTest(unittest.TestCase):
    """design_record.py lint: 承認の原稿の 7 つの節・図・表・選択肢の補足を点検し、内部の言葉に警告を出す。"""

    def setUp(self):
        self.dir = os.path.join(tempfile.gettempdir(), f'approval-lint-{uuid.uuid4().hex}')
        os.makedirs(self.dir)
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        with open(os.path.join(FIXTURES, 'approve-good.md'), encoding='utf-8') as f:
            self.good = f.read()
        # 本物の home・プラグインの yomiyasu を拾わせない
        self.env = isolated_env(self.dir)

    def lint(self, text):
        path = os.path.join(self.dir, 'approve.md')
        with open(path, 'w', encoding='utf-8', newline='') as f:
            f.write(text)
        result = subprocess.run([sys.executable, SCRIPT, 'lint', '--draft', path], capture_output=True, env=self.env)
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
        result = subprocess.run([sys.executable, SCRIPT, 'lint', '--draft', EXAMPLE], capture_output=True, env=self.env)
        stderr = result.stderr.decode('utf-8', errors='replace')
        self.assertEqual(result.returncode, 0, stderr)
        self.assertNotIn('! L', stderr)


class YomiyasuLintTest(unittest.TestCase):
    """design_record.py lint / markdown: 日本語の原稿に yomiyasu の候補を警告で足す。道具は偽物 (testdata/fake_yomiyasu.py)。"""

    IMPACT = '点検しなかった (変える所が 2 か所だけで、ほかから頼られていないため)。'
    OPTION = '- リポジトリの中 | 誰でも同じ図を見られる。差分が毎回出る'
    QUESTION = '図の置き場所はどちらにしますか'

    def setUp(self):
        self.dir = os.path.join(tempfile.gettempdir(), f'yomiyasu-lint-{uuid.uuid4().hex}')
        os.makedirs(os.path.join(self.dir, 'home'))
        os.makedirs(os.path.join(self.dir, 'plugins'))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        with open(os.path.join(FIXTURES, 'approve-good.md'), encoding='utf-8') as f:
            self.good = f.read()
        self.dump = os.path.join(self.dir, 'dump.json')
        self.point_at(FAKE_YOMIYASU)

    def point_at(self, path):
        with open(os.path.join(self.dir, 'home', 'agents.json'), 'w', encoding='utf-8') as f:
            json.dump({'tools': {'yomiyasu': {'path': path}}}, f)

    def env(self, **extra):
        return isolated_env(self.dir, FAKE_YOMIYASU_DUMP=self.dump, ATELIER_YOMIYASU_TIMEOUT='2', **extra)

    def lint(self, text, command='lint', **extra):
        path = os.path.join(self.dir, 'approve.md')
        with open(path, 'w', encoding='utf-8', newline='') as f:
            f.write(text)
        args = [command, '--draft', path]
        if command == 'markdown':
            pbi = os.path.join(self.dir, 'pbi')
            shutil.copytree(os.path.join(FIXTURES, 'pbi'), pbi, dirs_exist_ok=True)
            args += ['--dir', pbi]
        result = subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, env=self.env(**extra))
        return subprocess.CompletedProcess(result.args, result.returncode, result.stdout.decode('utf-8'),
                                           result.stderr.decode('utf-8', errors='replace'))

    def line_of(self, text, needle):
        return text.split('\n').index(needle) + 1

    def candidates(self, result):
        return [row for row in result.stderr.splitlines() if ' yomiyasu fake_rule' in row]

    def skipped(self, result):
        return [row for row in result.stderr.splitlines() if row.startswith('! yomiyasu skipped')]

    def test_a_candidate_in_prose_is_a_warning_at_the_file_line(self):
        text = self.good.replace(self.IMPACT, self.IMPACT + MARKER)
        result = self.lint(text)
        self.assertEqual(result.returncode, 0, result.stderr)
        line = self.line_of(text, self.IMPACT + MARKER)
        self.assertGreater(line, 5, 'the file line counts the frontmatter')
        self.assertEqual(self.candidates(result), [f'! L{line} yomiyasu fake_rule → {MARKER}を言い直す'])
        self.assertTrue(result.stdout.startswith('ok\t'), result.stdout)
        self.assertEqual(self.skipped(result), [])

    def test_a_clean_draft_has_no_candidate_and_exit_0(self):
        result = self.lint(self.good)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('! L', result.stderr)
        self.assertTrue(os.path.exists(self.dump), 'yomiyasu ran on a Japanese draft')

    def test_crlf_draft_keeps_the_file_line(self):
        text = self.good.replace(self.IMPACT, self.IMPACT + MARKER)
        result = self.lint(text.replace('\n', '\r\n'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.candidates(result)), 1, result.stderr)
        self.assertIn(f'! L{self.line_of(text, self.IMPACT + MARKER)} yomiyasu', result.stderr)

    def test_a_marker_inside_a_diagram_fence_is_not_reported(self):
        text = self.good.replace('```flow\n', '```flow\n' + MARKER + '\n', 1)
        result = self.lint(text)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.candidates(result), [])

    def test_a_marker_in_an_ask_question_and_option_is_reported(self):
        text = self.good.replace(self.QUESTION, self.QUESTION + MARKER).replace(self.OPTION, self.OPTION + MARKER)
        result = self.lint(text)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = sorted(int(row.split()[1][1:]) for row in self.candidates(result))
        self.assertEqual(lines, [self.line_of(text, self.QUESTION + MARKER), self.line_of(text, self.OPTION + MARKER)])
        with open(self.dump, encoding='utf-8') as f:
            sent = json.load(f)['text']
        self.assertNotIn('```', sent)
        self.assertNotIn('lang: ja', sent, 'the frontmatter is not sent')

    def test_a_marker_in_the_callout_is_reported(self):
        callout = '設計の受け渡しを、HTML のページで読んで返事を貼る形にする。'
        text = self.good.replace(callout, callout + MARKER)
        result = self.lint(text)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.candidates(result),
                         [f'! L{self.line_of(text, callout + MARKER)} yomiyasu fake_rule → {MARKER}を言い直す'])

    def test_ask_options_are_sent_without_the_list_mark(self):
        result = self.lint(self.good)
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(self.dump, encoding='utf-8') as f:
            sent = json.load(f)['text'].split('\n')
        self.assertIn(self.OPTION[2:], sent)
        self.assertNotIn(self.OPTION, sent)

    def test_exit_code_stays_6_for_a_draft_with_errors(self):
        text = self.good.replace(self.IMPACT, self.IMPACT + MARKER).replace('## 作る人と見直し', '## 段ごとの記録\n- 計画\n')
        result = self.lint(text)
        self.assertEqual(result.returncode, 6, result.stderr)
        self.assertIn('✗ L', result.stderr)
        self.assertEqual(len(self.candidates(result)), 1, result.stderr)

    def test_a_missing_tool_is_one_skipped_line_and_other_checks_run(self):
        self.point_at(os.path.join(self.dir, 'nowhere', 'yomiyasu_lint.py'))
        row = '| PBI-012 | route と tier を変える | ページで設計を読める | なし |'
        text = self.good.replace('| PBI-012 | 承認の一覧をページにする | ページで設計を読める | なし |', row)
        result = self.lint(text)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.skipped(result)), 1, result.stderr)
        self.assertTrue(self.skipped(result)[0].endswith('(tools_check.py)'), result.stderr)
        self.assertIn(f'! L{self.line_of(text, row)} route', result.stderr)
        broken = self.lint(text.replace('## 影響範囲', '## 段ごとの記録\n'))
        self.assertEqual(broken.returncode, 6)
        self.assertEqual(len(self.skipped(broken)), 1, broken.stderr)
        self.assertIn('✗ L', broken.stderr)

    def test_a_tool_that_fails_is_one_skipped_line_and_the_exit_code_is_unchanged(self):
        text = self.good.replace(self.IMPACT, self.IMPACT + MARKER)
        for mode in ('crash', 'garbage', 'format3', 'sleep'):
            with self.subTest(mode=mode):
                result = self.lint(text, FAKE_YOMIYASU_MODE=mode)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(self.skipped(result)), 1, result.stderr)
                self.assertEqual(self.candidates(result), [])
                self.assertNotIn('Traceback', result.stderr)

    def test_an_english_draft_does_not_call_yomiyasu(self):
        result = self.lint('---\nlang: en\n---\n## Summary\nHello there.\n')
        self.assertEqual(result.returncode, 6, result.stderr)
        self.assertFalse(os.path.exists(self.dump), 'yomiyasu must not run')
        self.assertNotIn('yomiyasu', result.stderr)

    def test_lang_ja_alone_calls_yomiyasu(self):
        result = self.lint('---\nlang: ja\n---\n## Summary\nHello there.\n')
        self.assertTrue(os.path.exists(self.dump), result.stderr)

    def test_kanji_alone_in_a_draft_without_frontmatter_calls_yomiyasu(self):
        result = self.lint('## Summary\n結論です。\n')
        self.assertTrue(os.path.exists(self.dump), result.stderr)

    def test_a_question_draft_is_checked_too(self):
        with open(os.path.join(FIXTURES, 'question-good.md'), encoding='utf-8') as f:
            good = f.read()
        text = good.replace('## 結論\n', '## 結論\n' + MARKER + '\n', 1)
        path = os.path.join(self.dir, 'question.md')
        with open(path, 'w', encoding='utf-8', newline='') as f:
            f.write(text)
        result = subprocess.run([sys.executable, SCRIPT, 'lint', '--draft', path, '--kind', 'decision'],
                                capture_output=True, env=self.env())
        stderr = result.stderr.decode('utf-8', errors='replace')
        self.assertEqual(result.returncode, 0, stderr)
        self.assertIn(f'! L{text.split(chr(10)).index(MARKER) + 1} yomiyasu fake_rule', stderr)

    def test_a_character_outside_the_ansi_code_page_does_not_hang(self):
        text = self.good.replace(self.IMPACT, self.IMPACT + '𠮷野家' + MARKER)
        result = self.lint(text)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.candidates(result)), 1, result.stderr)
        self.assertEqual(self.skipped(result), [])

    def test_markdown_checks_too_and_its_stdout_is_unchanged(self):
        text = self.good.replace(self.IMPACT, self.IMPACT + MARKER)
        with_tool = self.lint(text, 'markdown')
        self.assertEqual(with_tool.returncode, 0, with_tool.stderr)
        self.assertEqual(len(self.candidates(with_tool)), 1, with_tool.stderr)
        self.point_at(os.path.join(self.dir, 'nowhere', 'yomiyasu_lint.py'))
        without = self.lint(text, 'markdown')
        self.assertEqual(without.returncode, 0, without.stderr)
        self.assertEqual(len(self.skipped(without)), 1, without.stderr)
        self.assertEqual(with_tool.stdout, without.stdout)
        self.assertNotIn('yomiyasu', with_tool.stdout)


class YomiyasuPathTest(unittest.TestCase):
    """design_record.yomiyasu_path: agents.json の tools.yomiyasu.path、無ければプラグインのキャッシュのいちばん新しい版。"""

    def setUp(self):
        self.dir = os.path.join(tempfile.gettempdir(), f'yomiyasu-path-{uuid.uuid4().hex}')
        self.home = os.path.join(self.dir, 'home')
        self.plugins = os.path.join(self.dir, 'plugins')
        os.makedirs(self.home)
        os.makedirs(self.plugins)
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        sys.path.insert(0, HERE)
        self.addCleanup(sys.path.remove, HERE)
        import design_record
        self.module = design_record

    def cache(self, version):
        path = os.path.join(self.plugins, 'cache', 'yomiyasu', 'yomiyasu', version, 'scripts', 'yomiyasu_lint.py')
        os.makedirs(os.path.dirname(path))
        with open(path, 'w', encoding='utf-8') as f:
            f.write('')
        return path

    def find(self):
        found = self.module.yomiyasu_path(home=self.home, env={'PBI_PLUGINS_DIR': self.plugins})
        return None if found is None else os.path.normpath(str(found))

    def test_nothing_found_is_none(self):
        self.assertIsNone(self.find())

    def test_the_highest_cached_version_wins_by_number(self):
        self.cache('2.9.0')
        newest = self.cache('2.10.0')
        self.cache('2.2.5')
        self.assertEqual(self.find(), os.path.normpath(newest))

    def test_a_version_folder_without_the_script_is_skipped(self):
        older = self.cache('2.1.0')
        os.makedirs(os.path.join(self.plugins, 'cache', 'yomiyasu', 'yomiyasu', '3.0.0'))
        self.assertEqual(self.find(), os.path.normpath(older))

    def test_agents_json_wins_when_the_file_exists(self):
        self.cache('2.3.0')
        with open(os.path.join(self.home, 'agents.json'), 'w', encoding='utf-8') as f:
            json.dump({'tools': {'yomiyasu': {'path': FAKE_YOMIYASU}}}, f)
        self.assertEqual(self.find(), os.path.normpath(FAKE_YOMIYASU))

    def test_agents_json_pointing_nowhere_falls_back_to_the_cache(self):
        cached = self.cache('2.3.0')
        with open(os.path.join(self.home, 'agents.json'), 'w', encoding='utf-8') as f:
            json.dump({'tools': {'yomiyasu': {'path': os.path.join(self.dir, 'gone.py')}}}, f)
        self.assertEqual(self.find(), os.path.normpath(cached))

    def test_lint_text_never_runs_yomiyasu(self):
        dump = os.path.join(self.dir, 'dump.json')
        with open(os.path.join(self.home, 'agents.json'), 'w', encoding='utf-8') as f:
            json.dump({'tools': {'yomiyasu': {'path': FAKE_YOMIYASU}}}, f)
        saved = {key: os.environ.get(key) for key in ('ATELIER_HOME', 'FAKE_YOMIYASU_DUMP')}
        os.environ.update(ATELIER_HOME=self.home, FAKE_YOMIYASU_DUMP=dump)

        def restore():
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.addCleanup(restore)
        with open(os.path.join(FIXTURES, 'approve-good.md'), encoding='utf-8') as f:
            self.module.lint_text(f.read(), 'approve')
        self.assertFalse(os.path.exists(dump))


QUESTION_KINDS = ('decision', 'send-approval', 'rework-limit', 'destructive', 'full-run', 'handoff-loop',
                  'launch-failure')


class QuestionDraftLintTest(unittest.TestCase):
    """design_record.py lint --kind <kind>: 問いの原稿の 6 つの節・図・ask を点検する。--kind を省くと承認の点検。"""

    def setUp(self):
        self.dir = os.path.join(tempfile.gettempdir(), f'question-lint-{uuid.uuid4().hex}')
        os.makedirs(self.dir)
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        with open(os.path.join(FIXTURES, 'question-good.md'), encoding='utf-8') as f:
            self.good = f.read()
        # 本物の home・プラグイン・送り手の設定に触れさせない
        self.env = dict(os.environ, ATELIER_HOME=os.path.join(self.dir, 'home'),
                        PBI_PLUGINS_DIR=os.path.join(self.dir, 'plugins'),
                        HARNESS_LOG_CONFIG=os.path.join(self.dir, 'no-harness-log.json'),
                        ATELIER_EVENTS_SETTLE='off', ATELIER_EVENTS_QUIET='1')

    def lint(self, text, *extra, kind='decision'):
        path = os.path.join(self.dir, 'question.md')
        with open(path, 'w', encoding='utf-8', newline='') as f:
            f.write(text)
        args = ['--kind', kind] if kind else []
        result = subprocess.run([sys.executable, SCRIPT, 'lint', '--draft', path, *args, *extra],
                                capture_output=True, env=self.env)
        return subprocess.CompletedProcess(result.args, result.returncode, result.stdout.decode('utf-8'),
                                           result.stderr.decode('utf-8', errors='replace'))

    def line_of(self, text, needle):
        return text.split('\n').index(needle) + 1

    def assert_error(self, text, line, word, kind='decision'):
        result = self.lint(text, kind=kind)
        self.assertEqual(result.returncode, 6, result.stdout + result.stderr)
        errors = [row for row in result.stderr.splitlines() if row.startswith('✗ L')]
        self.assertTrue(any(row.startswith(f'✗ L{line} ') and word in row for row in errors),
                        f'no "✗ L{line} … {word}" in:\n{result.stderr}')
        return result

    def replace_section(self, heading, next_heading, body):
        start, end = self.good.index(heading), self.good.index(next_heading)
        return self.good[:start] + body + self.good[end:]

    def ask_close(self, text):
        """ask の囲みを閉じる行の位置 (文字の位置)。"""
        start = text.index('```ask\n')
        return text.index('\n```\n', start) + 1

    def test_a_longer_closing_fence_closes_the_block(self):
        # 結論の callout を ```` で閉じる。閉じないと、あとの必ずの節が囲みの中に消える
        start = self.good.index('```callout')
        at = self.good.index('\n```\n', start) + 1
        text = self.good[:at] + '````' + self.good[at + 3:]
        result = self.lint(text)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_an_unclosed_ask_at_the_end_is_an_error(self):
        at = self.ask_close(self.good)
        text = self.good[:at]  # 4 つの必ずの節だけで、最後の ask の閉じが無い
        self.assert_error(text, self.line_of(text, '```ask'), '閉じていない')

    def test_good_draft_passes_for_every_kind(self):
        for kind in QUESTION_KINDS:
            with self.subTest(kind=kind):
                result = self.lint(self.good, kind=kind)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn('✗', result.stderr)
                self.assertNotIn('! L', result.stderr)
                self.assertTrue(result.stdout.startswith('ok\t'), result.stdout)

    def test_crlf_draft_passes(self):
        self.assertEqual(self.lint(self.good.replace('\n', '\r\n')).returncode, 0)

    def test_unknown_kind_is_refused(self):
        self.assertNotEqual(self.lint(self.good, kind='bogus').returncode, 0)

    def test_kind_approve_runs_the_approval_rules(self):
        result = self.lint(self.good, kind='approve')
        self.assertEqual(result.returncode, 6, result.stderr)
        self.assertIn('承認の原稿', result.stderr)

    def test_without_kind_the_approval_rules_run(self):
        result = self.lint(self.good, kind=None)
        self.assertEqual(result.returncode, 6, result.stderr)
        self.assertIn('in the approval draft', result.stderr)
        self.assertNotIn('question draft', result.stderr)

    def test_approval_draft_passes_without_kind(self):
        with open(os.path.join(FIXTURES, 'approve-good.md'), encoding='utf-8') as f:
            result = self.lint(f.read(), kind=None)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_approval_draft_with_none_decisions_still_passes_as_approve(self):
        with open(os.path.join(FIXTURES, 'approve-good.md'), encoding='utf-8') as f:
            good = f.read()
        start, end = good.index('## 決めること'), good.index('## PBI')
        result = self.lint(good[:start] + '## 決めること\nなし\n\n' + good[end:], kind='approve')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_error_trailer_names_the_question_draft(self):
        text = self.replace_section('## 決めること', '## 判断の材料', '## 決めること\nなし\n\n')
        result = self.lint(text)
        self.assertEqual(result.returncode, 6)
        self.assertIn('errors in the question draft', result.stderr)
        self.assertNotIn('approval draft', result.stderr)

    def test_missing_required_section_is_an_error(self):
        text = self.replace_section('## 今の流れ', '## 答えたあとの流れ', '')
        self.assert_error(text, self.line_of(text, '## 答えたあとの流れ'), '今の流れ')

    def test_missing_summary_is_an_error(self):
        text = self.replace_section('## 結論', '## 今の流れ', '')
        self.assert_error(text, self.line_of(text, '## 今の流れ'), '結論')

    def test_sections_out_of_order_is_an_error(self):
        start, end = self.good.index('## 今の流れ'), self.good.index('## 答えたあとの流れ')
        text = self.good[:start] + self.good[end:self.good.index('## 決めること')] + self.good[start:end] \
            + self.good[self.good.index('## 決めること'):]
        self.assert_error(text, self.line_of(text, '## 今の流れ'), '順')

    def test_optional_sections_out_of_order_is_an_error(self):
        start, end = self.good.index('## 判断の材料'), self.good.index('## 影響範囲')
        text = self.good[:start] + self.good[end:] + '\n' + self.good[start:end]
        self.assert_error(text, self.line_of(text, '## 判断の材料'), '順')

    def test_duplicate_section_is_an_error(self):
        text = self.good + '\n## 影響範囲\nもう 1 つ。\n'
        rows = text.split('\n')
        self.assert_error(text, rows.index('もう 1 つ。'), '2 つ')

    def test_other_sections_are_errors(self):
        for heading in ('## PBI', '## PBIs', '## 作る人と見直し', '## Who builds', '## 段ごとの記録'):
            with self.subTest(heading=heading):
                text = self.good + f'\n{heading}\n| PBI | 何をするか |\n|---|---|\n'
                self.assert_error(text, self.line_of(text, heading), heading[3:])

    def test_pbi_table_header_rule_is_not_applied(self):
        text = self.good.replace('| 置き場所 | 見られる人 | 差分 |', '| 題 | 段 | route の先頭 |')
        self.assertEqual(self.lint(text).returncode, 0)

    def test_optional_sections_can_be_omitted(self):
        text = self.good[:self.good.index('## 判断の材料')]
        result = self.lint(text)
        self.assertEqual(result.returncode, 0, result.stderr)
        only_impact = self.replace_section('## 判断の材料', '## 影響範囲', '')
        self.assertEqual(self.lint(only_impact).returncode, 0)

    def test_before_without_a_diagram_is_an_error(self):
        text = self.replace_section('## 今の流れ', '## 答えたあとの流れ', '## 今の流れ\nチャットに一覧を出している。\n\n')
        self.assert_error(text, self.line_of(text, '## 今の流れ'), '図')

    def test_empty_after_diagram_is_an_error(self):
        text = self.replace_section('## 答えたあとの流れ', '## 決めること', '## 答えたあとの流れ\n```flow\n```\n\n')
        self.assert_error(text, self.line_of(text, '## 答えたあとの流れ'), '図')

    def test_other_diagram_kinds_pass(self):
        for kind, body in (('tree', 'docs/\n  pbi/'), ('er', 'PBI ||--o{ 作業票'), ('sequence', '設計者 -> 利用者: 聞く')):
            with self.subTest(kind=kind):
                text = self.replace_section('## 今の流れ', '## 答えたあとの流れ', f'## 今の流れ\n```{kind}\n{body}\n```\n\n')
                self.assertEqual(self.lint(text).returncode, 0)

    def test_after_without_marks_or_labels_is_an_error(self):
        text = self.replace_section('## 答えたあとの流れ', '## 決めること',
                                    '## 答えたあとの流れ\n```flow\n(設計者が図を描く) -> 図の置き場所を決める\n```\n\n')
        self.assert_error(text, self.line_of(text, '## 答えたあとの流れ'), '印')

    def test_after_with_only_edge_labels_passes(self):
        for edge in ('{置くか} -> リポジトリの中に保存する: 中に置く', '{置くか} -|中に置く|-> リポジトリの中に保存する'):
            with self.subTest(edge=edge):
                text = self.replace_section('## 答えたあとの流れ', '## 決めること',
                                            f'## 答えたあとの流れ\n```flow\n{edge}\n{{置くか}} -> 外に置く: 外に置く\n```\n\n')
                result = self.lint(text)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_after_with_only_change_marks_passes(self):
        text = self.replace_section('## 答えたあとの流れ', '## 決めること',
                                    '## 答えたあとの流れ\n```flow\n+ 設計者が図を描く -> 中に保存する\n~ 承認する\n```\n\n')
        self.assertEqual(self.lint(text).returncode, 0)

    def test_change_marks_outside_the_after_diagram_do_not_count(self):
        text = self.replace_section('## 答えたあとの流れ', '## 決めること',
                                    '## 答えたあとの流れ\n+ 文の中の印の行\n```flow\n図の置き場所を決める -> 保存する\n```\n\n')
        self.assert_error(text, self.line_of(text, '## 答えたあとの流れ'), '印')

    def test_before_diagram_needs_no_marks(self):
        self.assertEqual(self.lint(self.good).returncode, 0)

    def test_no_decisions_is_an_error(self):
        text = self.replace_section('## 決めること', '## 判断の材料', '## 決めること\n\n')
        self.assert_error(text, self.line_of(text, '## 決めること'), 'ask')

    def test_none_decisions_is_an_error(self):
        for word in ('なし', 'None', 'none', '- なし'):
            with self.subTest(word=word):
                text = self.replace_section('## 決めること', '## 判断の材料', f'## 決めること\n{word}\n\n')
                self.assert_error(text, self.line_of(text, '## 決めること'), 'ask')

    def test_option_without_a_note_is_an_error(self):
        text = self.good.replace('- リポジトリの外 | 差分は出ない。図を見るには別の場所へ行く', '- リポジトリの外')
        self.assert_error(text, self.line_of(text, '- リポジトリの外'), '|')

    def test_blank_note_is_an_error(self):
        text = self.good.replace('- リポジトリの外 | 差分は出ない。図を見るには別の場所へ行く', '- リポジトリの外 |  ')
        self.assert_error(text, self.line_of(text, '- リポジトリの外 |  '), '|')

    def test_ask_without_options_is_an_error(self):
        text = self.replace_section('## 決めること', '## 判断の材料', '## 決めること\n```ask\n置き場所は?\n```\n\n')
        self.assert_error(text, self.line_of(text, '```ask'), '選択肢')

    def test_ask_fence_the_reply_check_cannot_read_is_an_error(self):
        text = self.good.replace('```ask', '~~~ask').replace('差分は出ない。図を見るには別の場所へ行く\n```',
                                                          '差分は出ない。図を見るには別の場所へ行く\n~~~')
        self.assert_error(text, self.line_of(text, '~~~ask'), '~~~ask')

    def test_ask_outside_decisions_is_an_error(self):
        ask = '```ask\n点検しますか\n* する | 時間がかかる\n- しない | 速い\n```\n'
        text = self.good.replace('## 影響範囲\n', '## 影響範囲\n' + ask)
        self.assert_error(text, self.line_of(text, '## 影響範囲') + 1, '決めること')

    def test_every_error_is_listed_at_once(self):
        text = self.good.replace('- リポジトリの外 | 差分は出ない。図を見るには別の場所へ行く', '- リポジトリの外')
        text = text.replace('+ 図の置き場所を決める -> リポジトリの中に保存する\n+ リポジトリの中に保存する -> 利用者が承認する\n'
                            '- 図をページに埋め込む -> 利用者が承認する\n', '')
        result = self.lint(text)
        self.assertEqual(result.returncode, 6)
        self.assertEqual(len([row for row in result.stderr.splitlines() if row.startswith('✗ L')]), 2, result.stderr)
        self.assertEqual(result.stderr.count('  例: '), 2, result.stderr)

    def test_english_headings_pass(self):
        names = {'## 結論': '## Summary', '## 今の流れ': '## Before', '## 答えたあとの流れ': '## After',
                 '## 決めること': '## Decisions', '## 判断の材料': '## Details', '## 影響範囲': '## Impact'}
        text = self.good
        for ja, en in names.items():
            text = text.replace(ja, en)
        result = self.lint(text)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_panel_id_and_attributes_are_ignored(self):
        self.assertEqual(self.lint(self.good.replace('## 結論', '## A 結論 {span=2}')).returncode, 0)

    def test_new_internal_terms_are_warnings(self):
        terms = ('rework-limit', 'send-approval', 'full-run', 'handoff-loop', 'launch-failure', 'no-agent-tool',
                 'grill', 'stale')
        for term in terms:
            with self.subTest(term=term):
                for sentence in (f'{term} を使う。', f'{term}の問い。'):
                    text = self.good.replace('## 判断の材料\n', f'## 判断の材料\n{sentence}\n')
                    result = self.lint(text)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    line = self.line_of(text, sentence)
                    self.assertTrue(any(row.startswith(f'! L{line} {term} ') for row in result.stderr.splitlines()),
                                    f'no warning for {term}:\n{result.stderr}')

    def test_kind_names_that_are_plain_words_are_not_warned(self):
        text = self.good.replace('承認の図を、', 'approve と decision と destructive を使う承認の図を、')
        self.assertNotIn('! L', self.lint(text).stderr)

    def test_hyphenated_term_inside_a_longer_word_is_not_warned(self):
        text = self.good.replace('承認の図を、', 'prefull-runner と grilled と staleness の図を、')
        self.assertNotIn('! L', self.lint(text).stderr)

    def test_new_internal_terms_also_warn_in_approval_drafts(self):
        with open(os.path.join(FIXTURES, 'approve-good.md'), encoding='utf-8') as f:
            good = f.read()
        path = os.path.join(self.dir, 'approve.md')
        with open(path, 'w', encoding='utf-8', newline='') as f:
            f.write(good.replace('## 結論\n', '## 結論\nrework-limit に当たった。\n', 1))
        result = subprocess.run([sys.executable, SCRIPT, 'lint', '--draft', path], capture_output=True, env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('rework-limit', result.stderr.decode('utf-8', errors='replace'))

    def test_lint_text_returns_errors_and_warnings(self):
        sys.path.insert(0, HERE)
        self.addCleanup(sys.path.remove, HERE)
        import design_record
        errors, warnings = design_record.lint_text(self.good, 'decision')
        self.assertEqual((errors, warnings), ([], []))
        errors, warnings = design_record.lint_text(self.good.replace('承認の図を、', 'grill の承認の図を、'), 'send-approval')
        self.assertEqual(errors, [])
        self.assertEqual([w[1] for w in warnings], ['grill'])
        errors, _ = design_record.lint_text(self.good.replace('```ask', '```text'), 'decision')
        self.assertTrue(errors and all(len(e) == 3 for e in errors))
        errors, _ = design_record.lint_text(self.good)
        self.assertTrue(errors, 'the default kind is approve: a question draft is not an approval draft')
        with open(os.path.join(FIXTURES, 'approve-good.md'), encoding='utf-8') as f:
            self.assertEqual(design_record.lint_text(f.read(), 'approve'), ([], []))


if __name__ == '__main__':
    unittest.main()
