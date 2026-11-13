"""設計の正本 (docs/pbi/design-record.json) と関心事の抜粋を書き、承認用の Markdown を出し、貼られた返信を正本と照合する
(appendix/adapters.md、`## Confirm`)。HTML は作らない。am.mjs も呼ばない。

使い方 (リポジトリのルートで):
  python <このディレクトリ>/design_record.py slice <id> --question Q [--read PATH:START-END ...] [--unknown U ...] [--dir docs/pbi]
  python <このディレクトリ>/design_record.py write --id ID --target PBI-<n> [--target ...] [--concern notes/<id>.slice.json ...] [--work PBI-<n>.work.json ...] [--dir docs/pbi]
  python <このディレクトリ>/design_record.py lint --draft FILE
  python <このディレクトリ>/design_record.py markdown --draft FILE [--dir docs/pbi]
  python <このディレクトリ>/design_record.py check-reply [--reply FILE] [--draft FILE] [--dry-run] [--dir docs/pbi]
  python <このディレクトリ>/design_record.py work PBI-<n> [--dir docs/pbi]
  python <このディレクトリ>/design_record.py stale PBI-<n> [--dir docs/pbi]

stale   PBI-<n>.work.json の target_blobs と今の作業ツリーの各ファイルの git blob のハッシュを比べる
        (docs/specs/work-json.md)。depends_on の各 PBI の作業票の targets に入るパスは除く。残る違い
        (changed・removed・設計のとき無かったファイルが今ある appeared) を `stale<TAB>種類<TAB>パス` で標準出力に出し、
        終了コード 7。違いが無ければ `fresh` を出して 0。作業票や依存の作業票が無い・形が違う (target_blobs が
        targets と合わない、自分自身に依存する)・パスがリポジトリの外を指すときは、標準エラーに出して 2。
        承認の正本 (/2) があれば、古さを見る前に、自分と依存の作業票が記録のときのままかを確かめ、
        違えば (書き換え・消えた・記録に無い) 標準エラーに出して 3。最初に起こすときだけ流し、手戻しでは流さない。
work   PBI-<n>.md と PBI-<n>.route.json から実装役への受け渡しの JSON docs/pbi/PBI-<n>.work.json (atelier.work/1) を書く
        (docs/specs/work-json.md)。goal・targets・do_not_touch・depends_on・base_sha (Git の HEAD)・target_blobs
        (作業ツリー上の対象ファイルの git blob のハッシュ。無いファイルは null)・route・slices (Notes の `slice: <id>` の行)・refs。
        Goal・Targets・Do not touch の節が無い・空、Goal・Targets が none、Targets にパスを取れない行がある、route.json が無い・読めない、指した slice のファイルが無いときは
        原因を全部標準エラーに出し、何も書かずに終了コード 6。リポジトリのルートで動かす (パスはそこからの相対)。
slice  docs/pbi/notes/<id>.slice.json に、問い・読んだ path と行・unknown だけを書く。ファイルの本文は入れない。
write   正本を書く。中身は schema・design_id・revision・targets・concerns (schema 1) と、--work を渡したときの works
        (schema atelier.design-record/2。works は {pbi, file, hash} の並びで、hash は作業票の中身の sha256。順に意味がある)。
        --work も前の /2 の正本も無いときは schema 1 のまま書く (既存の承認のハッシュを変えない)。同じ id の正本があり、
        targets・concerns・works (並びか中身) が変わったときだけ revision を 1 上げる。別の id なら 1 から。
        concerns や --work のファイルが無ければ 2。slice は atelier.design-slice/2 (base_sha を持つ。/1 も読む)。
lint    承認の原稿 (appendix/approve.md) を点検する。7 つの節の名前と順・前後の図・あとの図の印・PBI の表の見出し・
        ask の置き場所と選択肢の補足を見る。誤りは標準エラーに `✗ L<行>` と `例:` の行で全部出し、終了コード 6。
        説明の無い内部の言葉は `! L<行> <言葉> → <言い換えの案>` の警告で、終了コードは 0 のまま。正本は読まない。
markdown 先に lint と同じ点検をし、誤りがあれば何も出さずに 6 で止める。通れば、設計の下書き (Markdown) に返信の照合情報 reply-meta-<name>: <value> (design_id・revision・record_hash・draft_hash) を
        frontmatter として足して標準出力に出す。skill に渡すのも、skill が無いときチャットに出すのも、この同じ Markdown。
check-reply 貼られた返信 (--reply、無ければ標準入力) を正本と照合する。\\r\\n を \\n にし、行頭で始まる最後の
        ```am-reply-meta フェンスだけを読む (返信の末尾にあるもの。`  > ` の引用の中は行頭ではないので読まない)。
        design_id・revision・record_hash を正本から計算した値と比べ、--draft を渡したときは draft_hash も比べる。
        未選択の ask (答えが **—**) は未回答で、承認ではない。--draft を渡したときは、下書きの ```ask の囲みの問いが
        返信の決定に無いものも未回答に数える (決定を書かない返信で承認にしない)。

hash: record_hash は正本を sort_keys・区切り (',', ':')・ensure_ascii=False の JSON にした UTF-8 の sha256。
      draft_hash は下書きから reply-meta- の行を除き、\\r\\n を \\n にした UTF-8 の sha256。

check-reply の終了コード:
  0 一致。docs/pbi/design-approvals.jsonl に 1 行足す (--dry-run なら足さない)
  3 不一致 (値が違う、または revision が古い)。止める。認証の失敗ではない
  4 フェンスが無い・読めない。どの版にも結ばない
  5 一致したが未回答の ask がある。承認ではないので記録しない
  2 引数の誤り・正本が無い
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import common

# イベントの口 (同じフォルダ)。無い環境でも照合と記録は続ける
try:
    import events
except (Exception, SystemExit):
    events = None

SCHEMA = 'atelier.design-record/1'
SCHEMA_2 = 'atelier.design-record/2'
RECORD_NAME = 'design-record.json'
APPROVALS_NAME = 'design-approvals.jsonl'
SLICE_SCHEMA = 'atelier.design-slice/2'
SLICE_SCHEMAS = ('atelier.design-slice/1', SLICE_SCHEMA)

# renderer (answer-me-with-any の src/parse.js) が受ける照合情報の名前と値の形・件数
META_NAME = re.compile(r'^[a-z][a-z0-9_]{0,31}$')
META_VALUE = re.compile(r'^[A-Za-z0-9._:+/=-]{1,128}$')
META_MAX = 16
META_PREFIX = 'reply-meta-'
FENCE_OPEN = '```am-reply-meta'
# 返信の決定の行 (renderer の replyText): "1. [K] 問い" と、その次の "   → **答え** _(注記)_"
DECISION = re.compile(r'^(\d+)\. \[([^\]]*)\] (.*)$')
ANSWER = re.compile(r'^ {3}[←→] (.*)$')
UNANSWERED = '**—**'
# 下書きの問いの囲み (answer-me-with-any の ask ブロック)
ASK_OPEN = '```ask'
SLICE_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
# 作業票 (work) の形と、PBI の Markdown から読む行の形
WORK_SCHEMA = 'atelier.work/1'
PBI_NAME = re.compile(r'^PBI-\d+$')
PBI_REF = re.compile(r'PBI-\d+')
NONE_WORDS = ('none', 'なし')
BULLET = re.compile(r'^\s*[-*](?:\s+|$)')
WORK_TARGET_LINE = re.compile(r'^\s*(?:`([^`]*)`|(\S+))')
WORK_REF_LINE = re.compile(r'^\s*(?:[-*]\s*)?`?([\w.-]+)`?')
WORK_SLICE_LINE = re.compile(r'^\s*(?:[-*]\s+)?slice:[ \t]*(.*?)\s*$')
WORK_DEPENDS = re.compile(r'^\s*-\s*Depends on:\s*(.*)$', re.M)
READ =re.compile(r'^(.+):(\d+)(?:-(\d+))?$')

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_MISMATCH = 3
EXIT_UNBOUND = 4
EXIT_UNANSWERED = 5
EXIT_DRAFT = 6
EXIT_STALE = 7


def fail(msg: str, code: int = EXIT_USAGE) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)


def normalize(text: str) -> str:
    """改行を \\n にそろえる (\\r\\n と、残った \\r)。"""
    return text.replace('\r\n', '\n').replace('\r', '\n')


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def record_hash(record: dict) -> str:
    return sha256(json.dumps(record, sort_keys=True, separators=(',', ':'), ensure_ascii=False))


def strip_meta_lines(draft: str) -> str:
    """下書きから reply-meta- で始まる行を除く (frontmatter の中だけ)。改行は \\n にそろえる。"""
    lines = normalize(draft).split('\n')
    if not lines or lines[0].strip() != '---':
        return '\n'.join(lines)
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == '---'), None)
    if end is None:
        return '\n'.join(lines)
    head = [line for line in lines[1:end] if not line.startswith(META_PREFIX)]
    return '\n'.join([lines[0], *head, *lines[end:]])


def draft_hash(draft: str) -> str:
    return sha256(strip_meta_lines(draft))


def check_meta(entries: list) -> list[str]:
    """照合情報の名前・値・件数が renderer の規則に合うか。合わないものの説明 (英語) を返す。"""
    errors = []
    if len(entries) > META_MAX:
        errors.append(f'too many reply-meta entries ({len(entries)} > {META_MAX})')
    for name, value in entries:
        if not META_NAME.fullmatch(name):
            errors.append(f'reply-meta name {name!r} must match {META_NAME.pattern}')
        if not META_VALUE.fullmatch(value):
            errors.append(f'reply-meta-{name} value {value!r} must match {META_VALUE.pattern}')
    return errors


def meta_entries(record: dict, draft: str) -> list:
    return [('design_id', record['design_id']), ('revision', str(record['revision'])),
            ('record_hash', record_hash(record)), ('draft_hash', draft_hash(draft))]


def envelope_base(draft: str) -> str:
    """照合情報を入れる前の、出力と同じ形の下書き: frontmatter が無ければ空の frontmatter を付け、末尾を改行で終える。

    draft_hash はこの形で取る。入力のまま取ると、markdown が足す frontmatter と改行のぶん、出力と合わない。
    """
    text = normalize(draft)
    lines = text.split('\n')
    has_front = bool(lines) and lines[0].strip() == '---' and any(line.strip() == '---' for line in lines[1:])
    if not has_front:
        text = '---\n---\n' + text
    return text if text.endswith('\n') else text + '\n'


def with_envelope(draft: str, entries: list) -> str:
    """下書きの frontmatter に照合情報の行を足す (前からある reply-meta- の行は置き換える)。frontmatter が無ければ作る。"""
    body = strip_meta_lines(draft)
    meta = [f'{META_PREFIX}{name}: {value}' for name, value in entries]
    lines = body.split('\n')
    if lines and lines[0].strip() == '---':
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == '---'), None)
        if end is not None:
            return '\n'.join([*lines[:end], *meta, *lines[end:]])
    return '\n'.join(['---', *meta, '---', *lines])


class DraftError(ValueError):
    """下書きに lint の誤りがある。errors と warnings は lint_draft の戻り値の形のまま。"""

    def __init__(self, errors: list, warnings: list):
        super().__init__(f'{len(errors)} error(s) in the approval draft')
        self.errors = errors
        self.warnings = warnings


def approve_meta(draft_text: str, record: dict) -> str:
    """承認の原稿に照合情報 (design_id・revision・record_hash・draft_hash) を足した原稿を返す。

    順は今の markdown と同じ: lint (誤りがあれば DraftError)、envelope_base、meta_entries、
    check_meta (誤りがあれば ValueError)、with_envelope。sys.exit も print もしない。
    警告が要るときは呼び手が lint_draft を別に呼ぶ。record は正本 (読めて正しい形のもの) を渡す。
    """
    errors, warnings = lint_draft(draft_text)
    if errors:
        raise DraftError(errors, warnings)
    base = envelope_base(draft_text)
    entries = meta_entries(record, base)
    problems = check_meta(entries)
    if problems:
        raise ValueError('; '.join(problems))
    return with_envelope(base, entries)


def question_meta(draft_text: str, run_id: str | None, seq: int, kind: str) -> str:
    """approve 以外の問いの原稿に照合情報 (run_id・seq・kind・draft_hash) を足した原稿を返す。

    draft_hash は approve と同じ規則 (envelope_base の原稿から取る)。run_id が None なら none と書く。
    check_meta は通さない (approve の 4 項目用)。
    """
    base = envelope_base(draft_text)
    entries = [('run_id', 'none' if run_id is None else run_id), ('seq', str(seq)), ('kind', kind),
               ('draft_hash', draft_hash(base))]
    return with_envelope(base, entries)


def split_reply(text: str) -> tuple[str, str | None]:
    """返信を (本文, フェンスの中の JSON) に分ける。行頭で始まる最後のフェンスが末尾 (空白を除く) で閉じるときだけ読む。"""
    text = normalize(text)
    start = None
    position = 0
    for line in text.split('\n'):
        if line == FENCE_OPEN:
            start = position
        position += len(line) + 1
    if start is None:
        return text, None
    rest = text[start + len(FENCE_OPEN) + 1:]
    match = re.fullmatch(r'(.*?)\n```\s*', rest, re.S)
    if match is None:
        return text, None
    return text[:start], match.group(1)


def draft_asks(draft: str) -> list[dict]:
    """下書きの ```ask の囲みを順に読む。[{question, options}]。

    問いは囲みの最初の空でない行。選択肢は `* ` (推奨) か `- ` で始まる行の、` | ` より前。
    """
    found = []
    block = None
    for line in normalize(draft).split('\n'):
        if block is None:
            if line.strip() == ASK_OPEN:
                block = []
            continue
        if line.strip() == '```':
            rows = [row.strip() for row in block if row.strip()]
            if rows:
                options = [row[2:].split(' | ')[0].strip() for row in rows[1:] if row[:2] in ('* ', '- ')]
                found.append({'question': rows[0], 'options': options})
            block = None
            continue
        block.append(line)
    return found


