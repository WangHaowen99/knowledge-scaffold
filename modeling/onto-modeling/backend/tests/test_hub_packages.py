"""建模项目对接知识包 Hub（ADR-0098），只用隔离目录里的合成输入；Hub 用同级 knowledge-hub 源码的真实存储。

发布按提交导出的只读树进行：测试里改了文件要先提交才进包。"""
import hashlib
import io
import json
from pathlib import Path
import shutil
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.main import app
from app.services import auth, hub_packages, knowledge_hub
from app.services import publish as publish_service
from app.services import solver as solver_service
from app.services.workspace import WorkspaceError, WorkspaceManager, _git

# Standalone modeling test bundles do not include the sibling Hub source tree.
pytest.importorskip('hub', reason='Hub source is not included in standalone modeling tests')

from hub.package_contract import PackageError
from hub.package_store import PackageStore, public
from hub.storage import Store
from hub.user_auth import Identity


def commit(root, message='test: synthetic edit'):
    _git(root, 'add', '-A')
    _git(root, 'commit', '-qm', message, '--allow-empty')


def client_for(claims=None):
    """完整应用（v1 路由与鉴权中间件），合成登录令牌。"""
    return TestClient(app, headers={'Authorization': 'Bearer synthetic-access'})


def login(monkeypatch, sub='operator_a'):
    monkeypatch.setattr(auth, 'ENABLED', True)
    claims = {'sub': sub, 'iss': 'https://example.com'}
    monkeypatch.setattr(auth, 'verify_access_token', lambda token: dict(claims) if token else None)
    return claims


@pytest.fixture
def setup(tmp_path, monkeypatch):
    manager = WorkspaceManager(tmp_path / 'modeling', tmp_path / 'assets')
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path / 'modeling')
    monkeypatch.setattr(deps, 'workspaces', manager)
    meta = manager.create_project('project_alpha', owner='operator_a')
    root = manager.project_root(meta['id'])
    (root / 'research').mkdir(exist_ok=True)
    (root / 'research/report.md').write_text('# Synthetic research\n')
    (root / 'ontology/object/object_demo.md').write_text('# Synthetic object\n')
    (root / 'ontology/operation').mkdir(exist_ok=True)
    (root / 'ontology/operation/operation_demo.md').write_text('# Synthetic operation\n')
    (root / 'reports').mkdir(exist_ok=True)
    (root / 'reports/validation.md').write_text('# Synthetic validation\n')
    commit(root)
    store = PackageStore(Store(tmp_path / 'hub'))
    captured = []

    def call(method, path, token, body=None, upload=None, headers=None, download=None, upload_name=None, **_):
        actor = Identity(token, 'modeling')
        captured.append((method, path, body))
        with store.io.lock():
            if path == '/api/package-publish-requests':
                return store.reserve(body, actor)[0]
            if method == 'GET' and path.startswith('/api/package-publish-requests/'):
                try:
                    return public(store.get_request(path.rsplit('/', 1)[-1], actor))
                except PackageError as exc:
                    raise WorkspaceError(str(exc), exc.status, exc.code) from exc
            if path.endswith('/content'):
                with Path(upload).open('rb') as handle:
                    return store.upload(path.split('/')[-2], actor, handle, headers['X-Package-Digest'])
            if path.endswith('/commit'):
                return store.commit(path.split('/')[-2], actor)
            parts = path.split('?')[0].strip('/').split('/')
            if len(parts) == 2:
                return store.packages(actor.subject)
            if len(parts) == 4:
                return store.listing(parts[2], actor.subject)
            receipt, archive = store.release(parts[2], parts[4], actor.subject)
            if download:
                shutil.copyfile(archive, download)
                return Path(download)
            return receipt
    monkeypatch.setattr(hub_packages, 'call', call)
    return manager, meta['id'], store, captured, call


