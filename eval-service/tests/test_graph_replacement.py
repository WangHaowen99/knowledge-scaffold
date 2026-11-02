"""All graph nodes, properties, connections, identities and credentials are synthetic."""
from copy import deepcopy
import time

import pytest
from neo4j.exceptions import ServiceUnavailable

from evaluation.core import Failure, Identity
from evaluation.data_sources import DataSources
from evaluation.graph_import import GraphImports, graph_scope
from test_evaluation import environment, wait


class Graph:
    """Transactional in-memory Neo4j boundary; never connects to a database."""
    def __init__(self):
        self.nodes = {
            'old_a': {'evaluation_scope': 'workspace_demo', 'knowledge_package_id': 'knowledge_package_demo', 'id': 'old_0001'},
            'old_b': {'evaluation_scope': 'workspace_demo', 'knowledge_package_id': 'knowledge_package_demo', 'id': 'old_0002'},
            'other_workspace': {'evaluation_scope': 'workspace_other', 'knowledge_package_id': 'knowledge_package_demo', 'id': 'other_0001'},
            'other_ontology': {'evaluation_scope': 'workspace_demo', 'knowledge_package_id': 'knowledge_package_other', 'id': 'other_0002'},
        }
        self.edges = {'old_edge': ('old_a', 'old_b'), 'other_edge': ('other_workspace', 'other_ontology')}
        self.marker = None
        self.calls = []
        self.commits = self.rollbacks = 0
        self.fail_import = self.lose_commit = False
        self.before_write = None

    def driver(self, *args, **kwargs):
        graph = self

        class Driver:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def session(self, **kwargs): return Session()

        class Session:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def begin_transaction(self, **kwargs):
                if kwargs['timeout'] == 120 and graph.before_write:
                    graph.before_write()
                    graph.before_write = None
                return Transaction()

        class Result:
            def __init__(self, rows=()): self.rows = rows
            def single(self): return self.rows[0] if self.rows else None
            def __iter__(self): return iter(self.rows)
            def consume(self): pass

        class Transaction:
            def __init__(self):
                self.nodes = deepcopy(graph.nodes)
                self.edges = deepcopy(graph.edges)
                self.marker = deepcopy(graph.marker)
                self.committed = False
            def __enter__(self): return self
            def __exit__(self, *args):
                if not self.committed: graph.rollbacks += 1
            def commit(self):
                graph.nodes, graph.edges, graph.marker = self.nodes, self.edges, self.marker
                self.committed = True
                graph.commits += 1
                if graph.lose_commit:
                    graph.lose_commit = False
                    raise ServiceUnavailable('Synthetic lost acknowledgement')
            def run(self, query, **params):
                graph.calls.append((query, params))
                if 'MATCH (m:' in query:
                    return Result([self.marker] if self.marker else [])
                if 'MERGE (m:' in query:
                    self.marker = {'run_id': params['run_id'], 'fingerprint': params['fingerprint']}
                    return Result()
                scoped = {key for key, node in self.nodes.items()
                          if node.get('evaluation_scope') == params['workspace'] and node.get('knowledge_package_id') == params['ontology']}
                internal = {key for key, (a, b) in self.edges.items() if a in scoped and b in scoped}
                boundary = {key for key, (a, b) in self.edges.items() if (a in scoped) != (b in scoped)}
                if 'count(n)' in query: return Result([{'total': len(scoped)}])
                if 'count(r)' in query: return Result([{'total': len(boundary if 'coalesce' in query else internal)}])
                if 'RETURN elementId(n)' in query: return Result([{'id': key} for key in sorted(scoped)])
                if 'RETURN elementId(r)' in query: return Result([{'id': key} for key in sorted(internal)])
                if 'DELETE r' in query:
                    assert 'a.`evaluation_scope`=$workspace AND a.`knowledge_package_id`=$ontology' in query
                    assert 'b.`evaluation_scope`=$workspace AND b.`knowledge_package_id`=$ontology' in query
                    assert set(params['ids']) == internal
                    for key in internal & set(params['ids']): del self.edges[key]
                elif 'DELETE n' in query:
                    assert 'n.`evaluation_scope`=$workspace AND n.`knowledge_package_id`=$ontology' in query
                    assert not boundary
                    assert set(params['ids']) == scoped
                    for key in scoped & set(params['ids']): del self.nodes[key]
                elif query.startswith('UNWIND $rows'):
                    if graph.fail_import: raise ServiceUnavailable('Synthetic write failure')
                    for row in params['rows']:
                        self.nodes['new_' + row['id']] = {**row, 'evaluation_scope': params['workspace'], 'knowledge_package_id': params['ontology']}
                elif 'MERGE (a)-[:`related`]->(b)' in query:
                    assert 'a.`knowledge_package_id`=$ontology' in query and 'b.`knowledge_package_id`=$ontology' in query
                    for key in scoped: self.edges['related_' + key] = (key, key)
                else: raise AssertionError(query)
                return Result()

        return Driver()


