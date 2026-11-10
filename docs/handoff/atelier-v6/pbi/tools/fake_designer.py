"""PBI-003 の受け入れ: claude -p の代わりの偽の設計者。標準入力の依頼文から Episode・Result・Answer を読み、
出番の結果を書いて、claude -p --output-format json に似た 1 行を出す。受け取った引数を FAKE_DESIGNER_LOG に追記する。"""
import json
import os
import re
import sys
from pathlib import Path

prompt = sys.stdin.read()
with open(os.environ['FAKE_DESIGNER_LOG'], 'a', encoding='utf-8') as log:
    log.write(json.dumps({'args': sys.argv[1:]}) + '\n')
episode, seq = re.search(r'^Episode: (\w+) \(seq (\d+)\)', prompt, re.M).groups()
result_path = re.search(r'^Result: .*?write (\S+)', prompt, re.M).group(1)
data = {'schema': 'atelier.episode/1', 'episode': episode, 'seq': int(seq), 'summary': [f'fake {episode}']}
if episode == 'design' and not re.search(r'^Answer: ', prompt, re.M):
    data.update(status='question', question={'kind': 'decision', 'path': 'docs/pbi/notes/q.md', 'summary': 'go?'})
elif episode == 'design':
    data.update(status='done', next={'episode': 'build', 'wave': 1})
elif episode == 'build':
    data.update(status='done', next={'episode': 'finish'})
else:
    data.update(status='done', next=None, report='docs/pbi/report.md')
Path(result_path).parent.mkdir(parents=True, exist_ok=True)
Path(result_path).write_text(json.dumps(data), encoding='utf-8')
print(json.dumps({'type': 'result', 'is_error': False, 'session_id': 'fake-session', 'total_cost_usd': 0.25,
                  'result': 'fake'}))
