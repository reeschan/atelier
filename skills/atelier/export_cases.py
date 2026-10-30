"""判定を終えた PBI ごとに、実装担当に渡したもの・課題・結果を正規化した 1 行 (case) にして docs/pbi/cases.jsonl に書く
(appendix/finish.md、終わりに 1 回)。runs.jsonl の保全先にも同じものを写す。読むのは docs/pbi/ だけで、作業は書き換えない。

使い方 (リポジトリのルートで):
  python <このディレクトリ>/export_cases.py [--dir docs/pbi] [--repo NAME]

ここではプロンプトを組み立てない。外のハーネス (harness2 など) が case を読み、自分の構成要素に当てはめる。
case の形は CASE_SCHEMA の版と appendix/cases.md にある。要素は種類ごとに分ける:
  harness: 実装担当に注入したもの。system_prompt (docs/pbi/worker.md)、guardrails (効いていた CLAUDE.md。
           ルートと Targets のパスの親ディレクトリにあるもの)、memory (Refs の id と要旨と、get したか)。
           本文は入れず、パスと sha256 の先頭 12 文字だけを入れる (worker.md は保全先にも写るので本文を入れる)
  task:    PBI の節 (Goal・Targets・Do not touch・Acceptance criteria・Verify commands・Notes・Depends on)
  result:  runs.jsonl の行を実行ごとに縮めたもの (結果・原因・指摘・検証の終了コード・メモリと範囲の違反)
  private: 差し戻しの note の Expected と Repro。採点にだけ使い、実装担当に見せない
PBI の行が runs.jsonl に無いもの、「使えず」だけのものは書かない。
"""
import argparse
import json
import re
import sys
from pathlib import Path

import record_run
import verify

CASE_SCHEMA = 'atelier.case/1'
SECTIONS = {'goal': ('Goal',), 'targets': ('Targets',), 'do_not_touch': ('Do not touch',),
            'acceptance': ('Acceptance criteria',), 'notes': ('Notes',)}
NUMBERED = re.compile(r'^\s*(\d+)[.:)]\s+(.*\S)\s*$')
PATHLIKE = re.compile(r'`?([\w./-]+/[\w.-]+|[\w-]+\.[A-Za-z0-9]+)`?')
DEPENDS = re.compile(r'^\s*-\s*Depends on:\s*(.*)$', re.M)
PBI_ID = re.compile(r'PBI-\d+')
EXPECTED = re.compile(r'^\s*-\s*(Expected|Repro):\s*(.*\S)\s*$', re.M)


def section(text: str, names: tuple[str, ...]) -> str:
    alt = '|'.join(re.escape(n) for n in names)
    m = re.search(rf'^## (?:{alt})\s*\n(.*?)(?=^## |\Z)', text, re.S | re.M)
    return m.group(1).strip() if m else ''


def numbered(text: str) -> list[dict]:
    return [{'n': int(m.group(1)), 'text': m.group(2)} for line in text.splitlines() if (m := NUMBERED.match(line))]


def task_of(pbi_text: str) -> dict:
    out = {k: section(pbi_text, v) for k, v in SECTIONS.items()}
    out['acceptance'] = numbered(out['acceptance'])
    out['verify'] = [{'n': n, 'command': c} for n, c in verify.commands(pbi_text)]
    m = DEPENDS.search(pbi_text)
    out['depends_on'] = PBI_ID.findall(m.group(1)) if m else []
    m = re.search(r'^# (.*\S)', pbi_text, re.M)
    out['title'] = m.group(1) if m else ''
    return out


def target_paths(targets: str) -> list[str]:
    return sorted({m.group(1) for m in PATHLIKE.finditer(targets)})


def guardrails(root: Path, targets: list[str]) -> list[dict]:
    """ルートと、Targets の各パスの親ディレクトリにある CLAUDE.md (実装担当がファイルを読むと読み込まれるもの)。"""
    dirs = {Path('.')}
    for t in targets:
        for parent in Path(t).parents:
            dirs.add(parent)
    out = []
    for d in sorted(dirs, key=lambda p: (len(p.parts), p.as_posix())):
        path = root / d / 'CLAUDE.md'
        if path.is_file():
            out.append({'kind': 'convention', 'path': (d / 'CLAUDE.md').as_posix(),
                        'version': record_run.file_version(path)})
    return out


def memory_of(pbi_text: str, rows: list[dict]) -> list[dict]:
    missed = set()
    known = False
    for r in rows:
        m = (r.get('memory') or {}).get('missed_refs')
        if m is not None:
            known = True
            missed |= set(m)
    out = []
    for line in record_run.refs_section(pbi_text).splitlines():
        m = record_run.LEADING_REF_ID.match(line)
        if not m or m.group(1).lower() in ('none', 'なし'):
            continue
        gist = line[m.end():].lstrip(' :：-').strip()
        out.append({'id': m.group(1), 'gist': gist, 'fetched': (m.group(1) not in missed) if known else None})
    return out


