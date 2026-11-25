"""Configuration, safe failures, online identity and bounded HTTP transport."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

import httpx

# Provider wording for a request beyond the model's context window (OpenAI-compatible gateways and common
# Chinese gateways); matched against the error body, which itself is never relayed.
CONTEXT_OVERFLOW = re.compile(
    r'context[_ ]length|maximum context|context window|too many tokens|prompt is too long|input is too long'
    r'|reduce the length|exceeds? the (?:model|maximum)|上下文长度|超出.{0,8}(?:上下文|长度)|输入过长', re.IGNORECASE)
# A dependency's machine-readable error code, e.g. consumer's ontology_name_conflict; only a bare identifier is kept.
UPSTREAM_CODE = re.compile(r'"error_code"\s*:\s*"([a-z][a-z0-9_]{2,63})"')


class Failure(Exception):
    def __init__(self, code, message, status=400, phase='input', *, upstream_status=None, context_overflow=False,
                 upstream_code='', details=None, retry_after=None):
        super().__init__(message)
        self.code, self.message, self.status, self.phase = code, message, status, phase
        # Structured, user-facing detail such as which objects conflict with the data tables; returned with the error.
        self.details = details
        # Available for contextual diagnostics, never included in the public payload.
        self.upstream_status = upstream_status
        # The provider said the request exceeds its model's context window; callers may send less and retry.
        self.context_overflow = context_overflow
        self.upstream_code = upstream_code
        # Seconds a rate-limited or unavailable dependency asked callers to wait, when it said so.
        self.retry_after = retry_after

    def payload(self):
        value = dict(code=self.code, message=self.message, phase=self.phase,
                     retryable=self.status in (429, 502, 503, 504))
        if self.details is not None:
            value['details'] = self.details
        return value


def transient(error):
    """A dependency failure that may pass by itself: rate limits, unavailable or overloaded services, timeouts."""
    return error.status in (429, 502, 503, 504)


def retry_after(value):
    """Seconds of a Retry-After header given as a number; dates and other forms are ignored."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if 0 <= seconds <= 3600 else None


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value).encode()).hexdigest()


def plain_http_url(url):
    """A split http(s) URL with a host and without credentials, query or fragment."""
    extras = (url.username, url.password, url.query, url.fragment)
    return url.scheme in ('http', 'https') and bool(url.hostname) and not any(extras)


def endpoint(value):
    if not plain_http_url(urlsplit(value)):
        raise Failure('configuration_missing', '依赖服务地址未正确配置', 503, 'configuration')
    return value.rstrip('/')


# Environment variables of the Config fields after data_dir, in field order.
ENVIRONMENT = ('EVAL_AUTH_URL', 'EVAL_AUTH_ISSUER', 'EVAL_HUB_URL', 'EVAL_HUB_SERVICE_TOKEN',
               'EVAL_CONSUMER_API_URL', 'EVAL_CONSUMER_SERVICE_TOKEN', 'EVAL_ENCRYPTION_KEY',
               'EVAL_JUDGE_BASE_URL', 'EVAL_JUDGE_API_KEY', 'EVAL_JUDGE_MODEL',
               'EVAL_JUDGE_CONFIG_REVISION', 'EVAL_SQL_CONFIG', 'EVAL_NL2SQL_URL', 'EVAL_TRUSTED_ORIGINS',
               'EVAL_MODELING_URL', 'EVAL_MODELING_SERVICE_TOKEN', 'EVAL_NOTIFICATION_MAX_ATTEMPTS',
               'EVAL_JUDGE_ALLOWED_HOSTS', 'EVAL_FEEDBACK_ENABLED', 'EVAL_FLYWHEEL_SERVICE_TOKEN',
               'EVAL_NOTIFY_FEEDBACK_RUNS', 'EVAL_FEEDBACK_RETENTION_DAYS', 'EVAL_FEEDBACK_WINDOW_DAYS',
               'EVAL_FEEDBACK_L1_MIN_USERS', 'EVAL_FEEDBACK_L1_MIN_DOWNVOTES', 'EVAL_FEEDBACK_SEAL_CLUSTERS',
               'EVAL_FEEDBACK_SEAL_IDLE_MINUTES')


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
    modeling_url: str = ''
    # Sent with completion notifications to onto-modeling; empty disables them.
    modeling_service_token: str = ''
    # Deliveries of one notification before it is given up as failed.
    notification_max_attempts: str = ''
    # Comma-separated host names a judge gateway may use although they resolve to a private address.
    judge_allowed_hosts: str = ''
    feedback_enabled: str = 'true'
    flywheel_token: str = ''
    # Whether runs built from consumer feedback are announced to onto-modeling; on once modeling files them by source.
    notify_feedback_runs: str = 'true'
    feedback_retention_days: str = ''
    feedback_window_days: str = ''
    feedback_min_users: str = ''
    feedback_min_downvotes: str = ''
    feedback_seal_clusters: str = ''
    feedback_seal_idle_minutes: str = ''

    def number(self, field, default):
        """A positive integer setting; empty or invalid values fall back to the default."""
        try:
            value = int(str(getattr(self, field) or '').strip())
        except ValueError:
            return default
        return value if value > 0 else default

    def flag(self, field):
        return str(getattr(self, field) or '').strip().lower() in ('1', 'true', 'yes', 'on')

    @classmethod
    def environment(cls):
        defaults = {'EVAL_FEEDBACK_ENABLED': 'true', 'EVAL_NOTIFY_FEEDBACK_RUNS': 'true'}
        return cls(Path(os.environ.get('EVAL_DATA_DIR', '.runtime')),
                   *[os.environ.get(name) or defaults.get(name, '') for name in ENVIRONMENT])