def test_actual_modeling_export_uses_independent_identity_and_retains_parent(setup):
    manager, pid, store, captured, _ = setup
    first = hub_packages.publish(manager, pid, 'request_demo', 'synthetic-actor', 'synthetic-scope')
    assert first['ontology_id'] != pid and first['ontology_id'] != pid
    assert first == hub_packages.publish(manager, pid, 'request_demo', 'synthetic-actor', 'synthetic-scope')
    second = hub_packages.publish(manager, pid, 'request_next', 'synthetic-actor', 'synthetic-scope')
    assert second['base_release_id'] == first['release_id']
    assert second['ontology_id'] == first['ontology_id']
    assert second['version'] == '0.1.2'
    assert all('project_id' not in (body or {}) and 'workspace_id' not in (body or {}) for _, _, body in captured)
    assert not (store.root / 'projects').exists()
    assert hub_packages.releases(pid, 'synthetic-scope', 'synthetic-actor')['total'] == 2
    hub_packages.publish(manager, pid, 'request_demo', 'synthetic-actor', 'synthetic-scope')
    assert hub_packages.association(pid, 'synthetic-scope')['release_id'] == second['release_id']


def test_members_share_identity_but_require_explicit_hub_authorization(setup):
    from hub.package_contract import PackageError
    from hub.user_auth import subject_id
    manager, pid, store, _, _ = setup
    first = hub_packages.publish(manager, pid, 'request_demo', 'actor_a', 'scope_a')
    with pytest.raises(PackageError):
        hub_packages.publish(manager, pid, 'request_demo', 'actor_b', 'scope_b')
    target = dict(issuer='https://example.com', sub='operator_b')
    actor_b = subject_id(**target)
    store.grant(first['ontology_id'], 'actor_a', [target])
    second = hub_packages.publish(manager, pid, 'request_allowed', actor_b, 'scope_b')
    assert first['ontology_id'] == second['ontology_id'] == hub_packages.ontology_id(pid)
    assert second['base_release_id'] == first['release_id']
    assert store.packages('actor_a')['total'] == store.packages(actor_b)['total'] == 1


def test_lost_upload_reuses_frozen_source_after_edits(setup, monkeypatch):
    manager, pid, _, _, actual = setup
    sent = []
    def flaky(method, path, token, **kwargs):
        if path.endswith('/content'):
            sent.append(Path(kwargs['upload']).read_bytes())
            if len(sent) == 1:
                raise WorkspaceError('Synthetic transport outage', 503)
        return actual(method, path, token, **kwargs)
    monkeypatch.setattr(hub_packages, 'call', flaky)
    with pytest.raises(WorkspaceError):
        hub_packages.publish(manager, pid, 'request_demo', 'actor_a', 'scope_a')
    (manager.project_root(pid) / 'ontology/object/object_demo.md').write_text('# Changed synthetic object\n')
    result = hub_packages.publish(manager, pid, 'request_demo', 'actor_a', 'scope_a')
    assert result['status'] == 'committed' and sent[0] == sent[1]
    assert not list(hub_packages.directory(pid, 'scope_a').rglob('package.zip'))


def test_hub_committed_local_receipt_failure_is_recoverable(setup, monkeypatch):
    manager, pid, store, _, _ = setup
    with monkeypatch.context() as m:
        m.setattr(hub_packages, 'record', lambda *a, **k: (_ for _ in ()).throw(OSError('synthetic failure')))
        result = hub_packages.publish(manager, pid, 'request_demo', 'actor_a', 'scope_a')
    assert result['local_record_pending']
    restored = hub_packages.publish(manager, pid, 'request_demo', 'actor_a', 'scope_a')
    assert restored['release_id'] == result['release_id']


def test_delegated_retry_returns_saved_receipt_after_local_association_advanced(setup, monkeypatch):
    manager, pid, store, _, _ = setup
    result = hub_packages.publish(manager, pid, 'request_demo', 'actor_a', 'scope_a')
    restored = hub_packages.publish(
        manager, pid, 'request_demo', '', 'scope_a', delegated={
            'token_id': '1' * 32, 'issuer': 'https://example.com', 'sub': 'operator_a'},
        expected_input_digest=hub_packages.transport.load(
            hub_packages.directory(pid, 'scope_a') / 'requests/request_demo/task.json')['payload']['input_digest'],
        expected_base=(None, None))
    assert restored == result
    assert store.packages('actor_a')['total'] == 1
    assert hub_packages.association(pid, 'scope_a')['release_id'] == result['release_id']


