"""1 回の実行 (PBI × 初回 / 差し戻し n 回目 × route の何番目) を docs/pbi/runs.jsonl に 1 行で記録し、
その実行の生ログをリポジトリの外の保全先にコピーする (SKILL.md 8 章、合否を決めた直後に 1 回)。

使い方 (リポジトリのルートで):
  python <このディレクトリ>/record_run.py <報告名> --attempt N --outcome 合格|差し戻し|昇格|使えず|保留
      [--findings 重大,中,軽微] [--runner R --model M --effort E --tier T] [--work-key KEY] [--repo NAME]
      [--dir docs/pbi] [--projects DIR] [--prices FILE] [--home DIR]

報告名は PBI-007 か PBI-007.rework-1。--attempt は route の何番目か (1 始まり。使えずで次へ回したら増やす)。
実行先・モデル・effort・段は docs/pbi/<PBI>.route.json の route[attempt-1] から取る。昇格などで route.json と
違う実行先で動かしたときは --runner / --model / --effort / --tier で渡す。

判断による値は --outcome と --findings の 2 つだけで、行の judged に分けて入れる。それ以外はファイルから読む:
  opencode: docs/pbi/opencode/<報告名>.jsonl の step_finish (tokens・cost の合計、数をステップ数、時刻の幅)
  agent:    Claude Code の会話記録のうち、description が報告名で始まるサブエージェント (agent_stats.load_requests)
  codex:    docs/pbi/codex/<報告名>.log の「tokens used」の合計だけ (内訳は null)
  agy:      docs/pbi/agy/<報告名>.json に usage があれば読む。無ければ null
  dsh:      docs/pbi/dsh/<報告名>.jsonl の status イベントの usage (トークンの合計、数をステップ数) と
            <報告名>.run.json の開始・終了時刻 (dsh_run.py が書く)
  検証:     docs/pbi/verify/<報告名>.json (verify.py が書く)。unchanged.py check の行の終了コードを触らない範囲とする
取れない値は 0 ではなく null にする。cost_reported は実行先が出した USD (opencode の cost)、cost_api_equiv は
トークン × 単価表の API 換算 (参考値で請求額ではない)。単価表は同梱の --prices に home の --home/prices.json を
重ねたもの。トークン数と単価が分かる実行先ならどれでも出す。prices_checked_at は cost_api_equiv を出したときに入れる。

保全先は環境変数 PBI_RUNS_ARCHIVE、無ければ ~/.claude/pbi-runs にする (クラウドで同期するフォルダなどに残したいときは
PBI_RUNS_ARCHIVE にそのフォルダを入れる)。
<保全先>/<リポジトリ名>/<ブランチ>__<W-番号>/<報告名>/attempt-<N>/ にコピーし、runs.jsonl も
<保全先>/<リポジトリ名>/<ブランチ>__<W-番号>/runs.jsonl に写す。コピーに失敗しても終了コードは 0 で、
行の archive.ok を false にして標準出力に警告を出す。同じ (報告名, attempt) が既にあれば記録せず終了コード 2。
"""
import argparse
import datetime
import json
import os
import re
import shutil
import sys
from pathlib import Path

import agent_stats
import common

HERE = Path(__file__).resolve().parent
SCHEMA = 1
OUTCOMES = ('合格', '差し戻し', '昇格', '使えず', '保留')
SEVERITIES = ('重大', '中', '軽微')
REPORT_RE = re.compile(r'^(PBI-\d+)(?:\.rework-(\d+))?$')
RUNNER_DIRS = ('opencode', 'codex', 'agy', 'dsh')
RUNNER_SUFFIXES = ('.prompt.md', '.jsonl', '.err', '.log', '.last.md', '.json', '.patch.yml', '.run.json', '.final.md')
TOKEN_FIELDS = ('input', 'output', 'reasoning', 'cache_read', 'cache_write', 'cache_write_5m', 'cache_write_1h')
PRICE_KEYS = ('input', 'output', 'cache_read', 'cache_write_5m', 'cache_write_1h')


def fail(msg: str) -> None:
    print(msg, file=sys.stderr)
    sys.exit(2)


def empty_tokens(source=None) -> dict:
    return {**{k: None for k in TOKEN_FIELDS}, 'total': None, 'source': source}


def with_total(tokens: dict) -> dict:
    parts = [tokens[k] for k in ('input', 'output', 'reasoning', 'cache_read', 'cache_write') if tokens[k] is not None]
    tokens['total'] = sum(parts) if parts else None
    return tokens


