"""model_fetch.py (実行先ごとのモデルの取得) の単体テスト。"""
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

import model_fetch

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / 'testdata' / 'cli'

# 偽の CLI の本体。隣の <名前>.json の commands から、引数に合う応答を出す。
FAKE_SCRIPT = '''\
"""偽の CLI。同じ名前の .json の commands から、引数に合う応答を出す。"""
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
config = json.loads(Path(__file__).resolve().with_suffix('.json').read_text(encoding='utf-8'))
key = ' '.join(sys.argv[1:])
entry = config.get('commands', {}).get(key)
if entry is None:
    entry = config.get('default', {})
sys.stdout.write(entry.get('out', ''))
sys.stderr.write(entry.get('err', ''))
raise SystemExit(entry.get('code', 0))
'''


def fixture(name):
    """testdata/cli の fixture の中身。"""
    return (FIXTURES / name).read_text(encoding='utf-8')


def make_temp_dir():
    """中に書ける一時ディレクトリ。"""
    while True:
        path = Path(tempfile.gettempdir()) / f'pbi-model-fetch-{uuid.uuid4().hex}'
        try:
            path.mkdir()
            return path
        except FileExistsError:
            continue


def write_config(script, commands, default=None):
    """偽の CLI の応答を、スクリプトの隣の .json に書く。"""
    config = {'commands': commands}
    if default is not None:
        config['default'] = default
    Path(script).with_suffix('.json').write_text(json.dumps(config), encoding='utf-8')


def make_fake(directory, name, commands, default=None):
    """PATH に置く偽の CLI。Windows は .cmd、それ以外は実行権を付けたシェルスクリプト。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / f'{name}_fake.py'
    script.write_text(FAKE_SCRIPT, encoding='utf-8')
    write_config(script, commands, default)
    if os.name == 'nt':
        cli = directory / f'{name}.cmd'
        cli.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding='utf-8')
    else:
        cli = directory / name
        cli.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding='utf-8')
        cli.chmod(0o755)
    return cli


def make_js_fake(directory, name, commands):
    """dsh の .js の CLI (Node の代わりに PBI_DSH_NODE で Python を指す)。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / f'{name}.js'
    script.write_text(FAKE_SCRIPT, encoding='utf-8')
    write_config(script, commands)
    return script