def test_saved_receipt_rejects_reused_request_id_for_changed_publish_intent(setup):
    manager, pid, store, _, _ = setup
    result = hub_packages.publish(manager, pid, 'request_demo', 'actor_a', 'scope_a')
    (manager.project_root(pid) / 'ontology/object/object_demo.md').write_text('# Changed synthetic object\n')
    commit(manager.project_root(pid))
    _, changed_digest = hub_packages.transport.source_state(manager, pid, False)
    with pytest.raises(WorkspaceError) as exc:
        hub_packages.publish(
            manager, pid, 'request_demo', '', 'scope_a', delegated={
                'token_id': '2' * 32, 'issuer': 'https://example.com', 'sub': 'operator_a'},
            expected_input_digest=changed_digest,
            expected_base=(result['release_id'], result['package_digest']))
    assert exc.value.status == 409 and exc.value.code == 'publish_preview_stale'
    assert store.packages('actor_a')['total'] == 1


def test_lost_commit_response_recovers_after_a_newer_version_was_published(setup, monkeypatch):
    manager, pid, store, _, actual = setup
    committed = []
    def lose_first_commit(method, path, token, **kwargs):
        result = actual(method, path, token, **kwargs)
        if path.endswith('/commit') and not committed:
            committed.append(result)
            raise WorkspaceError('Synthetic lost commit response', 503)
        return result
    monkeypatch.setattr(hub_packages, 'call', lose_first_commit)
    with pytest.raises(WorkspaceError):
        hub_packages.publish(manager, pid, 'request_a', 'actor_a', 'scope_a')
    task = hub_packages.transport.load(
        hub_packages.directory(pid, 'scope_a') / 'requests/request_a/task.json')
    hub_packages.record(manager, pid, 'scope_a', committed[0])
    (manager.project_root(pid) / 'ontology/object/object_demo.md').write_text('# Newer synthetic object\n')
    newer = hub_packages.publish(manager, pid, 'request_b', 'actor_a', 'scope_a')
    assert newer['version'] == '0.1.2'

    recovered = hub_packages.publish(
        manager, pid, 'request_a', 'actor_a', 'scope_a',
        expected_input_digest=task['payload']['input_digest'], expected_base=(None, None))
    assert recovered['release_id'] == committed[0]['release_id']
    assert store.packages('actor_a')['total'] == 1
    assert store.listing(recovered['ontology_id'], 'actor_a')['total'] == 2
    assert hub_packages.association(pid, 'scope_a')['release_id'] == newer['release_id']


def test_import_external_then_publish_preserves_manifest_and_package_identity(setup, tmp_path):
    manager, pid, store, _, _ = setup
    original = tmp_path / 'synthetic.zip'
    publish_service.export_package_zip(manager, pid, original, version='0.1.8', package_id='ontology_demo')
    data = original.read_bytes()
    actor = Identity('actor_a', 'consumer')
    payload = dict(request_id='external_demo', ontology_id='ontology_demo', change_type='external_import',
                   input_digest=hashlib.sha256(data).hexdigest(), version='0.1.8')
    store.reserve(payload, actor)
    store.upload('external_demo', actor, io.BytesIO(data), payload['input_digest'])
    receipt = store.commit('external_demo', actor)
    body = dict(request_id='import_demo', ontology_id=receipt['ontology_id'], release_id=receipt['release_id'],
                package_digest=receipt['package_digest'], name='project_beta')
    imported = hub_packages.import_package(manager, body, 'actor_a', 'scope_a', 'operator_a')
    assert hub_packages.import_package(manager, body, 'actor_a', 'scope_a', 'operator_a')['id'] == imported['id']
    imported_root = manager.project_root(imported['id'])
    # 同原生导入：标为导入，带进来的产物标「需复核」而不是直接算已确认
    from app.services.flow import state as node_state
    assert imported['created_from'] == 'import'
    assert {item['source'] for item in node_state.reviews(imported_root)} == {'import'}
    for rel in ('research/report.md', 'reports/validation.md'):
        path = imported_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# Synthetic stage artifact\n')
    result = hub_packages.publish(manager, imported['id'], 'next_demo', 'actor_a', 'scope_a', True)
    assert result['ontology_id'] == hub_packages.ontology_id(imported['id'])
    assert result['ontology_id'] != receipt['ontology_id'] and result['version'] == '0.1.1'
    assert 'base_digest' not in result


