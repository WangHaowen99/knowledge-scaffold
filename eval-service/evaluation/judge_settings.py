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
