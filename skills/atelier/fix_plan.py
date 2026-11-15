"""修正の段 (fix) の直しの一覧から、直しの PBI と波を書き、直しの差分を出す (appendix/fix.md)。

  python fix_plan.py plan [docs/pbi] [--round <r>] [--dry-run]
  python fix_plan.py diff [docs/pbi] PBI-<n>

最後の見直しは 1 回で、確かめた指摘は 1 つの直しの一覧 (<dir>/fix/round-<r>.json) になる。PBI ごとに差し戻さず、
修正の段がこの一覧を一度に直す (docs/specs/fix-stage-once.md)。

一覧の形 (atelier.fixes/1):
  {"schema": "atelier.fixes/1", "round": 1, "source": "review" | "e2e", "tier": "sonnet",
   "checks": ["<型・単体テストのコマンド>", ...],
   "e2e_round": <e2e の回 (source e2e のとき)>, "rerun": ["<流し直す spec>", ...],
   "items": [{"id": "F1", "severity": "critical|major|minor|e2e", "file": "<path>", "line": 12,
              "files": ["<ほかに触るファイル>"], "what": "<何が悪いか>", "fix": "<直し方>",
              "check": "<その指摘を確かめるコマンド>" | null, "pbi": "PBI-003" | null,
              "decision": "fix|skip|ask", "recommend": "fix|skip|code|adr", "from": "review|security|lint|e2e",
              "answer": "<ユーザーの答え>"}]}

plan: decision が fix の項目を、ファイルを共にする項目どうしでまとめ (つながったファイルの塊)、塊を MAX_WORKERS 人の
作業者に、項目の数が釣り合うように分ける。1 人分を 1 つの直しの PBI (PBI-<n>.md・.facts.json・.brief.md) にし、
waves.json の後ろに 1 つの波 ({"n", "pbis", "kind": "fix", "round"}) を足す。同じ波の PBI はファイルが重ならないので
並べて走らせられる。PBI に属さないファイル (仕様・ADR など) も Targets にできる。
  - PBI の頭に `- Fix round: <r>` と `- Fix items: F1, F3`。Targets はその人の項目のファイルだけ、Do not touch は
    それ以外のすべて。受け入れ条件は項目ごとに 1 つと整形・lint。検証コマンドは checks.json の整形と lint
    (Targets に。verify.py が求める)、一覧の checks、項目の check (重なりは 1 つに)。
  - 直す前の Targets を <dir>/fix/round-<r>/base/ に写す (base.json に、在ったかどうか)。既に写したファイルは
    写し直さない (流し直しても直す前の形のまま)。
  - 同じ回の PBI が既に波に入っていれば、項目が同じなら何も書かない (kept)。波に入っていなければ (前の実行が途中で
    止まった) 同じ番号で書き直して波に入れる。項目が変わっていれば例外 (新しい回にする)。
diff: その直しの PBI の Targets を、写しと比べた unified diff (直しの差分だけ) を出す。PBI ごとの見直しはこれを読む。

例外 (exception: の行) は次のどれか。例外のある項目は PBI にしない。
  - 一覧の schema が違う、items が配列でない、round が整数でない
  - id が空か重なる、decision が fix・skip・ask のどれでもない、severity が知らない値
  - decision が ask のまま (作業者を起こす前にユーザーに聞いて、answer と decision を書く)。ask が 1 つでも残って
    いれば何も書かない (答えのあとに、直す項目をすべて 1 つの波にまとめるため)
  - fix の項目の what・fix が空、file が無い、パスが絶対・`..` を含む・<dir> の中

出力: 書いた PBI ごとに `wrote PBI-<n> (<k> items: F1,F3; files: <path>, ...)`、足した波に `wave <n>: <PBI,...>`、
fix でない項目ごとに `skipped <id> (<decision>)`、例外ごとに `exception: <id>: <理由>`。書いたら
`then: python <skill>/design_record.py work <PBI>` と `then: python <skill>/prepare.py <dir> --only <PBI,...> --tier <tier>`。
終了コード: 0 = 例外なし。4 = 例外あり (例外の無い項目の PBI は書いた)。2 = 一覧が無い・読めない、diff の PBI が
直しの PBI でない・写しが無い。
"""
import argparse
import difflib
import re
import sys
from pathlib import Path, PurePosixPath

import common
import verify

