"""Judge model configuration (MODEL-CONFIG-01).

Settings saved by an administrator replace the deployment values as a whole. They apply to every
user's runs and receive evaluation questions and answers, so only administrators may change them.
The API key is stored encrypted and never returned.
"""
import time
import uuid
from urllib.parse import urlsplit

from cryptography.fernet import InvalidToken

from .core import Failure

KEY = 'judge'


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

    def _stored(self):
        """Current values; a saved key is still encrypted here."""
        saved = self.store.setting(KEY)
        if saved:
            return dict(url=saved['base_url'], key=saved['api_key'], model=saved['model'], revision=saved['revision'],
                        source='settings')
        c = self.config
        return dict(url=c.judge_url.rstrip('/'), key=c.judge_key, model=c.judge_model, revision=c.judge_revision,
                    source='deployment')

    def effective(self):
        current = self._stored()
        if current['source'] == 'settings':
            try:
                current['key'] = self.store.decrypt(current['key'])
            except InvalidToken as exc:
                raise Failure('judge_settings_unreadable', '判分模型配置无法解密，请管理员重新保存', 503, 'configuration') from exc
        return current

    def candidate(self, base_url, model, api_key):
        """Submitted values; an empty key reuses the current key only for the same gateway."""
        url, model = gateway(base_url), (model or '').strip()
        if not 0 < len(model) <= 200:
            raise Failure('judge_settings_invalid', '请填写判分模型名称', 422)
        if not api_key:
            current = self.effective()
            if not current['key'] or current['url'] != url:
                raise Failure('judge_key_required', '更换网关地址时须重新填写 API Key', 422)
            api_key = current['key']
        return dict(url=url, key=api_key, model=model)

    def save(self, base_url, model, api_key, owner):
        value = self.candidate(base_url, model, api_key)
        try:
            current = self.effective()
        except Failure:
            current = None  # An unreadable saved key is simply replaced.
        # Re-saving the same configuration keeps its revision, so queued runs stay valid.
        same = current and current['source'] == 'settings' and all(current[k] == value[k] for k in ('url', 'key', 'model'))
        revision = current['revision'] if same else 'ui-' + uuid.uuid4().hex[:8]
        self.store.put_setting(KEY, dict(base_url=value['url'], model=value['model'],
                                         api_key=self.store.encrypt(value['key']), revision=revision,
                                         updated_by=owner, updated_at=time.time()))

    def reset(self):
        self.store.put_setting(KEY, None)

    def view(self, admin):
        c = self._stored()
        value = dict(source=c['source'], model=c['model'], revision=c['revision'], editable=admin,
                     configured=all((c['url'], c['key'], c['model'], c['revision'])))
        if admin:
            value.update(base_url=c['url'], api_key_set=bool(c['key']))
        return value
