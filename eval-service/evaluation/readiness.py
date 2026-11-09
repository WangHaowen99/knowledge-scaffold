"""Deployment readiness is separate from process liveness and Judge configuration."""
from .core import Failure


def dependencies(config, transport):
    checks = {}
    for name, base, path, credential in (
        ('auth', config.auth_url, '/health', True),
        ('hub', config.hub_url, '/api/internal/ready', bool(config.hub_token.strip())),
        ('consumer', config.consumer_url, '/ready', bool(config.consumer_token.strip())),
    ):
        ready = False
        if base and credential:
            try:
                options = {'service': config.hub_token} if name == 'hub' else {}
                value = transport.json('GET', base, path, timeout=3, limit=16384, **options)
                ready = isinstance(value, dict) and (
                    value.get('ok') is True or value.get('status') == 'ok' or value.get('ready') is True)
                if name == 'hub':
                    ready = ready and value.get('service') == 'knowledge-hub' and value.get('identity') == 'evaluation'
            except Failure:
                pass
        checks[name] = {'ready': ready, 'code': 'ready' if ready else 'dependency_unavailable'}
    checks['encryption'] = {'ready': bool(config.encryption_key)}
    return {'ok': all(check['ready'] for check in checks.values()), 'service': 'evaluation', 'dependencies': checks}