SKILL = Path(__file__).resolve().parent
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_EXCEPTIONS = 4
SCHEMA = 'atelier.fixes/1'
MAX_WORKERS = 3
DECISIONS = ('fix', 'skip', 'ask')
SEVERITIES = ('critical', 'major', 'minor', 'e2e')
DEFAULT_TIER = 'sonnet'
ROUND_FILE = re.compile(r'^round-(\d+)\.json$')
PBI_FILE = re.compile(r'^PBI-(\d+)\.md$')
FIX_ROUND_LINE = re.compile(r'^\s*-\s*Fix round:\s*(\d+)\s*$', re.M)
FIX_ITEMS_LINE = re.compile(r'^\s*-\s*Fix items:\s*(.+?)\s*$', re.M)
SECTION = re.compile(r'^##\s+(.+?)\s*$', re.M)
TARGET_LINE = re.compile(r'^\s*-\s*`([^`]+)`')


def round_path(base: Path, number: int) -> Path:
    return base / 'fix' / f'round-{number}.json'


def latest_round(base: Path):
    numbers = [int(m.group(1)) for m in (ROUND_FILE.match(p.name) for p in (base / 'fix').glob('round-*.json')) if m]
    return max(numbers) if numbers else None


def norm_path(text: str) -> str:
    return PurePosixPath(text.strip().replace('\\', '/')).as_posix().removeprefix('./')


def path_problem(path: str, base: Path) -> str | None:
    if not path:
        return 'a file is empty'
    pure = PurePosixPath(path)
    if pure.is_absolute() or re.match(r'^[A-Za-z]:', path):
        return f'{path}: an absolute path (give it relative to the work root)'
    if '..' in pure.parts:
        return f'{path}: has ..'
    inside = norm_path(base.as_posix()).rstrip('/') + '/'
    if (path + '/').startswith(inside):
        return f'{path}: inside {inside.rstrip("/")} (a run record, not a fix target)'
    return None


def item_files(item: dict) -> list[str]:
    files = [item.get('file')] + list(item.get('files') or [])
    out = []
    for path in files:
        if isinstance(path, str) and path.strip():
            path = norm_path(path)
            if path not in out:
                out.append(path)
    return out


def check_items(data: dict, base: Path) -> tuple[list[dict], list[str], list[str]]:
    """(直す項目, 直さない項目の行, 例外の行)。"""
    problems, skipped, fixes = [], [], []
    items = data.get('items')
    if not isinstance(items, list):
        return [], [], ['list: items is not an array']
    seen = set()
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            problems.append(f'item {index}: not an object')
            continue
        name = item.get('id') if isinstance(item.get('id'), str) and item['id'].strip() else f'item {index}'
        found = []
        if name == f'item {index}':
            found.append('no id')
        elif name in seen:
            found.append('the id is used twice')
        seen.add(name)
        decision = item.get('decision')
        if decision not in DECISIONS:
            found.append(f"decision {decision!r} is not one of {', '.join(DECISIONS)}")
        if item.get('severity') not in SEVERITIES:
            found.append(f"severity {item.get('severity')!r} is not one of {', '.join(SEVERITIES)}")
        if decision == 'ask':
            found.append('decision is still ask: ask the user before any worker, then write answer and decision')
        if decision == 'fix':
            for key in ('what', 'fix'):
                if not (isinstance(item.get(key), str) and item[key].strip()):
                    found.append(f'{key} is empty')
            files = item_files(item)
            if not files:
                found.append('no file')
            for path in files:
                problem = path_problem(path, base)
                if problem:
                    found.append(problem)
            line = item.get('line')
            if line is not None and (isinstance(line, bool) or not isinstance(line, int)):
                found.append(f'line {line!r} is not an integer')
        if found:
            problems += [f'{name}: {problem}' for problem in dict.fromkeys(found)]
        elif decision == 'fix':
            fixes.append(item)
        else:
            skipped.append(f'skipped {name} ({decision})')
    return fixes, skipped, problems


def groups(fixes: list[dict]) -> list[list[dict]]:
    """ファイルを共にする項目どうしの塊 (一覧の順)。"""
    parent: dict[str, str] = {}

    def find(path):
        while parent.setdefault(path, path) != path:
            parent[path] = parent[parent[path]]
            path = parent[path]
        return path

    for item in fixes:
        files = item_files(item)
        for path in files[1:]:
            parent[find(path)] = find(files[0])
        find(files[0])
    chunks: dict[str, list[dict]] = {}
    for item in fixes:
        chunks.setdefault(find(item_files(item)[0]), []).append(item)
    return list(chunks.values())


