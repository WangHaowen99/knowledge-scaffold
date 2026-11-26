"""Ontology-scoped packages with durable request intents and immutable ZIP archives.

Mutation and recovery entry points run under the shared Store's global lock.
HTTP listings use package_queries for concurrent reads. A request is the
recovery journal; only a durable committing intent can publish.
"""
import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone

from .package_contract import DelegationBody, PackageError, PublishBody, check_capability
from .package_validation import inspect_package, validate_derivation
from .storage import digest, identifier, read_json, sync_dir, write_json


def now():
    return datetime.now(timezone.utc)


def request_key(subject, service, request_id):
    identifier(request_id)
    return hashlib.sha256(json.dumps([subject, service, request_id]).encode()).hexdigest()


PUBLIC = ('request_id', 'ontology_id', 'release_id', 'name', 'filename', 'existing', 'version', 'kind',
          'source_service', 'creator', 'change_type', 'base_release_id', 'base_digest', 'status',
          'package_digest', 'size', 'created_at', 'expires_at', 'committed_at')
# Fields a re-publication of an already archived version takes over from the original commit.
DUPLICATE_FIELDS = ('name', 'filename', 'version', 'kind', 'release_id', 'package_digest', 'size', 'committed_at')
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


def package_filename(ontology_id, name, version):
    safe = re.sub(r'[^\w. -]', '_', name, flags=re.UNICODE).strip(' .')[:80].strip(' .')
    if not safe:
        safe = 'knowledge_package'
    while len(safe.encode('utf-8')) > 120:
        safe = safe[:-1]
    return f'{ontology_id.replace("-", "")[:12]}-{safe}-{version}.zip'