class Base(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.bin = self.root / 'bin'

    def env(self, **extra):
        """本物の環境変数を残した env (Windows の SystemRoot など)。"""
        return dict(os.environ, **extra)

    def cache(self, base, text=None):
        """<base>/opencode/models.json を置き、base を返す。"""
        base = Path(base)
        path = base / 'opencode' / 'models.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        if text is not None:
            path.write_text(text, encoding='utf-8')
        return base


class OpencodeTest(Base):
    def opencode_cli(self, commands):
        return make_fake(self.bin, 'opencode', commands)

    def test_models_and_cache(self):
        cli = self.opencode_cli({'models --standalone': {'out': fixture('opencode-models.txt')}})
        cache = self.cache(self.root / 'cache', fixture('opencode-cache-models.json'))
        result = model_fetch.fetch('opencode', str(cli), self.env(XDG_CACHE_HOME=str(cache)))
        self.assertEqual(result['method'], 'cli')
        self.assertEqual([m['id'] for m in result['models']], [
            'opencode-go/beam-free',
            'opencode-go/claude-haiku-5-5',
            'opencode-go/deepseek-v4-pro',
            'opencode-go/deepseek-v4.1-flash',
            'opencode-go/gemini-4-argon-contributor',
            'opencode-go/glm-5.2',
        ])
        models = {m['id']: m for m in result['models']}
        flash = models['opencode-go/deepseek-v4.1-flash']
        self.assertEqual(flash['efforts'], ['low', 'high', 'max'])
        self.assertEqual(flash['price_in'], 0.15)
        self.assertEqual(flash['price_out'], 0.6)
        self.assertEqual(flash['name'], 'DeepSeek V4.1 Flash')
        # キャッシュに無いモデルは efforts が空
        self.assertEqual(models['opencode-go/beam-free']['efforts'], [])
        self.assertNotIn('price_in', models['opencode-go/beam-free'])
        # toggle の項目を飛ばして effort の values を取る
        self.assertEqual(models['opencode-go/claude-haiku-5-5']['efforts'],
                         ['low', 'medium', 'high', 'xhigh', 'max'])

    def test_cache_missing_still_lists_models(self):
        cli = self.opencode_cli({'models --standalone': {'out': fixture('opencode-models.txt')}})
        empty = self.root / 'empty'
        empty.mkdir()
        result = model_fetch.fetch('opencode', str(cli), self.env(XDG_CACHE_HOME=str(empty)))
        self.assertEqual(len(result['models']), 6)
        self.assertTrue(all(m['efforts'] == [] for m in result['models']))

    def test_cache_under_home_when_xdg_is_absent(self):
        cli = self.opencode_cli({'models --standalone': {'out': fixture('opencode-models.txt')}})
        home = self.root / 'home'
        self.cache(home / '.cache', fixture('opencode-cache-models.json'))
        env = self.env()
        env.pop('XDG_CACHE_HOME', None)
        env['HOME'] = str(home)
        result = model_fetch.fetch('opencode', str(cli), env)
        models = {m['id']: m for m in result['models']}
        self.assertEqual(models['opencode-go/deepseek-v4.1-flash']['efforts'], ['low', 'high', 'max'])

    def test_duplicate_ids_are_removed(self):
        output = fixture('opencode-models.txt') + 'opencode-go/beam-free\n'
        cli = self.opencode_cli({'models --standalone': {'out': output}})
        empty = self.root / 'empty'
        empty.mkdir()
        result = model_fetch.fetch('opencode', str(cli), self.env(XDG_CACHE_HOME=str(empty)))
        ids = [m['id'] for m in result['models']]
        self.assertEqual(len(ids), 6)
        self.assertEqual(ids.count('opencode-go/beam-free'), 1)

    def test_cli_missing(self):
        with self.assertRaises(model_fetch.FetchError):
            model_fetch.fetch('opencode', None)

    def test_nonzero_exit(self):
        cli = self.opencode_cli({'models --standalone': {'code': 1, 'out': 'boom\n'}})
        with self.assertRaises(model_fetch.FetchError):
            model_fetch.fetch('opencode', str(cli))


class CodexTest(Base):
    def codex_cli(self, entry):
        return make_fake(self.bin, 'codex', {'debug models': entry})

    def test_models(self):
        cli = self.codex_cli({'out': fixture('codex-debug-models.json')})
        result = model_fetch.fetch('codex', str(cli))
        self.assertEqual(result['method'], 'cli')
        self.assertEqual([m['id'] for m in result['models']], ['gpt-6-astra', 'gpt-5.6-luna'])
        models = {m['id']: m for m in result['models']}
        self.assertNotIn('codex-auto-review', models)
        self.assertEqual(models['gpt-6-astra']['name'], 'GPT-6-Astra')
        self.assertEqual(models['gpt-6-astra']['efforts'],
                         ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'])
        self.assertEqual(models['gpt-5.6-luna']['name'], 'GPT-5.6-Luna')

    def test_not_json(self):
        cli = self.codex_cli({'out': 'not json\n'})
        with self.assertRaises(model_fetch.FetchError):
            model_fetch.fetch('codex', str(cli))

    def test_no_model_list(self):
        cli = self.codex_cli({'out': '{"models": 3}\n'})
        with self.assertRaises(model_fetch.FetchError):
            model_fetch.fetch('codex', str(cli))

    def test_nonzero_exit(self):
        cli = self.codex_cli({'code': 1, 'out': fixture('codex-debug-models.json')})
        with self.assertRaises(model_fetch.FetchError):
            model_fetch.fetch('codex', str(cli))


class AgyTest(Base):
    def test_models(self):
        output = fixture('agy-models.txt') + 'Fetching available models...\n'
        cli = make_fake(self.bin, 'agy', {'models': {'out': output}})
        result = model_fetch.fetch('agy', str(cli))
        self.assertEqual(result['method'], 'cli')
        # fixture のタブ区切りは 13 行 (タブの無い行は捨てる)
        self.assertEqual(len(result['models']), 13)
        ids = [m['id'] for m in result['models']]
        self.assertNotIn('Fetching available models...', ids)
        models = {m['id']: m for m in result['models']}
        sonnet = models['claude-sonnet-5-5-high']
        self.assertEqual(sonnet['level'], 'high')
        self.assertEqual(sonnet['efforts'], [])
        self.assertEqual(sonnet['name'], 'Claude Sonnet 5.5 (High)')
        self.assertEqual(models['claude-sonnet-5-5-low']['level'], 'low')
        self.assertEqual(models['gpt-oss-120b-medium']['level'], 'medium')

    def test_tabless_line_is_the_only_thing_dropped(self):
        cli = make_fake(self.bin, 'agy', {'models': {'out': 'Fetching available models...\n'}})
        result = model_fetch.fetch('agy', str(cli))
        self.assertEqual(result['models'], [])


class DshTest(Base):
    def dsh_cli(self, output, code=0):
        return make_fake(self.bin, 'dsh', {'headless --dump-config': {'out': output, 'code': code}})

    def test_catalog_with_same_default(self):
        cli = self.dsh_cli(fixture('dsh-dump-config.yml'))
        result = model_fetch.fetch('dsh', str(cli))
        self.assertEqual(result['method'], 'catalog')
        ids = [m['id'] for m in result['models']]
        self.assertEqual(ids, ['deepseek-official/deepseek-flash'])
        flash = result['models'][0]
        self.assertEqual(flash['name'], 'DeepSeek V4.1 Flash (official API)')
        self.assertEqual(flash['efforts'], ['low', 'high', 'max'])

    def test_default_appended_at_the_end(self):
        output = fixture('dsh-dump-config.yml').replace('model: deepseek-flash', 'model: deepseek-pro')
        cli = self.dsh_cli(output)
        result = model_fetch.fetch('dsh', str(cli))
        ids = [m['id'] for m in result['models']]
        self.assertEqual(ids, ['deepseek-official/deepseek-flash', 'deepseek-official/deepseek-pro'])
        self.assertEqual(result['models'][-1]['efforts'], [])
        self.assertIsNone(result['models'][-1]['name'])

    def test_no_cli_returns_catalog(self):
        result = model_fetch.fetch('dsh', None)
        self.assertEqual(result['method'], 'catalog')
        self.assertEqual([m['id'] for m in result['models']], ['deepseek-official/deepseek-flash'])

    def test_dump_config_failure_returns_catalog(self):
        cli = self.dsh_cli('', code=1)
        result = model_fetch.fetch('dsh', str(cli))
        self.assertEqual([m['id'] for m in result['models']], ['deepseek-official/deepseek-flash'])

    def test_js_cli_runs_through_node(self):
        cli = make_js_fake(self.bin, 'fake-dsh', {'headless --dump-config': {'out': fixture('dsh-dump-config.yml')}})
        result = model_fetch.fetch('dsh', str(cli), self.env(PBI_DSH_NODE=sys.executable))
        self.assertEqual(result['method'], 'catalog')
        self.assertEqual([m['id'] for m in result['models']], ['deepseek-official/deepseek-flash'])


class AgentTest(Base):
    def test_builtin_models(self):
        result = model_fetch.fetch('agent', None)
        self.assertEqual(result['method'], 'builtin')
        self.assertEqual([m['id'] for m in result['models']], ['haiku', 'sonnet', 'opus', 'fable'])
        self.assertEqual(result['models'][0]['efforts'], ['low', 'medium', 'high', 'xhigh', 'max'])


class FailureTest(Base):
    def test_unknown_runner(self):
        with self.assertRaises(model_fetch.FetchError):
            model_fetch.fetch('nope', None)

    def test_error_message_is_one_english_line(self):
        cli = make_fake(self.bin, 'opencode', {'models --standalone': {'code': 2, 'out': 'x\n'}})
        with self.assertRaises(model_fetch.FetchError) as caught:
            model_fetch.fetch('opencode', str(cli))
        message = str(caught.exception)
        self.assertNotIn('\n', message)
        self.assertTrue(message.isascii())

    def test_missing_cli_file_message_is_english(self):
        with self.assertRaises(model_fetch.FetchError) as caught:
            model_fetch.fetch('opencode', str(self.root / 'no-such-cli'))
        message = str(caught.exception)
        self.assertNotIn('\n', message)
        self.assertTrue(message.isascii())

    def test_timeout(self):
        script = self.root / 'slow.py'
        script.write_text('import time\ntime.sleep(5)\n', encoding='utf-8')
        if os.name == 'nt':
            cli = self.root / 'slow.cmd'
            cli.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding='utf-8')
        else:
            cli = self.root / 'slow'
            cli.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding='utf-8')
            cli.chmod(0o755)
        with self.assertRaises(model_fetch.FetchError):
            model_fetch.fetch('opencode', str(cli), timeout=0.3)


