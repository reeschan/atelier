"""runs.jsonl (record_run.py が書く) をモデル別に集計する。読むだけで何も書き換えない。

使い方:
  python <このディレクトリ>/model_report.py [docs/pbi/runs.jsonl ...]     # 省略時は docs/pbi/runs.jsonl
  python <このディレクトリ>/model_report.py --archive [DIR]               # 保全先の全 runs.jsonl (リポジトリをまたぐ)
  python <このディレクトリ>/model_report.py ... --json                    # 表の代わりに JSON

モデルは「runner model effort」で分ける。列は、実行の件数・結果の内訳・使えず率・トークン・コスト・所要時間と、
仮の評価。数値は記録した事実をそのまま足したもので、null (取れなかった値) は足さずに件数を別に出す。

仮の評価は、後から人が見て調整するための目安で、振り分けの判断には使わない。規則はこのファイルの grade() だけにあり、
runs.jsonl には書かない (規則を変えても記録はやり直さずに済む):
  PBI ごとに、最初に「使えず」以外の結果を出した実行のモデルに付ける。
  その PBI に昇格か保留があれば D、なければ合格した実行の差し戻しの回数で A (0 回) / B (1 回) / C (2 回)。
  まだ合格していない PBI には付けない。
"""
import argparse
import json
import os
import sys
from pathlib import Path

from record_run import archive_root

GRADES = ('A', 'B', 'C', 'D')
UNAVAILABLE = '使えず'


def label(row: dict) -> str:
    effort = row.get('effort')
    return f"{row.get('runner')} {row.get('model')}" + (f' {effort}' if effort else '')


def load(paths: list[Path]) -> list[dict]:
    rows = []
    for p in paths:
        for line in p.read_text(encoding='utf-8').splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def grade(pbi_rows: list[dict]) -> tuple[str | None, str | None, int | None]:
    """(評価を受けるモデルのラベル, 評価, 合格までの差し戻しの回数)。"""
    ordered = sorted(pbi_rows, key=lambda r: (r.get('rework', 0), r.get('attempt', 0)))
    delivered = [r for r in ordered if r['judged']['outcome'] != UNAVAILABLE]
    if not delivered:
        return None, None, None
    owner = label(delivered[0])
    if any(r['judged']['outcome'] in ('昇格', '保留') for r in ordered):
        return owner, 'D', None
    passed = [r for r in ordered if r['judged']['outcome'] == '合格']
    if not passed:
        return owner, None, None
    n = passed[0].get('rework', 0)
    return owner, {0: 'A', 1: 'B'}.get(n, 'C'), n


def known_sum(values):
    vals = [v for v in values if v is not None]
    return (sum(vals) if vals else None), len(vals)


def summarize(rows: list[dict]) -> dict:
    models = {}
    for r in rows:
        m = models.setdefault(label(r), {'label': label(r), 'runner': r.get('runner'), 'model': r.get('model'),
                                         'effort': r.get('effort'), 'rows': []})
        m['rows'].append(r)
    by_pbi = {}
    for r in rows:
        by_pbi.setdefault((r.get('repo'), r.get('work_key'), r.get('pbi')), []).append(r)
    graded = {}
    for pbi_rows in by_pbi.values():
        owner, g, n = grade(pbi_rows)
        if owner is not None and g is not None:
            graded.setdefault(owner, []).append((g, n))
    out = []
    for name, m in sorted(models.items()):
        rs = m.pop('rows')
        outcomes = {}
        for r in rs:
            outcomes[r['judged']['outcome']] = outcomes.get(r['judged']['outcome'], 0) + 1
        tokens, tokens_known = known_sum((r.get('tokens') or {}).get('total') for r in rs)
        reported, _ = known_sum(r.get('cost_reported') for r in rs)
        api, _ = known_sum(r.get('cost_api_equiv') for r in rs)
        secs, secs_known = known_sum((r.get('time') or {}).get('seconds') for r in rs)
        mine = graded.get(name, [])
        reworks = [n for _, n in mine if n is not None]
        out.append({**m, 'attempts': len(rs), 'outcomes': outcomes,
                    'unavailable_rate': outcomes.get(UNAVAILABLE, 0) / len(rs),
                    'tokens_total': tokens, 'tokens_known': tokens_known,
                    'tokens_avg': (tokens / tokens_known) if tokens_known else None,
                    'cost_reported': reported, 'cost_api_equiv': api,
                    'seconds_avg': (secs / secs_known) if secs_known else None,
                    'pbis_graded': len(mine),
                    'grades': {g: sum(1 for x, _ in mine if x == g) for g in GRADES},
                    'rework_avg': (sum(reworks) / len(reworks)) if reworks else None})
    return {'rows': len(rows), 'pbis': len(by_pbi), 'models': out}


def money(v):
    return '—' if v is None else f'{v:.4f}'


def num(v):
    return '—' if v is None else f'{v:,.0f}'


def table(report: dict) -> str:
    lines = [f"実行 {report['rows']} 件 / PBI {report['pbis']} 件",
             '',
             '| モデル | 実行 | 結果の内訳 | 使えず率 | トークン合計 (取得数) | 平均 | 実費 USD | API 換算 USD | 平均秒 |',
             '|---|---|---|---|---|---|---|---|---|']
    for m in report['models']:
        outcomes = ' '.join(f'{k}{v}' for k, v in m['outcomes'].items())
        lines.append(f"| {m['label']} | {m['attempts']} | {outcomes} | {m['unavailable_rate']:.0%} | "
                     f"{num(m['tokens_total'])} ({m['tokens_known']}) | {num(m['tokens_avg'])} | "
                     f"{money(m['cost_reported'])} | {money(m['cost_api_equiv'])} | {num(m['seconds_avg'])} |")
    lines += ['',
              '仮の評価 (最初に成果物を出したモデルに付ける。A 一発合格 / B 差し戻し 1 回 / C 2 回 / D 昇格・保留。'
              '後から人が見て調整する目安で、振り分けには使わない)',
              '',
              '| モデル | 評価した PBI | A | B | C | D | 合格までの差し戻しの平均 |',
              '|---|---|---|---|---|---|---|']
    for m in report['models']:
        if not m['pbis_graded']:
            continue
        g = m['grades']
        avg = '—' if m['rework_avg'] is None else f"{m['rework_avg']:.2f}"
        lines.append(f"| {m['label']} | {m['pbis_graded']} | {g['A']} | {g['B']} | {g['C']} | {g['D']} | {avg} |")
    return '\n'.join(lines)


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('paths', nargs='*')
    ap.add_argument('--archive', nargs='?', const='', default=None, metavar='DIR')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()
    if args.archive is not None:
        root = Path(args.archive) if args.archive else archive_root()
        paths = sorted(root.glob('*/*/runs.jsonl'))
    else:
        paths = [Path(p) for p in args.paths] or [Path('docs/pbi/runs.jsonl')]
    missing = [p for p in paths if not p.is_file()]
    if missing or not paths:
        print(f"runs.jsonl が無い: {', '.join(map(str, missing)) or '(保全先に 0 件)'}", file=sys.stderr)
        sys.exit(2)
    report = summarize(load(paths))
    print(json.dumps(report, ensure_ascii=False) if args.json else table(report))


if __name__ == '__main__':
    main()
