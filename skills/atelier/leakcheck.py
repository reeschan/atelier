"""改善の変更 (worker.md・designer.md・appendix の diff) に、held-out の本文・失敗の本文・レビューの why が
写されていないかを、採用の前に確かめる。events.py の `leakcheck` サブコマンドが main(argv) に委ねる。

仕様は docs/specs/harness-metrics.md の末尾の追記 §3.7。比べる元 (sources) は
`<dir>/cases.jsonl` の case と `<dir>/reviews/*.review.json` から組み、どれが held-out かは
`splits` の manifest に聞く (manifest が無ければ全部 train で、stderr に注記する)。
manifest の held-out の case が手元の `cases.jsonl` に無ければ、保全先
(`record_run.archive_root()`) の `cases.jsonl` からも読む。manifest に未登録でも
`splits.is_heldout` が held-out とする保全先の case (split assign を飛ばした case) も読む。本文は手元で読むだけで、イベントにも
標準出力にも出さない (どの case に当たったかも出さない)。

使い方:
  git diff <base>..HEAD -- skills/atelier/worker.md > change.diff
  python skills/atelier/events.py leakcheck --dir docs/pbi --change ch-... --diff change.diff
  {"verdict": "block", "overlap_bucket": "mid", "vs": ["heldout"]}   # 終了コード 3

終了コード: 0 pass / warn・3 block・2 引数やファイル・sources の誤り。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
TAXONOMY_PATH = HERE / 'taxonomy.json'

ID_RE = re.compile(r'^[A-Za-z0-9._:/@-]{1,64}\Z')
FILE_RE = re.compile(r'[A-Za-z0-9_.-]+\.[A-Za-z][A-Za-z0-9]{0,7}')
# 順序つき。index が大きいほど重い。'0' が重なり無し。
BUCKETS = ('0', 'low', 'mid', 'high')
# vs はこの順 (辞書順) で出す。
VS = ('failure', 'heldout', 'rubric')
EXIT_BLOCK = 3


# ---------- 閾値 ----------

@dataclass(frozen=True)
class Thresholds:
    """重なりの段階の境目。「重なりあり」は段階が '0' でないこと、と 1 か所で決める。

    jaccard: 行ごとの Jaccard と全体の元に対する包含率の low・mid・high の下限。
    longest: mid・high の下限 (文字数)。ident は low。
    containment_longest: 全体の包含率を数える条件となる、全体の最長一致の下限 (文字数)。
        元の文が短ければ min(containment_longest, max(gram, 0.8 * 元の長さ)) に下げる。
        下限に届かなければ包含率は段階に足さない (同じ語彙の長い無関係な diff を拾わないため)。
    """
    gram: int = 8
    jaccard: tuple = (0.15, 0.30, 0.60)
    longest: tuple = (60, 201)
    ident_min_len: int = 8
    containment_longest: int = 40

    @classmethod
    def from_mapping(cls, data: Mapping | None) -> 'Thresholds':
        """既定の上に data の項目を重ねる。知らない項目・型の誤り・単調でない境目 → ValueError。"""
        if data is None:
            return cls()
        if not isinstance(data, Mapping):
            raise ValueError('thresholds must be an object')
        unknown = set(data) - {'gram', 'jaccard', 'longest', 'ident_min_len',
                               'containment_longest'}
        if unknown:
            raise ValueError('unknown threshold: ' + ','.join(sorted(map(str, unknown))))
        gram = data.get('gram', cls.gram)
        jaccard = data.get('jaccard', cls.jaccard)
        longest = data.get('longest', cls.longest)
        ident_min_len = data.get('ident_min_len', cls.ident_min_len)
        containment_longest = data.get('containment_longest', cls.containment_longest)
        if not _is_int(containment_longest) or containment_longest < 1:
            raise ValueError('containment_longest must be a positive integer')
        if not _is_int(gram) or gram < 1:
            raise ValueError('gram must be a positive integer')
        if not _is_int(ident_min_len) or ident_min_len < 1:
            raise ValueError('ident_min_len must be a positive integer')
        if (not isinstance(jaccard, (list, tuple)) or len(jaccard) != 3
                or not all(_is_number(v) and 0.0 <= v <= 1.0 for v in jaccard)
                or not jaccard[0] < jaccard[1] < jaccard[2]):
            raise ValueError('jaccard must be three increasing numbers in [0, 1]')
        if (not isinstance(longest, (list, tuple)) or len(longest) != 2
                or not all(_is_int(v) and v >= 1 for v in longest)
                or not longest[0] < longest[1]):
            raise ValueError('longest must be two increasing positive integers')
        return cls(gram=gram, jaccard=tuple(jaccard), longest=tuple(longest),
                   ident_min_len=ident_min_len, containment_longest=containment_longest)


DEFAULT_THRESHOLDS = Thresholds()


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def load_thresholds(path: Path = TAXONOMY_PATH) -> Thresholds:
    """taxonomy.json に "leakcheck" の object があればそれを from_mapping、無ければ既定。

    ファイルが無い → 既定。JSON でない・leakcheck の中身が誤り → ValueError (main は終了コード 2)。
    """
    try:
        raw = json.loads(Path(path).read_text(encoding='utf-8'))
    except OSError:
        return DEFAULT_THRESHOLDS
    except (ValueError, TypeError):
        raise ValueError('taxonomy.json is not readable as JSON') from None
    if not isinstance(raw, dict):
        raise ValueError('taxonomy.json is not an object')
    return Thresholds.from_mapping(raw.get('leakcheck'))


# ---------- 比べる元 ----------

@dataclass(frozen=True)
class Source:
    """比べる元 1 つ。text は 1 行ぶんの本文 (空なら識別子だけの元)。本文はこの型の外へ出さない。"""
    vs: str
    text: str = ''
    idents: frozenset = field(default_factory=frozenset)


def _read_jsonl(path: Path) -> list:
    try:
        text = Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


MISSING_SHOWN = 5  # HeldoutMissing の文に並べる id の数


class SourceError(Exception):
    """比べる元を組めない。main は終了コード 2 にし、str(exc) を `leakcheck: ` に続けて出す。"""


class ManifestUnreadable(SourceError):
    """split の manifest のファイルはあるが、読めないか形が違う。"""

    def __init__(self, path):
        super().__init__(f'cannot read split manifest: {path}')
        self.path = path


class HeldoutMissing(SourceError):
    """manifest の held-out の case が、手元の cases.jsonl にも保全先にも無い。"""

    def __init__(self, ids):
        self.ids = sorted(ids)
        super().__init__('held-out case not found: ' + ', '.join(self.ids[:MISSING_SHOWN]))


def _manifest(split_name: str, home):
    """manifest を読む。splits が無い・manifest のファイルが無い → None (すべて train)。

    ファイルはあるのに読めない・形が違う (splits.load_manifest が None) → ManifestUnreadable。
    """
    try:
        import splits
    except (Exception, SystemExit):
        return None
    path = None
    try:
        path = splits._manifest_path(split_name, home)
        if not path.exists():
            return None
        manifest = splits.load_manifest(split_name, home)
    except (Exception, SystemExit):
        raise ManifestUnreadable(path if path is not None else split_name) from None
    if not isinstance(manifest, dict):
        raise ManifestUnreadable(path)
    return manifest


def _heldout_ids(manifest) -> set:
    """manifest の held-out の case の id。regression・unsplit は train と同じに扱う。"""
    cases = manifest.get('cases') if isinstance(manifest, dict) else None
    if not isinstance(cases, dict):
        return set()
    return {case_id for case_id, row in cases.items()
            if isinstance(case_id, str) and isinstance(row, dict) and row.get('split') == 'heldout'}


def _is_heldout(manifest, case_id) -> bool:
    """case が held-out か。splits.is_heldout (split_of と同じ規則) に従う。

    manifest に未登録の case も、assign が heldout を与えるなら held-out。manifest が無い・
    `…:run` のような id・split_in が None の id は train。投げない。
    """
    if not isinstance(manifest, dict):
        return False
    try:
        import splits
        return bool(splits.is_heldout(manifest, case_id))
    except (Exception, SystemExit):
        return False


def _archived_cases(missing_ids: set, manifest=None, local_ids=frozenset()) -> list:
    """手元の cases.jsonl に無い held-out の case を、保全先の cases.jsonl から 1 回の走査で読む。

    拾うのは 2 種類: manifest の held-out で手元に無い id (missing_ids) と、manifest に未登録で
    手元にも無く、splits.is_heldout が True になる id (split assign を飛ばした前の作業の case)。
    manifest が None なら後者は拾わない。export_cases.py は
    <archive_root>/<repo>/<key_dir>/cases.jsonl に写す。読めないフォルダ・ファイル・行は飛ばす。
    同じ id が複数あれば最初の 1 つだけ。
    """
    predict = isinstance(manifest, dict)
    if not missing_ids and not predict:
        return []
    assigned = manifest.get('cases') if predict else None
    if not isinstance(assigned, dict):
        assigned = {}
    try:
        import record_run
    except (Exception, SystemExit):
        return []
    try:
        root = Path(record_run.archive_root())
        repos = sorted(path for path in root.iterdir() if path.is_dir())
    except (OSError, ValueError, TypeError):
        return []
    out = []
    seen = set()
    for repo in repos:
        try:
            keys = sorted(path for path in repo.iterdir() if path.is_dir())
        except OSError:
            continue
        for key in keys:
            for row in _read_jsonl(key / 'cases.jsonl'):
                case_id = row.get('id')
                if not isinstance(case_id, str) or case_id in seen:
                    continue
                if case_id in missing_ids or (
                        predict and case_id not in assigned and case_id not in local_ids
                        and _is_heldout(manifest, case_id)):
                    seen.add(case_id)
                    out.append(row)
    return out


def _task_texts(task: dict) -> list:
    """case の task の全部の節の文字列 (targets・do_not_touch・verify も含む)。"""
    out = []
    for key in ('goal', 'targets', 'do_not_touch', 'notes', 'title'):
        value = task.get(key)
        if isinstance(value, str):
            out.append(value)
    for key in ('acceptance', 'verify'):
        rows = task.get(key)
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    for name in ('text', 'command'):
                        if isinstance(row.get(name), str):
                            out.append(row[name])
    depends = task.get('depends_on')
    if isinstance(depends, list):
        out.extend(value for value in depends if isinstance(value, str))
    return out


def _file_names(task: dict, th: Thresholds = DEFAULT_THRESHOLDS) -> set:
    names = set()
    for text in _task_texts(task):
        for match in FILE_RE.finditer(text):
            name = match.group(0)
            if len(name) >= th.ident_min_len:
                names.add(name)
    return names


def _case_idents(case: dict, shared: set, th: Thresholds = DEFAULT_THRESHOLDS) -> frozenset:
    """case に固有の識別子。PBI の id と、ほかの case の task に出ないファイル名。"""
    out = set()
    pbi = case.get('pbi')
    if isinstance(pbi, str) and pbi.strip():
        out.add(pbi.strip())
    task = case.get('task')
    if isinstance(task, dict):
        for name in _file_names(task, th):
            if name not in shared:
                out.add(name)
    return frozenset(out)


def _case_texts(case: dict) -> list:
    """private.rework の Expected・Repro の本文 (item ごと)。"""
    out = []
    private = case.get('private')
    rework = private.get('rework') if isinstance(private, dict) else None
    if not isinstance(rework, list):
        return out
    for note in rework:
        items = note.get('items') if isinstance(note, dict) else None
        if not isinstance(items, list):
            continue
        for item in items:
            text = item.get('text') if isinstance(item, dict) else None
            if isinstance(text, str) and text.strip():
                out.append(text)
    return out


def _lines(text) -> list:
    if not isinstance(text, str):
        return []
    return [line for line in text.splitlines() if line.strip()]


def _acceptance(task: dict) -> list:
    rows = task.get('acceptance')
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        text = row.get('text') if isinstance(row, dict) else None
        if isinstance(text, str) and text.strip():
            out.append(text)
    return out


def load_sources(dir: Path, *, split_name: str = 'default', home=None,
                 thresholds: Thresholds = DEFAULT_THRESHOLDS) -> list:
    """比べる元を組む。読めない行・JSON でないファイルは飛ばす。

    manifest の held-out の case が手元の cases.jsonl に無ければ、保全先の cases.jsonl からも読む。
    manifest に未登録で splits.is_heldout が True の保全先の case (split assign を飛ばした case) も読む。
    manifest のファイルがあるのに読めなければ ManifestUnreadable、held-out の case がどちらにも
    無ければ HeldoutMissing (どちらも SourceError)。held-out の本文を比べずに pass にしないため。
    """
    base = Path(dir)
    th = thresholds
    manifest = _manifest(split_name, home)
    heldout_ids = _heldout_ids(manifest)
    cases = [row for row in _read_jsonl(base / 'cases.jsonl') if isinstance(row.get('id'), str)]
    local_ids = {case['id'] for case in cases}
    missing = heldout_ids - local_ids
    if manifest is not None:
        archived = _archived_cases(missing, manifest, local_ids)
        cases.extend(archived)
        found = {row['id'] for row in archived}
        if missing - found:
            raise HeldoutMissing(missing - found)

    # ファイル名が「case に固有」かを見るため、まず全 case のファイル名を数える。
    seen = {}
    for case in cases:
        task = case.get('task')
        if not isinstance(task, dict):
            continue
        for name in _file_names(task, th):
            seen.setdefault(name, set()).add(case['id'])
    shared = {name for name, ids in seen.items() if len(ids) >= 2}

    sources = []
    for case in cases:
        heldout = case['id'] in heldout_ids or _is_heldout(manifest, case['id'])
        task = case.get('task') if isinstance(case.get('task'), dict) else {}
        vs = 'heldout' if heldout else 'failure'
        for text in _case_texts(case):
            sources.append(Source(vs=vs, text=text))
        if heldout:
            for text in _lines(task.get('goal')) + _lines(task.get('notes')):
                sources.append(Source(vs='heldout', text=text))
            for text in _acceptance(task):
                sources.append(Source(vs='heldout', text=text))
            sources.append(Source(vs='heldout', idents=_case_idents(case, shared, th)))
        elif _case_texts(case):
            sources.append(Source(vs='failure', idents=_case_idents(case, shared, th)))

    reviews = base / 'reviews'
    try:
        paths = sorted(reviews.glob('*.review.json'))
    except OSError:
        paths = []
    for path in paths:
        try:
            doc = json.loads(path.read_text(encoding='utf-8', errors='replace'))
        except (OSError, ValueError, TypeError):
            continue
        findings = doc.get('findings') if isinstance(doc, dict) else None
        if not isinstance(findings, list):
            continue
        for row in findings:
            why = row.get('why') if isinstance(row, dict) else None
            if isinstance(why, str) and why.strip():
                sources.append(Source(vs='rubric', text=why))
    return sources


# ---------- diff と比べ方 ----------

def added_lines(diff_text: str) -> list:
    """unified diff の足した行の中身 (先頭の '+' を外したもの)。

    ファイルの見出しの '+++ ' (直前の行が '--- ') と '-'・' '・'\\' で始まる行は入れない。
    """
    added = []
    previous = ''
    for line in diff_text.splitlines():
        if line.startswith('+'):
            if line.startswith('+++ ') and previous.startswith('--- '):
                previous = line
                continue
            added.append(line[1:])
        previous = line
    return added


def _norm(text: str) -> str:
    return ' '.join(text.split())


def _grams(text: str, size: int) -> frozenset:
    if size < 1 or len(text) < size:
        return frozenset()
    return frozenset(text[i:i + size] for i in range(len(text) - size + 1))


class _Longest:
    """足した文の接尾辞オートマトン。作るのに O(n)、比べる元 1 つにつき O(m)。"""

    def __init__(self, text: str):
        self._next = [{}]
        self._link = [-1]
        self._length = [0]
        last = 0
        for ch in text:
            cur = len(self._length)
            self._length.append(self._length[last] + 1)
            self._link.append(-1)
            self._next.append({})
            state = last
            while state != -1 and ch not in self._next[state]:
                self._next[state][ch] = cur
                state = self._link[state]
            if state == -1:
                self._link[cur] = 0
            else:
                nxt = self._next[state][ch]
                if self._length[state] + 1 == self._length[nxt]:
                    self._link[cur] = nxt
                else:
                    clone = len(self._length)
                    self._length.append(self._length[state] + 1)
                    self._link.append(self._link[nxt])
                    self._next.append(dict(self._next[nxt]))
                    while state != -1 and self._next[state].get(ch) == nxt:
                        self._next[state][ch] = clone
                        state = self._link[state]
                    self._link[nxt] = clone
                    self._link[cur] = clone
            last = cur

    def against(self, text: str) -> int:
        state = 0
        run = 0
        best = 0
        for ch in text:
            while state != 0 and ch not in self._next[state]:
                state = self._link[state]
                run = self._length[state]
            nxt = self._next[state].get(ch)
            if nxt is None:
                state = 0
                run = 0
            else:
                state = nxt
                run += 1
                best = max(best, run)
        return best


_IDENT_CACHE: dict = {}


def _ident_pattern(ident: str):
    pattern = _IDENT_CACHE.get(ident)
    if pattern is None:
        pattern = re.compile(r'(?<![A-Za-z0-9_-])' + re.escape(ident) + r'(?![A-Za-z0-9_])')
        _IDENT_CACHE[ident] = pattern
    return pattern


def _level_of(score: float, longest: int, length: int, ident: bool, th: Thresholds) -> int:
    """score (8-gram の Jaccard)・最長一致・識別子から段階の index を出す。

    最長一致の mid の下限は、元の文の長さに合わせて下げる (短い元を写したときに 60 字の
    下限で見逃さないため)。元が gram より短ければ、下限は gram 以上なので最長一致では拾わない。
    """
    level = 0
    for step, low in enumerate(th.jaccard, start=1):
        if score >= low:
            level = max(level, step)
    if length > 0:
        mid = min(th.longest[0], max(th.gram, int(0.8 * length)))
        if longest >= mid:
            level = max(level, 2)
    if longest >= th.longest[1]:
        level = max(level, 3)
    if ident:
        level = max(level, 1)
    return level


def _bucket(src: Source, added: str, prepared: list, whole: tuple, th: Thresholds):
    """(段階の index, vs) を返す。

    行ごとの Jaccard と最長一致、および全体の包含率と最長一致の高い方を使う。
    包含率は元の 8-gram を分母にするので、無関係な追加行で薄まらない。その代わり diff が
    長いほど偶然に上がるので、全体の最長一致が containment_longest (短い元では下げた値) に
    届いたときだけ数える。届かなくても全体の最長一致そのものは数える。
    """
    level = 0
    text = _norm(src.text)
    if text:
        other = _grams(text, th.gram)
        best_jaccard = 0.0
        best_longest = 0
        for grams, longest in prepared:
            union = grams | other
            if union:
                best_jaccard = max(best_jaccard, len(grams & other) / len(union))
            best_longest = max(best_longest, longest.against(text))
        level = _level_of(best_jaccard, best_longest, len(text), False, th)
        grams, longest = whole
        whole_longest = longest.against(text)
        gate = min(th.containment_longest, max(th.gram, int(0.8 * len(text))))
        containment = 0.0
        if other and whole_longest >= gate:
            containment = len(grams & other) / len(other)
        level = max(level, _level_of(containment, whole_longest, len(text), False, th))
    ident = any(_ident_pattern(name).search(added) for name in src.idents)
    if ident:
        level = max(level, 1)
    return level, src.vs


def _prepared(added_lines: list, th: Thresholds) -> list:
    """足した行ごとの (8-gram の集合, 最長一致を測る接尾辞オートマトン)。"""
    out = []
    for line in added_lines:
        text = _norm(line)
        if text:
            out.append((_grams(text, th.gram), _Longest(text)))
    return out


def check(added_lines: list, sources: list, thresholds: Thresholds = DEFAULT_THRESHOLDS) -> dict:
    """{'verdict': 'pass'|'warn'|'block', 'overlap_bucket': BUCKETS のどれか, 'vs': VS の部分集合}。

    本文・case の id・当たった元の位置は返さない。純粋な関数 (ファイルも環境も読まない)。
    """
    th = thresholds
    added = _norm('\n'.join(added_lines))
    prepared = _prepared(added_lines, th)
    whole = (_grams(added, th.gram), _Longest(added))
    level = 0
    hit = set()
    for src in sources:
        found, vs = _bucket(src, added, prepared, whole, th)
        if found > 0:
            hit.add(vs)
        level = max(level, found)
    vs = [name for name in VS if name in hit]
    if 'heldout' in hit or 'rubric' in hit:
        verdict = 'block'
    elif 'failure' in hit:
        verdict = 'warn'
    else:
        verdict = 'pass'
    return {'verdict': verdict, 'overlap_bucket': BUCKETS[level], 'vs': vs}


# ---------- CLI ----------

def _usage(message: str) -> int:
    print('leakcheck: ' + message, file=sys.stderr)
    return 2


def _emit(base: Path, change: str, result: dict, added: list, th: Thresholds) -> None:
    """change.checked を 1 行。同じ変更・同じ diff・同じ閾値・同じ判定なら once で 1 行にまとめる。

    判定 (verdict・overlap_bucket・並べ替えた vs) も once の元に入れる。diff が同じでも
    cases.jsonl・reviews・manifest・--home が変われば判定は変わるので、そのときはもう 1 行出す。
    例外は外へ出さない。
    """
    try:
        import events
    except (Exception, SystemExit):
        return
    outcome = '\0'.join((result['verdict'], result['overlap_bucket'],
                         ','.join(sorted(result['vs']))))
    raw = '\0'.join((change, '\n'.join(added), repr(th), outcome))
    once = 'leakcheck:' + hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]
    try:
        events.emit(base, 'change.checked', {'change_id': change, **result}, once=once)
    except (Exception, SystemExit):
        pass


def main(argv=None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog='leakcheck.py',
                                     description='改善の diff に held-out の本文が写っていないか確かめる')
    parser.add_argument('--dir', required=True)
    parser.add_argument('--change', required=True)
    parser.add_argument('--diff', required=True)
    parser.add_argument('--split-name', dest='split_name', default='default')
    parser.add_argument('--home')
    try:
        args = parser.parse_args(args_list)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    base = Path(args.dir)
    if not base.is_dir():
        return _usage(f'no such --dir: {args.dir}')
    if not (isinstance(args.change, str) and ID_RE.match(args.change)):
        return _usage(f'bad --change: {args.change}')
    try:
        diff_text = Path(args.diff).read_text(encoding='utf-8', errors='replace')
    except (OSError, ValueError):
        return _usage(f'cannot read --diff: {args.diff}')
    try:
        th = load_thresholds()
    except (ValueError, TypeError):
        return _usage('taxonomy.json: bad leakcheck thresholds')
    try:
        manifest = _manifest(args.split_name, args.home)
        sources = load_sources(base, split_name=args.split_name, home=args.home, thresholds=th)
    except SourceError as exc:
        return _usage(str(exc))
    except (Exception, SystemExit) as exc:
        return _usage(f'cannot load sources: {exc}')
    if manifest is None:
        print('leakcheck: no split manifest; held-out not checked', file=sys.stderr)
    try:
        added = added_lines(diff_text)
        result = check(added, sources, th)
        _emit(base, args.change, result, added, th)
    except (Exception, SystemExit) as exc:
        return _usage(f'check failed: {type(exc).__name__}')
    print(json.dumps(result, ensure_ascii=False))
    return EXIT_BLOCK if result['verdict'] == 'block' else 0


if __name__ == '__main__':
    sys.exit(main())
