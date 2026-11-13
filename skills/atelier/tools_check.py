"""推奨の道具が入っているかを 1 行ずつ出す (appendix/setup.md、SKILL.md の Approve)。何も入れない。

  python tools_check.py [--home DIR] [--skills-dir DIR]

列 (タブ区切り、1 行目は見出し): tool / status / install / fallback
  status   ok <場所>、missing、または jevlint の no-key (キーが無いのでモデルの判定は使えず、AST だけで動く)。
           node は ok <場所> <版> / old <版> (20 未満) / missing (env の PATH で探す。confirm の am を流す)
  install  入れ方 (入れるのはユーザーが許可したときだけ)
  fallback 入れずに進むときの代わり

道具: omm (oh-my-mermaid の CLI)、omm-scan (oh-my-mermaid のプラグインのスキル)、jevlint、
sac-strategic-compact、principle-test-behavior-not-implementation (pstack)、answer-me-with-any (Approve と問いを HTML で
見せる confirm のスキル。古い名前 answer-me-with-html しか無ければそれを ok にし、新しい名前で入れ直して古いほうを消すよう添える。
appendix/adapters.md の `## Confirm`)。
jevlint は agents.find_cli で探し (home の agents.json の tools.jevlint.path、PATH、fnm・nvm の Node の版)、
`jevlint auth list` に typesafe があるかを見る (キーの値は出ない)。流すときは見つけた CLI のディレクトリを PATH の先頭に置く
(agents.cli_env)。シェルで `jevlint` や `which jevlint` を叩いた結果は、PATH の Node の版しだいなので有無の根拠にしない。
プラグインの置き場は PBI_PLUGINS_DIR、無ければ ~/.claude/plugins。スキルの置き場は --skills-dir、無ければ ~/.claude/skills。
終了コード: 0 (無い道具があっても 0)。
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

import adapter_gate
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
    'sac-strategic-compact': 'https://github.com/reeschan/sac-strategic-compact (into ~/.claude/skills/sac-strategic-compact)',
    'principle-test-behavior-not-implementation': '/plugin install pstack@cursor-plugins',
    'answer-me-with-any': f'git clone {CONFIRM_REPO} and check out {CONFIRM_COMMIT}, then copy its skills/answer-me-with-any '
                          'to ~/.claude/skills/answer-me-with-any (only after the user says yes)',
    'node': 'fnm install 22',
}
CONFIRM_OFF = 'confirm: off (the question file in chat, no reply check)'
FALLBACK = {
    'omm': 'map: prompts/map-fallback.md',
    'omm-scan': 'map: prompts/map-fallback.md',
    'jevlint': 'lint-survey and lint-recheck: prompts/lint-fallback.md',
    'sac-strategic-compact': 'appendix/kiln.md and prompts/kiln-chamber.md',
    'principle-test-behavior-not-implementation': 'appendix/tests-at-end.md, Test principle (fallback)',
    'answer-me-with-any': CONFIRM_OFF,
    'node': CONFIRM_OFF,
}


# jevlint の --version と auth list を待つ上限 (秒)。Start で毎回流すので、止まった CLI で全体を止めない
TIMEOUT = 30


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


def rows(home: Path, skills_dir: Path, env) -> list[tuple]:
    agents = common.load_json(home / 'agents.json', {})
    omm = agents_module.find_cli('omm', agents if isinstance(agents, dict) else {}, env)
    plugins = flow.plugins_root(env)
    scan = flow.find_plugin_file('oh-my-mermaid', 'skills/omm-scan/SKILL.md', plugins)
    sac = skills_dir / 'sac-strategic-compact' / 'SKILL.md'
    principle = flow.find_plugin_file('pstack', 'skills/principle-test-behavior-not-implementation/SKILL.md', plugins)
    # present.py と同じ探し方 (adapter_gate.find_am): SKILL.md だけで scripts/am.mjs の無い入れ方は ok にしない
    am, old = adapter_gate.find_am(skills_dir)
    confirm = flow.find_skill_file(agents_module.CONFIRM_SKILL, skills_dir, env)
    node = adapter_gate.node_status(env)
    node_row = (f"ok {node['path']} {node['version']}" if node['status'] == 'ok' else
                f"old {node['version']}" if node['status'] == 'old' else 'missing')
    status = {
        'omm': f'ok {omm}' if omm else 'missing',
        'omm-scan': f'ok {scan}' if scan else 'missing',
        'jevlint': jevlint_status(home),
        'sac-strategic-compact': f'ok {sac}' if sac.is_file() else 'missing',
        'principle-test-behavior-not-implementation': f'ok {principle}' if principle else 'missing',
        'answer-me-with-any': (f'ok {am} (old name {agents_module.CONFIRM_SKILL_OLD}; install '
                               f'{agents_module.CONFIRM_SKILL} and remove it)' if am and old else
                               f'ok {am}' if am else
                               f'missing scripts/am.mjs (found {confirm})' if confirm else 'missing'),
        'node': node_row,
    }
    return [(tool, status[tool], INSTALL[tool], FALLBACK[tool]) for tool in INSTALL]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--home')
    ap.add_argument('--skills-dir')
    args = ap.parse_args(argv)
    common.utf8_stdout()
    home = common.resolve_home(args.home)
    skills = Path(args.skills_dir) if args.skills_dir else Path.home() / '.claude' / 'skills'
    print(HEADER)
    for cells in rows(home, skills, os.environ):
        print('\t'.join(cells))
    return 0


if __name__ == '__main__':
    sys.exit(main())
