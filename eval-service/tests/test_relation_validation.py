"""Issue 367: synthetic relation packages, no live dependency calls or persistent archives."""
import io
import zipfile

import pytest

from evaluation import packages
from evaluation.core import Failure, digest
from evaluation.graph_import import GraphImports, relation_type
from test_evaluation import environment, synthetic_package, wait  # noqa: F401

FIELDS = '# 字段\n| 英文名称 | 类型 | 主键 |\n| --- | --- | --- |\n| id | string | 是 |\n| fk | string | 否 |\n'
TABLE = '# 关系\n| 关系 | 目标 | 源关联字段 | 目标关联字段 |\n| --- | --- | --- | --- |\n'


def archive_with(relations, other=True, fields=FIELDS):
    files, _ = packages.unpack(synthetic_package())
    files['objects/item.md'] = ('# 数据来源\nitems\n' + fields + relations).encode()
    if other:
        files['objects/other.md'] = ('# 数据来源\nothers\n' + FIELDS).encode()
    result = io.BytesIO()
    with zipfile.ZipFile(result, 'w') as archive:
        for name, body in files.items():
            archive.writestr('knowledge_package_demo/' + name, body)
    return result.getvalue()


def install(deps, raw):
    deps.raw = raw
    deps.receipt['package_digest'] = digest(raw)
    deps.releases['release_demo'] = (raw, dict(deps.receipt))


@pytest.mark.parametrize('relations', [
    TABLE + '| references | absent | id | id |\n',
    TABLE + '| references | other | fk | id |\n| references | other | fk | id |\n',
    TABLE + '| references | other | absent | id |\n',
    TABLE + '| references | other | fk | |\n',
    '# 关联对象\n- 当前对象 → references（fk → ） → [[other]]\n',
])
@pytest.mark.parametrize('mode', ['none', 'dataset', 'nl2sql_http', 'neo4j'])
def test_invalid_relations_block_preparation_before_any_import(environment, monkeypatch, relations, mode):
    client, app, deps, _ = environment
    install(deps, archive_with(relations))
    # Freeze provides synthetic schemas; no SQL or Neo4j connection is opened.
    source = {'mode': 'connection' if mode in ('nl2sql_http', 'neo4j') else mode, 'provider': mode}
    tables = {} if mode == 'none' else {name: {'columns': ['id', 'fk'], 'rows': []} for name in ('items', 'others')}
    monkeypatch.setattr(app.state.engine.data, 'freeze', lambda owner, value: (source, tables))
    response = client.post('/evaluation/api/package-preparations', json={
        'request_id': 'synthetic_invalid', 'package_ref': {'ontology_id': 'knowledge_package_demo', 'release_id': 'release_demo'}})
    assert response.status_code == 422
    body = response.json()
    assert body['code'] == 'EVALUATION_PACKAGE_INVALID'
    assert 'objects/item.md' in body['details']['problems'][0] and '行' in body['details']['problems'][0]
    assert not deps.workspaces and not deps.submissions and not deps.requests
    assert client.get('/evaluation/api/runs').json()['total'] == 0


@pytest.mark.parametrize('name', ['属于other', '1:N', '关联/包含', 'named`type'])
@pytest.mark.parametrize('graph', [False, True])
def test_legacy_names_survive_mapping_and_safe_graph_plan(name, graph):
    raw = archive_with(TABLE + f'| {name} | other | fk | id |\n')
    proof = packages.validate_relations(raw)
    assert proof['package_digest'] == digest(raw)
    mapping, preview = packages.generate_mapping(raw, {'items': ['id', 'fk'], 'others': ['id', 'fk']}, graph)
    if graph:
        assert preview['skipped'] == []
        assert preview['relations']['item'][0]['relation'] == name
        scope = {'workspace_property': 'workspace_id', 'ontology_property': 'ontology_id'}
        data = {table: {'columns': ['id', 'fk'], 'rows': [['synthetic_1', 'synthetic_1']]}
                for table in ('items', 'others')}
        _, queries = GraphImports.plan(scope, data, mapping, preview['relations'])
        assert f'[:`{name.replace(chr(96), chr(96) * 2)}`]' in queries[0]
    derived = packages.derive(raw, mapping, '0.1.2')
    files, _ = packages.unpack(derived)
    assert name in files['objects/item.md'].decode()
    packages.validate_relations(derived)


def test_self_relations_and_different_field_pairs_remain_distinct():
    raw = archive_with(TABLE + '| 1:N | item | fk | id |\n| 1:N | item | id | fk |\n')
    packages.validate_relations(raw)
    _, preview = packages.generate_mapping(raw, {'items': ['id', 'fk'], 'others': ['id', 'fk']}, True)
    assert len(preview['relations']['item']) == 2


