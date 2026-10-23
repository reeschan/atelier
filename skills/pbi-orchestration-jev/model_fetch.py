"""実行先ごとに使えるモデルの一覧を取る。

opencode・codex・agy は CLI の出力から、claude は同梱の別名から、dsh は同梱のカタログと --dump-config から、
agent は組み込みの定義から取る。取得の失敗は FetchError にし、ほかの実行先に
波及させないのは呼び出し側 (setup_agents.py) の責任にする。
"""
import json
import os
import shutil
import signal
import subprocess
from pathlib import Path

from common import AGENT, EXTERNAL_RUNNERS

# dsh の同梱カタログ (このファイルと同じディレクトリ)
CATALOG = Path(__file__).resolve().parent / 'dsh.models.json'

# agent (Agent ツール) の組み込みのモデルと effort
AGENT_MODELS = ('haiku', 'sonnet', 'opus', 'fable')
AGENT_EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')

# claude（Claude Code の headless）の同梱のモデルの別名。CLI から一覧は取れない
CLAUDE_MODELS = ('haiku', 'sonnet', 'opus')

# dsh の .js を起動する Node の環境変数
DSH_NODE = 'PBI_DSH_NODE'


class FetchError(Exception):
    """モデル一覧の取得の失敗。メッセージは英語の 1 行。"""


def fetch(runner: str, cli: str | None, env: dict | None = None, timeout: float = 60) -> dict:
    """実行先のモデルの一覧。{"method", "models"} を返す。

    models は CLI の出力順の {"id", "name", "efforts"} で、id の重複は除く。
    取得に失敗したら FetchError。agent は CLI を使わない。
    """
    if runner == AGENT:
        return _agent()
    if runner not in EXTERNAL_RUNNERS:
        raise FetchError(f'unknown runner: {runner}.')
    if runner == 'dsh':
        # dsh は一覧のコマンドが無いので、cli が無くてもカタログで返す
        return _dsh(cli, env, timeout)
    if runner == 'claude':
        # claude は一覧のコマンドが無いので、cli が無くても同梱の別名で返す
        return _claude()
    if cli is None:
        raise FetchError(f'{runner}: the CLI was not found.')
    if runner == 'opencode':
        return _opencode(cli, env, timeout)
    if runner == 'codex':
        return _codex(cli, env, timeout)
    return _agy(cli, env, timeout)


