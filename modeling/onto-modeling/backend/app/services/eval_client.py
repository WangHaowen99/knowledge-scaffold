"""以当前用户自己的登录调用独立评测服务（ADR-0092），请求和响应都有上限。

评测轮次与产物归评测服务所有，按调用者隔离；这里从不带服务身份。上游的响应体、地址和令牌一律不回显：
失败映射成固定文案，只有评测服务自己写给用户的 409 说明原样转给用户。
"""
import http.client
import json
import re
import urllib.parse

from .. import config
from .workspace import WorkspaceError

PREFIX = '/evaluation/api'
JSON_LIMIT = 2 * 1024 * 1024
ERROR_LIMIT = 64 * 1024
_CONFLICT_CODE = re.compile(r'^[a-z][a-z0-9_]{0,63}$')


def configured():
    return bool(config.EVAL_URL)


def _endpoint():
    if not config.EVAL_URL:
        raise WorkspaceError('未配置评测服务地址', 503, 'eval_unconfigured')
    url = urllib.parse.urlsplit(config.EVAL_URL)
    has_extras = any((url.username, url.password, url.query, url.fragment))
    if url.scheme not in ('http', 'https') or not url.hostname or has_extras:
        raise WorkspaceError('评测服务地址配置无效', 503, 'eval_unconfigured')
    return url


def _failure(status, raw):
    if status == 401:
        return WorkspaceError('评测服务未接受当前登录，请重新登录', 401, 'eval_unauthorized')
    if status in (403, 404):
        # 不区分“无权访问”与“不存在”，评测记录按发起人私有
        return WorkspaceError('评测记录不存在或无权访问', 404, 'eval_not_found')
    if status == 409:
        try:
            value = json.loads(raw)
        except ValueError:
            value = None
        detail = value if isinstance(value, dict) else {}
        code, message = detail.get('code'), detail.get('message')
        valid_code = isinstance(code, str) and _CONFLICT_CODE.fullmatch(code)
        if valid_code and isinstance(message, str) and message.strip():
            return WorkspaceError(message.strip()[:300], 409, code)
        return WorkspaceError('评测服务拒绝了请求，请刷新后重试', 409, 'eval_conflict')
    return WorkspaceError('评测服务不可用，请稍后重试', 503, 'eval_unavailable')


def request(method, path, token, body=None, limit=JSON_LIMIT):
    """2xx 响应的 (字节, 响应头)；path 相对 /evaluation/api。"""
    if not token:
        raise WorkspaceError('评测服务需要登录用户身份', 401, 'eval_unauthorized')
    url = _endpoint()
    conn_type = http.client.HTTPSConnection if url.scheme == 'https' else http.client.HTTPConnection
    conn = conn_type(url.hostname, url.port, timeout=30)
    headers = {'Authorization': f'Bearer {token}', 'Accept': '*/*'}
    payload = b''
    if body is not None:
        payload = json.dumps(body).encode()
        headers['Content-Type'] = 'application/json'
    headers['Content-Length'] = str(len(payload))
    try:
        conn.request(method, url.path.rstrip('/') + PREFIX + path, body=payload or None, headers=headers)
        response = conn.getresponse()
        if not 200 <= response.status < 300:
            raise _failure(response.status, response.read(ERROR_LIMIT))
        data = response.read(limit + 1)
        if len(data) > limit:
            raise WorkspaceError('评测服务响应超过限制', 502, 'eval_response_limit')
        return data, {key.lower(): value for key, value in response.getheaders()}
    except (OSError, http.client.HTTPException) as exc:
        raise WorkspaceError('评测服务不可用，请稍后重试', 503, 'eval_unavailable') from exc
    finally:
        conn.close()


def call(method, path, token, body=None):
    data, _ = request(method, path, token, body)
    try:
        return json.loads(data)
    except ValueError as exc:
        raise WorkspaceError('评测服务响应格式错误', 502, 'eval_invalid_response') from exc
