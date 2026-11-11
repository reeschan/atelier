"""設計の正本 (docs/pbi/design-record.json) と関心事の抜粋を書き、承認用の Markdown を出し、貼られた返信を正本と照合する
(appendix/adapters.md、`## Confirm`)。HTML は作らない。am.mjs も呼ばない。

使い方 (リポジトリのルートで):
  python <このディレクトリ>/design_record.py slice <id> --question Q [--read PATH:START-END ...] [--unknown U ...] [--dir docs/pbi]
  python <このディレクトリ>/design_record.py write --id ID --target PBI-<n> [--target ...] [--concern notes/<id>.slice.json ...] [--dir docs/pbi]
  python <このディレクトリ>/design_record.py lint --draft FILE
  python <このディレクトリ>/design_record.py markdown --draft FILE [--dir docs/pbi]
  python <このディレクトリ>/design_record.py check-reply [--reply FILE] [--draft FILE] [--dry-run] [--dir docs/pbi]

slice   docs/pbi/notes/<id>.slice.json に、問い・読んだ path と行・unknown だけを書く。ファイルの本文は入れない。
write   正本を書く。中身は schema・design_id・revision・targets・concerns だけ (schema 1)。同じ id の正本があり、
        targets か concerns が変わったときだけ revision を 1 上げる。別の id なら 1 から。concerns のファイルが無ければ 2。
lint    承認の原稿 (appendix/approve.md) を点検する。7 つの節の名前と順・前後の図・あとの図の印・PBI の表の見出し・
        ask の置き場所と選択肢の補足を見る。誤りは標準エラーに `✗ L<行>` と `例:` の行で全部出し、終了コード 6。
        説明の無い内部の言葉は `! L<行> <言葉> → <言い換えの案>` の警告で、終了コードは 0 のまま。正本は読まない。
markdown 先に lint と同じ点検をし、誤りがあれば何も出さずに 6 で止める。通れば、設計の下書き (Markdown) に返信の封筒 reply-meta-<name>: <value> (design_id・revision・record_hash・draft_hash) を
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
import re
import sys
from pathlib import Path

import common

SCHEMA = 'atelier.design-record/1'
RECORD_NAME = 'design-record.json'
APPROVALS_NAME = 'design-approvals.jsonl'
SLICE_SCHEMA = 'atelier.design-slice/1'

# renderer (answer-me-with-any の src/parse.js) が受ける封筒の名前と値の形・件数
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
READ = re.compile(r'^(.+):(\d+)(?:-(\d+))?$')

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_MISMATCH = 3
EXIT_UNBOUND = 4
EXIT_UNANSWERED = 5
EXIT_DRAFT = 6


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
    """封筒の名前・値・件数が renderer の規則に合うか。合わないものの説明 (英語) を返す。"""
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


def with_envelope(draft: str, entries: list) -> str:
    """下書きの frontmatter に封筒の行を足す (前からある reply-meta- の行は置き換える)。frontmatter が無ければ作る。"""
    body = strip_meta_lines(draft)
    meta = [f'{META_PREFIX}{name}: {value}' for name, value in entries]
    lines = body.split('\n')
    if lines and lines[0].strip() == '---':
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == '---'), None)
        if end is not None:
            return '\n'.join([*lines[:end], *meta, *lines[end:]])
    return '\n'.join(['---', *meta, '---', *lines])


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


def compare(meta: dict, record: dict, draft: str | None) -> list[str]:
    """封筒と正本の食い違い。空なら一致。revision が正本より古いときはそう書く。"""
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
        fail(f'{(base / RECORD_NAME).as_posix()}: missing or not a {SCHEMA} record (run write first)')
    return record


def valid_record(record) -> bool:
    return (isinstance(record, dict) and record.get('schema') == SCHEMA
            and set(record) == {'schema', 'design_id', 'revision', 'targets', 'concerns'}
            and isinstance(record['design_id'], str)
            and isinstance(record['revision'], int) and not isinstance(record['revision'], bool)
            and record['revision'] >= 1
            and all(isinstance(t, str) for t in record['targets'])
            and all(isinstance(c, str) for c in record['concerns']))


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
                             'reads': reads, 'unknown': list(args.unknown)})
    print(path.as_posix())
    return EXIT_OK


def cmd_write(args) -> int:
    base = Path(args.dir)
    targets = list(dict.fromkeys(args.target))
    concerns = [c.replace('\\', '/') for c in dict.fromkeys(args.concern)]
    for concern in concerns:
        if concern.startswith('/') or '..' in Path(concern).parts:
            fail(f'--concern {concern!r}: give a path under {base.as_posix()}')
        if not (base / concern).is_file():
            fail(f'--concern {concern!r}: no such file under {base.as_posix()}')
    errors = check_meta([('design_id', args.id)])
    if errors:
        fail('; '.join(errors))
    old = common.load_json(base / RECORD_NAME)
    revision = 1
    if valid_record(old) and old['design_id'] == args.id:
        same = old['targets'] == targets and old['concerns'] == concerns
        revision = old['revision'] if same else old['revision'] + 1
    record = {'schema': SCHEMA, 'design_id': args.id, 'revision': revision,
              'targets': targets, 'concerns': concerns}
    common.write_json(base / RECORD_NAME, record)
    print(f'{args.id} revision {revision} record_hash {record_hash(record)}')
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
    errors, warnings = lint_draft(draft)
    report_lint(errors, warnings)
    if errors:
        return EXIT_DRAFT
    entries = meta_entries(record, draft)
    errors = check_meta(entries)
    if errors:
        fail('; '.join(errors))
    text = with_envelope(draft, entries)
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
    problems = compare(meta, record, draft)
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
    md = sub.add_parser('markdown')
    md.add_argument('--draft', required=True)
    li = sub.add_parser('lint')
    li.add_argument('--draft', required=True)
    ck = sub.add_parser('check-reply')
    ck.add_argument('--reply')
    ck.add_argument('--draft')
    ck.add_argument('--dry-run', action='store_true')
    for parser in (sl, wr, md, li, ck):
        parser.add_argument('--dir', default='docs/pbi')
    args = ap.parse_args(argv)
    common.utf8_stdout()
    handlers = {'slice': cmd_slice, 'write': cmd_write, 'markdown': cmd_markdown, 'lint': cmd_lint,
                'check-reply': cmd_check_reply}
    return handlers[args.command](args)


if __name__ == '__main__':
    sys.exit(main())
