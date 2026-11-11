"""役割 (設計者・レビュー担当・テスト担当) を動かす実行先の、使えるかの判定と CLI の組み立て。

conductor.py (設計者) と role_run.py (レビュー担当・テスト担当) が使う。

- 実行先の CLI は、環境変数 ATELIER_<RUNNER>_CMD (JSON の文字列の配列。テストの差し替え口)、claude なら PBI_CLAUDE_BIN、
  home の agents.json の runners.<runner>.path、PATH の <runner> の順に探す。
- 使えるかは、agents.json で enabled が true で、CLI が見つかること。
- codex は `-a never exec --json -s <サンドボックス>` で起こし、shared-memory の道具の承認を `-c` で自動にする
  (ユーザーの ~/.codex/config.toml は書き換えない)。
"""
import json
import os
import re
import shutil
from pathlib import Path

import common

# 役割に置ける実行先。設計者は Agent ツールか claude -p か codex で動くものだけ
ROLE_RUNNERS = ('agent', 'opencode', 'codex', 'agy', 'dsh', 'claude')
DESIGNER_RUNNERS = ('agent', 'codex', 'claude')
CODEX_SANDBOXES = ('read-only', 'workspace-write', 'danger-full-access')
# 既定のサンドボックス。workspace-write はネットワークとワークスペースの外への書き込みを塞ぐので、
# dsh の作業者・入れ子の codex・ユーザーの道具 (fnm の Node・pnpm) が動かない
CODEX_DEFAULT_SANDBOX = 'danger-full-access'
CODEX_MCP_APPROVAL = 'mcp_servers.shared-memory.default_tools_approval_mode="auto"'
# codex がファイルを書くときの決まり。codex のシェルは Windows PowerShell 5.1 で、外のコマンドへ渡す文字を
# $OutputEncoding (既定 US-ASCII) で符号化するので、日本語が ? (0x3F) になる。設計者の依頼文と role_run.py が
# 渡し、runners.md の codex の節にも同じ文がある (test_role_run が一致を確かめる)
CODEX_TEXT_RULE = "On Windows, Windows PowerShell 5.1 sends text piped into another program (`@'...'@ | python -`) as US-ASCII, so every non-ASCII character arrives as `?`. Write files with apply_patch, or write a script to a `.py` file saved as UTF-8 (apply_patch, or `Set-Content -Encoding utf8`; Windows PowerShell 5.1 saves in the ANSI code page by default) and run `python <file>`. Keep non-ASCII text out of PowerShell pipes; when you need one, run `$OutputEncoding = [System.Text.UTF8Encoding]::new($false)` first in the same command."


def runners_for(part: str) -> tuple:
    """役割の部分 (designer・reviewer.deep・reviewer.other・tester) に置ける実行先。"""
    return DESIGNER_RUNNERS if part == 'designer' else ROLE_RUNNERS


def _agents(home=None) -> dict:
    data = common.load_json(common.resolve_home(home) / 'agents.json', {})
    return data if isinstance(data, dict) else {}


def runner_entry(runner: str, home=None) -> dict:
    runners = _agents(home).get('runners')
    entry = runners.get(runner) if isinstance(runners, dict) else None
    return entry if isinstance(entry, dict) else {}


def find_command(runner: str, home=None):
    """実行先の CLI のコマンドの先頭。(コマンド, 見つからない理由) を返し、見つかれば理由は None。"""
    given = os.environ.get(f'ATELIER_{runner.upper()}_CMD')
    if given:
        try:
            words = json.loads(given)
        except ValueError:
            words = None
        if isinstance(words, list) and words and all(isinstance(w, str) and w.strip() for w in words):
            return list(words), codex_home_problem() if runner == 'codex' else None
        return None, f'ATELIER_{runner.upper()}_CMD is not a JSON array of strings'
    if runner == 'claude' and os.environ.get('PBI_CLAUDE_BIN'):
        # claude_run.py と同じ場所の指定 (作業者の claude と同じ実行ファイルを使う)
        configured = os.environ['PBI_CLAUDE_BIN']
        if Path(configured).is_file():
            return [configured], None
        return None, f'claude not found (PBI_CLAUDE_BIN {configured})'
    path = runner_entry(runner, home).get('path')
    if isinstance(path, str) and path and Path(path).is_file():
        return [path], codex_home_problem() if runner == 'codex' else None
    found = shutil.which(runner)
    if found:
        return [found], codex_home_problem() if runner == 'codex' else None
    where = f' (agents.json path {path})' if isinstance(path, str) and path else ''
    return None, f'{runner} not found{where}'


