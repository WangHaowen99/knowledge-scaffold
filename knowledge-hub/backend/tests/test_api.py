"""Retired project routes must never regain an authorization bypass."""
from test_packages import api
import secrets

import pytest


def test_project_resource_routes_are_removed(api):
    client, _ = api
    for method, path in [
        ('get', '/api/projects/project_alpha/resources/current'),
        ('put', '/api/projects/project_alpha/resources/current'),
        ('get', '/api/projects/project_alpha/resources/current/file_demo'),
        ('delete', '/api/internal/projects/project_alpha/artifacts'),
    ]:
        assert getattr(client, method)(path).status_code == 404


def test_new_resource_pagination_is_bounded(api):
    client, _ = api
    assert client.get('/api/packages/ontology_demo/resources/current?limit=101').status_code == 422


@pytest.mark.parametrize('identity,key', [
    ('modeling', 'HUB_SERVICE_TOKEN'), ('evaluation', 'HUB_EVALUATION_SERVICE_TOKEN'),
])
def test_readiness_checks_service_identity_without_a_user_token(api, monkeypatch, identity, key):
    client, _ = api
    secret = secrets.token_hex(32)
    monkeypatch.setenv(key, secret)
    result = client.get('/api/internal/ready', headers={'X-Hub-Service-Token': secret})
    assert result.status_code == 200
    assert result.json() == {'ok': True, 'service': 'knowledge-hub', 'identity': identity}
    assert secret not in result.text
    assert client.get('/api/internal/ready').status_code == 401


def test_readiness_rejects_ambiguous_service_credentials(api, monkeypatch):
    client, _ = api
    secret = secrets.token_hex(32)
    monkeypatch.setenv('HUB_SERVICE_TOKEN', secret)
    monkeypatch.setenv('HUB_EVALUATION_SERVICE_TOKEN', secret)
    assert client.get('/api/internal/ready', headers={'X-Hub-Service-Token': secret}).status_code == 503
