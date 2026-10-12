"""共通 Jev クライアント (Python 版)。

CLI:
  python jev.py <config.json> <ゲート名> <文章ファイル>
  python jev.py --request <request.json>

import して使う:
  from jev import call_jev, get_api_key, JevError

キーの読み方・再試行の決まりは README.md を参照。
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
    """code: "no-key" | "http" | "network" | "timeout" | "bad-response"。status は HTTP ステータス (無ければ 0)。"""

    def __init__(self, message, code, status=0):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


def get_api_key(env=os.environ):
    """1. 環境変数 TYPESAFE_API_KEY、2. 資格情報ファイル、の順で探す。無ければ None。"""
    key = env.get('TYPESAFE_API_KEY')
    if key:
        return key
    path = env.get('TYPESAFE_CREDENTIALS_FILE') or os.path.join(
        os.path.expanduser('~'), '.config', 'typesafe', 'credentials.json'
    )
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    key = data.get('api_key')
    if isinstance(key, str) and key:
        return key
    return None


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
    """Jev を呼んで応答 JSON (dict) を返す。失敗は JevError を投げる。"""
    if api_key is None:
        api_key = get_api_key()
    if not api_key:
        raise JevError('Jev の API キーが見つからない', 'no-key')
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
            detail = e.read().decode('utf-8', 'replace')[:500]
            err = JevError(f'HTTP {status}: {detail}', 'http', status)
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
            if _is_timeout(e):
                err = JevError(f'タイムアウト: {e!r}', 'timeout')
            else:
                err = JevError(f'接続エラー: {e!r}', 'network')
            if not other_retried:
                other_retried = True
                continue
            raise err from e
        try:
            data = json.loads(text)
        except ValueError:
            raise JevError(f'応答が JSON でない: {text[:500]}', 'bad-response', 200) from None
        if not isinstance(data, dict) or not isinstance(data.get('answers'), dict):
            raise JevError(f'応答に answers が無い: {text[:500]}', 'bad-response', 200)
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
        result = call_jev(state, questions, model, endpoint=endpoint)
    except JevError as e:
        print(json.dumps({'error': e.message}, ensure_ascii=False))
        sys.exit(1)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
