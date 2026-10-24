"""スクリプトが共通に使う小さな関数と定数。

パスの長さの扱い (long_path・shown)、プロジェクトのディレクトリ名 (project_dir_for)、
JSON の読み込み (load_json) と書き込み (write_json)、単価表 (load_prices・price_for)、
home の解決 (resolve_home)、表示の補助 (utf8_stdout・now・step_label) をここに集める。
"""
import datetime
import json
import os
import sys
import tempfile
from pathlib import Path

TIERS = ('haiku', 'sonnet', 'opus')
EXTERNAL_RUNNERS = ('opencode', 'codex', 'agy', 'dsh', 'claude')
AGENT = 'agent'
RUNNERS = EXTERNAL_RUNNERS + (AGENT,)
# プロジェクトごとのディレクトリが並ぶ根
PROJECTS = Path.home() / '.claude' / 'projects'


# home のディレクトリ名。旧名 (改名前) の home しか無ければそちらを使う
HOME_NAME = 'atelier'
OLD_HOME_NAME = 'pbi-orchestration-jev'


def resolve_home(arg: str | None) -> Path:
    """home のディレクトリ。引数 → ATELIER_HOME → PBI_HOME → PBI_TEMPLATES_HOME → XDG_CONFIG_HOME → ~/.config。

    PBI_HOME と PBI_TEMPLATES_HOME は旧名 (互換のためだけに読む)。XDG_CONFIG_HOME・~/.config の下は atelier だが、
    それが無く旧名の pbi-orchestration-jev があれば旧名を使う (改名前の設定をそのまま読むため)。
    先頭の ~ は展開する (引用符の中や settings.json の env ではシェルが展開しないため)。
    """
    if arg:
        return Path(arg).expanduser()
    for name in ('ATELIER_HOME', 'PBI_HOME', 'PBI_TEMPLATES_HOME'):
        value = os.environ.get(name)
        if value:
            return Path(value).expanduser()
    base = os.environ.get('XDG_CONFIG_HOME')
    root = Path(base).expanduser() if base else Path.home() / '.config'
    new, old = root / HOME_NAME, root / OLD_HOME_NAME
    return old if not new.exists() and old.is_dir() else new


def long_path(p):
    """Windows で 260 文字を超えるパスを開けるように \\\\?\\ を付ける。

    glob の根に付ければ、そこから出てくるパスすべてに効く。
    """
    if os.name != 'nt':
        return p
    s = str(Path(p).resolve())
    return Path(p) if s.startswith('\\\\?\\') else Path('\\\\?\\' + s)


def shown(p):
    """表示用のパス (\\\\?\\ を外す)。"""
    return str(p).removeprefix('\\\\?\\')


def project_dir_for(cwd):
    """プロジェクトのディレクトリ名 (英数字以外を - にしたもの)。"""
    return long_path(PROJECTS) / ''.join(c if c.isalnum() else '-' for c in str(cwd))


def load_json(path, default=None):
    """JSON を読む。読めない・JSON でないときは default を返す。"""
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def write_json(path, data) -> None:
    """JSON を UTF-8 で書く。親ディレクトリを作り、一時ファイル + os.replace で置き換える。

    ensure_ascii=False・indent 2・末尾の改行。書きかけのファイルを読ませないための置き換え。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + '\n'
    handle, temporary = tempfile.mkstemp(prefix=f'.{target.name}.', suffix='.tmp', dir=str(target.parent))
    try:
        with os.fdopen(handle, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
        os.replace(temporary, target)
    except BaseException:
        # 失敗した一時ファイルを残さない (os.replace の後はもう無い)
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def load_prices(home, bundled=None) -> dict:
    """同梱の単価表に home の単価表の models を上から重ねる。

    bundled を省くと、このファイルと同じディレクトリの prices.json。同じモデルのキーは home が勝つ。
    checked_at は同梱のもの、home に checked_at があれば home_checked_at として持つ。
    どちらも読めなければ {'models': {}}。
    """
    base = load_json(Path(bundled) if bundled is not None else Path(__file__).resolve().parent / 'prices.json', {})
    prices = dict(base) if isinstance(base, dict) else {}
    models = prices.get('models')
    models = dict(models) if isinstance(models, dict) else {}
    extra = load_json(Path(home) / 'prices.json', {})
    if isinstance(extra, dict):
        home_models = extra.get('models')
        if isinstance(home_models, dict):
            models.update(home_models)
        if 'checked_at' in extra:
            prices['home_checked_at'] = extra['checked_at']
    prices['models'] = models
    return prices


def price_for(prices: dict, model: str) -> dict | None:
    """model に当たる単価 (長いキーを優先)。

    単価表のキーが model の全体か、model の最後の / より後ろの部分の先頭と一致するもの。
    無ければ None。
    """
    models = (prices or {}).get('models') or {}
    tail = str(model).rsplit('/', 1)[-1]
    hit = None
    for key in models:
        if str(model) == key or tail.startswith(key):
            if hit is None or len(key) > len(hit):
                hit = key
    return models[hit] if hit is not None else None


def utf8_stdout():
    """stdout と stderr を UTF-8 にする (reconfigure を持たない stream はそのまま)。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, 'reconfigure', None)
        if reconfigure is not None:
            reconfigure(encoding='utf-8')


def now() -> datetime.datetime:
    """タイムゾーン付きの現在時刻。"""
    return datetime.datetime.now().astimezone()


def step_label(step: dict) -> str:
    """手順のラベル (<runner> <model> <effort>)。effort が null なら付けない。"""
    label = f"{step.get('runner')} {step.get('model')}"
    if step.get('effort') is not None:
        label += f" {step['effort']}"
    return label
