# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Evaluation provisioning uses real storage and exclusively synthetic identities."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from api.evaluation_workspaces import provision_evaluation_workspace
from api.workspace_acl_service import WorkspaceIdentityDirectoryUnavailableError
from workspace.workspaces import (
    WorkspaceAuthorizationError,
    create_workspace,
    get_default_workspace_id,
    get_workspace,
    list_workspaces,
    update_workspace,
)


@pytest.fixture
def directory():
    return SimpleNamespace(list_users=lambda: [{'username': 'operator_a'}, {'username': 'operator_b'}])


def test_first_run_creates_private_workspace_without_a_default(directory):
    assert list_workspaces() == []
    result = provision_evaluation_workspace(directory, owner='operator_a', run_id='a' * 32)
    assert result['created'] is True
    workspace = get_workspace(result['workspace']['id'])
    assert workspace['owner'] == 'operator_a'
    assert workspace['members'] == []
    assert get_default_workspace_id() is None
    repeated = provision_evaluation_workspace(directory, owner='operator_a', run_id='a' * 32)
    assert repeated['created'] is False
    assert repeated['workspace']['id'] == workspace['id']
    assert len(list_workspaces()) == 1


def test_runs_never_reuse_default_personal_or_modeling_spaces(directory, monkeypatch):
    create_workspace('Synthetic default', workspace_id='workspace_demo', owner='operator_b', set_as_default=True)
    create_workspace('Synthetic personal', workspace_id='personal_demo', owner='operator_a')
    create_workspace('Synthetic modeling', workspace_id='mw-shadow-project-alpha', owner='operator_a')
    monkeypatch.setenv('WORKSPACE_MAX_COUNT', '1')
    first = provision_evaluation_workspace(directory, owner='operator_a', run_id='a' * 32)
    second = provision_evaluation_workspace(directory, owner='operator_a', run_id='b' * 32)
    other = provision_evaluation_workspace(directory, owner='operator_b', run_id='a' * 32)
    assert len({first['workspace']['id'], second['workspace']['id'], other['workspace']['id']}) == 3
    assert all(item['workspace']['id'].startswith('eval-') for item in (first, second, other))
    assert get_default_workspace_id() == 'workspace_demo'
    assert get_workspace('personal_demo')['owner'] == 'operator_a'
    assert get_workspace('mw-shadow-project-alpha')['owner'] == 'operator_a'


def test_concurrent_retries_only_create_one_run_workspace(directory):
    def provision(_):
        return provision_evaluation_workspace(directory, owner='operator_a', run_id='a' * 32)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(provision, range(8)))
    assert sum(item['created'] for item in results) == 1
    assert len({item['workspace']['id'] for item in results}) == 1
    assert len(list_workspaces()) == 1


@pytest.mark.parametrize('change', [{'owner': 'operator_b'}, {'members': ['operator_b']}])
def test_retry_refuses_foreign_or_shared_run_workspace(directory, change):
    result = provision_evaluation_workspace(directory, owner='operator_a', run_id='a' * 32)
    workspace_id = result['workspace']['id']
    update_workspace(workspace_id, **change)
    with pytest.raises(WorkspaceAuthorizationError):
        provision_evaluation_workspace(directory, owner='operator_a', run_id='a' * 32)
    assert len(list_workspaces()) == 1
    assert get_workspace(workspace_id)[next(iter(change))] == next(iter(change.values()))


def test_create_race_recovers_only_the_same_run_space(directory, monkeypatch):
    from api import evaluation_workspaces
    create = evaluation_workspaces.create_workspace_with_validated_owner
    def concurrent_create(*args, **kwargs):
        create(*args, **kwargs)
        raise ValueError('Synthetic concurrent creation')
    monkeypatch.setattr(evaluation_workspaces, 'create_workspace_with_validated_owner', concurrent_create)
    result = provision_evaluation_workspace(directory, owner='operator_a', run_id='a' * 32)
    assert result['created'] is False
    assert result['workspace']['owner'] == 'operator_a'
    assert len(list_workspaces()) == 1


def test_identity_directory_failure_does_not_create_a_workspace():
    with pytest.raises(WorkspaceIdentityDirectoryUnavailableError):
        provision_evaluation_workspace(None, owner='operator_a', run_id='a' * 32)
    assert list_workspaces() == []
