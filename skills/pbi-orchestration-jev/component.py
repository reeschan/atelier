"""入れたスキルを外部部品 (home の components/) として登録し、テンプレートに差す (docs/specs/atelier-workflow.md)。

  python component.py add <skill> --stage <stage> [--after <stage>] [--workflow NAME] [--when W] [--where W]
                          [--memory M] [--forbid TEXT ...] [--home DIR]
  python component.py remove <id> [--workflow NAME] [--home DIR]
  python component.py list [--home DIR]

このスキルは他の人のスキルも、その部品の定義も同梱しない。スキル自体は Claude Code のプラグインのコマンド
(`/plugin marketplace add ...`・`/plugin install ...`) で利用者が入れ、このコマンドが入ったスキルの SKILL.md を
読んで部品の定義を home に書く。

<skill> は `<plugin>:<name>` (プラグインのスキル) か `<name>` (~/.claude/skills/<name>/ の自分のスキル)。
プラグインは PBI_PLUGINS_DIR、無ければ ~/.claude/plugins の下で <plugin>/ (か <plugin>/<版>/) の skills/ から
<name>/SKILL.md を探す (分類のフォルダを挟んでもよい)。自分のスキルは PBI_SKILLS_DIR、無ければ ~/.claude/skills の下を見る。
SKILL.md の frontmatter が disable-model-invocation: true なら read-skill の部品 (サブエージェントに SKILL.md を
読ませて従わせる)、そうでなければ skill の部品 (Skill ツールで呼ぶ)。ライセンスとリンクはプラグインの
.claude-plugin/plugin.json (license・repository か homepage) から取る。

add は <home>/components/<stage>.<plugin>-<name>.json (関所は gate.…、自分のスキルは <stage>.<name>.json) を書き、--workflow のテンプレート
(既定 jev-routed) に差す。home にそのテンプレートが無ければ同梱のものを写してから変える。工程では先頭
(足した部品をいちばん先に試す) に、関所では --after の工程の関所の最後に入れる。書いたあと flow.py と同じ検証をして、誤りがあれば
何も残さず終了コード 2 で止める。remove は部品を home とテンプレートから外す。同梱の部品は消せない。
"""
import argparse
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import common  # noqa: E402
import flow  # noqa: E402

# 外部部品を差してよい工程。route は Jev、build はどのテンプレートでも worker.md なので差さない
STAGE_DOCS = {
    'understand': 'appendix/design.md',
    'design': 'appendix/design.md',
    'plan': 'appendix/plan.md',
    'verify': 'appendix/verify.md',
    'review': 'appendix/review.md',
    'fix': 'appendix/rework.md',
    'finish': 'appendix/finish.md',
}
# 関所はどの工程のあとにも置ける。手順は read-skill なら engine.md、ほかはその工程の doc
GATE_DOCS = dict(STAGE_DOCS, route='appendix/engine.md', build='appendix/verify.md')
# レビューの部品に渡さない引数 (報告だけをさせる。appendix/review.md)
REVIEW_FORBID = ['--fix', '--comment', 'ultra', '--max-findings']
# read-skill の部品はサブエージェントで動かし、出力のファイルだけを書かせる (appendix/engine.md)
READ_SKILL_FORBID = ['do not change any file except the output file named in the prompt']
SKILL_NAME = re.compile(r'^(?:([A-Za-z0-9][A-Za-z0-9_.-]{0,63}):)?([A-Za-z0-9][A-Za-z0-9_.-]{0,63})$')


def skills_root(env) -> Path:
    return Path(env['PBI_SKILLS_DIR']) if env.get('PBI_SKILLS_DIR') else Path.home() / '.claude' / 'skills'


