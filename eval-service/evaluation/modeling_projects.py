"""Display-only lookup of the caller's modeling project names for Hub packages.

The evaluation service never stores or accepts modeling project data. Names are fetched from
onto-modeling on demand with the user's own login, and any failure only hides them. One call
covers at most 20 packages with 5 names each, so the result stays below 20 KiB.
"""
import re
from urllib.parse import urlencode

from .core import Failure

PACKAGES = 20
NAMES = 5
PACKAGE_ID = re.compile(r'^[A-Za-z0-9_-]{1,128}$')
UNAVAILABLE = {'available': False, 'items': []}


def linked_projects(config, http, token, package_ids):
    wanted = list(dict.fromkeys(package_ids))
    if not 1 <= len(wanted) <= PACKAGES or not all(PACKAGE_ID.fullmatch(value) for value in wanted):
        raise Failure('invalid_input', f'每次查询 1 到 {PACKAGES} 个有效的 Hub 包标识', 422)
    if not config.modeling_url:
        return UNAVAILABLE
    query = urlencode([('ontology_id', value) for value in wanted])
    try:
        value = http.json('GET', config.modeling_url, '/api/hub/package-projects?' + query, token=token, limit=65536)
    except Failure:
        return UNAVAILABLE
    if not isinstance(value, dict) or not isinstance(value.get('items'), list):
        return UNAVAILABLE
    items = []
    for item in value['items']:
        if not isinstance(item, dict) or item.get('ontology_id') not in wanted:
            continue
        names = [name[:128] for name in item.get('projects') or [] if isinstance(name, str)][:NAMES]
        total = item.get('total') if isinstance(item.get('total'), int) else len(names)
        items.append(dict(ontology_id=item['ontology_id'], projects=names, total=max(total, len(names))))
    return {'available': True, 'items': items}
