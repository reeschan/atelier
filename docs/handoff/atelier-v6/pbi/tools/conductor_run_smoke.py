"""PBI-003 の受け入れ: conductor.py run (headless) を偽の設計者 (ATELIER_DESIGNER_CMD) で確かめる。
python docs/pbi/tools/conductor_run_smoke.py   (リポジトリのルートで)。全部通れば終了コード 0。"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CON = ROOT / 'skills' / 'atelier' / 'conductor.py'
FAKE = Path(__file__).resolve().parent / 'fake_designer.py'
FAILS = []


def check(name, ok, detail=''):
    print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else f'  [{detail.strip()[:400]}]'))
    if not ok:
        FAILS.append(name)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / 'fake.log'
        env = dict(os.environ, ATELIER_HOME=str(Path(tmp) / 'home'), PYTHONIOENCODING='utf-8',
                   ATELIER_DESIGNER_CMD=json.dumps([sys.executable, str(FAKE)]), FAKE_DESIGNER_LOG=str(log),
                   ANTHROPIC_API_KEY='must-not-leak')

        def con(*args):
            p = subprocess.run([sys.executable, str(CON), *args, '--dir', 'docs/pbi'], cwd=tmp, env=env,
                               capture_output=True, text=True, encoding='utf-8')
            return p.returncode, p.stdout + p.stderr

        rc, out = con('start', '--goal', 'headless', '--launch', 'headless')
        check('start headless', rc == 0, out)
        rc, out = con('run')
        check('run stops at the question', rc == 10 and out.startswith('state: question')
              and len(out.splitlines()) <= 8, out)
        args = json.loads(log.read_text(encoding='utf-8').splitlines()[0])['args']
        check('claude -p arguments', '-p' in args and '--agent' in args and 'atelier-designer' in args
              and '--permission-prompts' in args and '--session-id' in args and '--output-format' in args, args)
        out_json = Path(tmp) / 'docs/pbi/conductor/episodes/1-design.json'
        check('claude output kept', out_json.is_file(), str(out_json))
        rc, out = con('answer', '--text', 'yes')
        check('answer', rc == 0, out)
        rc, out = con('run', '--max-episodes', '1')
        check('max-episodes stops after one', rc == 0 and out.startswith('state: ready'), out)
        rc, out = con('run')
        check('run to the end', rc == 0 and out.startswith('state: finished'), out)
        run = json.loads((Path(tmp) / 'docs/pbi/conductor/run.json').read_text(encoding='utf-8'))
        costs = [h.get('cost_usd') for h in run['history']]
        sessions = [h.get('session_id') for h in run['history']]
        check('cost and session in history', costs.count(0.25) == 4 and 'fake-session' in sessions, run['history'])
        rc, out = con('status')
        check('status shows the cost total', '1.00' in out, out)

        env['ATELIER_DESIGNER_CMD'] = json.dumps([sys.executable, '-c', 'import sys; sys.exit(3)'])
        con('start', '--goal', 'broken', '--launch', 'headless', '--force')
        rc, out = con('run')
        check('a designer that writes nothing ends as a question after two tries',
              rc == 10 and 'launch-failure' in out, out)

    print(f'{len(FAILS)} failed' if FAILS else 'all passed')
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