class VersionTest(Base):
    def test_first_non_empty_line(self):
        cli = make_fake(self.bin, 'codex', {'--version': {'out': '\ncodex-cli 0.153.4\nsecond line\n'}})
        self.assertEqual(model_fetch.version('codex', str(cli)), 'codex-cli 0.153.4')

    def test_failure_is_none(self):
        cli = make_fake(self.bin, 'codex', {'--version': {'code': 1, 'out': 'codex-cli 0.153.4\n'}})
        self.assertIsNone(model_fetch.version('codex', str(cli)))

    def test_empty_output_is_none(self):
        cli = make_fake(self.bin, 'codex', {'--version': {'out': '\n\n'}})
        self.assertIsNone(model_fetch.version('codex', str(cli)))

    def test_agent_and_missing_cli_are_none(self):
        self.assertIsNone(model_fetch.version('agent', None))
        self.assertIsNone(model_fetch.version('opencode', None))

    def test_js_cli_runs_through_node(self):
        cli = make_js_fake(self.bin, 'fake-dsh', {'--version': {'out': '0.2.0-rc.2\n'}})
        self.assertEqual(model_fetch.version('dsh', str(cli), self.env(PBI_DSH_NODE=sys.executable)),
                         '0.2.0-rc.2')


if __name__ == '__main__':
    unittest.main()
