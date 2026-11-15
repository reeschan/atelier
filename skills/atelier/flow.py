"""開発フローのテンプレート (workflows/) を解決して検証し、工程ごとの計画を表で返す (docs/specs/atelier-workflow.md)。

  python flow.py <pbi-dir> [--workflow NAME] [--home DIR] [--decide | --skill-tool-episodes]
  python flow.py <pbi-dir> --skill NAME

テンプレートの解決順は、--workflow を省くと <pbi-dir>/workflow.json → <home>/workflows/jev-routed.json →
同梱の workflows/jev-routed.json。--workflow NAME を渡すと <home>/workflows/NAME.json → 同梱の workflows/NAME.json
(<pbi-dir>/workflow.json は読まない)。home は common.resolve_home で解決する。
コンポーネントの定義は同梱の components/<id>.json と <home>/components/<id>.json を読み、then に書いた部品が実在することも
確かめる。同梱するのはこのスキルの部品と Claude Code の built-in だけ。他の人のスキルは利用者がプラグインのコマンドで
入れ、component.py add がその SKILL.md から home の部品を書く。home の部品は同梱の id を上書きできない (後ろ盾を守るため)。

オプションの工程 (ui-design・marketing・security・model-check・map・lint-survey・lint-recheck) は、テンプレートの optional に
{"stage", "after", "components"} で置いたときだけ after の工程の直後 (関所より先) に並ぶ。ui-design・marketing・security・
model-check は関所と同じく成り立つ部品をすべて動かし、後ろ盾は要らない。map・lint-survey・lint-recheck (PICK_FIRST) は
工程と同じく先に使えるものを 1 つだけ選び、最後の候補は後ろ盾にする (appendix/optional.md)。

検証すること: 9 つの工程がすべてあること・知らない工程が無いこと、各工程の候補が 1 つ以上で、どれも実在し、
その工程を担う部品であること、最後の候補が後ろ盾 (builtin か、requires の無い script) であること、
関所の after が工程であり、部品が gate を担うこと、when の値、PBI ごとの when (tier-opus・coupling-high) が
after: route の関所だけにあること、where が runner の部品が memory full でないこと、calls_back が真の部品を
使っていないこと、オプションの工程の stage・after・部品 (その工程を担うもの、1 つの工程は 1 回だけ、PICK_FIRST の工程は
最後の候補が後ろ盾)、
fix.max_rework が 0〜5 の整数、build.external_memory が none か refs-only、部品の needs_skill_tool (任意) が真偽値。
候補の where の上書き (main・subagent-readonly・subagent。runner の部品は変えられない) と models
(部品が起こすサブエージェントのモデル。`opus xhigh` の形で 1〜10 個、where が main の候補だけ。サブエージェントは
サブエージェントを起こせないため)。

標準出力はタブ区切りの表 (stage / component / invoke / where / memory / when / status / doc / models。1 行目は見出し)。
doc は、その部品を動かす手順が書いてあるスキルの中のファイル (設計者はそこを読んで動かす)。
where はテンプレートの上書きがあればそれ。models はカンマ区切り、無ければ `-`。
関所の行の stage は `gate after <工程>`、オプションの工程の行は `<工程> after <工程>`。status は builtin・script ok・cli missing <名前>・env missing <名前>・
plugin ok <SKILL.md の場所>・plugin missing <名前> (read-skill の部品)・skill ok <SKILL.md の場所>・skill builtin・
skill missing (kind skill の部品)。表の前に `workflow <名前> (<読んだファイル>)` の 1 行を出す。

スキルとプラグインの置き場:
- プラグインの置き場は PBI_PLUGINS_DIR、無ければ <CLAUDE_CONFIG_DIR か ~/.claude>/plugins。その installed_plugins.json の
  installPath (入っている版の置き場) だけを見る。scope が user のものと、projectPath が作業ルートかその上にあるものを使い、
  marketplaces の写しは使わない。installed_plugins.json が無いときは、置き場の下 (marketplaces を除く) の
  <プラグイン名>/ か <プラグイン名>/<版>/ を探す。
- <プラグイン>:<スキル> は、その置き場の skills/ の下 (分類のフォルダを挟んでもよい) の <スキル>/SKILL.md。
- <スキル> は、作業ルートの .claude/skills/<スキル>/SKILL.md、次に自分のスキルの置き場 (PBI_SKILLS_DIR、無ければ
  <CLAUDE_CONFIG_DIR か ~/.claude>/skills) の <スキル>/SKILL.md。どちらにも無く BUILTIN_SKILLS にあれば skill builtin。
- 作業ルートは <pbi-dir> とその上のうち、最初に .git を持つフォルダ。

--skill NAME は、スキル 1 つの状態 (skill ok <場所>・skill builtin・skill missing) だけを 1 行で出す。テンプレートは読まない。

--skill-tool-episodes は、`workflow` の行のあと `skill-tool-episodes <出番,…>` (無ければ `-`) の 1 行を出す。
Skill ツールを持つ設計者で起こす出番の一覧 (skill_tool_episodes)。

--decide は、振り分けのあと (<pbi-dir>/PBI-*.route.json が揃ってから) に使う。PBI ごとに決まる when を
route.json から判定し、表 (place / component / pbi / decision / reason) を出す。対象は after: route の関所の部品
(PBI ごとに 1 行) と、review の工程の when が heavy-ask の部品 (pbi は all)。
tier-opus は tier が opus、coupling-high は scores.coupling が 2、deep は review が deep のとき run。always は run、
設計者が判断する when (clearly-complex など) は decision を orchestrator にする。heavy-ask は <pbi-dir>/adapters.json の
heavy-review に答え (heavy_check.py record) があればその run / skip、無ければ orchestrator (最後の見直しの直前に
heavy_check.py check で差分を調べ、深ければユーザーに聞く)。route.json が 1 つも無ければ終了コード 2。

重い見直しを確かめずに動かす道は残さない (docs/specs/heavy-review-ask.md)。review の工程では deep を使えず
(PBI の review が deep でも、差分が深いとは限らないため)、main で動く部品 (自分でレビュアーを起こす。builtin を除く)
は when を heavy-ask にしなければならない。どちらも検証の誤りにする。

終了コード: 0 = 正常。2 = 検証の誤り (表は出さず、`error <内容>` を 1 行ずつ標準出力に出す)・
テンプレートが読めない・<pbi-dir> が無い・引数の誤り。
"""
import argparse
import os
import re
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
# オプションの工程。テンプレートの optional に置いたときだけ、after の工程の直後に動く
OPTIONAL = ('ui-design', 'marketing', 'security', 'model-check', 'map', 'lint-survey', 'lint-recheck')
# 工程と同じく先に使えるものを 1 つだけ選ぶオプションの工程。最後の候補は後ろ盾 (代わりの依頼文) にする。
# ほかのオプションの工程は成り立つ部品をすべて動かし、後ろ盾は要らない (無ければ飛ばす)
PICK_FIRST = ('map', 'lint-survey', 'lint-recheck')
# optional に置くときの既定の after (component.py add が使う)。verify のあとは、全 PBI が通りテストの工程も終えた
# あと、review の前に 1 回だけ動く (appendix/optional.md)
OPTIONAL_AFTER = {'ui-design': 'design', 'marketing': 'understand', 'security': 'review', 'model-check': 'design',
                  'map': 'understand', 'lint-survey': 'understand', 'lint-recheck': 'verify'}
