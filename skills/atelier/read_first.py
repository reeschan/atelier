'''設計者が出番の最初に 1 回だけ読むファイル (Read first) を、read_first.json の表から組む。

  python read_first.py <episode> [--entry <entry>] --out <path> [--skill-dir <dir>]

表は { "limits": {episode: 文字数の上限}, "episodes": {episode: {entry: [並び]}} } の形。
limits は、その episode のどの入口のまとめたファイルも超えない文字数。今の中身に 1 割ほどの余裕を足した値にする
(文書を少し直しても落ちず、読む量が大きく増えたら test_read_first.py で気づけるように)。
並びの 1 つは次のどれか。
- '<file>': スキルのファイルを丸ごと。
- '<file>#<見出し>': そのファイルの前置き (最初の見出しの前と、H1 の題のあと最初の下の見出しの前まで) と、
  その見出しの節 (次の同じか上の階層の見出しの前まで)。前置きは 1 つのファイルにつき 1 回だけ付ける。
- 'docs/...': 作業ルートのファイル。中身は写さず、読むファイルとして頭に名前を並べる
  (例えば、run 全体の行き止まりの docs/pbi/dead-ends.md)。
各部分の頭には、元のファイルと見出しの 1 行 (`<!-- from <file>, <見出し> -->`) を付ける。

出力は 'read-first: <path>' の 1 行。終了コード: 0 書いた / 2 表に無い episode、無いファイルや見出し。
'''
import argparse
import json
import re
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
TABLE_NAME = 'read_first.json'
HEADING = re.compile(r'^(#{1,6}) (.+?)\s*$')
FENCE = re.compile(r'^\s*(```|~~~)')
WORK_ROOT_PREFIX = 'docs/'


def entry_of(continue_from: bool, answer_kind, answer_topic, escalated: bool, lead) -> str:
    '''依頼文の行から、表の入口を決める。

    複数が当たるときは次の順で最初のものを使う。
    1. escalated: 担当が設計者に渡した点を決める出番 ('escalated')。
    2. lead: 担当が段を回す出番 ('lead:<test|fix>')。lead は依頼文の Lead: の値で、最初の語を取る。
       担当が出した問いの答えや引き継ぎも、段の手順はそのままなので lead を先にする。
    3. answer_kind: 問いの答えから続ける出番 ('answer:<kind>'、topic があれば 'answer:<kind>:<topic>')。
    4. continue_from: 状態の板から続ける出番 ('continue')。
    5. どれでもない: 最初の起動 ('first')。
    '''
    if escalated:
        return 'escalated'
    if lead:
        return f'lead:{str(lead).split()[0]}'
    if answer_kind:
        return f'answer:{answer_kind}:{answer_topic}' if answer_topic else f'answer:{answer_kind}'
    if continue_from:
        return 'continue'
    return 'first'


def load_table(skill_dir: Path = SKILL_DIR) -> dict:
    return json.loads((Path(skill_dir) / TABLE_NAME).read_text(encoding='utf-8'))


def items_of(table: dict, episode: str, entry: str) -> list:
    '''(episode, entry) の並び。無い entry は、topic 付きの答えなら topic なしの答え、それも無ければ first に戻る。'''
    entries = table['episodes'].get(episode)
    if entries is None:
        raise ValueError(f'no episode in {TABLE_NAME}: {episode}')
    tried = [entry]
    if entry.startswith('answer:') and entry.count(':') >= 2:
        tried.append(entry.rsplit(':', 1)[0])
    tried.append('first')
    for name in tried:
        if name in entries:
            return entries[name]
    raise ValueError(f'no entry in {TABLE_NAME}: {episode} {entry}')


def headings(lines: list) -> list:
    '''(行の番号, 階層, 名前) の並び。``` か ~~~ の囲みの中の行は見出しに数えない。'''
    found, fenced = [], False
    for index, line in enumerate(lines):
        if FENCE.match(line):
            fenced = not fenced
            continue
        match = None if fenced else HEADING.match(line)
        if match:
            found.append((index, len(match.group(1)), match.group(2)))
    return found


def preface(lines: list, marks: list) -> list:
    '''前置き: 最初の見出しの前の行と、H1 の題で始まるなら、その題から次の見出しの前までの行。'''
    if not marks:
        return lines
    first_index, first_level, _name = marks[0]
    if first_level == 1 and len(marks) > 1:
        return lines[:marks[1][0]]
    if first_level == 1:
        return lines
    return lines[:first_index]


def section(lines: list, marks: list, name: str, label: str) -> list:
    '''(見出しの節, 階層)。節はその見出しの行から、次の同じか上の階層の見出しの前まで。'''
    for position, (index, level, heading) in enumerate(marks):
        if heading != name:
            continue
        end = len(lines)
        for later_index, later_level, _later in marks[position + 1:]:
            if later_level <= level:
                end = later_index
                break
        return lines[index:end], level
    raise ValueError(f'no heading: {label}')


def trimmed(lines: list) -> str:
    return '\n'.join(lines).strip('\n')


def compose(table: dict, episode: str, entry: str, skill_dir: Path) -> str:
    '''表の並びをつなげた本文を返す。'''
    items = items_of(table, episode, entry)
    work_files = [item for item in items if item.startswith(WORK_ROOT_PREFIX)]
    head = [f'# Read first: {episode} ({entry})', '',
            'The conductor put this file together from `read_first.json` for this episode. Read it once, '
            'in order. Each part starts with the file and the heading it was taken from; a heading a part '
            'names without a file is in that part\'s file.']
    if work_files:
        head += ['', 'Also read these files of the work root, when they exist: '
                 + ', '.join(f'`{name}`' for name in work_files) + '.']
    parts = ['\n'.join(head)]
    prefaced = set()
    for item in items:
        if item.startswith(WORK_ROOT_PREFIX):
            continue
        name, _, heading = item.partition('#')
        path = Path(skill_dir) / name
        if not path.is_file():
            raise ValueError(f'no file: {item}')
        lines = path.read_text(encoding='utf-8').splitlines()
        if not heading:
            prefaced.add(name)
            parts.append(f'<!-- from {name} -->\n\n{trimmed(lines)}')
            continue
        marks = headings(lines)
        body, level = section(lines, marks, heading, item)
        text = trimmed(body)
        if name not in prefaced:
            prefaced.add(name)
            text = f'{trimmed(preface(lines, marks))}\n\n{text}'
        parts.append(f'<!-- from {name}, {"#" * level} {heading} -->\n\n{text}')
    return '\n\n'.join(parts) + '\n'


def build(episode: str, entry: str, out: Path, skill_dir: Path = SKILL_DIR) -> Path:
    '''read_first.json の (episode, entry) の並びをつなげて out に書き、out を返す。'''
    skill_dir = Path(skill_dir)
    text = compose(load_table(skill_dir), episode, entry, skill_dir)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding='utf-8')
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='設計者が出番の最初に読むファイルを組む')
    ap.add_argument('episode')
    ap.add_argument('--entry', default='first')
    ap.add_argument('--out', required=True)
    ap.add_argument('--skill-dir', default=str(SKILL_DIR))
    args = ap.parse_args(argv)
    try:
        out = build(args.episode, args.entry, Path(args.out), Path(args.skill_dir))
    except ValueError as error:
        print(f'error: {error}')
        return 2
    print(f'read-first: {out.as_posix()}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
