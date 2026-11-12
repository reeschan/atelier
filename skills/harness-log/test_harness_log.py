"""harness_log.py の CLI と関数を、一時の設定・一時の home・手元の bare リポジトリで通しで確かめる。"""
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HL = HERE / 'harness_log.py'
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
sys.path.insert(0, str(HERE))
import harness_log as hl  # noqa: E402

REMOTE_URL = 'https://example.com/acme/widget.git'
SCHEMA = {
    'schema': 'harness.allowlist/1', 'skill': 'demo', 'data_schema': 'demo.event/1', 'phase': 'test',
    'types': {
        'thing.done': {'fields': {
            'n': {'type': 'int'}, 'name': {'type': 'id'}, 'who': {'type': 'hash'},
            'meta': {'type': 'object', 'fields': {'ok': {'type': 'bool'}}},
            'tags': {'type': 'list', 'items': {'type': 'enum', 'values': ['a', 'b']}, 'max': 2},
            'roles': {'type': 'map', 'keys': {'type': 'id'}, 'values': {'type': 'number'}},
        }},
        'task.profiled': {'fields': {
            'scope': {'type': 'enum', 'values': ['run', 'unit']}, 'task_type': {'type': 'id'},
            'domain': {'type': 'id'}, 'case_id': {'type': 'hash'},
            'size': {'type': 'object', 'fields': {'files': {'type': 'id'}, 'lines': {'type': 'id'}}},
        }},
    },
}


def _force_rm(func, path, _exc):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def rmtree(path):
    if Path(path).exists():
        shutil.rmtree(path, onerror=_force_rm)


class HarnessLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='hl-test-'))
        self.home = self.tmp / 'home'
        self.home.mkdir()
        gitcfg = self.tmp / 'gitconfig'
        gitcfg.write_text('', encoding='utf-8')
        self.cfg = self.tmp / 'harness-log.json'
        self.env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home),
                        HARNESS_LOG_CONFIG=str(self.cfg), GIT_TERMINAL_PROMPT='0',
                        GIT_CONFIG_GLOBAL=str(gitcfg), GIT_CONFIG_NOSYSTEM='1')
        self.bare = self.tmp / 'remote.git'
        self.git('init', '-q', '--bare', str(self.bare))
        self.work = self.tmp / 'work'
        self.git('init', '-q', str(self.work))
        self.git('remote', 'add', 'origin', REMOTE_URL, cwd=self.work)
        self.schema = self.tmp / 'allow.json'
        self.schema.write_text(json.dumps(SCHEMA), encoding='utf-8')
        self.checks = 0

    def tearDown(self):
        rmtree(self.tmp)

    # ---- helpers ----
    def git(self, *args, cwd=None):
        return subprocess.run(['git', *args], cwd=str(cwd) if cwd else None, env=self.env, capture_output=True,
                              text=True, encoding='utf-8', errors='replace', creationflags=NO_WINDOW)

    def run_hl(self, *args, cwd=None, env=None):
        return subprocess.run([sys.executable, str(HL), *args], cwd=str(cwd or self.work), env=env or self.env,
                              capture_output=True, text=True, encoding='utf-8', errors='replace',
                              creationflags=NO_WINDOW)

    def init(self, *show):
        extra = [x for s in show for x in ('--show-repo', s)]
        r = self.run_hl('init', '--remote', str(self.bare), *extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(self.cfg.read_text(encoding='utf-8'))

    def emit(self, data, *extra, type_='thing.done', cwd=None):
        r = self.run_hl('emit', '--schema', str(self.schema), '--type', type_, '--data', json.dumps(data), *extra,
                        cwd=cwd)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def spool(self):
        return Path(json.loads(self.cfg.read_text(encoding='utf-8'))['spool_dir'])

    def spool_files(self):
        return hl._spool_files(self.spool())

    def spool_lines(self):
        return [json.loads(ln) for f in self.spool_files() for ln in f.read_text(encoding='utf-8').splitlines()]

    def bare_commits(self):
        r = self.git('rev-list', '--all', '--count', cwd=self.bare)
        return int(r.stdout.strip() or 0) if r.returncode == 0 else 0

    def bare_files(self):
        self.checks += 1
        dest = self.tmp / f'check{self.checks}'
        self.git('clone', '-q', str(self.bare), str(dest))
        return {f.relative_to(dest).as_posix(): f.read_text(encoding='utf-8')
                for f in dest.rglob('*') if f.is_file() and '.git' not in f.relative_to(dest).parts}

    def hash_cli(self, value):
        r = self.run_hl('hash', value)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    # ---- tests ----
    def test_no_config_does_nothing(self):
        self.assertEqual(self.emit({'n': 1}).returncode, 0)
        r = self.run_hl('flush')
        self.assertEqual(r.returncode, 0)
        self.assertFalse(self.cfg.exists())
        self.assertFalse((self.home / '.claude').exists())
        self.assertIsNone(hl.emit(self.schema, 'thing.done', {'n': 1}, config_path=self.cfg))
        self.assertFalse(hl.flush(config_path=self.cfg))
        self.assertEqual(self.bare_commits(), 0)

    def test_init_creates_disabled_config_key_and_clone_without_push(self):
        cfg = self.init('acme/*')
        self.assertIs(cfg['enabled'], False)
        self.assertRegex(cfg['key'], r'^[0-9a-f]{64}$')
        self.assertEqual(cfg['show_repos'], ['acme/*'])
        self.assertTrue((Path(cfg['clone_dir']) / '.git').is_dir())
        self.assertTrue(Path(cfg['clone_dir']).is_relative_to(self.home))
        self.assertEqual(self.bare_commits(), 0)
        again = self.run_hl('init', '--remote', str(self.bare))
        self.assertEqual(again.returncode, 2)
        self.assertEqual(json.loads(self.cfg.read_text(encoding='utf-8'))['key'], cfg['key'])

    def test_emit_drops_unlisted_values_from_spool_and_remote(self):
        self.init()
        self.emit({'n': 3, 'secret': 'TOPSECRET-VALUE-42', 'meta': {'ok': True, 'path': 'TOPSECRET-NESTED'}})
        raw = ''.join(f.read_text(encoding='utf-8') for f in self.spool_files())
        self.assertNotIn('TOPSECRET', raw)
        [line] = self.spool_lines()
        self.assertEqual(line['data'], {'n': 3, 'meta': {'ok': True}})
        self.assertEqual(line['dropped'], ['secret', 'meta.path'])
        self.assertEqual(self.run_hl('enable').returncode, 0)
        self.assertEqual(self.run_hl('flush').returncode, 0)
        files = self.bare_files()
        self.assertTrue(files)
        self.assertEqual(sum(t.count('TOPSECRET') for t in files.values()), 0)
        r = self.git('log', '--all', '-p', cwd=self.bare)
        self.assertNotIn('TOPSECRET', r.stdout)

    def test_disabled_flush_adds_no_commit(self):
        self.init()
        self.emit({'n': 1})
        r = self.run_hl('flush')
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.bare_commits(), 0)
        self.assertEqual(len(self.spool_lines()), 1)

    def test_enabled_flush_pushes_one_commit_and_clears_spool(self):
        self.init()
        self.emit({'n': 1}, '--run-id', 'r1', '--seq', '1')
        self.emit({'scope': 'run', 'task_type': 'feature', 'domain': 'cli', 'size': {'files': '2-3', 'lines': '1-50'}},
                  '--run-id', 'r1', '--seq', '2',
                  type_='task.profiled')
        self.emit({'scope': 'unit', 'task_type': 'fix'}, '--run-id', 'r1', type_='task.profiled')
        month = self.spool_lines()[0]['ts'][:7]
        self.run_hl('enable')
        r = self.run_hl('flush')
        self.assertEqual((r.returncode, r.stderr), (0, ''))
        self.assertEqual(self.bare_commits(), 1)
        files = self.bare_files()
        self.assertEqual(set(files), {f'test/demo/{month}/r1.jsonl', 'test/demo/coverage.json', 'SCHEMA.md'})
        self.assertEqual(len(files[f'test/demo/{month}/r1.jsonl'].splitlines()), 3)
        cov = json.loads(files['test/demo/coverage.json'])
        self.assertEqual(cov['rows'], [{'task_type': 'feature', 'domain': 'cli', 'size': '2-3', 'count': 1}])
        self.assertIn('<!-- skill:demo -->', files['SCHEMA.md'])
        for key in ('split_set_id', 'unit_id', 'dropped'):
            self.assertIn(f'`{key}`', files['SCHEMA.md'])
        self.assertEqual(self.spool_files(), [])

    def test_push_failure_keeps_exit_zero_and_resends_once(self):
        self.init()
        self.run_hl('enable')
        self.emit({'name': 'marker-one'}, '--run-id', 'r1')
        self.run_hl('flush')
        self.emit({'name': 'marker-two'}, '--run-id', 'r1')
        rmtree(self.bare)
        r = self.run_hl('flush')
        self.assertEqual(r.returncode, 0)
        self.assertEqual(len([ln for ln in r.stderr.splitlines() if ln.strip()]), 1, r.stderr)
        # remote に届かないときは古い土台にコミットせず、行をスプールに残して errors.log に 1 行
        self.assertEqual([ln['data']['name'] for ln in self.spool_lines()], ['marker-two'])
        errors = (self.spool() / 'errors.log').read_text(encoding='utf-8').splitlines()
        self.assertEqual(len(errors), 1)
        self.assertIn('pull failed', errors[0])
        clone = Path(json.loads(self.cfg.read_text(encoding='utf-8'))['clone_dir'])
        self.assertNotIn('marker-two', self.git('log', '--all', '-p', cwd=clone).stdout)
        pv = self.run_hl('preview')
        self.assertIn('marker-two', pv.stdout)
        self.git('init', '-q', '--bare', str(self.bare))
        r = self.run_hl('flush')
        self.assertEqual((r.returncode, r.stderr), (0, ''))
        text = ''.join(t for p, t in self.bare_files().items() if p.endswith('.jsonl'))
        self.assertEqual(text.count('marker-one'), 1)
        self.assertEqual(text.count('marker-two'), 1)
        self.assertEqual(self.bare_commits(), 2)
        self.run_hl('flush')  # 送る物が無い flush で二度入らない
        text = ''.join(t for p, t in self.bare_files().items() if p.endswith('.jsonl'))
        self.assertEqual((text.count('marker-one'), text.count('marker-two')), (1, 1))

    def host(self, name):
        """同じ鍵と remote で、スプールと clone だけが別の 2 台目の設定を作り、その env を返す。"""
        cfg = json.loads(self.cfg.read_text(encoding='utf-8'))
        cfg.update(spool_dir=str(self.tmp / name / 'spool'), clone_dir=str(self.tmp / name / 'clone'))
        path = self.tmp / f'{name}.json'
        path.write_text(json.dumps(cfg), encoding='utf-8')
        return dict(self.env, HARNESS_LOG_CONFIG=str(path)), cfg

    def test_two_hosts_flush_after_remote_moves_and_comes_back(self):
        self.init()
        self.run_hl('enable')
        a = self.env
        b, cfg_b = self.host('b')
        sent = []

        def step(env, name, ok=True):
            data = json.dumps({'scope': 'run', 'task_type': name, 'domain': 'cli'})
            r = self.run_hl('emit', '--schema', str(self.schema), '--type', 'task.profiled', '--data', data,
                            '--run-id', name, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            r = self.run_hl('flush', env=env)
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stderr == '', ok, (name, r.stderr))
            sent.append(name)

        step(a, 'a1')
        step(b, 'b1')
        away = self.tmp / 'away.git'
        os.replace(self.bare, away)
        step(b, 'b2', ok=False)
        os.replace(away, self.bare)
        step(a, 'a2')
        for i in range(3, 6):
            step(b, f'b{i}')
            step(a, f'a{i}')
        files = self.bare_files()
        text = ''.join(t for p, t in files.items() if p.endswith('.jsonl'))
        for name in sent:
            self.assertEqual(text.count(f'"task_type":"{name}"'), 1, name)
        rows = json.loads(files['test/demo/coverage.json'])['rows']
        self.assertEqual(sorted(r['task_type'] for r in rows), sorted(sent))
        status = self.git('status', '-sb', cwd=cfg_b['clone_dir']).stdout.splitlines()[0]
        self.assertNotIn('ahead', status)
        self.assertNotIn('behind', status)
        self.assertEqual(hl._spool_files(Path(cfg_b['spool_dir'])), [])

    def test_coverage_counts_a_run_once_and_uses_size_bucket(self):
        d = self.tmp / 'clone' / 'test' / 'demo' / '2026-01'
        d.mkdir(parents=True)

        def ev(run, files, case=None):
            data = {'scope': 'run', 'task_type': 'feature', 'domain': 'cli', 'size': {'files': files}}
            if case:
                data['case_id'] = case
            return json.dumps({'type': 'task.profiled', 'run_id': run, 'data': data})

        (d / 'r1.jsonl').write_text('\n'.join([ev('r1', '1'), ev('r1', '1'), ev('r1', '2-3')]) + '\n',
                                    encoding='utf-8')
        (d / 'adhoc.jsonl').write_text('\n'.join([ev(None, '4-7', 'h:aaa'), ev(None, '4-7', 'h:aaa'),
                                                  ev(None, '4-7', 'h:bbb')]) + '\n', encoding='utf-8')
        cov = hl._coverage(self.tmp / 'clone', 'test', 'demo')
        self.assertEqual(cov['rows'], [
            {'task_type': 'feature', 'domain': 'cli', 'size': '2-3', 'count': 1},
            {'task_type': 'feature', 'domain': 'cli', 'size': '4-7', 'count': 2},
        ])
        self.assertEqual(hl._axis({'size_bucket': 'L', 'files': '1'}), 'L')
        self.assertEqual(hl._axis({'lines': '1'}), 'unknown')

    def test_long_hash_value_is_hashed_not_dropped(self):
        long = 'atelier-metrics:atelier-metrics:feature/harness-metrics:W-1:PBI-12'
        self.assertGreater(len(long), 64)
        clean, dropped = hl.check(SCHEMA, 'task.profiled', {'case_id': long})
        self.assertEqual((clean, dropped), ({'case_id': long}, []))
        self.init()
        line = hl.emit(self.schema, 'task.profiled', {'case_id': long, 'scope': 'run'}, config_path=self.cfg,
                       cwd=self.work)
        self.assertEqual(line['data']['case_id'], self.hash_cli(long))
        self.assertRegex(line['data']['case_id'], r'^h:[0-9a-f]{12}$')
        self.assertNotIn(long, ''.join(f.read_text(encoding='utf-8') for f in self.spool_files()))
        self.assertEqual(hl.check(SCHEMA, 'task.profiled', {'case_id': ''})[1], ['case_id'])

    def test_emit_without_run_id_goes_to_adhoc_file(self):
        self.init()
        self.emit({'n': 1})
        [f] = self.spool_files()
        line = json.loads(f.read_text(encoding='utf-8'))
        self.assertEqual(f.name, f"adhoc-{line['ts'][:10]}.jsonl")
        self.assertIsNone(line['run_id'])

    def test_repo_shown_when_matching_show_repos(self):
        self.init('acme/*')
        self.emit({'n': 1})
        self.assertEqual(self.spool_lines()[0]['repo'], 'acme/widget')

    def test_repo_hashed_when_not_listed_and_none_without_remote(self):
        self.init('other/*')
        self.emit({'n': 1})
        bare_repo = self.tmp / 'noremote'
        self.git('init', '-q', str(bare_repo))
        self.emit({'n': 2}, cwd=bare_repo)
        lines = sorted(self.spool_lines(), key=lambda x: x['data']['n'])
        self.assertEqual(lines[0]['repo'], self.hash_cli('acme/widget'))
        self.assertRegex(lines[0]['repo'], r'^h:[0-9a-f]{12}$')
        self.assertEqual(lines[1]['repo'], 'none')

    def test_branch_host_and_hash_fields_are_keyed_hashes(self):
        self.init()
        self.emit({'who': 'alice', 'name': 'plain'})
        [line] = self.spool_lines()
        branch = self.git('symbolic-ref', '--short', 'HEAD', cwd=self.work).stdout.strip()
        self.assertEqual(line['branch'], self.hash_cli(branch))
        self.assertEqual(line['host'], self.hash_cli(socket.gethostname()))
        self.assertEqual(line['data']['who'], self.hash_cli('alice'))
        self.assertEqual(line['data']['name'], 'plain')
        self.assertNotIn('alice', json.dumps(line))
        self.assertEqual(line['schema'], 'harness.event/1')
        self.assertEqual((line['phase'], line['skill'], line['data_schema']), ('test', 'demo', 'demo.event/1'))

    def test_preview_shows_spool_and_disable_still_spools(self):
        self.init()
        self.emit({'name': 'first-line'})
        pv = self.run_hl('preview')
        self.assertEqual(pv.returncode, 0)
        self.assertIn('"name":"first-line"', pv.stdout)
        self.assertEqual(self.run_hl('enable').returncode, 0)
        self.assertEqual(self.run_hl('disable').returncode, 0)
        self.assertIs(json.loads(self.cfg.read_text(encoding='utf-8'))['enabled'], False)
        self.emit({'name': 'second-line'})
        self.assertEqual(len(self.spool_lines()), 2)
        self.run_hl('flush')
        self.assertEqual(self.bare_commits(), 0)

    def test_enable_without_config_is_refused(self):
        r = self.run_hl('enable')
        self.assertEqual(r.returncode, 2)
        self.assertFalse(self.cfg.exists())

    def test_envelope_keys_function(self):
        self.init()
        env = {'split': 'train', 'split_set_id': 's.1', 'change_id': 'c-1', 'config_fp': 'abc123def456',
               'trial': 0, 'unit_id': 'u:r1:p1:1'}
        line = hl.emit(self.schema, 'thing.done', {'n': 1}, envelope=env, config_path=self.cfg, cwd=self.work)
        for k, v in env.items():
            self.assertEqual(line[k], v)
        self.assertNotIn('dropped', line)
        bad = hl.emit(self.schema, 'thing.done', {'n': 2}, config_path=self.cfg, cwd=self.work,
                      envelope={'split': 'poem', 'trial': '1', 'other': 'x', 'change_id': None, 'unit_id': 'u1'})
        for k in ('split', 'trial', 'other', 'change_id'):
            self.assertNotIn(k, bad)
        self.assertEqual(bad['unit_id'], 'u1')
        self.assertEqual(bad['dropped'], ['envelope.split', 'envelope.trial', 'envelope.other'])
        spooled = sorted(self.spool_lines(), key=lambda x: x['data']['n'])
        self.assertEqual(spooled[0]['split'], 'train')
        self.assertEqual(spooled[0]['data'], {'n': 1})
        self.assertNotIn('split', spooled[1])

    def test_envelope_keys_cli(self):
        self.init()
        self.emit({'n': 1}, '--envelope', json.dumps({'split': 'heldout', 'trial': 3, 'bogus': 1, 'trial2': None}))
        [line] = self.spool_lines()
        self.assertEqual((line['split'], line['trial']), ('heldout', 3))
        self.assertEqual(line['dropped'], ['envelope.bogus', 'envelope.trial2'])
        self.assertNotIn('split', line['data'])
        r = self.run_hl('emit', '--schema', str(self.schema), '--type', 'thing.done', '--data', '{}',
                        '--envelope', '[1]')
        self.assertEqual(r.returncode, 2)

    def test_load_config_fills_embedding_defaults_and_keeps_values(self):
        self.cfg.write_text(json.dumps({'key': 'k', 'remote': 'r', 'custom': 1,
                                        'embedding': {'url': 'http://h:11434', 'model': 'm2', 'extra': 'x'}}),
                            encoding='utf-8')
        cfg = hl.load_config(self.cfg)
        self.assertEqual(cfg['custom'], 1)
        self.assertEqual(cfg['embedding'], {'url': 'http://h:11434', 'model': 'm2', 'extra': 'x',
                                            'provider': 'ollama', 'dims': None, 'normalize': True,
                                            'prefix': '', 'timeout': 10})
        self.cfg.write_text(json.dumps({'key': 'k'}), encoding='utf-8')
        emb = hl.load_config(self.cfg)['embedding']
        self.assertIsNone(emb['url'])
        self.assertEqual(emb['model'], 'qwen3-embedding:0.6b')
        self.assertIsNone(hl.load_config(self.tmp / 'missing.json'))

    def test_check_keeps_hash_raw_and_drops_bad_nested_values(self):
        clean, dropped = hl.check(SCHEMA, 'thing.done', {
            'who': 'bob', 'n': True, 'tags': ['a', 'z', 'b', 'a'], 'roles': {'x': 1.5, 'bad key!': 2, 'y': 'no'},
        })
        self.assertEqual(clean, {'who': 'bob', 'tags': ['a', 'b'], 'roles': {'x': 1.5}})
        self.assertEqual(dropped, ['n', 'tags[]', 'roles.*'])
        with self.assertRaises(ValueError):
            hl.check(SCHEMA, 'nope', {})

    def test_unsafe_run_id_is_dropped_and_stays_inside_spool(self):
        self.init()
        self.emit({'n': 1}, '--run-id', '../../evil', '--stage', 'has space')
        [line] = self.spool_lines()
        self.assertIsNone(line['run_id'])
        self.assertIsNone(line['stage'])
        self.assertEqual(line['dropped'], ['run_id', 'stage'])
        self.assertEqual([p for p in self.tmp.rglob('evil*')], [])

    def test_values_ending_in_newline_are_dropped(self):
        self.assertIsNone(hl.ID_RE.match('a' * 10 + '\n'))
        self.assertIsNone(hl.FILE_RE.match('r1\n'))
        self.assertIsNone(hl.NAME_RE.match('n\n'))
        clean, dropped = hl.check(SCHEMA, 'thing.done', {'name': 'a' * 64 + '\n', 'who': 'bob\n', 'n': 1})
        self.assertEqual((clean, dropped), ({'n': 1}, ['name', 'who']))
        self.init()
        line = hl.emit(self.schema, 'thing.done', {'n': 1}, run_id='nl-run\n', envelope={'unit_id': 'u1\n'},
                       config_path=self.cfg, cwd=self.work)
        self.assertNotIn('unit_id', line)
        self.assertIsNone(line['run_id'])
        self.assertEqual(line['dropped'], ['run_id', 'envelope.unit_id'])
        self.assertEqual([p for p in self.tmp.rglob('nl-run*')], [])
        [f] = self.spool_files()
        self.assertTrue(f.name.startswith('adhoc-'))

    # ---- 記録のしかた (remote・local・off) と status ----
    def status(self):
        r = self.run_hl('status')
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_status_is_none_without_config(self):
        self.assertEqual(self.status(), 'none')
        self.assertFalse(self.cfg.exists())

    def test_init_needs_exactly_one_mode(self):
        self.assertEqual(self.run_hl('init').returncode, 2)
        self.assertEqual(self.run_hl('init', '--local', '--off').returncode, 2)
        self.assertFalse(self.cfg.exists())

    def test_local_keeps_lines_in_spool_and_never_commits(self):
        r = self.run_hl('init', '--local')
        self.assertEqual(r.returncode, 0, r.stderr)
        cfg = json.loads(self.cfg.read_text(encoding='utf-8'))
        self.assertEqual(cfg['mode'], 'local')
        self.assertIsNone(cfg['remote'])
        self.assertIs(cfg['enabled'], False)
        self.assertRegex(cfg['key'], r'^[0-9a-f]{64}$')
        self.assertFalse((Path(cfg['clone_dir']) / '.git').exists())
        self.emit({'n': 1})
        self.assertEqual(len(self.spool_lines()), 1)
        self.assertEqual(self.run_hl('enable').returncode, 2)  # 送り先が無いので送り始められない
        self.assertEqual(self.run_hl('flush').returncode, 0)
        self.assertFalse(hl.flush(config_path=self.cfg))
        self.assertEqual(self.bare_commits(), 0)
        self.assertEqual(len(self.spool_lines()), 1)
        self.assertEqual(self.status(), 'local spool=1')

    def test_local_then_remote_keeps_key_and_sends_the_kept_lines(self):
        self.assertEqual(self.run_hl('init', '--local').returncode, 0)
        key = json.loads(self.cfg.read_text(encoding='utf-8'))['key']
        self.emit({'n': 1}, '--run-id', 'r1')
        r = self.run_hl('init', '--remote', str(self.bare), '--show-repo', 'acme/*')
        self.assertEqual(r.returncode, 0, r.stderr)
        cfg = json.loads(self.cfg.read_text(encoding='utf-8'))
        self.assertEqual((cfg['mode'], cfg['key'], cfg['enabled']), ('remote', key, False))
        self.assertEqual(cfg['show_repos'], ['acme/*'])
        self.assertTrue((Path(cfg['clone_dir']) / '.git').is_dir())
        self.assertEqual(self.status(), 'remote disabled spool=1')
        self.assertEqual(self.bare_commits(), 0)
        self.assertEqual(self.run_hl('enable').returncode, 0)
        self.assertEqual(self.run_hl('flush').returncode, 0)
        self.assertEqual(self.bare_commits(), 1)
        self.assertEqual(self.spool_lines(), [])
        self.assertTrue(any(p.endswith('/r1.jsonl') for p in self.bare_files()))

    def test_off_records_nothing_and_is_not_changed_by_init(self):
        self.assertEqual(self.run_hl('init', '--off').returncode, 0)
        cfg = json.loads(self.cfg.read_text(encoding='utf-8'))
        self.assertEqual(cfg['mode'], 'off')
        self.emit({'n': 1})
        self.assertIsNone(hl.emit(self.schema, 'thing.done', {'n': 1}, config_path=self.cfg))
        self.assertEqual(self.spool_files(), [])
        self.assertEqual(self.status(), 'off')
        self.assertEqual(self.run_hl('init', '--local').returncode, 2)
        self.assertEqual(self.run_hl('init', '--remote', str(self.bare)).returncode, 2)
        self.assertEqual(self.run_hl('enable').returncode, 2)
        self.assertEqual(json.loads(self.cfg.read_text(encoding='utf-8'))['mode'], 'off')

    def test_old_config_without_mode_reads_as_remote(self):
        self.init()
        raw = json.loads(self.cfg.read_text(encoding='utf-8'))
        raw.pop('mode', None)
        self.cfg.write_text(json.dumps(raw), encoding='utf-8')
        self.assertEqual(hl.load_config(self.cfg)['mode'], 'remote')
        self.assertEqual(self.status(), 'remote disabled spool=0')


if __name__ == '__main__':
    unittest.main()
