"""Test-only Hub transport for existing evaluation protocol fixtures."""
from pathlib import Path
import sys

import pytest

for parent in Path(__file__).resolve().parents:
    hub_backend = parent / 'knowledge-hub' / 'backend'
    if hub_backend.exists():
        sys.path.insert(0, str(hub_backend))
        break


@pytest.fixture(autouse=True)
def evaluation_hub_archive(request, tmp_path, monkeypatch):
    if request.module.__name__.split('.')[-1] not in ('test_modeling_eval', 'test_publish_binding_gate'):
        return
    from hub.storage import Store
    from app.services import knowledge_hub
    store = Store(tmp_path / 'synthetic-hub')

    def call(method, path, token='', body=None, upload=None, headers=None, download=None):
        import shutil
        parts = path.strip('/').split('/')
        pid = parts[2]
        with store.lock():
            if parts[-1] == 'release-requests':
                return store.reserve(pid, body['request_id'], body, 'operator_a')
            if parts[-1] == 'content':
                with Path(upload).open('rb') as f:
                    return store.upload(pid, parts[-2], 'operator_a', f, headers['X-Package-Digest'])
            if parts[-1] == 'commit':
                return store.commit(pid, parts[-2], 'operator_a')
            if parts[-1] == 'package':
                _, package = store.release(pid, parts[-2])
                shutil.copyfile(package, download)
                return Path(download)
        raise AssertionError(f'Unexpected synthetic Hub operation: {method}')

    monkeypatch.setattr(knowledge_hub, 'call', call)
