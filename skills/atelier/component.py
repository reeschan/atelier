"""入れたスキルを外部部品 (home の components/) として登録し、テンプレートに差す (docs/specs/atelier-workflow.md)。

  python component.py add <skill> --stage <stage|gate|ui-design|marketing|security> [--after <stage>] [--workflow NAME] [--when W] [--where W]
                          [--memory M] [--forbid TEXT ...] [--home DIR]
  python component.py remove <id> [--workflow NAME] [--home DIR]
  python component.py list [--home DIR]
  python component.py example list
  python component.py example check <name|path> [--home DIR]
  python component.py example use <name|path> [--as NAME] [--replace] [--home DIR]

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
(足した部品をいちばん先に試す) に、関所では --after の工程の関所の最後に、オプションの工程 (ui-design・marketing・security) では
その工程の最後に入れる (テンプレートにまだ無ければ --after か既定の場所に置く)。書いたあと flow.py と同じ検証をして、誤りがあれば
何も残さず終了コード 2 で止める。remove は部品を home とテンプレートから外す。同梱の部品は消せない。

example は、外部部品を使うテンプレートの見本 (examples/workflows/<name>.json) を扱う。見本はテンプレート (workflow) と、
それが使うスキルの入れ方と登録のしかた (skills) だけを持ち、スキルの中身も部品の定義も持たない。
check は skills ごとに、入っているか (installed / missing) と home に登録済みか (registered / not registered /
differs) を表で出し、足りないスキルを入れるコマンドを `install <コマンド>` の行で出す。足りなければ終了コード 3。
このスクリプトは何も入れない (入れるのは利用者か、利用者が承認したあとのオーケストレーター)。
use はすべてのスキルが入っていれば、各スキルを部品として home に登録し、見本のテンプレートを
<home>/workflows/<--as か見本の名前>.json に書く。足りなければ check と同じ行を出して終了コード 3。
home に同じ名前のテンプレートがあれば --replace が要る。誤りがあれば書いたものを元に戻して終了コード 2。
"""
import argparse
import json
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
# オプションの工程 (flow.OPTIONAL) の手順
OPTIONAL_DOC = 'appendix/optional.md'
# add の --stage に渡せるもの
ADD_STAGES = sorted(STAGE_DOCS) + ['gate'] + list(flow.OPTIONAL)
# レビューの部品に渡さない引数 (報告だけをさせる。appendix/review.md)
REVIEW_FORBID = ['--fix', '--comment', 'ultra', '--max-findings']
# remove に渡す部品の id (<工程か gate>.<名前>)
COMPONENT_ID = re.compile(r'^(?:' + '|'.join(flow.ROLES) + r')\.[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')
# read-skill の部品はサブエージェントで動かし、出力のファイルだけを書かせる (appendix/engine.md)
READ_SKILL_FORBID = ['do not change any file except the output file named in the prompt']
EXAMPLES = HERE / 'examples' / 'workflows'
# 見本のスキルが足りないときの終了コード (2 は誤り)
MISSING = 3
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
    doc = 'appendix/engine.md' if read_skill else (GATE_DOCS[args.after] if stage == 'gate' else
                                                    OPTIONAL_DOC if stage in flow.OPTIONAL else STAGE_DOCS[stage])
    interactive = role in ('understand', 'design') + flow.OPTIONAL and not read_skill
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
        'when': 'always',
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
    """home のテンプレート (無ければ同梱のものの写し) を返す。どちらも無ければ ValueError。

    home にあって読めないファイルは、同梱のもので上書きしないように ValueError にする。
    """
    path = home / 'workflows' / f'{name}.json'
    if path.exists():
        data = common.load_json(path)
        if not isinstance(data, dict):
            raise ValueError(f'{common.shown(path)}: not a readable workflow; fix or remove it first')
        return path, data
    data = common.load_json(flow.BUNDLED_WORKFLOWS / f'{name}.json')
    if not isinstance(data, dict):
        raise ValueError(f'workflow not found: {name}')
    return path, data


