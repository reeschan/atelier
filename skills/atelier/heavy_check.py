"""最後の見直しの直前に、実装を終えた差分が重い見直し (when が heavy-ask の部品) に見合う深い実装かを調べ、
ユーザーに聞く問いの原稿を書き、答えを残す (appendix/review.md の `## Heavy review`、docs/specs/heavy-review-ask.md)。

  python heavy_check.py check  <pbi-dir> --base REF [--workflow NAME] [--home DIR] [--root DIR]
                               [--designer-model M] [--usual-model M] [--lang ja|en] [--draft FILE]
  python heavy_check.py record <pbi-dir> --decision run|skip --by user|auto [--reason TEXT]

check: <pbi-dir>/adapters.json に heavy-review の答えがあれば `recorded <決定> ...` を出して終わる (resume で聞き直さない)。
  無ければテンプレート (flow.py と同じ解決) の review の工程から when が heavy-ask の行を探す。無ければ `verdict none`。
  あれば、--root (既定はカレント) の作業ツリーをマージ基点 (git merge-base REF HEAD) と比べて (`git diff -M`、
  新しいファイルは呼び出し側が先に git add -N しておく。<pbi-dir> と .claude/worktrees/ の下は数えない) 数字を取り、
  3 つの基準に当てる:
    new-app          新しいマニフェスト (package.json・pyproject.toml・go.mod など) を、マージ基点でマニフェストの無かった
                     フォルダに足し、そのフォルダの下に NEW_APP_LINES 行以上足した (アプリケーションを一から作った)
    new-subsystem    コードのモジュールを SUBSYSTEM_MODULES 個以上変え、そのうち新しいモジュールが 1 つ以上あり、
                     データモデルか外の入出力のファイル (移行・スキーマ・API の定義・ルートなど) を新しく足した
    new-architecture 配置先のファイル (Dockerfile・*.tf・fly.toml など) を足して ARCH_LINES 行以上変えた、または
                     マージ基点からあるマニフェストの依存を入れ替え (足して消した)、ARCH_DELETED_LINES 行以上消した
  1 つでも当たれば verdict heavy、無ければ light。差分が大きいだけ・sensitive・review deep は基準ではない。
  <pbi-dir>/verify/heavy.json (schema atelier.heavy-check/1) に数字・基準・起こすレビュアーと費用の見込みを書き、
  heavy なら問いの原稿 (appendix/questions.md の decision の形) を --draft (既定 <pbi-dir>/notes/heavy-review.question.md) に書く。
  費用の見込みは chars-est/1 で数えた中身 (変えたファイルの今の中身と差分) から、ESTIMATE の式で出す高めの目安。
  レビュアーのモデルは候補の models (`opus xhigh` の形。inherit と、models の無い候補の部品が起こす分は --designer-model)、
  where が main の行は設計者の取りまとめの分を --designer-model で足す。ふつうの見直しは --usual-model の 1 人。
  単価は common.load_prices (home の prices.json が勝つ) で、モデルの系統の名前 (opus・sonnet など) を含むキーに当てる。

record: heavy.json を読み、<pbi-dir>/adapters.json の heavy-review に答えを書く。run は --by user のときだけで、
  heavy.json を書いたあとの shown の行 (kind decision、present.py が見せた記録 presentations.jsonl) が要る。
  確認のページを見せずに重い見直しを動かす記録は作れない。--by auto は skip だけで、verdict が heavy の skip には
  --reason (差分を読んで深くないと見た理由) が要る。stage.decided のイベントを出す。

標準出力 (check): verdict・criterion・numbers・reviewer・cost・usual・record・draft の行。
終了コード: 0 = 正常。2 = 引数の誤り・<pbi-dir> が無い・git が失敗した・テンプレートが読めない・heavy.json が無い。
3 = record が断った (確認のページを見せていない run、auto の run、理由の無い auto の skip)。
"""
import argparse
import datetime
import fnmatch
import hashlib
import json
import subprocess
import sys
from pathlib import Path, PurePosixPath

import common
import flow
import present

try:
    import events
except (Exception, SystemExit):  # events.py の無い古い install でも止めない
    events = None

SCHEMA = 'atelier.heavy-check/1'
RECORD_KEY = 'heavy-review'
HEAVY_WHEN = 'heavy-ask'
DEFAULT_DRAFT = Path('notes') / 'heavy-review.question.md'
RESULT = Path('verify') / 'heavy.json'

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 3

# Windows で git のコンソールのウインドウを出さない (test_no_window.py)
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