def trusted_origins(value):
    """Origins allowed to hand over a login, in browser origin form; non-origin entries are ignored."""
    result = []
    for item in value.split(','):
        url = urlsplit(item.strip())
        if not plain_http_url(url) or url.path.strip('/'):
            continue
        host = f'[{url.hostname}]' if ':' in url.hostname else url.hostname
        default = 80 if url.scheme == 'http' else 443
        result.append(f'{url.scheme}://{host}' + (f':{url.port}' if url.port and url.port != default else ''))
    return result


class Transport:
    def request(self, method, base, path, *, token='', service='', workspace='', limit=2*1024*1024, timeout=30, **kwargs):
        headers = kwargs.pop('headers', {})
        if token:
            headers['Authorization'] = 'Bearer ' + token
        if service:
            headers['X-Hub-Service-Token'] = service
        if workspace:
            headers['X-Workspace-Id'] = workspace
        try:
            with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
                with client.stream(method, endpoint(base) + path, headers=headers, **kwargs) as response:
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > limit:
                            raise Failure('response_limit', '依赖响应超过限制', 502, 'dependency')
                    if not 200 <= response.status_code < 300:
                        # Do not relay provider errors, URLs, headers or secret-bearing bodies; only whether the
                        # input was too long for the model and a bare error code leave this function.
                        text = bytes(raw[:16384]).decode('utf-8', 'ignore')
                        overflow = response.status_code == 413 or bool(CONTEXT_OVERFLOW.search(text))
                        code = UPSTREAM_CODE.search(text)
                        raise Failure('dependency_rejected', '依赖服务拒绝请求', response.status_code
                                      if response.status_code in (401,403,404,409,410,413,422,429) else 503, 'dependency',
                                      upstream_status=response.status_code, context_overflow=overflow,
                                      upstream_code=code.group(1) if code else '',
                                      retry_after=retry_after(response.headers.get('retry-after')))
                    return bytes(raw)
        except httpx.TimeoutException as exc:
            raise Failure('dependency_timeout', '依赖服务响应超时，请稍后重试', 504, 'dependency') from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise Failure('dependency_unavailable', '依赖服务不可用，请重试或恢复任务', 503, 'dependency') from exc

    def json(self, method, base, path, **kwargs):
        try:
            return json.loads(self.request(method, base, path, **kwargs))
        except ValueError as exc:  # also UnicodeDecodeError
            raise Failure('invalid_response', '依赖响应格式错误', 502, 'dependency') from exc


class Identity:
    def __init__(self, config, transport):
        self.config, self.transport = config, transport

    def profile(self, token):
        """Display name and role come from validated userinfo, never unverified token claims."""
        if not token:
            raise Failure('unauthenticated', '请登录', 401, 'authorization')
        value = self.transport.json('GET', self.config.auth_url, '/userinfo', token=token, limit=16384)
        if not isinstance(value, dict) or not isinstance(value.get('sub'), str) or not 0 < len(value['sub']) <= 256:
            raise Failure('invalid_identity', '身份验证失败', 401, 'authorization')
        if value.get('password_change_required'):
            raise Failure('account_action_required', '请返回建模或消费页面修改初始密码，重新登录后再打开评测', 403, 'authorization')
        return {'subject': digest([self.config.issuer or self.config.auth_url.rstrip('/'), value['sub']]),
                'username': value['sub'], 'role': value.get('role', '')}

    def account(self, token):
        """Owner digest and role; stable ownership is independent of display data."""
        profile = self.profile(token)
        return profile['subject'], profile['role']

    def verify(self, token):
        return self.account(token)[0]

    def renew(self, refresh_token):
        """A new token pair for a refresh token; the account service refuses revoked or expired ones."""
        value = self.transport.json('POST', self.config.auth_url, '/token', limit=16384,
                                    json={'grant_type': 'refresh_token', 'refresh_token': refresh_token})
        if not isinstance(value, dict) or not isinstance(value.get('access_token'), str) or not value['access_token']:
            raise Failure('invalid_identity', '身份验证失败', 401, 'authorization')
        return value
