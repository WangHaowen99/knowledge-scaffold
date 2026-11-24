"""Read-only graph scope checks and confirmed, atomic replacement.

The user confirms replacing old data in the scope. A rerun of an earlier evaluation needs no confirmation when
the scope holds exactly what an evaluation of the same user imported: the receipt keeps a fingerprint of the
imported data, so anything added or removed since makes the rerun ask again.
"""
import hashlib
import json
import logging
import re
import threading
import time

from .core import Failure, canonical, digest
from .data_sources import identifier, graph_keys
from .mapping_rules import valid_relation_type
from .field_types import TYPE_RULE_VERSION, convert_value, field_kind
from .declared_types import parse_declared_type

MAX_NODES = 100000
MAX_RELATIONSHIPS = 200000
CHECK_SECONDS = 600
RECEIPT_LABEL = '_EvaluationGraphImport'
logger = logging.getLogger(__name__)


def resolved_field_kinds(mapping, relations):
    kinds, references = {}, set()
    for label, item in mapping['objects'].items():
        resource = item['sources'][0]['resource']
        declarations = item.get('fields', {})
        kinds[label] = {}
        for field, rule in item['sources'][0]['fields'].items():
            declared = declarations.get(field, {}).get('type') or rule.get('type')
            kinds[label][field] = field_kind(declared, f'对象 {label}，表 {resource}，字段 {field}')
            if parse_declared_type(declared)[0] == '对象引用':
                references.add((label, field))
    neighbors = {endpoint: set() for endpoint in references}
    for source, items in relations.items():
        for edge in items:
            target = edge['target']
            left = (source, edge.get('sourceJoinProperty') or mapping['objects'][source]['primary_key'][0])
            right = (target, edge.get('targetJoinProperty') or mapping['objects'][target]['primary_key'][0])
            if left in references or right in references:
                neighbors.setdefault(left, set()).add(right)
                neighbors.setdefault(right, set()).add(left)
    visited = set()
    for start in sorted(references):
        if start in visited:
            continue
        component, pending, concrete = set(), [start], set()
        while pending:
            endpoint = pending.pop()
            if endpoint in component:
                continue
            component.add(endpoint)
            label, field = endpoint
            kind = kinds[label].get(field)
            if endpoint not in references and kind:
                concrete.add(kind)
            pending.extend(neighbors.get(endpoint, set()) - component)
        visited.update(component)
        if len(concrete) > 1 and concrete != {'integer', 'number'}:
            label, field = start
            raise Failure('graph_join_type_conflict', f'对象引用字段 {label}.{field} 的关联声明类型不一致',
                          422, 'data_import')
        resolved = 'integer' if 'integer' in concrete else next(iter(concrete), 'string')
        for label, field in component & references:
            kinds[label][field] = resolved
    return kinds


# 图里的对象名、字段名与 consumer 查询时同一规则：语句中一律用反引号引用，所以「合成对象（甲类）」这类带全角标点
# 的业务名也能导入。禁止反引号、控制字符和首尾空白；反斜杠可能在解析名称前构成 Cypher 的 Unicode 转义（如 \u0060
# 即反引号），同样禁止。关系名按 relation_type 的规则；Workspace / Ontology 隔离字段仍按严格的标识符检查
GRAPH_NAME_LIMIT = 128
_CONTROL = re.compile(r'[\x00-\x1f\x7f]')


def _usable_graph_name(value):
    if not isinstance(value, str) or not value or len(value) > GRAPH_NAME_LIMIT:
        return False
    if value != value.strip() or _CONTROL.search(value):
        return False
    return '`' not in value and chr(92) not in value


def graph_name(value, kind):
    if not _usable_graph_name(value):
        shown = _CONTROL.sub('', str(value or ''))[:64]
        raise Failure('invalid_graph_name', f'{kind}「{shown}」不能用于图数据：不能为空、不能含反引号、反斜杠或控制字符、'
                      f'首尾不能有空格，最多 {GRAPH_NAME_LIMIT} 个字符', 422, 'data_import')
    return value


def check_names(mapping, relations):
    """Every name the import writes into the graph; run before importing, and when preparing, so it fails early."""
    for label, item in (mapping or {}).get('objects', {}).items():
        graph_name(label, '对象名')
        for source in item.get('sources', [])[:1]:
            for field in source.get('fields', {}):
                graph_name(field, f'对象「{label}」的字段名')
        for key in item.get('primary_key', []):
            graph_name(key, f'对象「{label}」的主键')
    for source, items in (relations or {}).items():
        graph_name(source, '关系的源对象')
        for edge in items:
            graph_name(edge.get('target'), '关系的目标对象')
            relation_type(edge.get('relation'))
            for key in ('sourceJoinProperty', 'targetJoinProperty'):
                if edge.get(key):
                    graph_name(edge[key], '关系的关联字段')


