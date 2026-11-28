"""Single-volume archive with process locks, durable intents and Git recovery."""
from contextlib import contextmanager, suppress
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
import unicodedata
import zipfile


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
        with suppress(FileNotFoundError):
            os.unlink(name)


def unsafe_member(info, name, parts):
    """Member names that could escape or alias the extraction root, and non-regular members."""
    if not name or len(name) > 1024 or len(parts) > 32:
        return True
    if name.startswith('/') or '\\' in name or ':' in name:
        return True
    if any(p in ('', '.', '..') for p in name.split('/')):
        return True
    mode = info.external_attr >> 16
    return stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)


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
    def lock(self, *, blocking=True):
        # Single writer also serializes backup and deletion against all mutations.
        with (self.root / '.lock').open('a') as f:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def git(self, root, *args):
        result = subprocess.run(['git', '-c', 'user.name=knowledge-hub',  # NOSONAR(S4721)
                                 '-c', 'user.email=user@example.com', *args],
                                cwd=root, capture_output=True, shell=False, timeout=120)
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
        current, old = root / 'resources-current', root / '.resources-old'
        if not current.exists() and old.exists():
            os.replace(old, current)
        if old.exists():
            shutil.rmtree(old)
        for stale in root.glob('.resources-upload-*'):
            shutil.rmtree(stale)
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
                    if unsafe_member(i, name, parts) or canonical in seen or i.flag_bits & 1:
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
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > self.max_bytes:
                    raise HubError('Upload exceeds limit', 413)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())

    def resources(self, ontology_id, generation, stream, expected_digest):
        root = self.root / 'packages' / identifier(ontology_id)
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
            receipt = {'ontology_id': ontology_id, 'generation': generation, 'digest': expected_digest, 'files': files}
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

    def clear_resources(self, ontology_id, generation):
        root = self.root / 'packages' / identifier(ontology_id)
        self.recover(root)
        current = root / 'resources-current'
        before = read_json(current / 'receipt.json', {'generation': 0})
        if generation < 1:
            raise HubError('Invalid resource generation')
        if generation < before['generation']:
            raise HubError('Stale resource generation', 409)
        if generation == before['generation']:
            if not before.get('empty'):
                raise HubError('Resource generation conflict', 409)
            return before
        receipt = dict(ontology_id=ontology_id, generation=generation, digest=None, files=[], empty=True)
        target = Path(tempfile.mkdtemp(prefix='.resources-upload-', dir=root))
        try:
            write_json(target / 'receipt.json', receipt)
            if current.exists():
                os.replace(current, root / '.resources-old')
            os.replace(target, current)
            sync_dir(root)
            if (root / '.resources-old').exists():
                shutil.rmtree(root / '.resources-old')
        finally:
            shutil.rmtree(target, ignore_errors=True)
        return receipt

    def validate_resources(self, root):
        current = root / 'resources-current'
        if current.exists():
            resource_receipt = read_json(current / 'receipt.json')
            if not isinstance(resource_receipt, dict):
                raise HubError('Resource receipt unavailable', 503)
            for row in resource_receipt.get('files', []):
                path = current / row['path']
                contained = path.resolve().is_relative_to(current.resolve()) and not path.is_symlink()
                if not contained or not path.is_file() or digest(path) != row['digest']:
                    raise HubError('Resource integrity failure', 503)

    def recover_all(self):
        from .package_store import PackageStore
        PackageStore(self).recover_all()