def find_plugin_skill(plugin: str, name: str, root: Path) -> tuple[Path | None, str]:
    """root の下の <plugin>/ か <plugin>/<版>/ の skills/ から、<name>/SKILL.md をいちばん新しいものから探す。

    (SKILL.md の場所, プラグインの根からの相対パス)。分類のフォルダを挟む構成 (mattpocock-skills の
    skills/productivity/grilling/SKILL.md など) もあるので、skills/ の下を深さを問わず見る。無ければ (None, '')。
    """
    found = []
    root = Path(root)
    if not root.is_dir():
        return None, ''
    for current, dirs, _files in os.walk(root):
        if len(Path(current).relative_to(root).parts) >= flow.PLUGIN_SEARCH_DEPTH:
            dirs[:] = []
        here = Path(current)
        if here.name != plugin:
            continue
        for base in [here, *(d for d in here.iterdir() if d.is_dir())]:
            if (base / 'skills').is_dir():
                found += [(p, base) for p in (base / 'skills').rglob('SKILL.md') if p.parent.name == name]
    if not found:
        return None, ''
    path, base = max(found, key=lambda pair: pair[0].stat().st_mtime)
    return path, path.relative_to(base).as_posix()


def find_skill(skill: str, env) -> tuple[Path | None, str | None, str, str]:
    """(SKILL.md の場所, プラグイン名か None, スキル名, プラグインの根からの相対パス)。見つからなければ場所は None。"""
    m = SKILL_NAME.match(skill)
    if not m:
        return None, None, '', ''
    plugin, name = m.groups()
    if plugin:
        path, rel = find_plugin_skill(plugin, name, flow.plugins_root(env))
        return path, plugin, name, rel
    path = skills_root(env) / name / 'SKILL.md'
    return (path if path.is_file() else None), None, name, ''