def span(first, last) -> dict:
    """時刻 (datetime) の幅。"""
    if first is None or last is None:
        return {'start': None, 'end': None, 'seconds': None}
    return {'start': first.isoformat(timespec='seconds'), 'end': last.isoformat(timespec='seconds'),
            'seconds': round((last - first).total_seconds())}


def tokens_cost(tokens: dict, price: dict | None) -> float | None:
    """トークン数 × 単価 (USD / 100 万トークン) の API 換算。単価が無い・トークンの内訳が無いときは None。

    tokens に無いキーと単価表に無いキーは 0 として計算する。
    """
    if price is None:
        return None
    counts = {k: tokens.get(k) for k in PRICE_KEYS}
    if all(v is None for v in counts.values()):
        return None
    return sum((counts[k] or 0) * (price.get(k) or 0) for k in PRICE_KEYS) / 1_000_000


def from_ms(ms) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc).astimezone()


def read_opencode(path: Path) -> dict:
    """step_finish を合計する。opencode の reasoning は output と別に数える (opencode の出力どおり)。"""
    if not path.is_file():
        return {'tokens': empty_tokens(), 'steps': None, 'time': span(None, None), 'cost': None}
    t = {**{k: 0 for k in ('input', 'output', 'reasoning', 'cache_read', 'cache_write')},
         'cache_write_5m': None, 'cache_write_1h': None, 'source': path.as_posix()}
    steps, cost, stamps = 0, 0.0, []
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev.get('timestamp'), (int, float)):
            stamps.append(ev['timestamp'])
        if ev.get('type') != 'step_finish':
            continue
        part = ev.get('part') or {}
        tok = part.get('tokens') or {}
        cache = tok.get('cache') or {}
        steps += 1
        t['input'] += tok.get('input') or 0
        t['output'] += tok.get('output') or 0
        t['reasoning'] += tok.get('reasoning') or 0
        t['cache_read'] += cache.get('read') or 0
        t['cache_write'] += cache.get('write') or 0
        cost += part.get('cost') or 0
    when = span(from_ms(min(stamps)), from_ms(max(stamps))) if stamps else span(None, None)
    return {'tokens': with_total(t), 'steps': steps, 'time': when, 'cost': cost}


def read_codex(path: Path) -> dict:
    tokens = empty_tokens(path.as_posix() if path.is_file() else None)
    if path.is_file():
        m = re.search(r'tokens used\s*\n\s*([\d,]+)', path.read_text(encoding='utf-8', errors='replace'))
        if m:
            tokens['total'] = int(m.group(1).replace(',', ''))
    return {'tokens': tokens, 'steps': None, 'time': span(None, None), 'cost': None}


def find_usage(obj):
    if isinstance(obj, dict):
        if isinstance(obj.get('usage'), dict):
            return obj['usage']
        for v in obj.values():
            u = find_usage(v)
            if u is not None:
                return u
    elif isinstance(obj, list):
        for v in obj:
            u = find_usage(v)
            if u is not None:
                return u
    return None


def read_agy(path: Path) -> dict:
    tokens = empty_tokens(path.as_posix() if path.is_file() else None)
    if path.is_file():
        try:
            usage = find_usage(json.loads(path.read_text(encoding='utf-8', errors='replace')))
        except ValueError:
            usage = None
        if usage:
            tokens['input'] = usage.get('input_tokens')
            tokens['output'] = usage.get('output_tokens')
            with_total(tokens)
    return {'tokens': tokens, 'steps': None, 'time': span(None, None), 'cost': None}


def read_dsh(path: Path) -> dict:
    """dsh の --json の status イベントにある usage を合計する。dsh は reasoning を output に含めて数える
    (reasoning は null)。時刻は dsh_run.py が書く <報告名>.run.json から読む。cost は出ないので null。"""
    run_json = path.with_name(path.name.removesuffix('.jsonl') + '.run.json')
    when = span(None, None)
    if run_json.is_file():
        try:
            run = json.loads(run_json.read_text(encoding='utf-8'))
            when = span(datetime.datetime.fromisoformat(run['start']), datetime.datetime.fromisoformat(run['end']))
        except (ValueError, KeyError, TypeError):
            pass
    if not path.is_file():
        return {'tokens': empty_tokens(), 'steps': None, 'time': when, 'cost': None}
    t = {**{k: 0 for k in ('input', 'output', 'cache_read', 'cache_write')},
         'reasoning': None, 'cache_write_5m': None, 'cache_write_1h': None, 'source': path.as_posix()}
    steps = 0
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        usage = ev.get('usage') if isinstance(ev, dict) and ev.get('type') == 'status' else None
        if not isinstance(usage, dict):
            continue
        steps += 1
        t['input'] += usage.get('inputTokens') or 0
        t['output'] += usage.get('outputTokens') or 0
        t['cache_read'] += usage.get('cacheReadTokens') or 0
        t['cache_write'] += usage.get('cacheWriteTokens') or 0
    return {'tokens': with_total(t), 'steps': steps, 'time': when, 'cost': None}


