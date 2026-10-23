"""PBI を MCP で受け渡す実行先のための MCP サーバー (jev-mcp)。

外の MCP クライアントが、Jev の振り分け・実行・記録をそのまま使うための口。設計者が
docs/pbi/mcp/<報告名>.prompt.md に書いた依頼文を、MCP クライアント (外の実装担当) が受け取り、
結果を同じ形 (詳細ファイルと 5 行の要約) で返す。ファイルの形と設計者の流れ (承認・verify.py・
レビュー・差し戻し) は変えない。依存は標準ライブラリだけ。

使い方:
  python <このディレクトリ>/jev_mcp.py --root <作業リポジトリ> [--root ...]            # stdio
  python <このディレクトリ>/jev_mcp.py --http [--port 7830] --root <作業リポジトリ>    # Streamable HTTP

--root (または環境変数 PBI_MCP_ROOTS、os.pathsep 区切り) の下にあるリポジトリだけを扱う。
HTTP は 127.0.0.1 だけで待ち受け、Host が 127.0.0.1・localhost・[::1] でない要求は断る。

ツール:
  list_tasks     docs/pbi/mcp/ の依頼文と、その状態 (queued / claimed / done)
  claim_task     依頼文・PBI・差し戻し文・facts・route・worker.md を返し、<報告名>.claim.json で取得済みにする
  submit_result  docs/pbi/reports/<報告名>.md と docs/pbi/mcp/<報告名>.last.md (5 行) を書く
  fail_task      NEEDS_CONTEXT / BLOCKED として同じ 2 つを書く
  verify         verify.py を作業ディレクトリで実行し、表と番号ごとの終了コードを返す
  jev_gate       jev/jev.py でゲートを 1 回聞く (API キーはこのサーバーの側だけが読む)
  route          select_model.py の 1 件の判定 (段・route・外した手順・review・scores) をそのまま返す
  run            依頼文を docs/pbi/<実行先>/<報告名>.prompt.md に書き、実行先 (dsh / opencode) を 1 回動かして、
                 終了コード・.last.md・ログと、使えないときの判定 (runners.md の条件) を返す
  record         record_run.py で 1 回の実行の結果を docs/pbi/runs.jsonl に 1 行足す

外の MCP クライアントは route で手順を受け取り、run でその 1 つを動かし、record で結果を残す。
jev-mcp は、それらの実行先が動く機械で動かす。
"""
import argparse
import datetime
import http.server
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERIFY = HERE / 'verify.py'
JEV = HERE / 'jev' / 'jev.py'

sys.path.insert(0, str(HERE))
try:
    import select_model
    import templates
finally:
    sys.path.pop(0)

SERVER_NAME = 'jev-mcp'
SERVER_VERSION = '0.1.0'
PROTOCOL_VERSIONS = ('2025-06-18', '2025-03-26', '2024-11-05')
DEFAULT_PORT = 7830
ALLOWED_HOSTS = ('127.0.0.1', 'localhost', '[::1]')

REPORT_RE = re.compile(r'^(PBI-\d+)(?:\.rework-(\d+))?$')
NAME_RE = re.compile(r'^[A-Za-z0-9._-]+$')
STATUSES = ('DONE', 'DONE_WITH_CONCERNS', 'NEEDS_CONTEXT', 'BLOCKED')
FAIL_STATUSES = ('NEEDS_CONTEXT', 'BLOCKED')
# record の outcome (record_run.py の --outcome と同じ値)
OUTCOMES = ('合格', '差し戻し', '昇格', '使えず', '保留')
# claim_task が sections に入れる PBI の見出し (SKILL.md 2 章のテンプレート)
SECTIONS = ('目的', '対象', '触らない範囲', '受け入れ条件', '検証コマンド', '参照', '補足')
QUEUE = 'mcp'
# run の実行先ごとの渡し方 (runners.md の「実行先ごとの渡し方」)。あとで実行先を足せる形にする。
# logs: 作業ルートの docs/pbi/<実行先>/<報告名><接尾辞> のうち、あれば返すログ。
DSH_RUN = HERE / 'dsh_run.py'
RECORD_RUN = HERE / 'record_run.py'
OPENCODE_CONFIG = HERE / 'opencode.config.json'
DSH_ERR_RE = re.compile(r'^dsh: [A-Z][A-Z_]*:', re.M)
RUN_RUNNERS = {
    'dsh': {'logs': ('.jsonl', '.err', '.final.md', '.run.json')},
    'opencode': {'logs': ('.jsonl', '.err')},
}
RUN_TIMEOUT_DEFAULT = 3600
MODEL_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/:@+-]*$')
EFFORT_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')
# record の attempt の数え上げと追記を 1 つにするための lock (HTTP の要求は並列に来る)
RECORD_LOCK = threading.Lock()