def same_question(a: str, b: str) -> bool:
    """問いの文が同じか (空白の違いは見ない)。"""
    return ' '.join(a.split()) == ' '.join(b.split())


def decisions(body: str) -> list[dict]:
    """返信の決定の一覧。[{number, panel, question, answer, answered}]。答えが **—** なら未回答。"""
    found = []
    lines = body.split('\n')
    for index, line in enumerate(lines):
        match = DECISION.match(line)
        if not match:
            continue
        nxt = lines[index + 1] if index + 1 < len(lines) else ''
        answer = ANSWER.match(nxt)
        raw = answer.group(1) if answer else ''
        picked = raw.split(' _(')[0].strip()
        found.append({'number': int(match.group(1)), 'panel': match.group(2), 'question': match.group(3),
                      'answer': picked, 'answered': bool(picked) and picked != UNANSWERED})
    return found


def reply_choice(found: list[dict]):
    """返信の答えが 1 つだけで、a〜z の 1 文字ならその値。それ以外は None (イベントの choice)。"""
    answers = [d['answer'] for d in found if d['answered']]
    if len(answers) == 1 and len(answers[0]) == 1 and 'a' <= answers[0] <= 'z':
        return answers[0]
    return None


def compare(meta: dict, record: dict, draft: str | None) -> list[str]:
    """照合情報と正本の食い違い。空なら一致。revision が正本より古いときはそう書く。"""
    problems = []
    if not isinstance(meta, dict) or not all(isinstance(v, str) for v in meta.values()):
        return ['reply-meta is not an object of strings']
    if meta.get('design_id') != record['design_id']:
        problems.append(f"design_id {meta.get('design_id')!r} != {record['design_id']!r}")
    revision = meta.get('revision')
    if revision != str(record['revision']):
        if revision is not None and revision.isdigit() and int(revision) < record['revision']:
            problems.append(f"revision {revision} is older than {record['revision']}")
        else:
            problems.append(f"revision {revision!r} != {record['revision']}")
    elif meta.get('record_hash') != record_hash(record):
        problems.append('record_hash differs from the record')
    if draft is not None and meta.get('draft_hash') != draft_hash(draft):
        problems.append('draft_hash differs from --draft')
    return problems


