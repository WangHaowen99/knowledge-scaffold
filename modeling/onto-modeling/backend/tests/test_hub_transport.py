"""Loopback HTTP integration with real Hub transport and a synthetic authority."""
import importlib
import json
import os
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import uvicorn

from app import config
from app.services import knowledge_hub
from app.services.workspace import WorkspaceManager, WorkspaceError


def test_publish_list_download_and_revocation_over_http(tmp_path, monkeypatch):
    secret = secrets.token_urlsafe(32)
    monkeypatch.setenv('HUB_DATA_DIR', str(tmp_path / 'hub'))
    monkeypatch.setenv('HUB_SERVICE_TOKEN', secret)
    monkeypatch.setenv('ONTO_HUB_SERVICE_TOKEN', secret)
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path / 'onto')
    manager = WorkspaceManager(tmp_path / 'onto', tmp_path / 'assets')
    pid = manager.create_project('project_alpha', owner='operator_a')['id']
    allowed = True

    class Authority(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            valid = (allowed and payload['project_id'] == pid and
                     self.headers.get('X-Hub-Service-Token') == secret)
            self.send_response(200 if valid else 403)
            self.end_headers()
            self.wfile.write(json.dumps({'project_id': pid, 'subject': 'operator_a', 'allowed': valid}).encode())

    authority = ThreadingHTTPServer(('127.0.0.1', 0), Authority)
    authority_thread = threading.Thread(target=authority.serve_forever, daemon=True)
    authority_thread.start()
    monkeypatch.setenv('ONTO_AUTHZ_URL', f'http://127.0.0.1:{authority.server_port}/authorize')
    main = importlib.import_module('hub.main')
    from hub.storage import Store
    monkeypatch.setattr(main, 'store', Store(tmp_path / 'hub'))
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(main.app, log_level='error', access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    monkeypatch.setenv('ONTO_HUB_URL', f'http://127.0.0.1:{port}')
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(.02)
        assert server.started
        first = knowledge_hub.publish(manager, pid, 'request-a', 'synthetic', 'operator_a')
        second = knowledge_hub.publish(manager, pid, 'request-a', 'synthetic', 'operator_a')
        assert first == second
        listing = knowledge_hub.call('GET', f'/api/projects/{pid}/releases', 'synthetic')
        assert listing['total'] == 1
        destination = tmp_path / 'download.zip'
        knowledge_hub.download(pid, first['release_id'], 'synthetic', destination)
        assert knowledge_hub.file_digest(destination) == first['package_digest']
        knowledge_hub.sync_resources(manager, pid)
        allowed = False
        try:
            knowledge_hub.download(pid, first['release_id'], 'synthetic', destination)
        except WorkspaceError as exc:
            assert exc.status == 403
        else:
            raise AssertionError('Revoked access was accepted')
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()
        authority.shutdown()
        authority.server_close()
        authority_thread.join(5)
