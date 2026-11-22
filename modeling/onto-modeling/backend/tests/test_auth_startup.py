"""Onto starts after live Auth, using Consumer's /status fallback policy."""

import asyncio
import json
import urllib.error
from unittest import mock

from app import auth_startup


class Response:
    status = 200

    def __init__(self, body=b""):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, *_):
        return self.body


def test_auth_health_and_status_ready(monkeypatch):
    monkeypatch.setenv("ONTO_WEB_AUTH_URL", "https://auth.example.com")
    status = json.dumps({"schemaVersion": 1, "service": "auth", "state": "ready"}).encode()
    with mock.patch.object(auth_startup.urllib.request, "urlopen",
                           side_effect=[Response(), Response(status)]) as request:
        assert auth_startup.available()
    assert request.call_count == 2


def test_legacy_health_without_status_is_accepted(monkeypatch):
    monkeypatch.setenv("ONTO_WEB_AUTH_URL", "https://auth.example.com")
    with mock.patch.object(auth_startup.urllib.request, "urlopen",
                           side_effect=[Response(), urllib.error.HTTPError("/status", 404, "", {}, None)]):
        assert auth_startup.available()


def test_wait_retries_until_auth_is_live(monkeypatch):
    monkeypatch.setenv("ONTO_WEB_AUTH_URL", "https://auth.example.com")
    with mock.patch.object(auth_startup, "available", side_effect=[False, False, True]) as check, \
         mock.patch.object(auth_startup.asyncio, "sleep", new=mock.AsyncMock()):
        asyncio.run(auth_startup.wait_for_auth())
    assert check.call_count == 3
