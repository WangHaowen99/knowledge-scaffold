"""Synthetic member-sharing and deletion fault scenarios."""
import pytest
from app import config
from app.services import auth, hub_members as members, hub_packages, knowledge_hub
from app.services.workspace import WorkspaceError


@pytest.fixture
def sharing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(auth, 'ISSUER', 'https://example.com')
    monkeypatch.setattr(hub_packages, 'association', lambda pid: {'ontology_id': hub_packages.ontology_id(pid)})
    monkeypatch.setattr(auth, 'resolve_hub_member', lambda name: {'issuer': auth.ISSUER, 'sub': name})
    users = {}
    calls = []

    def call(method, path, token, **kwargs):
        calls.append((method, path))
        if not token:
            raise WorkspaceError('Synthetic authentication failure', 401)
        if method == 'GET':
            return {'items': list(users.values()), 'next_offset': None}
        if method == 'PUT':
            for user in kwargs['body']['users']:
                users[user['sub']] = {**user, 'subject_id': user['sub']}
        if method == 'DELETE':
            users.pop(path.rsplit('/', 1)[-1], None)
        return {}
    monkeypatch.setattr(hub_packages, 'call', call)
    return users, calls


def test_incremental_retry_supersedes_failed_grant(sharing):
    users, calls = sharing
    before = {'owner': 'operator_a', 'members': {}}
    editor = {'owner': 'operator_a', 'members': {'operator_b': 'editor'}}
    members.queue('project_demo', before, editor)
    assert members.sync(None, 'project_demo', '')['status'] == 'failed'
    members.queue('project_demo', editor, before)
    assert members.sync(None, 'project_demo', 'synthetic')['status'] == 'synced'
    assert not users
    assert not any(method == 'PUT' for method, _ in calls)


def test_initialization_and_ordinary_publish_do_not_restore_external_revoke(sharing):
    users, calls = sharing
    meta = {'owner': 'operator_a', 'members': {'operator_b': 'editor', 'operator_c': 'viewer'}}
    class Manager:
        def get_project(self, pid):
            return meta
    manager = Manager()
    assert members.after_publish(manager, 'project_demo', 'synthetic', True)['status'] == 'synced'
    assert set(users) == {'operator_a', 'operator_b'}
    users.pop('operator_b')
    calls.clear()
    members.after_publish(manager, 'project_demo', 'synthetic', False)
    members.after_publish(manager, 'project_demo', 'synthetic', True)
    assert not calls
    assert 'operator_b' not in users


def test_removed_viewer_revokes_independent_grant(sharing):
    users, _ = sharing
    users['operator_b'] = {'issuer': auth.ISSUER, 'sub': 'operator_b', 'subject_id': 'operator_b'}
    members.queue('project_demo', {'members': {'operator_b': 'viewer'}}, {'members': {}})
    assert members.sync(None, 'project_demo', 'synthetic')['status'] == 'synced'
    assert not users


def test_delete_failure_keeps_progress_and_retries_same_target(sharing, monkeypatch):
    class Manager:
        def project_root(self, pid):
            return None
    manager = Manager()
    calls = []
    ontology = hub_packages.ontology_id('project_demo')
    def call(method, path, token, **kwargs):
        calls.append(method)
        if method == 'GET':
            return {'name': 'knowledge_package_demo'}
        if calls.count('DELETE') == 1:
            raise WorkspaceError('Synthetic disk failure', 503)
        return {'ontology_id': ontology, 'state': 'deleted'}
    monkeypatch.setattr(hub_packages, 'call', call)
    with pytest.raises(WorkspaceError):
        members.delete_package(manager, 'project_demo', 'synthetic')
    assert members.deletion('project_demo')['state'] == 'deleting'
    with pytest.raises(WorkspaceError):
        members.writable('project_demo')
    members.delete_package(manager, 'project_demo', 'synthetic')
    members.delete_package(manager, 'project_demo', 'synthetic')
    assert calls == ['GET', 'DELETE', 'DELETE']
    assert members.deletion('project_demo')['state'] == 'deleted'