def unusable_reason(runner: str, home=None):
    """agent 以外の実行先が使えない理由。使えれば None。"""
    if runner == 'agent':
        return None
    if runner_entry(runner, home).get('enabled') is not True:
        return f'{runner} is disabled in agents.json (setup)'
    return find_command(runner, home)[1]


def model_listed(runner: str, model: str, home=None):
    """モデルが home の models/<runner>.json の一覧にあるか。一覧が無ければ None。"""
    data = common.load_json(common.resolve_home(home) / 'models' / f'{runner}.json', {})
    models = data.get('models') if isinstance(data, dict) else None
    if not isinstance(models, list) or not models:
        return None
    ids = {m.get('id') if isinstance(m, dict) else m for m in models}
    return model in ids


def codex_home() -> Path:
    """codex が設定とサインインを読む場所 (CODEX_HOME、無ければ ~/.codex)。"""
    given = os.environ.get('CODEX_HOME')
    return Path(given) if given else Path.home() / '.codex'


def codex_home_problem():
    """CODEX_HOME が指す場所にサインインが無ければ、その理由。問題が無ければ None。

    空のフォルダを CODEX_HOME にすると、codex は設定もサインインも無いまま起動して落ちる。
    """
    given = os.environ.get('CODEX_HOME')
    if not given or os.environ.get('OPENAI_API_KEY') or (Path(given) / 'auth.json').is_file():
        return None
    return (f'CODEX_HOME {given} has no sign-in (auth.json); leave CODEX_HOME unset so codex uses its own home')


MCP_TABLE = re.compile(r'^\s*\[mcp_servers\.(?:"shared-memory"|\'shared-memory\'|shared-memory)\]', re.M)


def codex_has_shared_memory() -> bool:
    """codex の config.toml に shared-memory の MCP があるか。無いのに承認の -c を渡すと、中身の無い表ができて落ちる。"""
    try:
        text = (codex_home() / 'config.toml').read_text(encoding='utf-8', errors='replace')
    except OSError:
        return False
    return bool(MCP_TABLE.search(text))


def codex_sandbox(permission_mode) -> str:
    return permission_mode if permission_mode in CODEX_SANDBOXES else CODEX_DEFAULT_SANDBOX


def codex_args(model: str, effort, sandbox: str, last_path) -> list:
    """codex の実行ファイルのあとに付ける引数。依頼は標準入力 (末尾の `-`) で渡す。"""
    args = ['-a', 'never', 'exec', '--skip-git-repo-check', '--json', '-m', model, '-s', sandbox]
    if codex_has_shared_memory():
        args += ['-c', CODEX_MCP_APPROVAL]
    if effort:
        # TOML の基本文字列として書く (\ などをエスケープし、設定した値のまま渡す。受けない値は codex が断る)
        quoted = json.dumps(str(effort), ensure_ascii=False).replace('\x7f', '\\u007f')
        args += ['-c', f'model_reasoning_effort={quoted}']
    return args + ['-o', Path(last_path).as_posix(), '-']


def codex_events(path) -> dict:
    """codex exec --json の JSONL から、thread_id・使用量・失敗の理由を拾う。読めない行は飛ばす。"""
    got = {'session_id': None, 'tokens': None, 'failed': None}
    try:
        lines = Path(path).read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return got
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get('type')
        if kind == 'thread.started' and isinstance(event.get('thread_id'), str):
            got['session_id'] = event['thread_id']
        elif kind == 'turn.completed' and isinstance(event.get('usage'), dict):
            usage = event['usage']
            got['tokens'] = {'input': usage.get('input_tokens'), 'cached_input': usage.get('cached_input_tokens'),
                             'output': usage.get('output_tokens')}
        elif kind in ('turn.failed', 'error'):
            error = event.get('error') if isinstance(event.get('error'), dict) else {}
            got['failed'] = str(error.get('message') or event.get('message') or kind)
    return got
