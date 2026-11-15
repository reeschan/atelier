"""役割 (設計者・レビュー担当・テスト担当) を動かす実行先の、使えるかの判定と CLI の組み立て。

conductor.py (設計者) と role_run.py (レビュー担当・テスト担当) が使う。

- 実行先の CLI は、環境変数 ATELIER_<RUNNER>_CMD (JSON の文字列の配列。テストの差し替え口)、claude なら PBI_CLAUDE_BIN、
  home の agents.json の runners.<runner>.path、PATH の <runner> の順に探す。
- 使えるかは、agents.json で enabled が true で、CLI が見つかること。
- codex は `-a never exec --json -s <サンドボックス>` で起こし、shared-memory の道具の承認を `-c` で自動にする
  (ユーザーの ~/.codex/config.toml は書き換えない)。workspace-write では、ネットワークと足された書き込み先を `-c` で
  その回だけ閉じ、Windows では MXC を選ぶ `-c` も渡す (codex_sandbox_args)。
- サンドボックスは、作業者と役割 (レビュー担当・テスト担当) が既定で workspace-write、設計者が既定で danger-full-access。
  作業者と役割を danger-full-access に戻すのは、home の agents.json の runners.codex.sandbox で明示したときだけ
  (codex_worker_sandbox)。
"""
import json
import os
import re
import shlex
import shutil
import sys
from pathlib import Path

import common

# 役割に置ける実行先。設計者は Agent ツールか claude -p か codex で動くものだけ
ROLE_RUNNERS = ('agent', 'opencode', 'codex', 'agy', 'dsh', 'claude')
DESIGNER_RUNNERS = ('agent', 'codex', 'claude')
CODEX_SANDBOXES = ('read-only', 'workspace-write', 'danger-full-access')
# 作業者と役割の既定。作業ツリーの外への書き込みとネットワークを塞ぐ。作業者は作業ツリーの中だけを書き
# (記録は設計者が書き、コミットもしない)、verify のコマンド (pnpm・Biome・node --test) は中で動く
# (2026-11-15、codex-cli 0.165、Windows 11 の MXC で確かめた)。
# ネットワークは開けない: 依存を足す pnpm add は、ネットワークを開けても store がワークスペースの外にあって
# Windows のサンドボックスでは書けず (writable_roots に足しても通らなかった)、依存を足すのは設計者の仕事になるため
CODEX_DEFAULT_SANDBOX = 'workspace-write'
# 作業者と役割の runners.codex.sandbox に置ける値。read-only では作業も結果の JSON も書けない
CODEX_WORKER_SANDBOXES = ('workspace-write', 'danger-full-access')
# 設計者の既定。設計者は作業者と役割を起こす: 入れ子の codex はサンドボックスの中ではコマンドを起こせず
# (プロセス作成エラー)、~/.config/atelier の記録・各 CLI の home (~/.codex・~/.claude など) への書き込みと
# ネットワークも要るので、サンドボックスでは回らない。permission_mode でサンドボックスの名前を指定すれば、それを使う
CODEX_DESIGNER_SANDBOX = 'danger-full-access'
# workspace-write の境界を、その回だけ決まった値にする -c。ユーザーの config.toml の
# sandbox_workspace_write.network_access = true と writable_roots は -s workspace-write でも効き残るので
# (2026-11-15 に確かめた)、ネットワークを閉じ、足された書き込み先を空にする。%TEMP% への書き込みは codex の既定のまま
CODEX_WORKSPACE_LIMITS = ('sandbox_workspace_write.network_access=false', 'sandbox_workspace_write.writable_roots=[]')
# Windows で codex が選ぶサンドボックスの実装。elevated (別の利用者 CodexSandboxOffline で動く) では
# %LOCALAPPDATA%\pnpm を読めず pnpm が動かず、unelevated では node --test が子を起こせない (spawn EPERM) うえ、
# network_access = false でもネットワークが塞がらない。MXC なら pnpm・node --test が動き、ネットワークも塞がる。
# MXC を選び、MXC を使えないときの戻り先は、ユーザーの windows.sandbox によらず elevated に決める
# (unelevated に戻るとネットワークが開くため。elevated ではネットワークが塞がることを確かめた。MXC の無い PC は試していない)
CODEX_WINDOWS_SANDBOX = ('windows.sandbox="elevated"', 'features.prefer_mxc=true')
CODEX_MCP_APPROVAL = 'mcp_servers.shared-memory.default_tools_approval_mode="auto"'
# codex がファイルを書くときの決まり。codex のシェルは Windows PowerShell 5.1 で、外のコマンドへ渡す文字を
# $OutputEncoding (既定 US-ASCII) で符号化するので、日本語が ? (0x3F) になる。設計者の依頼文と role_run.py が
# 渡し、runners.md の codex の節にも同じ文がある (test_role_run が一致を確かめる)
CODEX_TEXT_RULE = "On Windows, Windows PowerShell 5.1 sends text piped into another program (`@'...'@ | python -`) as US-ASCII, so every non-ASCII character arrives as `?`. Write files with apply_patch, or write a script to a `.py` file saved as UTF-8 (apply_patch, or `Set-Content -Encoding utf8`; Windows PowerShell 5.1 saves in the ANSI code page by default) and run `python <file>`. Keep non-ASCII text out of PowerShell pipes; when you need one, run `$OutputEncoding = [System.Text.UTF8Encoding]::new($false)` first in the same command."