def test_project_without_hub_package_lists_empty_items(setup, monkeypatch):
    _manager, pid, _, _, _ = setup
    login(monkeypatch)
    listed = client_for().get(f'/api/v1/projects/{pid}/hub/releases')
    assert listed.status_code == 200, listed.text
    assert listed.json()['items'] == [] and listed.json()['total'] == 0


def test_modeling_routes_require_login_and_project_permissions(setup, monkeypatch):
    manager, pid, _, _, _ = setup
    login(monkeypatch)
    url = f'/api/v1/projects/{pid}/releases'
    assert TestClient(app).post(url, json={'request_id': 'request_demo'}).status_code == 401
    response = client_for().post(url, json={'request_id': 'request_demo'})
    assert response.status_code == 201, response.text
    login(monkeypatch, 'operator_b')
    assert client_for().post(url, json={'request_id': 'request_other'}).status_code == 403


def test_solver_release_requires_acknowledgement_and_records_its_revision(setup, monkeypatch):
    from tests import test_solver_contract_check as solver
    manager, plain, _, calls, _ = setup
    pid, root = solver._with_contract(manager)
    complete_publish_stages(root)
    manager._update_meta(pid, owner='operator_a')
    login(monkeypatch)
    client = client_for()
    url = f'/api/v1/projects/{pid}/releases'
    refused = client.post(url, json={'request_id': 'solver_demo'})
    assert refused.status_code == 409 and refused.json()['code'] == 'solver_validation_acknowledgement_required'
    assert calls == []
    accepted = client.post(url, json={'request_id': 'solver_demo', 'solver_unverified_acknowledged': True})
    assert accepted.status_code == 201, accepted.text
    receipt = accepted.json()['hub']
    # 普通知识包不需要确认，也不记录求解契约修订
    assert client.post(f'/api/v1/projects/{plain}/releases', json={'request_id': 'plain_demo'}).status_code == 201
    assert hub_packages.solver_revisions(pid) == {receipt['release_id']: solver_service.solver_check(manager, pid).revision}
    stored = knowledge_hub.load(hub_packages._solver_releases_path(pid))[receipt['release_id']]
    assert stored['acknowledged_by'] == 'operator_a' and stored['version'] == accepted.json()['version']
    assert hub_packages.solver_revisions(plain) == {}


def complete_publish_stages(root):
    """Adds the synthetic stage files the publish gate requires, keeping the solver project's own objects."""
    (root / 'research').mkdir(exist_ok=True)
    (root / 'research/report.md').write_text('# Synthetic research\n')
    (root / 'reports').mkdir(exist_ok=True)
    (root / 'reports/validation.md').write_text('# Synthetic validation\n')
    objects = root / 'ontology/object'
    objects.mkdir(parents=True, exist_ok=True)
    if not any(path.name != 'index.md' for path in objects.glob('*.md')):
        (objects / 'object_demo.md').write_text('# Synthetic object\n')
    commit(root)


def solver_publish_client(setup, monkeypatch):
    from tests import test_solver_contract_check as solver
    manager = setup[0]
    pid, root = solver._with_contract(manager)
    complete_publish_stages(root)
    manager._update_meta(pid, owner='operator_a')
    login(monkeypatch)
    return manager, pid, root, app


def test_every_publish_entry_needs_the_solver_acknowledgement(setup, monkeypatch):
    manager, pid, _root, _app = solver_publish_client(setup, monkeypatch)
    calls = setup[3]
    subject = json.dumps(['https://example.com', 'operator_a'], ensure_ascii=False)
    with pytest.raises(WorkspaceError) as raised:
        hub_packages.publish(manager, pid, 'solver_direct', 'synthetic-actor', subject)
    assert raised.value.code == 'solver_validation_acknowledgement_required' and calls == []
    receipt = hub_packages.publish(manager, pid, 'solver_direct', 'synthetic-actor', subject, acknowledged_by='operator_a')
    assert receipt['status'] == 'committed'
    verified = hub_packages.publish(manager, pid, 'solver_verified', 'synthetic-actor', subject,
                                    verified_revision=solver_service.solver_check(manager, pid).revision)
    assert verified['status'] == 'committed'