# 承認の原稿の点検 (designer.md、`## Approve`)。節は決まった名前で決まった順。日本語と英語のどちらの名前でも受ける
APPROVAL_SECTIONS = [('結論', 'Summary'), ('今の流れ', 'Before'), ('この run のあとの流れ', 'After'),
                     ('決めること', 'Decisions'), ('PBI', 'PBIs'), ('影響範囲', 'Impact'),
                     ('作る人と見直し', 'Who builds')]
SECTION_BEFORE, SECTION_AFTER, SECTION_DECISIONS, SECTION_PBI = 1, 2, 3, 4
PBI_HEADERS = (['PBI', '何をするか', '終わると何ができるか', '先に要る PBI'],
               ['PBI', 'What it does', 'What works after', 'Needs'])
DIAGRAMS = ('flow', 'tree', 'er', 'sequence')
NO_DECISIONS = ('なし', 'none')
HEADING = re.compile(r'^##\s+(.*?)\s*$')
PANEL_ID = re.compile(r'^[A-Z]\s+(?=\S)')
PANEL_ATTRS = re.compile(r'\s*\{[^}]*\}\s*$')
FENCE = re.compile(r'^\s*(```+|~~~+|:::)\s*([A-Za-z-]*)')
CHANGE_MARK = re.compile(r'^\s*[+~-] ')
TABLE_ROW = re.compile(r'^\s*\|(.*)\|\s*$')
OPTION = re.compile(r'^\s*[*-] ')
EXPLAINED = re.compile(r'[(（]')
# 説明なしに出ると、atelier の外の人に通じない言葉と、その言い換えの案。言い換えは機械に任せきれないので警告だけにする。
# 英字の語は前後が英数字でないことで区切る (\b は日本語の文字も語の一部に数え、「routeの」に当たらないため)。
# 漢字 1 字の語は前後が漢字でないときだけ当てる (「段階」「手段」「波線」に当てないため)
_KANJI = '一-龥々'
_A, _Z = r'(?<![A-Za-z0-9_])', r'(?![A-Za-z0-9_])'


def _word(pattern: str) -> str:
    return _A + '(?:' + pattern + ')' + _Z


def _kanji(char: str) -> str:
    return f'(?<![{_KANJI}]){char}(?![{_KANJI}])'


INTERNAL_TERMS = [
    (re.compile(_word('route'), re.I), 'route', 'どのモデルにどの順で頼むか'),
    (re.compile(_word('tiers?'), re.I), 'tier', 'モデルの格 (opus・sonnet など)'),
    (re.compile(_word('stages?') + '|' + _kanji('段'), re.I), '段 (stage)',
     '設計者の作業の段階 (理解・設計・計画など)。載せるなら何をしたかを書く'),
    (re.compile(_word('waves?') + '|' + _kanji('波'), re.I), '波 (wave)', '同時に進める PBI のまとまり'),
    (re.compile(_word(r'criteria_\w+|rework_quality|names_condition')), '品質の点数の名前',
     '点数は載せず、判断が要るなら何が足りないかを言葉で書く'),
    (re.compile(_word('novelty|coupling|ambiguity'), re.I), 'novelty・coupling・ambiguity',
     '新しさ・ほかの部分とのつながり・あいまいさ (モデルを選ぶ目安)'),
    (re.compile(_A + r'(?:understand|design|map|lint-survey|lint-recheck|plan|route|gate|build|review|verify|'
                r'fix|finish)\.[\w-]+'), '部品の名前', '何をする段階かを言葉で書く (例えば「影響範囲の点検」)'),
    (re.compile(r'優先テンプレート|' + _word('priority[ -]templates?'), re.I), '優先テンプレート',
     'どのモデルを先に使うかの決まり'),
    (re.compile(_word('blast[ -]radius'), re.I), 'blast radius', '影響範囲の点検'),
    (re.compile(_word(r'P-\d+')), '課題の番号', '番号でなく、その課題の中身を言葉で書く'),
    (re.compile('封筒|' + _word('envelope|reply-meta'), re.I), '封筒 (reply-meta)', '照合情報'),
]


