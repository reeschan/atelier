"""テストの工程の survey (docs/pbi/notes/test-survey.md) から、テスト PBI を書く (appendix/tests-at-end.md、`## Test stage`)。

  python tests_plan.py [docs/pbi] [--survey <path>] [--dry-run]

人もモデルも survey の表を読み直さずに済むよう、表とケースからテスト PBI をそのまま組み、担当が見るのは
lint が出した例外の行だけにする。

survey の形 (テスト担当が書く。prompts/tester.md の survey):
  | file:test | verdict | reason | action |      verdict は keep・fix・rewrite・delete・add
  `### Cases: <test file>` の見出しの下に `Exemplar: <path>` の行とケースの表
  | id | behaviour | input | expected | mock |
  file:test は `<file>:<test>` か `<file>::<test>`。add の行はテストの名前を省いてよい。
  delete の reason は `covered by <file:test>` か `none needed` で始める。

test file ごとに 1 つの PBI (PBI-<n>.md・.facts.json・.brief.md) を書き、waves.json の後ろにテストの波を足す
(1 つの波に MAX_PER_WAVE 件まで。テストの実行が同じ機械を分け合うため)。Targets はその test file だけ、
Do not touch はそれ以外 (build PBI の Targets を名指す)、Notes に Exemplar:・Cases:・Delete:・fix と rewrite の
action、受け入れ条件はケース id ごとと Delete: ごと、検証コマンドは checks.json の整形と lint (その file に。
verify.py が求める)、tests-at-end.json の test_command ({file} を置き換える) と test_typecheck。keep だけの file には PBI を作らない。既に `- Test file: <file>` の PBI があり
waves.json の波に入っていれば書き直さない (流し直しても同じ PBI を二重に作らない)。波に入っていなければ (前の実行が
途中で止まった) 同じ番号で書き直して波に入れる。

lint (例外) は次のどれか。例外のある file の PBI は書かない。
  - tests-at-end.json に test_command が無い (このときは何も書かない)
  - test_paths に合わない file
  - fix・rewrite・add の行があるのにケースの節が無い、Exemplar: の行が無い、ケースが 1 つも無い
  - ケースの欄が空か `?` を含む、id が重なる
  - delete の reason が covered by か none needed で始まらない
  - 同じテストに keep と delete が並ぶ
  - 表の verdict が 5 つのどれでもない、file:test が空

出力: 書いた PBI ごとに `wrote PBI-<n> <file> (<k> cases, <d> deletes)`、足した波ごとに `wave <n>: <PBI,...>`、
例外ごとに `exception: <file>: <理由>`。tests-at-end.json に implement_tier があれば
`then: python <skill>/prepare.py <dir> --only <PBI,...> --tier <tier>` を出す。
終了コード: 0 = 例外なし。4 = 例外あり (例外の無い file の PBI は書いた)。2 = survey が無い・読めない。
"""
import argparse
import fnmatch
import json
import re
import sys
from pathlib import Path

import common
import verify

SKILL = Path(__file__).resolve().parent
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_EXCEPTIONS = 4
MAX_PER_WAVE = 3
VERDICTS = ('keep', 'fix', 'rewrite', 'delete', 'add')
NEEDS_CASES = ('fix', 'rewrite', 'add')
CASE_FIELDS = ('id', 'behaviour', 'input', 'expected', 'mock')
DELETE_REASONS = ('covered by', 'none needed')

PBI_FILE = re.compile(r'^PBI-(\d+)\.md$')
TEST_FILE_LINE = re.compile(r'^\s*-\s*Test file:\s*`?([^`\s]+)`?\s*$', re.M)
CASES_HEAD = re.compile(r'^#{2,4}\s+Cases:\s*`?([^`\s]+)`?\s*$')
EXEMPLAR = re.compile(r'^\s*Exemplar:\s*(.+?)\s*$')
SECTION = re.compile(r'^##\s+(.+?)\s*$', re.M)
PATH_WORD = re.compile(r'`([^`\s]+)`|(?<![\w/.-])([\w.-]+(?:/[\w.$\[\]-]+)+)')


def cells(line: str) -> list[str] | None:
    line = line.strip()
    if not line.startswith('|') or not line.endswith('|'):
        return None
    return [cell.strip() for cell in line[1:-1].split('|')]