# 基準の線。new-app はそのフォルダに足した行、new-architecture は変えた行と消した行
NEW_APP_LINES = 300
SUBSYSTEM_MODULES = 3
ARCH_LINES = 500
ARCH_DELETED_LINES = 300
# モジュールは、ファイルのフォルダの先頭からこの深さまで (src/auth/login.ts と src/auth/x/y.ts は src/auth)
MODULE_DEPTH = 2

MANIFESTS = ('package.json', 'pyproject.toml', 'setup.py', 'setup.cfg', 'go.mod', 'Cargo.toml', 'pom.xml',
             'build.gradle', 'build.gradle.kts', 'Gemfile', 'composer.json', 'deno.json', 'pubspec.yaml',
             'mix.exs', 'Package.swift', '*.csproj', '*.fsproj')
DATA_MODEL = {'dirs': ('migrations', 'migration', 'migrate', 'alembic', 'models', 'entities', 'schema', 'schemas',
                       'prisma'),
              'names': ('*.sql', '*.prisma', 'schema.rb', 'models.*', 'schema.*')}
INTERFACE = {'dirs': ('api', 'apis', 'routes', 'router', 'routers', 'handlers', 'controllers', 'endpoints',
                      'webhooks', 'graphql', 'rpc', 'proto'),
             'names': ('*.proto', '*.graphql', '*.gql', 'openapi.*', 'swagger.*', 'asyncapi.*')}
DEPLOY = {'dirs': ('k8s', 'kubernetes', 'helm', 'terraform', 'deploy'),
          'names': ('Dockerfile', '*.dockerfile', 'docker-compose.*', 'compose.yml', 'compose.yaml', '*.tf',
                    'serverless.yml', 'serverless.yaml', 'fly.toml', 'vercel.json', 'netlify.toml', 'app.yaml',
                    'Procfile', 'render.yaml', 'Chart.yaml', 'wrangler.toml')}
# モジュールを数えないファイル (文書と試験)
DOC_NAMES = ('*.md', '*.mdx', '*.rst', '*.txt')
TEST_DIRS = ('test', 'tests', '__tests__', 'spec', 'specs', 'e2e')
TEST_NAMES = ('test_*', '*_test.*', '*.test.*', '*.spec.*')

# 費用の見込みの式 (1 人あたり)。C は chars-est/1 で数えた中身 (変えたファイルの今の中身と差分)。
# 入力 = base_in + reads × C、出力 = out。キャッシュを数えないので、請求額より高めに出る目安
ESTIMATE = {
    'reviewer': {'base_in': 50_000, 'reads': 3, 'out': 20_000},   # 重い見直しの敵役 1 人 (変更を動かして確かめる)
    'lead': {'base_in': 20_000, 'reads': 1, 'out': 5_000},        # where が main の行を取りまとめる設計者の分
    'usual': {'base_in': 30_000, 'reads': 1, 'out': 8_000},       # ふつうの見直しのレビュアー 1 人
}
FAMILIES = ('opus', 'sonnet', 'haiku', 'fable')
LABELS = {
    'ja': {'new-app': '新しいアプリケーションを一から作った', 'new-subsystem': '自前のデータモデルか外の入出力を持つ新しい仕組みを足した',
           'new-architecture': '構成 (フレームワーク・データの置き場・配置先) を置き換えた'},
    'en': {'new-app': 'builds a new application from scratch',
           'new-subsystem': 'adds a new subsystem with its own data model or external interface',
           'new-architecture': 'replaces the architecture (framework, data store or deployment target)'},
}


class Refused(Exception):
    pass


class GitError(Exception):
    pass


# --- git と差分 ---

def git(root: Path, *words) -> str:
    try:
        run = subprocess.run(['git', *words], cwd=str(root), capture_output=True, text=True, encoding='utf-8',
                             errors='replace', creationflags=NO_WINDOW)
    except OSError as error:
        raise GitError(f'git {words[0]}: {error}') from error
    if run.returncode != 0:
        raise GitError(f'git {" ".join(words[:2])}: {run.stderr.strip() or run.returncode}')
    return run.stdout


def z_tokens(text: str) -> list:
    return text.split('\0')


def parse_numstat_z(text: str) -> dict:
    """`git diff --numstat -z -M` を {新しいパス: (足した行, 消した行)} にする。バイナリは (0, 0)。"""
    out, tokens, i = {}, z_tokens(text), 0
    while i < len(tokens):
        head = tokens[i].lstrip('\n')
        i += 1
        if not head:
            continue
        cells = head.split('\t')
        if len(cells) < 3:
            continue
        added, deleted, path = cells[0], cells[1], cells[2]
        if path == '' and i + 1 < len(tokens):  # 名前を変えたファイル: 古いパス、新しいパスが続く
            path = tokens[i + 1]
            i += 2
        out[path] = (int(added) if added.isdigit() else 0, int(deleted) if deleted.isdigit() else 0)
    return out


