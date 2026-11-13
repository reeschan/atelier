"""yomiyasu_lint.py の偽物。試験が本物の yomiyasu に頼らないために、決まった形の JSON を返す。

使い方は本物と同じ: fake_yomiyasu.py [file|-] --json [--no-words]
渡された文のうち、囲み (``` で始まる行から次の ``` まで。本物と同じく ```ask も) の外の行に目印
`ヨミヤス候補` があれば、その行 (渡された文の 1 始まり) に候補を 1 つ返す。出力は本物と同じく ensure_ascii=True。

環境変数:
  FAKE_YOMIYASU_MODE   crash    終了コード 3 で終わる
                       sleep    10 秒眠る (時間切れの試験)
                       garbage  JSON でない出力を返す
                       format3  format_version を 3 にする
  FAKE_YOMIYASU_DUMP   受け取った文 (UTF-8) と引数をこのファイルに書く (呼ばれたか・何を渡したかの確認)
"""
import json
import os
import sys
import time

MARKER = 'ヨミヤス候補'


def main(argv) -> int:
    args = [a for a in argv if not a.startswith('--')]
    source = args[0] if args else '-'
    if source == '-':
        text = sys.stdin.buffer.read().decode('utf-8')
    else:
        with open(source, 'rb') as stream:
            text = stream.read().decode('utf-8')
    dump = os.environ.get('FAKE_YOMIYASU_DUMP')
    if dump:
        with open(dump, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(json.dumps({'argv': argv, 'text': text}, ensure_ascii=False))
    mode = os.environ.get('FAKE_YOMIYASU_MODE', '')
    if mode == 'crash':
        print('fake yomiyasu crashed', file=sys.stderr)
        return 3
    if mode == 'sleep':
        time.sleep(10)
    if mode == 'garbage':
        print('not json at all')
        return 0
    findings = []
    in_fence = False
    for number, line in enumerate(text.replace('\r\n', '\n').split('\n'), 1):
        if line.lstrip().startswith('```'):
            in_fence = not in_fence
            continue
        if not in_fence and MARKER in line:
            findings.append({'rule': 'fake_rule', 'line': number, 'severity': 'warn',
                             'message': f'{MARKER}を言い直す', 'snippet': line.strip()})
    result = {'format': 'yomiyasu-lint', 'format_version': 3 if mode == 'format3' else 2, 'tool_version': 'fake',
              'candidate_count': len(findings), 'findings': findings}
    sys.stdout.write(json.dumps(result, ensure_ascii=True) + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