def draft_lines(draft: str) -> list[tuple[int, str]]:
    """frontmatter を除いた本文の行を (ファイルの行番号, 行) で返す。"""
    lines = normalize(draft).split('\n')
    start = 0
    if lines and lines[0].strip() == '---':
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == '---'), None)
        if end is not None:
            start = end + 1
    return [(i + 1, lines[i]) for i in range(start, len(lines))]


def section_name(title: str) -> str:
    return PANEL_ID.sub('', PANEL_ATTRS.sub('', title)).strip()


def section_index(title: str) -> int | None:
    name = section_name(title)
    return next((i for i, names in enumerate(APPROVAL_SECTIONS) if name in names), None)


def parse_draft(draft: str) -> list[dict]:
    """本文を `##` の節に分ける。囲み (```・:::) の中の `##` は見出しにしない。
    [{title, line, rows: [(行番号, 行, 囲みの言語 or None)]}]。最初の見出しの前の行は title None の節に入る。"""
    sections = [{'title': None, 'line': 1, 'rows': []}]
    fence, lang = None, None
    for number, line in draft_lines(draft):
        match = FENCE.match(line)
        if fence is None and match:
            fence, lang = match.group(1), match.group(2).lower()
            sections[-1]['rows'].append((number, line, lang))
            continue
        if fence is not None and match and line.strip() == fence:
            fence, lang = None, None
            sections[-1]['rows'].append((number, line, None))
            continue
        heading = HEADING.match(line) if fence is None else None
        if heading:
            sections.append({'title': heading.group(1), 'line': number, 'rows': []})
            continue
        sections[-1]['rows'].append((number, line, lang if fence is not None else None))
    return sections


def ask_blocks(rows) -> list[tuple[tuple[int, str], list[tuple[int, str]]]]:
    """節の行から ask の囲みを順に取り出す。[((開きの行番号, 開きの行), [(行番号, 中の行)])]。"""
    # 開きの行も中の行も言語 ask を持ち、閉じの行は持たない (parse_draft)。ask の行が途切れたら次の開きは新しい囲み
    blocks = []
    inside = False
    for number, line, lang in rows:
        if lang != 'ask':
            inside = False
            continue
        if not inside:
            blocks.append(((number, line), []))
            inside = True
            continue
        blocks[-1][1].append((number, line))
    return blocks


def table_headers(rows) -> list[list[str]]:
    """囲みの外の表の見出しの行 (次の行が区切りの行) を、セルの並びにして返す。"""
    plain = [(n, line) for n, line, lang in rows if lang is None]
    headers = []
    for (_, line), (_, below) in zip(plain, plain[1:]):
        head, rule = TABLE_ROW.match(line), TABLE_ROW.match(below)
        if head and rule and set(rule.group(1).replace('|', '').strip()) <= set('-: '):
            headers.append([cell.strip() for cell in head.group(1).split('|')])
    return headers


def lint_draft(draft: str) -> tuple[list[tuple[int, str, str]], list[tuple[int, str, str]]]:
    """承認の原稿を点検する。(誤り, 警告)。どちらも (行番号, 何が違うか, 例か言い換えの案) の並び。"""
    errors, warnings = [], []
    sections = parse_draft(draft)
    names = ' → '.join(f'## {ja}' for ja, _ in APPROVAL_SECTIONS)
    found = {}
    last = -1
    for section in sections[1:]:
        index = section_index(section['title'])
        if index is None:
            errors.append((section['line'], f'承認の原稿に無い節「{section["title"]}」',
                           f'決まった 7 つの節 ({names}) のどれかに入れる。段ごとの記録は載せない'))
            continue
        if index in found:
            errors.append((section['line'], f'節「{section_name(section["title"])}」が 2 つある', '1 つにまとめる'))
            continue
        if index < last:
            errors.append((section['line'], f'節「{section_name(section["title"])}」の順が違う',
                           f'節は {names} の順に置く'))
        found[index] = section
        last = max(last, index)
    body = draft_lines(draft)
    end_line = body[-1][0] if body else 1
    for index, (ja, en) in enumerate(APPROVAL_SECTIONS):
        if index not in found:
            later = [found[i]['line'] for i in found if i > index]
            place = f'## {APPROVAL_SECTIONS[index - 1][0]} のあと' if index else 'ページの最初'
            errors.append((min(later) if later else end_line, f'節「{ja}」({en}) が無い', f'## {ja} を{place}に置く'))

    def diagram_rows(section):
        return [(n, line) for n, line, lang in section['rows'] if lang in DIAGRAMS]

    for index in (SECTION_BEFORE, SECTION_AFTER):
        section = found.get(index)
        if section is None:
            continue
        body = [line for _, line in diagram_rows(section) if line.strip() and not FENCE.match(line)]
        if not body:
            errors.append((section['line'], f'節「{section_name(section["title"])}」に図が無いか、図が空',
                           '```flow で利用者から見た流れを描く (ファイルだけを変える run は ```tree)'))
        elif index == SECTION_AFTER and not any(CHANGE_MARK.match(line) for _, line in diagram_rows(section)):
            errors.append((section['line'], 'あとの流れの図に変わる所の印 (+ ・- ・~ ) が無い',
                           '+ 返事を照合する (PBI-003)'))
    section = found.get(SECTION_PBI)
    if section is not None and not any(h in PBI_HEADERS for h in table_headers(section['rows'])):
        errors.append((section['line'], 'PBI の表の見出しが「' + ' | '.join(PBI_HEADERS[0]) + '」でない',
                       '| ' + ' | '.join(PBI_HEADERS[0]) + ' |'))
    for section in sections:
        index = section_index(section['title']) if section['title'] is not None else None
        for number, line, lang in section['rows']:
            if lang == 'ask' and index != SECTION_DECISIONS:
                errors.append((number, '問い (ask) が決めることの節の外にある', '問いは ## 決めること にまとめる'))
                break
    section = found.get(SECTION_DECISIONS)
    if section is not None:
        asks = ask_blocks(section['rows'])
        plain = [line.strip().lstrip('-* ').rstrip('。.').lower() for _, line, lang in section['rows']
                 if lang is None and line.strip() and not FENCE.match(line)]
        if not asks and not any(text in NO_DECISIONS for text in plain):
            errors.append((section['line'], '決めることに問い (ask) も「なし」も無い',
                           '問いが無ければ「なし」の 1 行を書く'))
        for opener, body in asks:
            number, line = opener
            if line.strip() != ASK_OPEN:
                # 返事の照合 (draft_asks) が読むのは ```ask の囲みだけ。ほかの形の問いは答えを求められずに通ってしまう
                errors.append((number, f'問いの囲み「{line.strip()}」は返事の照合で読まれない', ASK_OPEN))
            rows = [(n, row) for n, row in body if row.strip()]
            if not rows or not any(OPTION.match(row) for _, row in rows[1:]):
                errors.append((number, '問い (ask) に問いの文か選択肢が無い', '1 行目に問いの文、次の行から * か - の選択肢'))
            for n, row in rows[1:]:
                note = row.split(' | ', 1)
                if OPTION.match(row) and (len(note) < 2 or not note[1].strip()):
                    errors.append((n, '選択肢に「| 選ぶと何が変わるか」の補足が無い',
                                   note[0].rstrip() + ' | 選ぶと何が変わるか'))
    for section in sections:
        for number, line, _ in section['rows']:
            for pattern, term, rewording in INTERNAL_TERMS:
                match = pattern.search(line)
                if match and not EXPLAINED.search(line[match.end():]):
                    warnings.append((number, term, rewording))
    errors.sort(key=lambda e: e[0])
    return errors, warnings


