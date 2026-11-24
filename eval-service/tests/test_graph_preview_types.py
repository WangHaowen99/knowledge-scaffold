"""Preview and import use identical typed rows; every document and value is synthetic."""
from copy import deepcopy
from datetime import datetime, time
import io
import zipfile

from openpyxl import Workbook
import pytest

from evaluation.core import Failure, Identity, digest
from evaluation.graph_import import GraphImports
from evaluation.packages import generate_mapping
from test_evaluation import environment


API = '/evaluation/api'
SCOPE = {'workspace_property': 'evaluation_scope', 'workspace_value': 'workspace_demo',
         'ontology_property': 'knowledge_package_id', 'ontology_value': 'knowledge_package_demo'}


def import_plan(deps, tables):
    mapping, preview = generate_mapping(deps.raw, {key: table['columns'] for key, table in tables.items()}, True)
    return GraphImports.plan(SCOPE, tables, mapping, preview.get('relations', {}))


def preview_result(client, query, deps, instances, relationships=None):
    response = client.post(f'{API}/data-source-previews', json=query)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['instances'] == instances
    if relationships is not None:
        assert result['relationship_count'] == relationships
    assert not deps.requests
    return result


def preview_input(client, deps, rows, value_type='string', target_type='string', targets=None):
    document = ('# item\n\n# 数据来源\nitems\n\n# 字段\n\n'
                '| 英文名称 | 中文名称 | 类型 | 主键 |\n| --- | --- | --- | --- |\n'
                '| id | 合成编号 | string | 是 |\n'
                f'| value | 合成值 | {value_type} | 否 |\n')
    if targets is not None:
        document += '\n# 关联对象\n- 当前对象 → related（value → id） → [[target]]\n'
    files = {}
    with zipfile.ZipFile(io.BytesIO(deps.raw)) as archive:
        files.update({name: archive.read(name) for name in archive.namelist()})
    files['knowledge_package_demo/objects/item.md'] = document.encode()
    if targets is not None:
        files['knowledge_package_demo/objects/target.md'] = (
            '# target\n\n# 数据来源\ntargets\n\n# 字段\n\n'
            '| 英文名称 | 中文名称 | 类型 | 主键 |\n| --- | --- | --- | --- |\n'
            f'| id | 合成编号 | {target_type} | 是 |\n').encode()
    package = io.BytesIO()
    with zipfile.ZipFile(package, 'w') as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    deps.raw = package.getvalue()
    deps.receipt = {**deps.receipt, 'package_digest': digest(deps.raw)}
    deps.releases['release_demo'] = (deps.raw, deps.receipt)
    book = Workbook()
    book.active.title = 'items'
    book.active.append(['id', 'value'])
    for row in rows:
        book.active.append(row)
    tables = {'items': {'columns': ['id', 'value'], 'rows': rows}}
    if targets is not None:
        sheet = book.create_sheet('targets')
        sheet.append(['id'])
        for value in targets:
            sheet.append([value])
        tables['targets'] = {'columns': ['id'], 'rows': [[value] for value in targets]}
    content = io.BytesIO()
    book.save(content)
    book.close()
    uploaded = client.post(f'{API}/inputs', files={'file': ('synthetic.xlsx', content.getvalue())})
    assert uploaded.status_code == 200, uploaded.text
    connection = {'uri': 'bolt://graph.example.net:7687', 'username': 'operator_a',
                  'password': 'synthetic-invalid-password', **SCOPE}
    connection.pop('ontology_value')
    saved = client.post(f'{API}/data-sources', json={
        'name': 'graph_source_demo', 'mode': 'connection', 'provider': 'neo4j',
        'connection': connection, 'input_ids': [uploaded.json()['id']], 'import_data': True})
    assert saved.status_code == 200, saved.text
    query = {'data_source_id': saved.json()['id'],
             'package_ref': {'ontology_id': 'knowledge_package_demo', 'release_id': 'release_demo'}}
    return query, tables