ROLES = STAGES + (GATE,) + OPTIONAL
# オプションの工程を置ける after。finish のあとは完了報告に載らないので置かない
OPTIONAL_PLACES = STAGES[:-1]
INVOKE_KINDS = ('skill', 'read-skill', 'script', 'builtin')
WHERE = ('main', 'subagent-readonly', 'subagent', 'runner')
MEMORY = ('full', 'refs-only', 'none')
# テンプレートの候補で上書きできる where。runner は worker.md の担当なので上書きでは選べない
ENTRY_WHERE = ('main', 'subagent-readonly', 'subagent')
# テンプレートの候補の models (部品が起こすサブエージェントのモデル)。Agent ツールの model と effort の形
MODEL_SPEC = re.compile(r'(opus|sonnet|haiku|fable|inherit)( (low|medium|high|xhigh|max))?')
# 1 つの部品が同時に起こすサブエージェントの上限
MAX_MODELS = 10
WHEN = ('always', 'clearly-complex', 'ask-or-decide', 'adapter-gate', 'tier-opus', 'coupling-high', 'deep',
        'ui-change', 'ui-retouch', 'user-facing', 'ask-user', 'heavy-ask')
# 画面の見た目の条件は 2 つで 1 組にし、同じ実行で両方は成り立たない (appendix/optional.md)。新しい画面・流れ・方向性は
# ui-change (design-studio など新しく見た目を作る部品)、いまある画面の見た目の手直しだけなら ui-retouch (unslop など
# 点検して直す部品)。作る部品と直す部品が同じ回に重なると方向性が二重になるため
# PBI ごとに決まる条件。段と brief の値が出そろう振り分けのあと (after: route の関所) でしか判定できない
PER_PBI_WHEN = ('tier-opus', 'coupling-high')
# 利用者に毎回聞く条件。聞く時点を決めてあるのはオプションの工程だけ (appendix/optional.md)
OPTIONAL_ONLY_WHEN = ('ask-user',)
# 実装を終えた差分が深い実装 (新しいアプリケーションを一から作るなど) のときだけ、ユーザーに聞いて動かす条件。
# 最後の見直しの直前に heavy_check.py が差分を調べて聞くので、review の工程だけで使える (appendix/review.md)
REVIEW_ONLY_WHEN = ('heavy-ask',)
# review の工程で使えない条件。deep は PBI の見込みで、差分が深いかは表さない (重い見直しを確かめずに動かさない)
NOT_IN_REVIEW_WHEN = ('deep',)
COST = ('low', 'mid', 'high')
EXTERNAL_MEMORY = ('none', 'refs-only')
# 差し戻しの上限の範囲 (今の上限は 2。5 を超える回数は設計の見直しに回すべき規模)
MAX_REWORK_RANGE = (0, 5)