def report_lint(errors: list, warnings: list) -> None:
    for number, what, example in errors:
        print(f'✗ L{number} {what}\n  例: {example}', file=sys.stderr)
    for number, term, rewording in warnings:
        print(f'! L{number} {term} → {rewording}', file=sys.stderr)
    if errors:
        print(f'✗ {len(errors)} errors in the approval draft: fix each line and run again', file=sys.stderr)


def load_record(base: Path) -> dict:
    record = common.load_json(base / RECORD_NAME)
    if not valid_record(record):
        fail(f'{(base / RECORD_NAME).as_posix()}: missing or not a {SCHEMA} / {SCHEMA_2} record (run write first)')
    return record


def valid_work_entry(entry) -> bool:
    return (isinstance(entry, dict) and set(entry) == {'pbi', 'file', 'hash'}
            and all(isinstance(entry[k], str) for k in entry))


def valid_record(record) -> bool:
    """/1 (works なし) と /2 (works あり) を受ける。/1 は読むだけで、そのままのハッシュで照合できる。"""
    if not isinstance(record, dict) or record.get('schema') not in (SCHEMA, SCHEMA_2):
        return False
    keys = {'schema', 'design_id', 'revision', 'targets', 'concerns'}
    if record['schema'] == SCHEMA_2:
        keys = keys | {'works'}
        if not isinstance(record.get('works'), list) or not all(valid_work_entry(w) for w in record['works']):
            return False
    return (set(record) == keys
            and isinstance(record['design_id'], str)
            and isinstance(record['revision'], int) and not isinstance(record['revision'], bool)
            and record['revision'] >= 1
            and isinstance(record['targets'], list) and isinstance(record['concerns'], list)
            and all(isinstance(t, str) for t in record['targets'])
            and all(isinstance(c, str) for c in record['concerns']))


def load_slice(path: Path) -> tuple[dict | None, str | None]:
    """関心事の抜粋を読む。(slice, 誤りの説明)。/1 と /2 を受け、/1 の base_sha は None として扱う。"""
    data = common.load_json(path)
    if not isinstance(data, dict) or data.get('schema') not in SLICE_SCHEMAS:
        return None, f'{path.as_posix()}: not an {" / ".join(SLICE_SCHEMAS)} slice'
    base_sha = data.get('base_sha')
    if base_sha is not None and not isinstance(base_sha, str):
        return None, f'{path.as_posix()}: base_sha must be a string'
    return {**data, 'base_sha': base_sha}, None


def cmd_slice(args) -> int:
    if not SLICE_ID.fullmatch(args.id):
        fail(f'slice id {args.id!r} must match {SLICE_ID.pattern}')
    reads = []
    for item in args.read:
        match = READ.fullmatch(item)
        if not match:
            fail(f'--read {item!r}: use PATH:START-END or PATH:LINE')
        first = int(match.group(2))
        last = int(match.group(3) or first)
        if first < 1 or last < first:
            fail(f'--read {item!r}: lines must be 1 or more and START <= END')
        reads.append({'path': match.group(1).replace('\\', '/'), 'lines': [first, last]})
    path = Path(args.dir) / 'notes' / f'{args.id}.slice.json'
    common.write_json(path, {'schema': SLICE_SCHEMA, 'id': args.id, 'question': args.question,
                             'reads': reads, 'unknown': list(args.unknown), 'base_sha': git_base_sha()})
    print(path.as_posix())
    return EXIT_OK


def work_file_hash(path: Path) -> str:
    """作業票の中身のハッシュ: sha256(normalize(UTF-8 本文))。write と check-reply が同じ計算を使う。"""
    return sha256(normalize(path.read_bytes().decode('utf-8-sig', errors='replace')))


def work_problems(base: Path, record: dict) -> list[str]:
    """正本の works とディスクの作業票を照らす。問題の文言の並びを返す (無ければ空)。"""
    problems = []
    recorded = set()
    for entry in record.get('works', []):
        recorded.add(entry['file'])
        path = base / entry['file']
        try:
            current = work_file_hash(path) if path.is_file() else None
        except OSError:
            current = None
        if current is None:
            problems.append(f"work {entry['file']} missing since write")
        elif current != entry['hash']:
            problems.append(f"work {entry['file']} changed since write")
    if base.is_dir():
        for path in sorted(base.glob('PBI-*.work.json')):
            if path.is_file() and path.name not in recorded:
                problems.append(f'work {path.name} not in the record')
    return problems