def parse_name_status_z(text: str) -> dict:
    """`git diff --name-status -z -M` を {新しいパス: 状態の 1 文字 (A・M・D・R など)} にする。"""
    out, tokens, i = {}, z_tokens(text), 0
    while i < len(tokens):
        status = tokens[i].lstrip('\n')
        i += 1
        if not status:
            continue
        if status[0] in 'RC' and i + 1 < len(tokens):
            out[tokens[i + 1]] = status[0]
            i += 2
        elif i < len(tokens):
            out[tokens[i]] = status[0]
            i += 1
    return out


def excluded(path: str, skip: list) -> bool:
    return any(path == s or path.startswith(s + '/') for s in skip)


def relative_skip(root: Path, pbi: Path) -> list:
    """数えないフォルダ (<pbi-dir> と .claude/worktrees) の、root からの相対の posix パス。"""
    skip = ['.claude/worktrees']
    try:
        skip.append(pbi.resolve().relative_to(root.resolve()).as_posix())
    except ValueError:
        pass
    return skip


def measure(root: Path, base: str, pbi: Path) -> dict:
    """マージ基点と作業ツリーの差分を測る。{merge_base, files, base_files, diff_sha256, content_tokens}。"""
    merge_base = git(root, 'merge-base', base, 'HEAD').strip()
    skip = relative_skip(root, pbi)
    numstat = parse_numstat_z(git(root, 'diff', '-M', '--numstat', '-z', merge_base, '--'))
    statuses = parse_name_status_z(git(root, 'diff', '-M', '--name-status', '-z', merge_base, '--'))
    files = []
    for path, (added, deleted) in sorted(numstat.items()):
        if excluded(path, skip):
            continue
        files.append({'path': path, 'status': statuses.get(path, 'M'), 'added': added, 'deleted': deleted})
    base_files = [p for p in git(root, 'ls-tree', '-r', '--name-only', merge_base).splitlines()
                  if p and not excluded(p, skip)]
    diff_text = git(root, 'diff', '-M', merge_base, '--', '.', *[f':(exclude){s}' for s in skip])
    content = 0
    for item in files:
        if item['status'] == 'D':
            continue
        try:
            content += present.tokens_est((root / item['path']).read_text(encoding='utf-8', errors='replace'))
        except OSError:
            continue
    return {'merge_base': merge_base, 'files': files, 'base_files': base_files,
            'diff_sha256': hashlib.sha256(diff_text.encode('utf-8')).hexdigest(),
            'content_tokens': content + present.tokens_est(diff_text)}


# --- 基準 ---

def matches(path: str, table: dict) -> bool:
    parts = PurePosixPath(path).parts
    name = parts[-1] if parts else path
    return any(d in table['dirs'] for d in parts[:-1]) or any(fnmatch.fnmatch(name, n) for n in table['names'])


def is_manifest(path: str) -> bool:
    return any(fnmatch.fnmatch(PurePosixPath(path).name, n) for n in MANIFESTS)


def is_code(path: str) -> bool:
    """モジュールに数えるファイル (文書でも試験でもない)。"""
    parts = PurePosixPath(path).parts
    name = parts[-1] if parts else path
    if any(fnmatch.fnmatch(name, n) for n in DOC_NAMES + TEST_NAMES):
        return False
    return not any(d in TEST_DIRS for d in parts[:-1])


def module_of(path: str) -> str:
    folder = PurePosixPath(path).parent.parts[:MODULE_DEPTH]
    return '/'.join(folder) or '.'


def folder_of(path: str) -> str:
    parent = PurePosixPath(path).parent.as_posix()
    return '' if parent == '.' else parent


def under(path: str, folder: str) -> bool:
    return folder == '' or path.startswith(folder + '/')


def numbers_of(files: list, base_files: list) -> dict:
    code = [f for f in files if is_code(f['path'])]
    modules = sorted({module_of(f['path']) for f in code})
    # 文書や試験しか無かったモジュールに初めてコードを足したときも、新しいモジュールに数える
    base_modules = {module_of(p) for p in base_files if is_code(p)}
    return {
        'files': len(files),
        'added_files': sum(1 for f in files if f['status'] == 'A'),
        'deleted_files': sum(1 for f in files if f['status'] == 'D'),
        'lines_added': sum(f['added'] for f in files),
        'lines_deleted': sum(f['deleted'] for f in files),
        'modules': modules,
        'new_modules': [m for m in modules if m != '.' and m not in base_modules],
    }


