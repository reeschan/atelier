"""最終レビューとセキュリティレビューを使うかを、毎回同じ基準で決めて表で返す (appendix/review.md)。

  python adapter_gate.py <pbi-dir> [--numstat FILE] [--home DIR]

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
設定が off のフェーズは判定にかかわらず off で、理由は turned off in agents.json。
設定がスキル名のときは判定はそのまま (run / skip) で、setting の列に名前を出す。

標準出力はタブ区切りの表 (phase / setting / decision / reason。1 行目は見出し)。
agents.load_adapters の警告は表のあとに `warn <警告>` の形で 1 行ずつ出す。
終了コード: 0 = 正常 (PBI 0 件も正常)。2 = <pbi-dir> が無い・--numstat が読めない・引数の誤り。
"""
import argparse
import re
import sys
from pathlib import Path

import agents as agents_module
import common

# PBI として数えるファイルの形 (.rework-1・.brief などの付いたものは数えない)
PBI_NAME = re.compile(r'^PBI-(\d+)\.md$')

# 最終レビューを飛ばす上限 (diff の行数と PBI の数)
MAX_DIFF = 300
MAX_PBIS = 2

DESIGN_REASON = 'decided by the orchestrator (appendix/design.md)'
OFF_REASON = 'turned off in agents.json'
SECURITY_SKIP_REASON = 'no sensitive or deep PBI'


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

    settings, warnings = agents_module.load_adapters(common.resolve_home(args.home))
    records = pbi_records(base)
    rows = [('design', design_row(settings['design'])),
            ('review', review_row(settings['review'], diff, records, unknown_reason)),
            ('security', security_row(settings['security'], records))]

    print('phase\tsetting\tdecision\treason')
    for phase, (setting, decision, reason) in rows:
        print(f'{phase}\t{setting}\t{decision}\t{reason}')
    for warning in warnings:
        print(f'warn {warning}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