def pack(chunks: list[list[dict]], workers: int = MAX_WORKERS) -> list[list[dict]]:
    """塊を workers 人に、項目の数が釣り合うように分ける (大きい塊から、いちばん少ない人へ)。順は一覧の順に戻す。"""
    order = {id(item): i for i, chunk in enumerate(chunks) for item in chunk}
    bins: list[list[dict]] = [[] for _ in range(min(workers, len(chunks)))]
    for chunk in sorted(chunks, key=len, reverse=True):
        min(bins, key=len).extend(chunk)
    return [sorted(group, key=lambda item: order[id(item)]) for group in bins if group]


def pbi_numbers(base: Path) -> list[int]:
    return sorted(int(m.group(1)) for m in (PBI_FILE.match(p.name) for p in base.glob('PBI-*.md')) if m)


def planned(base: Path, number: int) -> dict[str, list[str]]:
    """この回の直しの PBI → その項目の id。"""
    out = {}
    for path in sorted(base.glob('PBI-*.md')):
        if not PBI_FILE.match(path.name):
            continue
        text = path.read_text(encoding='utf-8', errors='replace')
        head = FIX_ROUND_LINE.search(text)
        if head and int(head.group(1)) == number:
            items = FIX_ITEMS_LINE.search(text)
            out[path.stem] = [word.strip() for word in (items.group(1) if items else '').split(',') if word.strip()]
    return out


def waved(base: Path) -> set[str]:
    data = common.load_json(base / 'waves.json', {})
    waves = data.get('waves') if isinstance(data, dict) else None
    return {pbi for wave in waves or [] if isinstance(wave, dict) for pbi in wave.get('pbis') or []}


def location(item: dict) -> str:
    line = item.get('line')
    first = item_files(item)[0]
    return f'{first}:{line}' if isinstance(line, int) and not isinstance(line, bool) else first


def pbi_text(name: str, number: int, group: list[dict], data: dict, base: Path, checks: list[str],
             others: list[str]) -> tuple[str, int]:
    files = []
    for item in group:
        files += [path for path in item_files(item) if path not in files]
    joined = ' '.join(files)
    verify_cmds = [f'{head} {joined}' for head in checks]
    for cmd in [c for c in data.get('checks') or [] if isinstance(c, str) and c.strip()]:
        if cmd.strip() not in verify_cmds:
            verify_cmds.append(cmd.strip())
    criteria = []
    for item in group:
        line = f"{item['id']} ({location(item)}, {item['severity']}): {item['what'].strip()} — fixed as: " \
               f"{item['fix'].strip()}"
        check = item.get('check')
        if isinstance(check, str) and check.strip():
            if check.strip() not in verify_cmds:
                verify_cmds.append(check.strip())
            line += f'; verify {verify_cmds.index(check.strip()) + 1} exits 0'
        criteria.append(line + '.')
    if checks:
        criteria.append(f"Format and lint: verify {', '.join(str(i) for i in range(1, len(checks) + 1))} exit 0.")
    rest = list(range(len(checks) + 1, len(verify_cmds) + 1))
    if rest:
        criteria.append(f"The other checks: verify {', '.join(str(i) for i in rest)} exit 0.")
    dont = ['- Every file outside the Targets, the other items of the fix list included.']
    if others:
        dont.append('- In particular the files of the other fix PBIs of this round: '
                    + ', '.join(f'`{path}`' for path in others))
    notes = [f'- Fix list: {round_path(base, number).as_posix()} (source {data.get("source", "review")})',
             f'- Review the fix only: `python {(SKILL / "fix_plan.py").as_posix()} diff {base.as_posix()} {name}` '
             f'(the change since the copy taken before the fix, in {(base / "fix" / f"round-{number}" / "base").as_posix()})',
             '- Fix each item as its line says and nothing else. A fix that needs a file outside the Targets is a '
             'finding for the report, not a change.', '', 'Items:']
    for item in group:
        owner = item.get('pbi') or 'none'
        notes.append(f"- {item['id']} [{item['severity']}, from {item.get('from') or 'review'}, PBI {owner}] "
                     f"{location(item)}: {item['what'].strip()} / fix: {item['fix'].strip()}"
                     + (f" / answer: {item['answer']}" if isinstance(item.get('answer'), str) and item['answer'] else ''))
    text = '\n'.join([
        f'# {name}: Fixes of round {number} ({len(group)} items)', '',
        '- Model: <set by prepare.py>', '- Depends on: none', '- Related specs: none',
        f'- Fix round: {number}', f"- Fix items: {', '.join(item['id'] for item in group)}", '',
        '## Goal',
        f'Fix the items of the fix list listed below at once, as the final review (or the E2E round) found them, '
        'so no second final review is needed.', '',
        '## Targets', *(f'- `{path}`' for path in files), '',
        '## Do not touch', *dont, '',
        '## Acceptance criteria', *(f'{i}. {line}' for i, line in enumerate(criteria, start=1)), '',
        '## Verify commands', *(f'{i}. `{cmd}`' for i, cmd in enumerate(verify_cmds, start=1)), '',
        '## Refs', 'none', '',
        '## Notes', *notes, ''])
    return text, len(criteria)