def runners_for(part: str) -> tuple:
    """役割の部分 (designer・episodes.<出番>・reviewer.deep・reviewer.other・tester) に置ける実行先。

    episodes.<出番> はその出番の設計者なので、設計者と同じ実行先に限る。
    """
    return DESIGNER_RUNNERS if part == 'designer' or part.startswith('episodes.') else ROLE_RUNNERS


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
            return list(words), codex_home_problem(words) if runner == 'codex' else None
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


def codex_home_problem(words=()):
    """CODEX_HOME が設定もサインインも無いフォルダなら、その理由。問題が無ければ None。

    空のフォルダを CODEX_HOME にすると、codex は設定もサインインも無いまま起動して落ちる。サインインは auth.json の
    ほか keyring にも置けるので (config.toml か、コマンドの -c の cli_auth_credentials_store)、config.toml がある
    ホームと、コマンド (words) が置き場を選んでいるときは断らない。
    """
    given = os.environ.get('CODEX_HOME')
    if not given or os.environ.get('OPENAI_API_KEY'):
        return None
    if any('cli_auth_credentials_store' in str(word) for word in words):
        return None
    home = Path(given)
    if (home / 'auth.json').is_file() or (home / 'config.toml').is_file():
        return None
    return (f'CODEX_HOME {given} has no sign-in and no config.toml; '
            'leave CODEX_HOME unset so codex uses its own home')


MCP_TABLE = re.compile(r'^\s*\[mcp_servers\.(?:"shared-memory"|\'shared-memory\'|shared-memory)\]', re.M)


def codex_has_shared_memory() -> bool:
    """codex の config.toml に shared-memory の MCP があるか。無いのに承認の -c を渡すと、中身の無い表ができて落ちる。"""
    try:
        text = (codex_home() / 'config.toml').read_text(encoding='utf-8', errors='replace')
    except OSError:
        return False
    return bool(MCP_TABLE.search(text))


def codex_sandbox(permission_mode) -> str:
    """設計者のサンドボックス。permission_mode がサンドボックスの名前ならその値、それ以外 (既定の auto を含む) は既定。"""
    return permission_mode if permission_mode in CODEX_SANDBOXES else CODEX_DESIGNER_SANDBOX


def codex_worker_sandbox(home=None) -> tuple:
    """作業者と役割のサンドボックスと、その出所。(サンドボックス, 'default' | 'agents.json' | 'agents.json invalid')。

    home の agents.json の runners.codex.sandbox が workspace-write か danger-full-access ならその値。
    無ければ既定、ほかの値なら既定に戻して 'agents.json invalid' を返す (サンドボックスを開ける側には倒さない)。
    """
    value = runner_entry('codex', home).get('sandbox')
    if value is None:
        return CODEX_DEFAULT_SANDBOX, 'default'
    if value in CODEX_WORKER_SANDBOXES:
        return value, 'agents.json'
    return CODEX_DEFAULT_SANDBOX, 'agents.json invalid'


def codex_sandbox_args(sandbox: str, windows=None) -> list:
    """サンドボックスの引数。

    workspace-write なら、ネットワークと足された書き込み先をその回だけ閉じる -c (CODEX_WORKSPACE_LIMITS) を足す。
    サンドボックスがあれば、Windows では MXC を選ぶ -c (CODEX_WINDOWS_SANDBOX) も足す。danger-full-access は -s だけ。
    """
    if windows is None:
        windows = sys.platform == 'win32'
    args = ['-s', sandbox]
    if sandbox == 'workspace-write':
        for value in CODEX_WORKSPACE_LIMITS:
            args += ['-c', value]
    if windows and sandbox != 'danger-full-access':
        for value in CODEX_WINDOWS_SANDBOX:
            args += ['-c', value]
    return args


def shell_words(args) -> str:
    """引数を bash にそのまま貼れる 1 行にする (runners.md の起動の行に写す prepare.py の行)。"""
    return ' '.join(shlex.quote(str(word)) for word in args)


def codex_args(model: str, effort, sandbox: str, last_path, windows=None) -> list:
    """codex の実行ファイルのあとに付ける引数。依頼は標準入力 (末尾の `-`) で渡す。"""
    args = ['-a', 'never', 'exec', '--skip-git-repo-check', '--json', '-m', model,
            *codex_sandbox_args(sandbox, windows)]
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
