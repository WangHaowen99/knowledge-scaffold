"""Safe connection forms and bounded connectivity checks."""
import re
import time
from urllib.parse import urlsplit

from .core import Failure, endpoint
from .data_sources import identifier

SQL_KEYS = ('url', 'scene', 'evaluation_workspace_id', 'token')
GRAPH_KEYS = ('uri', 'database', 'username', 'password', 'workspace_property', 'workspace_value', 'ontology_property')
GRAPH_SCHEMES = ('bolt', 'neo4j', 'bolt+s', 'neo4j+s', 'bolt+ssc', 'neo4j+ssc')


class SourceConfig:
    def __init__(self, store, http):
        self.store, self.http = store, http

    def connection(self, owner, provider, raw=None, existing=None):
        previous = {}
        if existing and existing.get('provider') == provider and existing.get('connection'):
            previous = self.store.decrypt(existing['connection'])
        if raw is None:
            if not previous:
                raise Failure('connection_missing', '请填写连接配置', 422)
            raw = previous
        if not isinstance(raw, dict):
            raise Failure('connection_invalid', '连接配置需要对象', 422)
        keys = GRAPH_KEYS if provider == 'neo4j' else SQL_KEYS
        if set(raw) - set(keys):
            raise Failure('connection_invalid', '连接配置包含不支持的字段', 422)
        if any(not isinstance(v,str) for v in raw.values()):
            raise Failure('connection_invalid','连接字段需要文本值',422)
        value = {key: str(raw.get(key, '')).strip() for key in keys}
        if any(len(v) > 4096 for v in value.values()):
            raise Failure('connection_invalid', '连接字段超过长度限制', 422)
        if provider!='neo4j':
            value['url']=endpoint(value['url'])
            if value['url'].endswith('/api/v1'): value['url']=value['url'][:-7]
        secret = 'password' if provider == 'neo4j' else 'token'
        target_keys = ('uri', 'username') if provider == 'neo4j' else ('url',)
        if not value[secret] and previous:
            if any(value[k] != previous.get(k, '') for k in target_keys):
                raise Failure('secret_required', '连接地址或账号已变化，请重新填写访问凭据', 422)
            value[secret] = previous.get(secret, '')
        if provider == 'neo4j':
            uri = urlsplit(value['uri'])
            extras = (uri.username, uri.password, uri.query, uri.fragment)
            if uri.scheme not in GRAPH_SCHEMES or not uri.hostname or any(extras):
                raise Failure('connection_invalid', '请填写有效的 Neo4j URL', 422)
            if not value['username'] or not value['password']:
                raise Failure('connection_invalid', '请填写图库账号与密码', 422)
            value['database'] = identifier(value['database'] or 'neo4j')
            value['workspace_property'] = identifier(value['workspace_property'] or 'd2o_workspace_id')
            if not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', value['workspace_value']):
                raise Failure('connection_invalid', 'Workspace 值须以小写字母开头，最多 64 个小写字母、数字、连字符或下划线', 422)
            if value['ontology_property']:
                identifier(value['ontology_property'])
                if value['ontology_property'] == value['workspace_property']:
                    raise Failure('connection_invalid', 'Workspace 与 Ontology 隔离字段不能相同', 422)
        else:
            if not re.fullmatch(r'[A-Za-z0-9_]{1,60}', value['scene']):
                raise Failure('connection_invalid', 'Scene 须为 1-60 个英文字母、数字或下划线', 422)
            if not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', value['evaluation_workspace_id']):
                raise Failure('connection_invalid', '请填写有效的评测工作空间 ID', 422)
        return value

    def view(self, owner, record):
        value = {k: v for k, v in record.items() if k != 'connection'}
        if record.get('connection'):
            config = self.store.decrypt(record['connection'])
            secret = 'password' if record.get('provider') == 'neo4j' else 'token'
            allowed = GRAPH_KEYS if secret == 'password' else SQL_KEYS
            value['connection_view'] = {k: config[k] for k in allowed if k != secret and k in config}
            value['secret_configured'] = bool(config.get(secret))
        value['inputs'] = []
        for fid in record.get('input_ids', []):
            try:
                item = self.store.get(owner, fid, 'inputs')
            except Failure as exc:
                if exc.status!=404: raise
                item = None
            if item:
                value['inputs'].append({k: item[k] for k in ('id', 'name', 'size', 'tables')})
        return value

    def test_connection(self, provider, connection):
        if provider not in ('nl2sql_http', 'nl2sql_template'):
            raise Failure('provider_invalid', '仅 NL2SQL 连接支持此连接测试', 422)
        try:
            response = self.http.json(
                'POST', connection['url'], '/api/v1/sql', token=connection.get('token', ''),
                json={'scene': connection['scene'], 'sql': 'SELECT 1'}, timeout=10, limit=64*1024)
        except Failure as exc:
            raise Failure('connection_unavailable', '连接测试失败，请检查服务地址、Scene、凭据与网络',
                          503, 'connection_test') from exc
        if not isinstance(response, dict) or response.get('success') is not True:
            raise Failure('connection_test_failed', '数据服务未通过连接测试，请检查 Scene 与访问权限',
                          422, 'connection_test')
        return {'success': True, 'message': '连接测试通过', 'tested_at': time.time()}