def record_solver_validation(pid, release_id, version, failed=0, earlier=None):
    """按评测服务完成通知的口径写入合成的求解验证结果；earlier 是更早结束的另一个发布 (release_id, version, failed)。"""
    from app.services import package_evaluations
    subject = json.dumps(['https://example.com', 'operator_a'], ensure_ascii=False)
    owner = 'e' * 64
    knowledge_hub.save(package_evaluations._owner_path(subject), {'owner': owner})

    def entry(release, label, failures, finished):
        return {'release_id': release, 'version': label, 'run_id': 'f' * 32, 'status': 'completed', 'run_type': 'solver',
                'finished_at': finished, 'artifacts': {'generated_at': finished},
                'solver_summary': {'total': 1, 'passed': 1 - failures, 'failed': failures}}
    releases = {release_id: entry(release_id, version, failed, 2000.0)}
    if earlier:
        releases[earlier[0]] = entry(*earlier, 1000.0)
    knowledge_hub.save(package_evaluations._index_path(hub_packages.ontology_id(pid), owner),
                       {'active': None, 'releases': {}, 'solver_releases': releases})


def solver_gate(client, pid):
    check = client.get(f'/api/v1/projects/{pid}/publish/check').json()
    return next(gate for gate in check['gates'] if gate['id'] == 'solver_contract')


@pytest.mark.parametrize('failed', [0, 1])
def test_verified_solver_revision_publishes_without_acknowledgement(setup, monkeypatch, failed):
    manager, pid, _root, _app = solver_publish_client(setup, monkeypatch)
    client = client_for()
    url = f'/api/v1/projects/{pid}/releases'
    first = client.post(url, json={'request_id': 'solver_first', 'solver_unverified_acknowledged': True})
    assert first.status_code == 201, first.text
    record_solver_validation(pid, first.json()['hub']['release_id'], first.json()['version'], failed)
    gate = solver_gate(client, pid)
    again = client.post(url, json={'request_id': 'solver_second'})
    if failed:
        assert gate['requires_acknowledgement'] is True and 'verified_revision' not in gate
        assert again.status_code == 409 and again.json()['code'] == 'solver_validation_acknowledgement_required'
        return
    revision = solver_service.solver_check(manager, pid).revision
    assert gate['requires_acknowledgement'] is False and gate['verified_revision'] == revision
    assert again.status_code == 201, again.text
    stored = knowledge_hub.load(hub_packages._solver_releases_path(pid))[again.json()['hub']['release_id']]
    assert stored['acknowledged_by'] == '' and stored['revision'] == revision


@pytest.mark.parametrize(('older_failed', 'newer_failed', 'exempt'), [(0, 1, False), (1, 0, True)])
def test_latest_validation_of_the_revision_decides_the_exemption(setup, monkeypatch, older_failed, newer_failed, exempt):
    _manager, pid, _root, _app = solver_publish_client(setup, monkeypatch)
    client = client_for()
    url = f'/api/v1/projects/{pid}/releases'
    older = client.post(url, json={'request_id': 'solver_older', 'solver_unverified_acknowledged': True}).json()
    newer = client.post(url, json={'request_id': 'solver_newer', 'solver_unverified_acknowledged': True}).json()
    record_solver_validation(pid, newer['hub']['release_id'], newer['version'], newer_failed,
                             earlier=(older['hub']['release_id'], older['version'], older_failed))
    gate = solver_gate(client, pid)
    again = client.post(url, json={'request_id': 'solver_third'})
    assert gate['requires_acknowledgement'] is (not exempt)
    assert again.status_code == (201 if exempt else 409), again.text


def test_changed_solver_revision_needs_acknowledgement_again(setup, monkeypatch):
    _manager, pid, root, _app = solver_publish_client(setup, monkeypatch)
    client = client_for()
    url = f'/api/v1/projects/{pid}/releases'
    first = client.post(url, json={'request_id': 'solver_first', 'solver_unverified_acknowledged': True}).json()
    record_solver_validation(pid, first['hub']['release_id'], first['version'])
    assert solver_gate(client, pid)['requires_acknowledgement'] is False
    model = next((root / 'ontology' / 'solver').glob('*/presentation.yaml'))
    model.write_text(model.read_text(encoding='utf-8') + '# synthetic edit\n', encoding='utf-8')
    commit(root, 'test: synthetic solver edit')
    assert solver_gate(client, pid)['requires_acknowledgement'] is True
    refused = client.post(url, json={'request_id': 'solver_second'})
    assert refused.status_code == 409 and refused.json()['code'] == 'solver_validation_acknowledgement_required'