def brief_text(group: list[dict], files: list[str]) -> str:
    return '\n'.join([f"Goal: fix {len(group)} listed findings in {len(files)} files",
                      'Kind of change: a fix to existing code or documents', 'New abstraction: none',
                      'External interaction: none', 'Invariants to keep: behaviour outside the listed findings',
                      'Judgment needed: none (each finding names the fix)', ''])


def snapshot(base: Path, number: int, files: list[str]) -> None:
    """直す前の形を写す。既に写したものは写し直さない。"""
    folder = base / 'fix' / f'round-{number}'
    manifest = common.load_json(folder / 'base.json', {})
    manifest = manifest if isinstance(manifest, dict) and isinstance(manifest.get('files'), dict) else {'files': {}}
    for path in files:
        if path in manifest['files']:
            continue
        source = Path(path)
        exists = source.is_file()
        manifest['files'][path] = exists
        if exists:
            target = folder / 'base' / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
    common.write_json(folder / 'base.json', manifest)


def cmd_plan(args) -> int:
    base = Path(args.dir)
    number = args.round if args.round is not None else latest_round(base)
    if number is None:
        print(f'fix_plan: no fix list in {(base / "fix").as_posix()} (round-<r>.json)', file=sys.stderr)
        return EXIT_USAGE
    path = round_path(base, number)
    data = common.load_json(path, None)
    if not isinstance(data, dict):
        print(f'fix_plan: cannot read {path.as_posix()}', file=sys.stderr)
        return EXIT_USAGE
    head = []
    if data.get('schema') != SCHEMA:
        head.append(f"list: schema {data.get('schema')!r} is not {SCHEMA}")
    if data.get('round') != number:
        head.append(f"list: round {data.get('round')!r} is not {number} (the file name)")
    if head:
        for line in head:
            print(f'exception: {line}')
        return EXIT_EXCEPTIONS
    fixes, skipped, problems = check_items(data, base)
    for line in skipped:
        print(line)
    for line in problems:
        print(f'exception: {line}')
    exceptions = bool(problems)
    asked = [item for item in data.get('items') or [] if isinstance(item, dict) and item.get('decision') == 'ask']
    if asked:
        # 聞く項目が残っているうちは何も書かない: 答えのあとに、直す項目をすべて 1 つの波にまとめるため
        print(f'fix_plan: {len(asked)} items still ask; ask the user first, then plan again (nothing written)')
        return EXIT_EXCEPTIONS
    if not fixes:
        if not exceptions:
            print(f'fix_plan: nothing to fix in round {number}')
        return EXIT_EXCEPTIONS if exceptions else EXIT_OK
    bins = pack(groups(fixes))
    done = planned(base, number)
    in_waves = waved(base)
    wanted = sorted(item['id'] for item in fixes)
    if done:
        have = sorted(i for ids in done.values() for i in ids)
        if all(pbi in in_waves for pbi in done):
            if have == wanted:
                for pbi, ids in done.items():
                    print(f"kept {pbi} ({', '.join(ids)}; already planned)")
                return EXIT_EXCEPTIONS if exceptions else EXIT_OK
            print(f'exception: round {number}: already planned with other items ({", ".join(have)}); '
                  f'write the new items as round {number + 1}')
            return EXIT_EXCEPTIONS
    # 波に入っていない同じ回の PBI (前の実行が途中で止まった) は、同じ番号から使い直す
    reuse = sorted(pbi for pbi in done if pbi not in in_waves)
    last = (pbi_numbers(base) or [0])[-1]
    names = []
    for _ in bins:
        if reuse:
            names.append(reuse.pop(0))
        else:
            last += 1
            names.append(f'PBI-{last:03d}')
    checks = list(verify.required_checks(base).values())
    files_of = []
    for group in bins:
        files = []
        for item in group:
            files += [p for p in item_files(item) if p not in files]
        files_of.append(files)
    if not args.dry_run:
        snapshot(base, number, [p for files in files_of for p in files])
    for name, group, files in zip(names, bins, files_of):
        others = [p for other in files_of if other is not files for p in other]
        body, criteria = pbi_text(name, number, group, data, base, checks, others)
        if not args.dry_run:
            (base / f'{name}.md').write_text(body, encoding='utf-8', newline='\n')
            common.write_json(base / f'{name}.facts.json', {
                'files_changed': len(files), 'files_new': sum(1 for p in files if not Path(p).exists()),
                'files_deleted': 0, 'modules': len({PurePosixPath(p).parts[0] for p in files}),
                'criteria': criteria, 'sensitive': False})
            (base / f'{name}.brief.md').write_text(brief_text(group, files), encoding='utf-8', newline='\n')
        print(f"wrote {name} ({len(group)} items: {','.join(item['id'] for item in group)}; "
              f"files: {', '.join(files)})")
    waves = common.load_json(base / 'waves.json', {})
    waves = waves if isinstance(waves, dict) and isinstance(waves.get('waves'), list) else {'waves': []}
    n = max((wave.get('n', 0) for wave in waves['waves'] if isinstance(wave, dict)), default=0) + 1
    waves['waves'].append({'n': n, 'pbis': names, 'kind': 'fix', 'round': number})
    print(f"wave {n}: {','.join(names)}")
    if not args.dry_run:
        common.write_json(base / 'waves.json', waves)
    for name in names:
        print(f"then: python {(SKILL / 'design_record.py').as_posix()} work {name}")
    tier = data.get('tier', DEFAULT_TIER)
    route = f"then: python {(SKILL / 'prepare.py').as_posix()} {base.as_posix()} --only {','.join(names)}"
    print(route + (f' --tier {tier}' if isinstance(tier, str) and tier.strip() else ''))
    return EXIT_EXCEPTIONS if exceptions else EXIT_OK