def is_rule(row: list[str]) -> bool:
    return all(re.fullmatch(r':?-{3,}:?', cell) for cell in row)


def split_name(text: str) -> tuple[str, str]:
    text = text.strip().strip('`')
    if '::' in text:
        file, _, test = text.partition('::')
    else:
        file, _, test = text.partition(':')
    return file.strip(), test.strip()


def parse_survey(text: str) -> tuple[list[dict], dict[str, dict], list[str]]:
    """(表の行 [{file, test, verdict, reason, action}], file → {exemplar, cases}, 形の誤り) を返す。"""
    rows, cases, errors = [], {}, []
    current = None
    header = None
    for raw in text.splitlines():
        head = CASES_HEAD.match(raw.strip())
        if head:
            current = cases.setdefault(head.group(1), {'exemplar': None, 'cases': []})
            header = None
            continue
        if current is not None:
            exemplar = EXEMPLAR.match(raw)
            if exemplar:
                current['exemplar'] = exemplar.group(1)
                continue
        row = cells(raw)
        if row is None:
            if raw.startswith('#'):
                current = None if not CASES_HEAD.match(raw.strip()) else current
                header = None
            continue
        if is_rule(row):
            continue
        lowered = [cell.lower() for cell in row]
        if lowered[:2] == ['file:test', 'verdict'] or lowered[:1] == ['id']:
            header = lowered
            continue
        if current is not None and header and header[0] == 'id':
            current['cases'].append(dict(zip(CASE_FIELDS, row + [''] * (len(CASE_FIELDS) - len(row)))))
        elif header and header[0] == 'file:test' and len(row) >= 4:
            file, test = split_name(row[0])
            verdict = row[1].lower()
            if not file:
                errors.append(f'a row has no file:test: {raw.strip()}')
                continue
            rows.append({'file': file, 'test': test, 'verdict': verdict, 'reason': row[2], 'action': row[3]})
    return rows, cases, errors


def file_exceptions(file: str, rows: list[dict], section: dict | None, test_paths: list[str]) -> list[str]:
    problems = []
    if test_paths and not any(fnmatch.fnmatch(file, glob) for glob in test_paths):
        problems.append('not in test_paths')
    for row in rows:
        if row['verdict'] not in VERDICTS:
            problems.append(f"verdict {row['verdict']!r} is not one of {', '.join(VERDICTS)}")
        if row['verdict'] == 'delete' and not row['reason'].lower().startswith(DELETE_REASONS):
            problems.append(f"delete {row['test'] or '(no name)'}: the reason starts with neither "
                            f"'covered by <file:test>' nor 'none needed'")
    keep = {row['test'] for row in rows if row['verdict'] == 'keep'}
    for row in rows:
        if row['verdict'] == 'delete' and row['test'] in keep:
            problems.append(f"{row['test']}: keep and delete on the same test")
    if any(row['verdict'] in NEEDS_CASES for row in rows):
        if section is None:
            problems.append('fix, rewrite or add rows but no "### Cases:" section')
        else:
            if not section['exemplar']:
                problems.append('no Exemplar: line')
            if not section['cases']:
                problems.append('no case in the Cases section')
            seen = set()
            for case in section['cases']:
                name = case['id'] or '(no id)'
                if case['id'] in seen:
                    problems.append(f'case {name}: the id is used twice')
                seen.add(case['id'])
                for field in CASE_FIELDS:
                    if not case[field]:
                        problems.append(f'case {name}: {field} is empty')
                    elif '?' in case[field]:
                        problems.append(f'case {name}: {field} has ?')
    return problems


def pbi_numbers(base: Path) -> list[int]:
    return sorted(int(m.group(1)) for m in (PBI_FILE.match(p.name) for p in base.glob('PBI-*.md')) if m)


def planned_files(base: Path) -> dict[str, str]:
    """既にテスト PBI のある test file → その PBI。"""
    out = {}
    for path in base.glob('PBI-*.md'):
        if PBI_FILE.match(path.name):
            match = TEST_FILE_LINE.search(path.read_text(encoding='utf-8', errors='replace'))
            if match:
                out[match.group(1)] = path.stem
    return out


