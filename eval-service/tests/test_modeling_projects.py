"""Modeling project names are a display-only lookup; all identities and names are synthetic."""
from urllib.parse import parse_qs, urlsplit

from evaluation.core import Failure
from test_evaluation import environment

URL = '/evaluation/api/modeling-projects'


def modeling(deps, handler):
    original = deps.json

    def dependency(method, base, path, **kwargs):
        if path.startswith('/api/hub/package-projects'):
            return handler(base, path, kwargs)
        return original(method, base, path, **kwargs)

    deps.json = dependency


def test_names_come_from_modeling_with_the_callers_login(environment):
    client, app, deps, cfg = environment
    cfg.modeling_url = 'http://modeling.example.com:8100'
    calls = []

    def handler(base, path, kwargs):
        calls.append((base, parse_qs(urlsplit(path).query)['ontology_id'], kwargs['token']))
        return {'items': [
            {'ontology_id': 'package_demo', 'projects': ['project_alpha', 'x' * 300, 3], 'total': 7},
            {'ontology_id': 'package_other', 'projects': ['project_beta'], 'total': 1},
        ]}

    modeling(deps, handler)
    response = client.get(URL + '?ontology_id=package_demo&ontology_id=package_demo')
    assert response.status_code == 200, response.text
    assert response.json() == {'available': True, 'items': [
        {'ontology_id': 'package_demo', 'projects': ['project_alpha', 'x' * 128], 'total': 7}]}
    assert calls == [('http://modeling.example.com:8100', ['package_demo'], 'synthetic-user-a')]


def test_modeling_failures_only_hide_names(environment):
    client, app, deps, cfg = environment
    assert client.get(URL + '?ontology_id=package_demo').json() == {'available': False, 'items': []}
    cfg.modeling_url = 'http://modeling.example.com:8100'
    for failure in (Failure('dependency_rejected', 'Synthetic rejection', 401, 'dependency'),
                    Failure('dependency_unavailable', 'Synthetic outage', 503, 'dependency')):
        def handler(base, path, kwargs, failure=failure):
            raise failure
        modeling(deps, handler)
        response = client.get(URL + '?ontology_id=package_demo')
        # A rejected modeling login must not be mistaken for an expired evaluation login.
        assert response.status_code == 200
        assert response.json() == {'available': False, 'items': []}


def test_lookup_requires_login_and_bounded_package_ids(environment):
    client, app, deps, cfg = environment
    cfg.modeling_url = 'http://modeling.example.com:8100'
    modeling(deps, lambda base, path, kwargs: {'items': []})
    assert client.get(URL + '?ontology_id=package_demo', headers={'Authorization': ''}).status_code == 401
    assert client.get(URL + '?ontology_id=bad/id').status_code == 422
    many = '&'.join(f'ontology_id=package_{i}' for i in range(21))
    assert client.get(URL + '?' + many).status_code == 422