def new_app(files: list, base_files: list) -> dict:
    base_manifest_dirs = {folder_of(p) for p in base_files if is_manifest(p)}
    for item in files:
        if item['status'] != 'A' or not is_manifest(item['path']):
            continue
        folder = folder_of(item['path'])
        if folder in base_manifest_dirs:
            continue
        lines = sum(f['added'] for f in files if under(f['path'], folder))
        if lines >= NEW_APP_LINES:
            return {'id': 'new-app', 'holds': True, 'files': [item['path']],
                    'why': f'{item["path"]} added where no manifest was; +{lines} lines under {folder or "the root"}',
                    'why_ja': f'マニフェストの無かった {folder or "ルート"} に {item["path"]} を足し、その下に {lines} 行足した'}
    return {'id': 'new-app', 'holds': False, 'files': [],
            'why': f'no new manifest with {NEW_APP_LINES}+ lines under its folder',
            'why_ja': f'新しいマニフェストを足し、その下に {NEW_APP_LINES} 行以上足したフォルダが無い'}


def new_subsystem(files: list, numbers: dict) -> dict:
    added = [f['path'] for f in files if f['status'] == 'A']
    signals = [p for p in added if matches(p, DATA_MODEL) or matches(p, INTERFACE)]
    holds = len(numbers['modules']) >= SUBSYSTEM_MODULES and bool(numbers['new_modules']) and bool(signals)
    new = ', '.join(numbers['new_modules'])
    shown_signals = ', '.join(signals[:5])
    why = (f'{len(numbers["modules"])} modules ({len(numbers["new_modules"])} new: {new or "none"}); '
           f'new data model or interface files: {shown_signals or "none"}')
    why_ja = (f'モジュール {len(numbers["modules"])} 個 (新しいもの {len(numbers["new_modules"])} 個: {new or "なし"})、'
              f'新しいデータモデルか入出力のファイル: {shown_signals or "なし"}')
    return {'id': 'new-subsystem', 'holds': holds, 'files': signals[:10], 'why': why, 'why_ja': why_ja}


def new_architecture(files: list, base_files: list, numbers: dict) -> dict:
    changed = numbers['lines_added'] + numbers['lines_deleted']
    deploy = [f['path'] for f in files if f['status'] == 'A' and matches(f['path'], DEPLOY)]
    if deploy and changed >= ARCH_LINES:
        return {'id': 'new-architecture', 'holds': True, 'files': deploy[:10],
                'why': f'deployment files added ({", ".join(deploy[:5])}); {changed} lines changed',
                'why_ja': f'配置先のファイルを足し ({", ".join(deploy[:5])})、{changed} 行変えた'}
    base = set(base_files)
    swapped = [f['path'] for f in files if f['status'] == 'M' and f['path'] in base and is_manifest(f['path'])
               and f['added'] and f['deleted']]
    if swapped and numbers['lines_deleted'] >= ARCH_DELETED_LINES:
        return {'id': 'new-architecture', 'holds': True, 'files': swapped[:10],
                'why': f'dependencies replaced in {", ".join(swapped[:5])}; -{numbers["lines_deleted"]} lines',
                'why_ja': f'{", ".join(swapped[:5])} の依存を入れ替え、{numbers["lines_deleted"]} 行消した'}
    return {'id': 'new-architecture', 'holds': False, 'files': deploy[:10] + swapped[:10],
            'why': f'no deployment file with {ARCH_LINES}+ lines changed, '
                   f'no dependency swap with {ARCH_DELETED_LINES}+ lines deleted',
            'why_ja': f'配置先のファイルを足して {ARCH_LINES} 行以上変えてはおらず、'
                      f'依存を入れ替えて {ARCH_DELETED_LINES} 行以上消してもいない'}


def judge(measured: dict) -> tuple:
    files, base_files = measured['files'], measured['base_files']
    numbers = numbers_of(files, base_files)
    criteria = [new_app(files, base_files), new_subsystem(files, numbers),
                new_architecture(files, base_files, numbers)]
    return numbers, criteria, 'heavy' if any(c['holds'] for c in criteria) else 'light'


# --- 起こすレビュアーと費用 ---