HEADER = 'stage\tcomponent\tinvoke\twhere\tmemory\twhen\tstatus\tdoc\tmodels'
DECIDE_HEADER = 'place\tcomponent\tpbi\tdecision\treason'
# プラグインの置き場の下を探す深さ (~/.claude/plugins/cache/<marketplace>/<plugin>/<版> まで届く)
PLUGIN_SEARCH_DEPTH = 5
# installed_plugins.json が無いときに探さない置き場の下のフォルダ。marketplaces は入れる前の写しで、入っている版ではない
NOT_INSTALLED_DIRS = ('marketplaces',)
# Claude Code に組み込みで、SKILL.md の無いまま Skill ツールで呼べるスキル
BUILTIN_SKILLS = ('code-review', 'security-review', 'simplify', 'init', 'claude-api', 'update-config',
                  'keybindings-help', 'fewer-permission-prompts', 'loop', 'schedule')
# 使える kind skill の状態の頭
SKILL_USABLE = ('skill ok ', 'skill builtin')
# 工程 (オプションの工程と関所は after の工程) から、その行を動かす出番。build と verify は、作業者と見直しを起こす
# 出番 (build・test の PBI・fix の PBI) のすべてで動く
STAGE_EPISODES = {
    'understand': ('design',), 'design': ('design',), 'plan': ('design',), 'route': ('design',),
    'build': ('build', 'test', 'fix'), 'verify': ('build', 'test', 'fix'),
    'review': ('review',), 'fix': ('fix',), 'finish': ('finish',),
}


def load_components(base=COMPONENTS, home: Path | None = None) -> tuple[dict, list[str]]:
    """同梱の components/*.json と <home>/components/*.json を読み、{id: 定義} と誤りの一覧を返す。"""
    found, errors = {}, []
    sources = [(Path(base), 'component')]
    if home is not None:
        sources.append((Path(home) / 'components', 'home component'))
    for directory, label in sources:
        for path in sorted(directory.glob('*.json')):
            data = common.load_json(path)
            if not isinstance(data, dict):
                errors.append(f'{label} {path.name}: not a JSON object')
                continue
            problem = component_error(data, path.stem)
            if problem:
                errors.append(f'{label} {path.name}: {problem}')
                continue
            if data['id'] in found:
                errors.append(f'{label} {path.name}: a bundled component already has this id')
                continue
            found[data['id']] = data
    for cid, data in found.items():
        for following in data.get('then', []):
            if following not in found:
                errors.append(f'component {cid}.json: then points at an unknown component: {following}')
    return found, errors


