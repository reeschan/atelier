'''指示文の中の相対パスの参照が実在することと、本文の大きさの上限を確かめる (S4)。

対象はスキルのディレクトリ直下の *.md と appendix/*.md と agents/*.md と prompts/*.md。
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
import json
import re
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 対象の指示文 (スキルのディレクトリからの相対パターン)
DOC_PATTERNS = ('*.md', 'appendix/*.md', 'agents/*.md', 'prompts/*.md')

# 実在を確かめる拡張子
SUFFIXES = ('.md', '.py', '.json', '.mjs', '.html')

# ファイル名だけの参照を探すディレクトリ (スキルのディレクトリからの相対。'' は直下)
SEARCH_DIRS = ('', 'appendix', 'prompts', 'studio', 'jev', 'rtk', 'examples')

# 取り除く前置き (この順に試す)
PREFIXES = ('<skill>/', '<このディレクトリ>/', '<スキル>/')

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
    'workflow.json',
    'route.json',
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


SKILL = HERE / 'SKILL.md'
COMPONENTS = HERE / 'components'
# 他の人のスキルを呼ぶ部品の例 (同梱しない。使う人が home の components/ に写す)
EXAMPLE_COMPONENTS = HERE / 'examples' / 'components'

# 毎ターン読む本文の上限 (generic-reorg.md の段 3)。compaction のあとに付け直されるのは先頭の約 5,000 トークン
SKILL_MAX_LINES = 120
SKILL_MAX_TOKENS = 5000
# トークン数の見積もり: UTF-8 のバイト数を 4 で割る (英語の文ではやや多めに出る)
BYTES_PER_TOKEN = 4


def external_components():
    '''Skill ツールかプラグインで呼ぶ部品を (id, link) の一覧で返す。'''
    rows = []
    for path in sorted(COMPONENTS.glob('*.json')) + sorted(EXAMPLE_COMPONENTS.glob('*.json')):
        data = json.loads(path.read_text(encoding='utf-8'))
        if data['invoke']['kind'] in ('skill', 'read-skill'):
            rows.append((data['id'], data.get('link', '')))
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

    def test_external_components_have_link_or_built_in(self):
        '''スキルを呼ぶ部品に https:// のリンクか built-in があること (ネットワークには出ない)。'''
        rows = external_components()
        self.assertGreaterEqual(len(rows), 1)
        for cid, link in rows:
            self.assertTrue(link.startswith('https://') or link == 'built-in', f'{cid}: {link}')

    def test_component_docs_are_instructions(self):
        '''部品の doc は、参照の検査の対象になる指示文であること。'''
        names = {document.relative_to(HERE).as_posix() for document in documents()}
        for path in sorted(COMPONENTS.glob('*.json')) + sorted(EXAMPLE_COMPONENTS.glob('*.json')):
            doc = json.loads(path.read_text(encoding='utf-8'))['doc']
            self.assertIn(doc, names, path.name)

    def test_skill_body_stays_small(self):
        '''本文は約 110 行・5,000 トークン未満に収める。'''
        text = SKILL.read_text(encoding='utf-8')
        self.assertLessEqual(len(text.splitlines()), SKILL_MAX_LINES)
        self.assertLess(len(text.encode('utf-8')) / BYTES_PER_TOKEN, SKILL_MAX_TOKENS)

    def test_headings_named_in_backticks_exist(self):
        '''`appendix/x.md` (`## 見出し`) と `appendix/x.md`, "見出し" の形で指す見出しが、その指示文にあること。'''
        backticked = re.compile(r'`([\w./-]+\.md)`[^`\n]{0,40}?\(`(## [^`]+)`\)')
        quoted = re.compile(r'`([\w./-]+\.md)`, "([^"]+)"')
        missing = []
        for document in documents():
            for lineno, line in enumerate(document.read_text(encoding='utf-8').splitlines(), 1):
                found = backticked.findall(line) + [(path, f'## {name}') for path, name in quoted.findall(line)]
                for path, heading in found:
                    target = HERE / path
                    if target.is_file() and heading not in target.read_text(encoding='utf-8').splitlines():
                        missing.append(f'{document.relative_to(HERE).as_posix()}:{lineno}: {path} {heading}')
        self.assertEqual([], missing, '\n'.join(missing))


if __name__ == '__main__':
    unittest.main()
