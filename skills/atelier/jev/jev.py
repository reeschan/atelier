"""共通 Jev クライアント (Python 版)。

CLI:
  python jev.py <config.json> <ゲート名> <文章ファイル>
  python jev.py --request <request.json>

import して使う:
  from jev import call_jev, get_api_key, resolve_api_key, JevError

キーの読み方・再試行の決まりは README.md を参照。
失敗時の CLI は {"error", "code", "where"} を出す。code は no-key、http-<status>
（401 と 403 は http-401 / http-403）、network、timeout、bad-response。where はキーを
見つけた場所か、見つからなかった理由。キーの値は出さない。
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_ENDPOINT = 'https://api.typesafe.ai/v1/systemone'
USAGE = (
    'usage:\n'
    '  python jev.py <config.json> <gate> <text-file>\n'
    '  python jev.py --request <request.json>\n'
)


class JevError(Exception):
    """code: "no-key" | "http" | "network" | "timeout" | "bad-response"。status は HTTP ステータス (無ければ 0)。

    where はキーを見つけた場所（env か資格情報ファイルのパス）か、no-key の理由。
    CLI は http を http-<status> にして出す（public_code）。
    """

    def __init__(self, message, code, status=0, where=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.where = where


class KeyLookup:
    """resolve_api_key の結果。key は見つかったキーか None。where は場所か失敗の理由。"""

    def __init__(self, key, where):
        self.key = key
        self.where = where


def credentials_path(env):
    """資格情報ファイルのパスと、TYPESAFE_CREDENTIALS_FILE で指定されたかを返す。"""
    explicit = env.get('TYPESAFE_CREDENTIALS_FILE') if hasattr(env, 'get') else None
    if isinstance(explicit, str) and explicit:
        return explicit, True
    home = ''
    if hasattr(env, 'get'):
        for name in ('HOME', 'USERPROFILE'):
            value = env.get(name)
            if isinstance(value, str) and value:
                home = value
                break
    if not home:
        home = os.path.expanduser('~')
    return os.path.join(home, '.config', 'typesafe', 'credentials.json'), False


def resolve_api_key(env=os.environ):
    """キーと、見つけた場所か失敗の理由を返す。

    成功の where は env（TYPESAFE_API_KEY）かファイルのパス。
    失敗の where は env無し、ファイル無し: <パス>、JSONが壊れている: <パス>、api_keyが無い: <パス>。
    環境変数が無く、資格情報ファイルの指定も無く、既定のファイルも無いときは env無し。
    ファイルは utf-8-sig で読む（BOM があっても読める）。キーの値は where に入れない。
    """
    raw = env.get('TYPESAFE_API_KEY') if hasattr(env, 'get') else None
    if isinstance(raw, str) and raw:
        return KeyLookup(raw, 'env')
    path, explicit = credentials_path(env)
    if not os.path.isfile(path):
        if explicit:
            return KeyLookup(None, f'ファイル無し: {path}')
        return KeyLookup(None, 'env無し')
    try:
        with open(path, encoding='utf-8-sig') as f:
            data = json.load(f)
    except ValueError:
        return KeyLookup(None, f'JSONが壊れている: {path}')
    except OSError:
        if explicit:
            return KeyLookup(None, f'ファイル無し: {path}')
        return KeyLookup(None, 'env無し')
    if isinstance(data, dict):
        key = data.get('api_key')
        if isinstance(key, str) and key:
            return KeyLookup(key, path)
    return KeyLookup(None, f'api_keyが無い: {path}')


def get_api_key(env=os.environ):
    """1. 環境変数 TYPESAFE_API_KEY、2. 資格情報ファイル、の順で探す。無ければ None。

    場所や失敗の理由が要るときは resolve_api_key。
    """
    return resolve_api_key(env).key


def public_code(err):
    """CLI に出す code。HTTP は http-<status> にして 401 と 403 をほかの HTTP と分ける。"""
    if err.code == 'http' and err.status:
        return f'http-{err.status}'
    return err.code


def _redact(text, api_key):
    if api_key and text and api_key in text:
        return text.replace(api_key, '[redacted]')
    return text


def _retry_wait(retry_after, n):
    try:
        seconds = float(retry_after) if retry_after is not None else None
    except ValueError:
        seconds = None
    if seconds is None or seconds < 0:
        seconds = 2 ** n
    return min(seconds, 30)


def _is_timeout(e):
    if isinstance(e, TimeoutError):
        return True
    return isinstance(e, urllib.error.URLError) and isinstance(e.reason, TimeoutError)


def call_jev(state, questions, model, endpoint=DEFAULT_ENDPOINT, api_key=None,
             timeout=120, max_retries=3, sleep=time.sleep):
    """Jev を呼んで応答 JSON (dict) を返す。失敗は JevError を投げる。

    api_key を省くと resolve_api_key の結果を使う。渡したときはその文字列を使い、where は付けない。
    """
    key_where = None
    if api_key is None:
        found = resolve_api_key()
        api_key = found.key
        key_where = found.where
    if not api_key:
        raise JevError('Jev の API キーが見つからない', 'no-key', where=key_where)

    def fail(message, code, status=0):
        return JevError(_redact(message, api_key), code, status, where=key_where)

    body = json.dumps({'state': state, 'model': model, 'questions': questions}).encode('utf-8')
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}

    rate_retries = 0
    other_retried = False
    while True:
        req = urllib.request.Request(endpoint, data=body, headers=headers, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                text = res.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            status = e.code
            retry_after = e.headers.get('retry-after') if e.headers else None
            detail = _redact(e.read().decode('utf-8', 'replace'), api_key)[:500]
            err = fail(f'HTTP {status}: {detail}', 'http', status)
            if status in (429, 529):
                if rate_retries < max_retries:
                    sleep(_retry_wait(retry_after, rate_retries))
                    rate_retries += 1
                    continue
                raise err
            if 500 <= status <= 599 and not other_retried:
                other_retried = True
                continue
            raise err
        except Exception as e:  # noqa: BLE001  接続エラー・タイムアウト
            shown = _redact(repr(e), api_key)
            if _is_timeout(e):
                err = fail(f'タイムアウト: {shown}', 'timeout')
            else:
                err = fail(f'接続エラー: {shown}', 'network')
            if not other_retried:
                other_retried = True
                continue
            raise err from e
        try:
            data = json.loads(text)
        except ValueError:
            raise fail(f'応答が JSON でない: {_redact(text, api_key)[:500]}', 'bad-response', 200) from None
        if not isinstance(data, dict) or not isinstance(data.get('answers'), dict):
            raise fail(f'応答に answers が無い: {_redact(text, api_key)[:500]}', 'bad-response', 200)
        return data


def _usage_exit(message=None):
    if message:
        sys.stderr.write(message + '\n')
    sys.stderr.write(USAGE)
    sys.exit(2)


def _load_json(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        _usage_exit(f'読めない: {path}: {e}')


def main(argv=None):
    sys.stdout.reconfigure(encoding='utf-8')
    args = sys.argv[1:] if argv is None else argv
    if len(args) == 2 and args[0] == '--request':
        req = _load_json(args[1])
        if not isinstance(req, dict) or not all(k in req for k in ('state', 'model', 'questions')):
            _usage_exit('request.json には state, model, questions が必要')
        state, model, questions = req['state'], req['model'], req['questions']
        endpoint = req.get('endpoint') or DEFAULT_ENDPOINT
        raw_timeout = req.get('timeout')
        timeout = raw_timeout if isinstance(raw_timeout, (int, float)) and not isinstance(raw_timeout, bool) else 120
    elif len(args) == 3 and not args[0].startswith('--'):
        config_path, gate, text_path = args
        config = _load_json(config_path)
        try:
            gate_def = config['gates'][gate]
            questions = {
                k: {'type': q['type'], 'instructions': q['instructions'], 'criteria': q['criteria']}
                for k, q in gate_def.items()
            }
            model = config['model']
            endpoint = config.get('endpoint') or DEFAULT_ENDPOINT
            timeout = 120
        except (KeyError, TypeError, AttributeError) as e:
            _usage_exit(f'config の形が違う、またはゲートが無い: {e!r}')
        try:
            with open(text_path, encoding='utf-8') as f:
                state = f.read()
        except OSError as e:
            _usage_exit(f'読めない: {text_path}: {e}')
    else:
        _usage_exit()

    try:
        result = call_jev(state, questions, model, endpoint=endpoint, timeout=timeout)
    except JevError as e:
        print(json.dumps({'error': e.message, 'code': public_code(e), 'where': e.where}, ensure_ascii=False))
        sys.exit(1)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
