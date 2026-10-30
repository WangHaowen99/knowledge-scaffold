"""Evaluation workspace provisioning failures from consumer are explained; all identities are synthetic."""
from evaluation.core import Failure
from test_evaluation import environment, preparation, run, wait

PATH = '/agentic/internal/evaluation/workspaces'


def consumer_answers(deps, handler):
    original = deps.json

    def dependency(method, base, path, **kwargs):
        return handler() if path == PATH else original(method, base, path, **kwargs)

    deps.json = dependency


def test_missing_consumer_endpoint_asks_for_a_consumer_redeploy(environment):
    client, app, deps, cfg = environment

    def missing():
        raise Failure('dependency_rejected', 'Synthetic not found', 404, 'dependency', upstream_status=404)

    consumer_answers(deps, missing)
    result = wait(client, run(client, preparation(client))['id'])
    assert result['status'] == 'interrupted', result
    assert result['error']['code'] == 'consumer_workspace_rejected'
    assert '重新部署 consumer' in result['error']['message']
    assert 'Synthetic not found' not in result['error']['message']


def test_unexpected_workspace_identifier_is_rejected(environment):
    client, app, deps, cfg = environment
    consumer_answers(deps, lambda: {'workspace': {'id': 'workspace_demo'}})
    result = wait(client, run(client, preparation(client))['id'])
    assert result['status'] == 'interrupted', result
    assert result['error']['code'] == 'consumer_workspace_protocol'
    # No question was sent to an unverified workspace.
    assert deps.submitted_models == []