class ToolError(Exception):
    """呼び出し側の誤り。isError の結果として返す。"""


def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding='utf-8')
    except OSError:
        return None


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def roots_from(args_roots, env=None) -> list[Path]:
    """扱ってよいディレクトリ。--root を先に、無ければ PBI_MCP_ROOTS。"""
    env = os.environ if env is None else env
    raw = list(args_roots or [])
    if not raw and env.get('PBI_MCP_ROOTS'):
        raw = [p for p in env['PBI_MCP_ROOTS'].split(os.pathsep) if p]
    return [Path(p).expanduser().resolve() for p in raw]


def within(path: Path, roots) -> bool:
    return any(path == r or r in path.parents for r in roots)


def unusable_reason(runner: str, code, last: str | None, jsonl: str, err: str) -> str | None:
    """runners.md のその実行先の「使えないとき」の条件。当たれば理由、使えれば None。"""
    reasons = []
    if code != 0:
        reasons.append(f'終了コードが 0 以外 ({code})')
    if runner == 'opencode':
        if '"type":"error"' in jsonl:
            reasons.append('.jsonl に "type":"error" がある')
        if 'database is locked' in err:
            reasons.append('.err に database is locked がある')
    elif runner == 'dsh' and DSH_ERR_RE.search(err):
        reasons.append('.err に dsh: <code>: がある')
    if last is None or not last.strip():
        reasons.append('.last.md が無いか空')
    return ', '.join(reasons) or None


def kill_tree(proc: subprocess.Popen):
    """プロセスグループごと止める。"""
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)], capture_output=True, timeout=15)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def sections_of(pbi_text: str) -> dict:
    """PBI の「## 見出し」ごとの本文 (前後の空白を落とす)。"""
    out = {}
    for name in SECTIONS:
        m = re.search(rf'^## {re.escape(name)}\s*\n(.*?)(?=^## |\Z)', pbi_text, re.S | re.M)
        if m:
            out[name] = m.group(1).strip()
    return out


def verify_commands(pbi_text: str) -> list[dict]:
    """verify.py と同じ読み方で検証コマンドを取り出す。"""
    sys.path.insert(0, str(HERE))
    try:
        import verify
    finally:
        sys.path.pop(0)
    return [{'n': n, 'command': c} for n, c in verify.commands(pbi_text)]


