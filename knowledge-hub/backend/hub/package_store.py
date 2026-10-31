"""User-owned packages with durable request intents and immutable ZIP archives.

All entry points run under the shared Store's global cross-process lock. A
request is the recovery journal; only a durable committing intent can publish.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from .package_contract import PackageError, PublishBody, check_capability
from .package_validation import validate_derivation
from .storage import atomic, digest, identifier, read_json, sync_dir, write_json


def now():
    return datetime.now(timezone.utc)


def request_key(subject, service, request_id):
    identifier(request_id)
    return hashlib.sha256(json.dumps([subject, service, request_id]).encode()).hexdigest()


PUBLIC = ('request_id', 'hub_package_id', 'release_id', 'manifest_id', 'version', 'kind',
          'source_service', 'change_type', 'base_release_id', 'base_digest', 'status',
          'package_digest', 'size', 'created_at', 'expires_at', 'committed_at')
SNAPSHOT = re.compile(r'^(?P<root>.+)-eval\.(?P<number>[0-9]{1,9})$')


def snapshot_version(base_version, occupied):
    """Evaluation snapshots are named <base version>-eval.<n>, outside the formal 0.1.N sequence.

    n counts the snapshots of that base version. A snapshot of a snapshot keeps the original base,
    and a snapshot without a base release is numbered against 0.1.0, before the first release.
    """
    match = SNAPSHOT.match(base_version or '')
    root = match.group('root') if match else (base_version or '0.1.0')
    numbers = [int(found.group('number')) for found in map(SNAPSHOT.match, occupied)
               if found and found.group('root') == root]
    return f'{root}-eval.{max(numbers, default=0) + 1}'


def public(row):
    result = {key: row[key] for key in PUBLIC if key in row and row[key] is not None}
    if result.get('status') == 'committing':
        result['status'] = 'uploaded'
    return result


class PackageStore:
    def __init__(self, store):
        self.io = store
        self.root = store.root

    def requests(self):
        return [read_json(path) for path in (self.root / 'publish-requests').glob('*.json')]

    def root_of(self, package_id):
        return self.root / 'packages' / identifier(package_id)

    def journal(self, row):
        return self.root / 'publish-requests' / (row['internal_request_id'] + '.json')

    def save(self, row):
        write_json(self.journal(row), row)

    def pending(self, row):
        return self.root_of(row['hub_package_id']) / 'pending' / row['internal_request_id']

    def archived(self, row):
        return self.root_of(row['hub_package_id']) / (
            'releases' if row['kind'] == 'release' else 'snapshots') / row['release_id']

    def ensure_metadata(self, row):
        root = self.root_of(row['hub_package_id'])
        meta = read_json(root / 'metadata.json')
        identity = {k: row[k] for k in ('hub_package_id', 'owner_subject', 'manifest_id')}
        if meta and any(meta.get(k) != v for k, v in identity.items()):
            raise PackageError('archive_integrity', 'Package identity mismatch', 503, 'recovery')
        if not meta:
            write_json(root / 'metadata.json', dict(identity, state='active', created_at=row['created_at']))
        if not (root / '.git').exists():
            self.io.git(root, 'init', '-q')
        atomic(root / '.gitignore', b'pending/\nmetadata.json\n.write-*\n')

    def owned(self, package_id, subject):
        meta = read_json(self.root_of(package_id) / 'metadata.json')
        if not meta or meta.get('owner_subject') != subject:
            raise PackageError('not_found', 'Package not found', 404, 'authorization')
        if meta.get('state') != 'active':
            raise PackageError('package_unavailable', 'Package is unavailable', 410)
        return meta

    def get_request(self, request_id, identity):
        key = request_key(identity.subject, identity.service, request_id)
        row = read_json(self.root / 'publish-requests' / (key + '.json'))
        if not row:
            raise PackageError('not_found', 'Publication request not found', 404)
        self.ensure_metadata(row)
        self.owned(row['hub_package_id'], identity.subject)
        check_capability(identity.service, row)
        self.recover_request(row)
        return row

    def reserve(self, body, identity):
        payload = PublishBody.model_validate(body).model_dump()
        check_capability(identity.service, payload)
        key = request_key(identity.subject, identity.service, payload['request_id'])
        existing = read_json(self.root / 'publish-requests' / (key + '.json'))
        if existing:
            if existing['input'] != payload:
                raise PackageError('request_conflict', 'Request inputs changed', 409)
            row = self.get_request(payload['request_id'], identity)
            self.require_live(row)
            return public(row), False
        package_id = payload['hub_package_id']
        if package_id:
            meta = self.owned(package_id, identity.subject)
            if meta['manifest_id'] != payload['manifest_id']:
                raise PackageError('identity_conflict', 'Manifest identity changed', 409)
        else:
            package_id = uuid.uuid4().hex
        base = None
        if payload['base_release_id']:
            base, _ = self.release(package_id, payload['base_release_id'], identity.subject)
            if base['package_digest'] != payload['base_digest']:
                raise PackageError('base_conflict', 'Base package digest mismatch', 409)
        occupied = {r['version'] for r in self.requests() if r['hub_package_id'] == package_id}
        version = payload['version']
        if version:
            if version in occupied:
                raise PackageError('version_conflict', 'Package version already reserved', 409)
        elif payload['kind'] == 'evaluation_snapshot':
            version = snapshot_version(base['version'] if base else '', occupied)
        else:
            numbers = [int(v.split('.')[2]) for v in occupied if re.fullmatch(r'0\.1\.[0-9]{1,12}', v)]
            version = f'0.1.{max(numbers, default=0) + 1}'
            while version in occupied:
                version = f'0.1.{int(version.split(".")[2]) + 1}'
        timestamp = now()
        row = dict(payload, hub_package_id=package_id, version=version, release_id=uuid.uuid4().hex,
                   input=payload, internal_request_id=key, owner_subject=identity.subject,
                   source_service=identity.service, status='pending', created_at=timestamp.isoformat(),
                   expires_at=(timestamp + timedelta(seconds=self.io.pending_ttl)).isoformat())
        # Persist allocation before metadata: a crash cannot allocate another package
        # or reuse a version. Recovery recreates metadata from this authoritative intent.
        self.save(row)
        self.ensure_metadata(row)
        return public(row), True

    def require_live(self, row):
        if row['status'] == 'expired':
            raise PackageError('request_expired', 'Publication request expired', 410)

    def validate(self, row, path):
        if digest(path) != row['package_digest']:
            raise PackageError('digest_conflict', 'Package digest mismatch', 409)
        base, base_path = None, None
        if row.get('base_release_id'):
            base, base_path = self.release(row['hub_package_id'], row['base_release_id'], row['owner_subject'])
            if base['package_digest'] != row['base_digest']:
                raise PackageError('base_conflict', 'Base package digest mismatch', 409)
        validate_derivation(self.io, path, row, base_path, base)

    def upload(self, request_id, identity, stream, expected_digest):
        row = self.get_request(request_id, identity)
        self.require_live(row)
        folder = self.pending(row)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / 'upload.tmp'
        try:
            self.io.copy_upload(stream, path)
            actual = digest(path)
            if actual != expected_digest:
                raise PackageError('digest_conflict', 'Package digest mismatch', 409)
            if row.get('package_digest'):
                if row['package_digest'] != actual:
                    raise PackageError('content_conflict', 'Publication content changed', 409)
                if row['status'] == 'committed':
                    return public(row)
            if row['change_type'] == 'external_import' and actual != row['input_digest']:
                raise PackageError('input_conflict', 'External input digest mismatch', 409)
            updated = dict(row, package_digest=actual, size=path.stat().st_size)
            self.validate(updated, path)
            os.replace(path, folder / 'package.zip')
            sync_dir(folder)
            updated['status'] = 'uploaded'
            self.save(updated)
            return public(updated)
        finally:
            path.unlink(missing_ok=True)

    def commit(self, request_id, identity):
        row = self.get_request(request_id, identity)
        self.require_live(row)
        if row['status'] == 'committed':
            return public(row)
        if row['status'] != 'uploaded':
            raise PackageError('upload_incomplete', 'Package upload incomplete', 409, 'commit')
        self.validate(row, self.pending(row) / 'package.zip')
        # The route has freshly authorized this actor. This durable decision is
        # the only state recovery is allowed to finish without a live user token.
        row.update(status='committing', committed_at=now().isoformat())
        self.save(row)
        self.finish(row)
        return public(row)

    def finish(self, row):
        target = self.archived(row)
        if not target.exists():
            self.validate(row, self.pending(row) / 'package.zip')
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(self.pending(row), target)
            sync_dir(target.parent)
        self.validate(row, target / 'package.zip')
        receipt = public(dict(row, status='committed'))
        write_json(target / 'receipt.json', receipt)
        root = self.root_of(row['hub_package_id'])
        self.io.git(root, 'add', '--', '.gitignore', target.relative_to(root).as_posix())
        if self.io.git(root, 'diff', '--cached', '--name-only'):
            self.io.git(root, 'commit', '-q', '-m', 'Archive knowledge package version')
        row['status'] = 'committed'
        self.save(row)

    def recover_request(self, row):
        if row['status'] == 'committing':
            self.finish(row)
        elif row['status'] in ('pending', 'uploaded') and now() >= datetime.fromisoformat(row['expires_at']):
            row['status'] = 'expired'
            self.save(row)
        if row['status'] == 'expired':
            for name in ('package.zip', 'upload.tmp'):
                (self.pending(row) / name).unlink(missing_ok=True)

    def release(self, package_id, release_id, subject):
        self.owned(package_id, subject)
        identifier(release_id)
        row = next((r for r in self.requests() if r['hub_package_id'] == package_id
                    and r['release_id'] == release_id and r['status'] == 'committed'), None)
        if not row:
            raise PackageError('not_found', 'Version not found', 404)
        path = self.archived(row) / 'package.zip'
        if not path.is_file() or digest(path) != row['package_digest']:
            raise PackageError('archive_integrity', 'Archive integrity failure', 503, 'download')
        return public(row), path

    def listing(self, package_id, subject, kind='release', offset=0, limit=20):
        self.owned(package_id, subject)
        rows = [public(r) for r in self.requests() if r['hub_package_id'] == package_id
                and r['status'] == 'committed' and r['kind'] == kind]
        rows.sort(key=lambda r: (r['committed_at'], r['release_id']), reverse=True)
        return self.page(rows, offset, limit)

    def packages(self, subject, offset=0, limit=20):
        visible = {}
        for row in self.requests():
            if row['owner_subject'] == subject and row['status'] == 'committed':
                meta = self.owned(row['hub_package_id'], subject)
                visible[row['hub_package_id']] = {k: meta[k] for k in ('hub_package_id', 'manifest_id', 'created_at')}
        rows = sorted(visible.values(), key=lambda r: (r['created_at'], r['hub_package_id']), reverse=True)
        return self.page(rows, offset, limit)

    @staticmethod
    def page(rows, offset, limit):
        selected = []
        for row in rows[offset:offset + limit]:
            if len(json.dumps(selected + [row]).encode()) > 60000:
                break
            selected.append(row)
        return dict(items=selected, total=len(rows), offset=offset, limit=limit,
                    next_offset=offset + len(selected) if offset + len(selected) < len(rows) else None)

    def recover_all(self):
        rows = self.requests()
        seen = set()
        for row in rows:
            key = request_key(row['owner_subject'], row['source_service'], row['request_id'])
            if key != row['internal_request_id'] or not self.journal(row).is_file():
                raise PackageError('archive_integrity', 'Request index mismatch', 503, 'recovery')
            unique = (row['hub_package_id'], row['version'])
            if unique in seen:
                raise PackageError('archive_integrity', 'Duplicate reserved version', 503, 'recovery')
            seen.add(unique)
            self.ensure_metadata(row)
        for row in sorted(rows, key=lambda r: r['created_at']):
            self.recover_request(row)
            if row['status'] == 'committed':
                self.validate(row, self.archived(row) / 'package.zip')
                if read_json(self.archived(row) / 'receipt.json') != public(row):
                    raise PackageError('archive_integrity', 'Archive receipt mismatch', 503, 'recovery')
            (self.pending(row) / 'upload.tmp').unlink(missing_ok=True)