@pytest.mark.parametrize('name', ['', 'a\\u0060b', 'a\x00b', 'a\nb', 'x' * 257])
def test_graph_relationship_quoting_rejects_unsafe_names(name):
    with pytest.raises(Failure):
        relation_type(name)


def test_explicit_fields_without_property_declarations_are_rejected():
    with pytest.raises(Failure, match='显式关联字段'):
        packages.validate_relations(archive_with(TABLE + '| references | other | fk | id |\n', fields=''))


@pytest.mark.parametrize('legacy', [False, True])
def test_legacy_names_create_run_and_old_preparations_are_revalidated(environment, legacy):
    client, app, deps, _ = environment
    install(deps, archive_with(TABLE + '| 1:N | other | fk | id |\n'))
    prep = client.post('/evaluation/api/package-preparations', json={
        'request_id': 'synthetic_valid', 'package_ref': {'ontology_id': 'knowledge_package_demo', 'release_id': 'release_demo'}}).json()
    owner, record = app.state.store.find(prep['id'], 'preparations')
    if legacy:
        record.pop('relation_validation')
        app.state.store.save(owner, prep['id'], record)
    cases = client.post('/evaluation/api/eval-sets', json={'name': 'Synthetic set', 'cases': [{'question': 'Synthetic question', 'reference': '2'}]}).json()
    response = client.post('/evaluation/api/runs', json={'request_id': 'synthetic_run', 'preparation_id': prep['id'], 'eval_set_id': cases['id']})
    assert response.status_code == 202, response.text
    assert wait(client, response.json()['id'])['status'] == 'completed'
    assert app.state.store.get(owner, prep['id'])['relation_validation']['rule_version'] == packages.RELATION_RULE_VERSION


def test_old_preparation_cannot_bypass_invalid_original_package(environment):
    client, app, deps, _ = environment
    raw = archive_with(TABLE + '| references | absent | id | id |\n')
    install(deps, raw)
    owner = app.state.engine.identity.verify('synthetic-user-a')
    prep = app.state.store.create(owner, 'preparations', {'status': 'ready', 'package_ref': deps.receipt,
        'base': deps.receipt, 'source': {'mode': 'none'}, 'mapping': None, 'preview': {}, 'changed': False})
    cases = client.post('/evaluation/api/eval-sets', json={'name': 'Synthetic set', 'cases': [{'question': 'Synthetic', 'reference': '2'}]}).json()
    response = client.post('/evaluation/api/runs', json={'request_id': 'synthetic_old', 'preparation_id': prep['id'], 'eval_set_id': cases['id']})
    assert response.status_code == 422 and response.json()['code'] == 'EVALUATION_PACKAGE_INVALID'
    assert not deps.submissions and not deps.workspaces


def test_package_changed_after_preparation_is_rejected_before_workspace_creation(environment, monkeypatch):
    client, app, deps, _ = environment
    install(deps, archive_with(TABLE + '| 1:N | other | fk | id |\n'))
    prep = client.post('/evaluation/api/package-preparations', json={
        'request_id': 'synthetic_pinned', 'package_ref': {'ontology_id': 'knowledge_package_demo', 'release_id': 'release_demo'}}).json()
    cases = client.post('/evaluation/api/eval-sets', json={'name': 'Synthetic set', 'cases': [{'question': 'Synthetic', 'reference': '2'}]}).json()
    monkeypatch.setattr(app.state.engine, 'start', lambda *args: None)
    response = client.post('/evaluation/api/runs', json={
        'request_id': 'synthetic_pinned_run', 'preparation_id': prep['id'], 'eval_set_id': cases['id']})
    assert response.status_code == 202
    # Simulate a corrupt dependency replacing immutable release bytes between preparation and execution.
    install(deps, archive_with(TABLE + '| references | absent | fk | id |\n'))
    engine = app.state.engine
    owner = engine.identity.verify('synthetic-user-a')
    monkeypatch.setattr(engine, 'token', lambda *args: 'synthetic-user-a')
    with pytest.raises(Failure) as error:
        engine._execute(owner, response.json()['id'])
    assert error.value.code == 'package_changed'
    assert not deps.workspaces and not deps.submissions


def test_same_identity_duplicate_reports_both_declarations():
    raw = archive_with(TABLE + '| references | other | fk | id |\n| references | other | fk | id |\n')
    with pytest.raises(Failure) as error:
        packages.validate_relations(raw)
    problem = error.value.details['problems'][0]
    assert '首次声明于' in problem and problem.count('objects/item.md') == 2
