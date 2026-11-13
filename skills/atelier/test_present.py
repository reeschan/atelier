"""present.py を CLI と公開関数 (shown・saved_draft・log_answered・tokens_est) から動かし、html・md・off の見せ方・
照合情報・見せた記録 (presentations.jsonl)・am と node の欠け・原稿の誤り (exit 3)・引数の誤り (exit 2) を、
呼び出し側が見るもの (標準出力・終了コード・残ったファイル) で確かめる。

この PC の am と node は見ない。偽の am (testdata/present/am.mjs) を一時ディレクトリの
skills/answer-me-with-any/scripts に置き、偽の node (testdata/present/fake_node.py) を
node.cmd / node として別の一時ディレクトリに置いて、PATH をそのディレクトリだけにする。
home (agents.json) とユーザーのホームも一時ディレクトリにし、~/.claude/skills は読まない。
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
FIXTURES = os.path.join(HERE, 'testdata', 'present')
DESIGN_FIXTURES = os.path.join(HERE, 'testdata', 'design')
sys.path.insert(0, FIXTURES)
import common  # noqa: E402
import design_record  # noqa: E402
import fakes  # noqa: E402
import present  # noqa: E402  (tokens_est・shown・saved_draft・log_answered は conductor も使う公開の契約)

SCRIPT = os.path.join(HERE, 'present.py')
DESIGN_RECORD = os.path.join(HERE, 'design_record.py')
CONDUCTOR = (Path(HERE).resolve() / 'conductor.py').as_posix()
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
OLD_NAME = 'answer-me-with-html'

QUESTION = ('# 問い: 進め方\n\n'
            'このまま進めますか。\n\n'
            '```ask\n進め方は?\n- *A で進める | 速い\n- B で進める\n```\n')
APPROVE = ('---\ntitle: 承認\n---\n'
           '# PBI の一覧\n\n- PBI-001 confirm の値\n- PBI-002 present\n')
REPORT = '# 完了報告\n\n全部の PBI が通った。\n'
BROKEN_SOURCE = '---\nreply-meta-x: 1\n---\n# 問い\n\nBROKEN\n'
SHOWN_FIELDS = {'schema', 'event', 'ts', 'run_id', 'seq', 'kind', 'format', 'reason', 'draft_chars', 'shown_chars',
                'shown_bytes', 'shown_tokens_est', 'md_tokens_est', 'page_bytes', 'method'}


def est(text):
    """chars-est/1 の見積もり (ASCII は 4 文字で 1 トークン、ほかは 1 文字 1 トークン)。"""
    ascii_count = sum(1 for ch in text if ord(ch) < 128)
    return (ascii_count + 3) // 4 + (len(text) - ascii_count)


def normalize(text):
    return text.replace('\r\n', '\n').replace('\r', '\n')


def draft_hash(text):
    """返信の照合に使う draft_hash (reply-meta- の行を除いた \\n の UTF-8 の sha256)。"""
    lines = normalize(text).split('\n')
    if lines and lines[0].strip() == '---':
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == '---'), None)
        if end is not None:
            lines = [lines[0], *[line for line in lines[1:end] if not line.startswith('reply-meta-')],
                     *lines[end:]]
    return hashlib.sha256('\n'.join(lines).encode('utf-8')).hexdigest()


def meta_lines(text):
    """frontmatter の中の reply-meta- の行 (現れた順)。"""
    lines = normalize(text).split('\n')
    if not lines or lines[0].strip() != '---':
        return []
    found = []
    for line in lines[1:]:
        if line.strip() == '---':
            break
        if line.startswith('reply-meta-'):
            found.append(line)
    return found


def frontmatter_meta(text):
    meta = {}
    for line in meta_lines(text):
        name, _, value = line[len('reply-meta-'):].partition(':')
        meta[name] = value.strip()
    return meta


def split_show(out):
    """(show: より前の行, show: の行のあとの本文)。show: の行が無ければ (全部の行, None)。"""
    lines = out.split('\n')
    for i, line in enumerate(lines):
        if line == 'show:':
            return lines[:i], '\n'.join(lines[i + 1:])
    return lines, None


ANSWER_TEXT = 'answer --text "..."'
REVISE_TEXT = 'answer --revise --text "..."'




class PresentTest(unittest.TestCase):
    def setUp(self):
        # tempfile の 0o700 は権限を絞った環境で読めなくなるので、既定の権限で作る。
        self.dir = os.path.join(tempfile.gettempdir(), f'present-{uuid.uuid4().hex}')
        os.mkdir(self.dir)
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.repo = os.path.join(self.dir, 'repo')
        self.pbi = os.path.join(self.repo, 'docs', 'pbi')
        self.home = os.path.join(self.dir, 'home')
        self.user = os.path.join(self.dir, 'user')
        self.skills = os.path.join(self.dir, 'skills')
        self.nodebin = os.path.join(self.dir, 'nodebin')
        self.empty = os.path.join(self.dir, 'empty')
        self.pages = os.path.join(self.dir, 'pages')
        self.am_log = os.path.join(self.dir, 'am-calls.jsonl')
        self.marker = os.path.join(self.dir, 'node-launched')
        for path in (self.pbi, self.home, self.user, self.skills, self.nodebin, self.empty):
            os.makedirs(path)
        fakes.install_am(self.skills)
        fakes.install_node(self.nodebin)

    # ---- 一時ディレクトリの道具 ----

    def env(self, no_node=False, **extra):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith('FAKE_') and key.upper() not in ('ATELIER_SKILLS_DIR', 'ATELIER_HOME')}
        for key in [key for key in env if key.upper() == 'PATH']:
            del env[key]
        env['PATH'] = self.empty if no_node else self.nodebin
        for key in ('HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'XDG_CONFIG_HOME'):
            env[key] = self.user
        env['HARNESS_LOG_CONFIG'] = os.path.join(self.dir, 'no-such-log-config.json')
        env['ATELIER_EVENTS_SETTLE'] = 'off'
        env['FAKE_AM_LOG'] = self.am_log
        env['FAKE_AM_PAGES'] = self.pages
        env['FAKE_NODE_MARKER'] = self.marker
        env.update(extra)
        return env

    def write(self, name, text):
        path = os.path.join(self.pbi, 'notes', name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Path(path).write_bytes(text.encode('utf-8'))
        return path

    def confirm(self, value):
        with open(os.path.join(self.home, 'agents.json'), 'w', encoding='utf-8', newline='\n') as stream:
            json.dump({'adapters': {'confirm': value}}, stream, ensure_ascii=False)

    def use_record(self):
        """design-record.json (と、それが指す notes) を docs/pbi に置く。"""
        shutil.copytree(os.path.join(DESIGN_FIXTURES, 'pbi'), self.pbi, dirs_exist_ok=True)

    def run_json(self, kind, seq, check=None, relaunch=None):
        question = None if kind == 'report' else {
            'kind': kind, 'path': f'docs/pbi/notes/{kind}.md', 'summary': 's', 'check': check,
            'state': 'docs/pbi/CONTEXT_STATE.md', 'seq': seq, 'episode': 'design', 'wave': None,
            'pbis': None, 'sent_by': None, 'relaunch': relaunch}
        run = {'schema': 'atelier.run/1', 'run_id': 'run-1234', 'seq': seq,
               'state': 'running' if kind == 'report' else 'question', 'question': question}
        self.write_run(json.dumps(run, ensure_ascii=False))

    def write_run(self, text):
        path = os.path.join(self.pbi, 'conductor', 'run.json')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)

    def saved(self, seq, kind, run8='run-1234'):
        return os.path.join(self.pbi, 'conductor', 'present', f'{run8}-{seq}-{kind}.md')

    def read_saved(self, seq, kind, run8='run-1234'):
        return Path(self.saved(seq, kind, run8)).read_bytes().decode('utf-8')

    def shown_rows(self):
        path = os.path.join(self.pbi, 'presentations.jsonl')
        if not os.path.isfile(path):
            return []
        with open(path, encoding='utf-8') as stream:
            rows = [json.loads(line) for line in stream if line.strip()]
        return [row for row in rows if row.get('event') == 'shown']

    def am_calls(self):
        if not os.path.isfile(self.am_log):
            return []
        with open(self.am_log, encoding='utf-8') as stream:
            return [json.loads(line) for line in stream if line.strip()]

    def command(self, rest):
        """present が出す conductor のコマンド。パスは present と同じ引用 (common.quoted)、作業のディレクトリは --dir で渡る。"""
        sub, _, tail = rest.partition(' ')
        return (f'python {common.quoted(CONDUCTOR)} {sub} --dir {common.quoted(self.pbi)}'
                + (f' {tail}' if tail else ''))

    def present(self, path, kind, *extra, env=None, skills=True):
        cmd = [sys.executable, SCRIPT, str(path), '--kind', kind, '--dir', self.pbi, '--home', self.home]
        if skills:
            cmd += ['--skills-dir', self.skills]
        cmd += list(extra)
        result = subprocess.run(cmd, cwd=self.repo, capture_output=True, text=True, encoding='utf-8',
                                errors='replace', env=env or self.env(), timeout=120, creationflags=NO_WINDOW)
        return result.returncode, normalize(result.stdout), normalize(result.stderr)

    def design_record_markdown(self, draft):
        result = subprocess.run([sys.executable, DESIGN_RECORD, 'markdown', '--draft', draft,
                                 '--dir', self.pbi], cwd=self.repo, capture_output=True,
                                env=self.env(), timeout=120, creationflags=NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
        return result.stdout

    def assert_log_row(self, row, **expected):
        for name, value in expected.items():
            with self.subTest(field=name):
                self.assertEqual(row.get(name), value, row)

    def then_lines(self, head):
        return [line for line in head if line.startswith('then: ')]

    def assert_no_record(self):
        self.assertEqual(self.shown_rows(), [])
        self.assertFalse(os.path.exists(os.path.join(self.pbi, 'presentations.jsonl')))

    # ---- html ----

    def test_html_decision_question(self):
        self.confirm('html')
        self.run_json('decision', 3)
        question = self.write('decision.md', QUESTION)
        code, out, err = self.present(question, 'decision')
        self.assertEqual(code, 0, out + err)
        head, body = split_show(out)
        link = body.rstrip('\n')
        self.assertEqual(head, ['format: html (setting)',
                                f'log: {Path(self.pbi).as_posix()}/presentations.jsonl',
                                f'page: {Path(link).as_posix()}',
                                f'then: {self.command(ANSWER_TEXT)}  # answer',
                                f'then: {self.command("answer --reply <file>")}  # reply'])
        self.assertEqual(body, link + '\n')
        self.assertEqual(len(link.split('\n')), 1, body)
        self.assertTrue(os.path.isfile(link), (link, out, err))

        saved = self.saved(3, 'decision')
        self.assertTrue(os.path.isfile(saved), saved)
        calls = self.am_calls()
        self.assertEqual(calls, [['render', saved, '--format', 'html'], ['render', saved, '--format', 'md']])

    def test_html_non_approve_draft_carries_the_four_items(self):
        self.confirm('html')
        self.run_json('decision', 3)
        question = self.write('decision.md', QUESTION)
        code, out, err = self.present(question, 'decision')
        self.assertEqual(code, 0, out + err)
        text = self.read_saved(3, 'decision')
        self.assertEqual(frontmatter_meta(text), {'run_id': 'run-1234', 'seq': '3', 'kind': 'decision',
                                                  'draft_hash': draft_hash(text)})
        self.assertEqual([line.split(':', 1)[0] for line in meta_lines(text)],
                         ['reply-meta-run_id', 'reply-meta-seq', 'reply-meta-kind', 'reply-meta-draft_hash'])
        for name in ('design_id', 'revision', 'record_hash'):
            self.assertNotIn(f'reply-meta-{name}', text)
        self.assertEqual(text, design_record.question_meta(QUESTION, 'run-1234', 3, 'decision'))

    def test_approve_with_check_keeps_the_design_record_output(self):
        self.use_record()
        self.confirm('html')
        self.run_json('approve', 2, check='design-record')
        draft = self.write('approve.md', Path(DESIGN_FIXTURES, 'approve-good.md').read_text(encoding='utf-8'))
        expected = self.design_record_markdown(draft)

        code, out, err = self.present(draft, 'approve')
        self.assertEqual(code, 0, out + err)
        head, body = split_show(out)
        self.assertEqual(head[0], 'format: html (setting)')
        self.assertEqual(self.then_lines(head), [
            f'then: {self.command("answer --reply <file>")}  # reply',
            f'then: {self.command("answer --approve [--choices 1=a,2=b]")}  # approve',
            f'then: {self.command(REVISE_TEXT)}  # revise'])
        self.assertTrue(os.path.isfile(body.strip()), (body, err))
        saved_bytes = Path(self.saved(2, 'approve')).read_bytes()
        self.assertEqual(saved_bytes, expected)
        self.assertEqual([line.split(':', 1)[0] for line in meta_lines(saved_bytes.decode('utf-8'))],
                         ['reply-meta-design_id', 'reply-meta-revision', 'reply-meta-record_hash',
                          'reply-meta-draft_hash'])
        rows = self.shown_rows()
        self.assertEqual(len(rows), 1, rows)
        self.assert_log_row(rows[0], kind='approve', seq=2, run_id='run-1234', format='html', reason='setting')

    def test_approve_without_check_gets_approve_revise_and_answer_lines(self):
        self.confirm('html')
        draft = self.write('approve.md', APPROVE)
        code, out, err = self.present(draft, 'approve')
        self.assertEqual(code, 0, out + err)
        head, _ = split_show(out)
        self.assertEqual(self.then_lines(head), [
            f'then: {self.command("answer --approve [--choices 1=a,2=b]")}  # approve',
            f'then: {self.command(REVISE_TEXT)}  # revise',
            f'then: {self.command(ANSWER_TEXT)}  # answer'])
        self.assertEqual(self.read_saved(0, 'approve', 'inline'), APPROVE)

    def test_approve_with_no_or_broken_design_record_shows_the_body_and_exits_3(self):
        self.confirm('html')
        self.run_json('approve', 2, check='design-record')
        draft = self.write('approve.md', APPROVE)
        with self.subTest(case='no design-record.json'):
            code, out, err = self.present(draft, 'approve')
            self.assertEqual(code, 3, out + err)
            head, body = split_show(out)
            self.assertTrue(head[0].startswith('error: '), head)
            self.assertIn('design-record.json', head[0])
            self.assertEqual(head[1], 'format: off (draft-error)')
            self.assertEqual(body, APPROVE)
            self.assertEqual(self.read_saved(2, 'approve'), APPROVE)
            rows = self.shown_rows()
            self.assertEqual(len(rows), 1, rows)
            self.assert_log_row(rows[0], kind='approve', seq=2, format='off', reason='draft-error',
                                md_tokens_est=None, page_bytes=None)
            self.assertEqual(self.am_calls(), [])
        with self.subTest(case='a draft the lint refuses'):
            self.use_record()
            bad = Path(DESIGN_FIXTURES, 'draft.md').read_text(encoding='utf-8')
            source = self.write('approve.md', bad)
            code, out, err = self.present(source, 'approve')
            self.assertEqual(code, 3, out + err)
            head, body = split_show(out)
            errors = [line for line in head if line.startswith('error: ')]
            self.assertTrue(errors, head)
            self.assertIn('format: off (draft-error)', head)
            self.assertEqual(body, normalize(bad) if normalize(bad).endswith('\n') else normalize(bad) + '\n')
            self.assertNotIn('reply-meta-', body)
            self.assertEqual(len(self.shown_rows()), 2)
            self.assert_log_row(self.shown_rows()[-1], format='off', reason='draft-error')
            self.assertEqual(self.am_calls(), [])

    # ---- md ----

    def test_md_question(self):
        self.run_json('decision', 4)
        question = self.write('decision.md', QUESTION)
        for how in ('--format', 'confirm'):
            with self.subTest(how=how):
                if how == 'confirm':
                    self.confirm('md')
                    extra = ()
                else:
                    extra = ('--format', 'md')
                for stale in (self.am_log, os.path.join(self.pbi, 'presentations.jsonl')):
                    if os.path.exists(stale):
                        os.remove(stale)
                code, out, err = self.present(question, 'decision', *extra)
                self.assertEqual(code, 0, out + err)
                head, body = split_show(out)
                self.assertEqual(head[0], 'format: md (setting)')
                self.assertTrue(body.startswith('# md'), body)
                self.assertIn('このまま進めますか。', body)
                self.assertNotIn('reply-meta-', body)
                saved = self.saved(4, 'decision')
                self.assertEqual(self.read_saved(4, 'decision'), QUESTION)
                self.assertEqual(self.am_calls(), [['render', saved, '--format', 'md']])
                rows = self.shown_rows()
                self.assertEqual(len(rows), 1, rows)
                self.assert_log_row(rows[0], format='md', reason='setting', page_bytes=None,
                                    md_tokens_est=est(body), shown_tokens_est=est(body))

    # ---- off ----

    def test_off_question(self):
        self.confirm('html')
        self.run_json('decision', 5)
        question = self.write('decision.md', QUESTION)
        code, out, err = self.present(question, 'decision', '--format', 'off')
        self.assertEqual(code, 0, out + err)
        head, body = split_show(out)
        self.assertEqual(head[0], 'format: off (setting)')
        self.assertEqual(body, QUESTION)
        self.assertEqual(self.read_saved(5, 'decision'), QUESTION)
        self.assertNotIn('reply-meta-', self.read_saved(5, 'decision'))
        self.assertEqual(self.am_calls(), [])
        self.assertFalse(os.path.exists(self.marker), 'node was launched')
        rows = self.shown_rows()
        self.assertEqual(len(rows), 1, rows)
        self.assert_log_row(rows[0], format='off', reason='setting', md_tokens_est=None, page_bytes=None,
                            shown_chars=len(QUESTION), shown_bytes=len(QUESTION.encode('utf-8')),
                            shown_tokens_est=est(QUESTION))

    # ---- shown row ----

    def test_shown_row_fields(self):
        self.run_json('decision', 3)
        question = self.write('decision.md', QUESTION)
        with self.subTest(format='html'):
            code, out, err = self.present(question, 'decision', '--format', 'html')
            self.assertEqual(code, 0, out + err)
            _, body = split_show(out)
            link = body.strip()
            saved_text = self.read_saved(3, 'decision')
            fake_md = '# md' + '\n'.join(line for line in saved_text.split('\n') if not line.startswith('reply-meta-'))
            row = self.shown_rows()[-1]
            self.assertEqual(set(row), SHOWN_FIELDS)
            self.assert_log_row(row, schema='atelier.presentation/1', event='shown', method='chars-est/1',
                                run_id='run-1234', seq=3, kind='decision', format='html', reason='setting',
                                draft_chars=len(QUESTION), shown_chars=len(link),
                                shown_bytes=len(link.encode('utf-8')), shown_tokens_est=est(link),
                                md_tokens_est=est(fake_md), page_bytes=os.path.getsize(link))
            self.assertIsInstance(row['ts'], str)
        for fmt in ('md', 'off'):
            with self.subTest(format=fmt):
                code, out, err = self.present(question, 'decision', '--format', fmt)
                self.assertEqual(code, 0, out + err)
                _, body = split_show(out)
                row = self.shown_rows()[-1]
                self.assertEqual(set(row), SHOWN_FIELDS)
                self.assert_log_row(row, event='shown', run_id='run-1234', seq=3, kind='decision', format=fmt,
                                    page_bytes=None, draft_chars=len(QUESTION), shown_chars=len(body),
                                    shown_bytes=len(body.encode('utf-8')), shown_tokens_est=est(body),
                                    md_tokens_est=est(body) if fmt == 'md' else None)
        self.assertEqual(len(self.shown_rows()), 3)

    def test_tokens_est_is_chars_est_1(self):
        for text, expected in (('', 0), ('abcd', 1), ('abcde', 2), ('abcdefgh', 2), ('あ', 1), ('あいう', 3),
                               ('ab あ', 2), ('abcde あい', 4), ('あいうえお', 5)):
            with self.subTest(text=text):
                self.assertEqual(present.tokens_est(text), expected)

    # ---- am と node が無いとき ----

    def test_missing_am_falls_back_to_off_without_calling_am(self):
        shutil.rmtree(os.path.join(self.skills, 'answer-me-with-any'))
        self.run_json('decision', 6)
        question = self.write('decision.md', QUESTION)
        for extra in ((), ('--format', 'html')):
            with self.subTest(extra=extra):
                self.confirm('html')
                code, out, err = self.present(question, 'decision', *extra)
                self.assertEqual(code, 0, out + err)
                head, body = split_show(out)
                self.assertEqual(head[0], 'format: off (missing-am)')
                self.assertEqual(body, QUESTION)
                self.assert_log_row(self.shown_rows()[-1], format='off', reason='missing-am',
                                    md_tokens_est=None, page_bytes=None)
                self.assertEqual(self.am_calls(), [])
                self.assertFalse(os.path.exists(self.marker), 'node was launched')
        self.assertEqual(len(self.shown_rows()), 2)

    def test_skills_dir_from_the_environment(self):
        self.confirm('html')
        self.run_json('decision', 6)
        question = self.write('decision.md', QUESTION)
        with self.subTest(case='ATELIER_SKILLS_DIR with the am'):
            code, out, err = self.present(question, 'decision', env=self.env(ATELIER_SKILLS_DIR=self.skills),
                                          skills=False)
            self.assertEqual(code, 0, out + err)
            self.assertEqual(split_show(out)[0][0], 'format: html (setting)')
        with self.subTest(case='no skills dir at all reads the (empty) test home, not the real one'):
            code, out, err = self.present(question, 'decision', skills=False)
            self.assertEqual(code, 0, out + err)
            self.assertEqual(split_show(out)[0][0], 'format: off (missing-am)')

    def test_missing_or_old_node_falls_back_to_off(self):
        self.confirm('html')
        self.run_json('decision', 7)
        question = self.write('decision.md', QUESTION)
        cases = (('no node', self.env(no_node=True), False),
                 ('node 18', self.env(FAKE_NODE_VERSION='v18.19.0'), True),
                 ('unreadable version', self.env(FAKE_NODE_VERSION='junk'), True),
                 ('node --version fails', self.env(FAKE_NODE_EXIT='3'), True))
        for name, env, launched in cases:
            with self.subTest(case=name):
                if os.path.exists(self.marker):
                    os.remove(self.marker)
                code, out, err = self.present(question, 'decision', env=env)
                self.assertEqual(code, 0, out + err)
                head, body = split_show(out)
                self.assertEqual(head[0], 'format: off (missing-node)')
                self.assertEqual(body, QUESTION)
                self.assertEqual(os.path.exists(self.marker), launched)
                self.assertEqual(self.am_calls(), [])
                self.assert_log_row(self.shown_rows()[-1], format='off', reason='missing-node')
        self.assertEqual(len(self.shown_rows()), len(cases))

    def test_old_name_am_warns(self):
        self.confirm('html')
        self.run_json('decision', 8)
        question = self.write('decision.md', QUESTION)
        warn = f'warn: confirm: old name {OLD_NAME}, install answer-me-with-any and remove it'
        shutil.rmtree(os.path.join(self.skills, 'answer-me-with-any'))
        old_am = fakes.install_am(self.skills, OLD_NAME)
        with self.subTest(case='only the old name'):
            code, out, err = self.present(question, 'decision')
            self.assertEqual(code, 0, out + err)
            head, _ = split_show(out)
            self.assertEqual(head[:2], ['format: html (setting)', warn])
            self.assertEqual(len(self.am_calls()), 2)
        with self.subTest(case='both names: the new one runs, no warn'):
            Path(old_am).write_text("import sys\nsys.stderr.write('✗ the old am ran\\n')\nsys.exit(1)\n",
                                    encoding='utf-8')
            fakes.install_am(self.skills)
            code, out, err = self.present(question, 'decision')
            self.assertEqual(code, 0, out + err)
            head, _ = split_show(out)
            self.assertEqual(head[0], 'format: html (setting)')
            self.assertFalse([line for line in head if line.startswith('warn: ')], head)
            self.assertNotIn('the old am ran', out)

    def test_confirm_auto_and_skill_name_are_read_as_html(self):
        self.run_json('decision', 8)
        question = self.write('decision.md', QUESTION)
        for value in ('auto', 'answer-me-with-any'):
            with self.subTest(value=value):
                self.confirm(value)
                code, out, err = self.present(question, 'decision')
                self.assertEqual(code, 0, out + err)
                head, _ = split_show(out)
                self.assertEqual(head[:2], ['format: html (setting)',
                                            f'warn: adapters.confirm: {value} is read as html'])
        with self.subTest(case='--format off beats the file'):
            self.confirm('auto')
            code, out, err = self.present(question, 'decision', '--format', 'off')
            self.assertEqual(code, 0, out + err)
            head, _ = split_show(out)
            self.assertEqual(head[0], 'format: off (setting)')
            self.assertFalse([line for line in head if line.startswith('warn: ')], head)

    def test_am_without_format_option_warns_and_shows_the_file(self):
        self.confirm('md')
        self.run_json('decision', 8)
        question = self.write('decision.md', QUESTION)
        code, out, err = self.present(question, 'decision', env=self.env(FAKE_AM_NO_FORMAT='1'))
        self.assertEqual(code, 0, out + err)
        head, body = split_show(out)
        self.assertEqual(head[0], 'format: off (missing-am)')
        self.assertIn('warn: am: --format md not supported; showing the file as it is', head)
        self.assertEqual(body, QUESTION)
        rows = self.shown_rows()
        self.assertEqual(len(rows), 1, rows)
        self.assert_log_row(rows[0], format='off', reason='missing-am', md_tokens_est=None, page_bytes=None)
        self.assertEqual(len(self.am_calls()), 1, self.am_calls())

    def test_html_retries_without_format_for_an_am_that_does_not_know_it(self):
        self.confirm('html')
        self.run_json('decision', 8)
        question = self.write('decision.md', QUESTION)
        code, out, err = self.present(question, 'decision', env=self.env(FAKE_AM_NO_FORMAT='1'))
        self.assertEqual(code, 0, out + err)
        head, body = split_show(out)
        self.assertEqual(head[0], 'format: html (setting)')
        self.assertTrue(os.path.isfile(body.strip()), (body, err))
        saved = self.saved(8, 'decision')
        self.assertEqual(self.am_calls()[:2], [['render', saved, '--format', 'html'], ['render', saved]])

    # ---- am が原稿を拒む・想定外に終わる ----

    def test_am_rejects_the_draft_exit_3(self):
        self.confirm('html')
        self.run_json('decision', 9)
        question = self.write('decision.md', BROKEN_SOURCE)
        code, out, err = self.present(question, 'decision')
        self.assertEqual(code, 3, out + err)
        head, body = split_show(out)
        self.assertEqual(head[:3], ['error: ✗ L1 [ask] broken',
                                    'error: ✗ 1 errors in the draft; no page was written',
                                    'format: off (draft-error)'])
        self.assertIn('BROKEN', body)
        self.assertNotIn('reply-meta-', body)
        self.assertEqual(self.read_saved(9, 'decision'), BROKEN_SOURCE)
        rows = self.shown_rows()
        self.assertEqual(len(rows), 1, rows)
        self.assert_log_row(rows[0], format='off', reason='draft-error', md_tokens_est=None, page_bytes=None,
                            shown_chars=len(body), draft_chars=len(BROKEN_SOURCE))
        self.assertNotIn('Traceback', out + err)

    def test_am_unexpected_exit_is_an_am_error(self):
        self.run_json('decision', 9)
        question = self.write('decision.md', QUESTION)
        for fmt in ('html', 'md'):
            with self.subTest(format=fmt):
                code, out, err = self.present(question, 'decision', '--format', fmt, env=self.env(FAKE_AM_EXIT='5'))
                self.assertEqual(code, 3, out + err)
                head, body = split_show(out)
                self.assertEqual(head[:2], ['error: ✗ fake am forced exit 5', 'format: off (am-error)'])
                self.assertEqual(body, QUESTION)
                self.assertEqual(self.read_saved(9, 'decision'), QUESTION)
                self.assert_log_row(self.shown_rows()[-1], format='off', reason='am-error', md_tokens_est=None,
                                    page_bytes=None)
        self.assertEqual(len(self.shown_rows()), 2)

    # ---- then の行 ----

    def test_full_run_then_lines(self):
        self.confirm('html')
        self.run_json('full-run', 2)
        question = self.write('full-run.md', QUESTION)
        code, out, err = self.present(question, 'full-run')
        self.assertEqual(code, 0, out + err)
        head, _ = split_show(out)
        self.assertEqual(self.then_lines(head), [f'then: {self.command("answer --full-run yes")}  # yes',
                                                 f'then: {self.command("answer --full-run no")}  # no'])

    def test_runner_questions_and_relaunch(self):
        self.confirm('html')
        relaunch = 'python tools/relaunch.py --resume'
        abort = f'then: {self.command("abort")}  # abort'
        answer = f'then: {self.command(ANSWER_TEXT)}  # answer'
        reply = f'then: {self.command("answer --reply <file>")}  # reply'
        cases = [('handoff-loop', relaunch, [answer, f'then: {relaunch}  # relaunch', abort]),
                 ('launch-failure', relaunch, [answer, f'then: {relaunch}  # relaunch', abort]),
                 ('handoff-loop', None, [answer, abort]),
                 ('decision', relaunch, [answer, reply, f'then: {relaunch}  # relaunch', abort])]
        for kind, relaunch_value, expected in cases:
            with self.subTest(kind=kind, relaunch=relaunch_value):
                self.run_json(kind, 2, relaunch=relaunch_value)
                question = self.write(f'{kind}.md', QUESTION)
                code, out, err = self.present(question, kind)
                self.assertEqual(code, 0, out + err)
                head, _ = split_show(out)
                self.assertEqual(self.then_lines(head), expected)

    # ---- 完了報告 ----

    def test_report_uses_the_doc_template_and_has_no_check_info(self):
        self.confirm('html')
        self.run_json('report', 9)
        report = self.write('report.md', REPORT)
        code, out, err = self.present(report, 'report')
        self.assertEqual(code, 0, out + err)
        head, body = split_show(out)
        self.assertEqual(head[0], 'format: html (setting)')
        self.assertEqual(self.then_lines(head), [])
        self.assertTrue(os.path.isfile(body.strip()), (body, err))
        saved = self.saved(9, 'report')
        self.assertEqual(Path(saved).read_bytes().decode('utf-8'), REPORT)
        self.assertNotIn('reply-meta-', REPORT)
        self.assertEqual(self.am_calls(), [['render', saved, '--template', 'doc', '--format', 'html'],
                                           ['render', saved, '--template', 'doc', '--format', 'md']])
        rows = self.shown_rows()
        self.assertEqual(len(rows), 1, rows)
        self.assert_log_row(rows[0], kind='report', seq=9, run_id='run-1234', format='html', reason='setting')

    # ---- run.json の扱い ----

    def test_without_run_json_the_question_is_inline(self):
        self.confirm('html')
        question = self.write('decision.md', QUESTION)
        code, out, err = self.present(question, 'decision')
        self.assertEqual(code, 0, out + err)
        head, _ = split_show(out)
        self.assertEqual(head[0], 'format: html (setting)')
        self.assertEqual(self.read_saved(0, 'decision', 'inline'), QUESTION)
        self.assertTrue(os.path.isfile(self.saved(0, 'decision', 'inline')))
        rows = self.shown_rows()
        self.assertEqual(len(rows), 1, rows)
        self.assert_log_row(rows[0], run_id=None, seq=0, kind='decision', format='html')

    def test_a_path_other_than_the_waiting_question_exits_2(self):
        self.confirm('html')
        self.run_json('decision', 3)
        other = self.write('other.md', QUESTION)
        code, out, err = self.present(other, 'decision')
        self.assertEqual(code, 2, out + err)
        self.assertIn('is not the waiting question', out)
        self.assertIn('docs/pbi/notes/decision.md', out)
        self.assertNotIn('Traceback', out + err)
        self.assert_no_record()
        self.assertFalse(os.path.exists(os.path.join(self.pbi, 'conductor', 'present')))
        self.assertEqual(self.am_calls(), [])

    def test_report_while_a_question_waits_exits_2(self):
        self.confirm('html')
        self.run_json('decision', 3)
        report = self.write('report.md', REPORT)
        code, out, err = self.present(report, 'report')
        self.assertEqual(code, 2, out + err)
        self.assertIn('a question is waiting', out)
        self.assertNotIn('Traceback', out + err)
        self.assert_no_record()
        self.assertFalse(os.path.exists(os.path.join(self.pbi, 'conductor', 'present')))
        self.assertEqual(self.am_calls(), [])

    def test_usage_errors_exit_2_and_write_no_record(self):
        self.confirm('html')
        question = self.write('decision.md', QUESTION)
        with self.subTest(case='broken run.json'):
            self.write_run('{not json')
            code, out, err = self.present(question, 'decision')
            self.assertEqual(code, 2, out + err)
            self.assertTrue(any(line.startswith('error: ') for line in out.split('\n')), out)
            self.assertNotIn('Traceback', out + err)
        with self.subTest(case='--kind differs from the waiting question'):
            self.run_json('decision', 3)
            code, out, err = self.present(question, 'send-approval')
            self.assertEqual(code, 2, out + err)
            self.assertIn('error: ', out)
        with self.subTest(case='non-report kind with no waiting question'):
            self.write_run(json.dumps({'schema': 'atelier.run/1', 'run_id': 'run-1234', 'seq': 3,
                                       'state': 'running', 'question': None}))
            code, out, err = self.present(question, 'decision')
            self.assertEqual(code, 2, out + err)
            self.assertIn('error: ', out)
        with self.subTest(case='source file missing'):
            self.run_json('decision', 3)
            code, out, err = self.present(os.path.join(self.pbi, 'notes', 'nope.md'), 'decision')
            self.assertEqual(code, 2, out + err)
            self.assertIn('no such file', out)
        self.assert_no_record()
        self.assertFalse(os.path.exists(os.path.join(self.pbi, 'conductor', 'present')))
        self.assertEqual(self.am_calls(), [])

    # ---- shown / saved_draft / log_answered ----

    def test_shown_saved_draft_and_log_answered(self):
        self.confirm('html')
        self.run_json('decision', 3)
        question = self.write('decision.md', QUESTION)
        self.assertFalse(present.shown(self.pbi, 'run-1234', 3), 'no log yet')
        code, out, err = self.present(question, 'decision')
        self.assertEqual(code, 0, out + err)
        with self.subTest(function='shown'):
            self.assertTrue(present.shown(self.pbi, 'run-1234', 3))
            self.assertFalse(present.shown(self.pbi, 'run-1234', 4))
            self.assertFalse(present.shown(self.pbi, 'run-9999', 3))
            self.assertFalse(present.shown(os.path.join(self.dir, 'no-such-dir'), 'run-1234', 3))
        with self.subTest(function='saved_draft'):
            self.assertEqual(Path(present.saved_draft(self.pbi, 3, 'decision', 'run-1234')),
                             Path(self.saved(3, 'decision')))
            self.assertIsNone(present.saved_draft(self.pbi, 3, 'decision', 'run-9999'))
            self.assertIsNone(present.saved_draft(self.pbi, 4, 'decision', 'run-1234'))
            self.assertIsNone(present.saved_draft(self.pbi, 3, 'approve', 'run-1234'))
        with self.subTest(function='log_answered'):
            reply = 'はい、進めます ok'
            for check in ('ok', 'mismatch', 'none'):
                present.log_answered(self.pbi, 'run-1234', 3, 'decision', reply, check)
            with open(os.path.join(self.pbi, 'presentations.jsonl'), encoding='utf-8') as stream:
                rows = [json.loads(line) for line in stream if line.strip()]
            answered = [row for row in rows if row['event'] == 'answered']
            self.assertEqual([row['check'] for row in answered], ['ok', 'mismatch', 'none'])
            for row in answered:
                self.assert_log_row(row, schema='atelier.presentation/1', event='answered', method='chars-est/1',
                                    run_id='run-1234', seq=3, kind='decision', reply_chars=len(reply),
                                    reply_bytes=len(reply.encode('utf-8')), reply_tokens_est=est(reply))
                self.assertIsInstance(row['ts'], str)
            self.assertEqual(len(self.shown_rows()), 1, 'answered rows are not shown rows')

    def test_inline_shown_and_saved_draft(self):
        self.confirm('off')
        question = self.write('decision.md', QUESTION)
        code, out, err = self.present(question, 'decision')
        self.assertEqual(code, 0, out + err)
        self.assertTrue(present.shown(self.pbi, None, 0))
        self.assertEqual(Path(present.saved_draft(self.pbi, 0, 'decision')), Path(self.saved(0, 'decision', 'inline')))

    # ---- import ----

    def test_present_does_not_import_conductor(self):
        result = subprocess.run([sys.executable, '-c', "import present, sys; print('conductor' in sys.modules)"],
                                cwd=HERE, capture_output=True, text=True, timeout=120, creationflags=NO_WINDOW,
                                env=self.env())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'False')

    # ---- 2 つの問いは何も共有しない ----

    def test_two_kinds_share_nothing(self):
        self.confirm('html')
        self.run_json('decision', 1)
        first = self.write('decision.md', QUESTION)
        code, out, err = self.present(first, 'decision')
        self.assertEqual(code, 0, out + err)
        first_saved = Path(self.saved(1, 'decision')).read_bytes()
        self.run_json('send-approval', 2)
        second = self.write('send-approval.md', '# 送ってよいか\n\n本文\n')
        code, out, err = self.present(second, 'send-approval')
        self.assertEqual(code, 0, out + err)
        rows = self.shown_rows()
        self.assertEqual([(row['seq'], row['kind']) for row in rows], [(1, 'decision'), (2, 'send-approval')])
        self.assertEqual(Path(self.saved(1, 'decision')).read_bytes(), first_saved)
        self.assertTrue(os.path.isfile(self.saved(2, 'send-approval')))
        self.assertEqual(sorted(os.listdir(os.path.join(self.pbi, 'conductor', 'present'))),
                         ['run-1234-1-decision.md', 'run-1234-2-send-approval.md'])


if __name__ == '__main__':
    unittest.main()