def relation_type(value):
    if not valid_relation_type(value):
        raise Failure('invalid_relation_type', '图关系名称包含不安全字符或超过长度限制', 422, 'data_import')
    return value.replace('`', '``')


def graph_scope(connection):
    if not isinstance(connection, dict):
        raise Failure('connection_invalid', '连接配置需要对象', 422, 'data_import')
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
    node_ids, relationship_ids = [], []
    # Labels, types and properties only feed the digest, so an edited value is detected; they are never
    # stored, logged or returned. The node and relationship counts stay bounded as above.
    for kind, query, count, maximum, ids in (
        ('node', match + ' RETURN elementId(n) AS id, labels(n) AS kind, properties(n) AS properties '
         'ORDER BY id LIMIT $maximum', nodes, MAX_NODES, node_ids),
        ('relationship', internal + ' RETURN elementId(r) AS id, type(r) AS kind, properties(r) AS properties '
         'ORDER BY id LIMIT $maximum', relationships, MAX_RELATIONSHIPS, relationship_ids),
    ):
        observed = 0
        for record in tx.run(query, **params, maximum=maximum + 1):
            observed += 1
            if observed > maximum:
                raise Failure('graph_scope_changed', '图数据在检查期间发生变化，请重新检查并确认', 409, 'data_import')
            # Temporal and spatial values are not JSON types; their text form is stable.
            fingerprint.update(json.dumps([kind, record['id'], record['kind'], record['properties']], default=str,
                                          ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())
            if include_ids: ids.append(record['id'])
        if observed != count:
            raise Failure('graph_scope_changed', '图数据在检查期间发生变化，请重新检查并确认', 409, 'data_import')
    result = {'node_count': nodes, 'relationship_count': relationships, 'boundary_relationship_count': external,
              'snapshot': fingerprint.hexdigest()}
    if include_ids: result['identifiers'] = {'node': node_ids, 'relationship': relationship_ids}
    return result


def import_identity(connection, tables, mapping, relations):
    """Identifies what an import would write: the target scope, the data, the mapping and the relations."""
    return digest([TYPE_RULE_VERSION, scope_target(connection), tables, mapping, relations])


def data_identity(tables):
    """The uploaded data alone, so a changed import can be told apart from a changed mapping."""
    return digest(tables)


def import_state(counts, marker, expected, expected_data=''):
    """How the graph scope compares with the data a prepared run would import."""
    if not counts['node_count']:
        return 'empty'
    if not expected:
        return 'unknown'
    if not marker:
        return 'unrecorded'
    if marker.get('fingerprint') != expected:
        # The receipt is keyed by the target scope, so the same data means the mapping or relations changed.
        # Receipts written before the data was recorded separately cannot tell the two apart.
        same_data = bool(expected_data) and marker.get('data_fingerprint') == expected_data
        return 'mapping_changed' if same_data else 'data_changed'
    # A receipt without a snapshot cannot prove that nothing else wrote to the scope, and an import never
    # leaves relationships across the scope boundary, so either one means the graph was changed.
    unchanged = marker.get('snapshot') == counts['snapshot'] and not counts['boundary_relationship_count']
    return 'unchanged' if unchanged else 'graph_changed'


def unchanged_since(current, consent):
    """The scope still has the snapshot the user confirmed and no relationship across its boundary."""
    return current['snapshot'] == consent['snapshot'] and not current['boundary_relationship_count']


class GraphImports:
    def __init__(self, store):
        self.store = store
        self.guard = threading.RLock()

    @staticmethod
    def connection(store, source, ontology_id):
        if source.get('mode') != 'connection' or source.get('provider') != 'neo4j' or not source.get('import_data'):
            raise Failure('graph_import_required', '请选择 Neo4j 连接并开启图数据导入', 422, 'data_import')
        connection = store.decrypt(source['connection']) if source.get('connection') else None
        if not isinstance(connection, dict):
            raise Failure('connection_invalid', '连接配置需要对象', 422, 'data_import')
        connection = {**connection, 'ontology_value': ontology_id}
        graph_scope(connection)
        return connection

    @staticmethod
    def fingerprint(source, connection):
        return digest([source.get('id'), source.get('revision'), source.get('input_ids', []),
                       source.get('tables_snapshot'), connection])

    def inspect(self, owner, source, ontology_id, preparation_id='', preparation=None):
        from neo4j import GraphDatabase, READ_ACCESS
        from neo4j.exceptions import DriverError, Neo4jError
        connection = self.connection(self.store, source, ontology_id)
        scope = graph_scope(connection)
        expected = expected_data = ''
        if preparation:
            tables = self.store.decrypt(source['tables_snapshot']) if source.get('tables_snapshot') else {}
            expected = import_identity(connection, tables, preparation.get('mapping'), preparation.get('preview', {}).get('relations', {}))
            expected_data = data_identity(tables)
        try:
            with GraphDatabase.driver(connection['uri'], auth=(connection.get('username', ''), connection.get('password', '')),
                                      connection_timeout=10) as driver:
                with driver.session(database=connection.get('database', 'neo4j'), default_access_mode=READ_ACCESS) as session:
                    with session.begin_transaction(timeout=30) as tx:
                        counts = snapshot(tx, scope)
                        marker = tx.run('MATCH (m:`' + RECEIPT_LABEL + '` {scope_fingerprint:$target}) '
                                        'RETURN m.run_id AS run_id, m.import_fingerprint AS fingerprint, '
                                        'm.data_fingerprint AS data_fingerprint, m.graph_snapshot AS snapshot',
                                        target=scope_target(connection)).single()
        except (DriverError, Neo4jError, OSError) as exc:
            raise Failure('graph_check_unavailable', '无法检查旧图数据，请检查图库连接与读取权限后重试', 503, 'data_import') from exc
        checked_at = time.time()
        # 范围里正好是某轮评测导入后的原样数据时记下那一轮：沿用评测配置的重跑据此自动替换，其余情况仍要用户确认
        untouched = marker and marker['snapshot'] and marker['snapshot'] == counts['snapshot']
        record = self.store.create(owner, 'graph_checks', {
            **counts, 'import_state': import_state(counts, marker, expected, expected_data),
            'fingerprint': self.fingerprint(source, connection), 'preparation_id': preparation_id,
            'checked_at': checked_at, 'expires_at': checked_at + CHECK_SECONDS,
            'imported_by': marker['run_id'] if untouched else '',
        })
        public = ('id', 'node_count', 'relationship_count', 'boundary_relationship_count', 'expires_at', 'import_state')
        return {key: record[key] for key in public} | {'scope': scope}

    def existing_nodes(self, source, ontology_id):
        """Nodes already in a read-only graph source's scope; None when the source does not name both scope fields.

        Solver validation of such a source reads only data that is already there, so an empty scope cannot pass.
        """
        from neo4j import GraphDatabase, READ_ACCESS
        from neo4j.exceptions import DriverError, Neo4jError
        connection = self.store.decrypt(source['connection']) if source.get('connection') else {}
        if not isinstance(connection, dict) or not connection.get('ontology_property'):
            return None
        connection = {**connection, 'ontology_value': ontology_id}
        try:
            scope = graph_scope(connection)
        except Failure:
            return None
        auth = (connection.get('username', ''), connection.get('password', ''))
        try:
            with GraphDatabase.driver(connection.get('uri', ''), auth=auth, connection_timeout=10) as driver:
                with driver.session(database=connection.get('database', 'neo4j'), default_access_mode=READ_ACCESS) as session:
                    with session.begin_transaction(timeout=30) as tx:
                        query = 'MATCH (n) WHERE ' + predicate('n', scope) + ' RETURN count(n) AS total'
                        return tx.run(query, **parameters(scope)).single()['total']
        except (DriverError, Neo4jError, OSError) as exc:
            raise Failure('graph_check_unavailable', '无法检查图中已有数据，请检查图库连接与读取权限后重试', 503, 'data_import') from exc

    def imported_by(self, owner, check_id):
        """True when the checked scope holds exactly what an evaluation of this owner imported, nothing else."""
        run_id = self.store.get(owner, check_id, 'graph_checks').get('imported_by')
        if not run_id:
            return False
        try:
            self.store.get(owner, run_id, 'runs')
        except Failure:
            return False
        return True

    def authorize(self, owner, prep, check_id, replace_existing):
        if not check_id:
            raise Failure('graph_check_required', '启动前请检查当前 Workspace 与 Ontology 范围的旧图数据', 409, 'data_import')
        record = self.store.get(owner, check_id, 'graph_checks')
        connection = self.connection(self.store, prep['source'], prep['package_ref']['ontology_id'])
        if record['preparation_id'] != prep['id'] or record['fingerprint'] != self.fingerprint(prep['source'], connection):
            raise Failure('graph_check_stale', '数据源或知识包范围已变化，请重新检查旧图数据', 409, 'data_import')
        if record['expires_at'] < time.time():
            raise Failure('graph_check_expired', '旧图数据检查已过期，请重新检查并确认', 409, 'data_import')
        # Unchanged data is reused as it is: nothing gets deleted, so nothing needs confirming.
        reuse = record.get('import_state') == 'unchanged' and not replace_existing
        if record['boundary_relationship_count']:
            raise Failure('graph_boundary_conflict', '此范围存在跨隔离范围的关系，请先处理这些关系或调整范围', 409, 'data_import')
        if record['node_count'] and not replace_existing and not reuse:
            raise Failure('graph_confirmation_required', '发现旧图数据，请确认删除后再导入新数据', 409, 'data_import')
        return {'snapshot': record['snapshot'], 'fingerprint': record['fingerprint'],
                'replace_existing': replace_existing, 'reuse': reuse, 'check_id': check_id}

    def ensure_current(self, run_id, source, ontology_id):
        from neo4j import GraphDatabase, READ_ACCESS
        from neo4j.exceptions import DriverError, Neo4jError
        connection = self.connection(self.store, source, ontology_id)
        try:
            with GraphDatabase.driver(connection['uri'], auth=(connection.get('username', ''), connection.get('password', '')),
                                      connection_timeout=10) as driver:
                with driver.session(database=connection.get('database', 'neo4j'), default_access_mode=READ_ACCESS) as session:
                    with session.begin_transaction(timeout=30) as tx:
                        marker = tx.run('MATCH (m:`' + RECEIPT_LABEL + '` {scope_fingerprint:$target}) '
                                        'RETURN m.run_id AS run_id, m.type_rule_version AS type_rule_version',
                                        target=scope_target(connection)).single()
                        if not marker or marker['run_id'] != run_id:
                            raise Failure('graph_scope_changed', '本轮图数据已被后续导入替换，请重新创建评测并确认范围', 409, 'data_import')
                        if marker.get('type_rule_version') != TYPE_RULE_VERSION:
                            raise Failure('graph_type_rules_changed', '图数据未按当前字段类型规则导入，请重新创建评测并检查旧图；本次未修改数据',
                                          409, 'data_import')
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
        import_fingerprint = import_identity(connection, tables, mapping, relations)
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
                            if consent.get('reuse'):
                                receipt_matches = bool(marker) and marker['fingerprint'] == import_fingerprint
                                if not receipt_matches or not unchanged_since(current, consent):
                                    raise Failure('graph_scope_changed', '图数据在确认后发生变化，请重新创建评测并确认；本次未修改数据', 409, 'data_import')
                                # The run takes over the receipt, so its later checks see the data as its own.
                                tx.run('MATCH (m:`' + RECEIPT_LABEL + '` {scope_fingerprint:$target}) SET m.run_id=$run_id',
                                       target=target, run_id=run_id).consume()
                                tx.commit()
                                return
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
                                   'SET m.run_id=$run_id, m.import_fingerprint=$fingerprint, m.data_fingerprint=$data_fingerprint, '
                                   'm.type_rule_version=$type_rule_version',
                                   target=target, run_id=run_id, fingerprint=import_fingerprint,
                                   data_fingerprint=data_identity(tables), type_rule_version=TYPE_RULE_VERSION).consume()
                            tx.commit()
                        self.record_snapshot(session, scope, target, run_id, import_fingerprint)
        except (DriverError, Neo4jError, OSError) as exc:
            raise Failure('graph_import_unavailable', '图数据替换未获确认成功，请检查连接或写入权限后恢复；恢复会先核对导入回执',
                          503, 'data_import') from exc

    @staticmethod
    def record_snapshot(session, scope, target, run_id, import_fingerprint):
        """Remember what the committed import left, so a later run can tell the graph is untouched.

        Runs after the commit because element ids are read across transactions. It is best effort:
        without a snapshot the next run asks before replacing instead of reusing the data.
        """
        from neo4j.exceptions import DriverError, Neo4jError
        try:
            with session.begin_transaction(timeout=30) as tx:
                counts = snapshot(tx, scope)
                tx.run('MATCH (m:`' + RECEIPT_LABEL + '` {scope_fingerprint:$target}) '
                       'WHERE m.run_id=$run_id AND m.import_fingerprint=$fingerprint SET m.graph_snapshot=$snapshot',
                       target=target, run_id=run_id, fingerprint=import_fingerprint, snapshot=counts['snapshot']).consume()
                tx.commit()
        except (DriverError, Neo4jError, OSError, Failure):
            logger.warning('Could not record the graph snapshot of a committed import', exc_info=True)

    @staticmethod
    def converted_rows(tables, mapping, relations, scope=None):
        if not mapping or not mapping.get('objects'):
            raise Failure('mapping_required', '图导入缺少对象映射', 422, 'data_import')
        check_names(mapping, relations)
        objects = {}
        for label, item in mapping['objects'].items():
            if label == RECEIPT_LABEL:
                raise Failure('graph_scope_conflict', '对象名与图导入回执保留名称冲突', 422, 'data_import')
            resource = item['sources'][0]['resource']
            if resource not in tables:
                raise Failure('mapping_table_missing', f'对象 {label}，表 {resource}：映射引用的数据表不存在',
                              422, 'data_import')
            fields = item['sources'][0]['fields']
            if scope and any(key in fields for key in (scope.get('workspace_property'), scope.get('ontology_property'))):
                raise Failure('graph_scope_conflict', '图隔离属性不能与业务字段重名', 422, 'data_import')
            graph_keys(label, item, tables[resource])
        kinds = resolved_field_kinds(mapping, relations)
        for label, item in mapping['objects'].items():
            resource = item['sources'][0]['resource']
            table = tables[resource]
            fields = item['sources'][0]['fields']
            rows, identities = [], {}
            keys = item['primary_key']
            for row_number, values in enumerate(table['rows'], 2):
                raw = {field: values[table['columns'].index(rule['source_column'])] for field, rule in fields.items()}
                row = {field: convert_value(value, kinds[label][field],
                                           f'对象 {label}，表 {resource}，第 {row_number} 行，字段 {field}')
                       for field, value in raw.items()}
                if any(row.get(key) is None or row.get(key) == '' for key in keys):
                    raise Failure('graph_key_missing', f'对象 {label}，表 {resource}，第 {row_number} 行：图数据缺少主键',
                                  422, 'data_import')
                identity = canonical([row[key] for key in keys])
                payload = {field: (type(value), value) for field, value in row.items()}
                previous = identities.get(identity)
                if previous and previous[0] != payload:
                    raise Failure('graph_key_collision', f'对象 {label}，表 {resource}，第 {row_number} 行：转换后的主键与第 {previous[1]} 行冲突',
                                  422, 'data_import')
                identities[identity] = (payload, row_number)
                rows.append(row)
            objects[label] = rows
        if not any(objects.values()):
            raise Failure('empty_data', '没有可导入的图数据，请检查上传文件', 422, 'data_import')
        for source, items in relations.items():
            for edge in items:
                target = edge['target']
                left = edge.get('sourceJoinProperty') or mapping['objects'][source]['primary_key'][0]
                right = edge.get('targetJoinProperty') or mapping['objects'][target]['primary_key'][0]
                left_kind, right_kind = kinds[source].get(left), kinds[target].get(right)
                compatible_kinds = left_kind == right_kind or {left_kind, right_kind} == {'integer', 'number'}
                if left_kind and right_kind and not compatible_kinds:
                    raise Failure('graph_join_type_conflict', f'关联字段 {source}.{left} 与 {target}.{right} 的声明类型不一致',
                                  422, 'data_import')
        return objects

    @staticmethod
    def plan(scope, tables, mapping, relations):
        converted = GraphImports.converted_rows(tables, mapping, relations, scope)
        objects, edges = [], []
        for label, rows in converted.items():
            keys = mapping['objects'][label]['primary_key']
            key_expr = ('`' + scope['workspace_property'] + '`:$workspace,`' + scope['ontology_property'] + '`:$ontology,' +
                        ','.join('`' + key + '`:row.`' + key + '`' for key in keys))
            objects.append(('UNWIND $rows AS row MERGE (n:`' + label + '` {' + key_expr + '}) SET n += row', rows))
        for source, items in relations.items():
            for edge in items:
                target = edge['target']
                left = edge.get('sourceJoinProperty') or mapping['objects'][source]['primary_key'][0]
                right = edge.get('targetJoinProperty') or mapping['objects'][target]['primary_key'][0]
                edges.append('MATCH (a:`' + source + '`), (b:`' + target + '`) WHERE ' + predicate('a', scope) +
                             ' AND ' + predicate('b', scope) + ' AND a.`' + left + '`=b.`' + right +
                             '` MERGE (a)-[:`' + relation_type(edge['relation']) + '`]->(b)')
        return objects, edges
