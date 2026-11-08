"""jevlint の audit --json を 2 つ (前・後) 比べ、変更で悪くなった所を表に書く (appendix/lint.md)。

  python lint_compare.py <before.json> <after.json> [--config docs/pbi/lint.json] [--out FILE]

悪くなったとする線 (どれか 1 つで 1 行):
  score      ファイルの点が score_drop (既定 5) 以上下がった。新しいファイルは比べない
  ast        AST の指摘が新しく出た (線を超えた)、または前からある指摘が ast_growth (既定 0.1 = 10%) より大きくなった。
             function-lines は function_lines (既定 100) 以上だけを見る。規則ごとに値を大きい順に並べ、同じ順位の
             前の値と比べる (前に無ければ新しい)。行番号は見ない
  question   問いの合致が compliance (既定 0.7) 以上から未満に落ちた。新しいファイル・新しい問いは未満なら数える
  bug-hunt   question と同じ線で、問いの id が bug-hunt. で始まるもの
exclude_questions (既定 ["documentation.contracts-stated"]) の問いは見ない。モデルの判定が無い (--ast-only の) JSON では
question と bug-hunt を見ない。

action の列: bug-hunt と、新しく function_lines 以上になった function-lines は rework-candidate (設計者が確かめたら
差し戻し)。前から長い関数が伸びたもの、ほかの AST の指摘、score、question は report (review の担当への申し送りと
完了報告に載せる)。report_only_paths (glob の一覧、既定は空。テストのファイルを入れる) に合うファイルの行は、
いつも report。

標準出力は 1 行: `worse <件数> (bug-hunt <n>, ast <n>, question <n>, score <n>) -> <out>`。--out を省くと表を標準出力に出す。
終了コード: 0 = 比べた (悪化の有無によらない)、2 = 読めない JSON・引数の誤り。
"""
import argparse
import fnmatch
import json
import sys
from pathlib import Path

import common

DEFAULTS = {
    'score_drop': 5,
    'function_lines': 100,
    'compliance': 0.7,
    'exclude_questions': ['documentation.contracts-stated'],
    'ast_growth': 0.1,
    'report_only_paths': [],
}
FUNCTION_LINES = 'function-lines'
BUG_HUNT = 'bug-hunt.'
KINDS = ('bug-hunt', 'ast', 'question', 'score')


def load_config(path: str | None) -> dict:
    """lint.json の比較の線。無い・読めない項目は既定。"""
    config = dict(DEFAULTS)
    if path and Path(path).is_file():
        try:
            data = json.loads(Path(path).read_text(encoding='utf-8'))
        except ValueError:
            data = {}
        if isinstance(data, dict):
            config.update({k: v for k, v in data.items() if k in DEFAULTS})
    return config


def load_audit(path: str) -> dict:
    """audit --json を {path: ファイルの報告} にする。形が違えば ValueError。"""
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(data, dict) or not isinstance(data.get('files'), list):
        raise ValueError(f'{path}: not a jevlint audit report (files is missing)')
    return {f['path'].replace('\\', '/'): f for f in data['files'] if isinstance(f, dict) and 'path' in f}


def ast_values(report: dict | None, config: dict) -> dict:
    """規則ごとの指摘の値 (大きい順)。function-lines は線以上だけ。"""
    values = {}
    if not report:
        return values
    for finding in (report.get('ast') or {}).get('findings') or []:
        rule, value = finding.get('rule'), finding.get('value')
        if not isinstance(rule, str) or not isinstance(value, (int, float)):
            continue
        if rule == FUNCTION_LINES and value < config['function_lines']:
            continue
        values.setdefault(rule, []).append(value)
    return {rule: sorted(found, reverse=True) for rule, found in values.items()}


def answers(report: dict | None) -> dict | None:
    """{問いの id: (合致, 直し方)}。モデルの判定が無ければ None。"""
    model = (report or {}).get('model')
    if not isinstance(model, dict) or not isinstance(model.get('answers'), list):
        return None
    return {a['id']: (a.get('compliance'), a.get('fix', '')) for a in model['answers']
            if isinstance(a, dict) and isinstance(a.get('id'), str) and isinstance(a.get('compliance'), (int, float))}


