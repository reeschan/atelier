"""PBI を MCP で受け渡す実行先のための MCP サーバー (jev-mcp)。

設計者が docs/pbi/mcp/<報告名>.prompt.md に書いた依頼文を、MCP クライアント (Harness² などの外部の
実装担当) が受け取り、結果を同じ形 (詳細ファイルと 5 行の要約) で返すための口。ファイルの形と設計者の
流れ (承認・verify.py・レビュー・差し戻し) は変えない。依存は標準ライブラリだけ。

使い方:
  python <このディレクトリ>/jev_mcp.py --root <作業リポジトリ> [--root ...]            # stdio
  python <このディレクトリ>/jev_mcp.py --http [--port 7830] --root <作業リポジトリ>    # Streamable HTTP
  python <このディレクトリ>/jev_mcp.py --root <作業リポジトリ> --session-root <作業ディレクトリ>  # run_session を許す

--root (または環境変数 PBI_MCP_ROOTS、os.pathsep 区切り) の下にあるリポジトリだけを扱う。
--session-root DIR (くり返し可。環境変数 PBI_MCP_SESSION_ROOTS、os.pathsep 区切りでも可) で run_session の cwd を許す。
HTTP は 127.0.0.1 だけで待ち受け、Host が 127.0.0.1・localhost・[::1] でない要求は断る。

ツール:
  list_tasks     docs/pbi/mcp/ の依頼文と、その状態 (queued / claimed / done)
  claim_task     依頼文・PBI・差し戻し文・facts・route・worker.md を返し、<報告名>.claim.json で取得済みにする
  submit_result  docs/pbi/reports/<報告名>.md と docs/pbi/mcp/<報告名>.last.md (5 行) を書く
  fail_task      NEEDS_CONTEXT / BLOCKED として同じ 2 つを書く
  verify         verify.py を作業ディレクトリで実行し、表と番号ごとの終了コードを返す
  jev_gate       jev/jev.py でゲートを 1 回聞く (API キーはこのサーバーの側だけが読む)
  route          役割 (executor / improver) の段で select_model の振り分けを行い、substrate か runners の手順に
                 絞ったモデルの候補と、外した手順を返す
  run_session    改善役のセッションを Claude Code (runner agent) の柵つきで cwd (--session-root の下) で回す。
                 ツールは Read/Edit/Write/Glob/Grep だけで、質問も MCP もなし。認証はこのサーバーを動かす
                 ユーザーの Claude Code の設定のまま (ANTHROPIC_* の環境変数は外す)
  record         Harness² の 1 回の試行の結果を docs/pbi/runs.jsonl に 1 行足す (source: "harness2")

Harness² のモデルのコネクターは route・run_session・record を使い、jev-mcp は Harness² と同じ機械で動かす。
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
import time
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
# route の役割ごとの既定の段 (executor = 実行役、improver = 改善役)
ROLE_TIERS = {'executor': 'haiku', 'improver': 'sonnet'}
# claim_task が sections に入れる PBI の見出し (SKILL.md 2 章のテンプレート)
SECTIONS = ('目的', '対象', '触らない範囲', '受け入れ条件', '検証コマンド', '参照', '補足')
QUEUE = 'mcp'
# run_session の実行先ごとの起動の仕方と柵。柵を申告できる実行先だけをここに置く (いまは agent = Claude Code)
CLAUDE_TOOLS = ('Read', 'Edit', 'Write', 'Glob', 'Grep')
CLAUDE_DROP_ENV = ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL',
                   'ANTHROPIC_BEDROCK_BASE_URL', 'CLAUDE_CODE_USE_BEDROCK')
SESSION_RUNNERS = {
    'agent': {'bin_env': 'PBI_CLAUDE_BIN', 'bin': 'claude', 'tools': CLAUDE_TOOLS, 'drop_env': CLAUDE_DROP_ENV},
}
SYSTEM_ARG_LIMIT = 120_000
# 結果の文にこれらがあれば、実行先が使えない (利用上限・認証の失敗) とみなす。小文字で比べる。
# 返答の本文に出うる一般の語 (authentication など) は使わず、CLI のエラーの文言だけにする
UNAVAILABLE_MARKERS = ('usage limit', 'rate_limit_error', 'invalid api key', 'please run /login',
                       'authentication_error', 'authentication failed')
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


def is_number(value) -> bool:
    """JSON の数値 (bool を除く)。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def session_roots_from(args_roots, env=None) -> list[Path]:
    """run_session の cwd を許すディレクトリ。--session-root を先に、無ければ PBI_MCP_SESSION_ROOTS。"""
    env = os.environ if env is None else env
    raw = list(args_roots or [])
    if not raw and env.get('PBI_MCP_SESSION_ROOTS'):
        raw = [p for p in env['PBI_MCP_SESSION_ROOTS'].split(os.pathsep) if p]
    return [Path(p).expanduser().resolve() for p in raw]