def test_preview_counts_relations_after_conversion_without_graph_access(environment, monkeypatch):
    import neo4j
    client, app, deps, cfg = environment
    query, tables = preview_input(client, deps, [[1, 1001], [1, 1001]], targets=['1001'])
    original = deepcopy(tables)
    objects, edges = import_plan(deps, tables)
    assert objects[0][1][0] == {'id': '1', 'value': '1001'}
    assert len(edges) == 1
    monkeypatch.setattr(neo4j.GraphDatabase, 'driver', lambda *args, **kwargs: pytest.fail('Preview opened Neo4j'))
    result = preview_result(client, query, deps, 2, 1)
    assert {item['name']: item['instances'] for item in result['objects']} == {'item': 1, 'target': 1}
    assert result['relations'][0]['instances'] == 1
    assert tables == original


@pytest.mark.parametrize('rows,value_type,target_type,targets,code', [
    ([[1, 'not_an_integer']], '整数型', 'string', None, 'graph_field_value_invalid'),
    ([[1, 'first'], [1, 'second']], 'string', 'string', None, 'graph_key_collision'),
    ([[1, 1001]], 'string', 'integer', [1001], 'graph_join_type_conflict'),
    ([[1, 'same']], 'unsupported', 'string', None, 'graph_field_type_unsupported'),
    ([[1, 'same']], '金额', 'string', None, 'graph_field_type_unsupported'),
    ([[1, 'same']], 'uuid', 'string', None, 'graph_field_type_unsupported'),
    ([[1, 'same']], 'varchar2(20)', 'string', None, 'graph_field_type_unsupported'),
    ([[None, 'same']], 'string', 'string', None, 'graph_key_missing'),
])
def test_preview_reports_the_same_failures_as_import(environment, rows, value_type, target_type, targets, code):
    client, app, deps, cfg = environment
    query, tables = preview_input(client, deps, rows, value_type, target_type, targets)
    with pytest.raises(Failure) as caught:
        import_plan(deps, tables)
    assert caught.value.code == code
    response = client.post(f'{API}/data-source-previews', json=query)
    assert response.status_code == 422, response.text
    assert response.json()['code'] == code
    assert not deps.requests


def test_preview_accepts_annotated_modeling_types(environment):
    client, app, deps, cfg = environment
    query, tables = preview_input(client, deps, [[1, '12.5']], '小数（合成单位）')
    preview_result(client, query, deps, 1)


@pytest.mark.parametrize('declared', ['时间', 'time', '时间（合成说明）', 'TIME'])
@pytest.mark.parametrize('value,normalized', [
    ('2026-01-02 12:30:00', '2026-01-02T12:30:00'),
    (datetime(2026, 1, 2, 12, 30), '2026-01-02T12:30:00'),
    ('12:30', '12:30:00'), (time(12, 30), '12:30:00'),
    ('2026-01-02T12:30:01.25+08:00', '2026-01-02T12:30:01.250000+08:00'),
])
def test_preview_temporal_excel_values_match_import(environment, declared, value, normalized):
    client, app, deps, cfg = environment
    query, tables = preview_input(client, deps, [[1, value]], declared, declared, [normalized])
    objects, edges = import_plan(deps, tables)
    assert objects[0][1] == [{'id': '1', 'value': normalized}]
    assert objects[1][1] == [{'id': normalized}]
    assert len(edges) == 1
    preview_result(client, query, deps, 2, 1)


@pytest.mark.parametrize('reference', ['对象引用', 'ref（合成说明）'])
def test_preview_reference_and_normalized_duplicates_match_import(environment, reference):
    client, app, deps, cfg = environment
    query, tables = preview_input(client, deps, [[1, '1001'], ['1', 1001]], reference, 'integer', [1001])
    objects, edges = import_plan(deps, tables)
    assert objects[0][1] == [{'id': '1', 'value': 1001}] * 2
    assert objects[1][1] == [{'id': 1001}]
    assert len(edges) == 1
    preview_result(client, query, deps, 2, 1)


def test_empty_document_type_defaults_to_string_in_preview_and_import(environment):
    client, app, deps, cfg = environment
    query, tables = preview_input(client, deps, [[1, 1001]], '', 'integer')
    mapping, preview = generate_mapping(deps.raw, {key: item['columns'] for key, item in tables.items()}, True)
    assert mapping['objects']['item']['fields']['value']['type'] == '字符串'
    objects, edges = GraphImports.plan(SCOPE, tables, mapping, preview.get('relations', {}))
    assert objects[0][1] == [{'id': '1', 'value': '1001'}]
    preview_result(client, query, deps, 1)