def compare_file(path: str, before: dict | None, after: dict, config: dict) -> list[dict]:
    rows = []
    if before is not None and isinstance(before.get('score'), (int, float)) and isinstance(after.get('score'), (int, float)):
        drop = before['score'] - after['score']
        if drop >= config['score_drop']:
            rows.append(row(path, 'score', f'score {before["score"]} -> {after["score"]}', 'report'))
    report_only = any(fnmatch.fnmatch(path, glob) for glob in config['report_only_paths'])
    old, new = ast_values(before, config), ast_values(after, config)
    for rule, values in new.items():
        previous = old.get(rule, [])
        for rank, value in enumerate(values):
            if rank < len(previous):
                if value <= previous[rank] * (1 + config['ast_growth']):
                    continue
                rows.append(row(path, 'ast', f'{rule} {value} (was {previous[rank]})', 'report'))
                continue
            new_long = rule == FUNCTION_LINES and not report_only
            rows.append(row(path, 'ast', f'{rule} {value} (new)', 'rework-candidate' if new_long else 'report'))
    old_answers, new_answers = answers(before), answers(after)
    if new_answers is not None:
        line = config['compliance']
        for qid, (value, fix) in sorted(new_answers.items()):
            if qid in config['exclude_questions'] or value >= line:
                continue
            was = (old_answers or {}).get(qid, (None, ''))[0]
            if was is not None and was < line:
                continue
            bug = qid.startswith(BUG_HUNT)
            detail = f'{qid} {was if was is None else round(was, 2)} -> {round(value, 2)}'
            if fix:
                detail += f'; fix: {fix}'
            rows.append(row(path, 'bug-hunt' if bug else 'question', detail,
                            'rework-candidate' if bug and not report_only else 'report'))
    return rows


def row(path: str, kind: str, detail: str, action: str) -> dict:
    return {'file': path, 'kind': kind, 'detail': detail, 'action': action}


def compare(before: dict, after: dict, config: dict) -> list[dict]:
    rows = []
    for path in sorted(after):
        rows += compare_file(path, before.get(path), after[path], config)
    order = {kind: i for i, kind in enumerate(KINDS)}
    return sorted(rows, key=lambda r: (order[r['kind']], r['file']))


def table(rows: list[dict], before_path: str, after_path: str) -> str:
    lines = ['# jevlint の見直し', '', f'- 前: `{before_path}`', f'- 後: `{after_path}`', '']
    if not rows:
        return '\n'.join(lines + ['悪くなった所は無い。', ''])
    lines += ['| file | kind | detail | action |', '|---|---|---|---|']
    for r in rows:
        detail = r['detail'].replace('|', '\\|').replace('\n', ' ')
        lines.append(f'| {r["file"]} | {r["kind"]} | {detail} | {r["action"]} |')
    return '\n'.join(lines + [''])


def summary(rows: list[dict], out: str) -> str:
    counts = {kind: sum(1 for r in rows if r['kind'] == kind) for kind in KINDS}
    parts = ', '.join(f'{kind} {counts[kind]}' for kind in KINDS)
    return f'worse {len(rows)} ({parts}) -> {out}'


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('before')
    ap.add_argument('after')
    ap.add_argument('--config', help='docs/pbi/lint.json (無ければ既定の線)')
    ap.add_argument('--out', help='表を書くファイル')
    args = ap.parse_args(argv)
    common.utf8_stdout()
    try:
        before, after = load_audit(args.before), load_audit(args.after)
    except (OSError, ValueError) as error:
        print(f'error {error}', file=sys.stderr)
        return 2
    rows = compare(before, after, load_config(args.config))
    text = table(rows, args.before, args.after)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding='utf-8')
        print(summary(rows, args.out))
    else:
        print(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
