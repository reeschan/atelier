'''指示文の中の相対パスの参照が実在することを確かめる (PBI-002)。

対象はスキルのディレクトリ直下の *.md と appendix/*.md と agents/*.md。
その中のバッククォートで囲んだ語のうち、スキルの中のファイルを指す相対パス
(拡張子 .md .py .json .mjs .html) を取り出し、スキルのディレクトリからの
相対として解決できることを確かめる。切れた参照があれば、指示文のファイル名・
行番号・切れたパスを並べて失敗する。

対象外:
- 作業リポジトリ側のパス (docs/...)
- ホームのパス (~/...)
- プレースホルダを含むパス (<...> や * を含むもの)
- URL
- 拡張子だけの語 (.last.md など) と、拡張子を持たない語
'''
import re
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 対象の指示文 (スキルのディレクトリからの相対パターン)
DOC_PATTERNS = ('*.md', 'appendix/*.md', 'agents/*.md')

# 実在を確かめる拡張子
SUFFIXES = ('.md', '.py', '.json', '.mjs', '.html')

# ファイル名だけの参照を探すディレクトリ (スキルのディレクトリからの相対。'' は直下)
SEARCH_DIRS = ('', 'appendix', 'studio', 'jev', 'rtk', 'examples')

# 取り除く前置き (この順に試す)
PREFIXES = ('<このディレクトリ>/', '<スキル>/')

BACKTICK = re.compile(r'`([^`\n]+)`')

# スキルの外 (home か作業リポジトリの docs/pbi/) に置くファイルの名前。
# 指示文がファイル名だけで指すことがあるので、実在の確認から外す。
# ディレクトリを含む参照 (appendix/agents.json など) は、スキルの中のファイルを
# 指すので、この集合では外さない。
OUTSIDE_SKILL_NAMES = {
    # docs/pbi/ の設定
    'jev.config.json',
    'routing.json',
    'routing.prev.json',
    # home の設定
    'overrides.json',
    'web.json',
    # 今後、指示文がファイル名だけで指す名前
    'agents.json',
    'state.json',
    'STATUS.md',
    'HANDOFF.md',
    'jev-log.jsonl',
    'runs.jsonl',
    # home と作業の記録 (アダプター・通知)
    'notices.json',
    'adapters.json',
}


def documents():
    '''対象の指示文を、重複なく名前順で返す。'''
    found = []
    seen = set()
    for pattern in DOC_PATTERNS:
        for path in sorted(HERE.glob(pattern)):
            if path.is_file() and path not in seen:
                seen.add(path)
                found.append(path)
    return found


def normalize(token):
    '''バッククォートの中の語を、スキルからの相対パスに直す。

    スキルの中のファイルを指さない語 (対象外) は None を返す。
    '''
    token = token.strip().strip('"\'()[]')
    token = token.rstrip('.,;:')
    if not token:
        return None
    # URL
    if '://' in token or token.lower().startswith(('http:', 'https:')):
        return None
    # ホームのパス・絶対パス
    if token.startswith(('~', '/')):
        return None
    if re.match(r'^[A-Za-z]:[\\/]', token):
        return None
    # <このディレクトリ>/ と <スキル>/ の前置きを取る
    for prefix in PREFIXES:
        if token.startswith(prefix):
            token = token[len(prefix):]
            break
    if token.startswith('./'):
        token = token[2:]
    token = token.replace('\\', '/')
    # プレースホルダ (<報告名>・PBI-<連番>・* など)
    if any(ch in token for ch in '<>*'):
        return None
    # 作業リポジトリ側のパス
    if token.startswith('docs/'):
        return None
    # 拡張子だけの語 (.last.md・.json など) はパスではない
    if token.startswith('.'):
        return None
    if token.endswith('/'):
        return None
    if not token.lower().endswith(SUFFIXES):
        return None
    return token


def candidates(line):
    '''1 行から、実在を確かめる相対パスの候補を返す。

    バッククォートの中に空白があればコマンドとみなし、語ごとに判定する。
    '''
    result = []
    for match in BACKTICK.finditer(line):
        span = match.group(1)
        tokens = span.split() if any(ch.isspace() for ch in span) else [span]
        for token in tokens:
            path = normalize(token)
            if path is not None:
                result.append((path, token))
    return result


