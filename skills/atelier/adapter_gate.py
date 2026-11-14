"""最終レビューとセキュリティレビューを使うかを、毎回同じ基準で決めて表で返す (appendix/review.md)。

  python adapter_gate.py <pbi-dir> [--numstat FILE] [--home DIR] [--skills-dir DIR] [--root DIR]

<pbi-dir> の PBI-<数字>.md を PBI として数える (.rework-1 や .brief の付いたファイルは数えない)。
PBI ごとに PBI-<n>.route.json の review (deep / normal) と PBI-<n>.facts.json の sensitive (真偽値) を読む。
route.json が無い・読めない PBI は deep、facts.json が無い・読めない PBI は sensitive でないとみなす。
--numstat は `git diff --numstat <merge-base>` (作業ツリーとの比較。未コミットの PBI の変更を含む) の出力。追加と削除の合計を diff の行数にし、
バイナリの行 (-  -  パス) は 0 行とする。文字コードは先頭のバイトで決め (UTF-16 の BOM があれば UTF-16、なければ utf-8-sig)、
空の行は無視する。空でない行が `追加<TAB>削除<TAB>パス` (0 以上の整数) でも `-<TAB>-<TAB>パス` でもないときは、
行数が不明として最終レビューを使う側 (run) に倒し、理由にその行番号 (1 始まり、最初の 1 つ) を書く。
--numstat を省いたときも行数は不明。
--home は common.resolve_home で解決し、フェーズごとの設定は agents.load_adapters で読む。

review は diff が 300 行以下・PBI が 2 件以下・deep も sensitive も無い、のすべてで skip。ほかは run。
security は sensitive か deep の PBI が 1 件でもあれば run。design はスクリプトでは決めず manual。
confirm は Approve の見せ方 (appendix/adapters.md、`## Confirm`)。値は html (既定)・md・off (auto とスキル名は agents.load_adapters が
html と読む)。resolve_confirm が、<skills-dir>/answer-me-with-any/scripts/am.mjs (無ければ旧名 answer-me-with-html) と node 20 以上の
有無から実際の見せ方 (format) と理由 (setting / missing-am / missing-node) を決める。decision は format (html / md)。
設定が off でないのに am か node が無いときは ask (reason に `format off (missing-am)` など。旧名の am なら旧名の warn を添える)。
どれでも何も入れない・作らない。skills-dir は無ければ ~/.claude/skills。
設定が off のフェーズは判定にかかわらず off で、理由は turned off in agents.json。
設定がスキル名のときは判定はそのまま (run / skip) で、setting の列に名前を出す (design・review・security)。

review-skill は最後のレビューに使うスキル (appendix/review.md、`## Pick the review`)。強い順に:
  1. agents.json の review がスキル名なら decision `agents.json` (利用者が決めたもの)
  2. --root (既定はカレント) の .claude/skills/*/SKILL.md と skills/*/SKILL.md のうち、フォルダの名前に review を
     含む最初のもの (名前の順) を decision `repo:<名前>`
  3. lens-review (lens-review:lens-review) がプラグインの置き場 (PBI_PLUGINS_DIR、無ければ ~/.claude/plugins) に
     あれば decision `lens-review:lens-review`
  4. どれも無ければ decision `template` (テンプレートの review の並び: code-review、組み込み)
規約のファイル (REVIEW.md・docs/review*.md・.github/review*.md) があれば reason に `rules: <パス>, ...` を足す
(どの決定でも、規約は最後のレビューの Rules: 行に渡す)。setting は review の設定のまま。off なら off。

標準出力はタブ区切りの表 (phase / setting / decision / reason。1 行目は見出し)。
agents.load_adapters の警告は表のあとに `warn <警告>` の形で 1 行ずつ出す。
表は design・review・review-skill・security・confirm の 5 行。
終了コード: 0 = 正常 (PBI 0 件も正常)。2 = <pbi-dir> が無い・--numstat が読めない・引数の誤り。
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import agents as agents_module
import common
import flow

# PBI として数えるファイルの形 (.rework-1・.brief などの付いたものは数えない)
PBI_NAME = re.compile(r'^PBI-(\d+)\.md$')

# 最終レビューを飛ばす上限 (diff の行数と PBI の数)
MAX_DIFF = 300
MAX_PBIS = 2

DESIGN_REASON = 'decided by the orchestrator (appendix/design.md)'
OFF_REASON = 'turned off in agents.json'
SECURITY_SKIP_REASON = 'no sensitive or deep PBI'

# am.mjs を流す node の最低の版
MIN_NODE_MAJOR = 20
# node --version を待つ上限 (秒)。止まった node で全体を止めない
NODE_TIMEOUT = 10
# Windows で子のコンソールのウインドウを出さない (ヘッドレス)。ほかの OS では 0
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def pbi_files(base) -> list:
    """<base> の PBI-<数字>.md を番号順に返す。"""
    found = []
    for path in Path(base).iterdir():
        match = PBI_NAME.fullmatch(path.name)
        if match and path.is_file():
            found.append((int(match.group(1)), path.name))
    return sorted(found)


def pbi_records(base) -> list:
    """PBI ごとの (名前, deep, deep の注記, sensitive) を番号順に返す。"""
    records = []
    for _, filename in pbi_files(base):
        stem = filename[:-len('.md')]
        route = common.load_json(Path(base) / f'{stem}.route.json')
        facts = common.load_json(Path(base) / f'{stem}.facts.json')
        records.append({
            'name': stem,
            'deep': not isinstance(route, dict) or route.get('review') == 'deep',
            'deep_note': '' if isinstance(route, dict) else ' (no route.json)',
            'sensitive': isinstance(facts, dict) and facts.get('sensitive') is True,
        })
    return records


def numstat_lines(path) -> tuple:
    """numstat の (追加と削除の合計, 行数が不明なときの理由) を返す。

    文字コードは先頭のバイトで決める (UTF-16 の BOM があれば UTF-16、なければ utf-8-sig)。
    空の行は無視する。空でない行が `追加<TAB>削除<TAB>パス` (0 以上の整数) でも
    バイナリの行 (`-<TAB>-<TAB>パス`) でもないときは、行数は不明として理由を返す。
    """
    data = Path(path).read_bytes()
    encoding = 'utf-16' if data[:2] in (b'\xff\xfe', b'\xfe\xff') else 'utf-8-sig'
    total = 0
    for number, line in enumerate(data.decode(encoding, errors='replace').splitlines(), start=1):
        if not line:
            continue
        cells = line.split('\t')
        if len(cells) >= 3 and cells[0] == '-' and cells[1] == '-':
            continue
        if len(cells) >= 3 and all(re.fullmatch(r'[0-9]+', cell) for cell in cells[:2]):
            total += int(cells[0]) + int(cells[1])
            continue
        return None, f'diff size unknown (unreadable numstat line {number})'
    return total, None


def deep_label(record) -> str:
    """deep の PBI の理由 (route.json が無いときはその旨を添える)。"""
    return f"{record['name']} is deep{record['deep_note']}"


def design_row(setting) -> tuple:
    if setting == 'off':
        return setting, 'off', OFF_REASON
    return setting, 'manual', DESIGN_REASON


def node_status(env=None) -> dict:
    """node の {"status": ok|old|missing, "path", "version"}。20 以上で ok。

    env の PATH で node を探し (env を省くと os.environ)、`node --version` を読む。
    流せない・止まった・終了コードが 0 でない・版が読めないときは、例外を出さず missing (版が読めて 20 未満は old)。
    """
    env = os.environ if env is None else env
    found = shutil.which('node', path=env.get('PATH'))
    result = {'status': 'missing', 'path': found, 'version': None}
    if not found:
        return result
    try:
        run = subprocess.run([found, '--version'], capture_output=True, text=True, encoding='utf-8',
                             timeout=NODE_TIMEOUT, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError, ValueError):
        return result
    if run.returncode != 0:
        return result
    version = run.stdout.strip()
    match = re.fullmatch(r'v?([0-9]+)(?:\.[0-9]+)*.*', version)
    if match is None:
        return result
    result['version'] = version
    result['status'] = 'ok' if int(match.group(1)) >= MIN_NODE_MAJOR else 'old'
    return result


def find_am(skills_dir=None) -> tuple:
    """(am.mjs のパス or None, 旧名で見つけたか)。answer-me-with-any を先に、次に旧名 answer-me-with-html を見る。

    skills_dir を省くと ATELIER_SKILLS_DIR、無ければ ~/.claude/skills。
    """
    if skills_dir is None:
        skills_dir = os.environ.get('ATELIER_SKILLS_DIR') or Path.home() / '.claude' / 'skills'
    for name, old in ((agents_module.CONFIRM_SKILL, False), (agents_module.CONFIRM_SKILL_OLD, True)):
        candidate = Path(skills_dir) / name / 'scripts' / 'am.mjs'
        if candidate.is_file():
            return candidate, old
    return None, False


def resolve_confirm(setting, skills_dir, env) -> dict:
    """Approve の見せ方を決める。{"setting","format","reason","am","node","warnings"}。

    setting は html・md・off (agents.load_adapters を通した値)。off なら format off・reason setting。
    am.mjs が無ければ format off・reason missing-am。node が無いか 20 未満なら format off・reason missing-node。
    ほかは format=setting・reason setting。旧名で am を見つけたら warnings に旧名の warn。
    off のときと am が無いときは node を起動しない (node は None)。node は 20 以上の node のパス。
    """
    resolved = {'setting': setting, 'format': 'off', 'reason': 'setting', 'am': None, 'node': None, 'warnings': []}
    if setting == 'off':
        return resolved
    am, old = find_am(skills_dir)
    if am is None:
        resolved['reason'] = 'missing-am'
        return resolved
    resolved['am'] = am.as_posix()
    if old:
        resolved['warnings'].append(f'confirm: old name {agents_module.CONFIRM_SKILL_OLD}, '
                                    f'install {agents_module.CONFIRM_SKILL} and remove it')
    node = node_status(env)
    if node['status'] != 'ok':
        resolved['reason'] = 'missing-node'
        return resolved
    resolved['node'] = node['path']
    resolved['format'] = setting
    return resolved


def confirm_row(setting, skills_dir, env) -> tuple:
    """Approve の見せ方の行 (setting, decision, reason)。decision は html・md・off・ask。

    setting が off なら off。am か node が無いときは ask (入れ方を選択肢に書く合図) で、reason に format と reason
    (例 `format off (missing-am)`) を書く。旧名の am なら reason に旧名の warn を添える。
    """
    if setting == 'off':
        return setting, 'off', OFF_REASON
    resolved = resolve_confirm(setting, skills_dir, env)
    if resolved['format'] != 'off':
        reason = f"format {resolved['format']} (am at {resolved['am']})"
        decision = resolved['format']
    else:
        reason = f"format off ({resolved['reason']})"
        decision = 'ask'
    for warning in resolved['warnings']:
        reason += f'; warn: {warning}'
    return setting, decision, reason


def review_row(setting, diff, records, unknown_reason='diff size unknown (no --numstat)') -> tuple:
    if setting == 'off':
        return setting, 'off', OFF_REASON
    unmet = []
    if diff is None:
        unmet.append(unknown_reason)
    elif diff > MAX_DIFF:
        unmet.append(f'diff {diff} lines > {MAX_DIFF}')
    if len(records) > MAX_PBIS:
        unmet.append(f'{len(records)} PBIs > {MAX_PBIS}')
    unmet += [deep_label(record) for record in records if record['deep']]
    unmet += [f"{record['name']} is sensitive" for record in records if record['sensitive']]
    if unmet:
        return setting, 'run', '; '.join(unmet)
    return setting, 'skip', f'diff {diff} lines, {len(records)} PBIs, no deep or sensitive'


REVIEW_SKILL_DIRS = ('.claude/skills', 'skills')
REVIEW_RULES = ('REVIEW.md', 'docs/review*.md', '.github/review*.md')
REVIEW_SETTINGS = ('auto', 'off')
LENS_REVIEW = 'lens-review:lens-review'


def review_skill_row(setting, root, env=None) -> tuple:
    """最後のレビューに使うリポジトリ独自のスキル (decision repo:<名前> か none) と規約 (reason の rules:)。"""
    root = Path(root)
    rules = sorted({path.as_posix() for pattern in REVIEW_RULES for path in root.glob(pattern) if path.is_file()})
    rules_note = f"; rules: {', '.join(Path(rule).relative_to(root).as_posix() for rule in rules)}" if rules else ''
    if setting == 'off':
        return setting, 'off', OFF_REASON
    if setting not in REVIEW_SETTINGS:
        return setting, 'agents.json', f'agents.json names {setting}; it wins over the repository{rules_note}'
    for folder in REVIEW_SKILL_DIRS:
        for skill in sorted((root / folder).glob('*/SKILL.md')):
            if 'review' in skill.parent.name.lower():
                return setting, f'repo:{skill.parent.name}', f'found {skill.relative_to(root).as_posix()}{rules_note}'
    none_here = f"no review skill in {' or '.join(REVIEW_SKILL_DIRS)}"
    found = flow.find_skill_file(LENS_REVIEW, Path(), env or os.environ)
    if found is not None:
        return setting, LENS_REVIEW, f'{none_here}; {LENS_REVIEW} at {found.as_posix()}{rules_note}'
    return setting, 'template', f'{none_here}; {LENS_REVIEW} not installed{rules_note}'


def security_row(setting, records) -> tuple:
    if setting == 'off':
        return setting, 'off', OFF_REASON
    hits = [f"{record['name']} is sensitive" for record in records if record['sensitive']]
    hits += [deep_label(record) for record in records if record['deep']]
    if hits:
        return setting, 'run', '; '.join(hits)
    return setting, 'skip', SECURITY_SKIP_REASON


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('pbi_dir', help='作業の docs/pbi (PBI-<数字>.md の置き場所)')
    ap.add_argument('--numstat', help='git diff --numstat の出力を保存したファイル')
    ap.add_argument('--home', help='agents.json を置く home (common.resolve_home)')
    ap.add_argument('--skills-dir', help='スキルの置き場 (既定は ~/.claude/skills)')
    ap.add_argument('--root', default='.', help='リポジトリのルート (review-skill を探す。既定はカレント)')
    args = ap.parse_args(argv)
    common.utf8_stdout()

    base = Path(args.pbi_dir)
    if not base.is_dir():
        print(f'{args.pbi_dir}: not a directory', file=sys.stderr)
        return 2

    diff = None
    unknown_reason = 'diff size unknown (no --numstat)'
    if args.numstat:
        try:
            diff, reason = numstat_lines(args.numstat)
        except OSError as error:
            print(f'{args.numstat}: {error}', file=sys.stderr)
            return 2
        if diff is None:
            unknown_reason = reason

    skills = Path(args.skills_dir) if args.skills_dir else Path.home() / '.claude' / 'skills'
    settings, warnings = agents_module.load_adapters(common.resolve_home(args.home))
    records = pbi_records(base)
    rows = [('design', design_row(settings['design'])),
            ('review', review_row(settings['review'], diff, records, unknown_reason)),
            ('review-skill', review_skill_row(settings['review'], args.root, os.environ)),
            ('security', security_row(settings['security'], records)),
            ('confirm', confirm_row(settings['confirm'], skills, os.environ))]

    print('phase\tsetting\tdecision\treason')
    for phase, (setting, decision, reason) in rows:
        print(f'{phase}\t{setting}\t{decision}\t{reason}')
    for warning in warnings:
        print(f'warn {warning}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