def version(runner: str, cli: str | None, env: dict | None = None, timeout: float = 15) -> str | None:
    """<cli> --version の最初の空でない行。agent・cli が無い・失敗は None。"""
    if cli is None or runner == AGENT:
        return None
    arguments = [cli, '--version']
    if runner == 'dsh' and _is_js(cli):
        arguments = _node_argv(env) + arguments
    try:
        result = _run_process(arguments, env, timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    for line in _text(result.stdout).splitlines():
        if line.strip():
            return line.strip()
    return None


def _agent() -> dict:
    """agent の組み込みのモデル。CLI は呼ばない。"""
    models = [{'id': model, 'name': None, 'efforts': list(AGENT_EFFORTS)} for model in AGENT_MODELS]
    return {'method': 'builtin', 'models': models}


def _claude() -> dict:
    """claude の同梱の固定の一覧（別名）。CLI は呼ばない。"""
    models = [{'id': model, 'name': None, 'efforts': list(AGENT_EFFORTS)} for model in CLAUDE_MODELS]
    return {'method': 'builtin', 'models': models}


def _opencode(cli: str, env: dict | None, timeout: float) -> dict:
    """opencode models --standalone と、キャッシュのモデル情報。"""
    result = _run('opencode', [cli, 'models', '--standalone'], env, timeout)
    text = _checked('opencode', result)
    cache = _opencode_cache(env)
    models, seen = [], set()
    for line in text.splitlines():
        model_id = line.strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        info = _opencode_info(cache, model_id)
        item = {'id': model_id, 'name': info.get('name'), 'efforts': info.get('efforts', [])}
        if 'price_in' in info:
            item['price_in'] = info['price_in']
        if 'price_out' in info:
            item['price_out'] = info['price_out']
        models.append(item)
    return {'method': 'cli', 'models': models}


def _opencode_info(cache: dict, model_id: str) -> dict:
    """キャッシュから、そのモデルの name・efforts・単価。無ければ efforts が空の辞書。"""
    provider, _, model = model_id.partition('/')
    entry = None
    if model:
        provider_entry = cache.get(provider) if isinstance(cache, dict) else None
        if isinstance(provider_entry, dict):
            entries = provider_entry.get('models')
            if isinstance(entries, dict):
                entry = entries.get(model)
    if not isinstance(entry, dict):
        return {'efforts': []}
    info = {'efforts': _reasoning_efforts(entry)}
    if isinstance(entry.get('name'), str):
        info['name'] = entry['name']
    cost = entry.get('cost')
    if isinstance(cost, dict):
        for source, target in (('input', 'price_in'), ('output', 'price_out')):
            value = cost.get(source)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                info[target] = value
    return info


def _reasoning_efforts(entry: dict) -> list:
    """reasoning_options のうち type が effort の項目の values。無ければ空。"""
    options = entry.get('reasoning_options')
    if not isinstance(options, list):
        return []
    for option in options:
        if isinstance(option, dict) and option.get('type') == 'effort':
            values = option.get('values')
            if isinstance(values, list):
                return [str(value) for value in values]
    return []


def _opencode_cache(env: dict | None) -> dict:
    """opencode のキャッシュ (<XDG_CACHE_HOME か ~/.cache>/opencode/models.json)。読めなければ空。"""
    path = _cache_home(env) / 'opencode' / 'models.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _cache_home(env: dict | None) -> Path:
    """XDG_CACHE_HOME・HOME・USERPROFILE・ホームディレクトリの順で決めるキャッシュの根。"""
    environment = os.environ if env is None else env
    base = environment.get('XDG_CACHE_HOME')
    if base:
        return Path(base)
    home = environment.get('HOME') or environment.get('USERPROFILE')
    return (Path(home) if home else Path.home()) / '.cache'


def _codex(cli: str, env: dict | None, timeout: float) -> dict:
    """codex debug models の JSON。visibility が hide のモデルは除く。"""
    result = _run('codex', [cli, 'debug', 'models'], env, timeout)
    text = _checked('codex', result)
    try:
        data = json.loads(text)
    except ValueError:
        raise FetchError('codex: the CLI did not return JSON.')
    entries = data.get('models') if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise FetchError('codex: the CLI output has no model list.')
    models, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict) or entry.get('visibility') == 'hide':
            continue
        model_id = entry.get('slug')
        if not isinstance(model_id, str) or not model_id or model_id in seen:
            continue
        seen.add(model_id)
        efforts = []
        levels = entry.get('supported_reasoning_levels')
        if isinstance(levels, list):
            for level in levels:
                if isinstance(level, dict) and isinstance(level.get('effort'), str):
                    efforts.append(level['effort'])
        name = entry.get('display_name')
        models.append({'id': model_id, 'name': name if isinstance(name, str) else None, 'efforts': efforts})
    return {'method': 'cli', 'models': models}


def _agy(cli: str, env: dict | None, timeout: float) -> dict:
    """agy models のタブ区切りの出力。タブの無い行は捨て、ID の末尾から level を取る。"""
    result = _run('agy', [cli, 'models'], env, timeout)
    text = _checked('agy', result)
    models, seen = [], set()
    for line in text.splitlines():
        if '\t' not in line:
            continue
        model_id, name = line.split('\t', 1)
        model_id = model_id.strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        item = {'id': model_id, 'name': name.strip() or None, 'efforts': []}
        level = _agy_level(model_id)
        if level is not None:
            item['level'] = level
        models.append(item)
    return {'method': 'cli', 'models': models}


def _agy_level(model_id: str) -> str | None:
    """ID の末尾の -low / -medium / -high。無ければ None。"""
    for level in ('low', 'medium', 'high'):
        if model_id.endswith('-' + level):
            return level
    return None


def _dsh(cli: str | None, env: dict | None, timeout: float) -> dict:
    """同梱のカタログと、--dump-config の既定のモデル。CLI の失敗はカタログだけで返す。"""
    models = _load_catalog()
    if cli is not None:
        default = _dsh_default(cli, env, timeout)
        if default is not None and all(model['id'] != default for model in models):
            models.append({'id': default, 'name': None, 'efforts': []})
    return {'method': 'catalog', 'models': models}


def _dsh_default(cli: str, env: dict | None, timeout: float) -> str | None:
    """--dump-config の agent-default-model の <provider>/<model>。失敗は None。"""
    arguments = [cli, 'headless', '--dump-config']
    if _is_js(cli):
        arguments = _node_argv(env) + arguments
    try:
        result = _run('dsh', arguments, env, timeout)
    except FetchError:
        return None
    if result.returncode != 0:
        return None
    return _default_model(_text(result.stdout))


