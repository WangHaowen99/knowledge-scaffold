"""Configuration, safe failures, online identity and bounded HTTP transport."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx


class Failure(Exception):
    def __init__(self, code, message, status=400, phase='input'):
        super().__init__(message)
        self.code, self.message, self.status, self.phase = code, message, status, phase

    def payload(self):
        return dict(code=self.code, message=self.message, phase=self.phase,
                    retryable=self.status in (429, 502, 503, 504))


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value).encode()).hexdigest()


def endpoint(value):
    url = urlsplit(value)
    if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise Failure('configuration_missing', '依赖服务地址未正确配置', 503, 'configuration')
    return value.rstrip('/')


@dataclass
class Config:
    data_dir: Path
    auth_url: str = ''
    issuer: str = ''
    hub_url: str = ''
    hub_token: str = ''
    consumer_url: str = ''
    consumer_token: str = ''
    encryption_key: str = ''
    judge_url: str = ''
    judge_key: str = ''
    judge_model: str = ''
    judge_revision: str = ''
    sql_config: str = ''
    nl2sql_url: str = ''
    trusted_origins: str = ''

    @classmethod
    def environment(cls):
        return cls(Path(os.environ.get('EVAL_DATA_DIR', '.runtime')),
                   *[os.environ.get(name, '') for name in (
                       'EVAL_AUTH_URL', 'EVAL_AUTH_ISSUER', 'EVAL_HUB_URL', 'EVAL_HUB_SERVICE_TOKEN',
                       'EVAL_CONSUMER_API_URL', 'EVAL_CONSUMER_SERVICE_TOKEN', 'EVAL_ENCRYPTION_KEY',
                       'EVAL_JUDGE_BASE_URL', 'EVAL_JUDGE_API_KEY', 'EVAL_JUDGE_MODEL',
                       'EVAL_JUDGE_CONFIG_REVISION', 'EVAL_SQL_CONFIG', 'EVAL_NL2SQL_URL', 'EVAL_TRUSTED_ORIGINS')])


def trusted_origins(value):
    """Origins allowed to hand over a login, in browser origin form; non-origin entries are ignored."""
    result = []
    for item in value.split(','):
        url = urlsplit(item.strip())
        if url.scheme not in ('http', 'https') or not url.hostname or url.path.strip('/') or url.query \
                or url.fragment or url.username or url.password:
            continue
        host = f'[{url.hostname}]' if ':' in url.hostname else url.hostname
        default = 80 if url.scheme == 'http' else 443
        result.append(f'{url.scheme}://{host}' + (f':{url.port}' if url.port and url.port != default else ''))
    return result


class Transport:
    def request(self, method, base, path, *, token='', service='', workspace='', limit=2*1024*1024, **kwargs):
        headers = kwargs.pop('headers', {})
        if token:
            headers['Authorization'] = 'Bearer ' + token
        if service:
            headers['X-Hub-Service-Token'] = service
        if workspace:
            headers['X-Workspace-Id'] = workspace
        try:
            with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
                with client.stream(method, endpoint(base) + path, headers=headers, **kwargs) as response:
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > limit:
                            raise Failure('response_limit', '依赖响应超过限制', 502, 'dependency')
                    if not 200 <= response.status_code < 300:
                        # Do not relay provider errors, URLs, headers or secret-bearing bodies.
                        raise Failure('dependency_rejected', '依赖服务拒绝请求', response.status_code
                                      if response.status_code in (401,403,404,409,410,413,422,429) else 503, 'dependency')
                    return bytes(raw)
        except (httpx.HTTPError, ValueError) as exc:
            raise Failure('dependency_unavailable', '依赖服务不可用，请重试或恢复任务', 503, 'dependency') from exc

    def json(self, method, base, path, **kwargs):
        try:
            return json.loads(self.request(method, base, path, **kwargs))
        except (ValueError, UnicodeError) as exc:
            raise Failure('invalid_response', '依赖响应格式错误', 502, 'dependency') from exc


class Identity:
    def __init__(self, config, transport):
        self.config, self.transport = config, transport

    def account(self, token):
        """Owner digest and role; the role is read from the account service, never from token claims."""
        if not token:
            raise Failure('unauthenticated', '请登录', 401, 'authorization')
        value = self.transport.json('GET', self.config.auth_url, '/userinfo', token=token, limit=16384)
        if not isinstance(value, dict) or not isinstance(value.get('sub'), str) or not 0 < len(value['sub']) <= 256:
            raise Failure('invalid_identity', '身份验证失败', 401, 'authorization')
        if value.get('password_change_required'):
            raise Failure('account_action_required', '请先完成账号操作', 403, 'authorization')
        return digest([self.config.issuer or self.config.auth_url.rstrip('/'), value['sub']]), value.get('role', '')

    def verify(self, token):
        return self.account(token)[0]
