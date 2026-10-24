"""開発フローのテンプレート (workflows/) を解決して検証し、工程ごとの計画を表で返す (docs/specs/atelier-workflow.md)。

  python flow.py <pbi-dir> [--workflow NAME] [--home DIR]

テンプレートの解決順は、--workflow を省くと <pbi-dir>/workflow.json → <home>/workflows/jev-routed.json →
同梱の workflows/jev-routed.json。--workflow NAME を渡すと <home>/workflows/NAME.json → 同梱の workflows/NAME.json
(<pbi-dir>/workflow.json は読まない)。home は common.resolve_home で解決する。
コンポーネントの定義は同梱の components/<id>.json だけを読む。

検証すること: 9 つの工程がすべてあること・知らない工程が無いこと、各工程の候補が 1 つ以上で、どれも実在し、
その工程を担う部品であること、最後の候補が後ろ盾 (builtin か、requires の無い script) であること、
関所の after が工程であり、部品が gate を担うこと、when の値、PBI ごとの when (tier-opus・coupling-high) が
after: route の関所だけにあること、where が runner の部品が memory full でないこと、calls_back が真の部品を
使っていないこと、fix.max_rework が 0〜5 の整数、build.external_memory が none か refs-only。

標準出力はタブ区切りの表 (stage / component / invoke / where / memory / when / status。1 行目は見出し)。
関所の行の stage は `gate after <工程>`。status は builtin・script ok・cli missing <名前>・env missing <名前>・
check skill list (スキルとプラグインはスクリプトからは見えないので、設計者が自分のスキル一覧で確かめる)。
表の前に `workflow <名前> (<読んだファイル>)` の 1 行を出す。

終了コード: 0 = 正常。2 = 検証の誤り (表は出さず、`error <内容>` を 1 行ずつ標準出力に出す)・
テンプレートが読めない・<pbi-dir> が無い・引数の誤り。
"""
import argparse
import os
import sys
from pathlib import Path

import agents as agents_module
import common

HERE = Path(__file__).resolve().parent
BUNDLED_WORKFLOWS = HERE / 'workflows'
COMPONENTS = HERE / 'components'
DEFAULT_WORKFLOW = 'jev-routed'

STAGES = ('understand', 'design', 'plan', 'route', 'build', 'verify', 'review', 'fix', 'finish')
GATE = 'gate'
INVOKE_KINDS = ('skill', 'read-skill', 'script', 'builtin')
WHERE = ('main', 'subagent-readonly', 'subagent', 'runner')
MEMORY = ('full', 'refs-only', 'none')
WHEN = ('always', 'clearly-complex', 'ask-or-decide', 'adapter-gate', 'tier-opus', 'coupling-high', 'deep')
# PBI ごとに決まる条件。段と brief の値が出そろう振り分けのあと (after: route の関所) でしか判定できない
PER_PBI_WHEN = ('tier-opus', 'coupling-high')
COST = ('low', 'mid', 'high')
EXTERNAL_MEMORY = ('none', 'refs-only')
# 差し戻しの上限の範囲 (今の上限は 2。5 を超える回数は設計の見直しに回すべき規模)
MAX_REWORK_RANGE = (0, 5)

HEADER = 'stage\tcomponent\tinvoke\twhere\tmemory\twhen\tstatus'


def load_components(base=COMPONENTS) -> tuple[dict, list[str]]:
    """components/*.json を読み、{id: 定義} と誤りの一覧を返す。"""
    found, errors = {}, []
    for path in sorted(Path(base).glob('*.json')):
        data = common.load_json(path)
        if not isinstance(data, dict):
            errors.append(f'component {path.name}: not a JSON object')
            continue
        problem = component_error(data, path.stem)
        if problem:
            errors.append(f'component {path.name}: {problem}')
            continue
        found[data['id']] = data
    return found, errors


