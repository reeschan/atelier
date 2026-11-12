"""conductor.py の流れ・ロック・失敗の扱いを、CLI を subprocess で動かして確かめる。

使う側 (呼び出し側) から見た振る舞いだけを見る。conductor.py は `--dir docs/pbi` 付きで一時ディレクトリを
作業ルートにして動かし、出力の行 (`state: ...`・`Episode: ...`)、終了コード、run.json と依頼文のファイルを見る。
home は一時ディレクトリにし (HOME・USERPROFILE・CLAUDE_CONFIG_DIR・ATELIER_HOME を差し替える)、本物の claude は
起動しない。headless の `run` は偽の設計者 (ATELIER_DESIGNER_CMD) だけで回す。
"""
import datetime
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
CON = os.path.join(HERE, 'conductor.py')
sys.path.insert(0, HERE)
import conductor  # noqa: E402  (無い /proc の道のテストだけ、関数を直接呼ぶ)
DESIGN_RECORD = os.path.join(HERE, 'design_record.py')
STATE = 'docs/pbi/CONTEXT_STATE.md'
RUN = 'docs/pbi/conductor/run.json'
LOCK = 'docs/pbi/conductor/lock'

# 偽の設計者 (docs/pbi/tools/fake_designer.py と同じ動き)。一時ディレクトリに書き出し、ATELIER_DESIGNER_CMD で差す。
# 標準入力の依頼文から Episode・Result・Answer を読み、出番の結果を書いて claude -p の JSON に似た 1 行を出す。
# FAKE_DESIGNER_LOG に引数と API キーが見えたかを追記し、FAKE_DESIGNER_SLEEP 秒待ち、FAKE_IS_ERROR=1 で
# 結果のファイルを書いたまま is_error: true を返す。
FAKE_DESIGNER = r'''"""テスト用の偽の設計者。"""
import json
import os
import re
import sys
import time
from pathlib import Path

prompt = sys.stdin.read()
with open(os.environ['FAKE_DESIGNER_LOG'], 'a', encoding='utf-8') as log:
    log.write(json.dumps({'args': sys.argv[1:], 'api_key_seen': 'ANTHROPIC_API_KEY' in os.environ,
                          'head': prompt.splitlines()[0] if prompt else ''}) + '\n')
time.sleep(float(os.environ.get('FAKE_DESIGNER_SLEEP') or 0))
episode, seq = re.search(r'^Episode: (\w+) \(seq (\d+)\)', prompt, re.M).groups()
result_path = re.search(r'^Result: .*?write (\S+)', prompt, re.M).group(1)
data = {'schema': 'atelier.episode/1', 'episode': episode, 'seq': int(seq),
        'summary': [os.environ.get('FAKE_SUMMARY') or f'fake {episode}']}
if episode == 'design' and not re.search(r'^Answer: ', prompt, re.M):
    state = Path('docs/pbi/CONTEXT_STATE.md')
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text('# state\nImmediate next action: read the answer\n', encoding='utf-8')
    data.update(status='question', state=state.as_posix(),
                question={'kind': 'decision', 'path': 'docs/pbi/notes/q.md', 'summary': 'go?'})
elif episode == 'design':
    Path('docs/pbi/waves.json').write_text(json.dumps({'waves': [{'n': 1, 'pbis': ['PBI-001']}]}), encoding='utf-8')
    data.update(status='done', next={'episode': 'build', 'wave': 1})
elif episode == 'build':
    data.update(status='done', next={'episode': 'finish'})
else:
    data.update(status='done', next=None, report='docs/pbi/report.md')
Path(result_path).parent.mkdir(parents=True, exist_ok=True)
Path(result_path).write_text(json.dumps(data), encoding='utf-8')
is_error = os.environ.get('FAKE_IS_ERROR') == '1'
if 'exec' in sys.argv[1:]:
    # codex exec --json の形: JSONL のイベントと、-o の最後の文
    args = sys.argv[1:]
    Path(args[args.index('-o') + 1]).write_text(f'fake {episode}\n', encoding='utf-8')
    print(json.dumps({'type': 'thread.started', 'thread_id': 'fake-thread'}))
    print(json.dumps({'type': 'turn.started'}))
    if is_error:
        print(json.dumps({'type': 'turn.failed', 'error': {'message': 'usage limit reached'}}))
        sys.exit(1)
    print(json.dumps({'type': 'turn.completed',
                      'usage': {'input_tokens': 100, 'cached_input_tokens': 40, 'output_tokens': 7}}))
    sys.exit(0)
print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': is_error, 'session_id': 'fake-session',
                  'total_cost_usd': 0.25, 'result': 'Credit balance is too low' if is_error else 'fake'}))
'''


def reply_fence(meta):
    """返信の本文 (決定 1 つ) と末尾の am-reply-meta の囲み。"""
    return '1. [Q] Which model?\n   → **a**\n\n```am-reply-meta\n' + json.dumps(meta) + '\n```\n'


def pids(obj):
    """run.json の中の、名前に pid を含むキーの整数の値を全部集める。"""
    found = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if 'pid' in key and isinstance(value, int) and not isinstance(value, bool):
                found.append(value)
            found += pids(value)
    elif isinstance(obj, list):
        for value in obj:
            found += pids(value)
    return found


class ConductorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.join(tempfile.gettempdir(), f'pbi-conductor-{uuid.uuid4().hex}')
        os.mkdir(self.tmp)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # エージェント定義は一時のユーザーディレクトリの .claude/agents に置く (本物の home を読まない)
        self.user = os.path.join(self.tmp, 'user')
        agents = os.path.join(self.user, '.claude', 'agents')
        os.makedirs(agents)
        for role in ('designer', 'reviewer', 'tester'):
            self.write_abs(os.path.join(agents, f'atelier-{role}.md'), f'---\nname: atelier-{role}\n---\ntest\n')
        self.fake = os.path.join(self.tmp, 'fake_designer.py')
        self.write_abs(self.fake, FAKE_DESIGNER)
        self.log = os.path.join(self.tmp, 'fake.log')
        self.home = os.path.join(self.tmp, 'home')
        self.env = dict(os.environ, ATELIER_HOME=self.home, PYTHONIOENCODING='utf-8', PYTHONUTF8='1',
                        HOME=self.user, USERPROFILE=self.user,
                        CLAUDE_CONFIG_DIR=os.path.join(self.user, '.claude'),
                        ATELIER_DESIGNER_CMD=json.dumps([sys.executable, self.fake]),
                        HARNESS_LOG_CONFIG=os.path.join(self.tmp, 'no-harness-log.json'),
                        ATELIER_EVENTS_SETTLE='off',
                        FAKE_DESIGNER_LOG=self.log)
        # codex のホーム: サインインと shared-memory の MCP の設定がある (本物の ~/.codex は読まない)
        self.codex_home = os.path.join(self.tmp, 'codex-home')
        self.write_abs(os.path.join(self.codex_home, 'auth.json'), '{}')
        self.write_abs(os.path.join(self.codex_home, 'config.toml'),
                       '[mcp_servers.shared-memory]\nurl = "http://127.0.0.1:7820/mcp"\n')
        self.env['CODEX_HOME'] = self.codex_home
        self.env.pop('OPENAI_API_KEY', None)
        for name in ('FAKE_DESIGNER_SLEEP', 'FAKE_IS_ERROR', 'HARNESS_LOG_PATH', 'ATELIER_EVENTS_QUIET'):
            self.env.pop(name, None)

    # ---- helpers ----

    def con(self, *args, env=None, timeout=120):
        """conductor.py を 1 回呼ぶ。(終了コード, 標準出力 + 標準エラー) を返す。"""
        full = dict(self.env)
        full.update(env or {})
        try:
            p = subprocess.run([sys.executable, CON, *args, '--dir', 'docs/pbi'], cwd=self.tmp, capture_output=True,
                               text=True, encoding='utf-8', errors='replace', env=full, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, f'timeout: conductor.py {" ".join(args)}'
        return p.returncode, p.stdout + p.stderr

    def path(self, rel):
        return rel if os.path.isabs(rel) else os.path.join(self.tmp, rel)

    def write_abs(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)

    def write(self, rel, text):
        self.write_abs(self.path(rel), text)

    def append(self, rel, text):
        os.makedirs(os.path.dirname(self.path(rel)), exist_ok=True)
        with open(self.path(rel), 'a', encoding='utf-8', newline='\n') as f:
            f.write(text)

    def read(self, rel):
        try:
            with open(self.path(rel), encoding='utf-8') as f:
                return f.read()
        except OSError:
            return ''

    def read_json(self, rel):
        return json.loads(self.read(rel) or '{}')

    def events(self):
        """docs/pbi/events.jsonl の行 (辞書のリスト)。"""
        return [json.loads(line) for line in self.read('docs/pbi/events.jsonl').splitlines() if line.strip()]

    def write_json(self, rel, data):
        self.write(rel, json.dumps(data))

    @staticmethod
    def line(out, key):
        """出力から `<key>: ...` の行の値を取る (無ければ '')。"""
        for line in out.splitlines():
            if line.startswith(key + ':'):
                return line.split(':', 1)[1].strip()
        return ''

    def request(self, out):
        return self.read(self.line(out, 'prompt'))

    def result(self, out, **fields):
        """next の出力から結果のパスと seq を読み、出番の結果を書く。"""
        given = self.line(out, 'result')
        self.assertTrue(given, f'next has no result line: {out}')
        path = self.path(given)
        episode, _, seq = self.line(out, 'episode').partition(' seq ')
        data = {'schema': 'atelier.episode/1', 'episode': episode, 'seq': int(seq or 0), 'summary': ['test']}
        data.update(fields)
        self.write_abs(path, json.dumps(data))

    def run_fake(self, out):
        """依頼文を偽の設計者に食わせ、結果のファイルを書かせる (next で running にした出番)。"""
        with open(self.path(self.line(out, 'prompt')), 'rb') as stream:
            p = subprocess.run([sys.executable, self.fake], stdin=stream, cwd=self.tmp, env=self.env,
                               capture_output=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr.decode('utf-8', errors='replace'))

    def approvals(self):
        return len(self.read('docs/pbi/design-approvals.jsonl').splitlines())

    def setup_approve(self, decisions=None):
        """設計の正本と承認の下書き (docs/pbi/notes/approve.md) を置き、封筒の値を返す。"""
        decisions = decisions or ['1. [Q] Which model?']
        self.write_json('docs/pbi/design-record.json', {
            'schema': 'atelier.design-record/1', 'design_id': 'test', 'revision': 1,
            'targets': ['PBI-001'], 'concerns': []})
        # 承認の原稿の 7 つの節 (appendix/approve.md)。ask の囲みは決めることに、ほかの行は結論に置く
        asks = '\n'.join(decisions) if '```ask' in decisions else 'なし'
        summary = '' if '```ask' in decisions else '\n'.join(decisions)
        self.write('docs/pbi/notes/approve.draft.md', '\n'.join([
            '---', 'title: test', '---', '## 結論', summary, '', '## 今の流れ', '```flow', 'A -> B', '```', '',
            '## この run のあとの流れ', '```flow', '- A -> B', '+ A -> C (PBI-001)', '```', '',
            '## 決めること', asks, '', '## PBI', '| PBI | 何をするか | 終わると何ができるか | 先に要る PBI |',
            '|---|---|---|---|', '| PBI-001 | C を足す | C が使える | なし |', '',
            '## 影響範囲', '点検しなかった (小さな変更のため)。', '',
            '## 作る人と見直し', '| PBI | 作るモデル | 見直しの深さ |', '|---|---|---|', '| PBI-001 | sonnet | ふつう |',
            '']))
        p = subprocess.run([sys.executable, DESIGN_RECORD, 'markdown', '--draft', 'docs/pbi/notes/approve.draft.md',
                            '--dir', 'docs/pbi'], cwd=self.tmp, capture_output=True, env=self.env, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr.decode('utf-8', errors='replace'))
        text = p.stdout.decode('utf-8')
        self.write('docs/pbi/notes/approve.md', text)
        return {line.split(':', 1)[0][len('reply-meta-'):]: line.split(':', 1)[1].strip()
                for line in text.splitlines() if line.startswith('reply-meta-')}

    def ask_approve(self, out, decisions=None):
        """running の出番の結果を承認の問いとして書き、done を呼ぶ。(meta, 終了コード, 出力)。"""
        meta = self.setup_approve(decisions)
        self.result(out, status='question', question={'kind': 'approve', 'path': 'docs/pbi/notes/approve.md',
                                                      'summary': 'approve 1 PBI', 'check': 'design-record'})
        rc, done = self.con('done')
        return meta, rc, done

    def to_build(self):
        """waves.json を置いて design を終え、build 1 の出番の next の出力を返す。"""
        self.write_json('docs/pbi/waves.json', {'waves': [{'n': 1, 'pbis': ['PBI-001']}]})
        rc, out = self.con('start', '--goal', 'goal')
        self.assertEqual(rc, 0, out)
        rc, out = self.con('next')
        self.assertEqual((rc, self.line(out, 'episode')), (0, 'design seq 1'), out)
        self.result(out, status='done', next={'episode': 'build', 'wave': 1})
        rc, out = self.con('done')
        self.assertEqual(rc, 0, out)
        rc, out = self.con('next')
        self.assertEqual(rc, 0, out)
        return out

    def answer_line(self, request):
        return next((line.split(':', 1)[1].split()[0] for line in request.splitlines()
                     if line.startswith('Answer:')), '')

    def sleeping_child(self):
        """まだ生きている子 (止められることを確かめるため)。"""
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
        self.addCleanup(self.stop_process, child)
        return child

    @staticmethod
    def stop_process(child):
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)

    def now_iso(self):
        return datetime.datetime.now().astimezone().isoformat(timespec='seconds')

    def record_child(self, pid):
        """running の出番に、生きた子の pid を書く (起動の時刻の記録は無し = launched_at で比べる)。"""
        run = self.read_json(RUN)
        running = run['running']
        running['child_pid'] = pid
        running.pop('child_pid_started', None)
        running['launched_at'] = self.now_iso()
        self.write_json(RUN, run)

    def headless_running(self, pid):
        """launch が headless の running に、生きた子の pid を書く (ループの外で走っている runner の代わり)。"""
        run = self.read_json(RUN)
        run['state'] = 'running'
        run['running'] = {'seq': 1, 'episode': 'design', 'wave': None, 'pbis': None,
                          'launched_at': self.now_iso(), 'launcher': 'headless', 'session_id': str(uuid.uuid4()),
                          'continue_from': None, 'answer': None,
                          'prompt': 'docs/pbi/conductor/episodes/1-design.prompt.md',
                          'result': 'docs/pbi/conductor/episodes/1-design.result.json',
                          'alive': 'docs/pbi/conductor/episodes/1-design.alive', 'child_pid': pid}
        self.write_json(RUN, run)

    def aside_runs(self):
        """`docs/pbi/conductor/run.<日時>[-n].json` (退けられた run.json) の名前の一覧。"""
        folder = self.path('docs/pbi/conductor')
        return [name for name in os.listdir(folder) if re.match(r'run\.\d{8}-\d{6}(-\d+)?\.json$', name)]

    def write_lock(self, pid, started_at=None):
        self.write(LOCK, json.dumps({'pid': pid, 'host': socket.gethostname(),
                                     'started_at': started_at if started_at is not None else self.now_iso()}))

    @staticmethod
    def dead_pid():
        """終わった子の pid (生死は conductor.py が確かめる)。"""
        child = subprocess.Popen([sys.executable, '-c', 'pass'])
        child.wait()
        return child.pid

    def uuids(self):
        return set(re.findall(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
                              self.read(RUN).lower()))

    # ---- tests ----

    def test_design_question_and_answer_check_reply(self):
        """start → design → question → answer と、check-reply の 0・3・4。"""
        rc, out = self.con('start', '--goal', 'design question')
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: ready'), out)
        rc, out = self.con('next')
        self.assertEqual((rc, self.line(out, 'episode')), (0, 'design seq 1'), out)
        self.assertIn('Episode: design (seq 1)', self.request(out))

        self.run_fake(out)
        rc, out = self.con('done')
        self.assertEqual((rc, out.splitlines()[0]), (10, 'state: question'), out)
        self.assertIn('question: decision', out)
        self.assertTrue(os.path.isfile(self.path(STATE)))

        rc, out = self.con('answer', '--text', 'run it')
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: ready'), out)
        rc, out = self.con('answer', '--text', 'twice')
        self.assertEqual(rc, 2, out)  # 答える問いが無い

        rc, out = self.con('next')  # 出番の途中の問いのあとの design (Continue from と Answer 付き)
        self.assertEqual(self.line(out, 'episode'), 'design seq 2', out)
        meta, rc, out = self.ask_approve(out)
        self.assertEqual((rc, out.splitlines()[0]), (10, 'state: question'), out)
        self.assertIn('question: approve', out)

        # 囲みの無い返信は check-reply 4 (どの版にも結ばない)
        rc, out = self.con('answer', '--text', 'ok')
        self.assertEqual(rc, 10, out)
        self.assertIn('check-reply exit 4', out)
        self.assertIn('answer --approve', out)
        # 古い revision の囲みは check-reply 3 (不一致)
        self.write('docs/pbi/notes/stale.md', reply_fence(dict(meta, revision='9')))
        rc, out = self.con('answer', '--reply', 'docs/pbi/notes/stale.md')
        self.assertEqual(rc, 10, out)
        self.assertIn('check-reply exit 3', out)
        # 問いは残り、承認はまだ記録されない
        rc, out = self.con('next')
        self.assertEqual(rc, 10, out)
        self.assertEqual(self.approvals(), 0)
        # 正しい囲みは check-reply 0
        self.write('docs/pbi/notes/reply.md', reply_fence(meta))
        rc, out = self.con('answer', '--reply', 'docs/pbi/notes/reply.md')
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: ready'), out)
        self.assertEqual(self.approvals(), 1)

    def test_revise_reopens_design(self):
        """`answer --revise --text` で design が Answer 付きで再び起きる。"""
        self.con('start', '--goal', 'revise')
        rc, out = self.con('next')
        meta, rc, out = self.ask_approve(out)
        self.assertEqual(rc, 10, out)

        rc, out = self.con('answer', '--revise', '--text', 'split PBI-006 in two')
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: ready'), out)
        run = self.read_json(RUN)
        self.assertTrue(run['answers'][-1]['revise'])

        rc, out = self.con('next')
        request = self.request(out)
        self.assertEqual(self.line(out, 'episode'), 'design seq 2', out)
        self.assertNotIn('Continue from:', request)
        answer = self.answer_line(request)
        self.assertTrue(answer.startswith('docs/pbi/conductor/answers/'), answer)
        self.assertIn('split PBI-006 in two', self.read(answer))

    def test_approve_with_choices_then_build(self):
        """`answer --approve --choices` が囲みを組み、承認を記録し、build へ進む。"""
        self.con('start', '--goal', 'approve with choices')
        rc, out = self.con('next')
        meta, rc, out = self.ask_approve(out, ['1. [Q] Which model?', '2. [Q] Which runner?'])
        self.assertEqual(rc, 10, out)

        rc, out = self.con('answer', '--approve', '--choices', '1=a,2=b')
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: ready'), out)
        reply = self.read('docs/pbi/conductor/answers/1-approve.md')
        self.assertIn('**a**', reply)
        self.assertIn('**b**', reply)
        self.assertIn('```am-reply-meta', reply)
        self.assertEqual(self.approvals(), 1)
        run = self.read_json(RUN)
        self.assertEqual(run['answers'][-1]['kind'], 'approve')

        rc, out = self.con('next')
        self.assertEqual(self.line(out, 'episode'), 'design seq 2', out)
        self.assertIn('Answer: ', self.request(out))
        self.write_json('docs/pbi/waves.json', {'waves': [{'n': 1, 'pbis': ['PBI-001']}]})
        self.result(out, status='done', next={'episode': 'build', 'wave': 1})
        rc, out = self.con('done')
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: ready'), out)
        rc, out = self.con('next')
        self.assertEqual(self.line(out, 'episode'), 'build seq 3', out)

    def test_handoff_progress_and_same_episode_restart(self):
        """handoff は同じ出番を Continue from 付きで起こし、進みがあれば数えない。"""
        self.write('docs/pbi/STATUS.md', '# status\n')
        self.write('docs/pbi/runs.jsonl', '')
        self.write(STATE, '# state\n')
        out = self.to_build()
        self.assertEqual(self.line(out, 'episode'), 'build seq 2', out)

        self.result(out, status='handoff', state=STATE)
        rc, out = self.con('done')
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: ready'), out)
        self.assertIn('next: build', out)
        rc, out = self.con('next')
        self.assertEqual(self.line(out, 'episode'), 'build seq 3', out)
        self.assertIn(f'Continue from: {STATE}', self.request(out))

        # 進みあり: runs.jsonl の行と STATUS.md が変わる
        self.append('docs/pbi/runs.jsonl', json.dumps({'report': 'PBI-001', 'attempt': 1}) + '\n')
        self.write('docs/pbi/STATUS.md', '# status\nchanged\n')
        self.result(out, status='handoff', state=STATE)
        rc, out = self.con('done')
        self.assertEqual(rc, 0, out)
        self.assertNotIn('handoff-loop', out)
        self.assertEqual(self.read_json(RUN)['handoff']['count'], 0)
        rc, out = self.con('next')
        self.assertEqual(self.line(out, 'episode'), 'build seq 4', out)
        self.assertIn(f'Continue from: {STATE}', self.request(out))

    def test_three_handoffs_without_progress_is_handoff_loop(self):
        """進みの無い handoff が 3 回続くと handoff-loop の問いになる。"""
        self.write('docs/pbi/STATUS.md', '# status\n')
        self.write('docs/pbi/runs.jsonl', '')
        self.write(STATE, '# state\n')
        out = self.to_build()
        # 進みありの handoff を 1 回置いて、進みを測る基準を作る
        self.append('docs/pbi/runs.jsonl', json.dumps({'report': 'PBI-001', 'attempt': 1}) + '\n')
        self.result(out, status='handoff', state=STATE)
        rc, out = self.con('done')
        self.assertEqual(rc, 0, out)
        rc, out = self.con('next')

        for n in (1, 2):
            self.result(out, status='handoff', state=STATE)
            rc, out = self.con('done')
            self.assertEqual(rc, 0, out)
            self.assertNotIn('handoff-loop', out)
            rc, out = self.con('next')
            self.assertEqual(self.line(out, 'episode'), f'build seq {n + 3}', out)

        self.result(out, status='handoff', state=STATE)
        rc, out = self.con('done')
        self.assertEqual((rc, out.splitlines()[0]), (10, 'state: question'), out)
        self.assertIn('handoff-loop', out)
        run = self.read_json(RUN)
        self.assertEqual(run['question']['kind'], 'handoff-loop')
        self.assertTrue(os.path.isfile(self.path(run['question']['path'])))
        rc, out = self.con('answer', '--text', 'go on')
        self.assertEqual(rc, 0, out)

    def test_mid_episode_question_continue_and_answer(self):
        """出番の途中の問い (state 付き) のあとは Continue from と Answer の両方が依頼文に入る。"""
        self.con('start', '--goal', 'mid episode')
        rc, out = self.con('next')

        # state の無い問い (approve 以外) は壊れた結果として失敗に数える
        self.result(out, status='question', question={'kind': 'decision', 'path': 'docs/pbi/notes/q.md',
                                                      'summary': 'run the design?'})
        rc, out = self.con('done')
        self.assertEqual((rc, out.splitlines()[0]), (12, 'state: retry'), out)
        rc, out = self.con('next')
        self.assertEqual(self.line(out, 'episode'), 'design seq 2', out)
        self.assertNotIn('Continue from:', self.request(out))

        self.write(STATE, '# state\nImmediate next action: ask\n')
        self.result(out, status='question', state=STATE,
                    question={'kind': 'decision', 'path': 'docs/pbi/notes/q.md', 'summary': 'run the design?'})
        rc, out = self.con('done')
        self.assertEqual((rc, out.splitlines()[0]), (10, 'state: question'), out)
        self.assertEqual(self.read_json(RUN)['question']['state'], STATE)

        rc, out = self.con('answer', '--text', 'go on')
        self.assertEqual(rc, 0, out)
        rc, out = self.con('next')
        request = self.request(out)
        self.assertEqual(self.line(out, 'episode'), 'design seq 3', out)
        self.assertIn(f'Continue from: {STATE}', request)
        self.assertIn('go on', self.read(self.answer_line(request)))

    def test_no_result_retry(self):
        """結果の無い出番は retry、2 回続けば launch-failure、done (question・次の出番・handoff) と答えで数が戻る。"""
        self.write_json('docs/pbi/waves.json', {'waves': [{'n': 1, 'pbis': ['PBI-001']}]})
        self.con('start', '--goal', 'no result')
        rc, out = self.con('next')
        rc, out = self.con('done')
        self.assertEqual((rc, out.splitlines()[0]), (12, 'state: retry'), out)
        self.assertIn('result missing or broken', out)
        self.assertEqual(self.read_json(RUN)['state'], 'retry')
        self.assertIsNone(self.read_json(RUN)['pending']['continue_from'])  # Continue from 無しで起こし直す
        self.assertEqual(self.read_json(RUN)['failures']['count'], 1)

        broken = {'ATELIER_DESIGNER_CMD': json.dumps([sys.executable, '-c', 'import sys; sys.exit(3)'])}
        rc, out = self.con('run', env=broken)  # 2 回続いた失敗
        self.assertEqual((rc, out.splitlines()[0]), (10, 'state: question'), out)
        self.assertIn('launch-failure', out)
        rc, out = self.con('answer', '--text', 'again')
        self.assertEqual(rc, 0, out)
        self.assertIsNone(self.read_json(RUN).get('failures'))  # どの答えのあとも 0
        rc, out = self.con('run', env=broken)  # 数は答えで 0 に戻っている (1 回目の失敗 = retry)
        self.assertEqual((rc, out.splitlines()[0]), (12, 'state: retry'), out)
        self.assertEqual(self.read_json(RUN)['failures']['count'], 1)

        # 失敗 1 回のあとの question を読む done も 0 に戻す (戻さなければ 1 が残る)
        rc, out = self.con('next')
        self.write(STATE, '# state\n')
        self.result(out, status='question', state=STATE,
                    question={'kind': 'decision', 'path': 'docs/pbi/notes/q.md', 'summary': 'go?'})
        rc, out = self.con('done')
        self.assertEqual((rc, out.splitlines()[0]), (10, 'state: question'), out)
        self.assertIsNone(self.read_json(RUN).get('failures'))
        rc, out = self.con('answer', '--text', 'ok')
        self.assertEqual(rc, 0, out)

        # design を build 1 まで進める
        rc, out = self.con('next')
        self.result(out, status='done', next={'episode': 'build', 'wave': 1})
        rc, out = self.con('done')
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: ready'), out)

        # 同じ出番・波で失敗 1 回のあと、次の出番を渡す done も 0 に戻す
        rc, out = self.con('next')
        rc, out = self.con('done')
        self.assertEqual((rc, out.splitlines()[0]), (12, 'state: retry'), out)
        self.assertEqual(self.read_json(RUN)['failures']['count'], 1)
        rc, out = self.con('next')
        self.result(out, status='done', next={'episode': 'build', 'wave': 1})
        rc, out = self.con('done')
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: ready'), out)
        self.assertIsNone(self.read_json(RUN).get('failures'))
        # 戻っていれば次の失敗は 1 回目 (retry)。戻っていなければ 2 回目 (launch-failure) になる
        rc, out = self.con('next')
        rc, out = self.con('done')
        self.assertEqual((rc, out.splitlines()[0]), (12, 'state: retry'), out)

        # 同じ出番・波で失敗 1 回のあとの handoff も 0 に戻す
        self.write(STATE, '# state\n')
        rc, out = self.con('next')
        self.result(out, status='handoff', state=STATE)
        rc, out = self.con('done')
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: ready'), out)
        self.assertIsNone(self.read_json(RUN).get('failures'))
        rc, out = self.con('next')
        rc, out = self.con('done')
        self.assertEqual((rc, out.splitlines()[0]), (12, 'state: retry'), out)

    def test_rejects_unlinked_next_and_unknown_wave(self):
        """つながりの表に無い next と waves.json に無い波は断り、状態を進めない。"""
        self.write_json('docs/pbi/waves.json', {'waves': [{'n': 1, 'pbis': ['PBI-001']}]})
        self.con('start', '--goal', 'refuse')
        rc, out = self.con('next')
        self.result(out, status='done', next={'episode': 'review', 'wave': 1})
        rc, out = self.con('done')
        self.assertEqual((rc, out.splitlines()[0]), (12, 'state: retry'), out)
        self.assertIn('design -> review is not allowed', out)
        self.assertEqual(self.read_json(RUN)['state'], 'retry')

        rc, out = self.con('next')
        self.result(out, status='done', next={'episode': 'build', 'wave': 1})
        rc, out = self.con('done')
        self.assertEqual(rc, 0, out)
        rc, out = self.con('next')
        self.assertEqual(self.line(out, 'episode'), 'build seq 3', out)

        self.result(out, status='done', next={'episode': 'design'})
        rc, out = self.con('done')
        self.assertEqual((rc, out.splitlines()[0]), (12, 'state: retry'), out)
        self.assertIn('build -> design is not allowed', out)

        # handoff は進みを数え直すので、波の拒否をもう一度試す前に数が戻る
        self.write(STATE, '# state\n')
        rc, out = self.con('next')
        self.result(out, status='handoff', state=STATE)
        rc, out = self.con('done')
        self.assertEqual(rc, 0, out)
        rc, out = self.con('next')
        self.result(out, status='done', next={'episode': 'build', 'wave': 9})
        rc, out = self.con('done')
        self.assertEqual((rc, out.splitlines()[0]), (12, 'state: retry'), out)
        self.assertIn('wave 9 is not in', out)

    def test_next_while_running_gives_no_launch(self):
        """running の間の next は起動の指示を出さず、--relaunch を案内する。"""
        self.con('start', '--goal', 'running')
        rc, out = self.con('next')
        self.assertEqual(rc, 0, out)
        rc, out = self.con('next')
        self.assertEqual(rc, 0, out)
        self.assertTrue(out.startswith('state: running since'), out)
        self.assertEqual(self.line(out, 'launch'), '')
        self.assertIn('next --relaunch', self.line(out, 'relaunch'))
        self.assertIn('episode: design seq 1', out)

    def test_relaunch_new_seq(self):
        """--relaunch は生きた子を止め、新しい seq と session で起こし直す。"""
        self.con('start', '--goal', 'relaunch')
        rc, out = self.con('next')
        old = out
        self.assertEqual(self.line(out, 'episode'), 'design seq 1', old)
        child = self.sleeping_child()
        self.record_child(child.pid)
        before = self.uuids()

        rc, out = self.con('next', '--relaunch')
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: running'), out)
        self.assertEqual(self.line(out, 'episode'), 'design seq 2', out)
        self.assertTrue(self.line(out, 'launch'), out)
        self.assertIsNotNone(child.wait(timeout=10))  # 止められた
        self.assertIn('relaunched', [row['status'] for row in self.read_json(RUN)['history']])
        self.assertTrue(self.uuids() - before)
        launched = out

        # 依頼文の Alive: の行のパスに書いた .alive の古さを status が出す
        request = self.request(out)
        alive_line = next(line for line in request.splitlines() if line.startswith('Alive:'))
        alive = re.search(r'(\S+\.alive)', alive_line).group(1)
        self.assertTrue(alive.replace('\\', '/').endswith('conductor/episodes/2-design.alive'), alive_line)
        old_time = time.time() - 600
        self.write(alive, datetime.datetime.fromtimestamp(old_time).astimezone().isoformat(timespec='seconds') + '\n')
        os.utime(self.path(alive), (old_time, old_time))
        rc, out = self.con('status')
        age = re.search(r'alive\D{0,3}(\d+)', out)
        self.assertEqual(rc, 0, out)
        self.assertIsNotNone(age, out)
        self.assertGreaterEqual(int(age.group(1)), 590)
        rc, out = self.con('next')
        self.assertTrue(out.startswith('state: running since'), out)
        self.assertIn('alive', out)

        # 前の seq の結果が遅れて書かれても、今の seq の結果だけを読む
        self.write_json('docs/pbi/waves.json', {'waves': [{'n': 1, 'pbis': ['PBI-001']}]})
        self.result(old, status='done', next={'episode': 'finish'})
        self.result(launched, status='done', next={'episode': 'build', 'wave': 1})
        rc, out = self.con('done')
        self.assertEqual(rc, 0, out)
        self.assertIn('build', out)
        self.assertNotIn('finish', out)

        # no-agent-tool は数えずに decision の問い (headless を案内する)
        rc, out = self.con('next')
        self.result(out, status='error', reason='no-agent-tool')
        rc, out = self.con('done')
        self.assertEqual((rc, out.splitlines()[0]), (10, 'state: question'), out)
        self.assertIn('decision', out)
        self.assertIn('run --launch headless --detach', out)
        rc, out = self.con('answer', '--text', 'use headless')
        self.assertEqual(rc, 0, out)
        rc, out = self.con('next', '--launch', 'headless')
        self.assertEqual(rc, 2, out)  # next は --launch を取らない

        # headless の running も、子を止めて ready に戻す
        self.con('start', '--goal', 'headless relaunch', '--force', '--launch', 'headless')
        child2 = self.sleeping_child()
        self.headless_running(child2.pid)
        rc, out = self.con('next', '--relaunch')
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: ready'), out)
        self.assertEqual(self.line(out, 'launch'), 'headless')
        self.assertIsNotNone(child2.wait(timeout=10))
        self.assertEqual(self.read_json(RUN)['state'], 'ready')

    def test_lock_live_dead_unreadable(self):
        """死んだロックは引き継ぎ、生きたロック (自分の pid) は断り、started_at が読めなくても生きていれば断る。"""
        gone = self.dead_pid()
        self.write_lock(gone)
        rc, out = self.con('start', '--goal', 'dead lock')
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: ready'), out)
        self.assertFalse(os.path.isfile(self.path(LOCK)))  # 手放したら消える

        self.write_lock(os.getpid())
        rc, out = self.con('start', '--goal', 'live lock')
        self.assertEqual(rc, 2, out)
        self.assertIn('locked by pid', out)

        self.write_lock(os.getpid(), 'not-a-time')
        rc, out = self.con('next')
        self.assertEqual(rc, 2, out)
        self.assertIn('locked by pid', out)

    def test_is_error_output_is_a_failure(self):
        """is_error: true は subtype: success でも起動の失敗で、結果のファイルがあっても救わない。"""
        self.con('start', '--goal', 'billing', '--launch', 'headless')
        rc, out = self.con('run', env={'FAKE_IS_ERROR': '1'})
        self.assertEqual((rc, out.splitlines()[0]), (12, 'state: retry'), out)
        self.assertIn('designer is_error', out)
        short = self.read_json(RUN)['run_id'][:8]
        self.assertTrue(os.path.isfile(self.path(f'docs/pbi/conductor/episodes/{short}-1-design.result.json')))
        rc, out = self.con('run', env={'FAKE_IS_ERROR': '1'})
        self.assertEqual((rc, out.splitlines()[0]), (10, 'state: question'), out)
        self.assertIn('launch-failure', out)

    def test_abort_and_force_start(self):
        """abort は aborted で閉じ、start は途中の run を断り、--force は途中の run.json を退ける。"""
        self.con('start', '--goal', 'abort')
        rc, out = self.con('next')
        rc, out = self.con('start', '--goal', 'again')
        self.assertEqual(rc, 2, out)

        rc, out = self.con('abort', '--reason', 'test stop')
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: aborted'), out)
        rc, out = self.con('next')
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: aborted'), out)
        self.assertEqual(self.line(out, 'launch'), '')
        rc, out = self.con('status')
        self.assertEqual(rc, 0, out)
        self.assertTrue(out.startswith('state: aborted'), out)
        self.assertIn('test stop', out)

        rc, out = self.con('start', '--goal', 'after abort')
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: ready'), out)
        in_progress = self.read_json(RUN)['run_id']
        rc, out = self.con('start', '--goal', 'refused')
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.read_json(RUN)['run_id'], in_progress)  # 断った start は退けない

        before = self.aside_runs()
        rc, out = self.con('start', '--goal', 'forced', '--force')
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: ready'), out)
        after = self.aside_runs()
        self.assertEqual(len(after), len(before) + 1)  # 途中の run.json が 1 つ退けられた
        added = set(after) - set(before)  # 同じ秒に始めても、増えた 1 つが今退けた (最も新しい) run
        self.assertEqual(len(added), 1, after)
        newest = added.pop()
        self.assertEqual(self.read_json(f'docs/pbi/conductor/{newest}')['run_id'], in_progress)
        self.assertNotEqual(self.read_json(RUN)['run_id'], in_progress)
        self.assertEqual(self.read_json(RUN)['state'], 'ready')

    def test_state_none(self):
        """run.json が無ければ status と next は state: none、HANDOFF.md があれば inline の行も出す。"""
        rc, out = self.con('status')
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: none'), out)
        self.assertNotIn('inline:', out)
        rc, out = self.con('next')
        self.assertEqual((rc, out.splitlines()[0]), (2, 'state: none'), out)
        rc, out = self.con('answer', '--text', 'x')
        self.assertEqual((rc, out.splitlines()[0]), (2, 'state: none'), out)

        self.write('docs/pbi/HANDOFF.md', '# handoff\n')
        rc, out = self.con('status')
        self.assertEqual(rc, 0, out)
        self.assertTrue(out.startswith('state: none'), out)
        self.assertIn('inline: v5 run', out)

    def test_request_optional_name_lines(self):
        """依頼文の `{?name}` の行は値のあるときだけ残る。"""
        self.con('start', '--goal', 'optional lines')
        rc, out = self.con('next')
        request = self.request(out)
        self.assertIn('Episode: design (seq 1)', request)
        self.assertIn('Goal: docs/pbi/conductor/goal.md', request)
        self.assertNotIn('Wave:', request)
        self.assertNotIn('PBIs:', request)
        self.assertNotIn('Continue from:', request)
        self.assertNotIn('Answer:', request)
        self.assertNotIn('{', request)
        launch = self.line(out, 'launch')
        self.assertTrue(launch.startswith('agent '), launch)
        self.assertIn('atelier-designer', launch)
        short = self.read_json(RUN)['run_id'][:8]
        self.assertIn(f'ATELIER.{short}.1 design', out)
        running = self.read_json(RUN)['running']
        self.assertEqual(running['launcher'], 'agent')
        self.assertTrue(running['launched_at'])

        self.write_json('docs/pbi/waves.json', {'waves': [{'n': 1, 'pbis': ['PBI-001']}]})
        self.result(out, status='done', next={'episode': 'build', 'wave': 1})
        self.con('done')
        rc, out = self.con('next')
        build = self.request(out)
        self.assertIn('Wave: 1 (its PBIs are in docs/pbi/waves.json)', build)
        self.assertNotIn('PBIs:', build)

        self.write(STATE, '# state\n')
        self.result(out, status='handoff', state=STATE)
        self.con('done')
        rc, out = self.con('next')
        request = self.request(out)
        self.assertIn(f'Continue from: {STATE}', request)
        self.assertNotIn('Answer:', request)

    def test_headless_without_agent_definition(self):
        """定義が無いときは先頭に Read の行 (agent) か --agents (headless) で後ろ盾し、API キーは渡さない。"""
        agents = os.path.join(self.user, '.claude', 'agents')
        for name in os.listdir(agents):
            os.remove(os.path.join(agents, name))

        self.con('start', '--goal', 'no definitions', '--launch', 'headless')
        rc, out = self.con('run', env={'ANTHROPIC_API_KEY': 'must-not-leak'})
        self.assertEqual((rc, out.splitlines()[0]), (10, 'state: question'), out)
        self.assertIn('warn agents', out)
        rows = [json.loads(line) for line in self.read(self.log).splitlines()]
        args = rows[0]['args'] if rows else []
        self.assertIn('-p', args)
        self.assertIn('--output-format', args)
        self.assertIn('--session-id', args)
        self.assertIn('atelier-designer', args)
        self.assertLess(args.index('--agents'), args.index('--agent'))
        fallback = json.loads(args[args.index('--agents') + 1])
        self.assertTrue(fallback['atelier-designer']['prompt'])
        self.assertFalse(any(row['api_key_seen'] for row in rows))

        # agent では general-purpose で起こし、依頼文の先頭に定義を読ませる行を付ける
        self.con('start', '--goal', 'fallback', '--force')
        rc, out = self.con('next')
        self.assertIn('warn agents', out)
        self.assertIn('general-purpose', self.line(out, 'launch'))
        lines = self.request(out).splitlines()
        self.assertTrue(lines[0].startswith('Read '), lines[:1])
        self.assertIn('agents/atelier-designer.md', lines[0])

    def test_run_detach_and_wait(self):
        """run --detach は running ですぐ返り、wait は running でなくなるまで待つ。"""
        self.con('start', '--goal', 'headless', '--launch', 'headless')
        env = {'FAKE_DESIGNER_SLEEP': '4', 'ANTHROPIC_API_KEY': 'must-not-leak'}
        began = time.monotonic()
        rc, out = self.con('run', '--detach', env=env)
        took = time.monotonic() - began
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: running'), out)
        self.assertLess(took, 3.5, f'{took:.1f}s')
        self.assertTrue(pids(self.read_json(RUN)))

        began = time.monotonic()
        rc, out = self.con('wait', '--timeout', '1')
        took = time.monotonic() - began
        self.assertEqual(rc, 0, out)
        self.assertTrue(out.startswith('state: running'), out)
        self.assertLess(took, 10, f'{took:.1f}s')

        rc, out = self.con('wait', '--timeout', '120')
        self.assertEqual((rc, out.splitlines()[0]), (10, 'state: question'), out)
        self.assertLessEqual(len(out.splitlines()), 8, out)

        rows = [json.loads(line) for line in self.read(self.log).splitlines()]
        args = rows[0]['args'] if rows else []
        for flag in ('-p', '--agent', 'atelier-designer', '--model', '--permission-mode',
                     '--permission-prompts', '--session-id'):
            self.assertIn(flag, args)
        self.assertFalse(any(row['api_key_seen'] for row in rows))
        self.assertTrue(os.path.isfile(self.path('docs/pbi/conductor/episodes/1-design.json')))

        rc, out = self.con('answer', '--text', 'yes')
        self.assertEqual(rc, 0, out)
        rc, out = self.con('run', '--max-episodes', '1')
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: ready'), out)
        rc, out = self.con('run')
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: finished'), out)

        run = self.read_json(RUN)
        costs = [row.get('cost_usd') for row in run['history']]
        sessions = [row.get('session_id') for row in run['history']]
        self.assertEqual(costs.count(0.25), 4, run['history'])
        self.assertIn('fake-session', sessions)
        rc, out = self.con('status')
        self.assertIn('1.00', out)

    def test_finish_writes_role_stats(self):
        """finish の流れで run が閉じ、進行役は統計 (roles.json) を書かないことを固定する。"""
        self.write_json('docs/pbi/waves.json', {'waves': [{'n': 1, 'pbis': ['PBI-001']}]})
        self.con('start', '--goal', 'finish')
        rc, out = self.con('next')
        self.result(out, status='done', next={'episode': 'build', 'wave': 1})
        self.con('done')
        rc, out = self.con('next')
        self.result(out, status='done', next={'episode': 'review'})
        rc, out = self.con('done')
        self.assertEqual(rc, 0, out)
        rc, out = self.con('next')
        self.result(out, status='done', next={'episode': 'build', 'wave': 1, 'pbis': ['PBI-001']})
        rc, out = self.con('done')
        self.assertEqual(rc, 0, out)
        rc, out = self.con('next')  # 差し戻しの build
        self.assertIn('PBI-001', self.request(out))
        # 差し戻しの build は送った review に返す (finish は断られる)
        self.result(out, status='done', next={'episode': 'review', 'wave': 1})
        rc, out = self.con('done')
        self.assertEqual(rc, 0, out)
        rc, out = self.con('next')  # 差し戻しを返した review
        self.result(out, status='done', next={'episode': 'finish'})
        rc, out = self.con('done')
        self.assertEqual(rc, 0, out)
        rc, out = self.con('next')
        self.assertEqual(self.line(out, 'episode'), 'finish seq 6', out)
        self.result(out, status='done', next=None, report='docs/pbi/report.md')
        rc, out = self.con('done')
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: finished'), out)
        self.assertIn('report: docs/pbi/report.md', out)

        rc, out = self.con('next')
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: finished'), out)
        rc, out = self.con('status')
        self.assertEqual(rc, 0, out)
        self.assertTrue(out.startswith('state: finished'), out)
        self.assertIn('cost: unmeasured', out)
        run = self.read_json(RUN)
        self.assertEqual(run['schema'], 'atelier.run/1')
        self.assertGreaterEqual(len(run['history']), 5)
        # 仕様では finish の出番が agent_stats.py を流して roles.json を書く。進行役は書かない
        self.assertFalse(os.path.isfile(self.path('docs/pbi/stats/roles.json')))

    def test_status_run_json_without_state(self):
        """schema は合っても state の無い run.json は壊れた run として断り、state: None を出さない。"""
        self.write_json(RUN, {'schema': conductor.SCHEMA})
        rc, out = self.con('status')
        self.assertNotEqual(rc, 0, out)
        self.assertNotIn('state: None', out)
        self.assertIn('no valid state', out)

    def test_question_relaunch_closes_and_runs_headless(self):
        """no-agent-tool の問いのあとの `run --launch headless --detach` は、問いを閉じて headless で起こす。"""
        self.con('start', '--goal', 'relaunch question')
        rc, out = self.con('next')
        self.result(out, status='error', reason='no-agent-tool')
        rc, out = self.con('done')
        self.assertEqual((rc, out.splitlines()[0]), (10, 'state: question'), out)
        self.assertIn('run --launch headless --detach', self.line(out, 'relaunch'))
        self.assertEqual(self.read_json(RUN)['question']['kind'], 'decision')

        rc, out = self.con('run', '--launch', 'headless', '--detach', env={'FAKE_DESIGNER_SLEEP': '3'})
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: running'), out)
        run = self.read_json(RUN)
        self.assertIsNone(run['question'])  # 問いは閉じた
        self.assertEqual(run['state'], 'running')
        self.assertEqual(run['running']['seq'], 2)  # 同じ design を headless で起こし直す
        prompt = self.read('docs/pbi/conductor/episodes/2-design.prompt.md')
        self.assertFalse(any(line.startswith('Answer:') for line in prompt.splitlines()), prompt)
        self.assertNotIn('Continue from:', prompt)
        self.assertNotIn('Conductor answer:', prompt)
        rc, out = self.con('wait', '--timeout', '120')
        self.assertEqual((rc, out.splitlines()[0]), (10, 'state: question'), out)

    def test_run_launch_headless_closes_answerless_question(self):
        """`run --launch headless` (detach 無し) も同じ問いを閉じ、切り替えの違う run は問いのまま。"""
        self.con('start', '--goal', 'loop question')
        rc, out = self.con('next')
        self.result(out, status='error', reason='no-agent-tool')
        rc, out = self.con('done')
        self.assertEqual(rc, 10, out)
        rc, out = self.con('run')  # launcher を切り替えない: 問いのまま
        self.assertEqual((rc, out.splitlines()[0]), (10, 'state: question'), out)
        self.assertEqual(self.read_json(RUN)['question']['path'],
                         'docs/pbi/conductor/episodes/1-decision.question.md')

        rc, out = self.con('run', '--launch', 'headless')
        self.assertEqual((rc, out.splitlines()[0]), (10, 'state: question'), out)
        run = self.read_json(RUN)
        self.assertEqual(run['seq'], 2)  # 問いを閉じ、design を headless で起こした
        self.assertEqual(run['question']['path'], 'docs/pbi/notes/q.md')  # 次は設計者の問い

    def test_start_asks_for_the_sender_setup_only_without_config(self):
        """送り手があって設定が無ければ、start は ask harness-log: の行を出す。設定ができたら出さない。"""
        rc, out = self.con('start', '--goal', 'first')
        self.assertEqual(rc, 0, out)
        ask = self.line(out, 'ask harness-log')
        self.assertIn('init --remote <URL> | --local | --off', ask)
        self.assertEqual(out.splitlines()[0], 'state: ready')
        self.write_abs(self.env['HARNESS_LOG_CONFIG'], json.dumps({'mode': 'local', 'key': 'k' * 64}))
        rc, out = self.con('start', '--force', '--goal', 'second')
        self.assertEqual(rc, 0, out)
        self.assertNotIn('ask harness-log', out)

    def test_start_force_stops_old_children(self):
        """start --force は前の run の child_pid と runner_pid を止めてから退ける。"""
        self.con('start', '--goal', 'first')
        rc, out = self.con('next')
        child = self.sleeping_child()
        runner = self.sleeping_child()
        self.record_child(child.pid)  # launched_at を今にして、両方を自分の子とみなさせる
        run = self.read_json(RUN)
        run['runner_pid'] = runner.pid
        run.pop('runner_pid_started', None)
        self.write_json(RUN, run)
        old_id = run['run_id']

        rc, out = self.con('start', '--goal', 'forced', '--force')
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: ready'), out)
        self.assertIsNotNone(child.wait(timeout=10))
        self.assertIsNotNone(runner.wait(timeout=10))
        new = self.read_json(RUN)
        self.assertNotEqual(new['run_id'], old_id)
        self.assertEqual(new['state'], 'ready')
        self.assertEqual(pids(new), [])

    def test_old_runner_does_not_settle_new_run(self):
        """前の run の runner は、run_id の違う出番の結果を読まない (片付けない)。"""
        self.con('start', '--goal', 'old runner', '--launch', 'headless')
        rc, out = self.con('run', '--detach', env={'FAKE_DESIGNER_SLEEP': '2'})
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: running'), out)
        # 偽の設計者が始まった = 前の runner は run_id と seq を捉えて子を待っている
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not os.path.isfile(self.path(self.log)):
            time.sleep(0.1)
        self.assertTrue(os.path.isfile(self.path(self.log)), 'the fake designer did not start')
        run = self.read_json(RUN)
        short = run['run_id'][:8]  # 出番の結果の名前は起こしたときの run_id
        run['run_id'] = str(uuid.uuid4())
        self.write_json(RUN, run)

        rc, out = self.con('wait', '--timeout', '60')
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: running'), out)
        self.assertIn('stale: no live runner', out)  # 前の runner は結果を読まずに終わった
        run = self.read_json(RUN)
        self.assertEqual(run['state'], 'running')
        self.assertEqual(run['history'], [])
        self.assertTrue(os.path.isfile(self.path(f'docs/pbi/conductor/episodes/{short}-1-design.result.json')))

    def test_conductor_question_answer_uses_conductor_answer(self):
        """state の無い conductor の問いの答えは `Conductor answer:`、state があれば `Continue from:` と `Answer:`。"""
        self.con('start', '--goal', 'conductor answer')
        rc, out = self.con('next')
        rc, out = self.con('done')  # 1 回目: retry
        self.assertEqual((rc, out.splitlines()[0]), (12, 'state: retry'), out)
        rc, out = self.con('next')
        rc, out = self.con('done')  # 2 回目: launch-failure
        self.assertEqual((rc, out.splitlines()[0]), (10, 'state: question'), out)
        self.assertIn('launch-failure', out)
        self.assertIsNone(self.read_json(RUN)['question']['state'])
        rc, out = self.con('answer', '--text', 'try again')
        self.assertEqual(rc, 0, out)
        rc, out = self.con('next')
        request = self.request(out)
        self.assertEqual(self.line(out, 'episode'), 'design seq 3', out)
        self.assertFalse(any(line.startswith('Answer:') for line in request.splitlines()), request)
        self.assertIn('Conductor answer:', request)
        self.assertNotIn('Continue from:', request)
        given = next(line.split(':', 1)[1].split()[0] for line in request.splitlines()
                     if line.startswith('Conductor answer:'))
        self.assertIn('try again', self.read(given))

        # 前の handoff の state がある conductor の問いの答えは、Continue from と Answer のまま
        self.write_json('docs/pbi/waves.json', {'waves': [{'n': 1, 'pbis': ['PBI-001']}]})
        self.result(out, status='done', next={'episode': 'build', 'wave': 1})
        rc, out = self.con('done')
        self.assertEqual(rc, 0, out)
        rc, out = self.con('next')  # build seq 4
        self.write(STATE, '# state\n')
        self.result(out, status='handoff', state=STATE)
        rc, out = self.con('done')
        self.assertEqual(rc, 0, out)
        rc, out = self.con('next')  # build seq 5 (handoff の続き)
        rc, out = self.con('done')  # 1 回目: retry
        self.assertEqual((rc, out.splitlines()[0]), (12, 'state: retry'), out)
        rc, out = self.con('next')
        rc, out = self.con('done')  # 2 回目: launch-failure (state 付き)
        self.assertEqual((rc, out.splitlines()[0]), (10, 'state: question'), out)
        self.assertIn('launch-failure', out)
        self.assertEqual(self.read_json(RUN)['question']['state'], STATE)
        rc, out = self.con('answer', '--text', 'after handoff')
        self.assertEqual(rc, 0, out)
        rc, out = self.con('next')
        request = self.request(out)
        self.assertIn(f'Continue from: {STATE}', request)
        self.assertTrue(any(line.startswith('Answer:') for line in request.splitlines()), request)
        self.assertFalse(any(line.startswith('Conductor answer:') for line in request.splitlines()), request)

    def test_status_and_wait_report_stale_runner(self):
        """running の子も runner も死んでいれば、status と wait は stale と relaunch を出し、wait は待たない。"""
        self.con('start', '--goal', 'stale', '--launch', 'headless')
        child = self.sleeping_child()
        self.headless_running(child.pid)
        rc, out = self.con('status')
        self.assertEqual(rc, 0, out)
        self.assertNotIn('stale:', out)
        child.kill()
        child.wait(timeout=10)
        rc, out = self.con('status')
        self.assertEqual(rc, 0, out)
        self.assertIn('stale: no live runner', out)
        self.assertIn('next --relaunch', self.line(out, 'relaunch'))
        began = time.monotonic()
        rc, out = self.con('wait', '--timeout', '60')
        took = time.monotonic() - began
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: running'), out)
        self.assertIn('stale: no live runner', out)
        self.assertLess(took, 10, f'{took:.1f}s')

    def test_rework_build_records_sent_by(self):
        """差し戻しの build は送った出番を引き継ぎ、ほかの出番 (finish も) を next にすると断る。"""
        self.write_json('docs/pbi/waves.json', {'waves': [{'n': 1, 'pbis': ['PBI-001']}]})
        self.con('start', '--goal', 'rework')
        rc, out = self.con('next')
        self.result(out, status='done', next={'episode': 'build', 'wave': 1})
        self.con('done')
        rc, out = self.con('next')  # build seq 2
        self.result(out, status='done', next={'episode': 'review'})
        rc, out = self.con('done')
        self.assertEqual(rc, 0, out)
        rc, out = self.con('next')  # review seq 3
        self.result(out, status='done', next={'episode': 'build', 'wave': 1, 'pbis': ['PBI-001']})
        rc, out = self.con('done')
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: ready'), out)
        self.assertEqual(self.read_json(RUN)['pending']['sent_by'], 'review')
        rc, out = self.con('next')  # build seq 4 (差し戻し)
        self.assertIn('Sent by: review', self.request(out))
        self.assertEqual(self.read_json(RUN)['running']['sent_by'], 'review')

        self.result(out, status='done', next={'episode': 'finish'})
        rc, out = self.con('done')
        self.assertEqual((rc, out.splitlines()[0]), (12, 'state: retry'), out)
        self.assertIn('sent by review', out)
        self.assertEqual(self.read_json(RUN)['pending']['sent_by'], 'review')  # retry も保つ

        rc, out = self.con('next')  # build seq 5 (retry)
        self.assertIn('Sent by: review', self.request(out))
        self.result(out, status='done', next={'episode': 'review', 'wave': 1})
        rc, out = self.con('done')
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: ready'), out)
        rc, out = self.con('next')
        self.assertEqual(self.line(out, 'episode'), 'review seq 6', out)

    def test_agent_episode_is_not_stale(self):
        """pid を 1 つも持たない agent 起動の出番は stale にせず、wait は timeout まで待つ。"""
        self.con('start', '--goal', 'agent not stale')
        rc, out = self.con('next')
        self.assertEqual(rc, 0, out)
        rc, out = self.con('status')
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: running'), out)
        self.assertNotIn('stale:', out)
        began = time.monotonic()
        rc, out = self.con('wait', '--timeout', '2')
        took = time.monotonic() - began
        self.assertNotIn('stale:', out)
        self.assertGreaterEqual(took, 1.5, f'{took:.1f}s')

    def test_sent_by_survives_launch_failure_question(self):
        """差し戻しの build が launch-failure の問いを通っても、答えのあとの依頼文に Sent by: が残り finish は断る。"""
        self.write_json('docs/pbi/waves.json', {'waves': [{'n': 1, 'pbis': ['PBI-001']}]})
        self.con('start', '--goal', 'sent by question')
        rc, out = self.con('next')
        self.result(out, status='done', next={'episode': 'build', 'wave': 1})
        self.con('done')
        rc, out = self.con('next')
        self.result(out, status='done', next={'episode': 'review'})
        self.con('done')
        rc, out = self.con('next')
        self.result(out, status='done', next={'episode': 'build', 'wave': 1, 'pbis': ['PBI-001']})
        self.con('done')
        rc, out = self.con('next')  # 差し戻しの build
        self.assertIn('Sent by: review', self.request(out))
        rc, out = self.con('done')  # 結果無し 1 回目
        self.assertEqual((rc, out.splitlines()[0]), (12, 'state: retry'), out)
        rc, out = self.con('next')
        rc, out = self.con('done')  # 2 回目: launch-failure
        self.assertEqual((rc, out.splitlines()[0]), (10, 'state: question'), out)
        self.assertEqual(self.read_json(RUN)['question']['sent_by'], 'review')
        rc, out = self.con('answer', '--text', 'go')
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read_json(RUN)['pending']['sent_by'], 'review')
        rc, out = self.con('next')
        self.assertIn('Sent by: review', self.request(out))
        self.result(out, status='done', next={'episode': 'finish'})
        rc, out = self.con('done')
        self.assertEqual((rc, out.splitlines()[0]), (12, 'state: retry'), out)
        self.assertIn('sent by review', out)

    def con_unstoppable(self, *args):
        """stop_child が常に失敗する (止められない子の代わり) conductor.py を CLI として 1 回呼ぶ。"""
        code = ('import sys; sys.path.insert(0, sys.argv[1]); import conductor; '
                'conductor.stop_child = lambda pid: False; sys.exit(conductor.main(sys.argv[2:]))')
        p = subprocess.run([sys.executable, '-c', code, HERE, *args, '--dir', 'docs/pbi'], cwd=self.tmp,
                           capture_output=True, text=True, encoding='utf-8', errors='replace', env=self.env,
                           timeout=120)
        return p.returncode, p.stdout + p.stderr

    def test_stale_agent_result_of_old_run_is_not_read(self):
        """agent の出番の結果の名前は run ごとに違い、前の run の設計者があとから書いた結果を新しい run の done は受け取らない。"""
        self.con('start', '--goal', 'run A')
        rc, out = self.con('next')
        old_result = self.line(out, 'result')
        old_id = self.read_json(RUN)['run_id']
        self.assertIn(f'{old_id[:8]}-1-design.result.json', old_result)
        self.con('start', '--force', '--goal', 'run B')
        rc, out = self.con('next')
        new_id = self.read_json(RUN)['run_id']
        new_result = self.line(out, 'result')
        self.assertIn(f'{new_id[:8]}-1-design.result.json', new_result)
        self.assertNotEqual(old_result, new_result)
        # 前の run の設計者 (start --force で止められない Agent ツールの子) が、自分のパスに結果を書く
        self.write(old_result, json.dumps({
            'schema': 'atelier.episode/1', 'episode': 'design', 'seq': 1, 'status': 'question', 'summary': ['run A'],
            'question': {'kind': 'approve', 'path': 'docs/pbi/notes/A.md', 'summary': 'approve A?'}}))
        rc, out = self.con('done')
        self.assertEqual((rc, out.splitlines()[0]), (12, 'state: retry'), out)
        run = self.read_json(RUN)
        self.assertIsNone(run.get('question'))
        self.assertNotIn('run A', json.dumps(run['history']))

    def test_stale_headless_result_of_unstopped_designer_is_not_read(self):
        """start --force で止められなかった headless の設計者が古い名前に書いた結果を、新しい run は受け取らない。"""
        old_name = 'docs/pbi/conductor/episodes/1-design.result.json'
        new_prompt = self.path('docs/pbi/conductor/episodes/1-design.prompt.md')
        self.con('start', '--goal', 'run A', '--launch', 'headless')
        # 前の run の設計者: 新しい run の出番が起きたあとで、古い名前 (<seq>-<出番>) に結果を書き、動き続ける
        old_designer = (
            'import json, os, sys, time\n'
            'deadline = time.monotonic() + 60\n'
            'while not os.path.isfile(sys.argv[1]) and time.monotonic() < deadline: time.sleep(0.05)\n'
            "data = {'schema': 'atelier.episode/1', 'episode': 'design', 'seq': 1, 'status': 'question',"
            " 'summary': ['old run A'], 'question': {'kind': 'approve', 'path': 'docs/pbi/notes/A.md',"
            " 'summary': 'approve A?'}}\n"
            "open(sys.argv[2], 'w', encoding='utf-8').write(json.dumps(data))\n"
            'time.sleep(120)\n')
        child = subprocess.Popen([sys.executable, '-c', old_designer, new_prompt, self.path(old_name)])
        self.addCleanup(self.stop_process, child)
        self.headless_running(child.pid)
        rc, out = self.con_unstoppable('start', '--force', '--goal', 'run B', '--launch', 'headless')
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: ready'), out)
        self.assertEqual([entry['pid'] for entry in self.read_json(RUN)['stuck_pids']], [child.pid])
        new_id = self.read_json(RUN)['run_id']
        # 新しい run の設計者は、前の設計者が古い名前に書き終わるのを待ち、自分の結果を書かずに終わる
        silent = (
            'import json, os, sys, time\n'
            'deadline = time.monotonic() + 60\n'
            'while not os.path.isfile(sys.argv[1]) and time.monotonic() < deadline: time.sleep(0.05)\n'
            "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,"
            " 'session_id': 'fake-session', 'total_cost_usd': 0, 'result': 'fake'}))\n")
        rc, out = self.con('run', env={'ATELIER_DESIGNER_CMD': json.dumps(
            [sys.executable, '-c', silent, self.path(old_name)])})
        with open(new_prompt, encoding='utf-8') as f:
            new_result = self.line(f.read(), 'Result')
        self.assertIn(f'{new_id[:8]}-1-design.result.json', new_result)
        self.assertNotIn(f'write {old_name}', new_result)
        self.assertTrue(os.path.isfile(self.path(old_name)))  # 前の設計者は古い名前に書いた
        self.assertEqual((rc, out.splitlines()[0]), (12, 'state: retry'), out)
        run = self.read_json(RUN)
        self.assertIsNone(run.get('question'))
        self.assertNotIn('old run A', json.dumps(run['history']))

    def test_abort_keeps_stuck_pids_for_next_abort(self):
        """abort で止められなかった子は stuck_pids に残して warn を出し、次の abort がそれを止める。"""
        self.con('start', '--goal', 'stuck')
        self.con('next')
        child = self.sleeping_child()
        self.record_child(child.pid)
        rc, out = self.con_unstoppable('abort')
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: aborted'), out)
        self.assertIn(f'warn child {child.pid} still alive', out)
        self.assertIn('warn stuck pids', out)
        run = self.read_json(RUN)
        self.assertIsNone(run['running'])
        self.assertEqual([entry['pid'] for entry in run['stuck_pids']], [child.pid])
        self.assertIsNone(child.poll())

        rc, out = self.con('abort')  # 閉じた run でも、残った pid があれば止めにいく
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: aborted'), out)
        self.assertIsNotNone(child.wait(timeout=10))
        self.assertNotIn('stuck_pids', self.read_json(RUN))
        rc, out = self.con('abort')  # 残りが無ければ、これまでどおり断る
        self.assertEqual(rc, 2, out)

    def test_start_force_carries_and_stops_stuck_pids(self):
        """start --force で止められなかった子は新しい run の stuck_pids に引き継ぎ、次の start --force が止める。"""
        self.con('start', '--goal', 'first')
        self.con('next')
        child = self.sleeping_child()
        self.record_child(child.pid)
        rc, out = self.con_unstoppable('start', '--goal', 'second', '--force')
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: ready'), out)
        self.assertIn('warn stuck pids', out)
        self.assertEqual([entry['pid'] for entry in self.read_json(RUN)['stuck_pids']], [child.pid])
        rc, out = self.con('start', '--goal', 'third', '--force')
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: ready'), out)
        self.assertIsNotNone(child.wait(timeout=10))
        self.assertEqual(pids(self.read_json(RUN)), [])

    def test_done_refused_while_headless_designer_runs(self):
        """headless の設計者が生きている間の done は片付けず、running と wait の案内を出して 2 で終わる。"""
        self.con('start', '--goal', 'busy', '--launch', 'headless')
        child = self.sleeping_child()
        self.headless_running(child.pid)
        self.write('docs/pbi/conductor/episodes/1-design.result.json', json.dumps({
            'schema': 'atelier.episode/1', 'episode': 'design', 'seq': 1, 'status': 'question', 'summary': ['early'],
            'question': {'kind': 'approve', 'path': 'docs/pbi/notes/a.md', 'summary': 'ok?'}}))
        rc, out = self.con('done')
        self.assertEqual((rc, out.splitlines()[0]), (2, 'state: running'), out)
        self.assertIn('wait', self.line(out, 'then'))
        run = self.read_json(RUN)
        self.assertEqual((run['state'], run['running']['child_pid'], run['history']), ('running', child.pid, []))

        self.stop_process(child)
        child.wait(timeout=10)
        rc, out = self.con('done')  # 設計者がもういなければ、これまでどおり片付ける
        self.assertEqual((rc, out.splitlines()[0]), (10, 'state: question'), out)

    def test_detached_runner_skips_other_run(self):
        """切り離した runner は、親が渡した run_id・seq の出番でなければ何もせずに終わり、親は runner の pid を記録する。"""
        self.con('start', '--goal', 'run B', '--launch', 'headless')
        rc, out = self.con('run', '--run-id', str(uuid.uuid4()), '--seq', '1')
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: ready'), out)
        self.assertEqual((self.read_json(RUN)['state'], self.read_json(RUN)['seq']), ('ready', 0))
        self.assertFalse(os.path.isfile(self.log), 'the designer must not start')

        rc, out = self.con('run', '--detach', env={'FAKE_DESIGNER_SLEEP': '3'})
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: running'), out)
        runner = self.read_json(RUN).get('runner_pid')
        self.assertTrue(isinstance(runner, int) and conductor.pid_alive(runner), runner)  # 親 (終わった) ではない
        rc, out = self.con('wait', '--timeout', '60')
        self.assertEqual((rc, out.splitlines()[0]), (10, 'state: question'), out)

    @unittest.skipIf(os.name == 'nt' or os.path.exists('/proc/self/stat'),
                     'needs POSIX without /proc (macOS/BSD)')
    def test_process_started_without_proc(self):
        """`/proc` の無い POSIX では ps の lstart から起動の時刻を読み、None にしない。"""
        started = conductor.process_started(os.getpid())
        self.assertIsNotNone(started)
        self.assertLess(started, time.time())

    # ---- roles ----

    def write_home_roles(self, roles):
        self.write_abs(os.path.join(self.home, 'agents.json'), json.dumps({'roles': roles}))

    def test_roles_take_run_then_repo_then_home_and_log_the_source(self):
        """役割の値はキーごとに run → repo → home → 既定の順で取り、roles: の行と running・history に出所を残す。"""
        self.write_home_roles({'designer': {'model': 'sonnet'}, 'tester': {'model': 'haiku'},
                               'reviewer': {'other': {'effort': 'max'}}})
        self.write_json('docs/pbi/roles.json', {'roles': {'designer': {'model': 'fable'},
                                                          'reviewer': {'deep': {'model': 'sonnet'}}}})
        rc, out = self.con('start', '--goal', 'roles', '--role', 'reviewer.deep=opus',
                           '--role', 'reviewer.other.effort=low')
        self.assertEqual(rc, 0, out)
        rc, out = self.con('next')
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.line(out, 'roles'),
                         'designer fable (repo), reviewer.deep opus (run), reviewer.other sonnet (default)/low (run), '
                         'tester haiku (home)')
        self.assertIn('model=fable', self.line(out, 'launch'))
        self.assertIn('deep=opus other=sonnet/low', self.request(out))
        running = self.read_json(RUN)['running']
        self.assertEqual(running['roles']['designer']['model'], {'value': 'fable', 'from': 'repo'})
        self.assertEqual(running['roles']['designer']['permission_mode'], {'value': 'auto', 'from': 'default'})
        self.assertEqual(running['roles']['reviewer.other']['effort'], {'value': 'low', 'from': 'run'})

        self.run_fake(out)
        self.con('done')
        history = self.read_json(RUN)['history']
        self.assertEqual(history[-1]['roles']['tester']['model'], {'value': 'haiku', 'from': 'home'})

    def test_invalid_role_value_falls_to_the_next_layer_with_a_warning(self):
        """不正な値はその層を飛ばして次の層の値を使い、使った値と出所を warn に書く。"""
        self.write_home_roles({'designer': {'model': 'sonnet'}})
        self.write_json('docs/pbi/roles.json', {'roles': {'designer': {'model': ''}}})
        self.con('start', '--goal', 'bad roles')
        rc, out = self.con('next')
        self.assertEqual(rc, 0, out)
        self.assertIn("warn roles.designer.model (repo): '' is not valid; using 'sonnet' (home)", out)
        self.assertIn('designer sonnet (home)', self.line(out, 'roles'))

        # 役割が辞書でなければ、その層の役割ごと飛ばす
        self.write_json('docs/pbi/roles.json', {'roles': {'tester': 'haiku'}})
        self.con('start', '--goal', 'bad tester', '--force')
        rc, out = self.con('next')
        self.assertIn('warn roles.tester (repo): not an object; skipped', out)
        self.assertIn('tester sonnet (default)', self.line(out, 'roles'))

    def test_start_refuses_an_unknown_role(self):
        """--role の役割かキーが表に無ければ、run を作らずに exit 2。"""
        for given in ('builder=opus', 'tester.permission_mode=auto', 'designer'):
            rc, out = self.con('start', '--goal', 'x', '--role', given)
            self.assertEqual(rc, 2, f'{given}: {out}')
            self.assertIn('--role', out)
        self.assertFalse(os.path.isfile(self.path(RUN)))

    def test_headless_run_uses_the_logged_designer_model(self):
        """run は running に残した値で設計者を起こす (start --role の model が --model に渡る)。"""
        self.con('start', '--goal', 'headless roles', '--launch', 'headless', '--role', 'designer=sonnet',
                 '--role', 'designer.effort=high')
        rc, out = self.con('run')
        self.assertEqual(rc, 10, out)
        args = json.loads(self.read(self.log).splitlines()[0])['args']
        self.assertEqual(args[args.index('--model') + 1], 'sonnet')
        self.assertEqual(args[args.index('--effort') + 1], 'high')

    def test_an_unreadable_roles_file_is_named_in_a_warning(self):
        """roles.json が JSON でなければ、その層を飛ばして warn roles (repo): unreadable を出す。"""
        self.write('docs/pbi/roles.json', '{"roles": {"designer": ')
        self.con('start', '--goal', 'broken roles')
        rc, out = self.con('next')
        self.assertEqual(rc, 0, out)
        self.assertIn('warn roles (repo): unreadable; skipped', out)
        self.assertIn('designer opus (default)', self.line(out, 'roles'))

    def test_a_kept_record_with_a_bad_value_is_not_used(self):
        """running.roles の値が不正 (手で直した run.json など) なら roles_from_record は None を返し、run は取り直す。"""
        roles, sources, _ = conductor.resolve_roles([])  # 本物の home を読まない
        record = conductor.roles_record(roles, sources)
        self.assertEqual(conductor.roles_from_record(record), roles)
        record['designer']['model']['value'] = None
        self.assertIsNone(conductor.roles_from_record(record))
        record['designer']['model']['value'] = 'opus'
        record['tester']['effort']['value'] = 3
        self.assertIsNone(conductor.roles_from_record(record))
    def test_approve_answers_each_draft_ask_block(self):
        """下書きの ```ask の囲みも決定になる。選ばなければ未回答で承認にならず、--choices で選べば進む。"""
        asks = ['```ask', 'Where does OMM go?', '* cache | outside', '- repo | inside', '```']
        self.con('start', '--goal', 'approve ask blocks')
        rc, out = self.con('next')
        meta, rc, out = self.ask_approve(out, asks)
        self.assertEqual(rc, 10, out)

        rc, out = self.con('answer', '--approve')
        self.assertNotEqual(out.splitlines()[0], 'state: ready', out)
        self.assertEqual(self.approvals(), 0)

        rc, out = self.con('answer', '--approve', '--choices', '1=cache')
        self.assertEqual((rc, out.splitlines()[0]), (0, 'state: ready'), out)
        reply = self.read('docs/pbi/conductor/answers/1-approve.md')
        self.assertIn('1. [ask] Where does OMM go?', reply)
        self.assertIn('**cache**', reply)
        self.assertEqual(self.approvals(), 1)

    # ---- 役割の実行先 ----

    def write_home_runners(self, runners, models=None):
        """home の agents.json に runners を書く (codex は偽の設計者のファイルを path にする)。models は {runner: [id]}。"""
        self.write_abs(os.path.join(self.home, 'agents.json'), json.dumps({'runners': runners}))
        for runner, ids in (models or {}).items():
            self.write_abs(os.path.join(self.home, 'models', f'{runner}.json'),
                           json.dumps({'runner': runner, 'models': [{'id': i} for i in ids]}))

    def codex_start(self, *extra, models=None):
        # codex の設計者は agent の役割を claude -p で起こすので、claude も使えるようにしておく
        self.write_home_runners({'codex': {'enabled': True, 'path': self.fake}, 'agent': {'enabled': True},
                                 'claude': {'enabled': True, 'path': self.fake}},
                                models if models is not None else {'codex': ['gpt-6.1-terra']})
        return self.con('start', '--goal', 'codex designer', '--role', 'designer.runner=codex',
                        '--role', 'designer=gpt-6.1-terra', *extra)

    def test_codex_designer_makes_next_point_to_headless(self):
        """設計者の実行先が codex なら、launch agent の run でも next は起動させず run --detach を案内する。"""
        rc, out = self.codex_start('--role', 'designer.effort=high')
        self.assertEqual(rc, 0, out)
        rc, out = self.con('next')
        self.assertEqual(rc, 0, out)
        self.assertIn('launch: headless (designer runner codex)', out)
        self.assertIn('designer gpt-6.1-terra/high@codex (run)', self.line(out, 'roles'))
        self.assertIn('run --detach', self.line(out, 'then'))
        self.assertEqual(self.read_json(RUN)['state'], 'ready')

    def test_codex_designer_runs_on_codex_exec_and_keeps_tokens(self):
        """run は codex exec で設計者を起こし (定義を読む行を先頭に)、JSONL の使用量と thread を history に残す。"""
        rc, out = self.codex_start('--role', 'designer.effort=high')
        self.assertEqual(rc, 0, out)
        rc, out = self.con('run')
        self.assertEqual(rc, 10, out)
        row = json.loads(self.read(self.log).splitlines()[0])
        args = row['args']
        self.assertEqual(args[:3], ['-a', 'never', 'exec'])
        for flag, value in (('-m', 'gpt-6.1-terra'), ('-s', 'danger-full-access')):
            self.assertEqual(args[args.index(flag) + 1], value)
        self.assertIn('--json', args)
        self.assertIn('--skip-git-repo-check', args)
        configs = [args[i + 1] for i, word in enumerate(args) if word == '-c']
        self.assertIn('model_reasoning_effort="high"', configs)
        self.assertIn('mcp_servers.shared-memory.default_tools_approval_mode="auto"', configs)
        self.assertEqual(args[-1], '-')
        self.assertIn('agents/atelier-designer.md', row['head'])
        history = self.read_json(RUN)['history'][-1]
        self.assertEqual(history['session_id'], 'fake-thread')
        self.assertEqual(history['tokens'], {'input': 100, 'cached_input': 40, 'output': 7})
        self.assertNotIn('cost_usd', history)
        self.assertEqual(history['roles']['designer']['runner'], {'value': 'codex', 'from': 'run'})

    def test_codex_designer_prompt_says_no_agent_tool(self):
        """codex の設計者の依頼文は Agent tool: no、指定の無い run は Agent tool: yes。"""
        self.con('start', '--goal', 'plain')
        rc, out = self.con('next')
        self.assertIn('Agent tool: yes', self.request(out))
        self.assertNotIn('Writing files:', self.request(out))
        self.assertIn('subagent_type=atelier-designer model=opus', self.line(out, 'launch'))
        self.codex_start('--force')
        rc, out = self.con('run')
        self.assertEqual(rc, 10, out)
        prompts = sorted(p for p in os.listdir(self.path('docs/pbi/conductor/episodes')) if p.endswith('.prompt.md'))
        codex_prompt = self.read(f'docs/pbi/conductor/episodes/{prompts[-1]}')
        self.assertIn('Agent tool: no', codex_prompt)
        self.assertIn('Writing files: On Windows', codex_prompt)

    def test_codex_designer_failure_is_a_launch_failure(self):
        """codex の turn.failed と 0 以外の終了は、起動の失敗 (retry) になる。"""
        self.codex_start()
        rc, out = self.con('run', env={'FAKE_IS_ERROR': '1'})
        self.assertEqual((rc, out.splitlines()[0]), (12, 'state: retry'), out)
        self.assertIn('usage limit reached', out)

    def test_codex_permission_mode_picks_the_sandbox(self):
        """designer.permission_mode が codex のサンドボックスの名前ならその値を、それ以外は danger-full-access を渡す。"""
        self.codex_start('--role', 'designer.permission_mode=workspace-write')
        self.con('run')
        args = json.loads(self.read(self.log).splitlines()[0])['args']
        self.assertEqual(args[args.index('-s') + 1], 'workspace-write')

    def test_role_runners_show_in_roles_line_and_prompt(self):
        """レビュー担当とテスト担当の実行先は roles: の行と依頼文の Roles: の行に @<runner> で出る。"""
        self.write_home_runners({'codex': {'enabled': True, 'path': self.fake},
                                 'dsh': {'enabled': True, 'path': self.fake}},
                                {'codex': ['gpt-6.1-terra'], 'dsh': ['deepseek-official/deepseek-flash']})
        rc, out = self.con('start', '--goal', 'roles on runners', '--role', 'reviewer.deep.runner=codex',
                           '--role', 'reviewer.deep=gpt-6.1-terra', '--role', 'tester.runner=dsh',
                           '--role', 'tester=deepseek-official/deepseek-flash')
        self.assertEqual(rc, 0, out)
        rc, out = self.con('next')
        self.assertEqual(rc, 0, out)
        self.assertIn('reviewer.deep gpt-6.1-terra@codex (run)', self.line(out, 'roles'))
        self.assertIn('tester deepseek-official/deepseek-flash@dsh (run)', self.line(out, 'roles'))
        request = self.request(out)
        self.assertIn('deep=gpt-6.1-terra@codex', request)
        self.assertIn('tester deepseek-official/deepseek-flash@dsh', request)
        # 設計者が agent なら Agent ツールで起動する (headless に回さない)
        self.assertIn('subagent_type=atelier-designer', self.line(out, 'launch'))

    def test_start_refuses_unusable_runners(self):
        """無効・見つからない実行先と、設計者に使えない実行先は、run を作らずに exit 2。"""
        self.write_home_runners({'opencode': {'enabled': False, 'path': self.fake},
                                 'codex': {'enabled': True, 'path': os.path.join(self.tmp, 'no-codex.exe')},
                                 'dsh': {'enabled': True, 'path': self.fake}})
        only_python = {'PATH': os.path.dirname(sys.executable)}
        cases = (('reviewer.other.runner=opencode', 'disabled'), ('designer.runner=codex', 'not found'),
                 ('designer.runner=dsh', 'designer'), ('tester.runner=mcp', 'tester'))
        for given, word in cases:
            rc, out = self.con('start', '--goal', 'x', '--role', given, env=only_python)
            self.assertEqual(rc, 2, f'{given}: {out}')
            self.assertIn(word, out, given)
        self.assertFalse(os.path.isfile(self.path(RUN)))

    def test_unlisted_model_only_warns(self):
        """モデルが実行先の一覧に無ければ warn を出して始める。"""
        rc, out = self.codex_start(models={'codex': ['gpt-6.1-sol']})
        self.assertEqual(rc, 0, out)
        self.assertIn('warn roles.designer.model', out)
        self.assertIn('gpt-6.1-terra', out)

    def test_invalid_runner_in_a_layer_falls_through(self):
        """repo の roles.json の受け付けない runner は、その層を飛ばして次の層の値を使う。"""
        self.write_json('docs/pbi/roles.json', {'roles': {'designer': {'runner': 'opencode'}}})
        self.con('start', '--goal', 'bad runner')
        rc, out = self.con('next')
        self.assertEqual(rc, 0, out)
        self.assertIn("warn roles.designer.runner (repo): 'opencode' is not valid; using 'agent' (default)", out)
        self.assertIn('designer opus (default)', self.line(out, 'roles'))

    def test_garbled_question_and_summary_are_warned(self):
        """問いのファイルや要約に ? が 3 つ以上続けば warn garbled を出す。化けていなければ出さない。"""
        self.con('start', '--goal', 'garbled')
        rc, out = self.con('next')
        self.write(STATE, '# state\nImmediate next action: ask\n')
        self.write('docs/pbi/notes/q.md', '# ?????\n\n????????????\n')
        self.result(out, status='question', state=STATE, summary=['design: ??????'],
                    question={'kind': 'decision', 'path': 'docs/pbi/notes/q.md', 'summary': '???????'})
        rc, out = self.con('done')
        self.assertEqual((rc, out.splitlines()[0]), (10, 'state: question'), out)
        warn = next((line for line in out.splitlines() if line.startswith('warn garbled:')), '')
        for place in ('summary', 'question.summary', 'docs/pbi/notes/q.md'):
            self.assertIn(place, warn)
        self.assertNotIn(STATE, warn)
        # 答えるまで status と wait にも残る (切り離した run の呼び手にも届く)
        for command in ('status', 'wait'):
            rc, out = self.con(command, *(['--timeout', '0'] if command == 'wait' else []))
            self.assertIn('warn garbled:', out, command)
        self.con('answer', '--text', 'rewrite the files')
        rc, out = self.con('status')
        self.assertNotIn('warn garbled', out)

        self.con('start', '--goal', 'clean', '--force')
        rc, out = self.con('next')
        self.write('docs/pbi/notes/q.md', '# 設計の問い\n\nどれにしますか?\n')
        self.result(out, status='question', state=STATE, summary=['design: 問いがある'],
                    question={'kind': 'decision', 'path': 'docs/pbi/notes/q.md', 'summary': '進めますか?'})
        rc, out = self.con('done')
        self.assertEqual(rc, 10, out)
        self.assertNotIn('warn garbled', out)

    def test_run_keeps_garbled_warnings_of_episodes_it_moved_past(self):
        """run が次の出番へ進んでも、途中の出番の warn garbled は最後に出す行と status に残る。"""
        self.con('start', '--goal', 'garbled run', '--launch', 'headless')
        rc, out = self.con('run')
        self.assertEqual(rc, 10, out)
        self.con('answer', '--text', 'go')
        rc, out = self.con('run', env={'FAKE_SUMMARY': 'design ????????'})
        self.assertEqual(out.splitlines()[0], 'state: finished', out)
        self.assertIn('warn garbled:', out)
        self.assertIn('warn garbled:', self.con('status')[1])

    def test_codex_effort_is_passed_as_an_escaped_toml_string(self):
        """effort の \\ や " は TOML の基本文字列としてエスケープして渡し、設定した値のまま codex に届く。"""
        self.codex_start('--role', 'designer.effort=a\\b"c')
        self.con('run')
        args = json.loads(self.read(self.log).splitlines()[0])['args']
        self.assertIn('model_reasoning_effort="a\\\\b\\"c"', args)

    def test_claude_designer_found_only_through_agents_json_runs(self):
        """agents.json の path だけで見つかる claude の設計者も、start が通れば run で起こせる。"""
        from pathlib import Path
        from test_claude_run import make_wrapper
        wrapper = make_wrapper(Path(self.tmp), Path(self.fake))
        self.write_home_runners({'claude': {'enabled': True, 'path': str(wrapper)}})
        only = {'ATELIER_DESIGNER_CMD': '', 'PBI_CLAUDE_BIN': '', 'PATH': os.path.dirname(sys.executable)}
        rc, out = self.con('start', '--goal', 'claude designer', '--role', 'designer.runner=claude', env=only)
        self.assertEqual(rc, 0, out)
        rc, out = self.con('run', env=only)
        self.assertEqual(rc, 10, out)
        args = json.loads(self.read(self.log).splitlines()[0])['args']
        self.assertIn('-p', args)


    # ---- 実行先の隔離と承認の封筒 ----

    def test_codex_home_without_sign_in_is_refused(self):
        """CODEX_HOME が空のフォルダ (サインインが無い) なら、codex の設計者は start で断る。"""
        empty = os.path.join(self.tmp, 'empty-codex-home')
        os.makedirs(empty)
        self.env['CODEX_HOME'] = empty
        rc, out = self.codex_start()
        self.assertEqual(rc, 2, out)
        self.assertIn('no sign-in', out)

    def test_mcp_approval_only_when_codex_config_has_shared_memory(self):
        """config.toml に shared-memory が無ければ、承認の -c を渡さない (中身の無い表で codex が落ちるため)。"""
        self.write_abs(os.path.join(self.codex_home, 'config.toml'), 'model = "x"\n')
        self.codex_start()
        self.con('run')
        args = json.loads(self.read(self.log).splitlines()[0])['args']
        self.assertFalse(any('shared-memory' in word for word in args), args)

    def test_codex_designer_prompt_says_runner_state_is_not_outside(self):
        """依頼文は、実行先の CLI が自分の場所に書くのは外への書き込みでないこと、CODEX_HOME を動かさないことを言う。"""
        self.con('start', '--goal', 'state')
        rc, out = self.con('next')
        request = self.request(out)
        self.assertIn('Runner state:', request)
        self.assertIn('leave `CODEX_HOME`', request)

    def test_codex_designer_with_agent_roles_needs_claude(self):
        """codex の設計者で agent の役割が残り、claude が無効なら start は役割の名前を出して断る。"""
        self.write_home_runners({'codex': {'enabled': True, 'path': self.fake}})
        rc, out = self.con('start', '--goal', 'x', '--role', 'designer.runner=codex', '--role', 'designer=gpt-6.1-terra',
                           '--role', 'reviewer.deep.runner=codex', '--role', 'reviewer.deep=gpt-6.1-terra')
        self.assertEqual(rc, 2, out)
        self.assertIn('reviewer.other, tester', out)
        self.assertIn('claude -p', out)
        rc, out = self.con('start', '--goal', 'x', '--role', 'designer.runner=codex', '--role', 'designer=gpt-6.1-terra',
                           *[word for part in ('reviewer.deep', 'reviewer.other', 'tester')
                             for word in ('--role', f'{part}.runner=codex', '--role', f'{part}=gpt-6.1-terra')])
        self.assertEqual(rc, 0, out)

    def test_dsh_role_warns_about_its_sandbox(self):
        self.write_home_runners({'dsh': {'enabled': True, 'path': self.fake}})
        rc, out = self.con('start', '--goal', 'x', '--role', 'tester.runner=dsh',
                           '--role', 'tester=deepseek-official/deepseek-flash')
        self.assertEqual(rc, 0, out)
        self.assertIn('warn roles.tester.runner dsh', out)

    def test_approve_draft_changed_after_its_envelope_is_a_failure(self):
        """封筒を付けたあとで承認の下書きを書き換えた結果は、done が失敗 (retry) にする。"""
        self.con('start', '--goal', 'envelope')
        rc, out = self.con('next')
        self.setup_approve()
        self.append('docs/pbi/notes/approve.md', '\nedited after the envelope\n')
        self.result(out, status='question', question={'kind': 'approve', 'path': 'docs/pbi/notes/approve.md',
                                                      'summary': 'approve 1 PBI', 'check': 'design-record'})
        rc, out = self.con('done')
        self.assertEqual((rc, out.splitlines()[0]), (12, 'state: retry'), out)
        self.assertIn('changed after its envelope', out)

    def test_workflow_line_only_when_named(self):
        """--workflow を渡さない run の依頼文には Workflow の行を出さない (既定の名前を探させない)。"""
        self.con('start', '--goal', 'no workflow')
        rc, out = self.con('next')
        self.assertNotIn('Workflow:', self.request(out))
        self.con('start', '--goal', 'named', '--workflow', 'jev-routed', '--force')
        rc, out = self.con('next')
        self.assertIn('Workflow: jev-routed', self.request(out))


if __name__ == '__main__':
    unittest.main()
