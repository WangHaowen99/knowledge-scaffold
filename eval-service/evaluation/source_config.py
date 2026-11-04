"""Safe connection forms and bounded, read-only database introspection."""
import re
import time
from urllib.parse import urlsplit

from .core import Failure, digest, endpoint
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
        value = {k: v for k, v in record.items() if k not in ('connection', 'schema_fingerprint')}
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

    def inspect(self, owner, provider, connection):
        if provider not in ('nl2sql_http', 'nl2sql_template'):
            raise Failure('provider_invalid', '仅 NL2SQL 连接支持数据库表字段读取', 422)
        schema, seen = {}, set()
        # This fixed SELECT reads metadata only, scoped to the selected database.
        for offset in range(0, 25601, 200):
            sql = ("SELECT TABLE_NAME AS table_name, COLUMN_NAME AS column_name, "
                   "COLUMN_TYPE AS data_type, IS_NULLABLE AS is_nullable "
                   "FROM information_schema.columns WHERE TABLE_SCHEMA = '" + connection['scene'] +
                   "' ORDER BY TABLE_NAME, ORDINAL_POSITION LIMIT 200 OFFSET " + str(offset))
            try:
                response = self.http.json('POST', connection['url'], '/api/v1/sql', token=connection.get('token', ''),
                                          json={'scene': connection['scene'], 'sql': sql}, limit=1024*1024)
            except Failure as exc:
                raise Failure('schema_unavailable', '读取数据库结构失败，请检查连接、Scene 与元数据查询权限后重试', 503, 'schema') from exc
            rows = response.get('data') if isinstance(response, dict) and response.get('success') is True else None
            if not isinstance(rows, list) or len(rows) > 200:
                raise Failure('schema_protocol', '数据服务未返回有效的数据库结构', 422, 'schema')
            for row in rows:
                if not isinstance(row, dict):
                    raise Failure('schema_protocol', '数据库字段格式无效', 422, 'schema')
                table, column = identifier(row.get('table_name')), identifier(row.get('column_name'))
                if (table, column) in seen:
                    raise Failure('schema_changed', '数据库结构读取不一致，请重新读取', 409, 'schema')
                seen.add((table, column))
                schema.setdefault(table, []).append({'name': column, 'type': str(row.get('data_type', ''))[:128], 'nullable': row.get('is_nullable') == 'YES'})
                if len(schema) > 100 or len(schema[table]) > 256:
                    raise Failure('schema_limit', '数据库结构超过 100 张表或每表 256 个字段的限制', 413, 'schema')
            if len(rows) < 200: break
        else:
            raise Failure('schema_limit','数据库结构读取达到上限，请缩小数据源范围',413,'schema')
        if not schema:
            raise Failure('schema_empty', '此数据库没有可读取的数据表，请检查 Scene 与访问权限', 422, 'schema')
        record = self.store.create(owner, 'source_inspections', {'provider': provider, 'fingerprint': digest([provider, connection]),
                                  'schema': [{'name': name, 'columns': columns} for name, columns in schema.items()],
                                  'tables': {name: [c['name'] for c in columns] for name, columns in schema.items()}, 'created_at': time.time()})
        return {'inspection_id': record['id'], 'schema': record['schema'], 'tables': record['tables'], 'read_at': record['created_at']}

    def apply_inspection(self, owner, provider, connection, inspection_id):
        record = self.store.get(owner, inspection_id, 'source_inspections')
        if record['fingerprint'] != digest([provider, connection]):
            raise Failure('schema_stale', '连接信息已变化，请重新读取数据库结构', 409, 'schema')
        return {'inspection_id': inspection_id, 'schema': record['schema'], 'tables': record['tables'], 'read_at': record['created_at']}