def heavy_rows(workflow: dict, components: dict) -> list:
    """review の工程で when が heavy-ask の行 ({component, where, models})。"""
    rows = []
    for item in workflow['stages']['review']:
        cid, when = flow.entry(item)
        component = components[cid]
        if (when or component['when']) == HEAVY_WHEN:
            rows.append({'component': cid, 'where': flow.entry_where(item, component),
                         'models': flow.entry_models(item)})
    return rows


def family_of(model: str, designer: str) -> str:
    name = model.split()[0] if model else designer
    return designer if name == 'inherit' else name


def price_of(prices: dict, family: str):
    keys = [k for k in (prices.get('models') or {}) if f'-{family}-' in f'-{k}-' or k == family]
    return prices['models'][sorted(keys)[-1]] if keys else None


def cost_of(prices: dict, family: str, shape: dict, content: int) -> dict:
    tokens_in = shape['base_in'] + shape['reads'] * content
    price = price_of(prices, family)
    usd = None if price is None else round((tokens_in * price['input'] + shape['out'] * price['output']) / 1e6, 2)
    return {'model': family, 'input': tokens_in, 'output': shape['out'], 'usd': usd}


def reviewers_of(row: dict, prices: dict, content: int, designer: str) -> list:
    people = [cost_of(prices, family_of(m, designer), ESTIMATE['reviewer'], content) | {'spec': m}
              for m in row['models']] or [cost_of(prices, designer, ESTIMATE['reviewer'], content)
                                          | {'spec': OWN_CHOICE}]
    if row['where'] == 'main':
        people.append(cost_of(prices, designer, ESTIMATE['lead'], content) | {'spec': LEAD})
    return people


# 候補に models の無い行のレビュアーと、main の行の取りまとめ。原稿では言葉に置き換える
OWN_CHOICE = "the skill's own choice"
LEAD = 'lead (designer)'
SPEC_WORDS = {'ja': {OWN_CHOICE: 'スキルが選ぶモデル', LEAD: '取りまとめ (設計者)'}, 'en': {}}


def spec_word(spec: str, lang: str) -> str:
    return SPEC_WORDS[lang].get(spec, spec)


def total(people: list):
    return None if any(p['usd'] is None for p in people) else round(sum(p['usd'] for p in people), 2)


def money(value) -> str:
    return 'price unknown' if value is None else f'${value:.2f}'


# --- 問いの原稿 ---

def draft_text(result: dict, lang: str) -> str:
    row = result['rows'][0]
    people, usual = row['reviewers'], result['usual']
    held = [c for c in result['criteria'] if c['holds']]
    n = result['numbers']
    models = ', '.join(spec_word(p['spec'], lang) for p in people if p['spec'] != LEAD)
    heavy_cost, usual_cost = money(row['total_usd']), money(usual['usd'])
    if lang == 'en':
        return draft_en(row, people, usual, held, n, models, heavy_cost, usual_cost, result)
    reasons = '、'.join(LABELS['ja'][c['id']] for c in held)
    criteria_rows = '\n'.join(f'| {LABELS["ja"][c["id"]]} | {"当たる" if c["holds"] else "当たらない"} | {c["why_ja"]} |'
                              for c in result['criteria'])
    people_rows = '\n'.join(f'| {spec_word(p["spec"], "ja")} | {p["model"]} | 約 {p["input"]:,} | 約 {p["output"]:,} '
                            f'| {money(p["usd"])} |' for p in people)
    return f"""---
lang: ja
template: sheet
cols: 2
title: 重い見直しを動かすかの確認
subtitle: 実装を終えた差分を調べ、深い実装に当たったので、最後の見直しの前で止まっています
---
## 結論 {{span=2}}
```callout warn 差分が深い実装に当たりました。重い見直しを動かしますか
実装を終えた差分を調べたところ、「{reasons}」に当たりました。重い見直し (部品 `{row['component']}`。敵役のレビュアーが変更を実際に動かして確かめる見直し) は、レビュアー {models} で、費用の見込みは {heavy_cost} です。ふつうの見直し (部品 `{usual['spec']}`。テンプレートの次の候補) なら {usual_cost} です。答えが無ければ重い見直しは動かず、ふつうの見直しが動きます。
```

## 今の流れ
```flow
(作業が終わる) -> すべての PBI が通る
すべての PBI が通る -> 差分の深さを調べる
差分の深さを調べる -> *最後の見直しの前で止まった: 重い見直しを動かすかの確認が要る
最後の見直しの前で止まった -> (あなたの返事を待つ)
```

## 答えたあとの流れ
```flow
(あなたの返事を待つ) -> {{重い見直しを動かすか}}
{{重い見直しを動かすか}} -> 敵役のレビュアーが変更を動かして確かめる: 動かす
敵役のレビュアーが変更を動かして確かめる -> 指摘を直すか報告に載せる
{{重い見直しを動かすか}} -> ふつうの見直しが動く: ふつうの見直し
ふつうの見直しが動く -> 指摘を直すか報告に載せる
```

## 決めること {{span=2}}
```ask
最後の見直しに、重い見直しを動かしますか
* 動かす | 敵役のレビュアー ({models}) が変更を実際に動かし、示せた指摘だけを残す。費用の見込みは {heavy_cost}
- ふつうの見直し | 重い見直しは動かず、テンプレートの次の候補が見直す。費用の見込みは {usual_cost}
```

## 判断の材料 {{span=2}}
深いと見た理由 (3 つの基準のうち 1 つでも当たれば深いと見ます。差分が大きいだけでは深いと見ません):

| 基準 | 結果 | 理由 |
|---|---|---|
{criteria_rows}

差分の数字 (マージ基点 `{result['merge_base'][:10]}` から作業ツリーまで):

| 変えたファイル | 新しいファイル | 消したファイル | 足した行 | 消した行 | モジュール | 新しいモジュール |
|---|---|---|---|---|---|---|
| {n['files']} | {n['added_files']} | {n['deleted_files']} | {n['lines_added']:,} | {n['lines_deleted']:,} | {len(n['modules'])} | {len(n['new_modules'])} |

起こすレビュアーと費用の見込み (中身 約 {result['content_tokens']:,} トークンから出した高めの目安。キャッシュは数えない):

| レビュアー | モデル | 入力の見込み | 出力の見込み | 費用の見込み |
|---|---|---|---|---|
{people_rows}
| ふつうの見直し | {usual['model']} | 約 {usual['input']:,} | 約 {usual['output']:,} | {usual_cost} |
"""