def is_unavailable(cli_missing: bool, returncode, data, text: str, ok: bool) -> bool:
    """実行先が使えない (CLI が無い・利用上限や認証の失敗・JSON が読めない異常終了) かどうか。
    タイムアウトはここに来ない (呼び出し側で ok: false の timeout にする)。"""
    if cli_missing:
        return True
    if data is None and returncode != 0:
        return True
    if not ok:
        lowered = (text or '').lower()
        return any(marker in lowered for marker in UNAVAILABLE_MARKERS)
    return False


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

    def __init__(self, roots, session_roots=None):
        if not roots:
            raise ValueError('--root か PBI_MCP_ROOTS で、扱うリポジトリを 1 つ以上指定する')
        self.roots = [Path(r).resolve() for r in roots]
        self.session_roots = [Path(r).resolve() for r in (session_roots or [])]

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

    def route(self, repo: str, report: str, role: str, substrate: str = '', runners: list | None = None,
              tier: str | None = None) -> dict:
        """役割の段で select_model の振り分けを行い、試す順のモデルの候補を返す。

        executor は substrate と同じ実行先の手順だけ、improver は runners に入る実行先の手順だけに絞る。
        段は tier があればそこ、無ければ ROLE_TIERS の既定。facts は報告名の PBI の facts.json。
        """
        root = self.repo(repo)
        m = self.report(report)
        if role not in ROLE_TIERS:
            raise ToolError(f'role は {" / ".join(ROLE_TIERS)} のどれか: {role}')
        chosen = ROLE_TIERS[role] if tier is None else tier
        if chosen not in select_model.TIERS:
            raise ToolError(f'tier は {" / ".join(select_model.TIERS)} のどれか: {tier}')
        if role == 'executor':
            if not isinstance(substrate, str) or not substrate.strip():
                raise ToolError('executor では substrate (実行先) を指定する')
            substrate = substrate.strip()
            allowed = {substrate}
        else:
            if runners is None:
                runners = ['agent']
            if not isinstance(runners, list) or not all(isinstance(r, str) for r in runners):
                raise ToolError('runners は実行先の名前の配列')
            allowed = set(runners)
        base = root / 'docs' / 'pbi'
        facts = read_json(base / f'{m.group(1)}.facts.json')
        if not isinstance(facts, dict):
            facts = {'sensitive': False}
        home = templates.resolve_home(None)
        try:
            routing, _ = select_model.find_routing(None, str(base), home)
        except SystemExit as error:
            raise ToolError(f'振り分けテンプレートを読めない: {error.code}') from None
        decided = select_model.decide(facts, {}, datetime.date.today(), forced=chosen, routing=routing,
                                      home=home, pbi=select_model.pbi_number(report))
        kept, removed = [], []
        for step in decided['route']:
            if step.get('runner') in allowed:
                kept.append(step)
            elif role == 'executor':
                removed.append({**step, 'reason': f'substrate {substrate} で動かない'})
            elif step.get('runner') == 'agent':
                removed.append({**step, 'reason': 'runners で指定されていない'})
            else:
                removed.append({**step, 'reason': '柵を申告できない実行先'})
        reasons = list(decided['reasons'])
        if role == 'executor':
            reasons.append(f'絞り込み: executor は substrate {substrate} の手順だけ')
        else:
            reasons.append(f"絞り込み: improver は runners {','.join(runners)} の手順だけ")
        return {'role': role, 'tier': chosen, 'route': kept, 'dropped': decided['dropped'] + removed,
                'template': decided['template'], 'reasons': reasons}

    def run_session(self, cwd: str, mission: str, system: str = '', wall: int = 3600, runner: str = 'agent',
                    model: str = '', effort: str = '') -> dict:
        """改善役のセッションを、実行先の柵つきで cwd (--session-root の下) で回す。"""
        if not self.session_roots:
            raise ToolError('run_session を使うには、サーバーの起動時に --session-root を指定して')
        if runner not in SESSION_RUNNERS:
            raise ToolError(f'柵を申告できるのは {" / ".join(SESSION_RUNNERS)} だけ: {runner}')
        if not isinstance(cwd, str) or not cwd:
            raise ToolError('cwd を指定する')
        if not isinstance(mission, str) or not mission.strip():
            raise ToolError('mission を書く')
        for label, value in (('system', system), ('model', model), ('effort', effort)):
            if not isinstance(value, str):
                raise ToolError(f'{label} は文字列')
        if isinstance(wall, bool) or not isinstance(wall, int) or wall <= 0:
            raise ToolError('wall は正の整数 (秒)')
        path = Path(cwd).expanduser().resolve()
        if not path.is_dir():
            raise ToolError(f'ディレクトリが無い: {cwd}')
        if not within(path, self.session_roots):
            raise ToolError(f'--session-root の外: {cwd}')
        spec = SESSION_RUNNERS[runner]
        fence = {'tools': list(spec['tools']), 'cwd_only': True, 'strict_mcp': True, 'no_prompts': True,
                 'restricted': True, 'permission_mode': 'acceptEdits', 'dropped_env': list(spec['drop_env'])}
        result = {'ok': False, 'text': '', 'cost_usd': None, 'tokens': {'input': None, 'output': None},
                  'wall_s': 0.0, 'error': None, 'unavailable': False, 'fence': fence}
        binary = shutil.which(os.environ.get(spec['bin_env']) or spec['bin'])
        if binary is None:
            result.update(error=f'{spec["bin"]} が見つからない (PATH か {spec["bin_env"]})', unavailable=True)
            return result
        argv = [binary, '--restricted', '--tools', ','.join(spec['tools']), '--permission-mode', fence['permission_mode'],
                '--permission-prompts', 'none', '--strict-mcp-config', '-p', '--output-format', 'json']
        if model:
            argv += ['--model', model]
        if effort:
            argv += ['--effort', effort]
        stdin = mission
        if system:
            if len(system) > SYSTEM_ARG_LIMIT:
                stdin = f'# SYSTEM INSTRUCTIONS\n\n{system}\n\n# MISSION\n\n{mission}'
            else:
                argv += ['--append-system-prompt', system]
        env = {k: v for k, v in os.environ.items() if k not in spec['drop_env']}
        flags = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' \
            else {'start_new_session': True}
        started = time.monotonic()
        try:
            proc = subprocess.Popen(argv, cwd=path, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace', **flags)
        except OSError as e:
            result.update(error=f'起動できない: {e}', unavailable=True)
            return result
        try:
            out, err = proc.communicate(stdin, timeout=wall)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            try:
                proc.communicate(timeout=10)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                pass
            result.update(wall_s=round(time.monotonic() - started, 2), error=f'timeout ({wall}s)')
            return result
        result['wall_s'] = round(time.monotonic() - started, 2)
        try:
            data = json.loads(out)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            data = None
        if data is None:
            text, ok = out.strip(), False
            result['error'] = f'終了コード {proc.returncode}: ' + (err.strip()[-500:] or text[-500:] or '出力なし')
        else:
            body = data.get('result', data.get('text'))
            text = body if isinstance(body, str) else ('' if body is None else json.dumps(body, ensure_ascii=False))
            ok = not data.get('is_error') and data.get('subtype', 'success') == 'success' and proc.returncode == 0
            usage = data.get('usage') if isinstance(data.get('usage'), dict) else {}
            result['cost_usd'] = data.get('total_cost_usd')
            result['tokens'] = {'input': usage.get('input_tokens'), 'output': usage.get('output_tokens')}
            if not ok:
                result['error'] = f'is_error={data.get("is_error")} subtype={data.get("subtype")} ' \
                                  f'終了コード {proc.returncode}'
        result.update(ok=ok, text=text,
                      unavailable=is_unavailable(False, proc.returncode, data, text + ' ' + (err or ''), ok))
        return result

    def record(self, repo: str, report: str, role: str, runner: str, model: str, effort: str = '',
               outcome: dict | None = None) -> dict:
        """Harness² の 1 回の試行の結果を docs/pbi/runs.jsonl に 1 行足す。

        行の列名は record_run.py に合わせ、この口から取れない値は null にする。attempt は同じ
        report・role・runner・model の行の数 + 1。outcome (ok / unavailable / ...) の型が違えば ToolError。
        """
        root = self.repo(repo)
        m = self.report(report)
        if role not in ROLE_TIERS:
            raise ToolError(f'role は {" / ".join(ROLE_TIERS)} のどれか: {role}')
        for name, value in (('runner', runner), ('model', model), ('effort', effort)):
            if not isinstance(value, str):
                raise ToolError(f'{name} は文字列')
        if not runner or not model:
            raise ToolError('runner と model を指定する')
        if not isinstance(outcome, dict):
            raise ToolError('outcome はオブジェクト ({"ok": true, ...})')
        if not isinstance(outcome.get('ok'), bool):
            raise ToolError('outcome.ok は true / false')
        unavailable = outcome.get('unavailable', False)
        if not isinstance(unavailable, bool):
            raise ToolError('outcome.unavailable は true / false')
        cost = outcome.get('cost_usd')
        if cost is not None and not is_number(cost):
            raise ToolError('outcome.cost_usd は数値')
        wall = outcome.get('wall_s')
        if wall is not None and not is_number(wall):
            raise ToolError('outcome.wall_s は数値')
        tokens = outcome.get('tokens')
        if tokens is None:
            tokens = {}
        elif not isinstance(tokens, dict):
            raise ToolError('outcome.tokens は {"input": 数値, "output": 数値} のオブジェクト')
        token_values = {}
        for name in ('input', 'output'):
            value = tokens.get(name)
            if value is not None and not is_number(value):
                raise ToolError(f'outcome.tokens.{name} は数値')
            token_values[name] = value
        for name in ('error', 'run_id', 'label'):
            value = outcome.get(name)
            if value is not None and not isinstance(value, str):
                raise ToolError(f'outcome.{name} は文字列')
        # ok が最優先。ok でなく unavailable なら「使えず」、それ以外は「不合格」
        verdict = '合格' if outcome['ok'] else ('使えず' if unavailable else '不合格')
        with RECORD_LOCK:
            base = root / 'docs' / 'pbi'
            runs = base / 'runs.jsonl'
            attempt = 1
            for line in (read_text(runs) or '').splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get('report') == report and row.get('role') == role \
                        and row.get('runner') == runner and row.get('model') == model:
                    attempt += 1
            row = {
                'schema': 1,
                'recorded_at': now_iso(),
                'repo': root.name,
                'pbi': m.group(1),
                'report': report,
                'attempt': attempt,
                'rework': int(m.group(2) or 0),
                'tier': None,
                'runner': runner,
                'model': model,
                'effort': effort or None,
                'time': {'seconds': wall},
                'tokens': token_values,
                'cost_reported': cost,
                'outcome': verdict,
                'source': 'harness2',
                'role': role,
            }
            if outcome.get('run_id'):
                row['run_id'] = outcome['run_id']
            if outcome.get('label'):
                row['label'] = outcome['label']
            if outcome.get('error'):
                row['error'] = outcome['error'][:500]
            base.mkdir(parents=True, exist_ok=True)
            # 追記は 1 行を 1 回の write で書く (同時の呼び出しで行が混ざらないように)
            with open(runs, 'a', encoding='utf-8', newline='\n') as f:
                f.write(json.dumps(row, ensure_ascii=False) + '\n')
        return {'ok': True, 'path': runs.relative_to(root).as_posix(), 'attempt': attempt}


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
         'runner': {'type': 'string', 'default': 'mcp', 'description': '取得した実行先の名前 (例 harness2)'}}}},
    {'name': 'submit_result',
     'description': '結果を docs/pbi/reports/<報告名>.md と docs/pbi/mcp/<報告名>.last.md (worker.md の 5 行) に書く。'
                    'artifacts は実行先の出力 (Harness² の summary など) をそのまま JSON で残す。',
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
     'description': '役割 (executor / improver) の段で select_model の振り分けを行い、試す順のモデルの候補と'
                    '外した手順を返す。executor では substrate と同じ実行先の手順だけ、improver では '
                    'runners (既定 ["agent"]) に入る実行先の手順だけに絞る。',
     'inputSchema': {'type': 'object', 'required': ['repo', 'report', 'role'], 'properties': {
         'repo': REPO, 'report': REPORT,
         'role': {'type': 'string', 'enum': list(ROLE_TIERS)},
         'substrate': {'type': 'string', 'description': 'executor の実行先 (例 opencode)。executor では必須'},
         'runners': {'type': 'array', 'items': {'type': 'string'},
                     'description': 'improver で使う実行先 (既定 ["agent"])'},
         'tier': {'type': 'string', 'enum': list(select_model.TIERS)}}}},
    {'name': 'run_session',
     'description': '改善役のセッションを、サーバーが選んだ実行先 (runner agent = Claude Code) で、柵 (ツールの制限・'
                    'cwd だけ・許可の質問なし・MCP なし) つきで回す。cwd はサーバーの --session-root の下だけ。'
                    '実行先が使えないときは unavailable: true を返す。',
     'inputSchema': {'type': 'object', 'required': ['cwd', 'mission'], 'properties': {
         'cwd': {'type': 'string', 'description': '作業ディレクトリの絶対パス (--session-root の下)'},
         'mission': {'type': 'string', 'description': '標準入力に渡す依頼'},
         'system': {'type': 'string', 'description': '追加のシステムプロンプト (12 万字超は標準入力の先頭に入れる)'},
         'wall': {'type': 'integer', 'default': 3600, 'description': '打ち切る秒数'},
         'runner': {'type': 'string', 'default': 'agent', 'enum': list(SESSION_RUNNERS)},
         'model': {'type': 'string'},
         'effort': {'type': 'string'}}}},
    {'name': 'record',
     'description': 'Harness² の 1 回の試行の結果 (合否・使えたか・cost・トークン・時間) を、そのリポジトリの '
                    'docs/pbi/runs.jsonl に 1 行足す。同じ report・role・runner・model の次の attempt にする。',
     'inputSchema': {'type': 'object', 'required': ['repo', 'report', 'role', 'runner', 'model', 'outcome'],
                     'properties': {
                         'repo': REPO, 'report': REPORT,
                         'role': {'type': 'string', 'enum': list(ROLE_TIERS)},
                         'runner': {'type': 'string', 'description': '実行先 (opencode / agent / dsh など)'},
                         'model': {'type': 'string'},
                         'effort': {'type': 'string', 'default': '', 'description': '空なら行では null'},
                         'outcome': {'type': 'object', 'required': ['ok'], 'properties': {
                             'ok': {'type': 'boolean', 'description': '試行が成果を出したか'},
                             'unavailable': {'type': 'boolean', 'description': '実行先が使えなかったか'},
                             'cost_usd': {'type': 'number'},
                             'tokens': {'type': 'object', 'properties': {
                                 'input': {'type': 'number'}, 'output': {'type': 'number'}}},
                             'wall_s': {'type': 'number'},
                             'error': {'type': 'string', 'description': '先頭 500 字を行に残す'},
                             'run_id': {'type': 'string'},
                             'label': {'type': 'string'}}}}}},
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
    ap.add_argument('--session-root', action='append', default=[],
                    help='run_session の cwd を許すディレクトリ (くり返し可)')
    ap.add_argument('--http', action='store_true', help='Streamable HTTP で待ち受ける (省略時は stdio)')
    ap.add_argument('--port', type=int, default=DEFAULT_PORT)
    args = ap.parse_args(argv)
    roots = roots_from(args.root)
    if not roots:
        ap.error('--root か PBI_MCP_ROOTS で、扱うリポジトリを 1 つ以上指定する')
    server = Server(Tasks(roots, session_roots_from(args.session_root)))
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
