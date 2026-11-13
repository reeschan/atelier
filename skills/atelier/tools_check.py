"""推奨の道具が入っているかを 1 行ずつ出す (appendix/setup.md、SKILL.md の Approve)。何も入れない。

  python tools_check.py [--home DIR] [--skills-dir DIR] [--probe]

列 (タブ区切り、1 行目は見出し): tool / status / install / fallback
  status   ok <場所>、missing、jevlint の no-key (キーが無いのでモデルの判定は使えず、AST だけで動く)、
           または jev の ok <読み込み元> / no-key <理由>。--probe のときだけ jev は http-<status> / network / timeout にもなる
  install  入れ方 (入れるのはユーザーが許可したときだけ)
  fallback 入れずに進むときの代わり

道具: omm (oh-my-mermaid の CLI)、omm-scan (oh-my-mermaid のプラグインのスキル)、jevlint、jev、
sac-strategic-compact、principle-test-behavior-not-implementation (pstack)、answer-me-with-any (Approve と問いを HTML で
見せる confirm のスキル。古い名前 answer-me-with-html しか無ければそれを ok にし、新しい名前で入れ直して古いほうを消すよう添える。
appendix/adapters.md の `## Confirm`)。
jevlint は agents.find_cli で探し (home の agents.json の tools.jevlint.path、PATH、fnm・nvm の Node の版)、
`jevlint auth list` に typesafe があるかを見る (キーの値は出ない)。流すときは見つけた CLI のディレクトリを PATH の先頭に置く
(agents.cli_env)。シェルで `jevlint` や `which jevlint` を叩いた結果は、PATH の Node の版しだいなので有無の根拠にしない。
jev は既定ではキーの有無と読み込み元だけを見る (env か資格情報ファイルのパス、失敗ならその理由)。通信しない。
--probe を付けたときだけ GET https://api.typesafe.ai/v1/models で疎通を確認する。キーの値はどの列にも出さない。
プラグインの置き場は PBI_PLUGINS_DIR、無ければ ~/.claude/plugins。スキルの置き場は --skills-dir、無ければ ~/.claude/skills。
終了コード: 0 (無い道具があっても 0)。
"""
import argparse
import importlib.util
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import agents as agents_module
import common
import flow

# Windows で子のコンソールのウインドウを出さない (ヘッドレス)。ほかの OS では 0
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

HEADER = 'tool\tstatus\tinstall\tfallback'
# confirm のスキルの出所 (repo と、設計で合わせた fork の main)
CONFIRM_REPO = 'https://github.com/reeschan/answer-me-with-any'
CONFIRM_COMMIT = 'bd70c602d794'
INSTALL = {
    'omm': 'npm install -g oh-my-mermaid',
    'omm-scan': '/plugin marketplace add oh-my-mermaid/oh-my-mermaid, then /plugin install oh-my-mermaid@oh-my-mermaid',
    'jevlint': 'fnm install 26.12.1; npm install -g @indxjp/jevlint (with that Node first in PATH); '
               'jevlint auth set typesafe',
    'jev': 'set TYPESAFE_API_KEY, or write {"api_key": "<key>"} to ~/.config/typesafe/credentials.json '
           '(or TYPESAFE_CREDENTIALS_FILE). Never print the key',
    'sac-strategic-compact': 'https://github.com/reeschan/sac-strategic-compact (into ~/.claude/skills/sac-strategic-compact)',
    'principle-test-behavior-not-implementation': '/plugin install pstack@cursor-plugins',
    'answer-me-with-any': f'git clone {CONFIRM_REPO} and check out {CONFIRM_COMMIT}, then copy its skills/answer-me-with-any '
                          'to ~/.claude/skills/answer-me-with-any (only after the user says yes)',
}
FALLBACK = {
    'omm': 'map: prompts/map-fallback.md',
    'omm-scan': 'map: prompts/map-fallback.md',
    'jevlint': 'lint-survey and lint-recheck: prompts/lint-fallback.md',
    'jev': 'route and gates: prepare.py uses facts only (score 1) and records the failure code',
    'sac-strategic-compact': 'appendix/kiln.md and prompts/kiln-chamber.md',
    'principle-test-behavior-not-implementation': 'appendix/tests-at-end.md, Test principle (fallback)',
    'answer-me-with-any': 'confirm: the same Markdown in chat (appendix/adapters.md, Confirm)',
}


# jevlint の --version と auth list を待つ上限 (秒)。Start で毎回流すので、止まった CLI で全体を止めない
TIMEOUT = 30
# --probe の GET /v1/models。既定の実行は通信しない
MODELS_URL = 'https://api.typesafe.ai/v1/models'
PROBE_TIMEOUT = 10


