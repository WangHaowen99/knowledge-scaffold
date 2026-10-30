"""Judge model configuration (MODEL-CONFIG-01).

The shared default comes from an administrator's saved settings, otherwise from the deployment.
It applies to every user's runs, so only administrators may change or disable it. Each user may save a
personal configuration that overrides the default for their own runs only, and remove it to
return to the default. API keys are stored encrypted and never returned.

Disabling the default only withdraws it from users' judging; the built-in NL2SQL service still uses it.
"""
import time
import uuid
from urllib.parse import urlsplit

from cryptography.fernet import InvalidToken

from .core import Failure

KEY = 'judge'
ENABLED_KEY = 'judge:default-enabled'
DISABLED = dict(url='', key='', model='', revision='', source='disabled')


def personal_key(owner):
    return f'{KEY}:user:{owner}'


def connection_test_failure(error):
    """Explain model gateway failures without returning provider bodies, URLs or keys."""
    status = error.upstream_status or error.status
    if error.code == 'dependency_timeout' or status == 504:
        return Failure('judge_test_timeout', '模型服务响应超时，请稍后重试，或检查网关和模型服务的响应速度。', 504, 'judging')
    if error.code == 'dependency_rejected':
        diagnostics = {
            401: ('judge_test_authentication', '模型认证失败：API Key 无效或已过期，请重新填写后重试。', 422),
            403: ('judge_test_permission', '模型访问被拒绝：当前 API Key 无权调用该模型，请检查模型授权。', 422),
            404: ('judge_test_not_found', '未找到模型或接口：请核对网关地址和模型名称，确认网关支持 /chat/completions。', 422),
            400: ('judge_test_request', '模型拒绝测试请求：请核对模型名称，并确认模型支持工具调用和结构化判分。', 422),
            422: ('judge_test_request', '模型拒绝测试请求：请核对模型名称，并确认模型支持工具调用和结构化判分。', 422),
            429: ('judge_test_rate_limit', '模型服务请求受限：请检查调用额度或并发限制，稍后重试。', 429),
        }
        if status in diagnostics:
            code, message, public_status = diagnostics[status]
            # A model API Key failure must not be mistaken for an expired user login.
            return Failure(code, message, public_status, 'judging')
        if status >= 500:
            return Failure('judge_test_service', '模型服务暂时不可用，请检查网关和模型服务状态，稍后重试。', 503, 'judging')
    if error.code == 'dependency_unavailable':
        return Failure('judge_test_connection', '无法连接模型服务，请检查网关地址、网络连通性与证书配置。', 503, 'judging')
    if error.code in ('judge_protocol', 'invalid_response', 'response_limit'):
        return Failure('judge_test_protocol', '模型返回内容不符合判分要求，请确认网关兼容 Chat Completions，且模型支持工具调用和结构化判分。', 422, 'judging')
    return Failure('judge_test_failed', '测试连接失败：模型服务未能完成判分请求，请检查网关、模型和访问权限后重试。', 422, 'judging')


def gateway(value):
    value = (value or '').strip().rstrip('/')
    url = urlsplit(value)
    if len(value) > 2048 or url.scheme not in ('http', 'https') or not url.hostname or url.username \
            or url.password or url.query or url.fragment:
        raise Failure('judge_settings_invalid', '判分模型网关须为不含账号和查询参数的 http(s) 地址', 422)
    return value


