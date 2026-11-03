"""旧路径兼容中间件（API 契约 1.2、第 6 章；方案 A14）：改写目标与 v1 路由一致、弃用头、
错误体带 error、已删接口 410、永久保留的旧路径照常。数据全部合成。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Match

from app.api import compat, deps
from app.main import app
from app.services import auth
from app.services.workspace import WorkspaceManager

SAMPLE_BODIES = {
    "/api/projects/p1/studio/draft": {"path": "ontology/object/订单.md", "content": "x"},
    "/api/projects/p1/studio/submit": {"path": "ontology/object/订单.md"},
    "/api/projects/p1/session/messages": {"text": "hi", "conversation_id": "0123456789ab"},
    "/api/admin/trash/restore": {"name": "demo-1"},
}


def _sample(rule: compat.Rule) -> str:
    pattern = rule.pattern.pattern.strip("^$")
    pattern = re.sub(r"\(\?P<(\w+)>\[\^/\]\+\)", lambda m: f"{m.group(1)}1" if m.group(1) != "id" else "p1", pattern)
    return pattern.replace("(?:/.*)?", "/sets")


def _route_for(method: str, path: str):
    scope = {"type": "http", "method": method, "path": path, "root_path": ""}
    for route in app.routes:
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return route
    return None


@pytest.mark.parametrize("rule", compat.RULES, ids=lambda rule: f"{rule.method} {rule.pattern.pattern}")
def test_every_rewrite_target_is_a_registered_v1_route(rule):
    path = _sample(rule)
    query = "scope=workspace&path=ontology/object/订单.md" if "export" in path or "draft" in path or "element" in path else "path=reports/r.md&path=x"
    outcome = compat.resolve(rule.method, path, query, SAMPLE_BODIES.get(path))
    assert outcome is not None, path
    if outcome[0] == "removed":
        return
    _, method, target = outcome
    assert target.startswith("/api/v1/"), target
    assert _route_for(method, target) is not None, f"{rule.method} {path} → {method} {target} 没有对应的 v1 路由"


def test_permanent_paths_are_not_rewritten():
    assert compat.match("GET", "/api/health") is None
    assert compat.match("POST", "/api/onto-mcp/p1/read/mcp") is None
    assert compat.match("GET", "/api/v1/projects") is None


def test_old_studio_paths_map_to_entry_ids():
    assert compat.entry_id_for_path("ontology/object/订单.md") == "object:订单"
    assert compat.entry_id_for_path("ontology/logic/创建订单.md") == "operation:创建订单"
    assert compat.entry_id_for_path("ontology/scripts/calc.py") == "script:calc"


# ---- 经 HTTP 的代表性旧路径 ----


@pytest.fixture()
def client(tmp_path: Path, monkeypatch) -> TestClient:
    assets = tmp_path / "assets"
    (assets / "workspace-toolkit" / "templates").mkdir(parents=True)
    (assets / "workspace-rules.md").write_text("# rules", encoding="utf-8")
    monkeypatch.setattr(deps, "workspaces", WorkspaceManager(tmp_path / "data", assets))
    monkeypatch.setattr(auth, "ENABLED", False)
    return TestClient(app)


def _deprecated(response, successor: str) -> None:
    assert response.headers["deprecation"] == "true"
    assert response.headers["sunset"] == compat.SUNSET
    assert response.headers["link"] == f"<{successor}>; rel=\"successor-version\""


def test_legacy_project_paths_reach_v1_with_deprecation_headers(client):
    created = client.post("/api/projects", json={"name": "兼容演示", "goal": "合成目标"})
    assert created.status_code == 201
    _deprecated(created, "/api/v1/projects")
    pid = created.json()["id"]

    listed = client.get("/api/projects")
    assert listed.status_code == 200 and listed.json()["items"][0]["id"] == pid
    _deprecated(listed, "/api/v1/projects")

    tree = client.get(f"/api/projects/{pid}/files")
    _deprecated(tree, f"/api/v1/projects/{pid}/files/tree")
    assert "tree" in tree.json()

    # v1 的响应形状，不做兼容；版本列表来自同一个处理器
    assert client.get(f"/api/projects/{pid}/versions").json() == client.get(f"/api/v1/projects/{pid}/versions").json()


def test_legacy_error_body_carries_error_field(client):
    created = client.post("/api/v1/projects", json={"name": "错误体", "goal": "合成目标"}).json()
    missing = client.get(f"/api/projects/{created['id']}/file", params={"path": "ontology/object/不存在.md"})
    assert missing.status_code == 404
    body = missing.json()
    assert body["code"] == "file_not_found" and body["error"] == body["message"]
    assert body["trace_id"]
    # 同一个错误经 v1 路径没有 error 字段
    direct = client.get(f"/api/v1/projects/{created['id']}/files/content", params={"path": "ontology/object/不存在.md"})
    assert "error" not in direct.json() and direct.json()["code"] == "file_not_found"


@pytest.mark.parametrize(
    "method,path,successor",
    [
        ("GET", "/api/projects/p1/eval/sets", None),
        ("POST", "/api/projects/p1/studio/lease", "/api/v1/projects/p1/scope"),
        ("GET", "/api/projects/p1/export?scope=package", "/api/v1/projects/p1/releases"),
        ("GET", "/api/projects/p1/export", None),
        ("GET", "/api/settings/assets", "/api/v1/ops/core-default"),
        ("POST", "/api/projects/p1/package/solver-validations", "/api/v1/projects/p1/solver/receipts"),
        ("GET", "/api/admin/modeling-cleanups", None),
    ],
)
def test_removed_endpoints_return_410(client, method, path, successor):
    response = client.request(method, path)
    assert response.status_code == 410
    body = response.json()
    assert body["code"] == "endpoint_removed" and body["details"]["successor"] == successor
    assert body["error"] == body["message"]
    assert response.headers["deprecation"] == "true"


def test_health_stays_as_is(client):
    response = client.get("/api/health")
    assert response.status_code == 200 and set(response.json()) == {"ok", "agent_sdk", "drawio"}
    assert "deprecation" not in response.headers
    assert client.get("/api/health/live").json()["status"] == "ok"


def test_validation_errors_are_invalid_argument(client):
    response = client.post("/api/v1/projects", content=b"[]", headers={"content-type": "application/json"})
    assert response.status_code in (400, 422)
    assert response.json()["code"] == "invalid_argument"


def test_unknown_v1_path_is_not_found(client):
    response = client.get("/api/v1/nope")
    assert response.status_code == 404 and response.json()["code"] == "not_found"
