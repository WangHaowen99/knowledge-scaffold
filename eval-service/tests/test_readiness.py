"""Readiness checks use fully synthetic services, settings and credentials."""
import json
import secrets

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest

from evaluation.api import create_app
from evaluation.core import Config, Failure
from evaluation.readiness import dependencies


class Dependencies:
    def __init__(self, failed='', identity='evaluation'):
        self.failed, self.identity = failed, identity
        self.calls = []

    def json(self, method, base, path, **options):
        self.calls.append((method, base, path, options))
        assert options['timeout'] == 3
        assert options['limit'] == 16384
        if base == self.failed:
            raise Failure('dependency_unavailable', 'Synthetic failure', 503)
        if path == '/api/internal/ready':
            return {'ok': True, 'service': 'knowledge-hub', 'identity': self.identity}
        if path == '/ready':
            return {'ready': True}
        return {'status': 'ok'}


def configuration(root):
    return Config(root, auth_url='https://auth.example.com', hub_url='https://hub.example.com',
                  hub_token=secrets.token_hex(32), consumer_url='https://consumer.example.com',
                  consumer_token=secrets.token_hex(32), encryption_key=Fernet.generate_key().decode())


def test_readiness_does_not_require_judge_configuration(tmp_path):
    config = configuration(tmp_path)
    transport = Dependencies()
    value = dependencies(config, transport)
    assert value['ok'] is True
    assert transport.calls[1][3]['service'] == config.hub_token
    text = json.dumps(value)
    for private in (config.hub_token, config.consumer_token, config.encryption_key, config.hub_url):
        assert private not in text


@pytest.mark.parametrize('failed', ['auth_url', 'hub_url', 'consumer_url'])
def test_any_unreachable_dependency_blocks_readiness(tmp_path, failed):
    config = configuration(tmp_path)
    assert dependencies(config, Dependencies(failed=getattr(config, failed)))['ok'] is False


@pytest.mark.parametrize('missing', ['hub_token', 'consumer_token', 'encryption_key'])
def test_missing_required_configuration_blocks_readiness(tmp_path, missing):
    config = configuration(tmp_path)
    setattr(config, missing, '')
    assert dependencies(config, Dependencies())['ok'] is False


def test_hub_modeling_identity_cannot_be_used_for_evaluation(tmp_path):
    assert dependencies(configuration(tmp_path), Dependencies(identity='modeling'))['ok'] is False


def test_readiness_endpoint_distinguishes_liveness_from_dependencies(tmp_path):
    config = configuration(tmp_path)
    with TestClient(create_app(config, Dependencies(failed=config.hub_url))) as client:
        assert client.get('/evaluation/api/health').status_code == 200
        result = client.get('/evaluation/api/ready')
        assert result.status_code == 503
        assert result.json()['dependencies']['hub']['ready'] is False