def draft_en(row, people, usual, held, n, models, heavy_cost, usual_cost, result) -> str:
    reasons = '; '.join(LABELS['en'][c['id']] for c in held)
    criteria_rows = '\n'.join(f'| {LABELS["en"][c["id"]]} | {"holds" if c["holds"] else "does not hold"} | {c["why"]} |'
                              for c in result['criteria'])
    people_rows = '\n'.join(f'| {spec_word(p["spec"], "en")} | {p["model"]} | ~{p["input"]:,} | ~{p["output"]:,} | {money(p["usd"])} |'
                            for p in people)
    return f"""---
lang: en
template: sheet
cols: 2
title: Run the heavy review?
subtitle: The finished diff looks like deep work, so the run stopped right before the final review
---
## Summary {{span=2}}
```callout warn The diff looks like deep work. Run the heavy review?
The finished diff {reasons}. The heavy review (component `{row['component']}`, adversarial reviewers who run the change to prove each finding) starts {models}, for about {heavy_cost}. The usual review (component `{usual['spec']}`, the template's next row) costs about {usual_cost}. With no answer, the heavy review does not run and the usual review does.
```

## Before
```flow
(the work ends) -> every PBI passes
every PBI passes -> the diff is checked for depth
the diff is checked for depth -> *stopped before the final review: the heavy review needs your yes
stopped before the final review -> (waiting for your answer)
```

## After
```flow
(waiting for your answer) -> {{run the heavy review}}
{{run the heavy review}} -> adversarial reviewers run the change: run it
adversarial reviewers run the change -> findings are fixed or reported
{{run the heavy review}} -> the usual review runs: usual review
the usual review runs -> findings are fixed or reported
```

## Decisions {{span=2}}
```ask
Run the heavy review as the final review?
* Run it | adversarial reviewers ({models}) run the change and keep only the findings they prove; about {heavy_cost}
- Usual review | the heavy review does not run; the template's next row reviews; about {usual_cost}
```

## Details {{span=2}}
Why it looks deep (any one criterion makes it deep; a large diff alone does not):

| Criterion | Result | Why |
|---|---|---|
{criteria_rows}

The diff (merge-base `{result['merge_base'][:10]}` to the working tree):

| Files | New files | Deleted files | Lines added | Lines deleted | Modules | New modules |
|---|---|---|---|---|---|---|
| {n['files']} | {n['added_files']} | {n['deleted_files']} | {n['lines_added']:,} | {n['lines_deleted']:,} | {len(n['modules'])} | {len(n['new_modules'])} |

Reviewers and the expected cost (a high estimate from about {result['content_tokens']:,} tokens of content; no cache counted):

| Reviewer | Model | Input | Output | Cost |
|---|---|---|---|---|
{people_rows}
| usual review | {usual['model']} | ~{usual['input']:,} | ~{usual['output']:,} | {usual_cost} |
"""