def attempt_of(r: dict) -> dict:
    judged = r.get('judged') or {}
    return {'report': r.get('report'), 'attempt': r.get('attempt'), 'rework': r.get('rework'),
            'runner': r.get('runner'), 'model': r.get('model'), 'effort': r.get('effort'), 'tier': r.get('tier'),
            'outcome': judged.get('outcome'), 'cause': judged.get('cause'), 'findings': judged.get('findings'),
            'verify_codes': (r.get('verify') or {}).get('codes'),
            'memory_violation': (r.get('memory') or {}).get('violation'),
            'do_not_touch_violation': (r.get('unchanged') or {}).get('violation'),
            'worker': (r.get('prompt') or {}).get('worker'), 'tokens': (r.get('tokens') or {}).get('total'),
            'archive': (r.get('archive') or {}).get('dir')}


def rework_notes(base: Path, pbi: str) -> list[dict]:
    out = []
    for path in sorted(base.glob(f'{pbi}.rework-*.md'), key=lambda p: int(p.stem.rsplit('-', 1)[1])
                       if p.stem.rsplit('-', 1)[1].isdigit() else 0):
        if not path.stem.rsplit('-', 1)[1].isdigit():
            continue
        text = path.read_text(encoding='utf-8', errors='replace')
        out.append({'note': path.name, 'items': [{'kind': m.group(1).lower(), 'text': m.group(2)}
                                                 for m in EXPECTED.finditer(text)]})
    return out


def build_case(base: Path, root: Path, repo: str, pbi: str, rows: list[dict]) -> dict | None:
    rows = sorted(rows, key=lambda r: (r.get('rework', 0), r.get('attempt', 0)))
    delivered = [r for r in rows if (r.get('judged') or {}).get('outcome') != '使えず']
    if not delivered:
        return None
    try:
        pbi_text = (base / f'{pbi}.md').read_text(encoding='utf-8')
    except OSError:
        pbi_text = ''
    task = task_of(pbi_text)
    worker = base / 'worker.md'
    try:
        worker_text = worker.read_text(encoding='utf-8')
    except OSError:
        worker_text = None
    outcomes = [(r.get('judged') or {}).get('outcome') for r in delivered]
    first = delivered[0]
    return {
        'schema': CASE_SCHEMA,
        'id': f"{repo}:{first.get('work_key') or ''}:{pbi}",
        'repo': repo, 'work_key': first.get('work_key'), 'pbi': pbi,
        'base_commit': first.get('base_commit'),
        'harness': {
            'system_prompt': {'path': 'docs/pbi/worker.md', 'version': record_run.file_version(worker),
                              'text': worker_text},
            'guardrails': guardrails(root, target_paths(task['targets'])),
            'memory': memory_of(pbi_text, delivered),
        },
        'task': task,
        'result': {'first_pass': outcomes[0] == '合格', 'passed': '合格' in outcomes,
                   'sent_back': any(o in ('差し戻し', '昇格', '保留') for o in outcomes),
                   'attempts': [attempt_of(r) for r in rows]},
        'private': {'rework': rework_notes(base, pbi)},
    }


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dir', default='docs/pbi')
    ap.add_argument('--repo')
    args = ap.parse_args()
    base = Path(args.dir)
    rows = record_run.read_rows(base / 'runs.jsonl')
    if not rows:
        print(f'{base / "runs.jsonl"} に行が無い', file=sys.stderr)
        sys.exit(2)
    root = Path.cwd()
    repo = args.repo or rows[0].get('repo') or root.resolve().name
    by_pbi = {}
    for r in rows:
        by_pbi.setdefault(r.get('pbi'), []).append(r)
    cases = [c for pbi, rs in sorted(by_pbi.items(), key=lambda kv: int(kv[0].split('-')[1]))
             if (c := build_case(base, root, repo, pbi, rs)) is not None]
    text = ''.join(json.dumps(c, ensure_ascii=False) + '\n' for c in cases)
    (base / 'cases.jsonl').write_text(text, encoding='utf-8')
    key = rows[-1].get('work_key')
    copied = 'ok'
    try:
        group = record_run.archive_root() / repo / record_run.key_dir(key)
        group.mkdir(parents=True, exist_ok=True)
        (group / 'cases.jsonl').write_text(text, encoding='utf-8')
    except OSError as error:
        copied = f'失敗 ({error})。作業は続けてよい'
    sent = sum(1 for c in cases if c['result']['sent_back'])
    print(f'cases: {len(cases)} 件 (差し戻しあり {sent} 件) を {base / "cases.jsonl"} に書いた。保全先 {copied}')


if __name__ == '__main__':
    main()