def cmd_write(args) -> int:
    base = Path(args.dir)
    targets = list(dict.fromkeys(args.target))
    concerns = [c.replace('\\', '/') for c in dict.fromkeys(args.concern)]
    for concern in concerns:
        if concern.startswith('/') or '..' in Path(concern).parts:
            fail(f'--concern {concern!r}: give a path under {base.as_posix()}')
        if not (base / concern).is_file():
            fail(f'--concern {concern!r}: no such file under {base.as_posix()}')
    works = []
    for item in dict.fromkeys(p.replace('\\', '/') for p in args.work):
        if item.startswith('/') or re.match(r'^[A-Za-z]:', item) or '..' in Path(item).parts:
            fail(f'--work {item!r}: give a path under {base.as_posix()}')
        if not (base / item).is_file():
            fail(f'--work {item!r}: no such file under {base.as_posix()}')
        data = common.load_json(base / item)
        if not isinstance(data, dict) or data.get('schema') != WORK_SCHEMA or not isinstance(data.get('pbi'), str):
            fail(f'--work {item!r}: not an {WORK_SCHEMA} work file (run work first)')
        works.append({'pbi': data['pbi'], 'file': item, 'hash': work_file_hash(base / item)})
    errors = check_meta([('design_id', args.id)])
    if errors:
        fail('; '.join(errors))
    old = common.load_json(base / RECORD_NAME)
    old_valid = valid_record(old)
    # works を持つのは /2 だけ。--work も前の /2 も無いときは /1 のまま書き、既存の承認のハッシュを変えない
    versioned = bool(works) or (old_valid and old['schema'] == SCHEMA_2)
    revision = 1
    if old_valid and old['design_id'] == args.id:
        same = (old['targets'] == targets and old['concerns'] == concerns
                and old.get('works', []) == works)
        revision = old['revision'] if same else old['revision'] + 1
    record = {'schema': SCHEMA_2 if versioned else SCHEMA, 'design_id': args.id, 'revision': revision,
              'targets': targets, 'concerns': concerns}
    if versioned:
        record['works'] = works
    common.write_json(base / RECORD_NAME, record)
    print(f'{args.id} revision {revision} record_hash {record_hash(record)}')
    return EXIT_OK


def pbi_section(text: str, name: str) -> str:
    """PBI の Markdown の `## <name>` の節の本文 (前後の空白を除く)。節が無ければ空。"""
    match = re.search(rf'^## {re.escape(name)}[ \t]*\n(.*?)(?=^## |\Z)', text, re.S | re.M)
    return match.group(1).strip() if match else ''


def is_none_text(text: str) -> bool:
    return text.strip().strip('`').lower() in NONE_WORDS


def clean_path(raw: str) -> str:
    return raw.strip().rstrip(':：,、').replace('\\', '/').removeprefix('./')


def work_targets(section: str, problems: list[str]) -> list[str]:
    """Targets の空でない各行 (箇条書きの印の有無を問わない) の先頭のパス (バッククォートの中身、無ければ最初の語)。
    パスを取れない行と none の行は誤り (Targets では none を認めない)。出てきた順で重複を除く。"""
    found = []
    for line in section.splitlines():
        if not line.strip():
            continue
        match = WORK_TARGET_LINE.match(BULLET.sub('', line, count=1))
        path = clean_path(match.group(1) or match.group(2) or '') if match else ''
        if not path:
            problems.append(f'Targets: no path in line {line.strip()!r} (write each target as a "- `path`" line)')
            continue
        if path.lower() in NONE_WORDS:
            problems.append('Targets: none is not allowed (name at least one target path)')
            continue
        if path.startswith('/') or re.match(r'^[A-Za-z]:', path) or '..' in Path(path).parts:
            problems.append(f'Targets: {path!r} must be a path under the repository root')
            continue
        if path not in found:
            found.append(path)
    return found


def work_do_not_touch(section: str) -> list[str]:
    """Do not touch の各行 (箇条書きの印を除いた文)。none だけなら空。"""
    items = []
    for line in section.splitlines():
        text = re.sub(r'^\s*[-*]\s+', '', line).strip()
        if text and not is_none_text(text) and text not in items:
            items.append(text)
    return items


def work_refs(section: str) -> list[str]:
    """Refs の各行の先頭の id (「- mem-1: 要旨」の mem-1)。none は id ではない。"""
    ids = []
    for line in section.splitlines():
        match = WORK_REF_LINE.match(line)
        if match and match.group(1).lower() not in NONE_WORDS and match.group(1) not in ids:
            ids.append(match.group(1))
    return ids


def work_slices(section: str, problems: list[str]) -> list[str]:
    """Notes の `slice: <id>` の行の id。出てきた順で重複を除く。id の形が違う行は誤り。"""
    ids = []
    for line in section.splitlines():
        match = WORK_SLICE_LINE.match(line)
        if not match:
            continue
        value = match.group(1).strip().strip('`')
        if not value:
            problems.append('Notes: slice declaration has no id (write `slice: <id>`)')
        elif not SLICE_ID.fullmatch(value):
            problems.append(f'Notes: slice id {value!r} must match {SLICE_ID.pattern}')
        elif value not in ids:
            ids.append(value)
    return ids


def work_route(route) -> list[dict] | None:
    """route.json の route を {runner, model, effort} の並びにする。形が違えば None。"""
    if not isinstance(route, dict) or not isinstance(route.get('route'), list) or not route['route']:
        return None
    steps = []
    for step in route['route']:
        if not isinstance(step, dict) or not isinstance(step.get('runner'), str) \
                or not isinstance(step.get('model'), str):
            return None
        effort = step.get('effort')
        if effort is not None and not isinstance(effort, str):
            return None
        steps.append({'runner': step['runner'], 'model': step['model'], 'effort': effort})
    return steps