# --- コマンド ---

def recorded(pbi: Path):
    data = common.load_json(pbi / 'adapters.json', {})
    record = data.get(RECORD_KEY) if isinstance(data, dict) else None
    return record if isinstance(record, dict) and record.get('decision') in ('run', 'skip') else None


def load_flow(pbi: Path, args) -> tuple:
    home = common.resolve_home(args.home)
    path, candidates = flow.find_workflow(pbi, args.workflow, home)
    if path is None:
        raise GitError('workflow not found: ' + ', '.join(common.shown(c) for c in candidates))
    workflow = common.load_json(path)
    components, errors = flow.load_components(home=home)
    errors += flow.workflow_errors(workflow, components)
    if errors:
        raise GitError(f'{common.shown(path)}: {errors[0]}')
    return workflow, components, home


def cmd_check(args, pbi: Path) -> int:
    record = recorded(pbi)
    if record is not None:
        print(f'recorded {record["decision"]} by {record.get("by", "?")} ({record.get("at", "?")}); '
              'do not ask again')
        return EXIT_OK
    workflow, components, home = load_flow(pbi, args)
    rows = heavy_rows(workflow, components)
    if not rows:
        print('verdict none (no heavy-ask row in the review stage)')
        return EXIT_OK
    root = Path(args.root)
    measured = measure(root, args.base, pbi)
    numbers, criteria, verdict = judge(measured)
    prices = common.load_prices(home)
    content = measured['content_tokens']
    for row in rows:
        row['reviewers'] = reviewers_of(row, prices, content, args.designer_model)
        row['total_usd'] = total(row['reviewers'])
    usual = cost_of(prices, args.usual_model, ESTIMATE['usual'], content) | {'spec': next_row(workflow, components)}
    result = {'schema': SCHEMA, 'checked_at': common.now().isoformat(timespec='seconds'), 'base': args.base,
              'merge_base': measured['merge_base'], 'diff_sha256': measured['diff_sha256'], 'verdict': verdict,
              'numbers': numbers, 'criteria': criteria, 'content_tokens': content, 'rows': rows, 'usual': usual,
              'draft': None}
    if verdict == 'heavy':
        draft = Path(args.draft) if args.draft else pbi / DEFAULT_DRAFT
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text(draft_text(result, args.lang), encoding='utf-8', newline='\n')
        result['draft'] = draft.as_posix()
    common.write_json(pbi / RESULT, result)
    print(f'verdict {verdict}')
    for c in criteria:
        print(f'criterion {c["id"]} {"holds" if c["holds"] else "no"}: {c["why"]}')
    print(f'numbers files {numbers["files"]} added {numbers["added_files"]} deleted {numbers["deleted_files"]} '
          f'lines +{numbers["lines_added"]} -{numbers["lines_deleted"]} modules {len(numbers["modules"])} '
          f'new-modules {len(numbers["new_modules"])}')
    for row in rows:
        print(f'reviewer {row["component"]} ({row["where"]}) '
              + ', '.join(f'{p["spec"]} {money(p["usd"])}' for p in row['reviewers']))
        print(f'cost {row["component"]} {money(row["total_usd"])}')
    print(f'usual {usual["spec"]} {usual["model"]} {money(usual["usd"])}')
    print(f'record {common.shown(pbi / RESULT)}')
    if result['draft']:
        print(f'draft {result["draft"]}')
    return EXIT_OK


def next_row(workflow: dict, components: dict) -> str:
    """heavy-ask でない最初の review の行 (ふつうの見直しの候補)。"""
    for item in workflow['stages']['review']:
        cid, when = flow.entry(item)
        if (when or components[cid]['when']) != HEAVY_WHEN:
            return cid
    return 'review.builtin'


def shown_after(pbi: Path, since: str) -> bool:
    """presentations.jsonl に、since 以後の kind decision の shown の行があるか。"""
    start = parse_time(since)
    try:
        lines = (pbi / present.LOG_NAME).read_text(encoding='utf-8').split('\n')
    except OSError:
        return False
    for line in lines:
        row = json_line(line)
        if (isinstance(row, dict) and row.get('event') == 'shown' and row.get('kind') == 'decision'
                and start is not None and (at := parse_time(row.get('ts'))) is not None and at >= start):
            return True
    return False


