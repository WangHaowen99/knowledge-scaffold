"""Names written into the graph follow the consumer's rule; all names, values and connections are synthetic."""
import pytest

import evaluation.api as api_module
from evaluation.core import Failure
from evaluation.graph_import import GraphImports, check_names
from test_evaluation import environment

SCOPE = {'workspace_property': 'evaluation_scope', 'workspace_value': 'workspace_demo',
         'ontology_property': 'knowledge_package_id', 'ontology_value': 'knowledge_package_demo'}
LABEL = '合成对象（甲类）'
MAPPING = {'objects': {
    LABEL: {'primary_key': ['编号'], 'sources': [{'resource': 'items', 'fields': {
        '编号': {'source_column': 'id'}, '数值（元）': {'source_column': 'value'}}}]},
    '目标对象': {'primary_key': ['编号'], 'sources': [{'resource': 'items', 'fields': {'编号': {'source_column': 'id'}}}]},
}}
TABLES = {'items': {'columns': ['id', 'value'], 'rows': [['item_0001', '2']]}}
RELATIONS = {LABEL: [{'target': '目标对象', 'relation': '关联（合成）'}]}


def test_business_names_with_full_width_punctuation_are_imported_quoted():
    objects, edges = GraphImports.plan(SCOPE, TABLES, MAPPING, RELATIONS)
    assert objects[0][0].startswith('UNWIND $rows AS row MERGE (n:`合成对象（甲类）` {')
    assert objects[0][1] == [{'编号': 'item_0001', '数值（元）': '2'}]
    assert '(a:`合成对象（甲类）`)' in edges[0] and '[:`关联（合成）`]' in edges[0]


@pytest.mark.parametrize('name', ['含`反引号', ' 首尾空格', '控制\x01字符', 'x' * 129, '',
                                  # 反斜杠能构成 Cypher Unicode 转义：\u0060 即反引号，可借此跳出名称的引用
                                  'A\\u0060) DETACH DELETE n //', '含\\反斜杠'])
def test_names_that_cannot_be_quoted_safely_are_refused(name):
    with pytest.raises(Failure) as caught:
        check_names({'objects': {name: {'primary_key': [], 'sources': []}}}, {})
    assert caught.value.code == 'invalid_graph_name' and caught.value.status == 422
    assert '\x01' not in caught.value.message


def test_relation_names_follow_the_relation_type_rule():
    # 关系名中的反引号转义后照常导入；反斜杠与控制字符可能改变语句，准备时就拒绝
    check_names(MAPPING, {LABEL: [{'target': '目标对象', 'relation': '关联`合成'}]})
    _, edges = GraphImports.plan(SCOPE, TABLES, MAPPING, {LABEL: [{'target': '目标对象', 'relation': '关联`合成'}]})
    assert '[:`关联``合成`]' in edges[0]
    for name in ('关联\\合成', '关联\x01合成'):
        with pytest.raises(Failure) as caught:
            check_names(MAPPING, {LABEL: [{'target': '目标对象', 'relation': name}]})
        assert caught.value.code == 'invalid_relation_type'


def test_preparing_a_graph_import_reports_a_bad_name_before_any_run(environment, monkeypatch):
    client, app, deps, cfg = environment
    uploaded = client.post('/evaluation/api/inputs', files={'file': ('items.csv', b'id,value\nitem_0001,2\n', 'text/csv')}).json()
    connection = {'uri': 'bolt://service.example.net:7687', 'database': 'neo4j', 'username': 'operator_a',
                  'password': 'synthetic-invalid-password', 'workspace_property': 'evaluation_scope',
                  'workspace_value': 'workspace_demo', 'ontology_property': 'knowledge_package_id'}
    source = client.post('/evaluation/api/data-sources', json={
        'name': 'Synthetic graph', 'mode': 'connection', 'provider': 'neo4j', 'connection': connection,
        'import_data': True, 'input_ids': [uploaded['id']]}).json()
    bad = {'objects': {'含`反引号': MAPPING['objects'][LABEL]}}
    monkeypatch.setattr(api_module, 'generate_mapping', lambda raw, tables, graph: (bad, {'objects': {}, 'relations': {}}))
    response = client.post('/evaluation/api/package-preparations', json={
        'request_id': 'prepare_bad_name', 'package_ref': {'ontology_id': 'knowledge_package_demo', 'release_id': 'release_demo'},
        'data_source_id': source['id']})
    assert response.status_code == 422 and response.json()['code'] == 'invalid_graph_name'
    assert '对象名' in response.json()['message']