def test_preview_passes_only_validated_scope_to_conversion(environment, monkeypatch):
    client, app, deps, cfg = environment
    query, tables = preview_input(client, deps, [[1, 'synthetic_value']])
    original = GraphImports.converted_rows
    seen = []

    def converted_rows(tables, mapping, relations, scope=None):
        seen.append(scope)
        assert scope == SCOPE
        return original(tables, mapping, relations, scope=scope)

    monkeypatch.setattr(GraphImports, 'converted_rows', staticmethod(converted_rows))
    response = client.post(f'{API}/data-source-previews', json=query)
    assert response.status_code == 200, response.text
    assert seen == [SCOPE]


@pytest.mark.parametrize('connection,code', [
    (None, 'connection_invalid'), ('synthetic-invalid-connection', 'connection_invalid'),
    ([], 'connection_invalid'), ({}, 'invalid_identifier'),
    ({'workspace_property': 'evaluation_scope', 'ontology_property': 'knowledge_package_id'}, 'graph_scope_required'),
    ({**SCOPE, 'ontology_property': 'evaluation_scope'}, 'graph_scope_required'),
    ({**SCOPE, 'workspace_property': 'value'}, 'graph_scope_conflict'),
    ({**SCOPE, 'ontology_property': 'value'}, 'graph_scope_conflict'),
])
def test_preview_rejects_invalid_stored_connections_without_graph_access(environment, monkeypatch, connection, code):
    import neo4j
    client, app, deps, cfg = environment
    query, tables = preview_input(client, deps, [[1, 'synthetic_value']])
    owner = Identity(cfg, deps).verify('synthetic-user-a')
    store = app.state.store
    source = store.get(owner, query['data_source_id'], 'data-sources')
    source['connection'] = store.encrypt(connection)
    store.save(owner, source['id'], source, source['revision'])
    monkeypatch.setattr(neo4j.GraphDatabase, 'driver', lambda *args, **kwargs: pytest.fail('Preview opened Neo4j'))
    with pytest.raises(Failure) as caught:
        connection = GraphImports.connection(store, source, 'knowledge_package_demo')
        GraphImports.plan(connection, tables, generate_mapping(deps.raw, {'items': ['id', 'value']}, True)[0], {})
    response = client.post(f'{API}/data-source-previews', json=query)
    assert response.status_code == 422, response.text
    assert response.json()['code'] == caught.value.code == code


def test_preview_missing_declared_table_is_a_mapping_failure(environment):
    client, app, deps, cfg = environment
    query, tables = preview_input(client, deps, [[1, 'synthetic_value']])
    uploaded = client.post(f'{API}/inputs', files={'file': ('other_items.csv', b'id,value\n1,synthetic_value\n')})
    assert uploaded.status_code == 200, uploaded.text
    owner = Identity(cfg, deps).verify('synthetic-user-a')
    store = app.state.store
    source = store.get(owner, query['data_source_id'], 'data-sources')
    source['input_ids'] = [uploaded.json()['id']]
    store.save(owner, source['id'], source, source['revision'])
    response = client.post(f'{API}/data-source-previews', json=query)
    assert response.status_code == 422, response.text
    assert response.json()['code'] == 'mapping_conflict'


def test_preview_does_not_convert_query_only_graph_values(environment, monkeypatch):
    client, app, deps, cfg = environment
    query, tables = preview_input(client, deps, [[1, 'synthetic_value']], '整数')
    owner = Identity(cfg, deps).verify('synthetic-user-a')
    store = app.state.store
    source = store.get(owner, query['data_source_id'], 'data-sources')
    source['import_data'] = False
    store.save(owner, source['id'], source, source['revision'])
    monkeypatch.setattr(GraphImports, 'converted_rows', staticmethod(
        lambda *args, **kwargs: pytest.fail('Read-only preview converted values')))
    preview_result(client, query, deps, 1)