def _default_model(text: str) -> str | None:
    """YAML のテキストから、agent-default-model の config の provider と model を取る。"""
    provider = model = None
    in_item = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('- id:'):
            if in_item:
                break
            in_item = _yaml_value(stripped[len('- id:'):]) == 'agent-default-model'
            continue
        if not in_item:
            continue
        if stripped.startswith('provider:'):
            provider = _yaml_value(stripped[len('provider:'):])
        elif stripped.startswith('model:'):
            model = _yaml_value(stripped[len('model:'):])
    if provider and model:
        return f'{provider}/{model}'
    return None


def _yaml_value(value: str) -> str:
    """YAML のスカラーの前後の空白と引用符を除く。"""
    return value.strip().strip("'\"")


def _load_catalog() -> list:
    """同梱の dsh.models.json の models。読めなければ FetchError。"""
    try:
        data = json.loads(CATALOG.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise FetchError('dsh: cannot read the bundled model catalog.')
    entries = data.get('models') if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise FetchError('dsh: the bundled model catalog has no model list.')
    models, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get('id')
        if not isinstance(model_id, str) or not model_id or model_id in seen:
            continue
        seen.add(model_id)
        name = entry.get('name')
        efforts = entry.get('efforts')
        models.append({
            'id': model_id,
            'name': name if isinstance(name, str) else None,
            'efforts': [str(value) for value in efforts] if isinstance(efforts, list) else [],
        })
    return models


def _run(runner: str, arguments: list, env: dict | None, timeout: float):
    """CLI を起動する。起動できない・時間切れは FetchError。"""
    try:
        return _run_process(arguments, env, timeout)
    except subprocess.TimeoutExpired:
        raise FetchError(f'{runner}: the CLI timed out after {timeout} seconds.')
    except OSError as error:
        # OS のメッセージは言語に依存するので、英語の決まった文にする
        if isinstance(error, FileNotFoundError):
            raise FetchError(f'{runner}: the CLI was not found.')
        if isinstance(error, PermissionError):
            raise FetchError(f'{runner}: permission denied when running the CLI.')
        raise FetchError(f'{runner}: cannot run the CLI.')


def _run_process(arguments: list, env: dict | None, timeout: float) -> subprocess.CompletedProcess:
    """CLI を起動して出力を集める。時間切れなら子孫のプロセスごと止めて TimeoutExpired を投げる。

    Windows の .cmd は cmd.exe の下で本体が動くので、subprocess.run の kill では本体が残り、
    本体が握ったパイプのせいで後の communicate が戻らないことがある。そのため木ごと止める。
    """
    process = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                               start_new_session=os.name != 'nt')
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(process)
        try:
            # 止めたあとの出力の回収。待ちすぎないよう短く区切る (5 秒は taskkill が終わるのに足りる幅)
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            for stream in (process.stdout, process.stderr):
                stream.close()
        raise subprocess.TimeoutExpired(arguments, timeout) from None
    return subprocess.CompletedProcess(arguments, process.returncode, stdout, stderr)


def _kill_tree(process: subprocess.Popen) -> None:
    """プロセスと子孫を止める。Windows は taskkill /T、ほかはプロセスグループに SIGKILL。"""
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(process.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
    except OSError:
        pass


def _checked(runner: str, result) -> str:
    """終了コード 0 を確かめて、標準出力を UTF-8 で返す。"""
    if result.returncode != 0:
        raise FetchError(f'{runner}: the CLI exited with code {result.returncode}.')
    return _text(result.stdout)


def _text(data) -> str:
    """CLI の出力を UTF-8 の文字列にする (読めないバイトは replacement)。"""
    if isinstance(data, str):
        return data
    if not data:
        return ''
    return data.decode('utf-8', errors='replace')


def _is_js(cli: str) -> bool:
    """.js の CLI か (dsh は Node 経由で起動する)。"""
    return str(cli).lower().endswith('.js')


def _node_argv(env: dict | None) -> list:
    """[PBI_DSH_NODE か PATH の node]。"""
    environment = os.environ if env is None else env
    configured = environment.get(DSH_NODE)
    if configured:
        return [configured]
    found = shutil.which('node', path=environment.get('PATH') or os.defpath)
    return [found or 'node']