def insert(workflow: dict, cid: str, stage: str, after: str | None, when: str | None = None) -> None:
    """工程なら先頭、関所なら after の工程の関所の最後、オプションの工程ならその工程の最後に差す。

    when はテンプレートの中の候補 ({"id", "when"}) に書く。部品の定義は複数のテンプレートで共有するので、
    あるテンプレートの when が別のテンプレートの動きを変えないようにするため。もう入っていれば when だけ書き換える。
    """
    item = {'id': cid, 'when': when} if when else cid
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
        place(gate['components'], cid, item, at_end=True)
        return
    if stage in flow.OPTIONAL:
        steps = workflow.setdefault('optional', [])
        step = next((s for s in steps if isinstance(s, dict) and s.get('stage') == stage), None)
        if step is None:
            step = {'stage': stage, 'after': after, 'components': []}
            steps.append(step)
        place(step['components'], cid, item, at_end=True)
        return
    place(workflow['stages'][stage], cid, item, at_end=False)


def place(items: list, cid: str, item, at_end: bool) -> None:
    ids = [flow.entry(existing)[0] for existing in items]
    if cid in ids:
        items[ids.index(cid)] = item
    elif at_end:
        items.append(item)
    else:
        items.insert(0, item)


def take_out(workflow: dict, cid: str) -> None:
    for stage, items in workflow.get('stages', {}).items():
        workflow['stages'][stage] = [item for item in items if flow.entry(item)[0] != cid]
    gates = []
    for gate in workflow.get('gates', []):
        gate['components'] = [item for item in gate.get('components', []) if flow.entry(item)[0] != cid]
        if gate['components']:
            gates.append(gate)
    workflow['gates'] = gates
    if 'optional' in workflow:
        steps = []
        for step in workflow['optional']:
            step['components'] = [item for item in step.get('components', []) if flow.entry(item)[0] != cid]
            if step['components']:
                steps.append(step)
        workflow['optional'] = steps


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


def optional_after(workflow: dict, stage: str, after: str | None) -> str | None:
    """オプションの工程を置く after。テンプレートにもうあればその after (違う after を渡したら ValueError)、
    無ければ渡した after か既定 (flow.OPTIONAL_AFTER)。ほかの工程は after をそのまま返す。"""
    if stage not in flow.OPTIONAL:
        return after
    step = next((s for s in workflow.get('optional') or [] if isinstance(s, dict) and s.get('stage') == stage), None)
    if step is not None:
        if after and after != step.get('after'):
            raise ValueError(f'{stage} is already placed after {step.get("after")} in this workflow')
        return step.get('after')
    return after or flow.OPTIONAL_AFTER[stage]


def place_of(stage: str, after: str | None) -> str:
    """表に出す場所 (工程、`gate after <工程>`、`<オプションの工程> after <工程>`)。"""
    if stage == 'gate':
        return f'gate after {after}'
    if stage in flow.OPTIONAL:
        return f'{stage} after {after or flow.OPTIONAL_AFTER[stage]}'
    return stage


def cmd_add(args, home: Path) -> int:
    if args.stage == 'gate' and args.after not in flow.STAGES:
        print('--stage gate needs --after <stage>', file=sys.stderr)
        return 2
    if args.stage in flow.OPTIONAL and args.after == 'finish':
        print('an optional stage cannot run after finish (its files would miss the completion report)', file=sys.stderr)
        return 2
    if args.stage not in ('gate',) + flow.OPTIONAL and args.after:
        print('--after is only for --stage gate or an optional stage', file=sys.stderr)
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
    comp_path = home / 'components' / f'{component["id"]}.json'
    existing = common.load_json(comp_path)
    if comp_path.exists() and existing != component:
        print(f'{component["id"]}: already registered with other settings ({common.shown(comp_path)}); '
              'remove it from every workflow first', file=sys.stderr)
        return 2
    try:
        args.after = optional_after(workflow, args.stage, args.after)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    insert(workflow, component['id'], args.stage, args.after, args.when)
    errors = save_checked(home, wf_path, workflow, comp_path, component)
    if errors:
        for error in errors:
            print(f'error {error}')
        return 2
    kind = component['invoke']['kind']
    place = place_of(args.stage, args.after)
    print(f'added {component["id"]} ({kind}, {place}) to {common.shown(wf_path)}')
    print(f'component {common.shown(comp_path)}')
    return 0


