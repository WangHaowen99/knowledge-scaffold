"""Safe connection forms and bounded connectivity checks."""
import re
import time
from urllib.parse import urlsplit

from .core import Failure, endpoint
from .data_sources import identifier

SQL_KEYS = ('url', 'scene', 'evaluation_workspace_id', 'token')
GRAPH_KEYS = ('uri', 'database', 'username', 'password', 'workspace_property', 'workspace_value', 'ontology_property')
GRAPH_SCHEMES = ('bolt', 'neo4j', 'bolt+s', 'neo4j+s', 'bolt+ssc', 'neo4j+ssc')
# 评测隔离范围：在新建评测选中数据源时填写，随数据源保存；保存连接时可以先空着，准备评测时必须已填写
GRAPH_SCOPE_KEYS = ('workspace_property', 'workspace_value', 'ontology_property')
SQL_SCOPE_KEYS = ('evaluation_workspace_id',)
SCOPE_VALUE = re.compile(r'[a-z][a-z0-9_-]{0,63}')


def scope_missing(provider, connection):
    """Why the connection cannot be evaluated yet, or '' when its isolation scope is filled in."""
    if provider == 'neo4j':
        return '' if connection.get('workspace_value') else '请在新建评测中选中该数据源，填写 Workspace 对应值'
    return '' if connection.get('evaluation_workspace_id') else '请在新建评测中选中该数据源，填写评测工作空间 ID'


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
            if value['workspace_value'] and not SCOPE_VALUE.fullmatch(value['workspace_value']):
                raise Failure('connection_invalid', 'Workspace 值须以小写字母开头，最多 64 个小写字母、数字、连字符或下划线', 422)
            if value['ontology_property']:
                identifier(value['ontology_property'])
                if value['ontology_property'] == value['workspace_property']:
                    raise Failure('connection_invalid', 'Workspace 与 Ontology 隔离字段不能相同', 422)
        else:
            if not re.fullmatch(r'[A-Za-z0-9_]{1,60}', value['scene']):
                raise Failure('connection_invalid', 'Scene 须为 1-60 个英文字母、数字或下划线', 422)
            if value['evaluation_workspace_id'] and not SCOPE_VALUE.fullmatch(value['evaluation_workspace_id']):
                raise Failure('connection_invalid', '评测工作空间 ID 须以小写字母开头，最多 64 个小写字母、数字、连字符或下划线', 422)
        return value

    def scope(self, owner, record, values):
        """The record's connection with only its isolation scope replaced; secrets and the rest stay as saved."""
        provider = record.get('provider')
        keys = GRAPH_SCOPE_KEYS if provider == 'neo4j' else SQL_SCOPE_KEYS
        if any(value is not None for key, value in values.items() if key not in keys):
            raise Failure('connection_invalid', '隔离范围包含该数据源不支持的字段', 422)
        current = self.store.decrypt(record['connection'])
        value = self.connection(owner, provider, {**current, **{k: values[k] for k in keys if values.get(k) is not None}}, record)
        missing = scope_missing(provider, value)
        if missing:
            raise Failure('scope_required', missing, 422)
        if provider == 'neo4j' and record.get('import_data') and not value['ontology_property']:
            raise Failure('graph_scope_required', '导入图数据需要 Ontology 隔离字段', 422)
        return value

    def require_scope(self, source):
        """An external connection evaluates in the scope chosen for it in the new-evaluation wizard."""
        if source.get('mode') != 'connection' or not source.get('connection'):
            return
        missing = scope_missing(source.get('provider'), self.store.decrypt(source['connection']))
        if missing:
            raise Failure('scope_required', missing, 422)

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