@pytest.fixture
def graph(monkeypatch):
    import neo4j
    value = Graph()
    monkeypatch.setattr(neo4j.GraphDatabase, 'driver', value.driver)
    return value


def preparation(client, name='prepare_graph'):
    connection = {'uri': 'bolt://service.example.net:7687', 'database': 'neo4j', 'username': 'operator_a',
                  'password': 'synthetic-invalid-password', 'workspace_property': 'evaluation_scope',
                  'workspace_value': 'workspace_demo', 'ontology_property': 'knowledge_package_id'}
    uploaded = client.post('/evaluation/api/inputs', files={'file': ('items.csv', b'id,value\nitem_0001,2\n', 'text/csv')}).json()
    source = client.post('/evaluation/api/data-sources', json={
        'name': 'Synthetic graph', 'mode': 'connection', 'provider': 'neo4j', 'connection': connection,
        'import_data': True, 'input_ids': [uploaded['id']],
    }).json()
    ref = {'ontology_id': 'knowledge_package_demo', 'release_id': 'release_demo'}
    response = client.post('/evaluation/api/package-preparations', json={
        'request_id': name, 'package_ref': ref, 'data_source_id': source['id'],
    })
    assert response.status_code == 200, response.text
    prep = response.json()
    cases = client.post('/evaluation/api/eval-sets', json={'name': 'Synthetic cases', 'cases': [{'question': 'Synthetic question', 'reference': '2'}]}).json()
    return {'data_source_id': source['id'], 'preparation_id': prep['id'], 'package_ref': ref}, {
        'request_id': 'run_graph', 'preparation_id': prep['id'], 'eval_set_id': cases['id'],
    }


def consumer(deps):
    original = deps.json
    calls = []
    def request(method, base, path, **kwargs):
        if path.endswith('/data-source'):
            calls.append(kwargs['json'])
            return {'success': True}
        if path.endswith('/data-source/test'): return {'success': True}
        return original(method, base, path, **kwargs)
    deps.json = request
    return calls


def check(client, query):
    response = client.post('/evaluation/api/graph-import-checks', json=query)
    assert response.status_code == 200, response.text
    return response.json()


def test_check_is_readonly_and_replacement_requires_explicit_confirmation(environment, graph):
    client, app, deps, cfg = environment
    query, intent = preparation(client)
    original = deepcopy(graph.nodes)
    inspected = check(client, query)
    assert inspected['node_count'] == 2 and inspected['relationship_count'] == 1
    assert inspected['boundary_relationship_count'] == 0
    assert 'password' not in str(inspected) and 'old_0001' not in str(inspected)
    assert graph.nodes == original and graph.commits == 0
    assert not any('DELETE' in sql or 'MERGE' in sql for sql, _ in graph.calls)
    missing = client.post('/evaluation/api/runs', json=intent)
    assert missing.status_code == 409 and missing.json()['code'] == 'graph_check_required'
    unconfirmed = client.post('/evaluation/api/runs', json={**intent, 'graph_check_id': inspected['id']})
    assert unconfirmed.status_code == 409 and unconfirmed.json()['code'] == 'graph_confirmation_required'
    assert client.get('/evaluation/api/runs').json()['total'] == 0
    assert graph.nodes == original
    assert client.post('/evaluation/api/runs', json={**intent, 'graph_check_id': inspected['id'], 'replace_existing_graph': 'yes'}).status_code == 422


