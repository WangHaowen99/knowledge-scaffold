"""Read-only graph scope checks and explicitly confirmed, atomic replacement."""
import hashlib
import threading
import time

from .core import Failure, canonical, digest
from .data_sources import identifier

MAX_NODES = 100000
MAX_RELATIONSHIPS = 200000
CHECK_SECONDS = 600
RECEIPT_LABEL = '_EvaluationGraphImport'


def graph_scope(connection):
    workspace = identifier(connection.get('workspace_property'))
    ontology = identifier(connection.get('ontology_property'))
    if workspace == ontology or any(not isinstance(connection.get(key), str) or not connection[key].strip()
                                    for key in ('workspace_value', 'ontology_value')):
        raise Failure('graph_scope_required', '图导入需要不同的 Workspace、Ontology 隔离字段及非空对应值', 422, 'data_import')
    return {key: connection[key] for key in ('workspace_property', 'workspace_value', 'ontology_property', 'ontology_value')}


def scope_target(connection):
    return digest([connection.get('uri'), connection.get('database', 'neo4j'), graph_scope(connection)])


def predicate(alias, scope):
    return (alias + '.`' + scope['workspace_property'] + '`=$workspace AND ' +
            alias + '.`' + scope['ontology_property'] + '`=$ontology')


def parameters(scope):
    return {'workspace': scope['workspace_value'], 'ontology': scope['ontology_value']}


def snapshot(tx, scope, include_ids=False):
    params = parameters(scope)
    match = 'MATCH (n) WHERE ' + predicate('n', scope)
    nodes = tx.run(match + ' RETURN count(n) AS total', **params).single()['total']
    internal = 'MATCH (a)-[r]->(b) WHERE ' + predicate('a', scope) + ' AND ' + predicate('b', scope)
    relationships = tx.run(internal + ' RETURN count(r) AS total', **params).single()['total']
    boundary = ('MATCH (a)-[r]-(b) WHERE ' + predicate('a', scope) +
                ' AND NOT coalesce((' + predicate('b', scope) + '), false)')
    external = tx.run(boundary + ' RETURN count(r) AS total', **params).single()['total']
    if nodes > MAX_NODES or relationships + external > MAX_RELATIONSHIPS:
        raise Failure('graph_scope_limit', '图范围超过 10 万个节点或 20 万条关系，请缩小范围后重新检查', 413, 'data_import')
    fingerprint = hashlib.sha256()
    identifiers = {'node': [], 'relationship': []}
    # Only bounded element identifiers are streamed; graph properties never leave the database.
    for kind, query, count, maximum in (
        ('node', match + ' RETURN elementId(n) AS id ORDER BY id LIMIT $maximum', nodes, MAX_NODES),
        ('relationship', internal + ' RETURN elementId(r) AS id ORDER BY id LIMIT $maximum', relationships, MAX_RELATIONSHIPS),
    ):
        observed = 0
        for record in tx.run(query, **params, maximum=maximum + 1):
            observed += 1
            if observed > maximum:
                raise Failure('graph_scope_changed', '图数据在检查期间发生变化，请重新检查并确认', 409, 'data_import')
            fingerprint.update(canonical([kind, record['id']]).encode())
            if include_ids: identifiers[kind].append(record['id'])
        if observed != count:
            raise Failure('graph_scope_changed', '图数据在检查期间发生变化，请重新检查并确认', 409, 'data_import')
    result = {'node_count': nodes, 'relationship_count': relationships, 'boundary_relationship_count': external,
              'snapshot': fingerprint.hexdigest()}
    if include_ids: result['identifiers'] = identifiers
    return result


