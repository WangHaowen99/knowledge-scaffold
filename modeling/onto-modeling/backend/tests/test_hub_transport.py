"""Loopback HTTP integration with real Hub transport and a synthetic account service."""
import importlib
import json
import os
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import uvicorn

from app import config
from app.services import knowledge_hub, hub_packages
from app.services.workspace import WorkspaceManager, WorkspaceError

pytest.importorskip('hub')


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
        def do_GET(self):
            self.send_response(200 if allowed else 401)
            self.end_headers()
            self.wfile.write(json.dumps({'sub': 'operator_a'}).encode())

    authority = ThreadingHTTPServer(('127.0.0.1', 0), Authority)
    authority_thread = threading.Thread(target=authority.serve_forever, daemon=True)
    authority_thread.start()
    monkeypatch.setenv('HUB_AUTH_URL', f'http://127.0.0.1:{authority.server_port}')
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
        issuer = f'http://127.0.0.1:{authority.server_port}'
        subject = json.dumps([issuer, 'operator_a'])
        first = hub_packages.publish(manager, pid, 'request-a', 'synthetic', subject)
        second = hub_packages.publish(manager, pid, 'request-a', 'synthetic', subject)
        assert first == second
        token_id = '1' * 32
        hub_packages.register_delegation(pid, token_id, 'synthetic', issuer, 'operator_a')
        delegated = hub_packages.publish(
            manager, pid, 'request-mcp', '', subject,
            delegated={'token_id': token_id, 'issuer': issuer, 'sub': 'operator_a'})
        assert delegated['version'] == '0.1.2' and delegated['status'] == 'committed'
        resources = knowledge_hub.sync_resources(manager, pid, 'synthetic', [])
        assert resources['ontology_id'] == first['ontology_id']
        listing = hub_packages.call('GET', f"/api/packages/{first['ontology_id']}/releases", 'synthetic')
        assert listing['total'] == 2
        destination = tmp_path / 'download.zip'
        hub_packages.download(first['ontology_id'], first['release_id'], 'synthetic', destination)
        assert knowledge_hub.file_digest(destination) == first['package_digest']
        allowed = False
        try:
            hub_packages.download(first['ontology_id'], first['release_id'], 'synthetic', destination)
        except WorkspaceError as exc:
            assert exc.status == 401
        else:
            raise AssertionError('Revoked access was accepted')
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()
        authority.shutdown()
        authority.server_close()
        authority_thread.join(5)