def test_confirmed_import_only_replaces_the_exact_pair_and_is_idempotent(environment, graph):
    client, app, deps, cfg = environment
    consumed = consumer(deps)
    query, intent = preparation(client)
    inspected = check(client, query)
    intent.update(graph_check_id=inspected['id'], replace_existing_graph=True)
    started = client.post('/evaluation/api/runs', json=intent)
    assert started.status_code == 202, started.text
    result = wait(client, started.json()['id'])
    assert result['status'] == 'completed', result
    assert set(graph.nodes) == {'other_workspace', 'other_ontology', 'new_item_0001'}
    assert graph.nodes['new_item_0001']['evaluation_scope'] == 'workspace_demo'
    assert graph.nodes['new_item_0001']['knowledge_package_id'] == 'knowledge_package_demo'
    assert graph.edges == {'other_edge': ('other_workspace', 'other_ontology')}
    assert graph.commits == 1
    assert consumed[0]['config']['workspace_value'] == 'workspace_demo'
    assert consumed[0]['config']['ontology_value'] == 'knowledge_package_demo'
    retry = client.post('/evaluation/api/runs', json=intent)
    assert retry.status_code == 202 and retry.json()['id'] == started.json()['id']
    assert graph.commits == 1


def test_failed_import_rolls_back_old_data_then_can_resume(environment, graph):
    client, app, deps, cfg = environment
    consumer(deps)
    query, intent = preparation(client)
    inspected = check(client, query)
    graph.fail_import = True
    old_nodes, old_edges = deepcopy(graph.nodes), deepcopy(graph.edges)
    started = client.post('/evaluation/api/runs', json={**intent, 'graph_check_id': inspected['id'], 'replace_existing_graph': True}).json()
    result = wait(client, started['id'])
    assert result['status'] == 'interrupted' and result['error']['code'] == 'graph_import_unavailable'
    assert graph.nodes == old_nodes and graph.edges == old_edges and graph.marker is None
    graph.fail_import = False
    assert client.post('/evaluation/api/runs/' + started['id'] + '/resume').status_code == 200
    assert wait(client, started['id'], statuses=('completed',))['status'] == 'completed'
    assert graph.commits == 1


def test_lost_commit_acknowledgement_reuses_receipt_without_deleting_new_data(environment, graph):
    client, app, deps, cfg = environment
    consumer(deps)
    query, intent = preparation(client)
    inspected = check(client, query)
    graph.lose_commit = True
    started = client.post('/evaluation/api/runs', json={**intent, 'graph_check_id': inspected['id'], 'replace_existing_graph': True}).json()
    result = wait(client, started['id'])
    assert result['status'] == 'interrupted'
    assert graph.marker['run_id'] == started['id'] and graph.commits == 1
    writes = len([sql for sql, _ in graph.calls if 'DELETE' in sql or sql.startswith('UNWIND')])
    client.post('/evaluation/api/runs/' + started['id'] + '/resume')
    assert wait(client, started['id'], statuses=('completed',))['status'] == 'completed'
    assert graph.commits == 1
    assert len([sql for sql, _ in graph.calls if 'DELETE' in sql or sql.startswith('UNWIND')]) == writes