class Tasks:
    """docs/pbi/mcp/ の依頼文を受け渡す操作。roots の外のパスは扱わない。"""

    def __init__(self, roots):
        if not roots:
            raise ValueError('--root か PBI_MCP_ROOTS で、扱うリポジトリを 1 つ以上指定する')
        self.roots = [Path(r).resolve() for r in roots]

    # --- パスの確かめ ---

    def repo(self, repo: str) -> Path:
        if not isinstance(repo, str) or not repo:
            raise ToolError('repo を指定する')
        path = Path(repo).expanduser().resolve()
        if not within(path, self.roots):
            raise ToolError(f'扱えるリポジトリの外: {repo}')
        if not (path / 'docs' / 'pbi').is_dir():
            raise ToolError(f'docs/pbi が無い: {repo}')
        return path

    def workdir(self, workdir: str | None, repo: Path) -> Path:
        if not workdir:
            return repo
        path = Path(workdir).expanduser().resolve()
        if not within(path, self.roots):
            raise ToolError(f'扱えるディレクトリの外: {workdir}')
        if not path.is_dir():
            raise ToolError(f'ディレクトリが無い: {workdir}')
        return path

    @staticmethod
    def report(report: str) -> re.Match:
        m = REPORT_RE.match(report or '')
        if not m:
            raise ToolError(f'報告名は PBI-<連番> か PBI-<連番>.rework-<n>: {report}')
        return m

    @staticmethod
    def paths(repo: Path, report: str) -> dict:
        base = repo / 'docs' / 'pbi'
        queue = base / QUEUE
        return {
            'base': base,
            'prompt': queue / f'{report}.prompt.md',
            'claim': queue / f'{report}.claim.json',
            'last': queue / f'{report}.last.md',
            'artifacts': queue / f'{report}.artifacts.json',
            'details': base / 'reports' / f'{report}.md',
        }

    @staticmethod
    def state(p: dict) -> str:
        if p['last'].is_file():
            return 'done'
        if p['claim'].is_file():
            return 'claimed'
        return 'queued'

    # --- ツール ---

    def list_tasks(self, repo: str, include_done: bool = False) -> dict:
        root = self.repo(repo)
        queue = root / 'docs' / 'pbi' / QUEUE
        tasks = []
        for prompt in sorted(queue.glob('*.prompt.md')) if queue.is_dir() else []:
            report = prompt.name[:-len('.prompt.md')]
            m = REPORT_RE.match(report)
            if not m:
                continue
            p = self.paths(root, report)
            state = self.state(p)
            if state == 'done' and not include_done:
                continue
            pbi_text = read_text(p['base'] / f'{m.group(1)}.md') or ''
            title = pbi_text.splitlines()[0].lstrip('# ').strip() if pbi_text else ''
            tasks.append({'report': report, 'pbi': m.group(1), 'title': title, 'state': state,
                          'claim': read_json(p['claim'])})
        return {'repo': str(root), 'tasks': tasks}

    def claim_task(self, repo: str, report: str, runner: str = 'mcp') -> dict:
        root = self.repo(repo)
        m = self.report(report)
        if not isinstance(runner, str) or not NAME_RE.match(runner):
            raise ToolError(f'runner は英数字と ._- だけ: {runner}')
        p = self.paths(root, report)
        prompt = read_text(p['prompt'])
        if prompt is None:
            raise ToolError(f'依頼文が無い: docs/pbi/{QUEUE}/{report}.prompt.md')
        if p['last'].is_file():
            raise ToolError(f'{report} は報告済み')
        try:
            fd = os.open(p['claim'], os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise ToolError(f'{report} はほかの実行先が取得済み: {read_json(p["claim"])}') from None
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump({'runner': runner, 'claimed_at': now_iso()}, f, ensure_ascii=False)
        pbi = m.group(1)
        pbi_text = read_text(p['base'] / f'{pbi}.md') or ''
        rework = read_text(p['base'] / f'{report}.md') if m.group(2) else None
        return {
            'repo': str(root),
            'report': report,
            'pbi': pbi,
            'prompt': prompt,
            'pbi_markdown': pbi_text,
            'sections': sections_of(pbi_text),
            'verify_commands': verify_commands(pbi_text),
            'rework': rework,
            'facts': read_json(p['base'] / f'{pbi}.facts.json'),
            'route': read_json(p['base'] / f'{pbi}.route.json'),
            'worker_md': read_text(p['base'] / 'worker.md'),
            'summary_path': f'docs/pbi/{QUEUE}/{report}.last.md',
            'details_path': f'docs/pbi/reports/{report}.md',
        }

    def _write_report(self, root: Path, report: str, status: str, result: str, changed, unresolved: str,
                      details: str, artifacts=None) -> dict:
        p = self.paths(root, report)
        if not p['claim'].is_file():
            raise ToolError(f'{report} は取得されていない (先に claim_task)')
        if p['last'].is_file():
            raise ToolError(f'{report} は報告済み')
        changed = [str(c) for c in (changed or [])]
        unresolved = (unresolved or '').strip() or 'なし'
        body = (details or '').strip()
        if body.startswith('状態:'):
            body = body.split('\n', 1)[1].strip() if '\n' in body else ''
        p['details'].parent.mkdir(parents=True, exist_ok=True)
        p['details'].write_text(f'状態: {status}\n\n{body}\n', encoding='utf-8')
        summary = '\n'.join([
            f'状態: {status}',
            f'結果: {result}',
            f'変更: {", ".join(changed) if changed else "なし"}',
            f'未解決: {" ".join(unresolved.splitlines())}',
            f'詳細: docs/pbi/reports/{report}.md',
        ]) + '\n'
        if artifacts is not None:
            p['artifacts'].write_text(json.dumps(artifacts, ensure_ascii=False, indent=1), encoding='utf-8')
        # .last.md を最後に書く (これがあれば報告済みとみなす)
        p['last'].write_text(summary, encoding='utf-8')
        return {'report': report, 'summary': summary,
                'summary_path': f'docs/pbi/{QUEUE}/{report}.last.md',
                'details_path': f'docs/pbi/reports/{report}.md',
                'artifacts_path': f'docs/pbi/{QUEUE}/{report}.artifacts.json' if artifacts is not None else None}

    def submit_result(self, repo: str, report: str, status: str, passed: int, total: int,
                      changed=None, unresolved: str = '', details: str = '', artifacts=None) -> dict:
        root = self.repo(repo)
        self.report(report)
        if status not in STATUSES:
            raise ToolError(f'status は {" / ".join(STATUSES)} のどれか: {status}')
        if not (isinstance(passed, int) and isinstance(total, int) and 0 <= passed <= total):
            raise ToolError('passed と total は 0 <= passed <= total の整数')
        return self._write_report(root, report, status, f'受け入れ条件 pass {passed}/{total}', changed,
                                  unresolved, details, artifacts)

    def fail_task(self, repo: str, report: str, status: str, reason: str, details: str = '') -> dict:
        root = self.repo(repo)
        self.report(report)
        if status not in FAIL_STATUSES:
            raise ToolError(f'status は {" / ".join(FAIL_STATUSES)} のどれか: {status}')
        if not (reason or '').strip():
            raise ToolError('reason を書く')
        body = details.strip() or f'## 未解決\n- {reason.strip()}'
        return self._write_report(root, report, status, '未実施', [], reason, body)

    def verify(self, repo: str, report: str, workdir: str | None = None, name: str | None = None,
               timeout: int = 900) -> dict:
        root = self.repo(repo)
        m = self.report(report)
        cwd = self.workdir(workdir, root)
        name = name or f'{report}.mcp'
        if not NAME_RE.match(name):
            raise ToolError(f'name は英数字と ._- だけ: {name}')
        base = root / 'docs' / 'pbi'
        r = subprocess.run([sys.executable, str(VERIFY), m.group(1), '--dir', str(base), '--name', name,
                            '--timeout', str(int(timeout))],
                           cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace')
        results = read_json(base / 'verify' / f'{name}.json')
        return {'code': r.returncode, 'table': r.stdout, 'stderr': r.stderr[-2000:],
                'results': (results or {}).get('results')}

    def jev_gate(self, repo: str, gate: str, text: str) -> dict:
        root = self.repo(repo)
        if not isinstance(gate, str) or not NAME_RE.match(gate):
            raise ToolError(f'gate は英数字と ._- だけ: {gate}')
        if not (text or '').strip():
            raise ToolError('text を書く')
        config = root / 'docs' / 'pbi' / 'jev.config.json'
        if not config.is_file():
            raise ToolError('docs/pbi/jev.config.json が無い')
        fd, tmp = tempfile.mkstemp(suffix='.txt')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(text)
            r = subprocess.run([sys.executable, str(JEV), str(config), gate, tmp], cwd=root,
                               capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300)
        finally:
            os.unlink(tmp)
        try:
            answer = json.loads(r.stdout)
        except ValueError:
            answer = None
        return {'code': r.returncode, 'answer': answer, 'stdout': None if answer is not None else r.stdout,
                'stderr': r.stderr[-2000:]}

    def route(self, repo: str, report: str, tier: str | None = None, skip: list | None = None) -> dict:
        """select_model の 1 件の判定を、そのまま返す。

        facts は報告名の PBI の docs/pbi/<PBI>.facts.json (差し戻しの報告名なら元の PBI)。無ければ判定できない。
        answers は docs/pbi/jev/<PBI>.brief.json があればその answers。tier は --tier、skip は --skip と同じ値。
        route.json は書かない (select_model.py を 1 件で動かしたときと同じ)。
        """
        root = self.repo(repo)
        m = self.report(report)
        if tier is not None and tier not in select_model.TIERS:
            raise ToolError(f'tier は {" / ".join(select_model.TIERS)} のどれか: {tier}')
        if skip is None:
            skip = []
        if not isinstance(skip, list) or not all(isinstance(s, str) for s in skip):
            raise ToolError('skip は runner 名かモデル ID (/ を含む値) の配列')
        skipped = frozenset(s for s in skip if s)
        invalid = {s for s in skipped if '/' not in s} - set(select_model.RUNNERS)
        if invalid:
            raise ToolError(f'skip は {" / ".join(select_model.RUNNERS)} のどれかかモデル ID (/ を含む値): '
                            f'{", ".join(sorted(invalid))}')
        base = root / 'docs' / 'pbi'
        pbi = m.group(1)
        facts = read_json(base / f'{pbi}.facts.json')
        if not isinstance(facts, dict):
            raise ToolError(f'facts が無い (判定できない): docs/pbi/{pbi}.facts.json')
        brief = base / 'jev' / f'{pbi}.brief.json'
        answers = select_model.load_answers(brief if brief.is_file() else None)
        home = templates.resolve_home(None)
        try:
            routing, _ = select_model.find_routing(None, str(base), home)
        except SystemExit as error:
            raise ToolError(f'振り分けテンプレートを読めない: {error.code}') from None
        return select_model.decide(facts, answers, datetime.date.today(), forced=tier, skip=skipped,
                                   routing=routing, home=home, pbi=select_model.pbi_number(report))

    def run(self, repo: str, report: str, runner: str, model: str, prompt: str, effort: str | None = None,
            workdir: str | None = None, timeout: int = RUN_TIMEOUT_DEFAULT) -> dict:
        """1 件の報告名を、runners.md の渡し方で実行先に 1 回動かす (dsh と opencode だけ)。

        依頼文を docs/pbi/<runner>/<報告名>.prompt.md に書き (末尾に要約の書き先を足す)、workdir
        (省略時は repo。--root の下だけ) を作業ルートとして起動する。パスは作業ルートからの相対で返す。
        """
        root = self.repo(repo)
        self.report(report)
        if runner not in RUN_RUNNERS:
            raise ToolError(f'run で扱うのは {" と ".join(RUN_RUNNERS)} だけ: {runner}')
        if not isinstance(model, str) or not MODEL_RE.match(model):
            raise ToolError(f'model は英数字と ._/:@+- だけ: {model}')
        if effort is not None and (not isinstance(effort, str) or (effort and not EFFORT_RE.match(effort))):
            raise ToolError(f'effort は英数字と ._- だけ: {effort}')
        effort = effort or None
        if not isinstance(prompt, str) or not prompt.strip():
            raise ToolError('prompt を書く')
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
            raise ToolError('timeout は正の整数 (秒)')
        cwd = self.workdir(workdir, root)
        rel = f'docs/pbi/{runner}'
        base = cwd / rel
        binary = None
        if runner == 'opencode':
            binary = shutil.which('opencode')
            if binary is None:
                raise ToolError('opencode が見つからない (PATH に opencode を足す。runners.md の opencode の節)')
        base.mkdir(parents=True, exist_ok=True)
        prompt_path = base / f'{report}.prompt.md'
        last_path = base / f'{report}.last.md'
        prompt_path.write_text(f'{prompt.rstrip()}\n\n要約の書き先: `{rel}/{report}.last.md`\n', encoding='utf-8')
        # 前の実行の .last.md を、今回の結果と取り違えない
        last_path.unlink(missing_ok=True)
        flags = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' \
            else {'start_new_session': True}
        env = None
        files = []
        if runner == 'dsh':
            argv = [sys.executable, str(DSH_RUN), report, '--model', model]
            if effort:
                argv += ['--effort', effort]
            streams = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.PIPE, 'stderr': subprocess.PIPE}
        else:
            argv = [binary, 'run', '-m', f'{model}#{effort}' if effort else model, '--format', 'json',
                    f'{rel}/{report}.prompt.md を読み、その指示どおりに作業してください。']
            env = {**os.environ, 'OPENCODE_CONFIG': str(OPENCODE_CONFIG)}
            files = [open(base / f'{report}.jsonl', 'wb'), open(base / f'{report}.err', 'wb')]
            streams = {'stdin': subprocess.DEVNULL, 'stdout': files[0], 'stderr': files[1]}
        timed_out = False
        errb = None
        try:
            try:
                proc = subprocess.Popen(argv, cwd=cwd, env=env, **streams, **flags)
            except OSError as e:
                raise ToolError(f'起動できない: {e}') from None
            try:
                _, errb = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                kill_tree(proc)
                timed_out = True
                try:
                    _, errb = proc.communicate(timeout=10)
                except (subprocess.TimeoutExpired, OSError, ValueError):
                    errb = None
        finally:
            for f in files:
                f.close()
        stderr = (errb or b'').decode('utf-8', errors='replace')
        code = None if timed_out else proc.returncode
        last = read_text(last_path)
        jsonl = read_text(base / f'{report}.jsonl') or ''
        err = read_text(base / f'{report}.err') or ''
        reason = unusable_reason(runner, code, last, jsonl, err)
        if timed_out:
            reason = f'timeout ({timeout}s) で打ち切った' + (f', {reason}' if reason else '')
        elif reason and stderr.strip():
            reason += f' / 起動スクリプトの stderr: {stderr.strip()[-300:]}'
        report_file = cwd / 'docs' / 'pbi' / 'reports' / f'{report}.md'
        return {
            'runner': runner,
            'model': model,
            'effort': effort,
            'code': code,
            'last': last,
            'report_path': f'docs/pbi/reports/{report}.md' if report_file.is_file() else None,
            'logs': [f'{rel}/{report}{suffix}' for suffix in RUN_RUNNERS[runner]['logs']
                     if (base / f'{report}{suffix}').is_file()],
            'unusable': reason is not None,
            'unusable_reason': reason,
        }

    def record(self, repo: str, report: str, attempt: int, outcome: str, findings: list | None = None,
               runner: str | None = None, model: str | None = None, effort: str | None = None,
               tier: str | None = None) -> dict:
        """record_run.py を 1 回呼び、1 回の実行 (PBI × attempt) を docs/pbi/runs.jsonl に 1 行足す。

        record_run.py <報告名> --attempt N --outcome O [...] を、repo をカレントにして動かす。引数は
        record_run.py の --findings・--runner・--model・--effort・--tier と同じ。同じ (報告名, attempt) が
        既にあれば record_run.py が終了コード 2 を返すので、そのまま返す。行は jev-mcp では組み立てない。
        """
        root = self.repo(repo)
        self.report(report)
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ToolError('attempt は 1 以上の整数')
        if not isinstance(outcome, str):
            raise ToolError('outcome は record_run.py の --outcome と同じ値の文字列')
        argv = [sys.executable, str(RECORD_RUN), report, '--attempt', str(attempt), '--outcome', outcome]
        if findings is not None:
            if not isinstance(findings, list) or len(findings) != 3 \
                    or not all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in findings):
                raise ToolError('findings は 重大,中,軽微 の 3 つの整数 (例 [0, 1, 2])')
            argv += ['--findings', ','.join(str(n) for n in findings)]
        for option, value in (('--runner', runner), ('--model', model), ('--effort', effort), ('--tier', tier)):
            if value is None:
                continue
            if not isinstance(value, str):
                raise ToolError(f'{option[2:]} は文字列')
            argv += [option, value]
        # 並列の要求で runs.jsonl の行が混ざらないよう、record_run.py の呼び出しを 1 つにする
        with RECORD_LOCK:
            try:
                r = subprocess.run(argv, cwd=root, capture_output=True, text=True, encoding='utf-8',
                                   errors='replace', env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
            except OSError as error:
                raise ToolError(f'record_run.py を起動できない: {error}') from None
        return {'code': r.returncode, 'stdout': r.stdout, 'stderr': r.stderr[-2000:]}


REPO = {'type': 'string', 'description': '作業リポジトリの絶対パス (サーバーの --root の下)'}
REPORT = {'type': 'string', 'description': '報告名 (PBI-007 / PBI-007.rework-1)'}

TOOLS = [
    {'name': 'list_tasks',
     'description': 'docs/pbi/mcp/ に置かれた依頼文と状態 (queued / claimed / done) を返す。',
     'inputSchema': {'type': 'object', 'required': ['repo'], 'properties': {
         'repo': REPO,
         'include_done': {'type': 'boolean', 'default': False, 'description': '報告済みも含める'}}}},
    {'name': 'claim_task',
     'description': '依頼文を取得済みにし、依頼文・PBI 本文と見出しごとの節・検証コマンド・差し戻し文・facts・route・'
                    'worker.md を返す。同じ報告名を 2 回は取得できない。',
     'inputSchema': {'type': 'object', 'required': ['repo', 'report'], 'properties': {
         'repo': REPO, 'report': REPORT,
         'runner': {'type': 'string', 'default': 'mcp', 'description': '取得した実行先の名前 (例 my-client)'}}}},
    {'name': 'submit_result',
     'description': '結果を docs/pbi/reports/<報告名>.md と docs/pbi/mcp/<報告名>.last.md (worker.md の 5 行) に書く。'
                    'artifacts は実行先の出力 (クライアントの実行の要約など) をそのまま JSON で残す。',
     'inputSchema': {'type': 'object', 'required': ['repo', 'report', 'status', 'passed', 'total'], 'properties': {
         'repo': REPO, 'report': REPORT,
         'status': {'type': 'string', 'enum': list(STATUSES)},
         'passed': {'type': 'integer', 'minimum': 0, 'description': '自分で確かめて pass した受け入れ条件の数'},
         'total': {'type': 'integer', 'minimum': 0, 'description': '受け入れ条件の数'},
         'changed': {'type': 'array', 'items': {'type': 'string'}, 'description': '変更したファイル'},
         'unresolved': {'type': 'string', 'description': '未解決 (無ければ空)'},
         'details': {'type': 'string', 'description': '詳細ファイルの本文 (worker.md の 4 つの見出し)'},
         'artifacts': {'type': 'object', 'description': '実行先の出力 (任意)'}}}},
    {'name': 'fail_task',
     'description': '進められなかったことを NEEDS_CONTEXT / BLOCKED として報告する。',
     'inputSchema': {'type': 'object', 'required': ['repo', 'report', 'status', 'reason'], 'properties': {
         'repo': REPO, 'report': REPORT,
         'status': {'type': 'string', 'enum': list(FAIL_STATUSES)},
         'reason': {'type': 'string'},
         'details': {'type': 'string'}}}},
    {'name': 'verify',
     'description': 'verify.py で PBI の検証コマンドを workdir (省略時は repo) で実行し、表と番号ごとの終了コードを返す。'
                    '合否は判定しない。',
     'inputSchema': {'type': 'object', 'required': ['repo', 'report'], 'properties': {
         'repo': REPO, 'report': REPORT,
         'workdir': {'type': 'string', 'description': '検証する作業ツリー (worktree など。--root の下)'},
         'name': {'type': 'string', 'description': 'ログ名 (既定 <報告名>.mcp)'},
         'timeout': {'type': 'integer', 'default': 900}}}},
    {'name': 'jev_gate',
     'description': 'jev/jev.py で docs/pbi/jev.config.json のゲートを 1 回聞く。文章だけを送る (コード・差分・秘密は送らない)。',
     'inputSchema': {'type': 'object', 'required': ['repo', 'gate', 'text'], 'properties': {
         'repo': REPO,
         'gate': {'type': 'string', 'description': 'brief / pbi_quality / report / rework_scope / rework_quality など'},
         'text': {'type': 'string'}}}},
    {'name': 'route',
     'description': 'select_model.py の 1 件の判定 (段・試す順の route・外した手順・review・scores・理由・'
                    '優先テンプレート) をそのまま返す。facts は docs/pbi/<PBI>.facts.json (差し戻しの報告名なら'
                    '元の PBI)、answers は docs/pbi/jev/<PBI>.brief.json。tier は --tier、skip は --skip と同じ。',
     'inputSchema': {'type': 'object', 'required': ['repo', 'report'], 'properties': {
         'repo': REPO, 'report': REPORT,
         'tier': {'type': 'string', 'enum': list(select_model.TIERS)},
         'skip': {'type': 'array', 'items': {'type': 'string'},
                  'description': 'route から外す runner 名かモデル ID (/ を含む値)'}}}},
    {'name': 'run',
     'description': '1 件の報告名を、runners.md の渡し方で実行先 (dsh / opencode) に 1 回動かす。prompt を '
                    'docs/pbi/<runner>/<報告名>.prompt.md に書き (末尾に要約の書き先を足す)、workdir を作業ルートとして'
                    '起動し、終了コード・.last.md の本文・報告ファイルとログのパス・使えないときの判定を返す。',
     'inputSchema': {'type': 'object', 'required': ['repo', 'report', 'runner', 'model', 'prompt'], 'properties': {
         'repo': REPO, 'report': REPORT,
         'runner': {'type': 'string', 'enum': list(RUN_RUNNERS)},
         'model': {'type': 'string', 'description': 'route の model (dsh は <provider>/<model>)'},
         'effort': {'type': 'string', 'description': 'route の effort (null / 省略なら付けない)'},
         'prompt': {'type': 'string', 'description': '依頼文の本文'},
         'workdir': {'type': 'string', 'description': '作業ルート (--root の下。省略時は repo)'},
         'timeout': {'type': 'integer', 'default': RUN_TIMEOUT_DEFAULT, 'description': '打ち切る秒数'}}}},
    {'name': 'record',
     'description': 'record_run.py を 1 回呼び、1 回の実行 (PBI × attempt) の結果を docs/pbi/runs.jsonl に '
                    '1 行足す。同じ (報告名, attempt) が既にあれば record_run.py の終了コード 2 をそのまま返す。',
     'inputSchema': {'type': 'object', 'required': ['repo', 'report', 'attempt', 'outcome'], 'properties': {
         'repo': REPO, 'report': REPORT,
         'attempt': {'type': 'integer', 'minimum': 1, 'description': 'route の何番目か (1 始まり)'},
         'outcome': {'type': 'string', 'enum': list(OUTCOMES), 'description': 'record_run.py の --outcome と同じ値'},
         'findings': {'type': 'array', 'items': {'type': 'integer', 'minimum': 0}, 'minItems': 3, 'maxItems': 3,
                      'description': '重大,中,軽微 の 3 つの整数 (省略可)'},
         'runner': {'type': 'string', 'description': 'route.json と違う実行先で動かしたとき (--runner)'},
         'model': {'type': 'string', 'description': 'route.json と違うモデルで動かしたとき (--model)'},
         'effort': {'type': 'string', 'description': '--effort と同じ'},
         'tier': {'type': 'string', 'description': '--tier と同じ'}}}},
]


class Server:
    """MCP の JSON-RPC (initialize・ping・tools/list・tools/call) を Tasks につなぐ。"""

    def __init__(self, tasks: Tasks):
        self.tasks = tasks

    def handle(self, msg):
        """1 件の要求に答える。通知 (id なし) には None を返す。"""
        if not isinstance(msg, dict) or msg.get('jsonrpc') != '2.0' or not isinstance(msg.get('method'), str):
            return self._error(msg.get('id') if isinstance(msg, dict) else None, -32600, 'Invalid Request')
        if 'id' not in msg:
            return None
        rid, method, params = msg['id'], msg['method'], msg.get('params') or {}
        if method == 'initialize':
            asked = params.get('protocolVersion')
            version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return self._result(rid, {'protocolVersion': version, 'capabilities': {'tools': {}},
                                      'serverInfo': {'name': SERVER_NAME, 'version': SERVER_VERSION}})
        if method == 'ping':
            return self._result(rid, {})
        if method == 'tools/list':
            return self._result(rid, {'tools': TOOLS})
        if method == 'tools/call':
            return self._result(rid, self.call(params.get('name'), params.get('arguments') or {}))
        return self._error(rid, -32601, f'Method not found: {method}')

    def call(self, name, arguments) -> dict:
        if name not in {t['name'] for t in TOOLS}:
            return self._tool_error(f'ツールが無い: {name}')
        if not isinstance(arguments, dict):
            return self._tool_error('arguments はオブジェクト')
        try:
            value = getattr(self.tasks, name)(**arguments)
        except ToolError as e:
            return self._tool_error(str(e))
        except TypeError as e:
            return self._tool_error(f'引数が合わない: {e}')
        return {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}],
                'structuredContent': value, 'isError': False}

    def handle_body(self, body):
        """1 件か配列 (batch) を受け、返す値 (無ければ None) を返す。"""
        if isinstance(body, list):
            out = [r for r in (self.handle(m) for m in body) if r is not None]
            return out or None
        return self.handle(body)

    @staticmethod
    def _result(rid, result):
        return {'jsonrpc': '2.0', 'id': rid, 'result': result}

    @staticmethod
    def _error(rid, code, message):
        return {'jsonrpc': '2.0', 'id': rid, 'error': {'code': code, 'message': message}}

    @staticmethod
    def _tool_error(message):
        return {'content': [{'type': 'text', 'text': message}], 'isError': True}