def frontmatter(text: str) -> dict:
    """SKILL.md の先頭の --- で囲んだ部分の `key: value` (1 行のものだけ)。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != '---':
        return {}
    found = {}
    for line in lines[1:]:
        if line.strip() == '---':
            break
        key, sep, value = line.partition(':')
        if sep and key and not key.startswith((' ', '\t')):
            found[key.strip()] = value.strip().strip('"\'')
    return found


def plugin_meta(skill_md: Path) -> dict:
    """SKILL.md から上にたどって最初に見つかった .claude-plugin/plugin.json。無ければ {}。"""
    for parent in skill_md.parents:
        data = common.load_json(parent / '.claude-plugin' / 'plugin.json')
        if isinstance(data, dict):
            return data
    return {}


def link_of(meta: dict) -> str:
    for key in ('repository', 'homepage'):
        value = meta.get(key)
        if isinstance(value, dict):
            value = value.get('url')
        if isinstance(value, str) and value.startswith('https://'):
            return value
    return ''


def component_name(plugin: str | None, name: str) -> str:
    """部品の id の後ろ半分。プラグインのスキルは <plugin>-<name> (同じ名前なら <plugin>)、自分のスキルは <name>。

    built-in の code-review と同じ名前のスキルがあっても、同梱の id とぶつからないようにするため。
    """
    if not plugin or plugin == name:
        return plugin or name
    return f'{plugin}-{name}'


def build_component(skill: str, skill_md: Path, plugin: str | None, name: str, rel: str, args) -> dict:
    """入ったスキルから部品の定義を作る。"""
    front = frontmatter(skill_md.read_text(encoding='utf-8', errors='replace'))
    read_skill = front.get('disable-model-invocation', '').lower() == 'true'
    if read_skill and not plugin:
        raise ValueError(f'{skill}: cannot be invoked by the model and is not a plugin skill')
    stage = args.stage
    role = 'gate' if stage == 'gate' else stage
    doc = 'appendix/engine.md' if read_skill else (GATE_DOCS[args.after] if stage == 'gate' else STAGE_DOCS[stage])
    interactive = role in ('understand', 'design') and not read_skill
    where = args.where or ('subagent-readonly' if read_skill else 'main' if interactive else 'subagent')
    memory = args.memory or ('full' if where == 'main' else 'none')
    meta = plugin_meta(skill_md) if plugin else {}
    title = (front.get('description') or skill).split('. ')[0][:120]
    return {
        'id': f'{role}.{component_name(plugin, name)}',
        'stages': [role],
        'title': {'ja': title, 'en': title},
        'invoke': {'kind': 'read-skill', 'plugin': plugin, 'path': rel} if read_skill
        else {'kind': 'skill', 'name': skill},
        'doc': doc,
        'where': where,
        'memory': memory,
        'when': args.when or 'always',
        'cost': 'mid',
        'requires': {'plugins': [plugin]} if read_skill else {'skills': [skill]},
        'forbid': (REVIEW_FORBID if role == 'review' or (role == 'gate' and args.after == 'review') else [])
        + (READ_SKILL_FORBID if read_skill else []) + list(args.forbid or []),
        'interactive': interactive,
        'calls_back': False,
        'license': meta.get('license', '') if isinstance(meta.get('license'), str) else '',
        'link': link_of(meta),
        'source': skill,
    }


def load_workflow(home: Path, name: str) -> tuple[Path, dict]:
    """home のテンプレート (無ければ同梱のものの写し) を返す。どちらも無ければ ValueError。"""
    path = home / 'workflows' / f'{name}.json'
    data = common.load_json(path)
    if data is None:
        data = common.load_json(flow.BUNDLED_WORKFLOWS / f'{name}.json')
    if not isinstance(data, dict):
        raise ValueError(f'workflow not found: {name}')
    return path, data


def insert(workflow: dict, cid: str, stage: str, after: str | None) -> None:
    """工程なら先頭、関所なら after の工程の関所の最後に差す (もう入っていれば何もしない)。"""
    if stage == 'gate':
        gates = workflow.setdefault('gates', [])
        gate = next((g for g in gates if isinstance(g, dict) and g.get('after') == after), None)
        if gate is None:
            gate = {'after': after, 'components': []}
            order = flow.STAGES.index(after)
            at = next((i for i, g in enumerate(gates)
                       if isinstance(g, dict) and g.get('after') in flow.STAGES
                       and flow.STAGES.index(g['after']) > order), len(gates))
            gates.insert(at, gate)
        if cid not in [flow.entry(item)[0] for item in gate['components']]:
            gate['components'].append(cid)
        return
    items = workflow['stages'][stage]
    if cid not in [flow.entry(item)[0] for item in items]:
        items.insert(0, cid)


def take_out(workflow: dict, cid: str) -> None:
    for stage, items in workflow.get('stages', {}).items():
        workflow['stages'][stage] = [item for item in items if flow.entry(item)[0] != cid]
    gates = []
    for gate in workflow.get('gates', []):
        gate['components'] = [item for item in gate.get('components', []) if flow.entry(item)[0] != cid]
        if gate['components']:
            gates.append(gate)
    workflow['gates'] = gates


def check(home: Path, workflow: dict) -> list[str]:
    components, errors = flow.load_components(home=home)
    return errors + flow.workflow_errors(workflow, components)


def save_checked(home: Path, wf_path: Path, workflow: dict, comp_path: Path | None, component: dict | None) -> list[str]:
    """部品とテンプレートを書き、検証に通らなければ元に戻して誤りを返す。"""
    backups = {p: p.read_bytes() if p.is_file() else None for p in (wf_path, comp_path) if p is not None}
    if comp_path is not None:
        common.write_json(comp_path, component)
    common.write_json(wf_path, workflow)
    errors = check(home, workflow)
    if errors:
        for path, data in backups.items():
            if data is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(data)
    return errors


def cmd_add(args, home: Path) -> int:
    if args.stage == 'gate' and args.after not in flow.STAGES:
        print('--stage gate needs --after <stage>', file=sys.stderr)
        return 2
    if args.stage != 'gate' and args.after:
        print('--after is only for --stage gate', file=sys.stderr)
        return 2
    skill_md, plugin, name, rel = find_skill(args.skill, os.environ)
    if skill_md is None:
        hint = f'install the plugin first (/plugin install {plugin}@<marketplace>)' if plugin else \
            'put it in ~/.claude/skills/<name>/ or use <plugin>:<name>'
        print(f'{args.skill}: skill not found; {hint}', file=sys.stderr)
        return 2
    try:
        component = build_component(args.skill, skill_md, plugin, name, rel, args)
        wf_path, workflow = load_workflow(home, args.workflow)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    bundled, _ = flow.load_components()
    if component['id'] in bundled:
        print(f'{component["id"]}: a bundled component already has this id', file=sys.stderr)
        return 2
    insert(workflow, component['id'], args.stage, args.after)
    comp_path = home / 'components' / f'{component["id"]}.json'
    errors = save_checked(home, wf_path, workflow, comp_path, component)
    if errors:
        for error in errors:
            print(f'error {error}')
        return 2
    kind = component['invoke']['kind']
    place = f'gate after {args.after}' if args.stage == 'gate' else args.stage
    print(f'added {component["id"]} ({kind}, {place}) to {common.shown(wf_path)}')
    print(f'component {common.shown(comp_path)}')
    return 0


def cmd_remove(args, home: Path) -> int:
    bundled, _ = flow.load_components()
    if args.id in bundled:
        print(f'{args.id}: a bundled component cannot be removed', file=sys.stderr)
        return 2
    comp_path = home / 'components' / f'{args.id}.json'
    try:
        wf_path, workflow = load_workflow(home, args.workflow)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    take_out(workflow, args.id)
    if wf_path.is_file():
        common.write_json(wf_path, workflow)
    comp_path.unlink(missing_ok=True)
    # 同じ部品を使う別のテンプレートがあれば、ここで誤りになる
    errors = check(home, workflow)
    for error in errors:
        print(f'error {error}')
    print(f'removed {args.id} from {common.shown(wf_path)}')
    return 2 if errors else 0


def cmd_list(home: Path) -> int:
    components, errors = flow.load_components(home=home)
    bundled, _ = flow.load_components()
    print('id\tsource\tinvoke\tstages\tlink')
    for cid, c in sorted(components.items()):
        source = 'bundled' if cid in bundled else c.get('source', 'home')
        name = c['invoke'].get('name') or c['invoke'].get('plugin') or c['invoke']['kind']
        print('\t'.join([cid, source, name, ','.join(c['stages']), c.get('link', '')]))
    for error in errors:
        print(f'error {error}')
    return 2 if errors else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--home', help='components/ と workflows/ を置く home (common.resolve_home)')
    sub = ap.add_subparsers(dest='command', required=True)
    add = sub.add_parser('add', help='入れたスキルを外部部品として登録し、テンプレートに差す')
    add.add_argument('skill', help='<plugin>:<name> か、~/.claude/skills の <name>')
    add.add_argument('--stage', required=True, choices=sorted(STAGE_DOCS) + ['gate'])
    add.add_argument('--after', choices=flow.STAGES, help='--stage gate のとき、どの工程のあとか')
    add.add_argument('--workflow', default=flow.DEFAULT_WORKFLOW, help='差すテンプレート (既定 jev-routed)')
    add.add_argument('--when', choices=flow.WHEN)
    add.add_argument('--where', choices=flow.WHERE)
    add.add_argument('--memory', choices=flow.MEMORY)
    add.add_argument('--forbid', action='append', help='渡さない引数や飛ばす手順 (繰り返せる)')
    remove = sub.add_parser('remove', help='外部部品を home とテンプレートから外す')
    remove.add_argument('id')
    remove.add_argument('--workflow', default=flow.DEFAULT_WORKFLOW)
    sub.add_parser('list', help='同梱と home の部品の一覧')
    args = ap.parse_args(argv)
    common.utf8_stdout()
    home = common.resolve_home(args.home)
    if args.command == 'add':
        return cmd_add(args, home)
    if args.command == 'remove':
        return cmd_remove(args, home)
    return cmd_list(home)


if __name__ == '__main__':
    sys.exit(main())
