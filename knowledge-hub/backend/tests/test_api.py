"""HTTP boundary tests; credentials are generated per test and never persisted."""
import importlib
import os
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import io
import json
import urllib.error


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        os.environ['HUB_DATA_DIR'] = cls.temp.name
        from fastapi.testclient import TestClient
        cls.main = importlib.import_module('hub.main')
        cls.client = TestClient(cls.main.app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.temp.cleanup()

    def setUp(self):
        self.secret = secrets.token_urlsafe(32)
        self.env = patch.dict(os.environ, {'HUB_SERVICE_TOKEN': self.secret,
            'ONTO_AUTHZ_URL': 'http://service.example.net/api/internal/hub/authorize'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.headers = {'X-Hub-Service-Token': self.secret}

    def auth_response(self, allowed=True, subject='operator_a', **extra):
        return io.BytesIO(json.dumps({'project_id': 'project-alpha', 'allowed': allowed,
                                     'subject': subject, **extra}).encode())

    def test_removed_project_package_routes_are_not_available(self):
        for method, path in [('post', '/api/projects/project-alpha/release-requests'),
                             ('get', '/api/projects/project-alpha/releases'),
                             ('get', '/api/projects/project-alpha/releases/release_demo/package')]:
            self.assertEqual(getattr(self.client, method)(path).status_code, 404)

    def test_permission_revocation_and_outage_fail_closed(self):
        with patch('hub.main.urllib.request.urlopen', return_value=self.auth_response()):
            self.assertEqual(self.client.get('/api/projects/project-alpha/resources/current').status_code, 200)
        with patch('hub.main.urllib.request.urlopen', return_value=self.auth_response(False)):
            self.assertEqual(self.client.get('/api/projects/project-alpha/resources/current').status_code, 403)
        with patch('hub.main.urllib.request.urlopen', side_effect=TimeoutError):
            self.assertEqual(self.client.get('/api/projects/project-alpha/resources/current').status_code, 503)

    def test_missing_callback_is_never_anonymous_even_in_development(self):
        with patch.dict(os.environ, {'ONTO_AUTHZ_URL': '', 'HUB_DEV_ALLOW_UNAUTH': '1'}):
            self.assertEqual(self.client.get('/api/projects/project-alpha/resources/current').status_code, 503)

    def test_development_modes_must_match(self):
        with patch.dict(os.environ, {'HUB_DEV_ALLOW_UNAUTH': '0'}), patch(
                'hub.main.urllib.request.urlopen', return_value=self.auth_response(development=True)):
            self.assertEqual(self.client.get('/api/projects/project-alpha/resources/current').status_code, 503)

    def test_resources_and_cleanup_require_project_state_callback(self):
        with patch('hub.main.urllib.request.urlopen', return_value=self.auth_response(False)):
            self.assertEqual(self.client.delete('/api/internal/projects/project-alpha/artifacts',
                                               headers=self.headers).status_code, 403)
        self.assertEqual(self.client.delete('/api/internal/projects/project-alpha/artifacts').status_code, 401)

    def test_pagination_validation(self):
        self.assertEqual(self.client.get('/api/projects/project-alpha/resources/current?limit=101').status_code, 422)


if __name__ == '__main__':
    unittest.main()
