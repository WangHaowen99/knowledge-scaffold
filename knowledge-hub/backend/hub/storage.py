"""Single-volume archive with process locks, durable intents and Git recovery."""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
import unicodedata
import zipfile

import yaml


class HubError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def identifier(value):
    if not re.fullmatch(r'[\w][\w-]{0,127}', value):
        raise HubError('Invalid identifier')
    return value


def digest(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        sync_dir(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def write_json(path, value):
    atomic(path, json.dumps(value, ensure_ascii=False, sort_keys=True).encode())


def read_json(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


class Store:
    def __init__(self, root, max_bytes=256 * 1024**2, max_expanded=1024 * 1024**2,
                 max_members=10000, max_file=256 * 1024**2, pending_ttl=7 * 86400):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes, self.max_expanded = max_bytes, max_expanded
        self.max_members, self.max_file = max_members, max_file
        self.pending_ttl = pending_ttl

    @contextmanager
    def lock(self):
        # Single writer also serializes backup and deletion against all mutations.
        with (self.root / '.lock').open('a') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def project(self, pid):
        identifier(pid)
        if (self.root / 'cleanup' / f'{pid}.json').exists():
            raise HubError('Project deleted', 410)
        return self.root / 'projects' / pid

    def git(self, root, *args):
        result = subprocess.run(['git', '-c', 'user.name=knowledge-hub',
                                 '-c', 'user.email=user@example.com', *args],
                                cwd=root, capture_output=True, timeout=120)
        if result.returncode:
            raise HubError('Archive journal unavailable', 503)
        return result.stdout

    def initialize(self, root):
        root.mkdir(parents=True, exist_ok=True)
        if not (root / '.git').exists():
            self.git(root, 'init', '-q')
        atomic(root / '.gitignore', b'resources-current/\n.resources-*/\npending/\nmetadata.json\n.write-*\n')

    def recover(self, root):
        if not root.exists():
            return
        self.initialize(root)
        # Receipt+ZIP directories are durable commit intents. Publish only after Git succeeds.
        for kind in ('releases', 'snapshots'):
            for folder in sorted((root / kind).glob('*')):
                receipt = read_json(folder / 'receipt.json')
                if not receipt or not (folder / 'package.zip').is_file():
                    raise HubError('Incomplete archive intent', 503)
                if digest(folder / 'package.zip') != receipt['package_digest']:
                    raise HubError('Archive integrity failure', 503)
        self.git(root, 'add', '--', '.gitignore')
        for kind in ('releases', 'snapshots'):
            if (root / kind).exists():
                self.git(root, 'add', '--', kind)
        if self.git(root, 'diff', '--cached', '--name-only'):
            self.git(root, 'commit', '-q', '-m', 'Archive knowledge package')
        current, old = root / 'resources-current', root / '.resources-old'
        if not current.exists() and old.exists():
            os.replace(old, current)
        if old.exists():
            shutil.rmtree(old)
        for stale in root.glob('.resources-upload-*'):
            shutil.rmtree(stale)
        for path in (root / 'pending').glob('*/receipt.json'):
            receipt = read_json(path)
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(receipt['created_at'])).total_seconds()
            if age > self.pending_ttl and receipt.get('status') != 'expired':
                receipt['status'] = 'expired'
                write_json(path, receipt)
                (path.parent / 'package.zip').unlink(missing_ok=True)
                (path.parent / 'upload.tmp').unlink(missing_ok=True)


    def receipts(self, root, kind='release'):
        directory = 'releases' if kind == 'release' else 'snapshots'
        return [read_json(p) for p in (root / directory).glob('*/receipt.json')]

    def reserve(self, pid, request_id, payload, subject):
        identifier(request_id)
        root = self.project(pid)
        self.initialize(root)
        self.recover(root)
        identity = {'source_digest': payload['source_digest'], 'source_commit': payload.get('source_commit', ''),
                    'publish_options': payload.get('publish_options', {}), 'kind': payload.get('kind', 'release'),
                    'actor_subject': subject}
        if identity['kind'] not in ('release', 'evaluation_snapshot'):
            raise HubError('Invalid package kind')
        if not re.fullmatch('[a-f0-9]{64}', identity['source_digest']):
            raise HubError('Invalid source digest')
        for receipt in self.receipts(root) + self.receipts(root, 'evaluation_snapshot'):
            if receipt['request_id'] == request_id:
                if any(receipt[k] != v for k, v in identity.items()):
                    raise HubError('Request content conflict', 409)
                return receipt
        pending = root / 'pending' / request_id
        existing = read_json(pending / 'receipt.json')
        if existing:
            if existing.get('status') == 'expired':
                raise HubError('Publication request expired', 410)
            if any(existing[k] != v for k, v in identity.items()):
                raise HubError('Request content conflict', 409)
            existing['status'] = 'pending'
            return existing
        meta = read_json(root / 'metadata.json', {'sequence': 0})
        # Include durable intents if metadata was restored independently.
        numbers = [r['sequence'] for r in self.receipts(root) + self.receipts(root, 'evaluation_snapshot')]
        numbers += [read_json(p)['sequence'] for p in (root / 'pending').glob('*/receipt.json')]
        sequence = max([meta['sequence'], *numbers]) + 1
        write_json(root / 'metadata.json', {'sequence': sequence})
        receipt = dict(identity, project_id=pid, package_id=pid, request_id=request_id,
                       release_id=uuid.uuid4().hex, sequence=sequence, version=f'0.1.{sequence}',
                       status='pending', created_at=datetime.now(timezone.utc).isoformat())
        write_json(pending / 'receipt.json', receipt)
        return receipt

    def request(self, pid, request_id, subject):
        root = self.project(pid)
        self.recover(root)
        identifier(request_id)
        candidates = self.receipts(root) + self.receipts(root, 'evaluation_snapshot')
        pending = read_json(root / 'pending' / request_id / 'receipt.json')
        if pending:
            if pending.get('status') == 'expired':
                raise HubError('Publication request expired', 410)
            pending['status'] = 'pending'
            candidates.append(pending)
        for receipt in candidates:
            if receipt['request_id'] == request_id:
                if receipt['actor_subject'] != subject:
                    raise HubError('Request belongs to another subject', 403)
                return receipt
        raise HubError('Request not found', 404)

    def inspect_zip(self, path):
        try:
            with zipfile.ZipFile(path) as z:
                infos = z.infolist()
                if len(infos) > self.max_members:
                    raise HubError('Too many archive members', 413)
                seen, total = set(), 0
                all_paths = {unicodedata.normalize('NFC', i.filename.rstrip('/')).casefold(): i.is_dir() for i in infos}
                for i in infos:
                    name = i.filename.rstrip('/')
                    parts = PurePosixPath(name).parts
                    canonical = unicodedata.normalize('NFC', name).casefold()
                    mode = i.external_attr >> 16
                    if (not name or len(name) > 1024 or len(parts) > 32 or name.startswith('/') or '\\' in name or ':' in name
                        or any(p in ('', '.', '..') for p in name.split('/'))
                        or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR))
                        or canonical in seen or i.flag_bits & 1):
                        raise HubError('Unsafe archive member')
                    seen.add(canonical)
                    total += i.file_size
                    if i.file_size > self.max_file or total > self.max_expanded:
                        raise HubError('Expanded archive exceeds limit', 413)
                    for n in range(1, len(parts)):
                        parent = unicodedata.normalize('NFC', '/'.join(parts[:n])).casefold()
                        if parent in all_paths and not all_paths[parent]:
                            raise HubError('Archive path conflict')
                # Validate CRC without allocating the expanded archive.
                for i in infos:
                    if not i.is_dir():
                        with z.open(i) as f:
                            while f.read(1024 * 1024):
                                pass
                return infos
        except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
            raise HubError('Invalid ZIP archive') from exc

    def copy_upload(self, stream, path):
        total = 0
        with path.open('wb') as out:
            while chunk := stream.read(1024 * 1024):
                total += len(chunk)
                if total > self.max_bytes:
                    raise HubError('Upload exceeds limit', 413)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())

    def upload(self, pid, request_id, subject, stream, expected_digest):
        receipt = self.request(pid, request_id, subject)
        root = self.project(pid)
        pending = root / 'pending' / identifier(request_id)
        pending.mkdir(parents=True, exist_ok=True)
        path = pending / 'upload.tmp'
        try:
            self.copy_upload(stream, path)
            actual = digest(path)
            if actual != expected_digest:
                raise HubError('Package digest mismatch', 409)
            if receipt.get('package_digest'):
                if receipt['package_digest'] != actual:
                    raise HubError('Request content conflict', 409)
                return receipt
            infos = self.inspect_zip(path)
            with zipfile.ZipFile(path) as z:
                manifest_name = f'{pid}/manifest.yaml'
                if any(not i.filename.startswith(pid + '/') for i in infos):
                    raise HubError('Package root mismatch')
                if manifest_name not in z.namelist() or z.getinfo(manifest_name).file_size > 65536:
                    raise HubError('Missing or oversized manifest')
                manifest = yaml.safe_load(z.read(manifest_name))
                if not isinstance(manifest, dict) or any(str(manifest.get(k)) != v for k, v in {
                    'kind': 'ontology-package', 'schema_version': '0.1', 'id': pid,
                    'version': receipt['version']}.items()):
                    raise HubError('Package manifest mismatch')
                for name in ('name', 'description'):
                    if not isinstance(manifest.get(name), str):
                        raise HubError('Missing manifest field')
                for name in ('objects/index.md', 'operations/index.md'):
                    if f'{pid}/{name}' not in z.namelist():
                        raise HubError('Missing package index')
            os.replace(path, pending / 'package.zip')
            receipt.update(package_digest=actual, size=(pending / 'package.zip').stat().st_size)
            write_json(pending / 'receipt.json', receipt)
            return receipt
        except yaml.YAMLError as exc:
            raise HubError('Invalid manifest') from exc
        finally:
            path.unlink(missing_ok=True)

    def commit(self, pid, request_id, subject):
        receipt = self.request(pid, request_id, subject)
        if receipt['status'] == 'committed':
            for folder in ('releases', 'snapshots'):
                if (self.project(pid) / folder / receipt['release_id'] / 'receipt.json').exists():
                    return receipt
        root = self.project(pid)
        pending = root / 'pending' / request_id
        package = pending / 'package.zip'
        if not receipt.get('package_digest') or not package.exists() or digest(package) != receipt['package_digest']:
            raise HubError('Package upload incomplete', 409)
        receipt['status'] = 'committed'
        write_json(pending / 'receipt.json', receipt)
        folder = root / ('releases' if receipt['kind'] == 'release' else 'snapshots')
        folder.mkdir(exist_ok=True)
        os.replace(pending, folder / receipt['release_id'])
        sync_dir(folder)
        self.recover(root)
        return receipt

    def listing(self, pid, offset=0, limit=20):
        root = self.project(pid)
        self.recover(root)
        rows = sorted(self.receipts(root), key=lambda r: r['sequence'], reverse=True)
        return {'releases': rows[offset:offset + limit], 'total': len(rows)}

    def release(self, pid, rid):
        root = self.project(pid)
        self.recover(root)
        identifier(rid)
        for folder in ('releases', 'snapshots'):
            path = root / folder / rid
            if (path / 'receipt.json').is_file():
                return read_json(path / 'receipt.json'), path / 'package.zip'
        raise HubError('Version not found', 404)

    def resources(self, pid, generation, stream, expected_digest):
        root = self.project(pid)
        self.initialize(root)
        self.recover(root)
        current = root / 'resources-current'
        before = read_json(current / 'receipt.json', {'generation': 0})
        if generation < 1:
            raise HubError('Invalid resource generation')
        if generation < before['generation']:
            raise HubError('Stale resource generation', 409)
        upload = Path(tempfile.mkdtemp(prefix='.resources-upload-', dir=root))
        try:
            archive = upload / 'resources.zip'
            self.copy_upload(stream, archive)
            if digest(archive) != expected_digest:
                raise HubError('Resource digest mismatch', 409)
            if generation == before['generation']:
                if expected_digest != before.get('digest'):
                    raise HubError('Resource generation conflict', 409)
                return before
            infos = self.inspect_zip(archive)
            target = upload / 'current'
            target.mkdir()
            files = []
            with zipfile.ZipFile(archive) as z:
                for info in infos:
                    if info.is_dir():
                        continue
                    p = PurePosixPath(info.filename)
                    if not (info.filename == 'resource/materials.json' or
                            len(p.parts) >= 3 and p.parts[:2] in (
                                ('resource', 'diagrams'), ('resource', 'excel'), ('resource', 'others'))):
                        raise HubError('Resource outside allowed scope')
                    dest = target / p
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(info) as src, dest.open('wb') as out:
                        shutil.copyfileobj(src, out, 1024 * 1024)
                        out.flush()
                        os.fsync(out.fileno())
                    files.append({'id': hashlib.sha256(info.filename.encode()).hexdigest(),
                                  'path': info.filename, 'size': info.file_size, 'digest': digest(dest)})
            metadata = target / 'resource' / 'materials.json'
            directives = read_json(metadata, {})
            paths = {f['path'] for f in files}
            if not isinstance(directives, dict) or any(p not in paths for p in directives):
                raise HubError('Invalid material metadata references')
            receipt = {'generation': generation, 'digest': expected_digest, 'files': files}
            write_json(target / 'receipt.json', receipt)
            if current.exists():
                os.replace(current, root / '.resources-old')
            os.replace(target, current)
            sync_dir(root)
            if (root / '.resources-old').exists():
                shutil.rmtree(root / '.resources-old')
            return receipt
        finally:
            shutil.rmtree(upload, ignore_errors=True)

    def delete(self, pid):
        identifier(pid)
        write_json(self.root / 'cleanup' / f'{pid}.json', {'project_id': pid, 'deleted': True})
        root = self.root / 'projects' / pid
        if root.exists():
            shutil.rmtree(root)
        sync_dir(self.root)

    def validate_resources(self, root):
        current = root / 'resources-current'
        if current.exists():
            resource_receipt = read_json(current / 'receipt.json')
            if not isinstance(resource_receipt, dict):
                raise HubError('Resource receipt unavailable', 503)
            for row in resource_receipt.get('files', []):
                path = current / row['path']
                if (not path.resolve().is_relative_to(current.resolve()) or path.is_symlink()
                        or not path.is_file() or digest(path) != row['digest']):
                    raise HubError('Resource integrity failure', 503)

    def recover_all(self):
        for tombstone in (self.root / 'cleanup').glob('*.json'):
            self.delete(tombstone.stem)
        for root in (self.root / 'projects').glob('*'):
            self.recover(root)
            self.validate_resources(root)