def component_error(data: dict, stem: str) -> str | None:
    """コンポーネントの定義の誤り (1 つ目だけ)。正しければ None。"""
    if data.get('id') != stem:
        return 'id does not match the file name'
    stages = data.get('stages')
    if not isinstance(stages, list) or not stages or any(s not in ROLES for s in stages):
        return 'stages must be a non-empty list of stages, gate or optional stages'
    title = data.get('title')
    if not isinstance(title, dict) or not all(isinstance(title.get(k), str) and title[k] for k in ('ja', 'en')):
        return 'title needs ja and en'
    invoke = data.get('invoke')
    if not isinstance(invoke, dict) or invoke.get('kind') not in INVOKE_KINDS:
        return f'invoke.kind must be one of {", ".join(INVOKE_KINDS)}'
    needed = {'skill': ('name',), 'read-skill': ('plugin', 'path'), 'script': ('path',), 'builtin': ()}[invoke['kind']]
    if not all(isinstance(invoke.get(k), str) and invoke[k] for k in needed):
        return f'invoke ({invoke["kind"]}) needs {", ".join(needed)}'
    if invoke['kind'] == 'script' and not (HERE / invoke['path']).is_file():
        return f'invoke points at a missing file: {invoke["path"]}'
    doc = data.get('doc')
    if not isinstance(doc, str) or not doc:
        return 'doc must name the file that holds the procedure'
    if not (HERE / doc).is_file():
        return f'doc points at a missing file: {doc}'
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
    if not isinstance(data.get('needs_skill_tool', False), bool):
        return 'needs_skill_tool must be true or false'
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


ENTRY_KEYS = {'id', 'when', 'where', 'models'}


def entry(item) -> tuple[str | None, str | None]:
    """テンプレートの候補 1 つ (文字列か {"id", "when", "where", "models"}) を (id, when の上書き) にする。"""
    if isinstance(item, str):
        return item, None
    if isinstance(item, dict) and isinstance(item.get('id'), str) and set(item) <= ENTRY_KEYS:
        return item['id'], item.get('when')
    return None, None


def entry_where(item, component: dict) -> str:
    """候補の where (テンプレートの上書き、無ければ部品の where)。"""
    return item.get('where') or component['where'] if isinstance(item, dict) else component['where']


def entry_models(item) -> list:
    """候補の models (部品が起こすサブエージェントのモデル)。無ければ空。"""
    return item.get('models') or [] if isinstance(item, dict) else []


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
    optional = workflow.get('optional', [])
    if not isinstance(optional, list):
        errors.append('optional must be a list')
        optional = []
    seen = set()
    for step in optional:
        if not isinstance(step, dict) or step.get('stage') not in OPTIONAL or step.get('after') not in OPTIONAL_PLACES \
                or not isinstance(step.get('components'), list) or not step['components']:
            errors.append(f'each optional step needs stage ({", ".join(OPTIONAL)}), after (a stage before finish) '
                          'and a non-empty components list')
            continue
        if step['stage'] in seen:
            errors.append(f'optional: {step["stage"]} appears more than once')
            continue
        seen.add(step['stage'])
        last = None
        for item in step['components']:
            last = check_item(item, step['stage'], f'{step["stage"]} after {step["after"]}', components, errors,
                              after=step['after'])
        if step['stage'] in PICK_FIRST and last is not None and not is_fallback(last):
            errors.append(f'{step["stage"]}: the last component must be builtin or a script with no requires '
                          f'({last["id"]})')
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
        errors.append(f'{place}: a component must be an id or {{"id", "when", "where", "models"}}')
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
    elif effective in PER_PBI_WHEN and (role != GATE or after != 'route'):
        errors.append(f'{place}: {cid} uses when {effective}, which only a gate after route can decide')
    elif effective in OPTIONAL_ONLY_WHEN and role not in OPTIONAL:
        errors.append(f'{place}: {cid} uses when {effective}, which only an optional stage can use')
    elif effective in REVIEW_ONLY_WHEN and role != 'review':
        errors.append(f'{place}: {cid} uses when {effective}, which only the review stage can use')
    elif effective in NOT_IN_REVIEW_WHEN and role == 'review':
        errors.append(f'{place}: {cid} uses when {effective}, which the review stage cannot use; use heavy-ask '
                      '(the diff is checked and the user asked right before the final review) or adapter-gate')
    elif role == 'review' and effective != 'heavy-ask' and component['invoke']['kind'] != 'builtin'             and entry_where(item, component) == 'main':
        # main で動く見直しは自分でレビュアーを起こす重い見直しなので、ユーザーに聞かずに動かさない
        errors.append(f'{place}: {cid} runs in main and starts its own reviewers, so its when must be heavy-ask')
    if isinstance(item, dict) and 'where' in item and item['where'] not in ENTRY_WHERE:
        errors.append(f'{place}: {cid} where must be one of {", ".join(ENTRY_WHERE)}')
    elif component['where'] == 'runner' and isinstance(item, dict) and 'where' in item:
        errors.append(f'{place}: {cid} runs on a runner; its where cannot change')
    if isinstance(item, dict) and 'models' in item:
        models = item['models']
        if not isinstance(models, list) or not models or len(models) > MAX_MODELS \
                or not all(isinstance(m, str) and MODEL_SPEC.fullmatch(m) for m in models):
            errors.append(f'{place}: {cid} models must be 1 to {MAX_MODELS} of "<opus|sonnet|haiku|fable|inherit> '
                          '[low|medium|high|xhigh|max]"')
        elif entry_where(item, component) != 'main':
            # サブエージェントはサブエージェントを起こせないので、models を使えるのは設計者が動かす部品だけ
            errors.append(f'{place}: {cid} has models but does not run in main (a subagent cannot start subagents)')
    return component