def matches(description: str, name: str) -> bool:
    """description が報告名で始まる実装担当か。設計者のレビュー・設計見直し (PBI-007.review / .design、
    以前の書き方の PBI-007 review / design) と、以前の書き方の差し戻し (PBI-007 rework-1) は、
    PBI-007 の実行ではないので外す。"""
    for sep in (' ', '　'):
        if description.startswith(name + sep):
            rest = description[len(name) + 1:].strip().split()
            word = rest[0].lower() if rest else ''
            return not (word in ('review', 'design') or word.startswith('rework'))
    return description == name


def norm_path(s: str) -> str:
    return s.removeprefix('\\\\?\\').replace('\\', '/').rstrip('/').lower()


def work_root(transcript: Path) -> str | None:
    """依頼文 (最初の user のメッセージ) の「作業ルート: <パス>」(SKILL.md 6.1)。無ければ None。"""
    try:
        with open(transcript, encoding='utf-8') as f:
            for _, line in zip(range(20), f):
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get('type') != 'user':
                    continue
                content = (row.get('message') or {}).get('content')
                if isinstance(content, list):
                    content = ' '.join(b.get('text', '') for b in content if isinstance(b, dict))
                m = re.search(r'作業ルート:\s*([^\s。]+)', content or '')
                return m.group(1) if m else None
    except OSError:
        return None
    return None


def find_agent_transcript(projects: Path, cwd: Path, name: str) -> Path | None:
    """description が報告名で始まるサブエージェントの会話記録 (最新のもの)。

    全プロジェクトから探し、依頼文の作業ルートが今のディレクトリと同じものを採る。フォルダを選ばずに始めた
    セッションから絶対パスで作業したとき、会話記録は作業リポジトリとは別のプロジェクトの下にあるため。
    作業ルートの無い会話記録は、今のディレクトリのプロジェクトにあるときだけ採る。
    """
    projects = common.long_path(projects)
    here = projects / common.project_dir_for(cwd).name
    target = norm_path(str(cwd))
    hits = []
    for meta in projects.glob('*/*/subagents/agent-*.meta.json'):
        try:
            desc = json.loads(meta.read_text(encoding='utf-8')).get('description', '')
        except (OSError, ValueError):
            continue
        if not matches(desc, name):
            continue
        jl = meta.with_name(meta.name.removesuffix('.meta.json') + '.jsonl')
        if not jl.is_file():
            continue
        root = work_root(jl)
        if (root is not None and norm_path(root) == target) or (root is None and meta.parents[2] == here):
            hits.append(jl)
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


def read_agent(transcript: Path | None, prices: dict) -> dict:
    if transcript is None:
        return {'tokens': empty_tokens(), 'steps': None, 'time': span(None, None), 'cost_api': None,
                'models_seen': [], 'transcript': None}
    reqs = agent_stats.load_requests(transcript)
    t = {'input': 0, 'output': 0, 'reasoning': None, 'cache_read': 0, 'cache_write_5m': 0, 'cache_write_1h': 0,
         'source': common.shown(transcript).replace('\\', '/')}
    cost, priced = 0.0, True
    for r in reqs:
        for k in ('input', 'output', 'cache_read', 'cache_write_5m', 'cache_write_1h'):
            t[k] += r[k]
        p = common.price_for(prices, r['model'])
        if p is None:
            priced = False
        else:
            cost += sum(r[k] * (p.get(k) or 0) for k in PRICE_KEYS)
    t['cache_write'] = t['cache_write_5m'] + t['cache_write_1h']
    first = datetime.datetime.fromtimestamp(reqs[0]['ts']).astimezone() if reqs else None
    last = datetime.datetime.fromtimestamp(reqs[-1]['ts']).astimezone() if reqs else None
    return {'tokens': with_total(t), 'steps': len(reqs), 'time': span(first, last),
            'cost_api': (cost / 1_000_000) if (priced and reqs) else None,
            'models_seen': sorted({r['model'] for r in reqs}), 'transcript': transcript}


