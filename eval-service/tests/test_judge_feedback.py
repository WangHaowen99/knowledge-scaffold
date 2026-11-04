"""Model gateway feedback and account display use only synthetic inputs."""
import httpx
import pytest
from contextlib import closing

from evaluation.core import Failure, Transport, digest
from test_evaluation import environment


@pytest.mark.parametrize('upstream,expected_code,expected_status', [
    (400, 'judge_test_request', 422),
    (401, 'judge_test_authentication', 422),
    (403, 'judge_test_permission', 422),
    (404, 'judge_test_not_found', 422),
    (422, 'judge_test_request', 422),
    (429, 'judge_test_rate_limit', 429),
    (500, 'judge_test_service', 503),
    (502, 'judge_test_service', 503),
    (504, 'judge_test_timeout', 504),
])
def test_model_test_explains_http_failure_without_echo_or_expiring_user(environment, upstream, expected_code, expected_status):
    client, app, deps, cfg = environment
    deps.roles['synthetic-user-a'] = 'admin'
    original = deps.json

    def dependency(method, base, path, **kwargs):
        if path == '/chat/completions':
            raise Failure('dependency_rejected', 'Synthetic private diagnostic: synthetic-invalid-secret',
                          upstream if upstream in (401, 403, 404, 422, 429) else 503,
                          'dependency', upstream_status=upstream)
        return original(method, base, path, **kwargs)

    deps.json = dependency
    response = client.post('/evaluation/api/judge-settings/test', json={
        'base_url': 'https://service.example.net/v1', 'model': 'judge_demo', 'api_key': 'synthetic-invalid-key',
    })
    assert response.status_code == expected_status
    assert response.json()['code'] == expected_code
    assert '依赖服务拒绝请求' not in response.text
    assert 'synthetic-invalid-secret' not in response.text
    assert 'service.example.net' not in response.text
    assert client.get('/evaluation/api/session').status_code == 200


@pytest.mark.parametrize('failure_code,expected', [
    ('dependency_timeout', 'judge_test_timeout'),
    ('dependency_unavailable', 'judge_test_connection'),
    ('judge_protocol', 'judge_test_protocol'),
    ('invalid_response', 'judge_test_protocol'),
])
def test_model_test_explains_transport_and_protocol_errors(environment, failure_code, expected):
    client, app, deps, cfg = environment
    deps.roles['synthetic-user-a'] = 'admin'
    original = deps.json

    def dependency(method, base, path, **kwargs):
        if path == '/chat/completions':
            raise Failure(failure_code, 'Synthetic diagnostic', 503)
        return original(method, base, path, **kwargs)

    deps.json = dependency
    response = client.post('/evaluation/api/judge-settings/test', json={
        'base_url': 'https://service.example.net/v1', 'model': 'judge_demo', 'api_key': 'synthetic-invalid-key',
    })
    assert response.json()['code'] == expected
    assert 'Synthetic diagnostic' not in response.text


def test_session_exposes_verified_username_and_role_without_persisting_display_identity(environment):
    client, app, deps, cfg = environment
    original = deps.json

    def dependency(method, base, path, **kwargs):
        value = original(method, base, path, **kwargs)
        if path == '/userinfo':
            return {**value, 'sub': 'operator_a', 'role': 'admin'}
        return value

    deps.json = dependency
    response = client.get('/evaluation/api/session')
    assert response.status_code == 200
    data = response.json()
    assert data['user'] == {'username': 'operator_a', 'role': 'admin'}
    assert data['subject'] == digest([cfg.issuer, 'operator_a'])
    assert response.headers['cache-control'] == 'no-store'
    assert all('operator_a' not in row[0] for row in app.state.store.db.execute('SELECT body FROM records'))


def test_transport_preserves_http_status_privately_and_distinguishes_timeout(monkeypatch):
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def stream(self, *args, **kwargs):
            return closing(httpx.Response(400, content=b'Synthetic private response', request=httpx.Request('POST', 'https://service.example.net')))

    monkeypatch.setattr(httpx, 'Client', Client)
    with pytest.raises(Failure) as error:
        Transport().request('POST', 'https://service.example.net', '/chat/completions')
    assert error.value.status == 503
    assert error.value.upstream_status == 400
    assert 'upstream_status' not in error.value.payload()

    def timeout(*args, **kwargs):
        raise httpx.ReadTimeout('Synthetic timeout diagnostic')
    monkeypatch.setattr(Client, 'stream', timeout)
    with pytest.raises(Failure) as error:
        Transport().request('POST', 'https://service.example.net', '/chat/completions')
    assert error.value.code == 'dependency_timeout'
    assert error.value.status == 504
