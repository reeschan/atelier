"""役割の受け渡しの JSON (レビュー担当・テスト担当・設計者の出番) を確かめ、波のレビューを 1 つの束にまとめる。

使い方 (リポジトリのルートで):
  python <このディレクトリ>/role_results.py check <file> [<file> ...]
  python <このディレクトリ>/role_results.py bundle --wave <n> [--dir docs/pbi] --out docs/pbi/reviews/wave-<n>.bundle.json

check   schema ごとに中身を確かめ、1 ファイル 1 行 (`<パス>: ok` か `<パス>: <誤り>; <誤り>`) を出す。
        atelier.review/1 (レビュー担当)・atelier.test-result/1 (テスト担当)・atelier.episode/1 (設計者の出番)。
        3 つのどれでもない schema (束のファイルを含む) は誤り。
bundle  波 n の PBI を <dir>/waves.json から決め、PBI ごとにいちばん新しい報告 (<dir>/reports/) を、次を
        自分で確かめて 1 報告 1 行の表にする。レビュー担当の自己申告に頼らない。
          - <dir>/reviews/<報告名>.review.json が在り、atelier.review/1 に合い、report・pbi がこの報告・PBI で、
            criteria が PBI の Acceptance criteria の番号を全部判定している
          - <dir>/verify/<報告名>.json (verify.py が書く) が在り、報告より新しく、name・pbi が合い、番号の重複が無く、
            コマンドが PBI の同じ番号のものと同じで、PBI の verify の番号を
            (tests-at-end.json の deferred に合うものを除いて) 全部覆う。表の verify の列はここの終了コードから作る。
            あとに回すコマンドしか無い PBI は verify.py --skip-deferred が JSON を書かずに終わるので、
            そのときに限り JSON の欠けを誤りにせず、verify の列を `verify: all deferred` にする。
            review.json の verify.failed と食い違えば、または fail の基準があるのに recommendation が pass なら、
            その行に mismatch を付ける
          - 触らない範囲 (unchanged.py の記録) が変わっていないか
          - 作ったモデルが分かり (分からなければ誤り)、reviewer.model と同じなら誤り
        表のあとに `bundle: <--out のパス>` を出す。束のファイル (--out) は引き継ぎ用の写しで、schema を持たず、
        check の対象にしない。誤りがあっても束のファイルは書く。

終了コード: 0 正しい / 3 誤り・欠け (設計者はその報告のレビューだけやり直す) / 2 引数の誤り・waves.json に波が無い
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import common
import record_run
import unchanged
import verify

# イベントの口 (同じフォルダ)。単体で配られたときなどに無ければ、イベントは出さない
try:
    import events
except Exception:
    events = None

REVIEW = 'atelier.review/1'
TEST_RESULT = 'atelier.test-result/1'
EPISODE = 'atelier.episode/1'

SEVERITIES = ('critical', 'major', 'minor')
VERDICTS = ('pass', 'fail', 'deferred')
RECOMMEND = ('pass', 'rework', 'tier-up', 'hold')
TEST_KINDS = ('wave', 'survey', 'final', 'full')
TEST_CAUSES = ('intended', 'regression', 'unknown')
TEST_RECOMMEND = ('pass', 'rework', 'needs-designer')

EPISODES = ('design', 'build', 'test', 'review', 'finish')
EPISODE_STATUS = ('done', 'question', 'handoff', 'error')
QUESTION_KINDS = ('approve', 'rework-limit', 'destructive', 'send-approval', 'decision', 'full-run')
# 出番の結果の question の任意の topic (docs/specs/harness-metrics.md の question.asked と同じ値)
QUESTION_TOPICS = ('runner-unavailable', 'omm', 'recheck', 'unknown', 'approve-ask',
                   'design-approval', 'headless', 'other')
SUMMARY_MAX_LINES = 5
SUMMARY_MAX_CHARS = 200
PBI_NAME = re.compile(r'^PBI-\d+$')
# 報告の Markdown の名前: PBI-003.md・PBI-003.rework-2.md (.state.md・.last.md・.log は報告ではない)
REPORT_FILE = re.compile(r'^(PBI-\d+)(?:\.rework-(\d+))?\.md$')
# PBI の verify のコマンドにある unchanged.py check <名前>
UNCHANGED_CALL = re.compile(r'unchanged\.py(?:\s+--dir\s+\S+)?\s+check\s+([^\s`\'"]+)')

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INVALID = 3


def _is_str(value) -> bool:
    return isinstance(value, str) and value.strip() != ''


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def check_review(data) -> list[str]:
    """レビュー担当の結果 (1 報告) の誤りを返す。空なら正しい。"""
    if not isinstance(data, dict) or data.get('schema') != REVIEW:
        return [f'schema is not {REVIEW}']
    errors = []
    for key in ('report', 'pbi'):
        if not _is_str(data.get(key)):
            errors.append(f'{key}: missing')
    reviewer = data.get('reviewer')
    if not isinstance(reviewer, dict) or not _is_str(reviewer.get('model')):
        errors.append('reviewer.model: missing')
    failed = data.get('verify', {}).get('failed') if isinstance(data.get('verify'), dict) else None
    if not isinstance(failed, list) or not all(_is_int(n) for n in failed):
        errors.append('verify.failed: a list of numbers')
    criteria = data.get('criteria')
    if not isinstance(criteria, list) or not criteria:
        errors.append('criteria: empty')
    else:
        for i, row in enumerate(criteria):
            if (not isinstance(row, dict) or not _is_int(row.get('n')) or row.get('verdict') not in VERDICTS
                    or not _is_str(row.get('why'))):
                errors.append(f'criteria[{i}]: needs n, verdict ({"/".join(VERDICTS)}) and why')
    findings = data.get('findings')
    if not isinstance(findings, list):
        errors.append('findings: not a list')
    else:
        for i, row in enumerate(findings):
            if (not isinstance(row, dict) or row.get('severity') not in SEVERITIES
                    or not _is_str(row.get('file')) or not _is_int(row.get('line'))
                    or not _is_str(row.get('why')) or not _is_str(row.get('repro'))):
                errors.append(f'findings[{i}]: needs severity ({"/".join(SEVERITIES)}), file, line, why and repro')
    if data.get('recommendation') not in RECOMMEND:
        errors.append(f'recommendation: one of {", ".join(RECOMMEND)}')
    built = data.get('built')
    if built is not None and (not isinstance(built, dict) or not _is_str(built.get('model'))):
        errors.append('built: {"runner", "model"} copied from the Built on: line')
    return errors


def check_test_result(data) -> list[str]:
    """テスト担当の結果の誤りを返す。survey の判定の数は test-survey.md の担当で、ここでは見ない。"""
    if not isinstance(data, dict) or data.get('schema') != TEST_RESULT:
        return [f'schema is not {TEST_RESULT}']
    errors = []
    if data.get('kind') not in TEST_KINDS:
        errors.append(f'kind: one of {", ".join(TEST_KINDS)}')
    commands = data.get('commands')
    if not isinstance(commands, list):
        errors.append('commands: not a list')
    else:
        for i, row in enumerate(commands):
            if not isinstance(row, dict) or not _is_str(row.get('command')) or not _is_int(row.get('code')):
                errors.append(f'commands[{i}]: needs command and code')
    failing = data.get('failing')
    if not isinstance(failing, list):
        errors.append('failing: not a list')
    else:
        for i, row in enumerate(failing):
            if (not isinstance(row, dict) or not _is_str(row.get('test')) or not _is_str(row.get('file'))
                    or not _is_str(row.get('pbi')) or row.get('cause') not in TEST_CAUSES
                    or not _is_str(row.get('why'))):
                errors.append(f'failing[{i}]: needs test, file, pbi, cause ({"/".join(TEST_CAUSES)}) and why')
    if data.get('recommendation') not in TEST_RECOMMEND:
        errors.append(f'recommendation: one of {", ".join(TEST_RECOMMEND)}')
    return errors


def _check_next(data, errors) -> None:
    """status done の next。finish の出番は next を持たず、完了報告のパス (report) を持つ。"""
    nxt = data.get('next')
    if data.get('episode') == 'finish':
        if nxt is not None:
            errors.append('next: none after finish')
        if not _is_str(data.get('report')):
            errors.append('report: the completion report path is needed after finish')
        if 'report_body' in data and not _is_str(data.get('report_body')):
            errors.append('report_body: the completion report text, when the designer could not write the file')
        return
    if not isinstance(nxt, dict) or nxt.get('episode') not in EPISODES:
        errors.append(f'next.episode: one of {", ".join(EPISODES)}')
        return
    if nxt['episode'] == 'build' and (not _is_int(nxt.get('wave')) or nxt['wave'] < 1):
        errors.append('next.wave: a number from 1 for build')
    if 'pbis' in nxt:
        pbis = nxt['pbis']
        if not isinstance(pbis, list) or not all(isinstance(p, str) and p.strip() for p in pbis):
            errors.append('next.pbis: a list of strings')


def check_episode(data) -> list[str]:
    """設計者の出番の結果の誤りを返す。出番のつながりと waves.json の波は conductor.py が確かめる。"""
    if not isinstance(data, dict) or data.get('schema') != EPISODE:
        return [f'schema is not {EPISODE}']
    errors = []
    if data.get('episode') not in EPISODES:
        errors.append(f'episode: one of {", ".join(EPISODES)}')
    if not _is_int(data.get('seq')):
        errors.append('seq: not a number')
    status = data.get('status')
    if status not in EPISODE_STATUS:
        errors.append(f'status: one of {", ".join(EPISODE_STATUS)}')
    summary = data.get('summary')
    if (not isinstance(summary, list) or len(summary) > SUMMARY_MAX_LINES
            or any(not isinstance(line, str) or len(line) > SUMMARY_MAX_CHARS for line in summary)):
        errors.append(f'summary: at most {SUMMARY_MAX_LINES} lines of {SUMMARY_MAX_CHARS} characters')
    if status == 'question':
        question = data.get('question')
        if (not isinstance(question, dict) or question.get('kind') not in QUESTION_KINDS
                or not _is_str(question.get('path')) or not _is_str(question.get('summary'))):
            errors.append(f'question: needs kind ({"/".join(QUESTION_KINDS)}), path and summary')
        else:
            if question.get('check') not in (None, 'design-record'):
                errors.append('question.check: design-record or absent')
            # topic は任意。無ければ other として数える (docs/specs/harness-metrics.md の question.asked)
            if question.get('topic') is not None and question['topic'] not in QUESTION_TOPICS:
                errors.append(f'question.topic: one of {", ".join(QUESTION_TOPICS)}')
            # 出番の途中の問い (approve 以外) は、答えのあと Continue from: で続けるので、状態の板が要る
            if question['kind'] != 'approve' and not _is_str(data.get('state')):
                errors.append('state: the state tablet path is needed for a question other than approve')
    if status == 'handoff' and not _is_str(data.get('state')):
        errors.append('state: the state tablet path is needed for a handoff')
    if status == 'done':
        _check_next(data, errors)
    if status == 'error' and not _is_str(data.get('reason')):
        errors.append('reason: needed for an error')
    return errors


CHECKS = {REVIEW: check_review, TEST_RESULT: check_test_result, EPISODE: check_episode}


# ---- イベント (review.judged・test.judged) ----

def event_dir(path) -> Path | None:
    """結果のファイルのパスから <dir> (docs/pbi に当たるフォルダ) を導く。決まった場所の外なら None。

    check は --dir を取らないので、cwd から決めるとスキルのフォルダに書いてしまう。
    """
    parent = Path(path).parent
    if parent.name == 'reviews':
        return parent.parent
    if parent.name == 'tests' and parent.parent.name == 'verify':
        return parent.parent.parent
    return None


def _counts(values, names) -> dict:
    """値のリストを names ごとに数えた object。"""
    values = list(values)
    return {name: sum(1 for value in values if value == name) for name in names}


def judged_event(schema: str, data: dict) -> tuple[str, dict]:
    """check が通った結果のイベント (type, data)。"""
    if schema == REVIEW:
        criteria = {'pass': 0, 'fail': 0, 'unknown': 0}
        for row in data['criteria']:
            # deferred とそれ以外は unknown に数える
            verdict = row['verdict'] if row['verdict'] in ('pass', 'fail') else 'unknown'
            criteria[verdict] += 1
        return 'review.judged', {
            'report': data['report'],
            'recommendation': data['recommendation'],
            'findings': _counts((row['severity'] for row in data['findings']), SEVERITIES),
            'criteria': criteria,
        }
    codes = [row['code'] for row in data['commands']]
    passed = sum(1 for code in codes if code == 0)
    return 'test.judged', {
        'kind': data['kind'],
        'passed': passed,
        'failed': len(codes) - passed,
        'failing': _counts((row['cause'] for row in data['failing']), TEST_CAUSES),
    }


def _judged_once(path, base) -> str:
    """once の鍵。<run_id>:<プロジェクトからの相対パス>:<中身の sha256>。

    run_id を鍵に入れるのは、`.events/seen.txt` が実行をまたいで残るため。run が変われば
    同じファイルをもう一度 check しても出す (記録は run ごとに数える)。run.json が無ければ none。
    パスも鍵に入れるのは、合格した test-result が波・PBI・時刻を持たず、波ごとに同じ本文が
    別のパスに書かれるから (同じファイルの繰り返しだけを落とす)。
    """
    target = Path(path)
    try:
        name = target.resolve().relative_to(Path(base).resolve().parent).as_posix()
    except (OSError, ValueError):
        name = target.as_posix()
    return f'{record_run.run_id_of(Path(base))}:{name}:{hashlib.sha256(target.read_bytes()).hexdigest()}'


def _emit_judged(path, data) -> None:
    """check が通った結果を、決まった場所 (reviews/・verify/tests/) にあるときだけイベントにする。"""
    if events is None or data.get('schema') not in (REVIEW, TEST_RESULT):
        return
    base = event_dir(path)
    if base is None:
        return
    try:
        once = _judged_once(path, base)
    except OSError:
        return
    type, payload = judged_event(data['schema'], data)
    # 指示文は「ok が出るまで check を繰り返す」ので、同じファイル・同じ中身の 2 回目は once で落とす
    events.emit(base, type, payload, once=once)


def check_file(path) -> list[str]:
    """ファイル 1 つの誤り。読めない・3 つの schema のどれでもないものも誤り。

    通った review・test-result の結果は、決まった場所にあるときだけ review.judged・test.judged にする。
    """
    data = common.load_json(path)
    if data is None:
        return ['not readable JSON']
    # schema が文字列でない (リスト・数・null) と CHECKS.get が例外になるので、渡さずに誤りにする
    schema = data.get('schema') if isinstance(data, dict) else None
    check = CHECKS.get(schema) if isinstance(schema, str) else None
    if check is None:
        return [f'unknown schema (expected one of {", ".join(CHECKS)})']
    errors = check(data)
    if not errors:
        _emit_judged(path, data)
    return errors


# ---- bundle ----

def wave_pbis(base: Path, wave: int) -> list[str] | None:
    """waves.json の波 n の PBI。ファイルが無い・波が無いときは None。"""
    data = common.load_json(base / 'waves.json')
    if not isinstance(data, dict) or not isinstance(data.get('waves'), list):
        return None
    for item in data['waves']:
        if isinstance(item, dict) and item.get('n') == wave and isinstance(item.get('pbis'), list):
            return [p for p in item['pbis'] if isinstance(p, str)]
    return None


def latest_report(base: Path, pbi: str) -> tuple[str, int, Path] | None:
    """その PBI のいちばん新しい試行の報告。(報告名, rework の番号, パス)。初回は番号 0。"""
    best = None
    for path in (base / 'reports').glob(f'{pbi}*.md'):
        match = REPORT_FILE.match(path.name)
        if not match or match.group(1) != pbi:
            continue
        attempt = int(match.group(2) or 0)
        if best is None or attempt > best[1]:
            best = (path.name[:-3], attempt, path)
    return best


def pbi_commands(base: Path, pbi: str) -> list[tuple[int, str]] | None:
    """PBI の verify のコマンド (番号, コマンド)。PBI が読めなければ None。"""
    try:
        return verify.commands((base / f'{pbi}.md').read_text(encoding='utf-8'))
    except OSError:
        return None


def verify_status(base: Path, report: str, pbi: str, report_path: Path, commands: list[tuple[int, str]]) -> dict:
    """verify の JSON を自分で読む。{'failed': [番号], 'errors': [理由], 'all_deferred': bool}。

    失敗は番号ごとの終了コード (0 以外か数でないもの)。覆う番号は、PBI の verify の番号のうち
    あとに回すもの (tests-at-end.json の deferred) を除いたもの。番号付きのコマンドが 1 つ以上あり、
    そのすべてがあとに回すもののときは verify.py --skip-deferred が JSON を書かずに終わるので、
    そのときに限り JSON の欠けを誤りにせず all_deferred を立てる。同じ番号が 2 つあるもの、PBI の同じ番号のコマンドと違うもの、
    name・pbi が報告名・PBI と違うものは誤り。
    """
    patterns = verify.deferred_patterns(base)
    required = [n for n, cmd in commands if not verify.is_deferred(cmd, patterns)]
    path = base / 'verify' / f'{report}.json'
    data = common.load_json(path)
    if not isinstance(data, dict) or not isinstance(data.get('results'), list):
        # 番号付きの verify コマンドが 1 つ以上あり、そのすべてがあとに回すもののときだけ立てる。
        # その PBI は verify.py --skip-deferred が JSON を書かずに終わるので、ファイルが無いことだけを
        # 誤りにしない (壊れた JSON が在るのは今までどおり誤り)。コマンドが 1 つも読めない PBI は誤り。
        if commands and not required and not path.is_file():
            return {'failed': [], 'errors': [], 'all_deferred': True}
        return {'failed': [], 'errors': [f'no verify/{report}.json'], 'all_deferred': False}
    if path.stat().st_mtime <= report_path.stat().st_mtime:
        return {'failed': [], 'errors': [f'verify/{report}.json is not newer than the report'], 'all_deferred': False}
    errors = []
    if data.get('name') != report or data.get('pbi') != pbi:
        errors.append(f'verify/{report}.json is for {data.get("name")}/{data.get("pbi")}, not {report}/{pbi}')
    rows = [r for r in data['results'] if isinstance(r, dict) and _is_int(r.get('n'))]
    numbers = [r['n'] for r in rows]
    duplicated = sorted({n for n in numbers if numbers.count(n) > 1})
    if duplicated:
        errors.append('verify has a number twice: ' + ','.join(str(n) for n in duplicated))
    expected = dict(commands)
    differ = sorted({r['n'] for r in rows if r['n'] in expected
                     and str(r.get('command', '')).strip() != expected[r['n']].strip()})
    if differ:
        errors.append('verify command differs from the PBI for ' + ','.join(str(n) for n in differ))
    # 同じ番号が 2 つあるときは、1 つでも失敗なら失敗
    failed = sorted({r['n'] for r in rows if not (_is_int(r.get('code')) and r['code'] == 0)})
    uncovered = [n for n in required if n not in numbers]
    if uncovered:
        errors.append('verify not run for ' + ','.join(str(n) for n in uncovered))
    return {'failed': failed, 'errors': errors, 'all_deferred': False}


def acceptance_numbers(base: Path, pbi: str) -> list[int]:
    """PBI の `## Acceptance criteria` の番号。節が無ければ空 (番号の照合をしない)。"""
    try:
        text = (base / f'{pbi}.md').read_text(encoding='utf-8')
    except OSError:
        return []
    found = re.search(r'^## (?:Acceptance criteria|受け入れ基準)\s*\n(.*?)(?=^## |\Z)', text, re.S | re.M)
    if not found:
        return []
    return [int(m.group(1)) for m in re.finditer(r'^\s*(\d+)[.:)]\s', found.group(1), re.M)]


def unchanged_column(base: Path, commands: list[tuple[int, str]]) -> str:
    """触らない範囲の列。ok / changed / none (save の記録が無い) / - (verify に unchanged.py check が無い)。"""
    name = None
    for _, cmd in commands:
        match = UNCHANGED_CALL.search(cmd)
        if match:
            name = match.group(1)
            break
    if name is None:
        return '-'
    if not unchanged.snapshot_path(base, name).is_file():
        return 'none'
    try:
        return 'changed' if unchanged.changed(base, name) else 'ok'
    except (OSError, ValueError, AttributeError):
        return 'none'


def built_model(base: Path, report: str, pbi: str, attempt: int, review: dict) -> str | None:
    """その報告を作ったモデル。

    review.json の built.model (依頼文の Built on: の写し。別の実行先に回したときも実際のモデル) を先に、
    無ければ runs.jsonl のその報告の最後の行の model、それも無ければ route.json の route[attempt]
    (route の長さを超えれば最後)。
    """
    built = review.get('built')
    if isinstance(built, dict) and _is_str(built.get('model')):
        return built['model']
    model = None
    try:
        lines = (base / 'runs.jsonl').read_text(encoding='utf-8').splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get('report') == report and _is_str(row.get('model')):
            model = row['model']
    if model:
        return model
    route = common.load_json(base / f'{pbi}.route.json', {})
    steps = route.get('route') if isinstance(route, dict) else None
    if isinstance(steps, list) and steps:
        step = steps[min(attempt, len(steps) - 1)]
        if isinstance(step, dict) and _is_str(step.get('model')):
            return step['model']
    return None


def same_model(a: str, b: str) -> bool:
    """同じモデルか。小文字にし、opus・sonnet・haiku のどれを含むかで決める (どれも含まなければ文字列の一致)。"""
    x, y = a.lower(), b.lower()
    tier_x = next((t for t in common.TIERS if t in x), None)
    tier_y = next((t for t in common.TIERS if t in y), None)
    if tier_x is None and tier_y is None:
        return x == y
    return tier_x == tier_y


def needs_designer(row: dict) -> bool:
    """設計者が review.json を開いて照合する行か。そろって通った行は表だけで決まる。誤りの行は設計者が見る。"""
    if row.get('errors'):
        return True
    return (row['recommendation'] != 'pass' or bool(row['verify_failed']) or row['unchanged'] in ('changed', 'none')
            or row['findings'][0] > 0 or row['findings'][1] > 0 or row['mismatch'])


def row_line(row: dict) -> str:
    """表の 1 行 (2 つの空白で区切る)。"""
    if row['errors']:
        return f"{row['report']}  error {'; '.join(row['errors'])}"
    failed = ','.join(str(n) for n in row['verify_failed'])
    # あとに回すコマンドしか無い PBI は verify の JSON が無い。合否ではなく、その旨を列に出す
    verify_column = 'verify: all deferred' if row.get('all_deferred') else f"verify {'fail ' + failed if failed else 'ok'}"
    parts = [row['report'], verify_column, f"unchanged {row['unchanged']}",
             f"review {row['recommendation']}", 'findings ' + ','.join(str(n) for n in row['findings'])]
    if row['mismatch']:
        parts.append('mismatch')
    if needs_designer(row):
        parts.append('open')
    return '  '.join(parts)


def bundle_row(base: Path, pbi: str) -> dict:
    """束の 1 行。errors が空でなければ、設計者はその報告のレビューだけやり直す。"""
    found = latest_report(base, pbi)
    if found is None:
        return {'report': pbi, 'pbi': pbi, 'errors': ['no report in reports/']}
    report, attempt, report_path = found
    row = {'report': report, 'pbi': pbi, 'errors': [], 'review': (base / 'reviews' / f'{report}.review.json').as_posix()}
    review = common.load_json(base / 'reviews' / f'{report}.review.json')
    if review is None:
        row['errors'].append(f'no reviews/{report}.review.json')
        return row
    problems = check_review(review)
    if problems:
        row['errors'] += problems
        return row
    # review.json がこの報告・この PBI のものか (別の報告のレビューを流用していないか)
    if review['report'] != report:
        row['errors'].append(f'review.json is for report {review["report"]}, not {report}')
    if review['pbi'] != pbi:
        row['errors'].append(f'review.json is for {review["pbi"]}, not {pbi}')
    if row['errors']:
        return row
    commands = pbi_commands(base, pbi)
    if commands is None:
        row['errors'].append(f'{pbi}.md not readable')
        return row
    status = verify_status(base, report, pbi, report_path, commands)
    row['errors'] += status['errors']
    wanted = acceptance_numbers(base, pbi)
    judged = {c['n'] for c in review['criteria']}
    missing = [n for n in wanted if n not in judged]
    if missing:
        row['errors'].append('review.json has no verdict for criteria ' + ','.join(str(n) for n in missing))
    built = built_model(base, report, pbi, attempt, review)
    reviewer = review['reviewer']['model']
    if built is None:
        row['errors'].append('built model unknown: no built.model in review.json, no row for '
                             f'{report} in runs.jsonl, no readable {pbi}.route.json')
    elif same_model(built, reviewer):
        row['errors'].append(f'reviewer {reviewer} is the model that built it ({built})')
    # レビュー担当の自己申告との食い違い: verify の失敗の集合、または fail の基準があるのに pass
    failing_but_pass = review['recommendation'] == 'pass' and any(c['verdict'] == 'fail' for c in review['criteria'])
    row.update({
        'recommendation': review['recommendation'],
        'verify_failed': status['failed'],
        'all_deferred': status['all_deferred'],
        'mismatch': sorted(set(review['verify']['failed'])) != status['failed'] or failing_but_pass,
        'unchanged': unchanged_column(base, commands),
        'findings': [sum(1 for f in review['findings'] if f['severity'] == s) for s in SEVERITIES],
        'reviewer': reviewer,
        'built': built,
    })
    return row


WAVE_PBI_NAME = re.compile(r'^PBI-[\w.-]+$')


def bundle(base: Path, wave: int) -> list[dict] | None:
    """波 n の行。waves.json が無い・波が無いときは None。"""
    pbis = wave_pbis(base, wave)
    if not pbis:
        return None
    rows = []
    for pbi in pbis:
        if not WAVE_PBI_NAME.match(pbi):
            # 絶対パスや glob の文字を含む名前は glob に渡さず、その行の誤りにする
            row = {'report': pbi, 'pbi': pbi, 'errors': [f'invalid PBI name in waves.json: {pbi!r}']}
        else:
            row = bundle_row(base, pbi)
        row['line'] = row_line(row)
        rows.append(row)
    return rows


def main(argv=None) -> int:
    common.utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest='command', required=True)
    p_check = sub.add_parser('check')
    p_check.add_argument('files', nargs='+')
    p_bundle = sub.add_parser('bundle')
    p_bundle.add_argument('--wave', type=int, required=True)
    p_bundle.add_argument('--out', required=True)
    p_bundle.add_argument('--dir', default='docs/pbi')
    args = ap.parse_args(argv)

    if args.command == 'check':
        bad = False
        for name in args.files:
            errors = check_file(name)
            bad = bad or bool(errors)
            print(f"{name}: {'ok' if not errors else '; '.join(errors)}")
        return EXIT_INVALID if bad else EXIT_OK

    base = Path(args.dir)
    rows = bundle(base, args.wave)
    if rows is None:
        print(f'role_results: wave {args.wave} is not in {(base / "waves.json").as_posix()}', file=sys.stderr)
        return EXIT_USAGE
    # 束は引き継ぎ用の写し。schema を持たせず、check の対象にしない
    try:
        common.write_json(args.out, {'wave': args.wave, 'rows': rows})
    except OSError as error:
        print(f'role_results: cannot write {Path(args.out).as_posix()}: {error.strerror or error}', file=sys.stderr)
        return EXIT_USAGE
    for row in rows:
        print(row['line'])
    print(f'bundle: {Path(args.out).as_posix()}')
    return EXIT_INVALID if any(row['errors'] for row in rows) else EXIT_OK


if __name__ == '__main__':
    sys.exit(main())