def cmd_remove(args, home: Path) -> int:
    # id はファイル名になるので、形を確かめてから components/ の直下に限る (../ で別のファイルを消さない)
    if not COMPONENT_ID.match(args.id):
        print(f'{args.id}: not a component id (<stage>.<name>)', file=sys.stderr)
        return 2
    bundled, _ = flow.load_components()
    if args.id in bundled:
        print(f'{args.id}: a bundled component cannot be removed', file=sys.stderr)
        return 2
    components_dir = (home / 'components').resolve()
    comp_path = (components_dir / f'{args.id}.json').resolve()
    if comp_path.parent != components_dir:
        print(f'{args.id}: not a component id (<stage>.<name>)', file=sys.stderr)
        return 2
    try:
        wf_path, workflow = load_workflow(home, args.workflow)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    take_out(workflow, args.id)
    # ほかの home のテンプレートがまだ使っていれば、何も変えずに止める
    others = sorted(p.stem for p in (home / 'workflows').glob('*.json')
                    if p.resolve() != wf_path.resolve() and uses(common.load_json(p), args.id))
    if others:
        print(f'{args.id}: still used by workflows {", ".join(others)}; remove it there first', file=sys.stderr)
        return 2
    errors = check_without(home, workflow, args.id)
    if errors:
        for error in errors:
            print(f'error {error}')
        return 2
    if wf_path.is_file():
        common.write_json(wf_path, workflow)
    comp_path.unlink(missing_ok=True)
    print(f'removed {args.id} from {common.shown(wf_path)}')
    return 0


def uses(workflow, cid: str) -> bool:
    if not isinstance(workflow, dict):
        return False
    items = [i for v in (workflow.get('stages') or {}).values() if isinstance(v, list) for i in v]
    for key in ('gates', 'optional'):
        items += [i for g in workflow.get(key) or [] if isinstance(g, dict) for i in g.get('components') or []]
    return cid in [flow.entry(item)[0] for item in items]


def check_without(home: Path, workflow: dict, cid: str) -> list[str]:
    """部品 cid を消したとしたときのテンプレートの誤り (消す前に確かめる)。"""
    components, errors = flow.load_components(home=home)
    components.pop(cid, None)
    return errors + flow.workflow_errors(workflow, components)


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


def load_example(ref: str) -> tuple[dict | None, str]:
    """見本 (同梱の名前かファイルのパス) を読む。(見本, 誤り)。"""
    path = Path(ref) if ref.endswith('.json') else EXAMPLES / f'{ref}.json'
    data = common.load_json(path)
    if not isinstance(data, dict):
        return None, f'{ref}: example not found or not readable'
    problem = example_error(data)
    return (None, f'{ref}: {problem}') if problem else (data, '')


def example_error(data: dict) -> str | None:
    """見本の形の誤り (1 つ目だけ)。"""
    if not isinstance(data.get('name'), str) or not flow_name_ok(data['name']):
        return 'name must be lowercase letters, digits and hyphens'
    if not isinstance(data.get('workflow'), dict):
        return 'workflow must be an object'
    skills = data.get('skills')
    if not isinstance(skills, list):
        return 'skills must be a list'
    for item in skills:
        if not isinstance(item, dict) or not isinstance(item.get('skill'), str) or not SKILL_NAME.match(item['skill']):
            return 'each skill needs skill (<plugin>:<name> or <name>)'
        if item.get('stage') not in ADD_STAGES:
            return f'{item["skill"]}: stage must be one of {", ".join(ADD_STAGES)}'
        if item['stage'] == 'gate' and item.get('after') not in flow.STAGES:
            return f'{item["skill"]}: stage gate needs after (a stage)'
        if item['stage'] in flow.OPTIONAL and item.get('after') not in (None,) + flow.OPTIONAL_PLACES:
            return f'{item["skill"]}: after must be a stage before finish'
        if item['stage'] not in ('gate',) + flow.OPTIONAL and 'after' in item:
            return f'{item["skill"]}: after goes with stage gate or an optional stage only'
        for key, allowed in (('where', flow.WHERE), ('memory', flow.MEMORY)):
            if item.get(key) is not None and item[key] not in allowed:
                return f'{item["skill"]}: {key} must be one of {", ".join(allowed)}'
        for key in ('license', 'link'):
            if not isinstance(item.get(key, ''), str):
                return f'{item["skill"]}: {key} must be a string'
        if not isinstance(item.get('forbid', []), list):
            return f'{item["skill"]}: forbid must be a list'
        install = item.get('install', {})
        if not isinstance(install, dict) or not all(isinstance(v, str) for v in install.values()):
            return f'{item["skill"]}: install takes strings (marketplace, marketplace_source, command, note)'
    return None


