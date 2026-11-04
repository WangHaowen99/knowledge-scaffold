"""Explicit sharing only; all inputs are synthetic."""
from pathlib import Path
from unittest.mock import patch
import pytest

from app import config
from app.services import knowledge_hub, hub_packages
from app.services.workspace import WorkspaceManager, WorkspaceError


def test_resources_require_selection_published_identity_and_user(tmp_path, monkeypatch):
    manager = WorkspaceManager(tmp_path / 'onto', tmp_path / 'assets')
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path / 'onto')
    pid = manager.create_project('project_alpha', owner='operator_a')['id']
    root = manager.project_root(pid)
    folder = root / 'resource/others'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'selected.txt').write_text('Synthetic selected material')
    (folder / 'private.txt').write_text('Synthetic unselected material')
    with pytest.raises(WorkspaceError):
        knowledge_hub.sync_resources(manager, pid, '', [])
    with pytest.raises(WorkspaceError):
        knowledge_hub.sync_resources(manager, pid, 'synthetic', [])
    linked = dict(ontology_id=hub_packages.ontology_id(pid))
    knowledge_hub.save(hub_packages.directory(pid, 'scope').parent / 'association.json', linked)
    calls = []
    def call(method, path, token, **kwargs):
        assert token == 'synthetic'
        assert path == f"/api/packages/{linked['ontology_id']}/resources/current"
        calls.append(method)
        if method == 'GET':
            return {'generation': 2}
        import zipfile
        with zipfile.ZipFile(kwargs['upload']) as archive:
            assert set(archive.namelist()) == {'resource/others/selected.txt', 'resource/materials.json'}
        assert kwargs['headers']['X-Resource-Generation'] == '3'
        return dict(ontology_id=linked['ontology_id'], generation=3, files=[])
    with patch.object(knowledge_hub, 'call', call):
        knowledge_hub.sync_resources(manager, pid, 'synthetic', ['resource/others/selected.txt'])
        knowledge_hub.retry_tasks(manager)
    assert calls == ['GET', 'PUT']
    with patch.object(knowledge_hub, 'call', side_effect=AssertionError('Unexpected remote delete')):
        knowledge_hub.stage_delete(manager, pid)
        knowledge_hub.retry_tasks(manager)