def claude_dir(env) -> Path:
    """Claude Code の設定の置き場。CLAUDE_CONFIG_DIR、無ければ ~/.claude。"""
    return Path(env['CLAUDE_CONFIG_DIR']) if env.get('CLAUDE_CONFIG_DIR') else Path.home() / '.claude'


def plugins_root(env) -> Path:
    """プラグインの置き場。PBI_PLUGINS_DIR、無ければ <CLAUDE_CONFIG_DIR か ~/.claude>/plugins。"""
    return Path(env['PBI_PLUGINS_DIR']) if env.get('PBI_PLUGINS_DIR') else claude_dir(env) / 'plugins'


def skills_root(env) -> Path:
    """自分のスキルの置き場。PBI_SKILLS_DIR、無ければ <CLAUDE_CONFIG_DIR か ~/.claude>/skills。"""
    return Path(env['PBI_SKILLS_DIR']) if env.get('PBI_SKILLS_DIR') else claude_dir(env) / 'skills'


def work_root_of(path) -> Path | None:
    """path とその上のうち、最初に .git を持つフォルダ (作業ルート)。無ければ None。"""
    here = Path(path).resolve()
    for candidate in (here, *here.parents):
        if (candidate / '.git').exists():
            return candidate
    return None


def is_under(path: Path, top: Path) -> bool:
    """path が top かその下にあるか。"""
    try:
        Path(path).resolve().relative_to(Path(top).resolve())
        return True
    except (ValueError, OSError):
        return False


def installed_dirs(plugin: str, root: Path, work_root: Path | None = None) -> list[Path] | None:
    """root/installed_plugins.json にある plugin の installPath (新しく入れたものから)。ファイルが無ければ None。

    scope が user のものと、projectPath が作業ルートかその上にあるもの (作業ルートが分からなければ user だけ) を使う。
    """
    data = common.load_json(Path(root) / 'installed_plugins.json')
    if not isinstance(data, dict):
        return None
    entries = []
    for key, rows in (data.get('plugins') or {}).items():
        if key.split('@')[0] != plugin or not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('installPath'), str):
                continue
            project = row.get('projectPath')
            if row.get('scope') == 'user' or (isinstance(project, str) and work_root is not None
                                              and is_under(work_root, Path(project))):
                entries.append(row)
    entries.sort(key=lambda row: str(row.get('lastUpdated') or row.get('installedAt') or ''), reverse=True)
    return [Path(row['installPath']) for row in entries if Path(row['installPath']).is_dir()]


