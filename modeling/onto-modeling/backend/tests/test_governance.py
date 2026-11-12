"""治理平面（详设 4.10、方案 E3～E8）：配置注册表、审计字段、存活与就绪、角色矩阵、运维监听、维护模式、
可观测。用户名、项目与令牌全部合成。
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.governance import config_registry, context, health, ops_listener, ops_state, telemetry
from app.main import app
from app.ops_app import ops_app
from app.routers import admin, ops_control
from app.services import audit, auth
from app.services.workspace import WorkspaceManager

ADMIN, USER, OPS = "operator_admin", "operator_user", "operator_ops"
TOKENS = {
    "admin-token": {"sub": ADMIN, "role": "admin"},
    "user-token": {"sub": USER, "role": "user"},
    "ops-token": {"sub": OPS, "role": "ops"},
}
RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
CLIENT_IP = "192.0.2.10"  # NOSONAR(S1313) 保留地址段 TEST-NET-1 的合成客户端地址


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def manager(tmp_path: Path, monkeypatch) -> WorkspaceManager:
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "BACKUP_DIR", "")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assets = tmp_path / "assets"
    (assets / "resource-template").mkdir(parents=True)
    (assets / "workspace-rules.md").write_text("# rules", encoding="utf-8")
    instance = WorkspaceManager(data, assets)
    monkeypatch.setattr(deps, "workspaces", instance)
    return instance


@pytest.fixture()
def auth_on(monkeypatch):
    monkeypatch.setattr(auth, "ENABLED", True)
    monkeypatch.setattr(auth, "verify_access_token", lambda token: TOKENS.get(token))
    monkeypatch.setattr(auth, "check_access_token", lambda token: (TOKENS.get(token), "" if token in TOKENS else "invalid"))


@pytest.fixture()
def main_client(manager, auth_on) -> TestClient:
    return TestClient(app)


@pytest.fixture()
def ops_client(manager, auth_on) -> TestClient:
    return TestClient(ops_app)


def _audit_entries() -> list[dict]:
    return [raw for _index, raw in audit.read_all()]


# ---- E5 配置注册表 ----


def test_every_backend_env_var_is_declared():
    """后端代码里读到的 ONTO_WEB_* 都在注册表里登记（来源、校验、生效时机）。"""
    app_dir = Path(config.BASE_DIR) / "app"
    used = set()
    for path in app_dir.rglob("*.py"):
        used |= set(re.findall(r"ONTO_WEB_[A-Z_]+", path.read_text(encoding="utf-8")))
    internal = {ops_listener.ENABLE_ENV, "ONTO_WEB_"}
    declared = {item["key"] for item in config_registry.items()}
    assert used - internal - declared == set()
    for item in config_registry.items():
        assert item["effect"] in config_registry.EFFECTS and item["validation"]


def test_registry_precedence_validation_and_secret_refs(manager, monkeypatch):
    monkeypatch.setenv("ONTO_WEB_MAX_SESSIONS", "500")  # 超出 1–64：告警并回落到默认值
    value, source = config_registry.resolve("ONTO_WEB_MAX_SESSIONS")
    assert (value, source) == ("12", "default")
    assert "ONTO_WEB_MAX_SESSIONS" in config_registry.problems()

    # 运维改过会话池：数据目录的值优先于环境变量（UI_FIRST）
    monkeypatch.setenv("ONTO_WEB_MAX_SESSIONS", "8")
    ops_state.save_pool_config(20, 900, "queue_only")
    try:
        assert config_registry.resolve("ONTO_WEB_MAX_SESSIONS") == (20, "data_dir")
        assert config.MAX_ACTIVE_SESSIONS == 20 and config.SESSION_OVERFLOW_POLICY == "queue_only"
    finally:
        monkeypatch.setattr(config, "MAX_ACTIVE_SESSIONS", 12)
        monkeypatch.setattr(config, "SESSION_IDLE_TTL_SECONDS", 600)
        monkeypatch.setattr(config, "SESSION_OVERFLOW_POLICY", "evict_idle_then_queue")

    monkeypatch.setenv("ONTO_WEB_AUTH_ADMIN_TOKEN", "synthetic-admin-token-value")
    secret = next(item for item in config_registry.items() if item["key"] == "ONTO_WEB_AUTH_ADMIN_TOKEN")
    assert secret["secret"] and secret["value"] == "cred://env/ONTO_WEB_AUTH_ADMIN_TOKEN"
    assert "synthetic-admin-token-value" not in json.dumps(config_registry.items())


def test_plugin_config_items_register_under_pack():
    item = config_registry.ConfigItem("TRACE_MAX_CASES", "20", "int", minimum=1, maximum=200, effect="next_message",
                                      pack_value="50", precedence=("env", "pack", "default"))
    config_registry.register(item, pack="trace@0.9.1")
    try:
        found = next(entry for entry in config_registry.items() if entry["key"] == "TRACE_MAX_CASES")
        assert found["source"] == "pack" and found["pack"] == "trace@0.9.1" and found["value"] == "50"
        with pytest.raises(ValueError):
            config_registry.register(config_registry.ConfigItem("ONTO_WEB_PORT"), pack="trace@0.9.1")
    finally:
        config_registry.unregister_pack("trace@0.9.1")


def test_config_registry_endpoint_roles(main_client):
    body = main_client.get("/api/v1/admin/config-registry", headers=_h("admin-token"), params={"effect": "restart"}).json()
    assert body["precedence"] == ["env", "data_dir", "pack", "default"]
    assert body["items"] and {item["effect"] for item in body["items"]} == {"restart"}
    assert main_client.get("/api/v1/admin/config-registry", headers=_h("ops-token")).status_code == 200
    assert main_client.get("/api/v1/admin/config-registry", headers=_h("user-token")).status_code == 403


# ---- E4 审计：响应之后记，带状态码、trace id、IP ----


def test_write_audit_after_response_with_status_trace_and_ip(main_client, manager):
    response = main_client.post("/api/v1/projects", headers={**_h("user-token"), "X-Forwarded-For": CLIENT_IP},
                                json={"name": "审计演示", "goal": "合成目标"})
    assert response.status_code == 201
    entry = next(e for e in _audit_entries() if e.get("path") == "/api/v1/projects")
    assert entry["status"] == 201 and entry["actor"] == USER and entry["actor_kind"] == "user"
    assert entry["trace_id"] == response.headers["x-trace-id"] and entry["ip"] == CLIENT_IP
    assert entry["type"] == "write" and entry["action"] == "http.post"
    assert "合成目标" not in json.dumps(entry, ensure_ascii=False)  # 不记请求正文

    denied = main_client.post("/api/v1/ops/backups", headers=_h("user-token"))
    assert denied.status_code == 404  # 运维面默认不在业务面上
    failed = main_client.request("DELETE", "/api/v1/projects/no-such-project", headers=_h("user-token"),
                                 json={"confirm_name": "x"})
    entry = next(e for e in _audit_entries() if e.get("path") == "/api/v1/projects/no-such-project")
    assert entry["status"] == failed.status_code == 404


def test_local_mode_audits_as_local(manager, monkeypatch):
    monkeypatch.setattr(auth, "ENABLED", False)
    TestClient(app).post("/api/v1/projects", json={"name": "本地演示", "goal": "合成"})
    entry = next(e for e in _audit_entries() if e.get("path") == "/api/v1/projects")
    assert entry["actor"] == "local" and entry["status"] == 201


def test_audit_event_categories_reach_ops_listing(ops_client, manager):
    audit.record(type="commit", action="agent.commit", actor="agent", actor_kind="agent", project_id="p1",
                 summary="2 个文件", trailer={"Onto-Source": "agent"})
    audit.record(type="plugin", action="pack.enable", actor=OPS, project_id="p1", summary="trace@0.9.1")
    audit.log(USER, "POST", "/api/v1/auth/login", {"action": "auth.login", "result": "ok"})
    body = ops_client.get("/api/v1/ops/audit", headers=_h("ops-token"), params={"type": "commit,plugin,auth"}).json()
    kinds = {(item["type"], item["action"]) for item in body["items"]}
    assert kinds == {("commit", "agent.commit"), ("plugin", "pack.enable"), ("auth", "auth.login")}
    commit = next(item for item in body["items"] if item["type"] == "commit")
    assert commit["trailer"] == {"Onto-Source": "agent"} and commit["actor_kind"] == "agent"


def test_audit_rotates_daily_and_keeps_legacy_file(manager):
    legacy = Path(config.DATA_DIR) / "audit.jsonl"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps({"ts": 1, "user": "old", "method": "POST", "path": "/api/projects"}) + "\n")
    audit.record(type="write", action="http.post", actor=USER)
    assert audit._file().name == f"audit-{time.strftime('%Y-%m-%d', time.gmtime())}.jsonl"
    entries = audit.read_all()
    assert [raw.get("actor") or raw.get("user") for _i, raw in entries] == [USER, "old"]


# ---- E7 存活与就绪 ----


def test_live_and_ready(main_client, monkeypatch):
    live = main_client.get("/api/health/live").json()
    assert live["status"] == "ok" and RFC3339.match(live["started_at"])
    assert main_client.get("/api/health").json()["ok"] is True  # 旧接口不变

    monkeypatch.setattr(health, "_cache", None)
    monkeypatch.setattr(health, "_startup_done", False)
    assert main_client.get("/api/health/ready").status_code == 503  # 启动恢复没做完

    monkeypatch.setattr(health, "_startup_done", True)
    monkeypatch.setattr(health, "_checks", {
        "git": ("git 可执行", lambda: ("ok", None), True),
        "sandbox_gateway": ("GET /v1/status", lambda: ("warn", "未连接"), False),
    })
    monkeypatch.setattr(health, "_cache", None)
    anonymous = main_client.get("/api/health/ready")
    assert anonymous.status_code == 200 and anonymous.json() == {
        "status": "degraded", "checks": [{"name": "git", "status": "ok"}, {"name": "sandbox_gateway", "status": "warn"}],
    }
    detailed = main_client.get("/api/health/ready", headers=_h("ops-token")).json()
    assert detailed["checks"][1]["detail"] == "未连接" and "latency_ms" in detailed["checks"][0]

    monkeypatch.setattr(health, "_cache", None)
    monkeypatch.setitem(health._checks, "git", ("git 可执行", lambda: ("fail", "找不到 git"), True))
    failing = main_client.get("/api/health/ready")
    assert failing.status_code == 503 and failing.json()["status"] == "not_ready"


def test_ready_lists_contract_dependencies():
    names = {item["name"] for item in health.probe(force=True)}
    assert {"model_gateway", "auth_server", "sandbox_gateway", "connector_gateway", "disk", "git", "assembly"} <= names


def test_dockerfile_healthcheck_uses_live():
    dockerfile = (Path(config.PACKAGE_ROOT) / "Dockerfile").read_text(encoding="utf-8")
    healthcheck = dockerfile.split("HEALTHCHECK", 1)[1].split("\n\n", 1)[0]
    assert "/api/health/live" in healthcheck and "/api/health/ready" not in healthcheck


# ---- E3 角色矩阵：admin、ops、user × 业务面、运维面 ----

ROLE_MATRIX = [
    # (面, 方法, 路径, {角色: 期望状态})
    ("business", "GET", "/api/v1/projects", {"user": 200, "admin": 200, "ops": 200}),
    ("business", "GET", "/api/v1/admin/settings/model", {"user": 403, "admin": 200, "ops": 403}),
    ("business", "GET", "/api/v1/admin/users", {"user": 403, "admin": 200, "ops": 200}),
    ("business", "PATCH", "/api/v1/admin/users/operator_user", {"user": 403, "admin": 200, "ops": 403}),
    ("business", "GET", "/api/v1/admin/service-tokens", {"user": 403, "admin": 200, "ops": 403}),
    ("ops", "GET", "/api/v1/ops/overview", {"user": 403, "admin": 200, "ops": 200}),
    ("ops", "GET", "/api/v1/ops/audit", {"user": 403, "admin": 200, "ops": 200}),
    ("ops", "GET", "/api/v1/ops/maintenance", {"user": 403, "admin": 200, "ops": 200}),
    ("ops", "POST", "/api/v1/ops/sessions/drain", {"user": 403, "admin": 403, "ops": 202}),
    ("ops", "PUT", "/api/v1/ops/sessions/config", {"user": 403, "admin": 403, "ops": 200}),
    ("ops", "POST", "/api/v1/ops/health/probe", {"user": 403, "admin": 403, "ops": 200}),
    ("ops", "POST", "/api/v1/ops/backups", {"user": 403, "admin": 202, "ops": 202}),  # 契约：admin 也可
]
_BODIES = {
    "/api/v1/admin/users/operator_user": {"role": "user"},
    "/api/v1/ops/sessions/drain": {"max_wait_s": 5},
    "/api/v1/ops/sessions/config": {"max_sessions": 12, "idle_ttl_s": 600, "overflow_policy": "evict_idle_then_queue"},
}


@pytest.mark.parametrize("plane,method,path,expected", ROLE_MATRIX)
def test_role_matrix(plane, method, path, expected, manager, auth_on, monkeypatch):
    monkeypatch.setattr(admin, "auth_admin_available", lambda: True)
    monkeypatch.setattr(admin, "_auth_admin_request", lambda m, p, payload=None: (200, (
        {"users": [{"username": USER, "role": "user"}, {"username": ADMIN, "role": "admin"}]} if m == "GET"
        else {"user": {"username": USER, "role": "user"}})))
    monkeypatch.setattr(config, "AUTH_ADMIN_TOKEN", "synthetic-admin-token")
    monkeypatch.setattr(ops_state, "start_drain", lambda wait: {"online": 0, "running": 0, "autobuild_running": 0, "eta_ms": 0})
    monkeypatch.setattr(config, "MAX_ACTIVE_SESSIONS", config.MAX_ACTIVE_SESSIONS)
    monkeypatch.setattr(config, "SESSION_IDLE_TTL_SECONDS", config.SESSION_IDLE_TTL_SECONDS)
    client = TestClient(ops_app if plane == "ops" else app)
    for role, status in expected.items():
        response = client.request(method, path, headers=_h(f"{role}-token"), json=_BODIES.get(path))
        assert response.status_code == status, (role, response.text)
        if status == 403:
            assert response.json()["code"] == "forbidden"


def test_admin_and_ops_only_see_project_metadata(manager, auth_on):
    client = TestClient(app)
    created = client.post("/api/v1/projects", headers=_h("user-token"), json={"name": "元数据演示", "goal": "合成目标"})
    project_id = created.json()["id"]
    for token in ("admin-token", "ops-token"):
        meta = client.get(f"/api/v1/projects/{project_id}", headers=_h(token))
        assert meta.status_code == 200 and "goal" not in meta.json()  # ProjectMetaView：没有目标、资料、产物
        files = client.get(f"/api/v1/projects/{project_id}/files/tree", headers=_h(token))
        assert files.status_code == 403 and files.json()["code"] == "project_owner_only"


def test_unknown_role_claim_is_treated_as_user(manager, monkeypatch):
    from app.governance import roles

    assert roles.roles_of({"sub": "x", "role": "superuser"}) == {"user"}
    assert roles.roles_of({"sub": "x", "role": "ops"}) == {"user", "ops"}
    assert roles.roles_of(None) == {"user", "admin", "ops"}


# ---- E8 运维监听 ----


def test_ops_plane_is_404_on_main_unless_enabled(main_client, monkeypatch):
    response = main_client.get("/api/v1/ops/overview", headers=_h("ops-token"))
    assert response.status_code == 404 and response.json()["code"] == "not_found"
    legacy = main_client.get("/api/admin/overview", headers=_h("ops-token"))  # 旧路径改写后同样 404
    assert legacy.status_code == 404
    monkeypatch.setattr(config, "OPS_ON_MAIN", True)
    assert main_client.get("/api/v1/ops/overview", headers=_h("ops-token")).status_code == 200


def test_ops_app_requires_jwt_and_serves_metrics_and_health(ops_client):
    assert ops_client.get("/api/v1/ops/overview").status_code == 401
    assert ops_client.get("/api/v1/ops/overview", headers=_h("ops-token")).status_code == 200
    assert ops_client.get("/api/health/live").json()["status"] == "ok"
    telemetry.count("onto_commits_total", source="web", target="mainline")
    metrics = ops_client.get("/metrics")
    assert metrics.status_code == 200 and "onto_commits_total" in metrics.text
    assert TestClient(app).get("/metrics").status_code != 200 or "onto_commits_total" not in TestClient(app).get("/metrics").text


def test_client_config_exposes_ops_base_url(main_client, monkeypatch):
    monkeypatch.setattr(config, "OPS_BASE_URL", "https://ops.example.com")
    assert main_client.get("/api/v1/client-config").json()["ops_base_url"] == "https://ops.example.com"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_embedded_ops_listener_serves_on_its_own_port(manager, auth_on, monkeypatch):
    port = _free_port()
    monkeypatch.setattr(config, "OPS_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "OPS_PORT", port)
    monkeypatch.setenv(ops_listener.ENABLE_ENV, "1")

    async def scenario():
        import urllib.request

        handle = await ops_listener.start()
        assert handle is not None
        try:
            for _ in range(50):
                if handle[0].started:
                    break
                await asyncio.sleep(0.05)

            def fetch(path, token=None):
                request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
                if token:
                    request.add_header("Authorization", f"Bearer {token}")
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                try:
                    with opener.open(request, timeout=5) as response:
                        return response.status
                except urllib.error.HTTPError as exc:
                    return exc.code

            live = await asyncio.to_thread(fetch, "/api/health/live")
            anonymous = await asyncio.to_thread(fetch, "/api/v1/ops/maintenance")
            authorized = await asyncio.to_thread(fetch, "/api/v1/ops/maintenance", "ops-token")
            return live, anonymous, authorized
        finally:
            await ops_listener.stop(handle)

    assert asyncio.run(scenario()) == (200, 401, 200)


def test_listener_disabled_without_run_flag_or_port(monkeypatch):
    monkeypatch.delenv(ops_listener.ENABLE_ENV, raising=False)
    assert not ops_listener.enabled()
    monkeypatch.setenv(ops_listener.ENABLE_ENV, "1")
    monkeypatch.setattr(config, "OPS_PORT", 0)
    assert not ops_listener.enabled()


# ---- 1.13 维护模式 ----


def test_maintenance_read_only_flow(manager, auth_on, ops_client):
    main = TestClient(app)
    entered = ops_client.post("/api/v1/ops/maintenance", headers=_h("ops-token"),
                              json={"mode": "read_only", "message": "升级内核", "autobuild": "pause_at_safe_point"})
    assert entered.status_code == 201
    state = entered.json()
    assert state["active"] and state["mode"] == "read_only" and RFC3339.match(state["started_at"])
    assert [step["state"] for step in state["steps"]] == ["current", "pending", "pending", "pending"]
    assert ops_client.post("/api/v1/ops/maintenance", headers=_h("ops-token"),
                           json={"mode": "read_only", "message": "x"}).json()["code"] == "maintenance_active"

    blocked = main.post("/api/v1/projects", headers=_h("user-token"), json={"name": "维护中", "goal": "x"})
    assert blocked.status_code == 503 and blocked.json()["code"] == "maintenance_read_only"
    assert blocked.json()["details"]["message"] == "升级内核"
    assert main.get("/api/v1/projects", headers=_h("user-token")).status_code == 200  # 读照常
    banner = main.get("/api/v1/client-config").json()["maintenance"]
    assert banner["mode"] == "read_only" and banner["message"] == "升级内核"

    # 第 2 步排空：没有在线会话，第 3 步直接可进；再到第 4 步恢复，最后退出
    advanced = ops_client.post("/api/v1/ops/maintenance/advance", headers=_h("ops-token")).json()
    assert [step["state"] for step in advanced["steps"]][:2] == ["done", "current"]
    for _ in range(2):
        advanced = ops_client.post("/api/v1/ops/maintenance/advance", headers=_h("ops-token")).json()
    assert advanced["steps"][-1]["state"] == "current"
    assert ops_client.delete("/api/v1/ops/maintenance", headers=_h("ops-token")).json() == {"active": False}
    assert main.post("/api/v1/projects", headers=_h("user-token"), json={"name": "维护后", "goal": "x"}).status_code == 201
    assert ops_client.delete("/api/v1/ops/maintenance", headers=_h("ops-token")).json()["code"] == "maintenance_not_active"
    actions = [raw.get("action") for _i, raw in audit.read_all() if raw.get("type") == "ops"]
    assert {"ops.maintenance_enter", "ops.maintenance_advance", "ops.maintenance_exit"} <= set(actions)


def test_maintenance_drain_step_blocks_while_sessions_online(manager, monkeypatch):
    ops_state.enter_maintenance("reject_new_sessions", "合成维护", OPS)
    try:
        monkeypatch.setattr(ops_state, "online_counts", lambda: {"online": 2, "running": 1, "autobuild_running": 0})
        monkeypatch.setattr(ops_state, "start_drain", lambda wait: {})
        ops_state.advance_maintenance(OPS)
        with pytest.raises(ops_state.MaintenanceError) as caught:
            ops_state.advance_maintenance(OPS)
        assert caught.value.code == "maintenance_step_blocked" and caught.value.details == {"step": "drain", "remaining": 2}
    finally:
        ops_state.exit_maintenance(OPS)


def test_maintenance_rejects_new_sessions_but_not_writes(manager, auth_on, monkeypatch):
    from app.governance.http import reject_new_session
    from app.api.errors import ApiError

    ops_state.enter_maintenance("reject_new_sessions", "合成维护", OPS)
    try:
        with pytest.raises(ApiError) as caught:
            reject_new_session("project-without-session")
        assert caught.value.status == 503 and caught.value.code == "maintenance_no_new_sessions"
        created = TestClient(app).post("/api/v1/projects", headers=_h("user-token"), json={"name": "照常写", "goal": "x"})
        assert created.status_code == 201
    finally:
        ops_state.exit_maintenance(OPS)
    reject_new_session("project-without-session")  # 退出后不再拒绝


def test_pool_config_and_breaker(ops_client, monkeypatch):
    monkeypatch.setattr(config, "MAX_ACTIVE_SESSIONS", config.MAX_ACTIVE_SESSIONS)
    monkeypatch.setattr(config, "SESSION_IDLE_TTL_SECONDS", config.SESSION_IDLE_TTL_SECONDS)
    monkeypatch.setattr(config, "SESSION_OVERFLOW_POLICY", config.SESSION_OVERFLOW_POLICY)
    bad = ops_client.put("/api/v1/ops/sessions/config", headers=_h("ops-token"),
                         json={"max_sessions": 99, "idle_ttl_s": 600, "overflow_policy": "evict_idle_then_queue"})
    assert bad.status_code == 400 and "max_sessions" in bad.json()["details"]["fields"]
    saved = ops_client.put("/api/v1/ops/sessions/config", headers=_h("ops-token"),
                           json={"max_sessions": 4, "idle_ttl_s": 300, "overflow_policy": "queue_only"}).json()
    assert saved["config"] == {"max_sessions": 4, "idle_ttl_s": 300, "overflow_policy": "queue_only"}
    assert config.MAX_ACTIVE_SESSIONS == 4

    assert ops_client.post("/api/v1/ops/packs/trace/0.9.1/breaker", headers=_h("ops-token"),
                           json={"action": "open"}).json()["code"] == "pack_version_not_found"
    monkeypatch.setattr(ops_control, "pack_lookup", lambda name, version: {"name": name, "version": version})
    opened = ops_client.post("/api/v1/ops/packs/trace/0.9.1/breaker", headers=_h("ops-token"),
                             json={"action": "open", "reason": "合成故障"}).json()
    assert opened["breaker"] == "open" and ops_state.breaker_state("trace", "0.9.1") == "open"
    half = ops_client.post("/api/v1/ops/packs/trace/0.9.1/breaker", headers=_h("ops-token"), json={"action": "close"})
    assert half.json()["breaker"] == "half_open"
    again = ops_client.post("/api/v1/ops/packs/trace/0.9.1/breaker", headers=_h("ops-token"), json={"action": "open"})
    assert again.json()["breaker"] == "open"


# ---- E6 可观测：公共标签、结构化日志、链路、指标 ----


def test_json_logs_carry_common_labels_and_redact_secrets(manager, tmp_path):
    import logging

    record = logging.LogRecord("onto.test", logging.WARNING, __file__, 1,
                               "调用网关失败 token=synthetic-token-value", (), None)
    record.event = "gateway.call"
    with context.bind(project="p1", session="c1", exec="e1", changeset="cs-1", pack="core@1.0.0", node="core.object",
                      trace_id="abc123"):
        line = json.loads(telemetry.JsonFormatter().format(record))
    assert line["level"] == "WARN" and line["event"] == "gateway.call"
    assert line["fields"] == {**line["fields"], "project": "p1", "session": "c1", "exec": "e1", "changeset": "cs-1",
                              "pack": "core@1.0.0", "node": "core.object", "trace_id": "abc123"}
    assert "synthetic-token-value" not in line["message"]


def test_request_trace_spans_and_metrics(manager, auth_on, ops_client):
    telemetry.reset_metrics()
    main = TestClient(app)
    response = main.post("/api/v1/projects", headers=_h("user-token"), json={"name": "链路演示", "goal": "x"})
    trace_id = response.headers["x-trace-id"]
    trace = ops_client.get(f"/api/v1/ops/traces/{trace_id}", headers=_h("ops-token")).json()
    assert trace["trace_id"] == trace_id and trace["spans"][0]["name"].startswith("POST /api/v1/projects")
    listed = ops_client.get("/api/v1/ops/traces", headers=_h("ops-token")).json()["items"]
    assert any(item["trace_id"] == trace_id for item in listed)
    assert ops_client.get("/api/v1/ops/traces/missing", headers=_h("ops-token")).json()["code"] == "trace_not_found"

    telemetry.record_turn("p1", {"duration_ms": 4000, "duration_api_ms": 2500, "usage": {"output_tokens": 300}})
    cards = {card["id"]: card for card in ops_client.get("/api/v1/ops/metrics", headers=_h("ops-token")).json()["cards"]}
    assert {"pool", "turn_duration", "model_latency_p95", "output_tokens", "model_fail_rate", "guard_denies",
            "check_failures", "merges", "reconnects"} <= set(cards)
    assert "collections" not in cards and all("note" not in card for card in cards.values())  # 证据源本期不开放
    assert cards["output_tokens"]["value"] == 300 and len(cards["output_tokens"]["series"]) == 12
    assert "onto_http_requests_total" in telemetry.prometheus_text()


def test_change_tx_publishes_commit_metric(manager):
    telemetry.reset_metrics()
    meta = manager.create_project("指标演示", owner=USER)
    manager.write_file(meta["id"], "notes.md", "合成\n", author=USER)
    assert 'onto_commits_total{source="web",target="mainline"} 1' in telemetry.prometheus_text()


def test_diagnostics_bundle_is_redacted(ops_client, monkeypatch):
    monkeypatch.setenv("ONTO_WEB_AUTH_ADMIN_TOKEN", "synthetic-admin-token-value")
    created = ops_client.post("/api/v1/ops/diagnostics", headers=_h("ops-token"), json={})
    assert created.status_code == 201
    diag = created.json()
    listed = ops_client.get("/api/v1/ops/diagnostics", headers=_h("ops-token")).json()["items"]
    assert [item["id"] for item in listed] == [diag["id"]]
    archive = ops_client.get(f"/api/v1/ops/diagnostics/{diag['id']}/download", headers=_h("ops-token"))
    assert archive.status_code == 200 and b"synthetic-admin-token-value" not in archive.content
    assert ops_client.get("/api/v1/ops/diagnostics/diag-missing/download",
                          headers=_h("ops-token")).json()["code"] == "diagnostics_not_found"


def test_versions_and_audit_export(ops_client):
    audit.record(type="ops", action="ops.drain", actor=OPS)
    versions = ops_client.get("/api/v1/ops/versions", headers=_h("ops-token")).json()["items"]
    assert {"onto-modeling", "kernel_api", "page_api", "metamodel"} <= {item["component"] for item in versions}
    exported = ops_client.get("/api/v1/ops/audit/export", headers=_h("ops-token"), params={"format": "csv"})
    assert exported.status_code == 200 and "ops.drain" in exported.text
    assert exported.headers["content-disposition"].startswith("attachment")


def test_agent_round_commits_are_audited(manager):
    from app.services.workspace import _git

    meta = manager.create_project("agent 提交", owner=USER)
    root = manager.project_root(meta["id"])
    base = _git(root, "rev-parse", "HEAD").strip()
    (root / "research" / "a.md").parent.mkdir(parents=True, exist_ok=True)
    (root / "research" / "a.md").write_text("合成\n", encoding="utf-8")
    _git(root, "add", "--", "research/a.md")
    _git(root, "commit", "-q", "-m", "research: 合成\n\nOnto-Source: agent\nOnto-Node: core.research@1.0.0")
    head = _git(root, "rev-parse", "HEAD").strip()
    audit.record_agent_commits(meta["id"], root, base, head, {"added": ["research/a.md"], "modified": [], "deleted": []})
    entry = next(raw for _i, raw in audit.read_all() if raw.get("action") == "agent.commit")
    assert entry["type"] == "commit" and entry["actor_kind"] == "agent" and entry["project_id"] == meta["id"]
    assert entry["trailer"] == {"Onto-Source": "agent", "Onto-Node": "core.research@1.0.0"}
    assert "commits=1" in entry["summary"] and "files=1" in entry["summary"]


def test_unclean_exit_is_reported_on_next_start(manager, ops_client, monkeypatch):
    from app.governance import lifecycle
    from app.services import startup_report

    monkeypatch.setattr(config, "OPS_ON_MAIN", False)
    lifecycle.mark_started()  # 上一个进程：启动后没走正常停机
    report = startup_report.run(manager)
    assert report["last_exit"]["reason"] == "unclean"
    body = ops_client.get("/api/v1/ops/startup-report", headers=_h("ops-token")).json()
    assert body["last_exit"]["reason"] == "unclean" and RFC3339.match(body["last_exit"]["at"])
    lifecycle.mark_stopped()
    assert startup_report.run(manager)["last_exit"]["reason"] == "shutdown"