def json_line(line: str):
    try:
        return json.loads(line)
    except ValueError:
        return None


def parse_time(text):
    try:
        return datetime.datetime.fromisoformat(str(text))
    except ValueError:
        return None


def cmd_record(args, pbi: Path) -> int:
    result = common.load_json(pbi / RESULT)
    if not isinstance(result, dict) or result.get('schema') != SCHEMA:
        print(f'{common.shown(pbi / RESULT)}: run heavy_check.py check first', file=sys.stderr)
        return EXIT_USAGE
    try:
        refuse(args, result, pbi)
    except Refused as error:
        print(f'refused: {error}', file=sys.stderr)
        return EXIT_REFUSED
    held = [c['id'] for c in result.get('criteria', []) if c.get('holds')]
    path = pbi / 'adapters.json'
    data = common.load_json(path, {})
    if not isinstance(data, dict):
        data = {}
    data[RECORD_KEY] = {
        'heavy': result['verdict'] == 'heavy' and not (args.by == 'auto' and args.reason),
        'verdict': result['verdict'], 'criteria': held,
        'reason': args.reason or ', '.join(held) or 'no criterion holds',
        'decision': args.decision, 'asked': args.by == 'user', 'by': args.by,
        'diff_sha256': result.get('diff_sha256'), 'checked_at': result.get('checked_at'),
        'at': common.now().isoformat(timespec='seconds'),
    }
    common.write_json(path, data)
    if events is not None:
        rows = result.get('rows') or [{}]
        events.emit(pbi, 'stage.decided', {
            'component': rows[0].get('component', 'review'), 'decision': args.decision, 'by': args.by,
            'reason': 'approved' if args.decision == 'run' else 'declined' if args.by == 'user' else 'when-false'})
    print(f'heavy-review {args.decision} by {args.by} -> {common.shown(path)}')
    return EXIT_OK


def refuse(args, result: dict, pbi: Path) -> None:
    if args.decision == 'run' and args.by != 'user':
        raise Refused('only the user can say run (ask with the question page first)')
    if args.decision == 'run' and not shown_after(pbi, result.get('checked_at')):
        raise Refused('the question page was not shown after the check (present.py <draft> --kind decision)')
    if args.by == 'auto' and result.get('verdict') == 'heavy' and not args.reason:
        raise Refused('the check says heavy; skipping without asking needs --reason (why the diff is not deep)')


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='command', required=True)
    check = sub.add_parser('check', help='差分を測って基準に当て、heavy なら問いの原稿を書く')
    check.add_argument('pbi_dir')
    check.add_argument('--base', required=True, help='作業を入れる先 (例えば origin/main)')
    check.add_argument('--workflow', help='テンプレートの名前 (flow.py と同じ)')
    check.add_argument('--home', help='home (common.resolve_home)')
    check.add_argument('--root', default='.', help='リポジトリのルート (既定はカレント)')
    check.add_argument('--designer-model', default='opus', choices=FAMILIES,
                       help='設計者のモデルの系統 (inherit と取りまとめの分の単価)')
    check.add_argument('--usual-model', default='opus', choices=FAMILIES,
                       help='ふつうの見直しのレビュアーのモデルの系統 (Roles の reviewer.deep)')
    check.add_argument('--lang', default='ja', choices=('ja', 'en'), help='問いの原稿の言葉 (ユーザーの依頼の言葉)')
    check.add_argument('--draft', help=f'問いの原稿の置き場所 (既定 <pbi-dir>/{DEFAULT_DRAFT.as_posix()})')
    record = sub.add_parser('record', help='答えを adapters.json の heavy-review に書く')
    record.add_argument('pbi_dir')
    record.add_argument('--decision', required=True, choices=('run', 'skip'))
    record.add_argument('--by', required=True, choices=('user', 'auto'),
                        help='user はユーザーの答え、auto は聞かずに決めた skip')
    record.add_argument('--reason', help='決めた理由 (auto の skip で verdict が heavy のときは必須)')
    return ap.parse_args(argv)


def main(argv=None) -> int:
    common.utf8_stdout()
    args = parse_args(argv)
    pbi = Path(args.pbi_dir)
    if not pbi.is_dir():
        print(f'{args.pbi_dir}: not a directory', file=sys.stderr)
        return EXIT_USAGE
    try:
        return cmd_check(args, pbi) if args.command == 'check' else cmd_record(args, pbi)
    except GitError as error:
        print(str(error), file=sys.stderr)
        return EXIT_USAGE


if __name__ == '__main__':
    sys.exit(main())