def resolve(path):
    '''スキルの中の実在するファイルを指すかを返す。

    ディレクトリを含む参照は、そのパスだけを見る。ファイル名だけの参照は、
    SEARCH_DIRS のどこかにあれば実在とみなす。
    '''
    if '/' in path:
        return (HERE / path).is_file()
    for directory in SEARCH_DIRS:
        base = HERE / directory if directory else HERE
        if (base / path).is_file():
            return True
    return False


def is_checked(path):
    '''実在を確かめる参照かを返す。

    スキルの外 (home か作業リポジトリの docs/pbi/) に置くファイルの名前は、
    指示文がファイル名だけで指すことがあるので確かめない。ディレクトリを含む
    参照は、スキルの中のファイルを指すので、今どおり確かめる。
    '''
    return '/' in path or path not in OUTSIDE_SKILL_NAMES


def missing_refs(refs):
    '''実在しない参照を返す。スキルの外のファイル名だけの参照は除く。'''
    return [ref for ref in refs if is_checked(ref[2]) and not resolve(ref[2])]


def collect():
    '''(指示文の名前, 行番号, パス) の一覧を返す。'''
    refs = []
    for document in documents():
        name = document.relative_to(HERE).as_posix()
        text = document.read_text(encoding='utf-8')
        for lineno, line in enumerate(text.splitlines(), 1):
            for path, _token in candidates(line):
                refs.append((name, lineno, path))
    return refs


def format_reference(ref):
    '''失敗メッセージの 1 行。指示文のファイル名・行番号・切れたパスを出す。'''
    name, lineno, path = ref
    return f'{name}:{lineno}: {path}'


ADAPTERS = HERE / 'appendix' / 'adapters.md'


def known_callee_rows():
    '''付録の「## Known callees」の表を (呼び出し名, Link 列) の一覧で返す。'''
    lines = ADAPTERS.read_text(encoding='utf-8').splitlines()
    start = lines.index('## Known callees')
    header = None
    rows = []
    for line in lines[start + 1:]:
        if line.startswith('## '):
            break
        if not line.startswith('|'):
            continue
        cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
        if header is None:
            header = cells
            continue
        if set(line) <= set('|-: '):
            continue
        rows.append((cells[0], cells[header.index('Link')]))
    return rows


class ReferencesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.refs = collect()

    def test_documents_are_found(self):
        names = {document.relative_to(HERE).as_posix() for document in documents()}
        self.assertIn('SKILL.md', names)
        self.assertIn('appendix/rework.md', names)
        self.assertIn('agents/pbi-worker.md', names)

    def test_references_exist(self):
        '''切れた参照がないこと (スキル外に置くファイル名だけの参照は除く)。'''
        missing = missing_refs(self.refs)
        self.assertEqual(
            [], missing,
            '\n'.join(format_reference(ref) for ref in missing),
        )

    def test_name_only_outside_skill_names_are_not_checked(self):
        '''名前だけのスキル外のファイル名は通し、ディレクトリつきは実在を確かめる。'''
        refs = [
            ('SKILL.md', 1, 'agents.json'),
            ('SKILL.md', 2, 'appendix/no-such.json'),
        ]
        self.assertEqual([refs[1]], missing_refs(refs))

    def test_checked_reference_count(self):
        '''参照を実際に確かめていること。'''
        self.assertGreaterEqual(len(self.refs), 10)

    def test_failure_message_has_name_line_and_path(self):
        line = format_reference(('appendix/rework.md', 42, 'appendix/missing.md'))
        self.assertIn('appendix/rework.md', line)
        self.assertIn('42', line)
        self.assertIn('appendix/missing.md', line)

    def test_known_callee_rows_have_link_or_built_in(self):
        '''既知の名前の表の各行に https:// のリンクか built-in があること (ネットワークには出ない)。'''
        rows = known_callee_rows()
        self.assertGreaterEqual(len(rows), 1)
        for callee, link in rows:
            link = link.strip('`')
            self.assertTrue(
                link.startswith('https://') or link == 'built-in',
                f'{callee}: {link}',
            )


if __name__ == '__main__':
    unittest.main()
