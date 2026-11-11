"""PBI-002 の受け入れ: conductor.py の start / next / done / answer / status を、結果のファイルを手で書いて確かめる。

python docs/pbi/tools/conductor_smoke.py   (リポジトリのルートで)
1 行 1 項目の PASS / FAIL を出し、全部通れば終了コード 0。
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CON = ROOT / 'skills' / 'atelier' / 'conductor.py'
FAILS = []


def con(cwd, *args):
    env = dict(os.environ, ATELIER_HOME=str(Path(cwd) / 'home'), PYTHONIOENCODING='utf-8')
    p = subprocess.run([sys.executable, str(CON), *args, '--dir', 'docs/pbi'], cwd=cwd, capture_output=True,
                       text=True, encoding='utf-8', env=env)
    return p.returncode, p.stdout + p.stderr


def check(name, ok, detail=''):
    print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else f'  [{detail.strip()[:400]}]'))
    if not ok:
        FAILS.append(name)


def line_value(out, key):
    for line in out.splitlines():
        if line.startswith(key + ':'):
            return line.split(':', 1)[1].strip()
    return ''


def result(cwd, out, **fields):
    """next の出力から結果のパスと seq を読み、出番の結果を書く。"""
    path = Path(cwd) / line_value(out, 'result')
    episode, _, seq = line_value(out, 'episode').partition(' seq ')
    data = {'schema': 'atelier.episode/1', 'episode': episode, 'seq': int(seq), 'summary': ['smoke']}
    data.update(fields)
    path.write_text(json.dumps(data), encoding='utf-8')


def main():
    with tempfile.TemporaryDirectory() as tmp:
        rc, out = con(tmp, 'start', '--goal', 'smoke goal')
        check('start', rc == 0 and out.startswith('state: ready'), out)
        rc, out = con(tmp, 'start', '--goal', 'again')
        check('start refuses a run in progress', rc == 2, out)

        rc, out = con(tmp, 'next')
        check('next design seq 1', rc == 0 and out.startswith('state: running')
              and line_value(out, 'episode') == 'design seq 1'
              and line_value(out, 'prompt') == 'docs/pbi/conductor/episodes/1-design.prompt.md', out)
        prompt = (Path(tmp) / 'docs/pbi/conductor/episodes/1-design.prompt.md').read_text(encoding='utf-8')
        check('prompt filled', 'Episode: design (seq 1)' in prompt and '{' not in prompt
              and 'Continue from:' not in prompt and 'Answer:' not in prompt and 'smoke goal' not in prompt
              and 'docs/pbi/conductor/goal.md' in prompt, prompt)
        check('launch line', 'atelier-designer' in out and 'ATELIER.1 design' in out, out)
        rc, out = con(tmp, 'next')
        check('next is idempotent while running', rc == 0 and line_value(out, 'episode') == 'design seq 1', out)

        result(tmp, out, status='question', question={'kind': 'decision', 'path': 'docs/pbi/notes/q.md',
                                                         'summary': 'run the design?'})
        rc, out = con(tmp, 'done')
        check('done question', rc == 10 and out.startswith('state: question') and 'decision' in out, out)
        rc, out = con(tmp, 'next')
        check('next while a question waits', rc == 10, out)
        rc, out = con(tmp, 'answer', '--text', 'run it')
        check('answer text', rc == 0 and out.startswith('state: ready'), out)
        rc, out = con(tmp, 'answer', '--text', 'twice')
        check('answer without a question', rc == 2, out)

        rc, out = con(tmp, 'next')
        p2 = (Path(tmp) / line_value(out, 'prompt')).read_text(encoding='utf-8')
        check('relaunch with Answer', line_value(out, 'episode') == 'design seq 2'
              and 'Answer: docs/pbi/conductor/answers/' in p2, out + p2)
        result(tmp, out, status='done', next={'episode': 'build', 'wave': 1})
        rc, out = con(tmp, 'done')
        check('done -> build 1', rc == 0 and out.startswith('state: ready') and 'build' in out, out)

        rc, out = con(tmp, 'next')
        p3 = (Path(tmp) / line_value(out, 'prompt')).read_text(encoding='utf-8')
        check('build wave line', line_value(out, 'episode') == 'build seq 3' and 'Wave: 1' in p3, out + p3)
        (Path(tmp) / 'docs/pbi/CONTEXT_STATE.md').write_text('state\n', encoding='utf-8')
        for n in (1, 2):
            result(tmp, out, status='handoff', state='docs/pbi/CONTEXT_STATE.md')
            rc, done_out = con(tmp, 'done')
            check(f'handoff {n} relaunches', rc == 0 and done_out.startswith('state: ready'), done_out)
            rc, out = con(tmp, 'next')
            pn = (Path(tmp) / line_value(out, 'prompt')).read_text(encoding='utf-8')
            check(f'continue from {n}', 'Continue from: docs/pbi/CONTEXT_STATE.md' in pn and 'Wave: 1' in pn, pn)
        result(tmp, out, status='handoff', state='docs/pbi/CONTEXT_STATE.md')
        rc, out = con(tmp, 'done')
        check('third handoff in a row asks', rc == 10 and 'handoff-loop' in out, out)
        rc, out = con(tmp, 'answer', '--text', 'go on')
        check('answer handoff-loop', rc == 0, out)

        rc, out = con(tmp, 'next')
        result(tmp, out, status='done', next={'episode': 'design'})
        rc, out = con(tmp, 'done')
        check('build -> design is refused', rc == 12 and out.startswith('state: failed'), out)
        rc, out = con(tmp, 'next')
        check('next relaunches after a failure', rc == 0 and out.startswith('state: running'), out)
        rc, out = con(tmp, 'done')
        check('second failure in a row asks', rc == 10 and 'launch-failure' in out, out)
        rc, out = con(tmp, 'answer', '--text', 'retry')
        check('answer launch-failure', rc == 0, out)

        rc, out = con(tmp, 'next')
        result(tmp, out, status='done', next={'episode': 'finish'})
        rc, out = con(tmp, 'done')
        rc, out = con(tmp, 'next')
        check('finish episode', line_value(out, 'episode').startswith('finish'), out)
        result(tmp, out, status='done', next=None, report='docs/pbi/report.md')
        rc, out = con(tmp, 'done')
        check('finished', rc == 0 and out.startswith('state: finished') and 'docs/pbi/report.md' in out, out)
        rc, out = con(tmp, 'next')
        check('next after finish', rc == 0 and out.startswith('state: finished'), out)
        rc, out = con(tmp, 'status')
        check('status', rc == 0 and out.startswith('state: finished') and len(out.splitlines()) <= 8, out)
        run = json.loads((Path(tmp) / 'docs/pbi/conductor/run.json').read_text(encoding='utf-8'))
        check('run.json schema and history', run.get('schema') == 'atelier.run/1' and len(run.get('history', [])) >= 9,
              json.dumps(run)[:300])

    with tempfile.TemporaryDirectory() as tmp:
        con(tmp, 'start', '--goal', 'approve')
        rc, out = con(tmp, 'next')
        (Path(tmp) / 'docs/pbi/notes').mkdir(parents=True, exist_ok=True)
        (Path(tmp) / 'docs/pbi/notes/approve.md').write_text('# PBI list\n', encoding='utf-8')
        result(tmp, out, status='question', question={'kind': 'approve', 'path': 'docs/pbi/notes/approve.md',
                                                         'summary': 'approve 3 PBIs', 'check': 'design-record'})
        rc, out = con(tmp, 'done')
        check('approve question', rc == 10 and 'approve' in out, out)
        rc, out = con(tmp, 'answer', '--text', 'ok')
        check('approve reply without a fence stays open', rc == 10 and 'check-reply' in out and '4' in out, out)
        rc, out = con(tmp, 'next')
        check('approve still waits', rc == 10, out)

        lock = Path(tmp) / 'docs/pbi/conductor/lock'
        lock.write_text(str(os.getpid()), encoding='utf-8')
        rc, out = con(tmp, 'answer', '--text', 'x')
        check('live lock refuses', rc == 2 and 'lock' in out.lower(), out)
        lock.write_text('999999999', encoding='utf-8')
        rc, out = con(tmp, 'status')
        check('stale lock is taken over', rc in (0, 10), out)

    print(f'{len(FAILS)} failed' if FAILS else 'all passed')
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
