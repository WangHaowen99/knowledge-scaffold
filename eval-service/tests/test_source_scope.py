"""The isolation scope of external connections, set in the new-evaluation wizard; all values are synthetic."""
from test_evaluation import environment

API = '/evaluation/api'
GRAPH = {'uri': 'bolt://service.example.net:7687', 'database': 'neo4j', 'username': 'operator_a',
         'password': 'synthetic-invalid-password', 'workspace_property': 'd2o_workspace_id',
         'workspace_value': '', 'ontology_property': 'd2o_ontology_id'}
SQL = {'url': 'https://service.example.net', 'scene': 'workspace_demo', 'evaluation_workspace_id': 'workspace_demo',
       'token': 'synthetic-invalid-token'}
PACKAGE = {'ontology_id': 'knowledge_package_demo', 'release_id': 'release_demo'}


def graph_source(client, **changes):
    response = client.post(f'{API}/data-sources', json={'name': 'graph_scope_demo', 'mode': 'connection', 'provider': 'neo4j',
                                                       'connection': {**GRAPH, **changes}, 'import_data': False})
    assert response.status_code == 200, response.text
    return response.json()


def prepare(client, source, request_id='prepare_scope_demo'):
    return client.post(f'{API}/package-preparations', json={'request_id': request_id, 'package_ref': PACKAGE,
                                                           'data_source_id': source['id']})


def scope(client, source, **values):
    return client.put(f"{API}/data-sources/{source['id']}/scope", json={'revision': source['revision'], **values})


def test_a_connection_saves_without_its_scope_but_cannot_be_evaluated_until_it_has_one(environment):
    client, app, deps, cfg = environment
    source = graph_source(client)
    response = prepare(client, source)
    assert response.status_code == 422 and response.json()['code'] == 'scope_required'
    assert 'Workspace 对应值' in response.json()['message']
    updated = scope(client, source, workspace_value='workspace_demo')
    assert updated.status_code == 200, updated.text
    view = updated.json()
    # 只改隔离范围：密码仍保存着，修订号加一，读取结果不含密码
    assert view['connection_view']['workspace_value'] == 'workspace_demo' and view['secret_configured'] is True
    assert view['revision'] == source['revision'] + 1 and 'synthetic-invalid-password' not in updated.text
    assert prepare(client, source, 'prepare_scope_demo_2').status_code == 200


def test_the_scope_is_validated_and_bound_to_the_saved_revision(environment):
    client, app, deps, cfg = environment
    source = graph_source(client, workspace_value='workspace_demo')
    assert scope(client, source, workspace_value='Invalid Value').status_code == 422
    assert scope(client, source, workspace_value='').json()['code'] == 'scope_required'
    assert scope(client, source, workspace_property='d2o_ontology_id').status_code == 422
    # Neo4j 数据源不接受 NL2SQL 的评测工作空间 ID
    assert scope(client, source, evaluation_workspace_id='workspace_demo').status_code == 422
    assert scope(client, source, workspace_value='workspace_next').status_code == 200
    # 用旧修订号再改：已被别人改过，拒绝
    assert scope(client, source, workspace_value='workspace_other').status_code == 409


def test_graph_import_keeps_requiring_an_ontology_scope(environment):
    client, app, deps, cfg = environment
    uploaded = client.post(f'{API}/inputs', files={'file': ('items.csv', b'id,value\nitem_0001,2\n', 'text/csv')}).json()
    response = client.post(f'{API}/data-sources', json={'name': 'graph_import_demo', 'mode': 'connection', 'provider': 'neo4j',
                                                       'connection': GRAPH, 'import_data': True,
                                                       'input_ids': [uploaded['id']]})
    assert response.status_code == 200, response.text
    result = scope(client, response.json(), workspace_value='workspace_demo', ontology_property='')
    assert result.status_code == 422 and result.json()['code'] == 'graph_scope_required'


def test_only_external_connections_have_a_scope(environment):
    client, app, deps, cfg = environment
    source = client.post(f'{API}/data-sources', json={'name': 'none_demo', 'mode': 'none'}).json()
    response = client.put(f"{API}/data-sources/{source['id']}/scope", json={'revision': source['revision'], 'workspace_value': 'workspace_demo'})
    assert response.status_code == 422 and response.json()['code'] == 'scope_unsupported'


def test_an_nl2sql_connection_keeps_its_scope_and_secret_when_edited_later(environment):
    client, app, deps, cfg = environment
    connection = {**SQL, 'evaluation_workspace_id': ''}
    payload = {'name': 'sql_scope_demo', 'mode': 'connection', 'provider': 'nl2sql_http', 'connection': connection}
    saved = client.post(f'{API}/data-sources', json=payload)
    assert saved.status_code == 200, saved.text
    source = saved.json()
    updated = scope(client, source, evaluation_workspace_id='workspace_sql_demo')
    assert updated.status_code == 200, updated.text
    # 之后在数据源编辑页保存其他修改：评测工作空间 ID 与已保存的凭据都沿用
    edited = {**payload, 'name': 'sql_scope_renamed', 'revision': updated.json()['revision'],
              'connection': {**{k: v for k, v in connection.items() if k != 'token'}, 'evaluation_workspace_id': 'workspace_sql_demo'}}
    response = client.put(f"{API}/data-sources/{source['id']}", json=edited)
    assert response.status_code == 200, response.text
    view = client.get(f"{API}/data-sources/{source['id']}").json()
    assert view['connection_view']['evaluation_workspace_id'] == 'workspace_sql_demo' and view['secret_configured'] is True