def scanned_dirs(plugin: str, root: Path) -> list[Path]:
    """installed_plugins.json の無い置き場の下で、<plugin>/ と <plugin>/<版>/ のフォルダ (marketplaces を除く)。"""
    root = Path(root)
    if not root.is_dir():
        return []
    found = []
    for current, dirs, _files in os.walk(root):
        here = Path(current)
        depth = len(here.relative_to(root).parts)
        if depth == 0:
            dirs[:] = [d for d in dirs if d not in NOT_INSTALLED_DIRS]
        if depth >= PLUGIN_SEARCH_DEPTH:
            dirs[:] = []
        if here.name == plugin:
            found += [here, *(d for d in here.iterdir() if d.is_dir())]
    return found


def plugin_dirs(plugin: str, root: Path, work_root: Path | None = None) -> list[Path]:
    """plugin の入っている版の置き場の候補。installed_plugins.json があればその installPath だけ。"""
    installed = installed_dirs(plugin, root, work_root)
    return scanned_dirs(plugin, root) if installed is None else installed


def newest(paths: list) -> Path | None:
    return max(paths, key=lambda c: c.stat().st_mtime) if paths else None


def find_plugin_file(plugin: str, rel: str, root: Path, work_root: Path | None = None) -> Path | None:
    """plugin の入っている版の置き場の <rel> のうち、いちばん新しいもの。無ければ None。"""
    return newest([d / rel for d in plugin_dirs(plugin, root, work_root) if (d / rel).is_file()])


def find_plugin_skill_file(plugin: str, skill: str, root: Path, work_root: Path | None = None) -> Path | None:
    """plugin の入っている版の skills/ の下 (分類のフォルダを挟んでもよい) の <skill>/SKILL.md。無ければ None。"""
    found = []
    for directory in plugin_dirs(plugin, root, work_root):
        if (directory / 'skills').is_dir():
            found += [p for p in (directory / 'skills').rglob('SKILL.md') if p.parent.name == skill]
    return newest(found)


def find_skill_file(name: str, skills_dir: Path, env, work_root: Path | None = None) -> Path | None:
    """スキルの SKILL.md。無ければ None。

    <プラグイン>:<スキル> はプラグインの入っている版の置き場から。ほかは作業ルートの .claude/skills/<名前>/SKILL.md、
    次に skills_dir/<名前>/SKILL.md。
    """
    if ':' in name:
        plugin, _, skill = name.partition(':')
        return find_plugin_skill_file(plugin, skill, plugins_root(env), work_root)
    candidates = ([Path(work_root) / '.claude' / 'skills'] if work_root is not None else []) + [Path(skills_dir)]
    return next((c / name / 'SKILL.md' for c in candidates if (c / name / 'SKILL.md').is_file()), None)


def skill_status(name: str, env, work_root: Path | None = None) -> str:
    """kind skill のスキルの状態: skill ok <SKILL.md の場所>、skill builtin、skill missing。"""
    found = find_skill_file(name, skills_root(env), env, work_root)
    if found is not None:
        return f'skill ok {found}'
    return 'skill builtin' if name in BUILTIN_SKILLS else 'skill missing'


def status(component: dict, agents, env, work_root: Path | None = None) -> str:
    """部品が使えるかの見込み。"""
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
    if kind == 'read-skill':
        plugin = component['invoke']['plugin']
        found = find_plugin_file(plugin, component['invoke']['path'], plugins_root(env), work_root)
        return f'plugin ok {found}' if found else f'plugin missing {plugin}'
    if kind == 'skill':
        return skill_status(component['invoke']['name'], env, work_root)
    for name in requires.get('plugins', []):
        if not plugin_dirs(name, plugins_root(env), work_root):
            return f'plugin missing {name}'
    for name in requires.get('skills', []):
        if not skill_status(name, env, work_root).startswith(SKILL_USABLE):
            return 'skill missing'
    return 'script ok'


def needs_skill_tool(component: dict, where: str, state: str) -> bool:
    """その行を動かす設計者に Skill ツールが要るか。

    設計者が動かす (where が main) 使える kind skill の行で、部品が needs_skill_tool を持つもの。組み込みのスキルは
    読む SKILL.md が無いので、needs_skill_tool が無くても Skill ツールで呼ぶ。
    """
    if where != 'main' or component['invoke']['kind'] != 'skill' or not state.startswith(SKILL_USABLE):
        return False
    return component.get('needs_skill_tool') is True or state == 'skill builtin'


def anchor_stage(place: str) -> str:
    """表の場所 (工程、`<オプションの工程> after <工程>`、`gate after <工程>`) の工程。"""
    return place.rsplit(' after ', 1)[1] if ' after ' in place else place