@pytest.mark.parametrize('change', ['new_node', 'cross_scope_edge'])
def test_changed_graph_after_confirmation_stops_before_delete(environment, graph, change):
    client, app, deps, cfg = environment
    consumer(deps)
    query, intent = preparation(client)
    inspected = check(client, query)
    def mutate():
        if change == 'new_node': graph.nodes['late_node'] = dict(graph.nodes['old_a'])
        else: graph.edges['cross_scope_edge'] = ('old_a', 'other_workspace')
    graph.before_write = mutate
    started = client.post('/evaluation/api/runs', json={**intent, 'graph_check_id': inspected['id'], 'replace_existing_graph': True}).json()
    result = wait(client, started['id'])
    assert result['status'] == 'interrupted' and result['error']['code'] == 'graph_scope_changed'
    assert 'old_a' in graph.nodes and 'old_edge' in graph.edges
    assert graph.commits == 0 and not any('DELETE' in sql for sql, _ in graph.calls)
    response = client.post('/evaluation/api/runs/' + started['id'] + '/resume')
    assert response.status_code == 409 and response.json()['code'] == 'graph_confirmation_required'


def test_cross_scope_relationships_prevent_confirmation(environment, graph):
    client, app, deps, cfg = environment
    query, intent = preparation(client)
    graph.edges['external_edge'] = ('old_a', 'other_workspace')
    inspected = check(client, query)
    assert inspected['boundary_relationship_count'] == 1
    response = client.post('/evaluation/api/runs', json={**intent, 'graph_check_id': inspected['id'], 'replace_existing_graph': True})
    assert response.status_code == 409 and response.json()['code'] == 'graph_boundary_conflict'
    assert graph.commits == 0


def test_empty_scope_import_still_requires_a_fresh_check(environment, graph):
    client, app, deps, cfg = environment
    consumer(deps)
    graph.nodes.pop('old_a'); graph.nodes.pop('old_b'); graph.edges.pop('old_edge')
    query, intent = preparation(client)
    inspected = check(client, query)
    assert inspected['node_count'] == 0
    response = client.post('/evaluation/api/runs', json={**intent, 'graph_check_id': inspected['id']})
    assert response.status_code == 202, response.text
    assert wait(client, response.json()['id'])['status'] == 'completed'
    assert not any('DELETE' in sql for sql, _ in graph.calls)


def test_checks_are_owner_preparation_and_revision_bound_and_expire(environment, graph):
    client, app, deps, cfg = environment
    query, intent = preparation(client)
    inspected = check(client, query)
    client.headers['Authorization'] = 'Bearer synthetic-user-b'
    assert client.post('/evaluation/api/graph-import-checks', json=query).status_code == 404
    client.headers['Authorization'] = 'Bearer synthetic-user-a'
    preview_only = check(client, {key: value for key, value in query.items() if key != 'preparation_id'})
    response = client.post('/evaluation/api/runs', json={**intent, 'graph_check_id': preview_only['id'], 'replace_existing_graph': True})
    assert response.status_code == 409 and response.json()['code'] == 'graph_check_stale'
    owner = Identity(cfg, deps).verify('synthetic-user-a')
    prep = app.state.store.get(owner, query['preparation_id'], 'preparations')
    other = app.state.store.create(owner, 'preparations', {key: value for key, value in prep.items() if key not in ('id', 'revision')})
    response = client.post('/evaluation/api/runs', json={**intent, 'preparation_id': other['id'], 'graph_check_id': inspected['id'], 'replace_existing_graph': True})
    assert response.status_code == 409 and response.json()['code'] == 'graph_check_stale'
    changed = {**prep, 'source': {**prep['source'], 'revision': prep['source']['revision'] + 1}}
    app.state.store.save(owner, prep['id'], changed)
    response = client.post('/evaluation/api/runs', json={**intent, 'graph_check_id': inspected['id'], 'replace_existing_graph': True})
    assert response.status_code == 409 and response.json()['code'] == 'graph_check_stale'
    app.state.store.save(owner, prep['id'], prep)
    record = app.state.store.get(owner, inspected['id'], 'graph_checks')
    app.state.store.save(owner, record['id'], {**record, 'expires_at': time.time() - 1})
    response = client.post('/evaluation/api/runs', json={**intent, 'graph_check_id': inspected['id'], 'replace_existing_graph': True})
    assert response.status_code == 409 and response.json()['code'] == 'graph_check_expired'
    assert graph.commits == 0


