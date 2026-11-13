"""テストを複数のプロセスに分けて流す (標準ライブラリだけ)。`python -m unittest discover` と同じテストを、速く流す。

  python run_tests.py [-s DIR] [-p 'test_*.py'] [-j N] [--chunk K]

テストを discover で集め、テストの関数を K 件ずつの束に分けて、N 個のプロセスに順に配る。
多くのテストが os.environ や作業のフォルダを書き換えるので、スレッドではなくプロセスで分ける。
1 つのプロセスの中では束を 1 つずつ流すので、ふつうの直列の実行と同じ前提で動く。

終了コード: 0 すべて通った / 1 落ちた・誤りがあった・テストが 0 件 / 2 引数の誤り。
"""
import argparse
import io
import os
import sys
import time
import unittest
from concurrent.futures import ProcessPoolExecutor, as_completed

DEFAULT_CHUNK = 4


def test_ids(suite) -> tuple:
    """(id の並び, その場で流すテスト)。読み込めなかったモジュール (_FailedTest) は id で読み直せないので、親で流す。"""
    ids, local = [], []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            more_ids, more_local = test_ids(item)
            ids += more_ids
            local += more_local
        elif isinstance(item, unittest.loader._FailedTest):
            local.append(item)
        else:
            ids.append(item.id())
    return ids, local


def chunks(ids: list, size: int) -> list:
    return [ids[i:i + size] for i in range(0, len(ids), size)]


def summarize(result: unittest.TestResult) -> dict:
    """プロセスの間で渡せる形の結果。"""
    return {'run': result.testsRun,
            'failures': [(str(test), text) for test, text in result.failures],
            'errors': [(str(test), text) for test, text in result.errors],
            'skipped': len(result.skipped),
            'expected_failures': len(result.expectedFailures),
            'unexpected_successes': [str(test) for test in result.unexpectedSuccesses]}


def init_worker(start: str) -> None:
    os.chdir(start)
    if start not in sys.path:
        sys.path.insert(0, start)


def run_chunk(names: list) -> dict:
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    return summarize(result)


def merge(total: dict, part: dict) -> None:
    total['run'] += part['run']
    total['failures'] += part['failures']
    total['errors'] += part['errors']
    total['skipped'] += part['skipped']
    total['expected_failures'] += part['expected_failures']
    total['unexpected_successes'] += part['unexpected_successes']


def report(total: dict, seconds: float, stream=sys.stderr) -> int:
    line = '=' * 70
    for label, rows in (('ERROR', total['errors']), ('FAIL', total['failures'])):
        for name, text in rows:
            stream.write(f'{line}\n{label}: {name}\n{"-" * 70}\n{text}\n')
    stream.write(f'{"-" * 70}\nRan {total["run"]} tests in {seconds:.3f}s\n\n')
    extras = [f'{key}={value}' for key, value in (
        ('failures', len(total['failures'])), ('errors', len(total['errors'])), ('skipped', total['skipped']),
        ('expected failures', total['expected_failures']),
        ('unexpected successes', len(total['unexpected_successes']))) if value]
    ok = not total['failures'] and not total['errors'] and not total['unexpected_successes']
    word = 'OK' if ok else 'FAILED'
    stream.write(f'{word} ({", ".join(extras)})\n' if extras else f'{word}\n')
    if total['run'] == 0:
        stream.write('no tests ran\n')
        return 1
    return 0 if ok else 1


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('-s', '--start', default='.', help='テストを探すフォルダ (既定 .)')
    ap.add_argument('-p', '--pattern', default='test_*.py', help="テストのファイル名 (既定 'test_*.py')")
    ap.add_argument('-j', '--jobs', type=int, default=os.cpu_count() or 1, help='プロセスの数 (既定 CPU の数)')
    ap.add_argument('--chunk', type=int, default=DEFAULT_CHUNK, help=f'1 つの束のテストの数 (既定 {DEFAULT_CHUNK})')
    args = ap.parse_args(argv)
    if args.jobs < 1 or args.chunk < 1:
        ap.error('--jobs and --chunk must be 1 or more')
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    start = os.path.abspath(args.start)
    began = time.perf_counter()
    init_worker(start)
    ids, local = test_ids(unittest.defaultTestLoader.discover(start, pattern=args.pattern, top_level_dir=start))
    total = {'run': 0, 'failures': [], 'errors': [], 'skipped': 0, 'expected_failures': 0, 'unexpected_successes': []}
    if local:
        merge(total, summarize(unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(unittest.TestSuite(local))))
    parts = chunks(ids, args.chunk)
    jobs = min(args.jobs, len(parts)) or 1
    with ProcessPoolExecutor(max_workers=jobs, initializer=init_worker, initargs=(start,)) as pool:
        futures = {pool.submit(run_chunk, part): part for part in parts}
        for future in as_completed(futures):
            try:
                merge(total, future.result())
            except Exception as error:  # 束のプロセスが落ちた: その束のテストを誤りとして数える
                for name in futures[future]:
                    total['run'] += 1
                    total['errors'].append((name, f'worker failed: {error!r}'))
    return report(total, time.perf_counter() - began)


if __name__ == '__main__':
    sys.exit(main())
