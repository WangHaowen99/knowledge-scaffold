"""Retired project routes must never regain an authorization bypass."""
from test_packages import api


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