def build_targets(base: Path, skip: set[str]) -> list[str]:
    """テスト PBI でない PBI の Targets に書かれたパス (Do not touch に名指す)。"""
    found = []
    for path in sorted(base.glob('PBI-*.md')):
        if not PBI_FILE.match(path.name) or path.stem in skip:
            continue
        text = path.read_text(encoding='utf-8', errors='replace')
        heads = list(SECTION.finditer(text))
        for i, head in enumerate(heads):
            if head.group(1).strip().lower() == 'targets':
                end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
                for match in PATH_WORD.finditer(text[head.end():end]):
                    word = match.group(1) or match.group(2)
                    if word not in found:
                        found.append(word)
    return found


def case_table(section: dict) -> list[str]:
    lines = ['| ' + ' | '.join(CASE_FIELDS) + ' |', '|' + '---|' * len(CASE_FIELDS)]
    lines += ['| ' + ' | '.join(case[field] for field in CASE_FIELDS) + ' |' for case in section['cases']]
    return lines


def pbi_text(name: str, file: str, rows: list[dict], section: dict | None, settings: dict,
             others: list[str], checks: list[str] = ()) -> tuple[str, int]:
    """テスト PBI の Markdown と受け入れ条件の数。"""
    cases = section['cases'] if section else []
    deletes = [row for row in rows if row['verdict'] == 'delete']
    run = len(checks) + 1  # 整形と lint のあとの、テストを流すコマンドの番号
    criteria = [f"{case['id']} ({case['behaviour']}): verify {run} runs the test named {case['id']} and it passes."
                for case in cases]
    criteria += [f"Delete {row['file']}:{row['test']}: no test named {row['test']} is left in {file}, "
                 f'and verify {run} passes.' for row in deletes]
    if checks:
        criteria.append(f"Format and lint: verify {', '.join(str(i) for i in range(1, run))} exit 0.")
    verify = [f'{head} {file}' for head in checks] + [settings['test_command'].replace('{file}', file)]
    if isinstance(settings.get('test_typecheck'), str) and settings['test_typecheck'].strip():
        verify.append(settings['test_typecheck'])
    notes = [f"Exemplar: {section['exemplar']}" if section and section['exemplar'] else 'Exemplar: none']
    if cases:
        notes += ['', 'Cases:', '', *case_table(section)]
    if deletes:
        notes += [''] + [f"Delete: {row['file']}:{row['test']} ({row['reason']})" for row in deletes]
    actions = [row for row in rows if row['verdict'] in ('fix', 'rewrite') and row['action']]
    if actions:
        notes += ['', 'Survey actions:'] + [f"- {row['verdict']} {row['test']}: {row['action']}" for row in actions]
    dont = ['- Every file outside the Targets: the source code, the other test files and the shared test setup.']
    if others:
        dont.append('- In particular the files of the build PBIs: ' + ', '.join(f'`{path}`' for path in others))
    text = '\n'.join([
        f'# {name}: Tests for {file}', '',
        '- Model: <set by prepare.py>', '- Depends on: none', '- Related specs: none', f'- Test file: `{file}`', '',
        '## Goal',
        f'Write the test cases of the test stage for `{file}`, as the survey (docs/pbi/notes/test-survey.md) '
        'decided, so the behaviour of this run stays pinned by tests.', '',
        '## Targets', f'- `{file}`', '',
        '## Do not touch', *dont, '',
        '## Acceptance criteria', *(f'{i}. {line}' for i, line in enumerate(criteria, start=1)), '',
        '## Verify commands', *(f'{i}. `{cmd}`' for i, cmd in enumerate(verify, start=1)), '',
        '## Refs', 'none', '',
        '## Notes', *notes, ''])
    return text, len(criteria)


def brief_text(file: str, cases: int, deletes: int) -> str:
    return '\n'.join([f'Goal: write {cases} test cases and remove {deletes} tests in {file}',
                      'Kind of change: add to an existing pattern', 'New abstraction: none',
                      'External interaction: none', 'Invariants to keep: the source code is not changed',
                      'Judgment needed: none (expected values come from the cases)', ''])