def run_git(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(['git', *args], capture_output=True, text=True, encoding='utf-8', errors='replace',
                          stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def git_base_sha() -> str:
    try:
        done = run_git(['rev-parse', 'HEAD'])
    except OSError as error:
        fail(f'git: {error}')
    if done.returncode != 0 or not done.stdout.strip():
        fail(f'git rev-parse HEAD failed: {done.stderr.strip() or "no commit yet"}')
    return done.stdout.strip()


def git_blobs(paths: list[str]) -> dict[str, str | None]:
    """作業ツリー上の各ファイルの blob のハッシュ (git hash-object)。ファイルが無ければ None。"""
    blobs: dict[str, str | None] = {path: None for path in paths}
    present = [path for path in paths if Path(path).is_file()]
    if present:
        try:
            done = run_git(['hash-object', '--', *present])
        except OSError as error:
            fail(f'git: {error}')
        hashes = done.stdout.split()
        if done.returncode != 0 or len(hashes) != len(present):
            fail(f'git hash-object failed: {done.stderr.strip() or "unexpected output"}')
        blobs.update(zip(present, hashes))
    return blobs


def cmd_work(args) -> int:
    pbi = args.pbi
    if not PBI_NAME.fullmatch(pbi):
        fail(f'{pbi!r}: use PBI-<n> (for example PBI-3)')
    base = Path(args.dir)
    md_path = base / f'{pbi}.md'
    route_path = base / f'{pbi}.route.json'
    problems: list[str] = []
    text = ''
    readable = True
    try:
        text = normalize(md_path.read_bytes().decode('utf-8-sig', errors='replace'))
    except OSError:
        readable = False
        problems.append(f'{md_path.as_posix()}: no such PBI file')
    goal = pbi_section(text, 'Goal')
    targets_text = pbi_section(text, 'Targets')
    do_not_touch_text = pbi_section(text, 'Do not touch')
    if readable and (not goal or is_none_text(BULLET.sub('', goal, count=1))):
        problems.append('## Goal is missing, empty or none')
    targets = work_targets(targets_text, problems)
    if readable and not targets_text:
        problems.append('## Targets is missing or empty (write each target as a "- `path`" line)')
    for path in targets:
        if Path(path).exists() and not Path(path).is_file():
            problems.append(f'Targets: {path!r} is not a file')
    do_not_touch = work_do_not_touch(do_not_touch_text)
    if readable and not do_not_touch_text:
        problems.append('## Do not touch is missing or empty')
    match = WORK_DEPENDS.search(text)
    depends_on = [p for p in dict.fromkeys(PBI_REF.findall(match.group(1))) if p != pbi] if match else []
    if not route_path.is_file():
        problems.append(f'{route_path.as_posix()}: no such route file')
        route = None
    else:
        route = work_route(common.load_json(route_path))
        if route is None:
            problems.append(f'{route_path.as_posix()}: no valid "route" list of runner and model')
    slices = work_slices(pbi_section(text, 'Notes'), problems)
    for slice_id in slices:
        slice_path = base / 'notes' / f'{slice_id}.slice.json'
        if not slice_path.is_file():
            problems.append(f'Notes: slice {slice_id!r}: no such file {slice_path.as_posix()}')
        else:
            _, bad = load_slice(slice_path)
            if bad:
                problems.append(f'Notes: slice {slice_id!r}: {bad}')
    if problems:
        for problem in problems:
            print(f'✗ {pbi}: {problem}', file=sys.stderr)
        print(f'✗ {len(problems)} problem(s): the work file was not written', file=sys.stderr)
        return EXIT_DRAFT
    work = {'schema': WORK_SCHEMA, 'pbi': pbi, 'goal': goal, 'targets': targets, 'do_not_touch': do_not_touch,
            'depends_on': depends_on, 'base_sha': git_base_sha(), 'target_blobs': git_blobs(targets),
            'route': route, 'slices': slices, 'refs': work_refs(pbi_section(text, 'Refs'))}
    path = base / f'{pbi}.work.json'
    common.write_json(path, work)
    print(path.as_posix())
    return EXIT_OK


def load_work(path: Path) -> dict:
    """作業票を読み、形が違えば fail (終了コード 2)。stale が使う項目だけを確かめる。"""
    work = common.load_json(path)
    if not isinstance(work, dict) or work.get('schema') != WORK_SCHEMA:
        fail(f'{path.as_posix()}: missing or not a {WORK_SCHEMA} work file (run work first)')
    blobs = work.get('target_blobs')
    if not isinstance(blobs, dict) or not all(isinstance(k, str) and (v is None or isinstance(v, str))
                                               for k, v in blobs.items()):
        fail(f'{path.as_posix()}: target_blobs must be an object of path to blob hash or null')
    for key in ('targets', 'depends_on'):
        if not isinstance(work.get(key), list) or not all(isinstance(v, str) for v in work[key]):
            fail(f'{path.as_posix()}: {key} must be a list of strings')
    for name in [*blobs, *work['targets']]:
        norm = name.replace('\\', '/')
        if not norm or norm.startswith('/') or re.match(r'^[A-Za-z]:', norm) or '..' in Path(norm).parts:
            fail(f'{path.as_posix()}: {name!r} must be a path under the repository root')
    for dep in work['depends_on']:
        if not PBI_NAME.fullmatch(dep):
            fail(f'{path.as_posix()}: depends_on {dep!r} must be PBI-<n>')
        if dep == work.get('pbi'):
            fail(f'{path.as_posix()}: depends_on names the PBI itself')
    norm = lambda name: name.replace('\\', '/').removeprefix('./')  # noqa: E731
    if {norm(k) for k in blobs} != {norm(t) for t in work['targets']}:
        fail(f'{path.as_posix()}: target_blobs must have exactly one entry per target (run work again)')
    return work


def check_tickets_against_record(base: Path, names: list[str]) -> None:
    """承認の正本 (/2) があれば、名指した作業票が記録のときのままかを確かめ、違えば mismatch (終了コード 3) で止める。
    正本が無い (confirm off) か /1 のときは確かめるものが無い。"""
    record = common.load_json(base / RECORD_NAME)
    if not valid_record(record) or record['schema'] != SCHEMA_2:
        return
    recorded = {entry['file']: entry['hash'] for entry in record['works']}
    problems = []
    for name in names:
        path = base / name
        if name not in recorded:
            problems.append(f'work {name} not in the record')
            continue
        try:
            current = work_file_hash(path) if path.is_file() else None
        except OSError:
            current = None
        if current != recorded[name]:
            problems.append(f'work {name} changed since write')
    if problems:
        for problem in problems:
            print(f'✗ {problem}: do not start it, return it to the designer', file=sys.stderr)
        sys.exit(EXIT_MISMATCH)


def cmd_stale(args) -> int:
    pbi = args.pbi
    if not PBI_NAME.fullmatch(pbi):
        fail(f'{pbi!r}: use PBI-<n> (for example PBI-3)')
    base = Path(args.dir)
    work = load_work(base / f'{pbi}.work.json')
    if work.get('pbi') != pbi:
        fail(f'{(base / f"{pbi}.work.json").as_posix()}: pbi is {work.get("pbi")!r}, not {pbi!r}')
    # 承認したときの作業票のままか (自分と依存の作業票)。古さを見る前に確かめる
    check_tickets_against_record(base, [f'{name}.work.json' for name in [pbi, *work['depends_on']]])
    # 先に終わった依存 PBI が変えるファイルは、古さに数えない
    excluded = set()
    for dep in work['depends_on']:
        excluded.update(t.replace('\\', '/').removeprefix('./') for t in load_work(base / f'{dep}.work.json')['targets'])
    recorded = {key.replace('\\', '/').removeprefix('./'): value for key, value in work['target_blobs'].items()}
    now = git_blobs(list(recorded))
    changes = []
    for path, then in recorded.items():
        current = now[path]
        if current == then or path in excluded:
            continue
        kind = 'appeared' if then is None else 'removed' if current is None else 'changed'
        changes.append((path, kind))
    if changes:
        for path, kind in changes:
            print(f'stale\t{kind}\t{path}')
        print(f'✗ {pbi}: {len(changes)} target(s) differ from the design: do not start it, return it to the designer',
              file=sys.stderr)
        return EXIT_STALE
    print(f'fresh\t{pbi}\t{len(recorded)} target(s) match' + (f', {len(excluded & set(recorded))} skipped (depends_on)'
                                                              if excluded & set(recorded) else ''))
    return EXIT_OK


def read_text(path: str) -> str:
    """UTF-8 (BOM 可) のバイト列のまま読む。改行は normalize がそろえる (OS の改行の変換に任せない)。"""
    try:
        return Path(path).read_bytes().decode('utf-8-sig', errors='replace')
    except OSError as error:
        fail(f'{path}: {error}')


def read_stdin() -> str:
    return sys.stdin.buffer.read().decode('utf-8-sig', errors='replace')


def cmd_lint(args) -> int:
    errors, warnings = lint_draft(read_text(args.draft))
    report_lint(errors, warnings)
    if errors:
        return EXIT_DRAFT
    print(f'ok	{len(warnings)} warning(s)')
    return EXIT_OK


def cmd_markdown(args) -> int:
    record = load_record(Path(args.dir))
    draft = read_text(args.draft)
    # 警告は approve_meta が返さないので、lint_draft を別に呼んで出す。照合情報の誤り (check_meta) で
    # 止めるときも、今と同じく警告を先に出す。
    _, warnings = lint_draft(draft)
    try:
        text = approve_meta(draft, record)
    except DraftError as error:
        report_lint(error.errors, error.warnings)
        return EXIT_DRAFT
    except ValueError as error:
        report_lint([], warnings)
        fail(str(error))
    report_lint([], warnings)
    # 改行は \n のまま出す (Windows の標準出力の \r\n への変換を通さない)
    sys.stdout.flush()
    sys.stdout.buffer.write((text if text.endswith('\n') else text + '\n').encode('utf-8'))
    sys.stdout.buffer.flush()
    return EXIT_OK


def cmd_check_reply(args) -> int:
    base = Path(args.dir)
    record = load_record(base)
    reply = read_text(args.reply) if args.reply else read_stdin()
    draft = read_text(args.draft) if args.draft else None
    body, raw = split_reply(reply)
    if raw is None:
        print('unbound\tno am-reply-meta fence at the end of the reply; not tied to any revision')
        return EXIT_UNBOUND
    try:
        meta = json.loads(raw)
    except ValueError:
        print('unbound\tthe am-reply-meta fence is not JSON; not tied to any revision')
        return EXIT_UNBOUND
    problems = work_problems(base, record) + compare(meta, record, draft)
    if problems:
        print('mismatch\t' + '; '.join(problems) + '\tstop: not approved')
        return EXIT_MISMATCH
    found = decisions(body)
    for d in found:
        state = 'answered' if d['answered'] else 'unanswered'
        print(f"ask\t{d['number']}\t{state}\t{d['answer'] or '-'}\t{d['question']}")
    open_asks = [d for d in found if not d['answered']]
    for ask in draft_asks(draft or ''):
        if not any(same_question(ask['question'], d['question']) for d in found):
            print(f"ask\t-\tmissing\t-\t{ask['question']}")
            open_asks.append(ask)
    if open_asks:
        print(f"unanswered\t{len(open_asks)} ask(s) without an answer\tnot approved")
        return EXIT_UNANSWERED
    row = {'recorded_at': common.now().isoformat(timespec='seconds'), 'design_id': record['design_id'],
           'revision': record['revision'], 'record_hash': record_hash(record),
           'draft_checked': draft is not None,
           'answers': [{'question': d['question'], 'answer': d['answer']} for d in found]}
    if not args.dry_run:
        base.mkdir(parents=True, exist_ok=True)
        with open(base / APPROVALS_NAME, 'a', encoding='utf-8', newline='\n') as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
        # 承認を保存したときだけ、選ばれた答えをイベントに残す。conductor の answer は自分で出すので
        # ATELIER_EVENTS_QUIET=1 で二重記録を止める
        if events is not None and os.environ.get('ATELIER_EVENTS_QUIET') != '1':
            events.emit(base, 'question.answered',
                        {'kind': 'approve', 'topic': 'design-approval', 'choice': reply_choice(found),
                         'wait_s': None, 'revise': False})
    saved = 'dry-run, not saved' if args.dry_run else f'saved to {(base / APPROVALS_NAME).as_posix()}'
    draft_note = '' if draft is not None else ' (draft_hash not checked: no --draft)'
    print(f"match\t{record['design_id']} revision {record['revision']}{draft_note}\t{saved}")
    return EXIT_OK


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='command', required=True)
    sl = sub.add_parser('slice')
    sl.add_argument('id')
    sl.add_argument('--question', required=True)
    sl.add_argument('--read', action='append', default=[])
    sl.add_argument('--unknown', action='append', default=[])
    wr = sub.add_parser('write')
    wr.add_argument('--id', required=True)
    wr.add_argument('--target', action='append', required=True)
    wr.add_argument('--concern', action='append', default=[])
    wr.add_argument('--work', action='append', default=[])
    md = sub.add_parser('markdown')
    md.add_argument('--draft', required=True)
    li = sub.add_parser('lint')
    li.add_argument('--draft', required=True)
    ck = sub.add_parser('check-reply')
    ck.add_argument('--reply')
    ck.add_argument('--draft')
    ck.add_argument('--dry-run', action='store_true')
    wk = sub.add_parser('work')
    wk.add_argument('pbi')
    st = sub.add_parser('stale')
    st.add_argument('pbi')
    for parser in (sl, wr, md, li, ck, wk, st):
        parser.add_argument('--dir', default='docs/pbi')
    args = ap.parse_args(argv)
    common.utf8_stdout()
    handlers = {'slice': cmd_slice, 'write': cmd_write, 'markdown': cmd_markdown, 'lint': cmd_lint,
                'check-reply': cmd_check_reply, 'work': cmd_work, 'stale': cmd_stale}
    return handlers[args.command](args)


if __name__ == '__main__':
    sys.exit(main())
