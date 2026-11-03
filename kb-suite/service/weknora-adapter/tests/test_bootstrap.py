"""deploy/compose/weknora/bootstrap.py against a fake WeKnora (synthetic data)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "deploy" / "compose" / "weknora" / "bootstrap.py"
ENV = {"WEKNORA_ADMIN_EMAIL": "owner@example.com", "WEKNORA_ADMIN_PASSWORD": "synthetic",
       "KBS_LLM_BASE_URL": "https://llm.example.com/v1", "KBS_LLM_API_KEY": "synthetic"}


@pytest.fixture(scope="module")
def bootstrap():
    spec = importlib.util.spec_from_file_location("weknora_bootstrap", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeWeKnora:
    def __init__(self) -> None:
        self.keys: dict[int, str] = {}
        self.models: list[dict] = []
        self.fail_embedding = False
        self.next_id = 1

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else {}
        if path == "/health":
            return httpx.Response(200)
        if path == "/api/v1/auth/login":
            return httpx.Response(200, json={"token": "t", "active_tenant": {"id": 7}})
        if path == "/api/v1/tenants/7/api-keys" and method == "GET":
            return httpx.Response(200, json={"data": [{"id": i, "name": n} for i, n in self.keys.items()]})
        if path == "/api/v1/tenants/7/api-keys" and method == "POST":
            self.next_id += 1
            self.keys[self.next_id] = body["name"]
            return httpx.Response(201, json={"data": {"id": self.next_id, "token": f"key-{self.next_id}"}})
        if path.startswith("/api/v1/tenants/7/api-keys/") and method == "DELETE":
            self.keys.pop(int(path.rsplit("/", 1)[1]))
            return httpx.Response(200, json={"success": True})
        if path == "/api/v1/models" and method == "GET":
            return httpx.Response(200, json={"data": self.models})
        if path == "/api/v1/models" and method == "POST":
            if body["type"] == "Embedding" and self.fail_embedding:
                return httpx.Response(400, json={"error": "bad model"})
            self.models.append({"id": f"m{len(self.models)}", "name": body["name"], "type": body["type"]})
            return httpx.Response(201, json={"data": self.models[-1]})
        return httpx.Response(404)


def _run(bootstrap, fake: FakeWeKnora, out: Path) -> int:
    with httpx.Client(base_url="https://weknora.example.com", transport=httpx.MockTransport(fake)) as client:
        return bootstrap.run(client, out, ENV)


def test_partial_failure_leaves_no_orphan_key(bootstrap, tmp_path):
    out, fake = tmp_path / "weknora.env", FakeWeKnora()
    fake.fail_embedding = True
    with pytest.raises(httpx.HTTPStatusError):
        _run(bootstrap, fake, out)
    # the key and the chat model reached the env file before the failure
    first = bootstrap._env_file(out)
    assert set(first) == {"WEKNORA_API_KEY", "WEKNORA_CHAT_MODEL_ID"} and len(fake.keys) == 1
    fake.fail_embedding = False
    assert _run(bootstrap, fake, out) == 1
    values = bootstrap._env_file(out)
    assert values["WEKNORA_API_KEY"] == first["WEKNORA_API_KEY"] and len(fake.keys) == 1
    assert len(fake.models) == 2 and out.stat().st_mode & 0o777 == 0o600
    assert _run(bootstrap, fake, out) == 0


def test_unrecorded_key_is_revoked_before_a_new_one(bootstrap, tmp_path):
    fake = FakeWeKnora()
    fake.keys = {1: "kb-gateway", 5: "someone-else"}
    _run(bootstrap, fake, tmp_path / "weknora.env")
    assert sorted(fake.keys.values()) == ["kb-gateway", "someone-else"] and 1 not in fake.keys


def test_weknora_url_is_required(bootstrap, monkeypatch, tmp_path):
    monkeypatch.delenv("WEKNORA_URL", raising=False)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("sys.argv", ["bootstrap.py", str(tmp_path / "weknora.env")])
    with pytest.raises(SystemExit, match="WEKNORA_URL"):
        bootstrap.main()