def test_publish_writes_back_to_the_evaluation_service_right_away(setup, monkeypatch):
    from app.services import package_evaluations
    _manager, plain, _, _, _ = setup
    login(monkeypatch)
    calls = []

    def after_publish(pid, subject, token, receipt):
        calls.append((pid, json.loads(subject), token, receipt['status'], receipt['release_id']))
        return {'run_id': 'f' * 32, 'status': 'published'}
    monkeypatch.setattr(package_evaluations, 'after_publish', after_publish)
    published = client_for().post(f'/api/v1/projects/{plain}/releases', json={'request_id': 'writeback_demo'})
    assert published.status_code == 201, published.text
    assert calls == [(plain, ['https://example.com', 'operator_a'], 'synthetic-access', 'committed',
                      published.json()['hub']['release_id'])]
    assert published.json()['evaluation_writeback']['status'] == 'published'


def test_publish_rechecks_the_verified_revision_when_freezing(setup, monkeypatch):
    manager, pid, _root, _app = solver_publish_client(setup, monkeypatch)
    subject = json.dumps(['https://example.com', 'operator_a'], ensure_ascii=False)
    with pytest.raises(WorkspaceError) as raised:
        hub_packages.publish(manager, pid, 'solver_race', 'synthetic-actor', subject, verified_revision='sha256:' + '0' * 64)
    assert raised.value.code == 'solver_validation_acknowledgement_required'


def test_package_project_lookup_lists_only_the_callers_visible_linked_projects(setup, monkeypatch):
    manager, pid, _, _, _ = setup
    claims = {'sub': 'operator_a', 'iss': 'https://example.com'}
    # Same issuer/subject key the Hub routes derive from the login claims.
    subject = json.dumps([claims['iss'], claims['sub']], ensure_ascii=False)
    first = hub_packages.publish(manager, pid, 'request_demo', 'synthetic-actor', subject)
    copy = manager.create_project('project_alpha_copy', owner='operator_a')
    hidden = manager.create_project('project_hidden', owner='operator_b')
    foreign = manager.create_project('project_foreign_scope', owner='operator_a')
    for project, scope in ((copy, subject), (hidden, subject), (foreign, 'scope_other')):
        hub_packages.record(manager, project['id'], scope, first)
    package_id = first['ontology_id']
    result = hub_packages.linked_projects(manager, subject, 'operator_a', False, [package_id, 'package_unknown'])
    assert result == {'items': [
        {'ontology_id': package_id, 'projects': ['project_alpha', 'project_alpha_copy', 'project_foreign_scope'], 'total': 3},
        {'ontology_id': 'package_unknown', 'projects': [], 'total': 0}]}
    assert hub_packages.linked_projects(manager, subject, 'operator_a', True, [package_id])['items'][0]['total'] == 4
    with pytest.raises(WorkspaceError):
        hub_packages.linked_projects(manager, subject, 'operator_a', False, [f'package_{i}' for i in range(21)])

    login(monkeypatch)
    # 评测服务调用的外部路径，一字不改
    url = f'/api/hub/package-projects?ontology_id={package_id}'
    assert TestClient(app).get(url).status_code == 401
    response = client_for().get(url)
    assert response.json()['items'][0]['projects'] == ['project_alpha', 'project_alpha_copy', 'project_foreign_scope']


def test_local_recovery_and_project_deletion_do_not_delete_hub_package(setup, monkeypatch):
    manager, pid, store, _, _ = setup
    with monkeypatch.context() as m:
        m.setattr(hub_packages, 'record', lambda *a, **k: (_ for _ in ()).throw(OSError('synthetic failure')))
        receipt = hub_packages.publish(manager, pid, 'request_demo', 'actor_a', 'scope_a')
    hub_packages.maintain(manager)
    assert hub_packages.association(pid, 'scope_a')['release_id'] == receipt['release_id']
    manager.delete_project(pid)
    hub_packages.maintain(manager)
    assert not hub_packages.directory(pid, 'scope_a').exists()
    assert store.release(receipt['ontology_id'], receipt['release_id'], 'actor_a')[0]['status'] == 'committed'


