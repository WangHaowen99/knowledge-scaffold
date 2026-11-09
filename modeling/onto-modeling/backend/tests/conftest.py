"""Test-only Hub transport for existing evaluation protocol fixtures."""
from datetime import datetime, timezone
from pathlib import Path
import shutil
import sys
import threading
import uuid

import pytest

# Cross-project Hub tests import the sibling knowledge-hub source and skip when it is absent.
for parent in Path(__file__).resolve().parents:
    hub_backend = parent / 'knowledge-hub' / 'backend'
    if hub_backend.exists():
        sys.path.append(str(hub_backend))
        break


@pytest.fixture(autouse=True)
def evaluation_hub_archive(request, tmp_path, monkeypatch):
    """In-memory Hub so evaluation tests do not depend on the knowledge-hub source tree."""
    if request.module.__name__.split('.')[-1] not in (
        'test_modeling_eval', 'test_publish_binding_gate', 'test_eval_import', 'test_eval_presets'
    ):
        return
    from app.services import hub_packages, auth, knowledge_hub
    from app.services.workspace import WorkspaceError
    archives = tmp_path / 'synthetic-hub'
    archives.mkdir()
    requests = {}
    lock = threading.Lock()
    monkeypatch.setattr(auth, 'ENABLED', True)
    monkeypatch.setattr(auth, 'verify_access_token', lambda token: {'sub': 'operator_a', 'iss': 'https://example.com'})

    def publish_request(request_id):
        row = requests.get(request_id)
        if row is None:
            raise WorkspaceError('Synthetic Hub request not found', 404)
        return row

    def call(method, path, token='', body=None, upload=None, headers=None, download=None, upload_name=None):
        parts = path.strip('/').split('/')
        with lock:
            if path == '/api/package-publish-requests':
                row = requests.setdefault(body['request_id'], dict(
                    body, status='pending', release_id=uuid.uuid4().hex,
                    version=f'0.1.0-eval.{len(requests) + 1}'))
                return dict(row)
            if method == 'GET' and len(parts) == 3 and parts[:2] == ['api', 'package-publish-requests']:
                return dict(publish_request(parts[-1]))
            if parts[-1] == 'content':
                row = publish_request(parts[-2])
                shutil.copyfile(upload, archives / row['release_id'])
                if knowledge_hub.file_digest(archives / row['release_id']) != headers['X-Package-Digest']:
                    raise AssertionError('Synthetic Hub upload digest mismatch')
                row.update(status='uploaded', package_digest=headers['X-Package-Digest'])
                return dict(row)
            if parts[-1] == 'commit':
                row = publish_request(parts[-2])
                row.update(status='committed', committed_at=datetime.now(timezone.utc).isoformat())
                return dict(row)
            if parts[:2] == ['api', 'packages'] and parts[3:4] == ['releases']:
                row = next(r for r in requests.values() if r['release_id'] == parts[4] and r['status'] == 'committed')
                if download:
                    shutil.copyfile(archives / row['release_id'], download)
                    return Path(download)
                return dict(row)
        raise AssertionError(f'Unexpected synthetic Hub operation: {method}')

    monkeypatch.setattr(hub_packages, 'call', call)
