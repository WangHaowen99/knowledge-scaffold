"""以当前用户自己的登录调用独立评测服务（ADR-0092），请求和响应都有上限。

评测轮次与产物归评测服务所有，按调用者隔离。只有登记或使用用户本人做出的委托（知识包数据飞轮，ADR-0108）时
才带建模的服务身份。上游的响应体、地址和令牌一律不回显：失败映射成固定文案，只有评测服务自己写给用户的
409 说明原样转给用户。
"""
import http.client
import json
import os
import re
import urllib.parse

from .. import config
from .delegated import Delegated
from .workspace import WorkspaceError

PREFIX = '/evaluation/api'
JSON_LIMIT = 2 * 1024 * 1024
ERROR_LIMIT = 64 * 1024
_CONFLICT_CODE = re.compile(r'^[a-z][a-z0-9_]{0,63}$')
LOGIN_CODES = ('login_expired', 'login_missing')


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


def _detail(raw):
    """The evaluation service's own (code, message, details); ('', '', None) when the body is not one of its errors."""
    try:
        value = json.loads(raw)
    except ValueError:
        return '', '', None
    detail = value if isinstance(value, dict) else {}
    code, message = detail.get('code'), detail.get('message')
    if not isinstance(code, str) or not isinstance(message, str):
        return '', '', None
    if _CONFLICT_CODE.fullmatch(code) and message.strip():
        details = detail.get('details')
        return code, message.strip()[:300], details if isinstance(details, dict) else None
    return '', '', None


def _failure(status, raw):
    code, message, details = _detail(raw) if status in (401, 409) else ('', '', None)
    if status == 401:
        # 委托保存的后台评测登录失效：保留评测服务的说明与错误码，飞轮据此只标记登录失效、不撤销委托
        if code in LOGIN_CODES:
            return WorkspaceError(message, 401, code)
        return WorkspaceError('评测服务未接受当前登录，请重新登录', 401, 'eval_unauthorized')
    if status in (403, 404):
        # 不区分“无权访问”与“不存在”，评测记录按发起人私有
        return WorkspaceError('评测记录不存在或无权访问', 404, 'eval_not_found')
    if status == 409:
        if code:
            error = WorkspaceError(message, 409, code)
            error.details = details  # 如删除旧图数据的提示带的确认码
            return error
        return WorkspaceError('评测服务拒绝了请求，请刷新后重试', 409, 'eval_conflict')
    # 服务连得上但返回了错误：带上状态码，和“连不上”区分开，排查时才知道该看哪一侧
    return WorkspaceError(f'评测服务返回错误（HTTP {status}），请稍后重试', 503, 'eval_unavailable')


def _service_credential():
    secret = os.environ.get('ONTO_EVALUATION_SERVICE_TOKEN', '')
    if not secret:
        raise WorkspaceError('未配置评测服务通知凭据', 503, 'eval_unconfigured')
    return secret


def _caller_headers(token, service, refresh, delegation):
    if isinstance(token, Delegated):
        return {'X-Modeling-Service-Token': _service_credential(), 'X-Evaluation-Delegation': token.handle}
    headers = {'Authorization': f'Bearer {token}'}
    if service:
        headers['X-Modeling-Service-Token'] = _service_credential()
    if delegation:
        headers['X-Evaluation-Delegation'] = delegation
    if refresh:
        # 只在这次请求里转交给评测服务，建模侧不保存、不记录
        headers['X-Refresh-Token'] = refresh
    return headers


def request(method, path, token, body=None, limit=JSON_LIMIT, service=False, refresh='', delegation=''):
    """2xx 响应的 (字节, 响应头)；path 相对 /evaluation/api。"""
    if not token:
        raise WorkspaceError('评测服务需要登录用户身份', 401, 'eval_unauthorized')
    url = _endpoint()
    headers = {**_caller_headers(token, service, refresh, delegation), 'Accept': '*/*'}
    conn_type = http.client.HTTPSConnection if url.scheme == 'https' else http.client.HTTPConnection
    conn = conn_type(url.hostname, url.port, timeout=30)
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
    return _parsed(data)


def _parsed(data):
    """A 2xx answer that is not JSON (say, a page of another server) is an error like any other."""
    try:
        return json.loads(data)
    except ValueError as exc:
        raise WorkspaceError('评测服务响应格式错误', 502, 'eval_invalid_response') from exc


def delegate(token, package_id, seconds, handle, refresh=''):
    """Register, with the user's login, that modeling may read this package's evaluations later.

    ``handle`` is chosen by modeling and written down before asking (grants), so a registration whose answer gets
    lost can still be revoked. With a refresh token the delegation also holds a login of its own, so evaluations
    can be started on it."""
    body = {'ontology_id': package_id, 'expires_in': seconds, 'delegation': handle}
    data, _ = request('POST', '/delegations', token, body, service=True, refresh=refresh)
    return _parsed(data)


def renew_delegation(handle, token, seconds):
    """The user is back: their delegation runs for the chosen period again, keeping its login."""
    data, _ = request('POST', '/delegations/current/renewal', token, {'expires_in': seconds},
                      service=True, delegation=handle)
    return _parsed(data)


def rerun(credential, run_id, request_id, package_ref, refresh='', confirmation=''):
    """Evaluate another release exactly as run ``run_id`` did: same evaluation set, data source and model.

    ``confirmation`` is the user's own confirmation to delete old graph data: the code of the refusal that
    showed that data. A delegation cannot give it."""
    body = {'request_id': request_id, 'package_ref': package_ref}
    if confirmation:
        body.update(replace_existing_graph=True, graph_confirmation_code=confirmation)
    data, _ = request('POST', f'/runs/{run_id}/reruns', credential, body, refresh=refresh)
    return _parsed(data)


def undelegate(handle):
    request('DELETE', '/delegations/current', Delegated(handle))