def skill_tool_episodes(base, workflow=None, env=None, home=None) -> set:
    """その run のテンプレートで、Skill ツールを持つ設計者で起こす出番の名前の集合。

    使える行 (status が skill ok か skill builtin) のうち where が main で needs_skill_tool の部品 (needs_skill_tool) を
    持つ出番。行の出番は、その行の工程 (オプションの工程と関所は after の工程) から STAGE_EPISODES で決める。
    home の agents.json の adapters.design が使えるスキルの名前なら、design の出番も足す。
    テンプレートの解決は CLI と同じ (workflow はテンプレートの名前、home を省くと common.resolve_home)。
    テンプレートが見つからない・読めない・検証に通らないときは ValueError。
    """
    env = os.environ if env is None else env
    base = Path(base)
    home = common.resolve_home(None) if home is None else Path(home)
    path, candidates = find_workflow(base, workflow, home)
    if path is None:
        raise ValueError('workflow not found: ' + ', '.join(common.shown(c) for c in candidates))
    data = common.load_json(path)
    if data is None:
        raise ValueError(f'{common.shown(path)}: not readable JSON')
    components, errors = load_components(home=home)
    errors += workflow_errors(data, components)
    if errors:
        raise ValueError('; '.join(errors))
    agents = common.load_json(home / 'agents.json')
    work_root = work_root_of(base)
    rows = plan_rows(data, components, agents if isinstance(agents, dict) else None, env, work_root)
    episodes = set()
    for place, cid, _kind, where, _memory, _when, state, _doc, _models in rows:
        if needs_skill_tool(components[cid], where, state):
            episodes.update(STAGE_EPISODES[anchor_stage(place)])
    # adapters.design のスキルは、テンプレートの design の部品の代わりに設計者が動かす (appendix/adapters.md)
    design = agents_module.load_adapters(home)[0]['design']
    if design not in ('auto', 'off') and skill_status(design, env, work_root).startswith(SKILL_USABLE):
        episodes.update(STAGE_EPISODES['design'])
    return episodes


def route_error(route) -> str | None:
    """--decide が読む route.json の欄の誤り。正しければ None。"""
    if not isinstance(route, dict):
        return 'not a readable JSON object'
    if not isinstance(route.get('tier'), str):
        return 'tier must be a string'
    if not isinstance(route.get('scores', {}), dict):
        return 'scores must be an object'
    if 'review' in route and not isinstance(route['review'], str):
        return 'review must be a string'
    return None


def per_pbi(when: str, route: dict) -> tuple[str, str]:
    """route.json 1 つについて、when の判定 (decision, reason)。"""
    if when == 'always':
        return 'run', 'always'
    if when == 'tier-opus':
        tier = route.get('tier')
        return ('run' if tier == 'opus' else 'skip'), f'tier {tier}'
    if when == 'coupling-high':
        coupling = (route.get('scores') or {}).get('coupling')
        return ('run' if coupling == 2 else 'skip'), f'coupling {coupling}'
    if when == 'deep':
        review = route.get('review')
        return ('run' if review == 'deep' else 'skip'), f'review {review}'
    return 'orchestrator', f'when {when} is decided by the orchestrator'


def heavy_decision(adapters) -> tuple[str, str]:
    """heavy-ask の行の判定。adapters.json の heavy-review の答え (heavy_check.py record) があればそれ。"""
    record = adapters.get('heavy-review') if isinstance(adapters, dict) else None
    if isinstance(record, dict) and record.get('decision') in ('run', 'skip'):
        return record['decision'], f'heavy-review {record["decision"]} by {record.get("by", "?")} (adapters.json)'
    return 'orchestrator', ('heavy-ask: run heavy_check.py check right before the final review; '
                            'ask with its page when the diff is deep (appendix/review.md)')


def decide_rows(workflow: dict, components: dict, routes: dict, adapters=None) -> list[tuple]:
    """after: route の関所の部品 (PBI ごと) と、review の工程の heavy-ask の部品の判定の行。"""
    rows = []
    for gate in workflow.get('gates', []):
        if gate['after'] != 'route':
            continue
        for item in gate['components']:
            cid, when = entry(item)
            effective = when or components[cid]['when']
            for pbi, route in routes.items():
                rows.append(('gate after route', cid, pbi, *per_pbi(effective, route)))
    for item in workflow['stages']['review']:
        cid, when = entry(item)
        if (when or components[cid]['when']) == 'heavy-ask':
            rows.append(('review', cid, 'all', *heavy_decision(adapters)))
    return rows