class GraphImports:
    def __init__(self, store):
        self.store = store
        self.guard = threading.RLock()

    @staticmethod
    def connection(store, source, manifest_id):
        if source.get('mode') != 'connection' or source.get('provider') != 'neo4j' or not source.get('import_data'):
            raise Failure('graph_import_required', '请选择 Neo4j 连接并开启图数据导入', 422, 'data_import')
        connection = {**store.decrypt(source['connection']), 'ontology_value': manifest_id}
        graph_scope(connection)
        return connection

    @staticmethod
    def fingerprint(source, connection):
        return digest([source.get('id'), source.get('revision'), source.get('input_ids', []),
                       source.get('tables_snapshot'), connection])

    def inspect(self, owner, source, manifest_id, preparation_id=''):
        from neo4j import GraphDatabase, READ_ACCESS
        from neo4j.exceptions import DriverError, Neo4jError
        connection = self.connection(self.store, source, manifest_id)
        scope = graph_scope(connection)
        try:
            with GraphDatabase.driver(connection['uri'], auth=(connection.get('username', ''), connection.get('password', '')),
                                      connection_timeout=10) as driver:
                with driver.session(database=connection.get('database', 'neo4j'), default_access_mode=READ_ACCESS) as session:
                    with session.begin_transaction(timeout=30) as tx:
                        counts = snapshot(tx, scope)
        except (DriverError, Neo4jError, OSError) as exc:
            raise Failure('graph_check_unavailable', '无法检查旧图数据，请检查图库连接与读取权限后重试', 503, 'data_import') from exc
        checked_at = time.time()
        record = self.store.create(owner, 'graph_checks', {
            **counts, 'fingerprint': self.fingerprint(source, connection), 'preparation_id': preparation_id,
            'checked_at': checked_at, 'expires_at': checked_at + CHECK_SECONDS,
        })
        return {key: record[key] for key in ('id', 'node_count', 'relationship_count', 'boundary_relationship_count', 'expires_at')} | {'scope': scope}

    def authorize(self, owner, prep, check_id, replace_existing):
        if not check_id:
            raise Failure('graph_check_required', '启动前请检查当前 Workspace 与 Ontology 范围的旧图数据', 409, 'data_import')
        record = self.store.get(owner, check_id, 'graph_checks')
        connection = self.connection(self.store, prep['source'], prep['package_ref']['manifest_id'])
        if record['preparation_id'] != prep['id'] or record['fingerprint'] != self.fingerprint(prep['source'], connection):
            raise Failure('graph_check_stale', '数据源或知识包范围已变化，请重新检查旧图数据', 409, 'data_import')
        if record['expires_at'] < time.time():
            raise Failure('graph_check_expired', '旧图数据检查已过期，请重新检查并确认', 409, 'data_import')
        if record['boundary_relationship_count']:
            raise Failure('graph_boundary_conflict', '此范围存在跨隔离范围的关系，请先处理这些关系或调整范围', 409, 'data_import')
        if record['node_count'] and not replace_existing:
            raise Failure('graph_confirmation_required', '发现旧图数据，请确认删除后再导入新数据', 409, 'data_import')
        return {'snapshot': record['snapshot'], 'fingerprint': record['fingerprint'],
                'replace_existing': replace_existing, 'check_id': check_id}

    def ensure_current(self, run_id, source, manifest_id):
        from neo4j import GraphDatabase, READ_ACCESS
        from neo4j.exceptions import DriverError, Neo4jError
        connection = self.connection(self.store, source, manifest_id)
        try:
            with GraphDatabase.driver(connection['uri'], auth=(connection.get('username', ''), connection.get('password', '')),
                                      connection_timeout=10) as driver:
                with driver.session(database=connection.get('database', 'neo4j'), default_access_mode=READ_ACCESS) as session:
                    with session.begin_transaction(timeout=30) as tx:
                        marker = tx.run('MATCH (m:`' + RECEIPT_LABEL + '` {scope_fingerprint:$target}) '
                                        'RETURN m.run_id AS run_id', target=scope_target(connection)).single()
                        if not marker or marker['run_id'] != run_id:
                            raise Failure('graph_scope_changed', '本轮图数据已被后续导入替换，请重新创建评测并确认范围', 409, 'data_import')
        except (DriverError, Neo4jError, OSError) as exc:
            raise Failure('graph_import_unavailable', '无法核对本轮图数据，请检查连接后恢复', 503, 'data_import') from exc

    def import_data(self, run_id, source, connection, tables, mapping, relations):
        from neo4j import GraphDatabase
        from neo4j.exceptions import DriverError, Neo4jError
        scope = graph_scope(connection)
        consent = source.get('graph_confirmation')
        if not consent or consent['fingerprint'] != self.fingerprint(source, connection):
            raise Failure('graph_check_required', '本轮缺少有效的图数据范围确认，请重新创建评测并确认', 409, 'data_import')
        objects, edges = self.plan(scope, tables, mapping, relations)
        target = scope_target(connection)
        import_fingerprint = digest([target, tables, mapping, relations])
        params = parameters(scope)
        try:
            with self.guard:
                with GraphDatabase.driver(connection['uri'], auth=(connection.get('username', ''), connection.get('password', '')),
                                          connection_timeout=10) as driver:
                    with driver.session(database=connection.get('database', 'neo4j')) as session:
                        with session.begin_transaction(timeout=120) as tx:
                            marker = tx.run('MATCH (m:`' + RECEIPT_LABEL + '` {scope_fingerprint:$target}) '
                                            'RETURN m.run_id AS run_id, m.import_fingerprint AS fingerprint', target=target).single()
                            if marker and marker['run_id'] == run_id and marker['fingerprint'] == import_fingerprint:
                                return
                            current = snapshot(tx, scope, include_ids=True)
                            if current['snapshot'] != consent['snapshot'] or current['boundary_relationship_count']:
                                raise Failure('graph_scope_changed', '旧图数据范围已变化，请重新创建评测并确认；本次未替换数据', 409, 'data_import')
                            if current['node_count']:
                                if not consent['replace_existing']:
                                    raise Failure('graph_confirmation_required', '删除旧图数据尚未获得确认，本次未导入', 409, 'data_import')
                                tx.run('MATCH (a)-[r]->(b) WHERE ' + predicate('a', scope) + ' AND ' +
                                       predicate('b', scope) + ' AND elementId(r) IN $ids DELETE r',
                                       **params, ids=current['identifiers']['relationship']).consume()
                                # DELETE refuses newly added cross-scope edges and rolls the whole transaction back.
                                tx.run('MATCH (n) WHERE ' + predicate('n', scope) + ' AND elementId(n) IN $ids DELETE n',
                                       **params, ids=current['identifiers']['node']).consume()
                            remaining = tx.run('MATCH (n) WHERE ' + predicate('n', scope) + ' RETURN count(n) AS total', **params).single()['total']
                            if remaining:
                                raise Failure('graph_scope_changed', '图数据在替换期间发生变化，请重新确认；本次操作已回滚', 409, 'data_import')
                            for statement, rows in objects:
                                for offset in range(0, len(rows), 200):
                                    tx.run(statement, **params, rows=rows[offset:offset + 200]).consume()
                            for statement in edges:
                                tx.run(statement, **params).consume()
                            tx.run('MERGE (m:`' + RECEIPT_LABEL + '` {scope_fingerprint:$target}) '
                                   'SET m.run_id=$run_id, m.import_fingerprint=$fingerprint',
                                   target=target, run_id=run_id, fingerprint=import_fingerprint).consume()
                            tx.commit()
        except (DriverError, Neo4jError, OSError) as exc:
            raise Failure('graph_import_unavailable', '图数据替换未获确认成功，请检查连接或写入权限后恢复；恢复会先核对导入回执',
                          503, 'data_import') from exc

    @staticmethod
    def plan(scope, tables, mapping, relations):
        if not mapping or not mapping.get('objects'):
            raise Failure('mapping_required', '图导入缺少对象映射', 422, 'data_import')
        objects, edges = [], []
        for label, item in mapping['objects'].items():
            identifier(label)
            if label == RECEIPT_LABEL:
                raise Failure('graph_scope_conflict', '对象名与图导入回执保留名称冲突', 422, 'data_import')
            table = tables[item['sources'][0]['resource']]
            fields = item['sources'][0]['fields']
            if any(key in fields for key in (scope['workspace_property'], scope['ontology_property'])):
                raise Failure('graph_scope_conflict', '图隔离属性不能与业务字段重名', 422, 'data_import')
            for field in fields:
                identifier(field)
            rows = [{field: row[table['columns'].index(rule['source_column'])] for field, rule in fields.items()}
                    for row in table['rows']]
            keys = item['primary_key']
            if not keys or any(any(row.get(key) is None or row.get(key) == '' for key in keys) for row in rows):
                raise Failure('graph_key_missing', '图数据缺少主键', 422, 'data_import')
            key_expr = ('`' + scope['workspace_property'] + '`:$workspace,`' + scope['ontology_property'] + '`:$ontology,' +
                        ','.join('`' + identifier(key) + '`:row.`' + key + '`' for key in keys))
            objects.append(('UNWIND $rows AS row MERGE (n:`' + label + '` {' + key_expr + '}) SET n += row', rows))
        if not any(rows for _, rows in objects):
            raise Failure('empty_data', '没有可导入的图数据，请检查上传文件', 422, 'data_import')
        for source, items in relations.items():
            for edge in items:
                target = identifier(edge['target'])
                left = identifier(edge.get('sourceJoinProperty') or mapping['objects'][source]['primary_key'][0])
                right = identifier(edge.get('targetJoinProperty') or mapping['objects'][target]['primary_key'][0])
                edges.append('MATCH (a:`' + identifier(source) + '`), (b:`' + target + '`) WHERE ' + predicate('a', scope) +
                             ' AND ' + predicate('b', scope) + ' AND a.`' + left + '`=b.`' + right +
                             '` MERGE (a)-[:`' + identifier(edge['relation']) + '`]->(b)')
        return objects, edges