def flow_name_ok(name: str) -> bool:
    return bool(re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', name))


def example_args(item: dict):
    return argparse.Namespace(stage=item['stage'], after=item.get('after'), where=item.get('where'),
                              memory=item.get('memory'), forbid=item.get('forbid', []))


def install_commands(item: dict) -> list[str]:
    """入っていないスキルを入れるコマンド。プラグインは marketplace add と plugin install、
    自分のスキルは install.command (clone して写すなど) か、無ければ置き場の案内。"""
    plugin, _name = SKILL_NAME.match(item['skill']).groups()
    install = item.get('install', {})
    if not plugin:
        if install.get('command'):
            return [install['command']]
        return [f'# put the skill in ~/.claude/skills/{item["skill"]}/ ({install.get("note", "your own skill")})']
    commands = []
    if install.get('marketplace_source'):
        commands.append(f'claude plugin marketplace add {install["marketplace_source"]}')
    marketplace = install.get('marketplace', 'claude-plugins-official')
    commands.append(f'claude plugin install {plugin}@{marketplace} --scope user')
    return commands


def example_rows(example: dict, home: Path) -> list[dict]:
    """スキルごとの状態。installed なら作る部品の定義 (component) と、home の登録の状態を持つ。"""
    rows = []
    for item in example['skills']:
        skill_md, plugin, name, rel = find_skill(item['skill'], os.environ)
        row = {'item': item, 'installed': skill_md is not None, 'component': None, 'registered': '-',
               'error': None}
        if skill_md is not None:
            try:
                row['component'] = build_component(item['skill'], skill_md, plugin, name, rel, example_args(item))
            except ValueError as e:
                row['error'] = str(e)
            if row['component'] is not None:
                path = home / 'components' / f'{row["component"]["id"]}.json'
                existing = common.load_json(path)
                row['registered'] = ('registered' if existing == row['component'] else
                                     'differs' if path.exists() else 'not registered')
        rows.append(row)
    return rows


def print_rows(rows: list[dict]) -> list[str]:
    """表を出し、足りないスキルを入れるコマンドを (重複なく) 返す。"""
    print('skill	stage	installed	registered	license	link')
    commands = []
    for row in rows:
        item = row['item']
        place = place_of(item['stage'], item.get('after'))
        print('\t'.join([item['skill'], place, 'installed' if row['installed'] else 'missing', row['registered'],
                         item.get('license', ''), item.get('link', '')]))
        if not row['installed']:
            commands += [c for c in install_commands(item) if c not in commands]
        if row['error']:
            print(f'error {row["error"]}')
    return commands


def cmd_example(args, home: Path) -> int:
    if args.action == 'list':
        print('name\tskills\tdescription')
        for path in sorted(EXAMPLES.glob('*.json')):
            data, problem = load_example(str(path))
            if data is None:
                print(f'error {problem}')
                continue
            description = data.get('description', {})
            text = description.get('en', '') if isinstance(description, dict) else ''
            print('\t'.join([data['name'], str(len(data['skills'])), text]))
        return 0
    example, problem = load_example(args.example)
    if example is None:
        print(problem, file=sys.stderr)
        return 2
    rows = example_rows(example, home)
    commands = print_rows(rows)
    for command in commands:
        print(f'install {command}')
    if any(row['error'] for row in rows):
        return 2
    if args.action == 'check':
        return MISSING if commands else 0
    if commands:
        print('some skills are missing; install them (the install lines above), then run use again', file=sys.stderr)
        return MISSING
    return use_example(example, rows, home, args.as_name or example['name'], args.replace)


def use_example(example: dict, rows: list[dict], home: Path, name: str, replace: bool) -> int:
    """部品を登録し、見本のテンプレートを home に書く。誤りがあれば全部元に戻す。"""
    if not flow_name_ok(name):
        print(f'{name}: not a valid workflow name', file=sys.stderr)
        return 2
    bundled, _ = flow.load_components()
    clash = [row['component']['id'] for row in rows if row['component']['id'] in bundled]
    if clash:
        print(f'{", ".join(clash)}: a bundled component already has this id', file=sys.stderr)
        return 2
    differs = [row['component']['id'] for row in rows if row['registered'] == 'differs']
    if differs:
        print(f'{", ".join(differs)}: already registered with other settings; '
              'remove them (component.py remove <id> --workflow <name>) first', file=sys.stderr)
        return 2
    wf_path = home / 'workflows' / f'{name}.json'
    if wf_path.exists() and not replace:
        print(f'{common.shown(wf_path)} exists; pass --replace to overwrite it', file=sys.stderr)
        return 2
    workflow = json.loads(json.dumps(example['workflow']))
    workflow['name'] = name
    written = {wf_path: wf_path.read_bytes() if wf_path.is_file() else None}
    for row in rows:
        path = home / 'components' / f'{row["component"]["id"]}.json'
        if row['registered'] != 'registered':
            written[path] = None
            common.write_json(path, row['component'])
    common.write_json(wf_path, workflow)
    errors = check(home, workflow)
    if errors:
        for path, data in written.items():
            if data is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(data)
        for error in errors:
            print(f'error {error}')
        return 2
    print(f'workflow {name} written to {common.shown(wf_path)}')
    for row in rows:
        print(f'component {row["component"]["id"]} ({row["registered"] if row["registered"] == "registered" else "added"})')
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--home', help='components/ と workflows/ を置く home (common.resolve_home)')
    sub = ap.add_subparsers(dest='command', required=True)
    add = sub.add_parser('add', help='入れたスキルを外部部品として登録し、テンプレートに差す')
    add.add_argument('skill', help='<plugin>:<name> か、~/.claude/skills の <name>')
    add.add_argument('--stage', required=True, choices=ADD_STAGES)
    add.add_argument('--after', choices=flow.STAGES,
                     help='--stage gate かオプションの工程のとき、どの工程のあとか (オプションの工程は省けば既定の場所)')
    add.add_argument('--workflow', default=flow.DEFAULT_WORKFLOW, help='差すテンプレート (既定 jev-routed)')
    add.add_argument('--when', choices=flow.WHEN)
    add.add_argument('--where', choices=flow.WHERE)
    add.add_argument('--memory', choices=flow.MEMORY)
    add.add_argument('--forbid', action='append', help='渡さない引数や飛ばす手順 (繰り返せる)')
    remove = sub.add_parser('remove', help='外部部品を home とテンプレートから外す')
    remove.add_argument('id')
    remove.add_argument('--workflow', default=flow.DEFAULT_WORKFLOW)
    sub.add_parser('list', help='同梱と home の部品の一覧')
    example = sub.add_parser('example', help='外部部品を使うテンプレートの見本を確かめる・使う')
    example_sub = example.add_subparsers(dest='action', required=True)
    example_sub.add_parser('list', help='同梱の見本の一覧')
    for action, text in (('check', '見本が使うスキルが入っているかと、足りないスキルを入れるコマンド'),
                         ('use', '見本のスキルを部品として登録し、テンプレートを home に書く')):
        one = example_sub.add_parser(action, help=text)
        one.add_argument('example', help='同梱の見本の名前か、見本のファイルのパス')
        if action == 'use':
            one.add_argument('--as', dest='as_name', help='home に書くテンプレートの名前 (既定は見本の名前)')
            one.add_argument('--replace', action='store_true', help='home の同じ名前のテンプレートを上書きする')
    args = ap.parse_args(argv)
    common.utf8_stdout()
    home = common.resolve_home(args.home)
    if args.command == 'add':
        return cmd_add(args, home)
    if args.command == 'remove':
        return cmd_remove(args, home)
    if args.command == 'example':
        return cmd_example(args, home)
    return cmd_list(home)


if __name__ == '__main__':
    sys.exit(main())
