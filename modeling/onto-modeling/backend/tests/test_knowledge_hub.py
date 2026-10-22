"""Hub integration with synthetic packages and a temporary archive."""
import io
import json
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from app import config
from app.routers import hub, projects
from app.services import auth, knowledge_hub
from app.services.workspace import WorkspaceError, WorkspaceManager


class HubIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.manager = WorkspaceManager(root / 'onto', root / 'assets')
        self.meta = self.manager.create_project('project_alpha', owner='operator_a')
        self.pid = self.meta['id']
        self.patches = [patch.object(config, 'DATA_DIR', root / 'onto'),
                        patch.object(projects, 'workspaces', self.manager)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_latest_resource_sync_retries_without_token_and_clears_old_files(self):
        from hub.storage import Store
        store = Store(Path(self.tmp.name) / 'hub')
        path = self.manager.project_root(self.pid) / 'resource/others/demo.txt'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('synthetic initial')
        def call(method, route, token='', **kwargs):
            self.assertEqual(token, '')
            with Path(kwargs['upload']).open('rb') as f:
                return store.resources(self.pid, int(kwargs['headers']['X-Resource-Generation']), f,
                                       kwargs['headers']['X-Resource-Digest'])
        with patch.object(knowledge_hub, 'call', side_effect=WorkspaceError('offline', 503)):
            with self.assertRaises(WorkspaceError):
                knowledge_hub.sync_resources(self.manager, self.pid)
        self.assertTrue(knowledge_hub.resource_status(self.pid)['pending'])
        path.write_text('synthetic replacement')
        with patch.object(knowledge_hub, 'call', call):
            self.assertFalse(knowledge_hub.sync_resources(self.manager, self.pid)['pending'])
            path.unlink()
            knowledge_hub.sync_resources(self.manager, self.pid)
        self.assertFalse((store.project(self.pid) / 'resources-current/resource/others/demo.txt').exists())

    def test_authorization_roles_and_cleanup_state(self):
        secret = secrets.token_urlsafe(32)
        app = FastAPI()
        app.include_router(hub.router)
        @app.exception_handler(WorkspaceError)
        async def errors(request, exc):
            from fastapi.responses import JSONResponse
            return JSONResponse({'error': str(exc)}, status_code=exc.status)
        client = TestClient(app)
        self.addCleanup(client.close)
        self.manager._update_meta(self.pid, members={'operator_b': 'viewer', 'operator_c': 'editor'})
        with patch.dict('os.environ', {'ONTO_HUB_SERVICE_TOKEN': secret}), patch.object(auth, 'ENABLED', True):
            for subject, role, expected in [('operator_a', 'user', 200), ('operator_b', 'user', 200),
                                            ('operator_c', 'user', 200), ('operator_d', 'user', 403),
                                            ('operator_d', 'admin', 200)]:
                with patch.object(auth, 'verify_access_token', return_value={'sub': subject, 'role': role}):
                    response = client.post('/api/internal/hub/authorize', json={'project_id': self.pid, 'operation': 'read'},
                        headers={'X-Hub-Service-Token': secret, 'Authorization': 'Bearer synthetic'})
                    self.assertEqual(response.status_code, expected)
            knowledge_hub.queue_delete(self.pid)
            headers = {'X-Hub-Service-Token': secret}
            payload = {'project_id': self.pid, 'operation': 'cleanup'}
            self.assertEqual(client.post('/api/internal/hub/authorize', json=payload, headers=headers).status_code, 409)
            self.manager.delete_project(self.pid)
            self.assertEqual(client.post('/api/internal/hub/authorize', json=payload, headers=headers).status_code, 200)


if __name__ == '__main__':
    unittest.main()
