"""agents.json の実行先と、route の手順を外す判定。

home の agents.json から実行先ごとの設定 (enabled・path・models) を読み、
CLI の場所 (find_cli) と、手順を外す理由 (drop_reason) を求める。
手順の絞り込み (filter_steps) は route の組み立てから使う。
"""
import os
import re
import shutil
from pathlib import Path

from common import AGENT, RUNNERS, load_json

# agents.json の adapters が取るフェーズと、設定値として扱う文字列
ADAPTER_PHASES = ('design', 'review', 'security')
ADAPTER_SETTINGS = ('auto', 'off')

# フェーズの値として受け付けるスキル名の形
SKILL_NAME_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9:_./-]{0,127}$')

# 手順を外す理由 (route.json の dropped の reason に書く)
REASON_DISABLED = '未使用 (agents.json)'
REASON_NOT_FOUND = '見つからない'
REASON_MODEL_NOT_SELECTED = '未選択のモデル'

# PATH にも Node のグローバルにも無いときに見る、Windows のインストーラの既定の場所 (環境変数, その下のパス)
WINDOWS_DEFAULT_LOCATIONS = {
    'agy': ('LOCALAPPDATA', ('agy', 'bin', 'agy.exe')),
}

# Node の版ごとのディレクトリの下で CLI を探すときの拡張子。
# Windows は実行できる .cmd・.exe を先にし、Windows 以外は拡張子なしだけを見る。
CLI_SUFFIXES_WINDOWS = ('.cmd', '.exe', '')
CLI_SUFFIXES_OTHER = ('',)


def cli_suffixes(os_name):
    """その OS で CLI を探す拡張子の並び。Windows は .cmd・.exe・拡張子なし、ほかは拡張子なし。"""
    return CLI_SUFFIXES_WINDOWS if os_name == 'nt' else CLI_SUFFIXES_OTHER


def load_agents(home) -> dict | None:
    """<home>/agents.json を読む。無い・壊れている・runners がオブジェクトでないときは None。"""
    data = load_json(Path(home) / 'agents.json')
    if not isinstance(data, dict) or not isinstance(data.get('runners'), dict):
        return None
    return data


def load_adapters(home) -> tuple[dict, list[str]]:
    """<home>/agents.json の adapters を読む。(フェーズごとの設定, 警告) を返す。

    1 つ目は常に design・review・security の 3 つのキーを持つ辞書で、既定は "auto"。
    2 つ目は英語の警告を 1 件 1 行にしたリスト。runners の有無・中身は見ない。
    """
    settings = {phase: 'auto' for phase in ADAPTER_PHASES}
    warnings = []
    data = load_json(Path(home) / 'agents.json')
    if not isinstance(data, dict) or 'adapters' not in data:
        return settings, warnings
    adapters = data['adapters']
    if not isinstance(adapters, dict):
        return settings, ['adapters: not an object, using auto for every phase']
    for key, value in adapters.items():
        if key not in settings:
            warnings.append(f'adapters.{key}: unknown phase, ignored')
        elif not _valid_adapter_value(value):
            warnings.append(f'adapters.{key}: invalid value, using auto')
        else:
            settings[key] = value
    return settings, warnings


def find_cli(runner, agents, env=None) -> str | None:
    """実行先の CLI の場所。env を省くと os.environ を読む。"""
    env = os.environ if env is None else env
    path = _configured_path(runner, agents)
    if path:
        return path
    search_path = env.get('PATH')
    if search_path is None:
        search_path = os.defpath
    found = shutil.which(runner, path=search_path)
    if found:
        return found
    found = _node_cli(runner, env)
    if found:
        return found
    if runner == 'dsh':
        bin_js = env.get('PBI_DSH_BIN')
        if bin_js and Path(bin_js).is_file():
            return bin_js
    return default_location(runner, env, os.name)


def default_location(runner, env, os_name) -> str | None:
    """WINDOWS_DEFAULT_LOCATIONS にある実行先の、インストーラの既定の場所。Windows 以外・無ければ None。"""
    if os_name != 'nt' or runner not in WINDOWS_DEFAULT_LOCATIONS:
        return None
    variable, parts = WINDOWS_DEFAULT_LOCATIONS[runner]
    base = env.get(variable)
    if not base:
        return None
    candidate = Path(base).joinpath(*parts)
    return str(candidate) if candidate.is_file() else None


