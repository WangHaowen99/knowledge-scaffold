# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""知识包共享授权与删除项目时一并删除 Hub 包的故障场景（develop !590 的 test_hub_members，ADR-0098）。

develop 的「建模者（members 里 role=editor）」在本分支是知识包的共享用户；其余断言照原样。全部为合成数据。
"""
import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.main import app
from app.services import auth, hub_members as members, hub_packages, knowledge_hub
from app.services.workspace import WorkspaceError, WorkspaceManager
from tests import synthetic


@pytest.fixture
def sharing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(auth, 'ISSUER', 'https://example.com')
    monkeypatch.setattr(hub_packages, 'association',
                        lambda pid, subject=None: {'ontology_id': hub_packages.ontology_id(pid)})
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


def _preset_shared(pid, names):
    """首次发布前已设好的共享用户（还没有 Hub 包，不排同步）。"""
    knowledge_hub.save(members.state_path(pid), {'shared': names})


class _Owner:
    def __init__(self, owner='operator_a'):
        self.owner = owner

    def get_project(self, pid):
        return {'owner': self.owner}

    def project_root(self, pid):
        return None


def test_incremental_retry_supersedes_failed_grant(sharing):
    users, calls = sharing
    members.queue('project_demo', [], ['operator_b'], 'operator_a')
    assert members.sync(None, 'project_demo', '')['status'] == 'failed'
    members.queue('project_demo', ['operator_b'], [], 'operator_a')
    assert members.sync(None, 'project_demo', synthetic.token('access'))['status'] == 'synced'
    assert not users
    assert not any(method == 'PUT' for method, _ in calls)


def test_initialization_and_ordinary_publish_do_not_restore_external_revoke(sharing):
    users, calls = sharing
    _preset_shared('project_demo', ['operator_b'])
    manager = _Owner()
    token = synthetic.token('access')
    assert members.after_publish(manager, 'project_demo', token, True)['status'] == 'synced'
    assert set(users) == {'operator_a', 'operator_b'}
    users.pop('operator_b')
    calls.clear()
    members.after_publish(manager, 'project_demo', token, False)
    members.after_publish(manager, 'project_demo', token, True)
    assert not calls
    assert 'operator_b' not in users


def test_removed_shared_user_revokes_independent_grant(sharing):
    """develop 是「移除的只读评审也撤权」：本分支移出共享名单就撤，包括其他渠道授予的权限。"""
    users, _ = sharing
    users['operator_b'] = {'issuer': auth.ISSUER, 'sub': 'operator_b', 'subject_id': 'operator_b'}
    members.queue('project_demo', ['operator_b'], [], 'operator_a')
    assert members.sync(None, 'project_demo', synthetic.token('access'))['status'] == 'synced'
    assert not users


def test_delete_failure_keeps_progress_and_retries_same_target(sharing, monkeypatch):
    manager = _Owner()
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
    token = synthetic.token('access')
    with pytest.raises(WorkspaceError):
        members.delete_package(manager, 'project_demo', token)
    assert members.deletion('project_demo')['state'] == 'deleting'
    with pytest.raises(WorkspaceError):
        members.writable('project_demo')
    members.delete_package(manager, 'project_demo', token)
    members.delete_package(manager, 'project_demo', token)
    assert calls == ['GET', 'DELETE', 'DELETE']
    assert members.deletion('project_demo')['state'] == 'deleted'


def test_authorized_users_consumes_all_pages(monkeypatch):
    def call(method, path, token):
        if 'offset=0' in path:
            return {'items': [{'sub': 'operator_a'}], 'next_offset': 1}
        return {'items': [{'sub': 'operator_b'}], 'next_offset': None}
    monkeypatch.setattr(hub_packages, 'call', call)
    assert len(members.authorized('ontology_demo', synthetic.token('access'))) == 2


@pytest.fixture
def project(sharing, tmp_path, monkeypatch):
    manager = WorkspaceManager(tmp_path / 'data', tmp_path / 'assets')
    monkeypatch.setattr(deps, 'workspaces', manager)
    monkeypatch.setattr(auth, 'ENABLED', False)
    meta = manager.create_project('project_demo', owner='operator_a')
    return manager, meta['id']


def test_sharing_route_preserves_local_success_on_hub_failure(project):
    """develop 的成员路由：本地名单已生效，Hub 同步失败只记 failed 与 pending，不回滚。"""
    _, pid = project
    result = TestClient(app).put(f'/api/v1/projects/{pid}/hub/sharing', json={'users': ['operator_b']}).json()
    assert result['users'] == ['operator_b']
    assert result['hub_sync']['status'] == 'failed'
    assert members.status(pid)['pending'] == {'operator_b': 'grant'}
    assert TestClient(app).get(f'/api/v1/projects/{pid}/hub/sharing').json()['users'] == ['operator_b']


def test_project_delete_requires_confirmation_and_preserves_project_on_hub_failure(project, monkeypatch):
    from app.routers import hub_packages as routes

    manager, pid = project
    monkeypatch.setattr(routes, 'identity', lambda request: (synthetic.token('access'), 'synthetic', 'operator_a'))
    client = TestClient(app)
    url = f'/api/v1/projects/{pid}'
    refused = client.request('DELETE', url, json={'confirm_name': 'project_demo', 'delete_hub_package': True})
    assert refused.status_code == 422 and '请确认' in refused.json()['message']

    def fail(*args):
        raise WorkspaceError('Synthetic Hub failure', 503)
    monkeypatch.setattr(members, 'delete_package', fail)
    failed = client.request('DELETE', url, json={'confirm_name': 'project_demo', 'delete_hub_package': True,
                                                 'hub_usage_confirmed': True})
    assert failed.status_code == 503 and 'Synthetic Hub failure' in failed.json()['message']
    assert manager.get_project(pid)['name'] == 'project_demo'


def test_existing_hub_tombstone_still_requires_delete_to_finish_cleanup(sharing, monkeypatch):
    calls = []
    ontology = hub_packages.ontology_id('project_demo')

    def call(method, path, token, **kwargs):
        calls.append(method)
        if method == 'GET':
            raise WorkspaceError('Synthetic deleted package', 410, 'package_deleted')
        return {'ontology_id': ontology, 'state': 'deleted'}
    monkeypatch.setattr(hub_packages, 'call', call)
    members.delete_package(_Owner(), 'project_demo', synthetic.token('access'))
    assert calls == ['GET', 'DELETE']


def test_retry_only_selected_member(sharing):
    users, _ = sharing
    members.queue('project_demo', [], ['operator_a', 'operator_b'])
    result = members.sync(None, 'project_demo', synthetic.token('access'), username='operator_b')
    assert set(users) == {'operator_b'}
    assert result['pending'] == {'operator_a': 'grant'}
    assert result['results']['operator_b']['status'] == 'synced'


# ---- 本分支独有：共享在知识包这一层、换所有者 ----


def test_sharing_before_first_publish_only_records_the_list(sharing, monkeypatch):
    monkeypatch.setattr(hub_packages, 'association', lambda pid, subject=None: None)
    members.set_shared('project_demo', ['operator_b', 'operator_a', 'operator_b'], owner='operator_a')
    assert members.shared_users('project_demo') == ['operator_b']  # 所有者不进名单，去重
    assert members.status('project_demo')['pending'] == {}
    assert members.sync(None, 'project_demo', synthetic.token('access'))['status'] == 'not_published'


def test_owner_change_grants_new_owner_and_keeps_previous_as_shared_user(sharing):
    users, _ = sharing
    _preset_shared('project_demo', ['operator_c'])
    members.owner_changed('project_demo', 'operator_a', 'operator_c')
    assert members.shared_users('project_demo') == ['operator_a']
    assert members.status('project_demo')['pending'] == {'operator_c': 'grant'}
    assert members.sync(None, 'project_demo', synthetic.token('access'))['status'] == 'synced'
    assert set(users) == {'operator_c'}


def test_deleting_package_blocks_publish_and_sharing_writes(sharing, project):
    _, pid = project
    knowledge_hub.save(members.state_path(pid).with_name('deletion.json'), {'state': 'deleting'})
    with pytest.raises(WorkspaceError) as blocked:
        hub_packages.publish(deps.workspaces, pid, 'request_demo', synthetic.token('access'), 'scope_demo')
    assert blocked.value.status == 409
    response = TestClient(app).put(f'/api/v1/projects/{pid}/hub/sharing', json={'users': ['operator_b']})
    assert response.status_code == 409


def test_resolve_hub_member_needs_directory_credentials(monkeypatch):
    monkeypatch.setattr(auth, 'AUTH_URL', '')
    with pytest.raises(auth.AuthError) as missing:
        auth.resolve_hub_member('operator_b')
    assert missing.value.status == 503
    monkeypatch.setattr(auth, 'AUTH_URL', 'https://auth.example.com')
    monkeypatch.setattr(auth, 'ISSUER', 'https://auth.example.com')
    monkeypatch.setattr(config, 'AUTH_ADMIN_TOKEN', synthetic.token('admin'))
    monkeypatch.setattr(auth, '_directory_users', lambda: [{'username': 'operator_b'}])
    assert auth.resolve_hub_member('operator_b') == {'issuer': 'https://auth.example.com', 'sub': 'operator_b'}
    with pytest.raises(auth.AuthError) as unknown:
        auth.resolve_hub_member('operator_x')
    assert unknown.value.status == 422


def test_admin_owner_change_queues_grant_for_new_owner(project):
    """AD-07：管理员换所有者，给新所有者排一次 Hub 授权（管理员不是授权用户时留在 pending），旧所有者留作共享用户。"""
    _, pid = project
    response = TestClient(app).put(f'/api/v1/admin/projects/{pid}/owner', json={'owner': 'operator_c'})
    assert response.status_code == 200, response.text
    assert members.shared_users(pid) == ['operator_a']
    assert members.status(pid)['pending'] == {'operator_c': 'grant'}