def read_verify(path: Path) -> tuple[dict | None, dict]:
    if not path.is_file():
        return None, {'checked': False, 'violation': None}
    try:
        results = json.loads(path.read_text(encoding='utf-8')).get('results', [])
    except ValueError:
        return None, {'checked': False, 'violation': None}
    codes = [r.get('code') for r in results]
    verify = {'passed': sum(1 for c in codes if c == 0), 'total': len(codes), 'codes': codes}
    checks = [r.get('code') for r in results if 'unchanged.py check' in str(r.get('command', ''))]
    unchanged = {'checked': bool(checks), 'violation': (any(c != 0 for c in checks) if checks else None)}
    return verify, unchanged


def work_key(base: Path, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    status = base / 'STATUS.md'
    if status.is_file():
        m = re.search(r'作業キー:\s*(\S+)', status.read_text(encoding='utf-8', errors='replace'))
        if m:
            return m.group(1)
    return None


def key_dir(key: str | None) -> str:
    """作業キー <repo>:<branch>:<W-番号> から <ブランチ>__<W-番号> (ブランチの英数字以外は -)。"""
    if not key:
        return 'no-work-key'
    parts = key.split(':')
    branch, num = (parts[1], parts[2]) if len(parts) >= 3 else ('', parts[-1])
    safe = ''.join(c if c.isalnum() or c in '._-' else '-' for c in branch).strip('-')
    return f'{safe}__{num}' if safe else num


def archive_root() -> Path:
    env = os.environ.get('PBI_RUNS_ARCHIVE')
    if env:
        return Path(env)
    return Path.home() / '.claude' / 'pbi-runs'


def raw_files(base: Path, pbi: str, name: str, rework: int) -> list[Path]:
    out = []
    for d in RUNNER_DIRS:
        out += [base / d / f'{name}{s}' for s in RUNNER_SUFFIXES]
    out.append(base / 'verify' / f'{name}.json')
    out += [p for p in (base / 'verify').glob(f'{name}.*.log') if p.name[len(name) + 1:-4].isdigit()]
    out += [base / f'{pbi}{s}' for s in ('.md', '.facts.json', '.route.json', '.brief.md')]
    if rework:
        out += [base / f'{name}.md', base / f'{name}.brief.md']
    out += sorted((base / 'jev').glob(f'{pbi}.*'))
    out.append(base / 'reports' / f'{name}.md')
    return [p for p in out if p.is_file()]


def archive(base: Path, dest: Path, files: list[Path], transcript: Path | None) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for f in files:
        target = dest / f.relative_to(base)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
    if transcript is not None:
        (dest / 'claude').mkdir(exist_ok=True)
        shutil.copy2(transcript, dest / 'claude' / transcript.name)
        meta = transcript.with_name(transcript.stem + '.meta.json')
        if meta.is_file():
            shutil.copy2(meta, dest / 'claude' / meta.name)


def read_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding='utf-8').splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def parse_findings(s: str | None) -> dict | None:
    if s is None:
        return None
    parts = s.split(',')
    if len(parts) != 3 or not all(p.strip().isdigit() for p in parts):
        fail('--findings は 重大,中,軽微 の 3 つの整数 (例 0,1,2)')
    return dict(zip(SEVERITIES, (int(p) for p in parts)))


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('report')
    ap.add_argument('--attempt', type=int, required=True)
    ap.add_argument('--outcome', required=True)
    ap.add_argument('--findings')
    for opt in ('--runner', '--model', '--effort', '--tier', '--work-key', '--repo'):
        ap.add_argument(opt)
    ap.add_argument('--dir', default='docs/pbi')
    ap.add_argument('--projects', default=str(agent_stats.PROJECTS))
    ap.add_argument('--prices', default=str(HERE / 'prices.json'), help='同梱の単価表')
    ap.add_argument('--home', help='home のディレクトリ (既定は common.resolve_home)')
    args = ap.parse_args()

    m = REPORT_RE.match(args.report)
    if not m:
        fail('報告名は PBI-<番号> か PBI-<番号>.rework-<n>')
    if args.outcome not in OUTCOMES:
        fail(f'--outcome は {" / ".join(OUTCOMES)} のどれか')
    if args.attempt < 1:
        fail('--attempt は 1 以上')
    findings = parse_findings(args.findings)
    pbi, rework = m.group(1), int(m.group(2) or 0)
    cwd = Path.cwd().resolve()
    base = Path(args.dir)
    runs = base / 'runs.jsonl'
    if any(r.get('report') == args.report and r.get('attempt') == args.attempt for r in read_rows(runs)):
        fail(f'{args.report} の attempt {args.attempt} は記録済み (runs.jsonl)')

    route_doc = {}
    try:
        route_doc = json.loads((base / f'{pbi}.route.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        pass
    route = route_doc.get('route') or []
    step = route[args.attempt - 1] if args.attempt <= len(route) else {}
    runner = args.runner or step.get('runner')
    model = args.model or step.get('model')
    effort = args.effort if args.effort is not None else step.get('effort')
    tier = args.tier or route_doc.get('tier')
    if not runner or not model:
        fail(f'{pbi}.route.json に attempt {args.attempt} の実行先が無い。--runner と --model で渡す')

    prices = common.load_prices(common.resolve_home(args.home), Path(args.prices))
    transcript = None
    cost_reported = None
    if runner == 'opencode':
        got = read_opencode(base / 'opencode' / f'{args.report}.jsonl')
        cost_reported = got['cost']
    elif runner == 'codex':
        got = read_codex(base / 'codex' / f'{args.report}.log')
    elif runner == 'agy':
        got = read_agy(base / 'agy' / f'{args.report}.json')
    elif runner == 'dsh':
        got = read_dsh(base / 'dsh' / f'{args.report}.jsonl')
    else:
        transcript = find_agent_transcript(Path(args.projects), cwd, args.report)
        got = read_agent(transcript, prices)
    if runner == 'agent':
        cost_api = got['cost_api']
    else:
        cost_api = tokens_cost(got['tokens'], common.price_for(prices, model))
    verify, unchanged = read_verify(base / 'verify' / f'{args.report}.json')

    key = work_key(base, args.work_key)
    repo = args.repo or cwd.name
    group = archive_root() / repo / key_dir(key)
    dest = group / args.report / f'attempt-{args.attempt}'
    archive_info = {'dir': dest.as_posix(), 'ok': True, 'error': None}
    try:
        archive(base, dest, raw_files(base, pbi, args.report, rework), transcript)
    except OSError as error:
        archive_info.update(ok=False, error=str(error))

    row = {
        'schema': SCHEMA,
        'recorded_at': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
        'repo': repo, 'work_key': key, 'pbi': pbi, 'report': args.report, 'attempt': args.attempt,
        'rework': rework, 'tier': tier, 'runner': runner, 'model': model, 'effort': effort,
        'models_seen': got.get('models_seen'),
        'time': got['time'], 'steps': got['steps'], 'tokens': got['tokens'],
        'cost_reported': cost_reported, 'cost_api_equiv': cost_api,
        'prices_checked_at': prices.get('checked_at') if cost_api is not None else None,
        'verify': verify, 'unchanged': unchanged,
        'judged': {'outcome': args.outcome, 'findings': findings},
        'archive': archive_info,
    }
    # 保全先の runs.jsonl はこの作業キーの行だけにする (前の作業の行を写すと --archive で二重に数える)。
    # 写すのに失敗したら archive.ok を false にしてから手元に追記するので、失敗が行に残る
    if archive_info['ok']:
        mine = [r for r in read_rows(runs) if r.get('work_key') == key] + [row]
        try:
            (group / 'runs.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in mine),
                                              encoding='utf-8')
        except OSError as error:
            archive_info.update(ok=False, error=str(error))
    base.mkdir(parents=True, exist_ok=True)
    with open(runs, 'a', encoding='utf-8') as f:
        f.write(json.dumps(row, ensure_ascii=False) + '\n')
    total = got['tokens']['total']
    cost = cost_reported if cost_reported is not None else cost_api
    saved = '保全 ok' if archive_info['ok'] else f"保全に失敗 ({archive_info['error']})。作業は続けてよい"
    print(f"記録: {args.report} attempt {args.attempt} {runner} {model} {args.outcome} "
          f"tokens={total if total is not None else '不明'} "
          f"cost={'不明' if cost is None else f'{cost:.4f}'} {saved}")


if __name__ == '__main__':
    main()