def drop_reason(step, agents, found) -> str | None:
    """手順を外す理由。外さないなら None。found は runner → CLI の場所 (または None)。

    agent の手順は外さない。ほかは、未使用・CLI が無い・モデルが一覧に無い、の順に見る。
    """
    runner = step.get('runner')
    if runner not in RUNNERS or runner == AGENT:
        return None
    config = _runner_config(runner, agents)
    if config.get('enabled') is False:
        return REASON_DISABLED
    if (found or {}).get(runner) is None:
        return REASON_NOT_FOUND
    models = config.get('models')
    if isinstance(models, list) and step.get('model') not in models:
        return REASON_MODEL_NOT_SELECTED
    return None


def filter_steps(steps, agents, found) -> tuple[list, list]:
    """(残す手順, 外した手順に reason を足したもの)。found は runner → CLI の場所。"""
    kept, dropped = [], []
    for step in steps:
        reason = drop_reason(step, agents, found)
        if reason is None:
            kept.append(step)
        else:
            dropped.append({**step, 'reason': reason})
    return kept, dropped


def _runner_config(runner, agents) -> dict:
    """agents.json のその runner の設定。無ければ空の辞書。"""
    runners = agents.get('runners') if isinstance(agents, dict) else None
    config = runners.get(runner) if isinstance(runners, dict) else None
    return config if isinstance(config, dict) else {}


def _valid_adapter_value(value) -> bool:
    """adapters の値として受け付けるか。設定値 (auto・off) か、スキル名の形の文字列。"""
    return (isinstance(value, str)
            and (value in ADAPTER_SETTINGS or SKILL_NAME_PATTERN.fullmatch(value) is not None))


def _configured_path(runner, agents) -> str | None:
    """agents.json の path が存在するファイルならそのパス。"""
    path = _runner_config(runner, agents).get('path')
    if isinstance(path, str) and Path(path).is_file():
        return path
    return None


def _node_cli(runner, env) -> str | None:
    """Node のグローバルに入っている CLI の場所。fnm・nvm の版の新しい方から見る。"""
    return _fnm_cli(runner, env) or _nvm_cli(runner, env)


def _fnm_cli(runner, env) -> str | None:
    base = _fnm_base(env)
    if base is None:
        return None
    installations = sorted((base / 'node-versions').glob('*/installation'),
                           key=lambda p: _version_key(p.parent.name), reverse=True)
    for installation in installations:
        found = _first_file(installation, runner, cli_suffixes(os.name))
        if found is None:
            found = _first_file(installation / 'bin', runner, ('',))
        if found:
            return found
    return None


def _nvm_cli(runner, env) -> str | None:
    base = _nvm_dir(env)
    if base is not None:
        bins = sorted((base / 'versions' / 'node').glob('*/bin'),
                      key=lambda p: _version_key(p.parent.name), reverse=True)
        for directory in bins:
            found = _first_file(directory, runner, ('',))
            if found:
                return found
    nvm_home = env.get('NVM_HOME')
    if nvm_home:
        versions = sorted(Path(nvm_home).glob('v*'), key=lambda p: _version_key(p.name), reverse=True)
        for directory in versions:
            found = _first_file(directory, runner, ('.cmd',))
            if found:
                return found
    return None


def _version_key(name):
    """版のディレクトリ名を並べるための数値のタプル。先頭の v を除き、数字でない部分は 0 にする。"""
    parts = []
    for part in str(name).removeprefix('v').split('.'):
        try:
            parts.append(int(part))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _fnm_base(env):
    """fnm のディレクトリ。FNM_DIR・(Windows) APPDATA/fnm・~/.local/share/fnm の順。"""
    value = env.get('FNM_DIR')
    if value:
        return Path(value)
    if os.name == 'nt':
        appdata = env.get('APPDATA')
        return Path(appdata) / 'fnm' if appdata else None
    home = env.get('HOME')
    return Path(home) / '.local' / 'share' / 'fnm' if home else None


def _nvm_dir(env):
    """nvm のディレクトリ。NVM_DIR・~/.nvm の順。"""
    value = env.get('NVM_DIR')
    if value:
        return Path(value)
    home = env.get('HOME')
    return Path(home) / '.nvm' if home else None


def _first_file(directory, name, suffixes) -> str | None:
    """directory の下で name + suffixes の最初のファイル。無ければ None。"""
    for suffix in suffixes:
        candidate = directory / f'{name}{suffix}'
        if candidate.is_file():
            return str(candidate)
    return None
