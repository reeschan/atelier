"""差し戻しの attempt を「失敗の空間」に埋め込み、保全先の vectors.jsonl に kind: failure で置く。

仕様は docs/specs/harness-metrics.md の追記 §3.6。埋め込む文は決まったテンプレートで組み、ベクトルは
手元の保全先にだけ書く。文もベクトルも外へは送らない (送り先は設定の埋め込みサーバーだけ)。
このスクリプトは events.jsonl に何も書かない。

使い方:
  python skills/atelier/failures.py failure-embed --dir docs/pbi

終了コード: 0 (対象 0 件・埋め込みの失敗もここ) / 2 引数の誤り。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import export_cases
import record_run
import task_profile

TEMPLATE_VERSION = 'failure/1'
DEFAULT_MODEL = 'qwen3-embedding:0.6b'
SENT_BACK = ('差し戻し', 'sent-back')
SEVERITIES = (('critical', '重大'), ('major', '中'), ('minor', '軽微'))
CAUSE_ORDER = {name: index for index, name in enumerate(record_run.CAUSES)}
FREE_TEXT_LIMIT = 400


# ---------- 失敗の文 (テンプレート) ----------

def _clean(text) -> str:
    """自由文から改行・区切りの `|`・連続する空白を落とす。区切り ` | ` が崩れないようにする。"""
    if not isinstance(text, str):
        return ''
    text = re.sub(r'\s+', ' ', text).strip()
    text = text.replace('|', ' ')
    return re.sub(r'\s+', ' ', text).strip()


def _clip(text, limit: int = FREE_TEXT_LIMIT) -> str:
    """自由文を 1 行に直してから先頭 limit 字で切る。"""
    return _clean(text)[:limit]


def _causes_text(row: dict) -> str:
    judged = row.get('judged') if isinstance(row.get('judged'), dict) else {}
    raw = judged.get('cause')
    if isinstance(raw, str):
        raw = raw.split(',')
    if not isinstance(raw, list):
        return 'none'
    names = {str(name).strip() for name in raw if isinstance(name, str) and str(name).strip()}
    if not names:
        return 'none'
    ordered = sorted(names, key=lambda name: (CAUSE_ORDER.get(name, len(CAUSE_ORDER)), name))
    return ','.join(ordered)


def _findings_text(row: dict) -> str:
    found = record_run.attempt_findings(row)
    if not isinstance(found, dict):
        return 'none'
    values = [found.get(english) for english, _japanese in SEVERITIES]
    if not any(isinstance(value, int) and not isinstance(value, bool) for value in values):
        return 'none'
    counts = [value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
              for value in values]
    return 'critical={},major={},minor={}'.format(*counts)


def _review_why(review) -> str:
    """review JSON の findings[].why を findings の順に `; ` でつなぎ、400 字で切る。"""
    if isinstance(review, str):
        try:
            review = json.loads(review)
        except ValueError:
            review = None
    if not isinstance(review, dict):
        return 'none'
    findings = review.get('findings')
    if not isinstance(findings, list):
        return 'none'
    parts = []
    for item in findings:
        if not isinstance(item, dict):
            continue
        why = _clean(item.get('why'))
        if why:
            parts.append(why)
    if not parts:
        return 'none'
    return _clip('; '.join(parts)) or 'none'


def _expected_text(rework_note) -> str:
    """rework note の `- Expected:` の項目を `; ` でつなぎ、400 字で切る。"""
    if not isinstance(rework_note, str) or not rework_note:
        return 'none'
    parts = []
    for match in export_cases.EXPECTED.finditer(rework_note):
        if match.group(1) != 'Expected':
            continue
        item = _clean(match.group(2))
        if item:
            parts.append(item)
    if not parts:
        return 'none'
    return _clip('; '.join(parts)) or 'none'


def failure_text(row, rework_note, review) -> str:
    """失敗の形を `causes | findings | review の why | test の分類 | rework note の Expected` にする。

    row は runs.jsonl の 1 行、rework_note は rework note の本文 (無ければ None)、review は
    reviews/<report>.review.json を読んだ dict (無ければ None)。test の分類は tester の結果を
    attempt に結べないので今は常に unknown。why と Expected は 400 字で切る。
    """
    row = row if isinstance(row, dict) else {}
    return ' | '.join((_causes_text(row), _findings_text(row), _review_why(review),
                       'unknown', _expected_text(rework_note)))


# ---------- space_id ----------

def _timeout_of(config: dict):
    timeout = config.get('timeout')
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        return task_profile.EMBED_DEFAULTS['timeout']
    return timeout


def _model_of(config: dict) -> str:
    model = config.get('model') if isinstance(config, dict) else None
    return model if isinstance(model, str) and model else DEFAULT_MODEL


def show_digest(config) -> str:
    """POST <url>/api/show の返りの最上位の digest。答えない・無いときは none。"""
    try:
        if not isinstance(config, dict):
            return 'none'
        url = config.get('url')
        if not isinstance(url, str) or not url.strip():
            return 'none'
        body = json.dumps({'model': _model_of(config)}, ensure_ascii=False).encode('utf-8')
        request = urllib.request.Request(url.rstrip('/') + '/api/show', data=body, method='POST',
                                         headers={'Content-Type': 'application/json'})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=_timeout_of(config)) as response:
            raw = response.read()
        document = json.loads(raw.decode('utf-8'))
        digest = document.get('digest') if isinstance(document, dict) else None
        return digest if isinstance(digest, str) and digest else 'none'
    except (Exception, SystemExit):
        return 'none'


def space_id(config, template_version) -> str:
    """失敗の空間の id。sha12(model, /api/show の digest, dims, template_version, "failure")。

    dims は設定の `dims` (null なら embed_failures が返りのベクトルの長さを入れる)。式はここ 1 か所だけ。
    """
    config = config if isinstance(config, dict) else {}
    dims = config.get('dims')
    dims = dims if isinstance(dims, int) and not isinstance(dims, bool) and dims > 0 else None
    parts = [_model_of(config), show_digest(config), dims, template_version, 'failure']
    blob = json.dumps(parts, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(blob).hexdigest()[:12]


# ---------- 保全先 (手元だけ) ----------

def _read_jsonl(path: Path) -> list:
    try:
        lines = Path(path).read_text(encoding='utf-8').splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _write_jsonl(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = ''.join(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n' for row in rows)
    with open(path, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(text)


def archive_vectors_path(base, repo, work_key) -> Path:
    """task_profile と同じ式: <archive_root>/<repo>/key_dir(<work_key>)/vectors.jsonl。"""
    return record_run.archive_root() / str(repo) / record_run.key_dir(work_key) / 'vectors.jsonl'


def _current_work_key(base: Path, rows: list):
    """今の作業キー。STATUS.md に無ければ runs.jsonl の最後の行の値 (門 R2: 前の作業を混ぜない)。"""
    key = record_run.work_key(Path(base), None)
    if isinstance(key, str) and key:
        return key
    for row in reversed(rows):
        value = row.get('work_key') if isinstance(row, dict) else None
        if isinstance(value, str) and value:
            return value
    return None


def _belongs(row: dict, key) -> bool:
    value = row.get('work_key')
    if key is None:
        return not (isinstance(value, str) and value)
    return value == key


def _is_sent_back(row: dict) -> bool:
    judged = row.get('judged') if isinstance(row.get('judged'), dict) else {}
    return judged.get('outcome') in SENT_BACK


def _row_run_id(base: Path, row: dict) -> str:
    """この行を記録した run。行が run_id を持つならそれを使い、古い行だけ今の run.json に落ちる。"""
    value = row.get('run_id')
    if isinstance(value, str) and value:
        return value
    return record_run.run_id_of(base)


def _unit_id(run_id: str, row: dict):
    report = row.get('report') or row.get('pbi')
    attempt = row.get('attempt')
    if not isinstance(report, str) or not report:
        return None
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        return None
    return f'at:{run_id}:{report}:{attempt}'


def _case_id(row: dict) -> str:
    pbi = row.get('pbi') if isinstance(row.get('pbi'), str) else ''
    return f"{row.get('repo')}:{row.get('work_key') or ''}:{pbi}"


def _rework_note(base: Path, row: dict):
    """差し戻しの行に対応する note は `<pbi>.rework-<row.rework + 1>.md` (R3)。"""
    pbi = row.get('pbi')
    if not isinstance(pbi, str) or not pbi:
        return None
    rework = row.get('rework')
    if not isinstance(rework, int) or isinstance(rework, bool) or rework < 0:
        rework = 0
    try:
        return (Path(base) / f'{pbi}.rework-{rework + 1}.md').read_text(encoding='utf-8', errors='replace')
    except OSError:
        return None


def _review(base: Path, row: dict):
    report = row.get('report') or row.get('pbi')
    if not isinstance(report, str) or not report:
        return None
    try:
        raw = json.loads((Path(base) / 'reviews' / f'{report}.review.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _failure_row(config: dict, space: str, row: dict, unit_id: str, vector: list) -> dict:
    return {
        'kind': 'failure',
        'space_id': space,
        'case_id': _case_id(row),
        'unit_id': unit_id,
        'model': _model_of(config),
        'dim': len(vector),
        'vector': vector,
    }


# ---------- 本体 ----------

def embed_failures(dir) -> int:
    """今の作業キーの差し戻しの行を埋め込み、足した行の数を返す。失敗も 0 件で返す。"""
    base = Path(dir)
    rows = record_run.read_rows(base / 'runs.jsonl')
    key = _current_work_key(base, rows)
    targets = []
    skipped = 0
    for row in rows:
        if not isinstance(row, dict) or not _is_sent_back(row):
            continue
        if not _belongs(row, key):
            skipped += 1
            continue
        unit_id = _unit_id(_row_run_id(base, row), row)
        if unit_id is None:
            continue
        targets.append((row, unit_id))
    if not targets:
        if skipped:
            print(f'failure-embed: 別の作業の差し戻し {skipped} 行を飛ばした', file=sys.stderr)
        return 0

    config = task_profile.embedding_config()
    if config is None:
        print('failure-embed: embedding の設定が無い (0 件)', file=sys.stderr)
        return 0
    texts = [failure_text(row, _rework_note(base, row), _review(base, row)) for row, _ in targets]
    vectors = task_profile.embed(texts, config)
    if vectors is None or len(vectors) != len(targets):
        print(f'failure-embed: {config.get("url")} に届かない (0 件)', file=sys.stderr)
        return 0

    # dims が設定に無いときは、返りのベクトルの長さを space_id の材料にする。
    space_config = dict(config)
    if space_config.get('dims') is None and vectors and vectors[0]:
        space_config['dims'] = len(vectors[0])
    space = space_id(space_config, TEMPLATE_VERSION)

    if skipped:
        print(f'failure-embed: 別の作業の差し戻し {skipped} 行を飛ばした', file=sys.stderr)

    files: dict = {}
    dirty = set()

    def rows_of(path: Path) -> list:
        if path not in files:
            files[path] = _read_jsonl(path)
        return files[path]

    added = 0
    for (row, unit_id), vector in zip(targets, vectors):
        path = archive_vectors_path(base, row.get('repo') or 'unknown', row.get('work_key'))
        current = rows_of(path)
        if any(item.get('kind') == 'failure' and item.get('space_id') == space
               and item.get('unit_id') == unit_id for item in current):
            continue
        current.append(_failure_row(space_config, space, row, unit_id, vector))
        dirty.add(path)
        added += 1

    for path in dirty:
        _write_jsonl(path, files[path])
    return added


# ---------- CLI ----------

def _config_url():
    try:
        config = task_profile.embedding_config()
        url = config.get('url') if isinstance(config, dict) else None
        return url if isinstance(url, str) and url else '?'
    except (Exception, SystemExit):
        return '?'


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ('failure-embed', 'embed'):
        args = args[1:]
    parser = argparse.ArgumentParser(prog='failures.py',
                                     description='差し戻しの attempt を失敗の空間に埋め込む')
    parser.add_argument('--dir', default='docs/pbi')
    try:
        parsed = parser.parse_args(args)
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 2
    try:
        embed_failures(Path(parsed.dir))
    except (Exception, SystemExit):
        # 文・why・Expected を標準エラーに出さない (URL と件数だけ)。
        print(f'failure-embed: {_config_url()} で失敗 (0 件)', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
