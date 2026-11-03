"""Test-only Hub transport for existing evaluation protocol fixtures."""
from pathlib import Path
import sys

import pytest

for parent in Path(__file__).resolve().parents:
    hub_backend = parent / 'knowledge-hub' / 'backend'
    if hub_backend.exists():
        sys.path.append(str(hub_backend))
        break


@pytest.fixture(autouse=True)
def evaluation_hub_archive(request, tmp_path, monkeypatch):
    if request.module.__name__.split('.')[-1] not in (
        'test_modeling_eval', 'test_publish_binding_gate', 'test_eval_import', 'test_eval_presets'
    ):
        return
    from hub.storage import Store
    from hub.package_store import PackageStore
    from hub.user_auth import Identity
    from app.services import hub_packages, auth
    store = PackageStore(Store(tmp_path / 'synthetic-hub'))
    monkeypatch.setattr(auth, 'ENABLED', True)
    monkeypatch.setattr(auth, 'verify_access_token', lambda token: {'sub': 'operator_a', 'iss': 'https://example.com'})

    def call(method, path, token='', body=None, upload=None, headers=None, download=None, upload_name=None):
        import shutil
        actor = Identity('synthetic-owner', 'modeling')
        parts = path.strip('/').split('/')
        with store.io.lock():
            if path == '/api/package-publish-requests':
                return store.reserve(body, actor)[0]
            if parts[-1] == 'content':
                with Path(upload).open('rb') as f:
                    return store.upload(parts[-2], actor, f, headers['X-Package-Digest'])
            if parts[-1] == 'commit':
                return store.commit(parts[-2], actor)
            receipt, package = store.release(parts[2], parts[4], actor.subject)
            if download:
                shutil.copyfile(package, download)
                return Path(download)
            return receipt
        raise AssertionError(f'Unexpected synthetic Hub operation: {method}')

    monkeypatch.setattr(hub_packages, 'call', call)