class JudgeSettings:
    def __init__(self, config, store):
        self.config, self.store = config, store

    def _saved(self, key, source):
        saved = self.store.setting(key)
        if not saved:
            return None
        return dict(url=saved['base_url'], key=saved['api_key'], model=saved['model'], revision=saved['revision'],
                    source=source)

    def default_enabled(self):
        saved = self.store.setting(ENABLED_KEY)
        return saved is None or saved['enabled']

    def set_default_enabled(self, enabled, owner):
        # The default keeps its revision, so runs stopped while it was disabled can resume once re-enabled.
        self.store.put_setting(ENABLED_KEY, dict(enabled=enabled, updated_by=owner, updated_at=time.time()))

    def _stored(self, owner=None):
        """Values in effect for the owner, or the shared default without one; saved keys are still encrypted."""
        if owner:
            personal = self._saved(personal_key(owner), 'personal')
            if personal:
                return personal
            if not self.default_enabled():
                return dict(DISABLED)
        found = self._saved(KEY, 'settings')
        if found:
            return found
        c = self.config
        return dict(url=c.judge_url.rstrip('/'), key=c.judge_key, model=c.judge_model, revision=c.judge_revision,
                    source='deployment')

    def _decrypt(self, current):
        if current['source'] in ('settings', 'personal'):
            try:
                current['key'] = self.store.decrypt(current['key'])
            except InvalidToken as exc:
                who = '请重新保存你的判分模型配置' if current['source'] == 'personal' else '请管理员重新保存'
                raise Failure('judge_settings_unreadable', '判分模型配置无法解密，' + who, 503, 'configuration') from exc
        return current

    def effective(self, owner=None):
        return self._decrypt(self._stored(owner))

    def _personal(self, owner):
        saved = self._saved(personal_key(owner), 'personal')
        return self._decrypt(saved) if saved else None

    def candidate(self, base_url, model, api_key, owner=None):
        """Submitted values; an empty key reuses the key of the configuration being edited, for the same gateway.

        A personal configuration never falls back to the shared key, so users cannot spend it on models they choose.
        """
        url, model = gateway(base_url), (model or '').strip()
        if not 0 < len(model) <= 200:
            raise Failure('judge_settings_invalid', '请填写判分模型名称', 422)
        if not api_key:
            current = self._personal(owner) if owner else self.effective()
            if not current or not current['key'] or current['url'] != url:
                raise Failure('judge_key_required', '请填写 API Key；仅在网关不变时可留空沿用当前配置的 Key', 422)
            api_key = current['key']
        return dict(url=url, key=api_key, model=model)

    def _put(self, key, value, previous, prefix, owner):
        # Re-saving the same configuration keeps its revision, so queued runs stay valid.
        same = previous and all(previous[k] == value[k] for k in ('url', 'key', 'model'))
        revision = previous['revision'] if same else prefix + uuid.uuid4().hex[:8]
        self.store.put_setting(key, dict(base_url=value['url'], model=value['model'],
                                         api_key=self.store.encrypt(value['key']), revision=revision,
                                         updated_by=owner, updated_at=time.time()))

    def save(self, base_url, model, api_key, owner):
        """Shared default, saved by an administrator."""
        value = self.candidate(base_url, model, api_key)
        try:
            current = self.effective()
        except Failure:
            current = None  # An unreadable saved key is simply replaced.
        self._put(KEY, value, current if current and current['source'] == 'settings' else None, 'ui-', owner)

    def reset(self):
        self.store.put_setting(KEY, None)

    def save_personal(self, owner, base_url, model, api_key):
        value = self.candidate(base_url, model, api_key, owner)
        try:
            previous = self._personal(owner)
        except Failure:
            previous = None
        self._put(personal_key(owner), value, previous, 'me-', owner)

    def reset_personal(self, owner):
        self.store.put_setting(personal_key(owner), None)

    def view(self, admin):
        """Shared default only; base URL and key state are visible to administrators."""
        c = self._stored()
        value = dict(source=c['source'], model=c['model'], revision=c['revision'], editable=admin,
                     configured=all((c['url'], c['key'], c['model'], c['revision'])), enabled=self.default_enabled())
        if admin:
            value.update(base_url=c['url'], api_key_set=bool(c['key']))
        return value

    def active(self, owner):
        """The configuration this user's new runs will use."""
        c = self._stored(owner)
        return dict(source=c['source'], model=c['model'], revision=c['revision'],
                    configured=all((c['url'], c['key'], c['model'], c['revision'])))

    def personal_view(self, owner):
        saved = self._saved(personal_key(owner), 'personal')
        personal = saved and dict(base_url=saved['url'], model=saved['model'], revision=saved['revision'],
                                  api_key_set=bool(saved['key']))
        return {'personal': personal, 'active': self.active(owner)}