class PackageStore:
    def __init__(self, store):
        self.io = store
        self.root = store.root

    def requests(self):
        return [read_json(path) for path in (self.root / 'publish-requests').glob('*.json')]

    def committed(self, package_id):
        """Committed requests of a package that archived a version themselves, not re-publications of one."""
        for r in self.requests():
            if r['ontology_id'] == package_id and r['status'] == 'committed' and not r.get('existing'):
                yield r

    def root_of(self, package_id):
        return self.root / 'packages' / identifier(package_id)

    def journal(self, row):
        return self.root / 'publish-requests' / (row['internal_request_id'] + '.json')

    def save(self, row):
        write_json(self.journal(row), row)

    def delegation_path(self, delegation_id):
        return self.root / 'delegations' / (identifier(delegation_id) + '.json')

    @staticmethod
    def delegation_changed(existing, record):
        for key in ('subject', 'issuer', 'sub', 'service', 'ontology_id'):
            if existing.get(key) != record.get(key):
                return True
        return False

    def register_delegation(self, body, identity):
        from .user_auth import require_direct
        require_direct(identity)
        if identity.service != 'modeling':
            raise PackageError('service_forbidden', 'Only modeling can register publication delegations', 403, 'authorization')
        value = DelegationBody.model_validate(body).model_dump()
        if value['expires_at'] is not None and value['expires_at'] <= now().timestamp():
            raise PackageError('invalid_delegation', 'Delegation expiry must be in the future', 422, 'authorization')
        path = self.delegation_path(value['delegation_id'])
        existing = read_json(path)
        record = {**value, 'subject': identity.subject, 'issuer': identity.issuer, 'sub': identity.sub,
                  'service': identity.service,
                  'created_at': (existing or {}).get('created_at', now().isoformat()),
                  'used_nonces': (existing or {}).get('used_nonces', {})}
        if (self.root_of(value['ontology_id']) / 'metadata.json').exists():
            self.owned(value['ontology_id'], identity.subject)
        if existing and self.delegation_changed(existing, record):
            raise PackageError('delegation_conflict', 'Delegation identity or target changed', 409, 'authorization')
        write_json(path, record)
        return {'delegation_id': value['delegation_id'], 'ontology_id': value['ontology_id'],
                'expires_at': value['expires_at'], 'active': True}

    def revoke_delegation(self, delegation_id, identity):
        from .user_auth import require_direct
        require_direct(identity)
        path = self.delegation_path(delegation_id)
        record = read_json(path)
        if record and (record['subject'] != identity.subject or record['service'] != identity.service):
            raise PackageError('not_found', 'Delegation not found', 404, 'authorization')
        path.unlink(missing_ok=True)
        return {'delegation_id': delegation_id, 'revoked': True}

    def delegation(self, delegation_id, identity):
        if not identity.delegation_id or identity.delegation_id != delegation_id:
            raise PackageError('not_found', 'Delegation not found', 404, 'authorization')
        if (self.root_of(identity.delegation_ontology_id) / 'metadata.json').exists():
            self.owned(identity.delegation_ontology_id, identity.subject)
        return {'delegation_id': delegation_id, 'ontology_id': identity.delegation_ontology_id,
                'active': True}

    def authorize_delegation(self, identity):
        if not identity.delegation_id:
            return None
        path = self.delegation_path(identity.delegation_id)
        record = read_json(path)
        timestamp = now().timestamp()
        unavailable = not record
        if record:
            wrong_subject = record.get('subject') != identity.subject
            wrong_service = record.get('service') != identity.service
            wrong_identity = wrong_subject or wrong_service
            wrong_package = record.get('ontology_id') != identity.delegation_ontology_id
            expiry = record.get('expires_at')
            expired = expiry is not None and expiry <= timestamp
            unavailable = wrong_identity or wrong_package or expired
        if unavailable:
            raise PackageError('delegation_revoked', 'Publication delegation is unavailable', 401, 'authorization')
        used = {nonce: expiry for nonce, expiry in record.get('used_nonces', {}).items() if expiry >= timestamp - 60}
        if identity.delegation_nonce in used:
            raise PackageError('delegation_replayed', 'Delegated authorization was already used', 409, 'authorization')
        used[identity.delegation_nonce] = identity.delegation_expires_at
        if len(used) > 1000:
            raise PackageError('delegation_busy', 'Delegation replay cache is full', 429, 'authorization')
        record['used_nonces'] = used
        write_json(path, record)
        return record

    def pending(self, row):
        return self.root_of(row['ontology_id']) / 'pending' / row['internal_request_id']

    def archived(self, row):
        return self.root_of(row['ontology_id']) / (
            'releases' if row['kind'] == 'release' else 'snapshots') / row['release_id']

    def ensure_metadata(self, row):
        # Only a durable commit decision establishes the initial authorization.
        root = self.root_of(row['ontology_id'])
        self.io.initialize(root)
        meta = read_json(root / 'metadata.json')
        if not meta:
            write_json(root / 'metadata.json', dict(
                ontology_id=row['ontology_id'],
                authorized_users={row['owner_subject']: row['creator_identity']},
                state='active', created_at=row['created_at']))

    def owned(self, ontology_id, subject, *, allow_deleted=False):
        meta = read_json(self.root_of(ontology_id) / 'metadata.json')
        if not meta or subject not in meta.get('authorized_users', {}):
            raise PackageError('not_found', 'Package not found', 404, 'authorization')
        if meta.get('state') != 'active' and not allow_deleted:
            raise PackageError('package_deleted', 'Package has been deleted', 410)
        return meta

    def authorize_request(self, row, identity):
        if identity.delegation_id and row['ontology_id'] != identity.delegation_ontology_id:
            raise PackageError('delegation_target_mismatch', 'Delegation does not allow this package', 403, 'authorization')
        if (self.root_of(row['ontology_id']) / 'metadata.json').exists():
            self.owned(row['ontology_id'], identity.subject)

    def authorized_users(self, ontology_id, subject, offset=0, limit=20):
        meta = self.owned(ontology_id, subject)
        rows = [dict(subject_id=key, **value) for key, value in sorted(meta['authorized_users'].items())]
        return dict(ontology_id=ontology_id, **self.page(rows, offset, limit))

    def grant(self, ontology_id, subject, users):
        from .user_auth import subject_id
        meta = self.owned(ontology_id, subject)
        for user in users:
            meta['authorized_users'][subject_id(user['issuer'], user['sub'])] = user
        write_json(self.root_of(ontology_id) / 'metadata.json', meta)
        return dict(ontology_id=ontology_id, granted=len(users))

    def revoke(self, ontology_id, subject, target):
        meta = self.owned(ontology_id, subject)
        if target in meta['authorized_users']:
            if len(meta['authorized_users']) == 1:
                raise PackageError('last_authorized_user', 'Cannot revoke the last authorized user', 409)
            del meta['authorized_users'][target]
            write_json(self.root_of(ontology_id) / 'metadata.json', meta)
        return dict(ontology_id=ontology_id, revoked=True)

    def delete(self, ontology_id, subject):
        meta = self.owned(ontology_id, subject, allow_deleted=True)
        if meta['state'] != 'deleted':
            meta.update(state='deleted', deleted_at=now().isoformat())
            write_json(self.root_of(ontology_id) / 'metadata.json', meta)
        self.purge_deleted(ontology_id)
        return dict(ontology_id=ontology_id, state='deleted')

    def purge_deleted(self, ontology_id):
        """Clear payloads under the write lock, preserving the durable ACL tombstone."""
        root = self.root_of(ontology_id)
        if read_json(root / 'metadata.json', {}).get('state') != 'deleted':
            raise PackageError('delete_required', 'Package must be deleted before cleanup', 409)
        for directory in ('publish-requests', 'delegations'):
            for path in (self.root / directory).glob('*.json'):
                if read_json(path, {}).get('ontology_id') == ontology_id:
                    path.unlink(missing_ok=True)
            if (self.root / directory).exists():
                sync_dir(self.root / directory)
        for path in root.iterdir():
            if path.name == 'metadata.json':
                continue
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        sync_dir(root)

    def get_request(self, request_id, identity):
        key = request_key(identity.subject, identity.service, request_id)
        row = read_json(self.root / 'publish-requests' / (key + '.json'))
        if not row:
            raise PackageError('not_found', 'Publication request not found', 404)
        self.authorize_request(row, identity)
        check_capability(identity.service, row)
        self.recover_request(row)
        return row

    def reserve(self, body, identity):
        payload = PublishBody.model_validate(body).model_dump()
        check_capability(identity.service, payload)
        if identity.delegation_id and payload['ontology_id'] != identity.delegation_ontology_id:
            raise PackageError('delegation_target_mismatch', 'Delegation does not allow this package', 403, 'authorization')
        key = request_key(identity.subject, identity.service, payload['request_id'])
        existing = read_json(self.root / 'publish-requests' / (key + '.json'))
        if existing:
            if existing['input'] != payload:
                raise PackageError('request_conflict', 'Request inputs changed', 409)
            row = self.get_request(payload['request_id'], identity)
            self.require_live(row)
            return public(row), False
        package_id = payload['ontology_id']
        if (self.root_of(package_id) / 'metadata.json').exists():
            self.owned(package_id, identity.subject)
            if payload['change_type'] == 'modeling_content' and not payload['base_release_id']:
                raise PackageError('base_required', 'Modeling update requires a base version', 409)
        base = None
        if payload['base_release_id']:
            base, _ = self.release(package_id, payload['base_release_id'], identity.subject)
            if base['package_digest'] != payload['base_digest']:
                raise PackageError('base_conflict', 'Base package digest mismatch', 409)
        occupied = {r['version'] for r in self.requests() if r['ontology_id'] == package_id}
        version = payload['version']
        if version:
            pass  # External uploads compare actual bytes with the committed version.
        elif payload['kind'] == 'evaluation_snapshot':
            version = snapshot_version(base['version'] if base else '', occupied)
        else:
            numbers = [int(v.split('.')[2]) for v in occupied if re.fullmatch(r'0\.1\.[0-9]{1,12}', v)]
            version = f'0.1.{max(numbers, default=0) + 1}'
            while version in occupied:
                version = f'0.1.{int(version.split(".")[2]) + 1}'
        timestamp = now()
        row = dict(payload, version=version, release_id=uuid.uuid4().hex,
                   input=payload, internal_request_id=key, owner_subject=identity.subject,
                   creator_identity=dict(issuer=identity.issuer, sub=identity.sub),
                   creator=dict(issuer=identity.issuer, sub=identity.sub),
                   source_service=identity.service, status='pending', created_at=timestamp.isoformat(),
                   expires_at=(timestamp + timedelta(seconds=self.io.pending_ttl)).isoformat())
        self.save(row)
        self.io.initialize(self.root_of(package_id))
        return public(row), True

    def require_live(self, row):
        if row['status'] == 'expired':
            raise PackageError('request_expired', 'Publication request expired', 410)

    def validate(self, row, path):
        if digest(path) != row['package_digest']:
            raise PackageError('digest_conflict', 'Package digest mismatch', 409)
        base, base_path = None, None
        if row.get('base_release_id'):
            base, base_path = self.release_unchecked(row['ontology_id'], row['base_release_id'])
            if base['package_digest'] != row['base_digest']:
                raise PackageError('base_conflict', 'Base package digest mismatch', 409)
        validate_derivation(self.io, path, row, base_path, base)
        manifest, _ = inspect_package(self.io, path, row['ontology_id'], row['version'])
        return manifest

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
            manifest = self.validate(updated, path)
            updated.update(name=manifest['name'])
            updated['filename'] = package_filename(updated['ontology_id'], updated['name'], updated['version'])
            duplicate = self.version_match(updated)
            if duplicate:
                updated['existing'] = True
                updated['release_id'] = duplicate['release_id']
                updated['creator'] = duplicate.get('creator')
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
        duplicate = self.version_match(row)
        if duplicate:
            row['creator'] = duplicate.get('creator')
            row.update({k: duplicate[k] for k in DUPLICATE_FIELDS})
            row.update(existing=True, status='committed')
            self.save(row)
            (self.pending(row) / 'package.zip').unlink(missing_ok=True)
            return public(row)
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
        root = self.root_of(row['ontology_id'])
        self.io.git(root, 'add', '--', '.gitignore', target.relative_to(root).as_posix())
        if self.io.git(root, 'diff', '--cached', '--name-only'):
            self.io.git(root, 'commit', '-q', '-m', 'Archive knowledge package version')
        self.ensure_metadata(row)
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
        return self.release_unchecked(package_id, release_id)

    def release_unchecked(self, package_id, release_id):
        identifier(release_id)
        row = next((r for r in self.committed(package_id) if r['release_id'] == release_id), None)
        if not row:
            raise PackageError('not_found', 'Version not found', 404)
        path = self.archived(row) / 'package.zip'
        if not path.is_file() or digest(path) != row['package_digest']:
            raise PackageError('archive_integrity', 'Archive integrity failure', 503, 'download')
        return public(row), path

    def listing(self, package_id, subject, kind='release', offset=0, limit=20):
        self.owned(package_id, subject)
        rows = [public(r) for r in self.committed(package_id) if r['kind'] == kind]
        rows.sort(key=lambda r: (r['committed_at'], r['release_id']), reverse=True)
        return self.page(rows, offset, limit)

    def version_match(self, row):
        other = next((r for r in self.committed(row['ontology_id']) if r['version'] == row['version']), None)
        if other and (other['package_digest'] != row['package_digest'] or other['kind'] != row['kind']):
            raise PackageError('version_conflict', 'Package version conflict', 409)
        return other

    def summary(self, ontology_id, subject):
        self.owned(ontology_id, subject)
        rows = self.listing(ontology_id, subject)['items']
        if not rows:
            rows = self.listing(ontology_id, subject, 'evaluation_snapshot')['items']
        if not rows:
            raise PackageError('not_found', 'Package not found', 404)
        return rows[0]

    def packages(self, subject, offset=0, limit=20):
        from .package_queries import PackageQueries
        return PackageQueries(self.io).packages(subject, offset, limit)

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
        for row in sorted(rows, key=lambda r: r['created_at']):
            if read_json(self.root_of(row['ontology_id']) / 'metadata.json', {}).get('state') == 'deleted':
                continue
            key = request_key(row['owner_subject'], row['source_service'], row['request_id'])
            if key != row['internal_request_id'] or not self.journal(row).is_file():
                raise PackageError('archive_integrity', 'Request index mismatch', 503, 'recovery')
            self.recover_request(row)
            if row['status'] == 'committed':
                if not row.get('existing'):
                    key = (row['ontology_id'], row['version'])
                    if key in seen:
                        raise PackageError('archive_integrity', 'Duplicate committed version', 503, 'recovery')
                    seen.add(key)
                if not (self.root_of(row['ontology_id']) / 'metadata.json').exists():
                    raise PackageError('archive_integrity', 'Package metadata missing', 503, 'recovery')
                self.validate(row, self.archived(row) / 'package.zip')
                if not row.get('existing') and read_json(self.archived(row) / 'receipt.json') != public(row):
                    raise PackageError('archive_integrity', 'Archive receipt mismatch', 503, 'recovery')
            (self.pending(row) / 'upload.tmp').unlink(missing_ok=True)
        for root in (self.root / 'packages').glob('*'):
            if read_json(root / 'metadata.json', {}).get('state') == 'deleted':
                continue
            self.io.recover(root)
            self.io.validate_resources(root)