def test_solver_publish_gate_precedes_hub_reservation(setup, monkeypatch):
    manager, pid, _, calls, _ = setup
    def blocked(source_root, state_root=None):
        raise WorkspaceError('Synthetic solver gate failure', code='solver_contract_invalid')
    monkeypatch.setattr(solver_service.service, 'ensure_publishable', blocked)
    with pytest.raises(WorkspaceError, match='solver gate'):
        hub_packages.publish(manager, pid, 'solver_gate_demo', 'actor_a', 'scope_a', True)
    assert calls == []


@pytest.mark.parametrize('allow_unbound', [False, True])
def test_consumer_import_rules_precede_hub_reservation(setup, allow_unbound):
    manager, pid, _, calls, _ = setup
    root = manager.project_root(pid)
    # 只差大小写的对象文件：consumer 解压时判为重复路径而拒绝导入；降级发布也不能绕过
    (root / 'ontology/object/OBJECT_DEMO.md').write_text('# Synthetic object\n')
    commit(root)
    with pytest.raises(WorkspaceError) as caught:
        hub_packages.publish(manager, pid, 'structure_demo', 'actor_a', 'scope_a', allow_unbound)
    assert caught.value.code == 'package_structure_invalid' and 'duplicate paths' in str(caught.value)
    assert calls == []
    assert not (hub_packages.directory(pid, 'scope_a') / 'requests' / 'structure_demo' / 'task.json').exists()
    (root / 'ontology/object/OBJECT_DEMO.md').unlink()
    commit(root)
    assert hub_packages.publish(manager, pid, 'structure_demo', 'actor_a', 'scope_a', allow_unbound)['status'] == 'committed'


def test_evaluation_rejects_changed_validated_objects_before_hub_reservation(setup):
    manager, pid, _, calls, _ = setup
    with pytest.raises(WorkspaceError, match='评测对象'):
        hub_packages.publish(manager, pid, 'snapshot_changed', 'actor_a', 'scope_a',
                             kind='evaluation_snapshot', object_texts={'object_demo': '# Earlier synthetic object'})
    assert calls == []


def test_fixed_namespace_and_project_association(setup):
    import uuid
    manager, pid, _, _, _ = setup
    assert hub_packages.ontology_id('project_alpha-00000001') == 'dd15af86-473f-562d-a47e-5f0677437380'
    assert hub_packages.ontology_id(pid) != hub_packages.ontology_id(pid + '-copy')


def test_publish_records_release_locally_with_hub_version(setup):
    """Hub 提交后本地留存那份包、在发布的提交上打 release/<Hub 版本> 标签，发布记录带 Hub 身份。"""
    manager, pid, store, _, _ = setup
    root = manager.project_root(pid)
    head = _git(root, 'rev-parse', 'HEAD').strip()
    receipt = hub_packages.publish(manager, pid, 'request_demo', 'actor_a', 'scope_a', author='operator_a')
    [entry] = publish_service.releases(manager, pid)
    assert entry['version'] == receipt['version'] == '0.1.1' and entry['commit'] == head
    assert entry['hub'] == {key: receipt[key] for key in ('ontology_id', 'release_id', 'package_digest')}
    path, record = publish_service.release_package(manager, pid, receipt['version'])
    _, archived = store.release(receipt['ontology_id'], receipt['release_id'], 'actor_a')
    assert path.read_bytes() == archived.read_bytes() and record['release_id'] == receipt['release_id']
    assert manager.get_project(pid)['last_package_publish']['release_id'] == receipt['release_id']


def test_uncommitted_edits_stay_out_of_the_hub_package(setup):
    manager, pid, store, _, _ = setup
    root = manager.project_root(pid)
    (root / 'ontology/object/object_demo.md').write_text('# Uncommitted synthetic edit\n')
    receipt = hub_packages.publish(manager, pid, 'request_demo', 'actor_a', 'scope_a')
    _, archived = store.release(receipt['ontology_id'], receipt['release_id'], 'actor_a')
    with zipfile.ZipFile(archived) as archive:
        texts = [archive.read(name).decode('utf-8') for name in archive.namelist() if name.endswith('.md')]
    assert not any('Uncommitted synthetic edit' in text for text in texts)