def test_graph_check_errors_are_bounded_and_never_expose_dependency_errors(environment, graph, monkeypatch):
    import neo4j
    import evaluation.graph_import as graph_import
    client, app, deps, cfg = environment
    query, intent = preparation(client)
    monkeypatch.setattr(graph_import, 'MAX_NODES', 1)
    response = client.post('/evaluation/api/graph-import-checks', json=query)
    assert response.status_code == 413 and response.json()['code'] == 'graph_scope_limit'
    assert not any('elementId' in sql for sql, _ in graph.calls)
    def unavailable(*args, **kwargs): raise ServiceUnavailable('Synthetic private dependency diagnostic')
    monkeypatch.setattr(neo4j.GraphDatabase, 'driver', unavailable)
    response = client.post('/evaluation/api/graph-import-checks', json=query)
    assert response.status_code == 503 and response.json()['code'] == 'graph_check_unavailable'
    assert 'Synthetic private dependency diagnostic' not in response.text
    assert graph.commits == 0


def test_scope_validation_and_invalid_imports_never_open_a_write_transaction(environment, graph):
    client, app, deps, cfg = environment
    for connection in ({}, {'workspace_property': 'invalid`field', 'ontology_property': 'knowledge_package_id'},
                       {'workspace_property': 'scope', 'ontology_property': 'scope', 'workspace_value': 'workspace_demo', 'ontology_value': 'knowledge_package_demo'}):
        with pytest.raises(Failure): graph_scope(connection)
    query, intent = preparation(client)
    owner = Identity(cfg, deps).verify('synthetic-user-a')
    prep = app.state.store.get(owner, query['preparation_id'], 'preparations')
    connection = GraphImports.connection(app.state.store, prep['source'], 'knowledge_package_demo')
    source = {**prep['source'], 'connection': app.state.store.encrypt(connection)}
    with pytest.raises(Failure) as failure:
        DataSources(cfg, app.state.store).prepare('a' * 32, source, prep['mapping'])
    assert failure.value.code == 'graph_check_required'
    assert not graph.calls


def test_same_scope_is_blocked_while_another_account_is_evaluating(environment, graph):
    client, app, deps, cfg = environment
    query, intent = preparation(client)
    inspected = check(client, query)
    owner = Identity(cfg, deps).verify('synthetic-user-a')
    other_owner = Identity(cfg, deps).verify('synthetic-user-b')
    prep = app.state.store.get(owner, query['preparation_id'], 'preparations')
    active = app.state.store.create(other_owner, 'runs', {'status': 'running', 'source': prep['source'], 'package_ref': prep['package_ref']})
    app.state.engine.active.add((other_owner, active['id']))
    try:
        response = client.post('/evaluation/api/runs', json={**intent, 'graph_check_id': inspected['id'], 'replace_existing_graph': True})
        assert response.status_code == 409 and response.json()['code'] == 'graph_scope_busy'
        assert graph.commits == 0
    finally:
        app.state.engine.active.remove((other_owner, active['id']))


def test_resuming_after_a_later_import_refuses_to_query_replaced_graph_data(environment, graph):
    client, app, deps, cfg = environment
    consumer(deps)
    query, intent = preparation(client)
    inspected = check(client, query)
    started = client.post('/evaluation/api/runs', json={**intent, 'graph_check_id': inspected['id'], 'replace_existing_graph': True}).json()
    assert wait(client, started['id'])['status'] == 'completed'
    owner = Identity(cfg, deps).verify('synthetic-user-a')
    run = app.state.store.get(owner, started['id'], 'runs')
    app.state.store.save(owner, started['id'], {**run, 'status': 'interrupted'})
    graph.marker['run_id'] = 'later_run_demo'
    count = len(deps.submissions)
    assert client.post('/evaluation/api/runs/' + started['id'] + '/resume').status_code == 200
    for _ in range(100):
        state = client.get('/evaluation/api/runs/' + started['id']).json()
        if (state.get('error') or {}).get('code') == 'graph_scope_changed': break
        time.sleep(.02)
    assert state['error']['code'] == 'graph_scope_changed'
    assert len(deps.submissions) == count and graph.commits == 1