def serve_stdio(server: Server, stdin=None, stdout=None):
    """1 行 1 メッセージの JSON-RPC を読み、答えを 1 行ずつ書く。"""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            body = json.loads(line)
        except ValueError:
            reply = Server._error(None, -32700, 'Parse error')
        else:
            reply = server.handle_body(body)
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + '\n')
            stdout.flush()


def make_handler(server: Server, port: int):
    allowed = {f'{h}:{port}' for h in ALLOWED_HOSTS} | set(ALLOWED_HOSTS)

    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, code, body=None):
            data = b'' if body is None else json.dumps(body, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            if body is not None:
                self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _host_ok(self):
            return self.headers.get('Host', '') in allowed

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, {'error': 'host not allowed'})
            if self.path == '/health':
                return self._send(200, {'ok': True})
            self.send_response(405)
            self.send_header('Allow', 'POST')
            self.send_header('Content-Length', '0')
            self.end_headers()

        def do_POST(self):
            if not self._host_ok():
                return self._send(403, {'error': 'host not allowed'})
            if self.path != '/mcp':
                return self._send(404, {'error': 'not found'})
            try:
                length = int(self.headers.get('Content-Length') or 0)
                body = json.loads(self.rfile.read(length).decode('utf-8'))
            except (ValueError, UnicodeDecodeError):
                return self._send(400, Server._error(None, -32700, 'Parse error'))
            reply = server.handle_body(body)
            if reply is None:
                return self._send(202)
            self._send(200, reply)

        def log_message(self, fmt, *args):
            sys.stderr.write('jev-mcp: ' + fmt % args + '\n')

    return Handler


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--root', action='append', default=[], help='扱う作業リポジトリ (くり返し可)')
    ap.add_argument('--http', action='store_true', help='Streamable HTTP で待ち受ける (省略時は stdio)')
    ap.add_argument('--port', type=int, default=DEFAULT_PORT)
    args = ap.parse_args(argv)
    roots = roots_from(args.root)
    if not roots:
        ap.error('--root か PBI_MCP_ROOTS で、扱うリポジトリを 1 つ以上指定する')
    server = Server(Tasks(roots))
    if not args.http:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8')
        serve_stdio(server)
        return 0
    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(server, args.port))
    sys.stderr.write(f'jev-mcp: http://127.0.0.1:{args.port}/mcp\n')
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