def main(argv=None) -> int:
    common.utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('dir', nargs='?', default='docs/pbi')
    ap.add_argument('--survey', help='survey のパス (既定 <dir>/notes/test-survey.md)')
    ap.add_argument('--dry-run', action='store_true', help='例外と書く予定の PBI を出すだけで、何も書かない')
    args = ap.parse_args(argv)
    base = Path(args.dir)
    survey = Path(args.survey) if args.survey else base / 'notes' / 'test-survey.md'
    try:
        text = survey.read_text(encoding='utf-8')
    except OSError as error:
        print(f'tests_plan: cannot read {survey.as_posix()}: {error}', file=sys.stderr)
        return EXIT_USAGE
    settings = common.load_json(base / 'tests-at-end.json', {})
    settings = settings if isinstance(settings, dict) else {}
    if not (isinstance(settings.get('test_command'), str) and '{file}' in settings['test_command']):
        print('exception: tests-at-end.json: no test_command with {file}; write it, then run tests_plan.py again')
        return EXIT_EXCEPTIONS
    rows, cases, errors = parse_survey(text)
    for error in errors:
        print(f'exception: survey: {error}')
    by_file: dict[str, list[dict]] = {}
    for row in rows:
        by_file.setdefault(row['file'], []).append(row)
    checks = list(verify.required_checks(base).values())
    planned = planned_files(base)
    waves_data = common.load_json(base / 'waves.json', {})
    waved = {pbi for wave in (waves_data.get('waves') or [] if isinstance(waves_data, dict) else [])
             if isinstance(wave, dict) for pbi in wave.get('pbis') or []}
    test_paths = [glob for glob in settings.get('test_paths') or [] if isinstance(glob, str)]
    others = build_targets(base, set(planned.values()))
    number = (pbi_numbers(base) or [0])[-1]
    written, exceptions = [], bool(errors)
    for file, file_rows in by_file.items():
        if all(row['verdict'] == 'keep' for row in file_rows):
            continue
        if file in planned and planned[file] in waved:
            print(f'kept {planned[file]} {file} (already planned)')
            continue
        problems = file_exceptions(file, file_rows, cases.get(file), test_paths)
        if problems:
            exceptions = True
            for problem in dict.fromkeys(problems):
                print(f'exception: {file}: {problem}')
            continue
        if file in planned:
            # 前の実行が PBI を書いたあと波に入れる前に止まった: 同じ番号で書き直し、波に入れる
            name = planned[file]
        else:
            number += 1
            name = f'PBI-{number:03d}'
        section = cases.get(file)
        body, criteria = pbi_text(name, file, file_rows, section, settings, others, checks)
        case_count = len(section['cases']) if section else 0
        delete_count = sum(1 for row in file_rows if row['verdict'] == 'delete')
        if not args.dry_run:
            (base / f'{name}.md').write_text(body, encoding='utf-8', newline='\n')
            common.write_json(base / f'{name}.facts.json', {
                'files_changed': 1, 'files_new': 0 if Path(file).exists() else 1, 'files_deleted': 0,
                'modules': 1, 'criteria': criteria, 'sensitive': False})
            (base / f'{name}.brief.md').write_text(brief_text(file, case_count, delete_count), encoding='utf-8',
                                                   newline='\n')
        written.append(name)
        print(f'wrote {name} {file} ({case_count} cases, {delete_count} deletes)')
    if written:
        waves = common.load_json(base / 'waves.json', {})
        waves = waves if isinstance(waves, dict) and isinstance(waves.get('waves'), list) else {'waves': []}
        last = max((wave.get('n', 0) for wave in waves['waves'] if isinstance(wave, dict)), default=0)
        for i in range(0, len(written), MAX_PER_WAVE):
            last += 1
            chunk = written[i:i + MAX_PER_WAVE]
            waves['waves'].append({'n': last, 'pbis': chunk, 'kind': 'test'})
            print(f"wave {last}: {','.join(chunk)}")
        if not args.dry_run:
            common.write_json(base / 'waves.json', waves)
        tier = settings.get('implement_tier')
        if tier:
            print(f"then: python {(SKILL / 'prepare.py').as_posix()} {base.as_posix()} --only {','.join(written)} "
                  f'--tier {tier}')
    if not written and not exceptions:
        print('tests_plan: nothing to plan (every row is keep, or already planned)')
    return EXIT_EXCEPTIONS if exceptions else EXIT_OK


if __name__ == '__main__':
    sys.exit(main())
