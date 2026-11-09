"""Deployment checks use loopback HTTP servers and synthetic credentials only."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import threading

import pytest

from app.services import deployment_readiness as readiness


@contextmanager
def dependency(payload, *, status=200, secret=''):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            authorized = not secret or self.headers.get('X-Hub-Service-Token') == secret
            self.send_response(status if authorized else 401)
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_missing_hub_configuration_is_explicit(monkeypatch):
    monkeypatch.delenv('ONTO_HUB_URL', raising=False)
    monkeypatch.delenv('ONTO_HUB_SERVICE_TOKEN', raising=False)
    monkeypatch.setattr(readiness.config, 'EVAL_URL', '')
    value = readiness.dependencies()
    assert value['ok'] is False
    assert value['package_publication']['code'] == 'hub_unconfigured'
    assert value['evaluation']['ready'] is False


@pytest.mark.parametrize('identity,valid_secret,expected', [
    ('modeling', True, True), ('evaluation', True, False), ('modeling', False, False),
])
def test_hub_readiness_checks_network_and_service_identity(monkeypatch, identity, valid_secret, expected):
    secret = secrets.token_hex(32)
    with dependency({'ok': True, 'service': 'knowledge-hub', 'identity': identity}, secret=secret) as url:
        monkeypatch.setenv('ONTO_HUB_URL', url)
        monkeypatch.setenv('ONTO_HUB_SERVICE_TOKEN', secret if valid_secret else secrets.token_hex(32))
        monkeypatch.setattr(readiness.config, 'EVAL_URL', '')
        value = readiness.dependencies()
    assert value['ok'] is expected
    assert value['package_publication']['ready'] is expected
    assert secret not in json.dumps(value)
    assert url not in json.dumps(value)


def test_unreachable_hub_is_reported_without_network_details(monkeypatch):
    monkeypatch.setenv('ONTO_HUB_URL', 'http://127.0.0.1:0')
    monkeypatch.setenv('ONTO_HUB_SERVICE_TOKEN', secrets.token_hex(32))
    monkeypatch.setattr(readiness.config, 'EVAL_URL', '')
    value = readiness.dependencies()
    assert value['package_publication']['code'] == 'hub_unavailable'
    assert '127.0.0.1' not in json.dumps(value)


@pytest.mark.parametrize('payload,status', [
    ({'ok': True, 'service': 'evaluation'}, 200),
    ({'ok': False, 'service': 'evaluation'}, 503),
])
def test_evaluation_dependency_check_uses_readiness(payload, status):
    with dependency(payload, status=status) as url:
        assert readiness.probe(url, '/evaluation/api/ready', 'evaluation') is (status == 200)
