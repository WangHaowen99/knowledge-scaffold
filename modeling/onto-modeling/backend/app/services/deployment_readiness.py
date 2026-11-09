"""Bounded deployment checks; never return dependency addresses or credentials."""
import http.client
import json
import os
from urllib.parse import urlsplit

from .. import config


def probe(base, path, service, *, secret='', identity='', timeout=3):
    connection = None
    try:
        url = urlsplit(base)
        if (url.scheme not in ('http', 'https') or not url.hostname
                or any((url.username, url.password, url.query, url.fragment))):
            return False
        connection_type = http.client.HTTPSConnection if url.scheme == 'https' else http.client.HTTPConnection
        connection = connection_type(url.hostname, url.port, timeout=timeout)
        headers = {'X-Hub-Service-Token': secret} if secret else {}
        connection.request('GET', url.path.rstrip('/') + path, headers=headers)
        response = connection.getresponse()
        raw = response.read(16385)
        if response.status != 200 or len(raw) > 16384:
            return False
        value = json.loads(raw)
        return (isinstance(value, dict) and value.get('service') == service
                and (value.get('ok') is True or value.get('status') == 'ok')
                and (not identity or value.get('identity') == identity))
    except (OSError, http.client.HTTPException, ValueError):
        return False
    finally:
        if connection is not None:
            connection.close()


def dependencies():
    hub_configured = bool(os.environ.get('ONTO_HUB_URL', '').strip()
                          and os.environ.get('ONTO_HUB_SERVICE_TOKEN', '').strip())
    hub_ready = hub_configured and probe(
        os.environ.get('ONTO_HUB_URL', ''), '/api/internal/ready', 'knowledge-hub',
        secret=os.environ.get('ONTO_HUB_SERVICE_TOKEN', ''), identity='modeling')
    evaluation_ready = probe(config.EVAL_URL, '/evaluation/api/ready', 'evaluation', timeout=10)
    return {
        'ok': hub_ready,
        'package_publication': {
            'ready': hub_ready,
            'code': 'ready' if hub_ready else 'hub_unavailable' if hub_configured else 'hub_unconfigured',
            'message': '知识包 Hub 已就绪' if hub_ready else
                       '知识包发布不可用：请部署知识包 Hub，并检查共享网络和 ONTO_HUB_* 配置',
        },
        'evaluation': {
            'ready': evaluation_ready,
            'code': 'ready' if evaluation_ready else 'evaluation_unavailable',
            'message': '评测服务依赖已就绪' if evaluation_ready else
                       '评测不可用：请部署独立评测服务并检查其依赖配置',
        },
    }