def jev_module():
    """同梱の jev.py。ディレクトリ名 jev とぶつからないように別のモジュール名で読む。"""
    name = 'atelier_jev_client'
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    path = Path(__file__).resolve().parent / 'jev' / 'jev.py'
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """別のホストへ Authorization を送らない。リダイレクトは HTTP ステータスとして扱う。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _probe_timed_out(exc) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    return isinstance(exc, urllib.error.URLError) and isinstance(getattr(exc, 'reason', None), TimeoutError)


def probe_models(api_key: str, url: str = MODELS_URL, timeout: float = PROBE_TIMEOUT) -> str:
    """GET /v1/models。戻りは ok、http-<status>、network、timeout。応答本文もキーも返さない。"""
    req = urllib.request.Request(url, method='GET', headers={'Authorization': f'Bearer {api_key}'})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as res:
            res.read(64)
        return 'ok'
    except urllib.error.HTTPError as exc:
        return f'http-{exc.code}'
    except Exception as exc:  # noqa: BLE001  接続できない・タイムアウト
        if _probe_timed_out(exc):
            return 'timeout'
        return 'network'


def _cell(text: str) -> str:
    return ' '.join(str(text).split())


def jev_status(env, probe: bool = False, models_url: str = MODELS_URL) -> str:
    """キーの有無と読み込み元。probe のときだけ疎通を足す。キーの値は含めない。"""
    found = jev_module().resolve_api_key(env)
    where = _cell(found.where)
    if found.key and found.key in where:
        where = where.replace(found.key, '[redacted]')
    if not found.key:
        return f'no-key {where}'
    if not probe:
        return f'ok {where}'
    result = probe_models(found.key, models_url)
    if result != 'ok' and found.key in result:
        result = result.replace(found.key, '[redacted]')
    if result == 'ok':
        return f'ok {where}'
    return f'{result} {where}'


def run(command: list[str], args: list[str]) -> subprocess.CompletedProcess | None:
    """コマンドを流す。CLI のディレクトリを PATH の先頭に置く (lint_run.py と同じ)。上限の時間を超えたら None。"""
    try:
        return subprocess.run([*command, *args], capture_output=True, text=True, encoding='utf-8',
                              env=dict(agents_module.cli_env(command[-1], os.environ), NO_COLOR='1', FORCE_COLOR='0'),
                              timeout=TIMEOUT, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return None


def jevlint_status(home: Path) -> str:
    agents = common.load_json(home / 'agents.json', {})
    found = agents_module.find_cli('jevlint', agents if isinstance(agents, dict) else {}, os.environ)
    if not found:
        return 'missing'
    command = [sys.executable, found] if found.endswith('.py') else [found]
    version = run(command, ['--version'])
    if version is None or version.returncode != 0:
        return f'missing (does not run: {found})'
    keys = run(command, ['auth', 'list'])
    label = f'{found} {version.stdout.strip()}'
    if keys is None or keys.returncode != 0 or 'typesafe' not in keys.stdout.lower():
        return f'no-key {label}'
    return f'ok {label}'


def rows(home: Path, skills_dir: Path, env, probe: bool = False) -> list[tuple]:
    agents = common.load_json(home / 'agents.json', {})
    omm = agents_module.find_cli('omm', agents if isinstance(agents, dict) else {}, env)
    plugins = flow.plugins_root(env)
    scan = flow.find_plugin_file('oh-my-mermaid', 'skills/omm-scan/SKILL.md', plugins)
    sac = skills_dir / 'sac-strategic-compact' / 'SKILL.md'
    principle = flow.find_plugin_file('pstack', 'skills/principle-test-behavior-not-implementation/SKILL.md', plugins)
    confirm = flow.find_skill_file(agents_module.CONFIRM_SKILL, skills_dir, env)
    old = None if confirm else flow.find_skill_file(agents_module.CONFIRM_SKILL_OLD, skills_dir, env)
    status = {
        'omm': f'ok {omm}' if omm else 'missing',
        'omm-scan': f'ok {scan}' if scan else 'missing',
        'jevlint': jevlint_status(home),
        'jev': jev_status(env, probe=probe),
        'sac-strategic-compact': f'ok {sac}' if sac.is_file() else 'missing',
        'principle-test-behavior-not-implementation': f'ok {principle}' if principle else 'missing',
        'answer-me-with-any': (f'ok {confirm}' if confirm else
                               f'ok {old} (old name {agents_module.CONFIRM_SKILL_OLD}; install '
                               f'{agents_module.CONFIRM_SKILL} and remove it)' if old else 'missing'),
    }
    return [(tool, status[tool], INSTALL[tool], FALLBACK[tool]) for tool in INSTALL]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--home')
    ap.add_argument('--skills-dir')
    ap.add_argument('--probe', action='store_true',
                    help='jev だけ GET /v1/models で疎通を確認する（既定はキーの有無と読み込み元だけ）')
    args = ap.parse_args(argv)
    common.utf8_stdout()
    home = common.resolve_home(args.home)
    skills = Path(args.skills_dir) if args.skills_dir else Path.home() / '.claude' / 'skills'
    print(HEADER)
    for cells in rows(home, skills, os.environ, probe=args.probe):
        print('\t'.join(cells))
    return 0


if __name__ == '__main__':
    sys.exit(main())
