"""タスクの特徴 (task.profiled) を出す。events.py の `profile` と `backfill` から main(argv) を委ねられる。

仕様は docs/specs/harness-metrics.md の「タスクの特徴 (task.profiled)」と seam 3、追記 §3.6。
測った値は taxonomy.json の段階に丸め、タグは語彙で点検し、PBI の目的と受け入れ条件 (run は goal) を
埋め込んでアンカーの近さの上位 3 つを出す。ベクトルは保全先の vectors.jsonl にだけ書き、イベントには入れない。

使い方:
  python skills/atelier/task_profile.py profile --dir docs/pbi --pbi PBI-3 \
      --task-type feature --domain skill --difficulty mid [--base <commit>]
  python skills/atelier/task_profile.py profile --dir docs/pbi --run \
      --task-type feature --domain skill --difficulty high
  python skills/atelier/task_profile.py backfill --dir docs/pbi

終了コード: 0 出した (埋め込みの失敗もここ) / 2 引数と語彙の誤り (何も出さない)。
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

try:
    import events
except Exception:  # 同じフォルダに events.py が無くても、手元だけは動く
    events = None

import export_cases
import record_run

TAXONOMY_PATH = HERE / 'taxonomy.json'
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
PBI_FILE = re.compile(r'^PBI-\d+\.md$')
PBI_NAME = re.compile(r'^PBI-\d+$')
TAG_KEYS = ('task_type', 'domain', 'difficulty_hint')
EVENT_TYPE = 'task.profiled'
DEFAULT_MODEL = 'qwen3-embedding:0.6b'
EMBED_DEFAULTS = {
    'url': None, 'model': DEFAULT_MODEL, 'provider': 'ollama',
    'dims': None, 'normalize': True, 'prefix': '', 'timeout': 10,
}
# docs/pbi の記録 (events.jsonl・runs.jsonl・verify・reports) は、タスクの大きさに数えない
RECORD_PREFIX = 'docs/pbi/'


class UsageError(Exception):
    """引数と語彙の誤り。main が終了コード 2 にして、stderr に 1 行だけ出す。"""


# ---------- 語彙と段階 ----------

def _normal_dep(name) -> str:
    """依存名を比べられる形にする (extras・版の指定・大文字小文字を落とす)。"""
    text = str(name).strip().lower()
    text = re.sub(r'\[[^\]]*\]', '', text)
    text = re.split(r'[\s<>=!~;(]', text)[0]
    return text.strip()


@dataclass(frozen=True)
class Taxonomy:
    version: str
    vocab: dict
    buckets: dict
    languages: dict
    frameworks: dict
    test_patterns: tuple
    anchors: tuple

    @classmethod
    def load(cls, path=None) -> 'Taxonomy':
        raw = json.loads(Path(path or TAXONOMY_PATH).read_text(encoding='utf-8'))
        if not isinstance(raw, dict):
            raise ValueError('taxonomy is not an object')
        version = raw.get('taxonomy_version')
        if not isinstance(version, str) or not version:
            raise ValueError('taxonomy_version is missing')
        vocab_raw = raw.get('vocab')
        if not isinstance(vocab_raw, dict):
            raise ValueError('vocab is missing')
        vocab = {}
        for key in TAG_KEYS:
            values = vocab_raw.get(key)
            if not isinstance(values, list) or not values:
                raise ValueError(f'vocab.{key} is missing')
            vocab[key] = tuple(v for v in values if isinstance(v, str))
        buckets = {}
        for name, entries in (raw.get('buckets') or {}).items():
            rows = []
            for entry in entries:
                lo, hi, label = entry
                rows.append((int(lo), None if hi is None else int(hi), str(label)))
            rows.sort(key=lambda row: row[0])
            buckets[str(name)] = rows
        languages = {str(k).lower(): str(v) for k, v in (raw.get('languages') or {}).items()}
        frameworks = {}
        for dep, fid in (raw.get('frameworks') or {}).items():
            key = _normal_dep(dep)
            if key:
                frameworks[key] = str(fid)
        test_patterns = tuple(str(p) for p in (raw.get('test_patterns') or []))
        anchors = []
        for item in raw.get('anchors') or []:
            if isinstance(item, dict) and isinstance(item.get('anchor_id'), str) and isinstance(item.get('text'), str):
                anchors.append({'anchor_id': item['anchor_id'], 'text': item['text'],
                                'task_type': item.get('task_type'), 'domain': item.get('domain')})
        return cls(version=version, vocab=vocab, buckets=buckets, languages=languages, frameworks=frameworks,
                   test_patterns=test_patterns, anchors=tuple(anchors))

    def check_tags(self, task_type, domain, difficulty) -> dict:
        given = {'task_type': task_type, 'domain': domain, 'difficulty_hint': difficulty}
        for key in TAG_KEYS:
            allowed = self.vocab.get(key, ())
            if given[key] not in allowed:
                flag = '--' + key.replace('_', '-')
                raise UsageError(f'bad {flag}: {given[key]} (expected one of {", ".join(allowed)})')
        return given

    def bucket(self, quantity: str, n):
        """n を段階の id に丸める。n が None、またはどの段階にも入らないときは None。"""
        if n is None:
            return None
        for lo, hi, label in self.buckets.get(quantity) or ():
            if n >= lo and (hi is None or n <= hi):
                return label
        return None


def _load_taxonomy() -> Taxonomy:
    try:
        return Taxonomy.load()
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise UsageError(f'taxonomy.json is not readable: {error}')


# ---------- 測る (git とファイル) ----------

@dataclass(frozen=True)
class Target:
    """何を測るか。scope='pbi' は PBI 1 つ、scope='run' は run 全体 (goal)。

    paths は PBI の `## Targets` のファイル (pbi のときだけ)。pbi の大きさは作業ツリー全体の
    差分ではなく、この一覧から測る。run のときは空で、差分全体を測る。
    """
    scope: str
    pbi: str
    text: str
    body_chars: int
    refs: int | None
    paths: tuple = ()


def _read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


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


def _git(cwd, *args):
    """git をウインドウ無しで走らせ、成功したら stdout、失敗したら None。"""
    try:
        done = subprocess.run(['git', '-C', str(cwd), *args], capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=30,
                              creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout


def repo_root(base: Path) -> Path:
    out = _git(base, 'rev-parse', '--show-toplevel')
    if out and out.strip():
        return Path(out.strip())
    return Path.cwd()


def default_base(base: Path) -> str:
    """今の作業の runs.jsonl 行のうち最初の base_commit。無ければ HEAD。

    runs.jsonl は前の作業の行も残るので、最初の行ではなく今の作業キーの行だけを見る。
    """
    for row in _current_rows(base):
        value = row.get('base_commit')
        if isinstance(value, str) and value.strip():
            return value.strip()
    return 'HEAD'


def _file_lines(path: Path) -> int:
    """ファイルの行数。無い・読めないときは 0 (計画だけのファイル)。"""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


def untracked_files(root: Path):
    """git ls-files --others --exclude-standard の (パス, 行数)。取れなければ None。

    git diff は未追跡のファイルを出さないが、新しい機能はたいてい未追跡の新規ファイルで足される。
    追加された行としてファイルの行数を数える。docs/pbi の記録はタスクの大きさに数えない。
    """
    out = _git(root, 'ls-files', '--others', '--exclude-standard')
    if out is None:
        return None
    rows = []
    for line in out.splitlines():
        path = line.strip()
        if not path or path.startswith(RECORD_PREFIX):
            continue
        if path.startswith('./'):
            path = path[2:]
        rows.append((path, _file_lines(Path(root) / path)))
    return rows


def changed_files(root: Path, base: str):
    """(パス, 足し+引き) の一覧。取れなければ None。docs/pbi 配下は除く。

    追跡済みの差分 (git diff --numstat <base>) に、未追跡のファイルを行数つきで足す。
    """
    out = _git(root, '-c', 'core.quotepath=false', 'diff', '--numstat', '--no-renames',
               '--end-of-options', base, '--')
    if out is None:
        return None
    rows = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split('\t')
        if len(parts) < 3:
            continue
        path = '\t'.join(parts[2:])
        if path.startswith(RECORD_PREFIX):
            continue
        if path.startswith('./'):
            path = path[2:]
        try:
            churn = int(parts[0]) + int(parts[1])
        except ValueError:
            churn = 0  # バイナリは '-'
        rows[path] = rows.get(path, 0) + churn
    for path, lines in untracked_files(root) or ():
        if path not in rows:
            rows[path] = lines
    return list(rows.items())


def target_churn(root: Path, base: str, paths: list):
    """`## Targets` のファイルの足し+引き。取れなければ None。

    run 側の changed_files と同じ意味の数にするため、追跡済みは git diff --numstat <base> の
    足し+引き、未追跡はファイル全体の行数で数える。まだ無い計画のファイルは 0 行。
    パスは `--` の後ろにだけ渡し、base の後にオプションと読ませない。
    """
    if not paths:
        return 0
    out = _git(root, '-c', 'core.quotepath=false', 'diff', '--numstat', '--no-renames',
               '--end-of-options', base, '--', *paths)
    if out is None:
        return None
    rows = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split('\t')
        if len(parts) < 3:
            continue
        path = '\t'.join(parts[2:])
        try:
            churn = int(parts[0]) + int(parts[1])
        except ValueError:
            churn = 0  # バイナリは '-'
        rows[path] = rows.get(path, 0) + churn
    untracked = dict(untracked_files(root) or ())
    for path in paths:
        if path in untracked and path not in rows:
            rows[path] = untracked[path]
    return sum(rows.values())


TARGET_BULLET = re.compile(r'^\s*[-*]\s+(.*\S)\s*$')
BACKTICK = re.compile(r'`([^`]+)`')
FILE_NAME = re.compile(r'^[A-Za-z0-9_.-]+\.[A-Za-z][A-Za-z0-9]{0,7}$')


def _looks_like_path(text: str, languages=None) -> bool:
    """Targets の行から拾った語がファイルのパスらしいか (引数・関数名・イベント名を落とす)。"""
    if not text or any(char.isspace() for char in text):
        return False
    if text.startswith('-') or '(' in text or ')' in text or '=' in text:
        return False
    if '<' in text or '>' in text:
        return False  # <dir>/conductor/run.log のような場所の書き方
    if '/' in text or '\\' in text:
        return True
    suffix = Path(text).suffix.lower()
    if languages is not None:
        return suffix in languages
    return bool(FILE_NAME.match(text))


def target_paths(targets_text: str, languages=None) -> list:
    """## Targets の各行のファイルのパス。バッククォートの中のパスを優先する。

    PBI の行は「新規 `skills/...`: 説明」のようにパスが行の先頭でないことがあるので、
    行の先頭の語ではなく、パスらしいバッククォートの中身を取る。languages を渡すと、
    スラッシュの無い名前は既知の拡張子のときだけパスとみなす (run.started などを落とす)。
    """
    found = set()
    for line in targets_text.splitlines():
        match = TARGET_BULLET.match(line)
        if not match:
            continue
        body = match.group(1)
        path = next((item.strip() for item in BACKTICK.findall(body)
                     if _looks_like_path(item.strip(), languages)), None)
        if path is None:
            first = body.split()[0].strip('`').rstrip(':：,、')
            path = first if _looks_like_path(first, languages) else None
        if path:
            path = path.replace('\\', '/')
            while path.startswith('./'):
                path = path[2:]
            found.add(path)
    return sorted(found)


def count_pbis(base: Path) -> int:
    """PBI-N.md だけ。PBI-N.brief.md・PBI-N.rework-1.md などの派生は数えない。"""
    return sum(1 for path in Path(base).glob('PBI-*.md') if PBI_FILE.match(path.name))


def count_waves(base: Path) -> int:
    raw = _read_json(Path(base) / 'waves.json')
    waves = raw.get('waves') if isinstance(raw, dict) else None
    return len(waves) if isinstance(waves, list) else 0


def top_dir(path: str) -> str:
    parts = Path(path).parts
    return parts[0] if len(parts) > 1 else '.'


def languages_of(paths, tax: Taxonomy) -> list:
    found = set()
    for path in paths:
        language = tax.languages.get(Path(path).suffix.lower())
        if language:
            found.add(language)
    return sorted(found)


def is_test_path(path: str, tax: Taxonomy) -> bool:
    for pattern in tax.test_patterns:
        if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(Path(path).name, pattern):
            return True
    return False


def count_repo_files(root: Path):
    out = _git(root, 'ls-files')
    if out is None:
        return None
    return sum(1 for line in out.splitlines() if line.strip())


def rules_lines(root: Path) -> int:
    total = 0
    for name in ('CLAUDE.md', 'AGENTS.md'):
        try:
            total += len((root / name).read_text(encoding='utf-8', errors='replace').splitlines())
        except OSError:
            pass
    return total


def _dependency_names(root: Path) -> list:
    names = []
    package = _read_json(root / 'package.json')
    if isinstance(package, dict):
        for section in ('dependencies', 'devDependencies', 'peerDependencies', 'optionalDependencies'):
            block = package.get(section)
            if isinstance(block, dict):
                names.extend(str(key) for key in block)
    try:
        pyproject = (root / 'pyproject.toml').read_text(encoding='utf-8', errors='replace')
    except OSError:
        pyproject = ''
    if pyproject:
        for block in re.findall(r'(?ms)^\s*dependencies\s*=\s*\[(.*?)^\s*\]', pyproject):
            names.extend(re.findall(r'["\']([^"\']+)["\']', block))
        optional = re.search(r'(?ms)^\[project\.optional-dependencies\]\s*$(.*?)(?=^\[|\Z)', pyproject)
        if optional:
            names.extend(re.findall(r'["\']([^"\']+)["\']', optional.group(1)))
        in_deps = False
        for line in pyproject.splitlines():
            stripped = line.strip()
            if stripped.startswith('[') and stripped.endswith(']'):
                in_deps = stripped[1:-1].strip().endswith('dependencies')
                continue
            if in_deps:
                match = re.match(r'^([A-Za-z0-9_.@/-]+)\s*=', stripped)
                if match:
                    names.append(match.group(1))
    try:
        requirements = (root / 'requirements.txt').read_text(encoding='utf-8', errors='replace')
    except OSError:
        requirements = ''
    for line in requirements.splitlines():
        stripped = line.split('#', 1)[0].strip()
        if not stripped or stripped.startswith('-'):
            continue
        match = re.match(r'^([A-Za-z0-9_.@/-]+)', stripped)
        if match:
            names.append(match.group(1))
    return names


def frameworks_of(root: Path, tax: Taxonomy) -> list:
    found = set()
    for name in _dependency_names(root):
        fid = tax.frameworks.get(_normal_dep(name))
        if fid:
            found.add(fid)
    return sorted(found)


def measure(base: Path, target: Target, base_commit: str, tax: Taxonomy) -> dict:
    """data の size・spread・context・languages・frameworks の元の数 (丸める前)。

    pbi は `## Targets` のファイルで測る。行数は run と同じ「base からの足し+引き」で、
    未追跡のファイルは全体の行数、まだ無い計画のファイルは 0 行 (ファイル数には数える)。
    どの PBI も作業ツリー全体の差分を測ると同じ値になる (レビュー Act On 4) ため、
    pbi と run で測る範囲を分ける。
    """
    root = repo_root(base)
    if target.scope == 'pbi':
        paths = list(target.paths)
        return {
            'files': len(paths),
            'lines': target_churn(root, base_commit, paths),
            'pbis': count_pbis(base),
            'waves': count_waves(base),
            'input_chars': target.body_chars,
            'dirs': len({top_dir(path) for path in paths}),
            'languages': languages_of(paths, tax),
            'tests_touched': bool(paths) and any(is_test_path(path, tax) for path in paths),
            'repo_files': count_repo_files(root),
            'rules_lines': rules_lines(root),
            'refs': target.refs,
            'frameworks': frameworks_of(root, tax),
        }
    changed = changed_files(root, base_commit)
    paths = [path for path, _ in changed] if changed is not None else []
    return {
        'files': len(changed) if changed is not None else None,
        'lines': sum(churn for _, churn in changed) if changed is not None else None,
        'pbis': count_pbis(base),
        'waves': count_waves(base),
        'input_chars': target.body_chars,
        'dirs': len({top_dir(path) for path in paths}) if changed is not None else None,
        'languages': languages_of(paths, tax),
        'tests_touched': bool(paths) and any(is_test_path(path, tax) for path in paths),
        'repo_files': count_repo_files(root),
        'rules_lines': rules_lines(root),
        'refs': target.refs,
        'frameworks': frameworks_of(root, tax),
    }


# ---------- 本文 ----------

def embed_text(task: dict) -> str:
    """Goal の節と受け入れ条件の各行。profile と backfill が同じ関数を通す。"""
    parts = []
    goal = task.get('goal') if isinstance(task, dict) else None
    if isinstance(goal, str) and goal.strip():
        parts.append(goal.strip())
    acceptance = task.get('acceptance') if isinstance(task, dict) else None
    if isinstance(acceptance, list):
        for item in acceptance:
            if isinstance(item, dict) and isinstance(item.get('text'), str) and item['text'].strip():
                parts.append(item['text'].strip())
    if not parts and isinstance(task, dict) and isinstance(task.get('title'), str):
        parts.append(task['title'].strip())
    return '\n'.join(part for part in parts if part)


def pbi_target(base: Path, pbi, languages=None) -> Target:
    if not isinstance(pbi, str) or not PBI_NAME.match(pbi):
        raise UsageError(f'bad --pbi: {pbi}')
    path = Path(base) / f'{pbi}.md'
    try:
        text = path.read_text(encoding='utf-8')
    except OSError:
        raise UsageError(f'no such PBI: {path}')
    task = export_cases.task_of(text)
    refs = len(record_run.leading_ref_ids(text))
    paths = tuple(target_paths(task.get('targets') or '', languages))
    return Target(scope='pbi', pbi=pbi, text=embed_text(task), body_chars=len(text), refs=refs, paths=paths)


def _find_goal(base: Path, goal) -> Path | None:
    candidates = []
    if isinstance(goal, str) and goal.strip():
        given = Path(goal.strip())
        if given.is_absolute():
            candidates.append(given)
        else:
            candidates.append(given)
            candidates.append(Path(base) / given)
        candidates.append(Path(base) / 'conductor' / given.name)
    candidates.append(Path(base) / 'conductor' / 'goal.md')
    for path in candidates:
        if path.is_file():
            return path
    return None


def run_target(base: Path) -> Target:
    run = _read_json(Path(base) / 'conductor' / 'run.json')
    goal = run.get('goal') if isinstance(run, dict) else None
    path = _find_goal(Path(base), goal)
    if path is None:
        raise UsageError(f'no goal file for --run (looked in {Path(base) / "conductor"})')
    try:
        text = path.read_text(encoding='utf-8-sig')
    except OSError:
        raise UsageError(f'cannot read the goal file: {path}')
    return Target(scope='run', pbi='', text=text, body_chars=len(text), refs=None)


# ---------- 埋め込み ----------

def config_path() -> Path:
    given = os.environ.get('HARNESS_LOG_CONFIG')
    if given:
        return Path(given)
    return Path.home() / '.claude' / 'harness-log.json'


def embedding_config():
    """設定の embedding を既定で補って返す。url が無い・読めないなら None。"""
    raw = _read_json(config_path())
    if not isinstance(raw, dict):
        return None
    emb = raw.get('embedding')
    config = dict(EMBED_DEFAULTS)
    if isinstance(emb, dict):
        config.update(emb)
    url = config.get('url')
    if not isinstance(url, str) or not url.strip():
        return None
    config['url'] = url.strip()
    model = config.get('model')
    config['model'] = model if isinstance(model, str) and model else DEFAULT_MODEL
    provider = config.get('provider')
    config['provider'] = provider if isinstance(provider, str) and provider else 'ollama'
    dims = config.get('dims')
    config['dims'] = dims if isinstance(dims, int) and not isinstance(dims, bool) and dims > 0 else None
    config['normalize'] = config.get('normalize') is not False
    prefix = config.get('prefix')
    config['prefix'] = prefix if isinstance(prefix, str) else ''
    timeout = config.get('timeout')
    config['timeout'] = timeout if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) \
        and timeout > 0 else EMBED_DEFAULTS['timeout']
    return config


def embed(texts, config):
    """Ollama の /api/embed で埋め込む。失敗はどれも None (呼び出し側を止めない)。"""
    try:
        if not isinstance(config, dict) or not isinstance(texts, list) or not texts:
            return None
        if not all(isinstance(text, str) for text in texts):
            return None
        if config.get('provider', 'ollama') != 'ollama':
            return None
        url = config.get('url')
        if not isinstance(url, str) or not url.strip():
            return None
        model = config.get('model') if isinstance(config.get('model'), str) and config['model'] else DEFAULT_MODEL
        prefix = config.get('prefix')
        prefix = prefix if isinstance(prefix, str) else ''
        timeout = config.get('timeout')
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            timeout = EMBED_DEFAULTS['timeout']
        payload = {'model': model, 'input': [prefix + text for text in texts]}
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        request = urllib.request.Request(url.rstrip('/') + '/api/embed', data=body, method='POST',
                                         headers={'Content-Type': 'application/json'})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
        document = json.loads(raw.decode('utf-8'))
        if not isinstance(document, dict):
            return None
        vectors = document.get('embeddings')
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            return None
        dims = config.get('dims')
        wanted = dims if isinstance(dims, int) and not isinstance(dims, bool) and dims > 0 else None
        out = []
        for vector in vectors:
            if not isinstance(vector, list) or not vector:
                return None
            if wanted is not None and len(vector) != wanted:
                return None
            row = []
            for value in vector:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    return None
                number = float(value)
                if not math.isfinite(number):
                    return None
                row.append(number)
            out.append(row)
        if len({len(row) for row in out}) != 1:
            return None
        if config.get('normalize') is not False:
            normalized = []
            for row in out:
                length = math.sqrt(sum(value * value for value in row))
                if length <= 0:
                    return None
                normalized.append([value / length for value in row])
            out = normalized
        return out
    except (Exception, SystemExit):
        return None


def _anchors_sha(tax: Taxonomy) -> str:
    blob = json.dumps([[a['anchor_id'], a['text']] for a in tax.anchors],
                      ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(blob).hexdigest()


def anchor_cache_path(tax: Taxonomy, model: str) -> Path:
    safe = re.sub(r'[^A-Za-z0-9]', '_', model or '')
    return record_run.archive_root() / '_anchors' / f'{tax.version}__{safe}.json'


def _load_anchor_cache(tax: Taxonomy, model: str, sha: str):
    raw = _read_json(anchor_cache_path(tax, model))
    if not isinstance(raw, dict):
        return None
    if raw.get('taxonomy_version') != tax.version or raw.get('model') != model or raw.get('text_sha256') != sha:
        return None
    vectors = raw.get('vectors')
    if not isinstance(vectors, dict):
        return None
    out = {}
    for anchor_id, vector in vectors.items():
        if not isinstance(anchor_id, str) or not isinstance(vector, list) or not vector:
            return None
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in vector):
            return None
        out[anchor_id] = [float(value) for value in vector]
    if set(out) != {anchor['anchor_id'] for anchor in tax.anchors}:
        return None
    return out


def _save_anchor_cache(tax: Taxonomy, model: str, sha: str, vectors: dict) -> None:
    try:
        path = anchor_cache_path(tax, model)
        path.parent.mkdir(parents=True, exist_ok=True)
        document = {'taxonomy_version': tax.version, 'model': model, 'text_sha256': sha, 'vectors': vectors}
        path.write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')
    except OSError:
        pass


def nearest(vector, vectors: dict, k: int = 3) -> list:
    """コサイン類似度の上位 k を [{anchor_id, score (小数 2 桁)}] で返す。"""
    if not isinstance(vector, list) or not vector or not isinstance(vectors, dict):
        return []
    length = math.sqrt(sum(value * value for value in vector))
    if length <= 0:
        return []
    scored = []
    for anchor_id, other in vectors.items():
        if not isinstance(other, list) or len(other) != len(vector):
            continue
        other_length = math.sqrt(sum(value * value for value in other))
        if other_length <= 0:
            continue
        dot = sum(left * right for left, right in zip(vector, other))
        score = dot / (length * other_length)
        if not math.isfinite(score):
            continue
        scored.append({'anchor_id': anchor_id, 'score': round(score, 2)})
    scored.sort(key=lambda row: (-row['score'], row['anchor_id']))
    return scored[:k]


def anchors_for(text, tax: Taxonomy, config):
    """(anchors, 本文のベクトル) を返す。埋め込めなければ (None, None)。"""
    try:
        if config is None or not tax.anchors or not isinstance(text, str):
            return None, None
        model = config.get('model') or DEFAULT_MODEL
        sha = _anchors_sha(tax)
        cached = _load_anchor_cache(tax, model, sha)
        if cached is None:
            texts = [text] + [anchor['text'] for anchor in tax.anchors]
            vectors = embed(texts, config)
            if vectors is None or len(vectors) != len(texts):
                return None, None
            body = vectors[0]
            cached = {anchor['anchor_id']: vector for anchor, vector in zip(tax.anchors, vectors[1:])}
            _save_anchor_cache(tax, model, sha, cached)
        else:
            vectors = embed([text], config)
            if vectors is None or len(vectors) != 1:
                return None, None
            body = vectors[0]
        anchors = nearest(body, cached, 3)
        if not anchors:
            return None, None
        return anchors, body
    except (Exception, SystemExit):
        return None, None


# ---------- 保全先 (手元だけ) ----------

def repo_name(base: Path) -> str:
    """今の作業の runs.jsonl 行のうち最初の repo。無ければリポジトリのフォルダ名。"""
    for row in _current_rows(base):
        repo = row.get('repo')
        if isinstance(repo, str) and repo.strip():
            return repo.strip()
    return repo_root(base).name


def work_key(base: Path):
    """今の作業キー。record_run・failures と同じく STATUS.md を先に、無ければ runs.jsonl の最後。"""
    key = record_run.work_key(Path(base), None)
    if isinstance(key, str) and key:
        return key
    for row in reversed(record_run.read_rows(Path(base) / 'runs.jsonl')):
        key = row.get('work_key')
        if isinstance(key, str) and key:
            return key
    return None


def _current_rows(base: Path) -> list:
    """runs.jsonl のうち、今の作業キー (work_key) と同じ行だけ。作業キーが無ければ空。"""
    key = work_key(base)
    if not isinstance(key, str) or not key:
        return []
    return [row for row in record_run.read_rows(Path(base) / 'runs.jsonl')
            if isinstance(row, dict) and row.get('work_key') == key]


def archive_dir(base: Path) -> Path:
    return record_run.archive_root() / repo_name(base) / record_run.key_dir(work_key(base))


def append_vector(base: Path, row: dict) -> None:
    """保全先の vectors.jsonl に 1 行。同じ (case_id, model, kind) は置き換える。失敗は飲む。"""
    try:
        path = archive_dir(base) / 'vectors.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [item for item in _read_jsonl(path)
                if not (item.get('case_id') == row.get('case_id') and item.get('model') == row.get('model')
                        and item.get('kind') == row.get('kind'))]
        rows.append(row)
        text = ''.join(json.dumps(item, ensure_ascii=False, separators=(',', ':')) + '\n' for item in rows)
        with open(path, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(text)
    except (Exception, SystemExit):
        pass


# ---------- 組み立てと出力 ----------

def case_id(repo: str, key, scope: str, pbi) -> str:
    """export_cases と同じ '<repo>:<work_key>:<pbi>' (run は pbi の所に 'run')。"""
    return f'{repo}:{key or ""}:{pbi if scope == "pbi" else "run"}'


def _rounded_size(tax: Taxonomy, measured: dict) -> dict:
    """size の段階の値。profile と、finish の backfill の測り直しが同じ関数を使う。"""
    return {
        'files': tax.bucket('files', measured['files']),
        'lines': tax.bucket('lines', measured['lines']),
        'pbis': tax.bucket('pbis', measured['pbis']),
        'waves': tax.bucket('waves', measured['waves']),
        'input_chars': tax.bucket('input_chars', measured['input_chars']),
    }


def build(target: Target, tags: dict, measured: dict, anchors, tax: Taxonomy, cid: str) -> dict:
    return {
        'scope': target.scope,
        'taxonomy_version': tax.version,
        'task_type': tags['task_type'],
        'domain': tags['domain'],
        'difficulty_hint': tags['difficulty_hint'],
        'size': _rounded_size(tax, measured),
        'spread': {
            'dirs': tax.bucket('dirs', measured['dirs']),
            'languages': tax.bucket('languages', len(measured['languages'])),
            'tests_touched': bool(measured['tests_touched']),
        },
        'context': {
            'repo_files': tax.bucket('repo_files', measured['repo_files']),
            'rules_lines': tax.bucket('rules_lines', measured['rules_lines']),
            'refs': tax.bucket('refs', measured['refs']),
        },
        'languages': list(measured['languages'])[:16],
        'frameworks': list(measured['frameworks'])[:16],
        'anchors': anchors,
        'case_id': cid,
    }


def _emit(base: Path, data: dict, pbi) -> bool:
    if events is None:
        return False
    try:
        return bool(events.emit(base, EVENT_TYPE, data, pbi=pbi))
    except (Exception, SystemExit):
        return False


def _vector_row(config, cid: str, vector: list) -> dict:
    model = config.get('model') if isinstance(config, dict) else None
    return {'kind': 'task', 'case_id': cid, 'model': model if isinstance(model, str) else DEFAULT_MODEL,
            'dim': len(vector), 'vector': vector}


# ---------- profile / backfill ----------

def _parse(parser, argv):
    try:
        return parser.parse_args(argv), None
    except SystemExit as error:
        return None, error.code if isinstance(error.code, int) else 2


def _profile(argv) -> int:
    parser = argparse.ArgumentParser(prog='task_profile.py profile',
                                     description='PBI か run のタスクの特徴を task.profiled として出す')
    parser.add_argument('--dir', default='docs/pbi')
    parser.add_argument('--pbi')
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--task-type', dest='task_type', required=True)
    parser.add_argument('--domain', required=True)
    parser.add_argument('--difficulty', required=True)
    parser.add_argument('--base')
    args, code = _parse(parser, argv)
    if args is None:
        return code if code is not None else 2
    if bool(args.pbi) == bool(args.run):
        raise UsageError('--pbi と --run のどちらか 1 つを渡す')
    base = Path(args.dir)
    tax = _load_taxonomy()
    tags = tax.check_tags(args.task_type, args.domain, args.difficulty)
    target = pbi_target(base, args.pbi, tax.languages) if args.pbi else run_target(base)
    base_commit = args.base or default_base(base)
    measured = measure(base, target, base_commit, tax)
    config = embedding_config()
    anchors, vector = anchors_for(target.text, tax, config)
    cid = case_id(repo_name(base), work_key(base), target.scope, target.pbi)
    data = build(target, tags, measured, anchors, tax, cid)
    _emit(base, data, target.pbi or None)
    if vector is not None:
        append_vector(base, _vector_row(config, cid, vector))
    return 0


def _find_case(cases: list, pbi: str, cid):
    """cid が分かっていれば、その id の行だけを返す (同じ PBI 番号の別の作業の行で代わりにしない)。
    cid が無いときだけ、PBI 番号の合う最後の行を返す。"""
    for row in reversed(cases):
        if row.get('pbi') != pbi:
            continue
        if cid and row.get('id') == cid:
            return row
    if cid:
        return None
    for row in reversed(cases):
        if row.get('pbi') == pbi:
            return row
    return None


def _backfill(argv) -> int:
    parser = argparse.ArgumentParser(prog='task_profile.py backfill',
                                     description='PBI の大きさを測り直し、anchors が null の task.profiled を'
                                                 ' cases.jsonl から埋め込み直す')
    parser.add_argument('--dir', default='docs/pbi')
    args, code = _parse(parser, argv)
    if args is None:
        return code if code is not None else 2
    base = Path(args.dir)
    rows = _read_jsonl(base / 'events.jsonl')
    if not rows:
        return 0
    # events.jsonl には前の作業の行も残る。今の作業の case_id と違う行は
    # 測り直し・埋め込み直し・出し直しをしない (前の作業の大きさを壊さない)。
    repo = repo_name(base)
    key = work_key(base)
    latest = {}
    for row in rows:
        if row.get('type') != EVENT_TYPE:
            continue
        data = row.get('data') if isinstance(row.get('data'), dict) else {}
        scope = data.get('scope')
        pbi = row.get('pbi') if isinstance(row.get('pbi'), str) else None
        if data.get('case_id') != case_id(repo, key, scope, pbi):
            continue
        latest[('run', None) if scope == 'run' else ('pbi', pbi)] = row
    if not latest:
        return 0
    tax = _load_taxonomy()
    config = embedding_config()
    cases = _read_jsonl(base / 'cases.jsonl') if config is not None else []
    base_commit = default_base(base)
    embedding_failed = False
    # finish に走るので、PBI ごとの大きさはここで測り直す (plan の確定時はまだ差分が無い)。
    # 埋め込みは今までどおり anchors が null の行だけ、設定があるときだけ。両方変わったら 1 行にまとめる。
    for key, row in latest.items():
        scope, pbi = key
        data = dict(row.get('data') or {})
        changed = False
        if scope == 'pbi':
            if not pbi:
                continue
            try:
                target = pbi_target(base, pbi, tax.languages)
            except UsageError:
                print(f'task_profile: backfill: no such PBI: {base / (str(pbi) + ".md")}', file=sys.stderr)
                target = None
            if target is not None:
                measured = measure(base, target, base_commit, tax)
                size = _rounded_size(tax, measured)
                if size != data.get('size'):
                    data['size'] = size
                    changed = True
        if data.get('anchors') is None and config is not None and not embedding_failed:
            text = None
            if scope == 'pbi':
                case = _find_case(cases, pbi, data.get('case_id'))
                if case is None:
                    print(f'task_profile: backfill: no case for {pbi}', file=sys.stderr)
                else:
                    text = embed_text(case.get('task') or {})
            else:
                try:
                    text = run_target(base).text
                except UsageError as error:
                    print(f'task_profile: backfill: {error}', file=sys.stderr)
            if text is not None:
                anchors, vector = anchors_for(text, tax, config)
                if anchors is None:
                    print('task_profile: backfill: embedding failed; stop', file=sys.stderr)
                    embedding_failed = True
                else:
                    data['anchors'] = anchors
                    changed = True
                    if vector is not None:
                        cid = data.get('case_id')
                        if not isinstance(cid, str) or not cid:
                            cid = case_id(repo_name(base), work_key(base), scope, pbi)
                        append_vector(base, _vector_row(config, cid, vector))
        if changed:
            _emit(base, data, pbi if scope == 'pbi' else None)
    return 0


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in ('profile', 'backfill'):
        print('task_profile: usage: task_profile.py profile|backfill --dir docs/pbi ...', file=sys.stderr)
        return 2
    try:
        if args[0] == 'profile':
            return _profile(args[1:])
        return _backfill(args[1:])
    except UsageError as error:
        print(f'task_profile: {error}', file=sys.stderr)
        return 2
    except (Exception, SystemExit):
        return 0


if __name__ == '__main__':
    sys.exit(main())