def component_error(data: dict, stem: str) -> str | None:
    """コンポーネントの定義の誤り (1 つ目だけ)。正しければ None。"""
    if data.get('id') != stem:
        return 'id does not match the file name'
    stages = data.get('stages')
    if not isinstance(stages, list) or not stages or any(s not in STAGES + (GATE,) for s in stages):
        return 'stages must be a non-empty list of stages or gate'
    title = data.get('title')
    if not isinstance(title, dict) or not all(isinstance(title.get(k), str) and title[k] for k in ('ja', 'en')):
        return 'title needs ja and en'
    invoke = data.get('invoke')
    if not isinstance(invoke, dict) or invoke.get('kind') not in INVOKE_KINDS:
        return f'invoke.kind must be one of {", ".join(INVOKE_KINDS)}'
    needed = {'skill': ('name',), 'read-skill': ('plugin', 'path'), 'script': ('path',), 'builtin': ('doc',)}[invoke['kind']]
    if not all(isinstance(invoke.get(k), str) and invoke[k] for k in needed):
        return f'invoke ({invoke["kind"]}) needs {", ".join(needed)}'
    if invoke['kind'] in ('script', 'builtin'):
        target = invoke.get('path') or invoke.get('doc')
        if not (HERE / target).is_file():
            return f'invoke points at a missing file: {target}'
    if data.get('where') not in WHERE:
        return f'where must be one of {", ".join(WHERE)}'
    if data.get('memory') not in MEMORY:
        return f'memory must be one of {", ".join(MEMORY)}'
    if data['where'] == 'runner' and data['memory'] == 'full':
        return 'a runner component cannot use memory full'
    if data.get('when') not in WHEN:
        return f'when must be one of {", ".join(WHEN)}'
    if data.get('cost') not in COST:
        return f'cost must be one of {", ".join(COST)}'
    requires = data.get('requires', {})
    if not isinstance(requires, dict) or any(
            k not in ('skills', 'plugins', 'cli', 'env') or not isinstance(v, list) or not all(isinstance(x, str) for x in v)
            for k, v in requires.items()):
        return 'requires takes lists of names under skills, plugins, cli, env'
    if invoke['kind'] == 'builtin' and requires:
        return 'a builtin component cannot have requires'
    for key in ('forbid', 'then'):
        value = data.get(key, [])
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            return f'{key} must be a list of strings'
    for key in ('interactive', 'calls_back'):
        if not isinstance(data.get(key), bool):
            return f'{key} must be true or false'
    return None


def is_fallback(component: dict) -> bool:
    """必ず使える後ろ盾か (builtin か、requires の無い script)。"""
    kind = component['invoke']['kind']
    return kind == 'builtin' or (kind == 'script' and not component.get('requires'))


def find_workflow(pbi_dir: Path, name: str | None, home: Path) -> tuple[Path | None, list[Path]]:
    """読むテンプレートのファイルと、探した候補の一覧を返す。"""
    candidates = []
    if name is None:
        candidates.append(pbi_dir / 'workflow.json')
        name = DEFAULT_WORKFLOW
    candidates += [home / 'workflows' / f'{name}.json', BUNDLED_WORKFLOWS / f'{name}.json']
    for candidate in candidates:
        if candidate.is_file():
            return candidate, candidates
    return None, candidates


def entry(item) -> tuple[str | None, str | None]:
    """テンプレートの候補 1 つ (文字列か {"id", "when"}) を (id, when の上書き) にする。"""
    if isinstance(item, str):
        return item, None
    if isinstance(item, dict) and isinstance(item.get('id'), str) and set(item) <= {'id', 'when'}:
        return item['id'], item.get('when')
    return None, None


def workflow_errors(workflow, components: dict) -> list[str]:
    """テンプレートの誤りをすべて返す。"""
    if not isinstance(workflow, dict):
        return ['workflow is not a JSON object']
    errors = []
    if not isinstance(workflow.get('name'), str) or not workflow['name']:
        errors.append('name is missing')
    stages = workflow.get('stages')
    if not isinstance(stages, dict):
        return errors + ['stages must be an object']
    errors += [f'unknown stage: {s}' for s in stages if s not in STAGES]
    for stage in STAGES:
        items = stages.get(stage)
        if not isinstance(items, list) or not items:
            errors.append(f'{stage}: needs at least one component')
            continue
        last = None
        for item in items:
            last = check_item(item, stage, stage, components, errors)
        if last is not None and not is_fallback(last):
            errors.append(f'{stage}: the last component must be builtin or a script with no requires ({last["id"]})')
    gates = workflow.get('gates', [])
    if not isinstance(gates, list):
        errors.append('gates must be a list')
        gates = []
    for gate in gates:
        if not isinstance(gate, dict) or gate.get('after') not in STAGES or not isinstance(gate.get('components'), list) \
                or not gate['components']:
            errors.append('each gate needs after (a stage) and a non-empty components list')
            continue
        for item in gate['components']:
            check_item(item, GATE, f'gate after {gate["after"]}', components, errors, after=gate['after'])
    fix = workflow.get('fix', {})
    rework = fix.get('max_rework') if isinstance(fix, dict) else None
    low, high = MAX_REWORK_RANGE
    if not isinstance(rework, int) or isinstance(rework, bool) or not low <= rework <= high:
        errors.append(f'fix.max_rework must be an integer from {low} to {high}')
    build = workflow.get('build', {})
    memory = build.get('external_memory') if isinstance(build, dict) else None
    if memory not in EXTERNAL_MEMORY:
        errors.append(f'build.external_memory must be one of {", ".join(EXTERNAL_MEMORY)}')
    return errors