def targets_of(text: str) -> list[str]:
    heads = list(SECTION.finditer(text))
    for i, head in enumerate(heads):
        if head.group(1).strip().lower() == 'targets':
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            return [norm_path(m.group(1)) for m in
                    (TARGET_LINE.match(line) for line in text[head.end():end].splitlines()) if m]
    return []


def lines_of(data: bytes):
    try:
        return data.decode('utf-8').splitlines(keepends=True)
    except UnicodeDecodeError:
        return None


def cmd_diff(args) -> int:
    common.utf8_stdout()
    base = Path(args.dir)
    path = base / f'{args.pbi}.md'
    try:
        text = path.read_text(encoding='utf-8')
    except OSError as error:
        print(f'fix_plan: cannot read {path.as_posix()}: {error}', file=sys.stderr)
        return EXIT_USAGE
    head = FIX_ROUND_LINE.search(text)
    if head is None:
        print(f'fix_plan: {args.pbi} is not a fix PBI (no "- Fix round:" line)', file=sys.stderr)
        return EXIT_USAGE
    number = int(head.group(1))
    folder = base / 'fix' / f'round-{number}'
    manifest = common.load_json(folder / 'base.json', None)
    if not isinstance(manifest, dict) or not isinstance(manifest.get('files'), dict):
        print(f'fix_plan: no copy before the fix ({(folder / "base.json").as_posix()})', file=sys.stderr)
        return EXIT_USAGE
    for target in targets_of(text):
        if target not in manifest['files']:
            print(f'not copied: {target} (no copy before the fix; read `git diff -- {target}`)')
            continue
        before = (folder / 'base' / target).read_bytes() if manifest['files'][target] else b''
        now_path = Path(target)
        now = now_path.read_bytes() if now_path.is_file() else b''
        if before == now:
            print(f'no change: {target}')
            continue
        old, new = lines_of(before), lines_of(now)
        if old is None or new is None:
            print(f'binary: {target} changed')
            continue
        sys.stdout.writelines(difflib.unified_diff(
            old, new, fromfile=f'a/{target} (before the fix)' if manifest['files'][target] else '/dev/null',
            tofile=f'b/{target}' if now_path.is_file() else '/dev/null'))
        if new and not new[-1].endswith('\n'):
            print()
    return EXIT_OK


def main(argv=None) -> int:
    common.utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest='command', required=True)
    plan = sub.add_parser('plan', help='直しの一覧から直しの PBI と波を書く')
    plan.add_argument('dir', nargs='?', default='docs/pbi')
    plan.add_argument('--round', type=int, help='一覧の回 (既定はいちばん新しい round-<r>.json)')
    plan.add_argument('--dry-run', action='store_true', help='書く予定と例外を出すだけで、何も書かない')
    diff = sub.add_parser('diff', help='直しの PBI の、直す前の写しとの差分を出す')
    diff.add_argument('dir', nargs='?', default='docs/pbi')
    diff.add_argument('pbi', help='PBI-<n>')
    args = ap.parse_args(argv)
    if args.command == 'diff':
        return cmd_diff(args)
    return cmd_plan(args)


if __name__ == '__main__':
    sys.exit(main())