def plan_rows(workflow: dict, components: dict, agents, env, work_root: Path | None = None) -> list[tuple]:
    """工程・オプションの工程・関所の順に、表の行を返す。オプションの工程と関所は after の工程の直後に置く。"""
    rows = []
    gates = workflow.get('gates', [])
    optional = workflow.get('optional', [])

    def row(place, item):
        cid, when = entry(item)
        component = components[cid]
        rows.append((place, cid, component['invoke']['kind'], entry_where(item, component), component['memory'],
                     when or component['when'], status(component, agents, env, work_root), component['doc'],
                     ','.join(entry_models(item)) or '-'))

    for stage in STAGES:
        for item in workflow['stages'][stage]:
            row(stage, item)
        for step in optional:
            if step['after'] == stage:
                for item in step['components']:
                    row(f'{step["stage"]} after {stage}', item)
        for gate in gates:
            if gate['after'] == stage:
                for item in gate['components']:
                    row(f'gate after {stage}', item)
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('pbi_dir', help='作業の docs/pbi (workflow.json を探す場所)')
    ap.add_argument('--workflow', help='テンプレートの名前 (home か同梱の workflows/<名前>.json)')
    ap.add_argument('--home', help='workflows/・components/・agents.json を置く home (common.resolve_home)')
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument('--decide', action='store_true', help='route.json から PBI ごとの when を判定して表で出す')
    mode.add_argument('--skill-tool-episodes', action='store_true',
                      help='Skill ツールを持つ設計者で起こす出番を 1 行で出す')
    mode.add_argument('--skill', metavar='NAME', help='スキル 1 つの状態 (skill ok <場所>・skill builtin・skill missing) を出す')
    args = ap.parse_args(argv)
    common.utf8_stdout()

    base = Path(args.pbi_dir)
    if not base.is_dir():
        print(f'{args.pbi_dir}: not a directory', file=sys.stderr)
        return 2
    if args.skill:
        print(skill_status(args.skill, os.environ, work_root_of(base)))
        return 0
    home = common.resolve_home(args.home)
    path, candidates = find_workflow(base, args.workflow, home)
    if path is None:
        print('workflow not found: ' + ', '.join(common.shown(c) for c in candidates), file=sys.stderr)
        return 2
    workflow = common.load_json(path)
    if workflow is None:
        print(f'{common.shown(path)}: not readable JSON', file=sys.stderr)
        return 2

    components, errors = load_components(home=home)
    errors += workflow_errors(workflow, components)
    if errors:
        for error in errors:
            print(f'error {error}')
        return 2

    print(f'workflow {workflow["name"]} ({common.shown(path)})')
    if args.decide:
        routes = {r.name.removesuffix('.route.json'): common.load_json(r)
                  for r in sorted(base.glob('PBI-*.route.json'))}
        if not routes:
            print(f'{args.pbi_dir}: no PBI-*.route.json yet (run the route stage first)', file=sys.stderr)
            return 2
        # 読めない route.json を空の route として skip と判定しないよう、表を出す前に止める
        bad = [f'{pbi}.route.json: {problem}' for pbi, route in routes.items() if (problem := route_error(route))]
        if bad:
            for line in bad:
                print(f'error {line}', file=sys.stderr)
            return 2
        print(DECIDE_HEADER)
        for cells in decide_rows(workflow, components, routes, common.load_json(base / 'adapters.json')):
            print('\t'.join(cells))
        return 0
    if args.skill_tool_episodes:
        episodes = skill_tool_episodes(base, args.workflow, os.environ, home)
        print(f'skill-tool-episodes {",".join(sorted(episodes)) or "-"}')
        return 0
    print(HEADER)
    # runners が無くても tools (jevlint など道具の場所) は読む
    agents = common.load_json(home / 'agents.json')
    for cells in plan_rows(workflow, components, agents if isinstance(agents, dict) else None, os.environ,
                           work_root_of(base)):
        print('\t'.join(cells))
    return 0


if __name__ == '__main__':
    sys.exit(main())