def check_item(item, role: str, place: str, components: dict, errors: list, after: str | None = None) -> dict | None:
    """候補 1 つを確かめ、誤りを errors に足す。正しければ部品の定義を返す。"""
    cid, when = entry(item)
    if cid is None:
        errors.append(f'{place}: a component must be an id or {{"id", "when"}}')
        return None
    component = components.get(cid)
    if component is None:
        errors.append(f'{place}: unknown component {cid}')
        return None
    if role not in component['stages']:
        errors.append(f'{place}: {cid} does not serve {role}')
        return None
    if component['calls_back']:
        errors.append(f'{place}: {cid} calls this skill back')
    effective = when or component['when']
    if when is not None and when not in WHEN:
        errors.append(f'{place}: {cid} has an unknown when: {when}')
    elif effective in PER_PBI_WHEN and after != 'route':
        errors.append(f'{place}: {cid} uses when {effective}, which only a gate after route can decide')
    return component


def status(component: dict, agents, env) -> str:
    """部品が使えるかの見込み。スクリプトから見えないもの (スキル・プラグイン) は check skill list。"""
    kind = component['invoke']['kind']
    if kind == 'builtin':
        return 'builtin'
    requires = component.get('requires', {})
    for name in requires.get('cli', []):
        if not agents_module.find_cli(name, agents, env):
            return f'cli missing {name}'
    for name in requires.get('env', []):
        if not env.get(name):
            return f'env missing {name}'
    if requires.get('skills') or requires.get('plugins') or kind in ('skill', 'read-skill'):
        return 'check skill list'
    return 'script ok'


def plan_rows(workflow: dict, components: dict, agents, env) -> list[tuple]:
    """工程と関所の順に、表の行を返す。関所は after の工程の直後に置く。"""
    rows = []
    gates = workflow.get('gates', [])

    def row(place, item):
        cid, when = entry(item)
        component = components[cid]
        rows.append((place, cid, component['invoke']['kind'], component['where'], component['memory'],
                     when or component['when'], status(component, agents, env)))

    for stage in STAGES:
        for item in workflow['stages'][stage]:
            row(stage, item)
        for gate in gates:
            if gate['after'] == stage:
                for item in gate['components']:
                    row(f'gate after {stage}', item)
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('pbi_dir', help='作業の docs/pbi (workflow.json を探す場所)')
    ap.add_argument('--workflow', help='テンプレートの名前 (home か同梱の workflows/<名前>.json)')
    ap.add_argument('--home', help='workflows/ と agents.json を置く home (common.resolve_home)')
    args = ap.parse_args(argv)
    common.utf8_stdout()

    base = Path(args.pbi_dir)
    if not base.is_dir():
        print(f'{args.pbi_dir}: not a directory', file=sys.stderr)
        return 2
    home = common.resolve_home(args.home)
    path, candidates = find_workflow(base, args.workflow, home)
    if path is None:
        print('workflow not found: ' + ', '.join(common.shown(c) for c in candidates), file=sys.stderr)
        return 2
    workflow = common.load_json(path)
    if workflow is None:
        print(f'{common.shown(path)}: not readable JSON', file=sys.stderr)
        return 2

    components, errors = load_components()
    errors += workflow_errors(workflow, components)
    if errors:
        for error in errors:
            print(f'error {error}')
        return 2

    print(f'workflow {workflow["name"]} ({common.shown(path)})')
    print(HEADER)
    for cells in plan_rows(workflow, components, agents_module.load_agents(home), os.environ):
        print('\t'.join(cells))
    return 0


if __name__ == '__main__':
    sys.exit(main())
