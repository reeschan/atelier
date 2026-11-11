"""テスト用の偽の jevlint。lint_run.py・tools_check.py のテストが home の agents.json の tools.jevlint.path で指す。

ファイルの中の行で結果を決める: `score: N` (点、既定 90)、`fn: N` (その長さの関数)、`bug: X` (bug-hunt の合致、既定 0.9)。
呼ばれるたびに FAKE_JEVLINT_LOG に {"args", "cwd", "path0" (PATH の先頭)} を 1 行足す。FAKE_JEVLINT_KEYS が空なら auth list に typesafe を出さない。
"""
import json
import os
import re
import sys


def log(args):
    path = os.environ.get('FAKE_JEVLINT_LOG')
    if path:
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'args': args, 'cwd': os.getcwd(),
                                'path0': os.environ.get('PATH', '').split(os.pathsep)[0]}) + '\n')


def report(path, ast_only):
    text = open(path, encoding='utf-8').read()
    score = int((re.search(r'score: (\d+)', text) or [None, 90])[1])
    findings = [{'rule': 'function-lines', 'message': f'length {n} (max 60)', 'value': int(n), 'limit': 60, 'line': 1}
                for n in re.findall(r'fn: (\d+)', text)]
    entry = {'path': path.replace('\\', '/'), 'language': 'typescript', 'lines': len(text.splitlines()), 'score': score,
             'ast': {'score': 0.5, 'findings': findings}}
    if not ast_only:
        bug = float((re.search(r'bug: ([\d.]+)', text) or [None, 0.9])[1])
        entry['model'] = {'answers': [
            {'id': 'bug-hunt.swallowed-error', 'title': 'errors are not swallowed', 'compliance': bug, 'weight': 2,
             'fix': 'rethrow or report the error'},
            {'id': 'documentation.contracts-stated', 'title': 'contracts', 'compliance': 0.1, 'weight': 1, 'fix': 'doc'},
        ]}
    return entry


def main(args):
    log(args)
    if args[:1] == ['--version']:
        print('0.0.0-fake')
        return 0
    if args[:2] == ['auth', 'list']:
        print('typesafe  (key set)' if os.environ.get('FAKE_JEVLINT_KEYS', '1') else 'no keys')
        return 0
    if args[:1] != ['audit']:
        print(f'unexpected command {args}', file=sys.stderr)
        return 9
    files = [a for a in args[1:] if not a.startswith('--')]
    if '--dry-run' in args:
        for f in files:
            print(f'  {f}')
        print(f'\n {len(files)} files, about {1000 * len(files):,} tokens (about ${0.01 * len(files):.4f}), '
              'would be sent to typesafe.')
        return 0
    ast_only = '--ast-only' in args
    print(json.dumps({'root': os.getcwd(), 'provider': 'typesafe', 'score': 80,
                      'files': [report(f, ast_only) for f in files],
                      'usage': {'inputTokens': 0 if ast_only else 1000 * len(files), 'estimatedCostUsd': 0.0,
                                'modelCalls': 0 if ast_only else len(files), 'cacheHits': 0}}))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
