"""Lock-free listing reads; each JSON is published by atomic replacement.

Concurrent deletion may remove a record before it is opened. Only that absence
is tolerated; unreadable or malformed records must not become empty results.
"""
import json

from .package_contract import PackageError
from .package_store import PackageStore, public


def read_record(path):
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    if not isinstance(value, dict):
        raise ValueError('Invalid stored record')
    return value


def metadata(path):
    value = read_record(path)
    if value is None:
        return None
    if (not isinstance(value.get('ontology_id'), str)
            or value.get('state') not in ('active', 'deleted')
            or not isinstance(value.get('authorized_users'), dict)):
        raise ValueError('Invalid package metadata')
    return value


class PackageQueries:
    def __init__(self, store):
        self.store = PackageStore(store)
        self.root = store.root

    def owned(self, package_id, subject):
        value = metadata(self.store.root_of(package_id) / 'metadata.json')
        if value is None or subject not in value['authorized_users']:
            raise PackageError('not_found', 'Package not found', 404, 'authorization')
        if value['ontology_id'] != package_id:
            raise ValueError('Package metadata mismatch')
        if value['state'] == 'deleted':
            raise PackageError('package_deleted', 'Package has been deleted', 410)
        return value

    def committed(self):
        for path in (self.root / 'publish-requests').glob('*.json'):
            row = read_record(path)
            if row is None:
                continue
            if (not isinstance(row.get('ontology_id'), str)
                    or row.get('status') not in ('pending', 'uploaded', 'committing', 'committed', 'expired')):
                raise ValueError('Invalid publication record')
            if row['status'] != 'committed' or row.get('existing'):
                continue
            if (row.get('kind') not in ('release', 'evaluation_snapshot')
                    or any(not isinstance(row.get(key), str) for key in
                           ('release_id', 'committed_at', 'version'))):
                raise ValueError('Invalid committed record')
            yield public(row)

    def packages(self, subject, offset=0, limit=20):
        visible = set()
        for path in (self.root / 'packages').glob('*/metadata.json'):
            meta = metadata(path)
            if meta is None:
                continue
            if meta['ontology_id'] != path.parent.name:
                raise ValueError('Package metadata mismatch')
            if meta['state'] == 'active' and subject in meta['authorized_users']:
                visible.add(meta['ontology_id'])
        latest = {}
        for row in self.committed():
            package_id = row['ontology_id']
            if package_id not in visible:
                continue
            rank = (row['kind'] == 'release', row['committed_at'], row['release_id'])
            if package_id not in latest or rank > latest[package_id][0]:
                latest[package_id] = (rank, row)
        rows = [dict(row, latest_release_version=row['version']) for _, row in latest.values()]
        rows.sort(key=lambda r: (r['committed_at'], r['ontology_id']), reverse=True)
        return self.store.page(rows, offset, limit)

    def listing(self, package_id, subject, kind='release', offset=0, limit=20):
        self.owned(package_id, subject)
        rows = [row for row in self.committed()
                if row['ontology_id'] == package_id and row['kind'] == kind]
        # Deletion writes its tombstone before removing the publication records.
        self.owned(package_id, subject)
        rows.sort(key=lambda r: (r['committed_at'], r['release_id']), reverse=True)
        return self.store.page(rows, offset, limit)