def test_authorized_users_consumes_all_pages(monkeypatch):
    def call(method, path, token):
        return {'items': [{'sub': 'operator_a'}], 'next_offset': 1} if 'offset=0' in path else {'items': [{'sub': 'operator_b'}], 'next_offset': None}
    monkeypatch.setattr(hub_packages, 'call', call)
    assert len(members.authorized('ontology_demo', 'synthetic')) == 2


def test_member_route_preserves_local_success_on_hub_failure(sharing, monkeypatch):
    from types import SimpleNamespace
    from app.routers import projects
    meta = {'owner': 'operator_a', 'members': {}}
    monkeypatch.setattr(projects, 'ensure_owner', lambda *args: None)
    monkeypatch.setattr(projects.workspaces, 'get_project', lambda pid: dict(meta, members=dict(meta['members'])))
    def set_members(pid, values):
        meta['members'] = values
        return meta
    monkeypatch.setattr(projects.workspaces, 'set_members', set_members)
    monkeypatch.setattr(projects.modeling_mcp, 'sync_members', lambda *args: None)
    request = SimpleNamespace(headers={}, query_params={})
    result = projects.put_members('project_demo', projects.MembersBody(members={'operator_b': 'editor'}), request)
    assert result['members'] == {'operator_b': 'editor'}
    assert result['hub_sync']['status'] == 'failed'
    assert members.status('project_demo')['pending'] == {'operator_b': 'grant'}


def test_project_delete_requires_confirmation_and_preserves_project_on_hub_failure(sharing, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from fastapi import BackgroundTasks
    from app.routers import projects, hub_packages as routes
    monkeypatch.setattr(projects, 'ensure_owner', lambda *args: None)
    monkeypatch.setattr(routes, 'identity', lambda request: ('synthetic', 'synthetic', 'operator_a'))
    touched = []
    monkeypatch.setattr(projects.workspaces, 'get_project', lambda pid: touched.append(pid))
    request = SimpleNamespace()
    with pytest.raises(WorkspaceError, match='请确认'):
        asyncio.run(projects.delete_project('project_demo', request, BackgroundTasks(),
                    projects.DeleteProjectBody(delete_hub_package=True)))
    def fail(*args):
        raise WorkspaceError('Synthetic Hub failure', 503)
    monkeypatch.setattr(members, 'delete_package', fail)
    with pytest.raises(WorkspaceError, match='Synthetic Hub failure'):
        asyncio.run(projects.delete_project('project_demo', request, BackgroundTasks(),
                    projects.DeleteProjectBody(confirmed=True, delete_hub_package=True, hub_usage_confirmed=True)))
    assert not touched


def test_existing_hub_tombstone_still_requires_delete_to_finish_cleanup(sharing, monkeypatch):
    class Manager:
        def project_root(self, pid):
            return None
    calls = []
    ontology = hub_packages.ontology_id('project_demo')
    def call(method, path, token, **kwargs):
        calls.append(method)
        if method == 'GET':
            raise WorkspaceError('Synthetic deleted package', 410, 'package_deleted')
        return {'ontology_id': ontology, 'state': 'deleted'}
    monkeypatch.setattr(hub_packages, 'call', call)
    members.delete_package(Manager(), 'project_demo', 'synthetic')
    assert calls == ['GET', 'DELETE']


def test_retry_only_selected_member(sharing):
    users, _ = sharing
    members.queue('project_demo', {'members': {}},
                  {'members': {'operator_a': 'editor', 'operator_b': 'editor'}})
    result = members.sync(None, 'project_demo', 'synthetic', username='operator_b')
    assert set(users) == {'operator_b'}
    assert result['pending'] == {'operator_a': 'grant'}
    assert result['results']['operator_b']['status'] == 'synced'
