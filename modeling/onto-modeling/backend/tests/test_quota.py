"""配额（详设 4.10.1、方案 E9、契约 SY-15、SY-16）：用量台账、限额与单独设置、发送前检查、采集频率、

管理接口与发送闸门。用户名、项目与用量全部是合成数据。
"""

from __future__ import annotations

import calendar
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app import config
from app.api import deps, errors
from app.api.errors import ApiError
from app.governance import quota
from app.routers import quotas as quota_routes
from app.services import agent_sessions, audit, auth, conversation_store
from app.services.workspace import META_FILE, WorkspaceManager
from tests.pack_helpers import make_assets

NOW = calendar.timegm((2026, 11, 15, 12, 0, 0))  # 固定时钟：月中，当天、当月、上月都好构造
DAY = 86400
RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(auth, "ENABLED", False)
    manager = WorkspaceManager(data, make_assets(tmp_path))
    monkeypatch.setattr(deps, "workspaces", manager)
    monkeypatch.setattr(agent_sessions.manager, "_sessions", {})
    monkeypatch.setattr(quota, "_service", None)
    service = quota.service()
    service.sync_interval = 0
    service.clock = lambda: NOW
    return SimpleNamespace(data=data, manager=manager, svc=service)


def _project(env, project_id: str, owner: str, name: str = "门店补货") -> str:
    root = env.manager.workspaces_dir / project_id
    root.mkdir(parents=True, exist_ok=True)
    meta = {"id": project_id, "name": name, "owner": owner, "created_at": NOW - 30 * DAY}
    (root / META_FILE).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return project_id


def _append(project_id: str, *events: dict, conversation_id: str = conversation_store.DEFAULT_ID) -> Path:
    path = conversation_store.history_path(project_id, conversation_id)
    with open(path, "a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    return path


def _usage(input_tokens: int, output_tokens: int = 0, cache_read: int = 0, ts: float = NOW) -> dict:
    """逐次记账引擎的一次模型调用。"""
    usage = {"input_tokens": input_tokens, "output_tokens": output_tokens, "cache_read_input_tokens": cache_read}
    return {"type": "model_usage", "ts": ts, "usage": usage}


def _result(input_tokens: int, output_tokens: int = 0, ts: float = NOW, cost: float = 0.0,
            per_call: bool = False) -> dict:
    event = {"type": "result", "ts": ts, "total_cost_usd": cost,
             "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}}
    if per_call:
        event["usage_per_call"] = True
    return event


def _session(state: str = "idle", engine=True, connecting: bool = False):
    return SimpleNamespace(state=state, engine=object() if engine else None, is_connecting=lambda: connecting)


# ---- 台账 ----


def test_sync_counts_usage_events_like_usage_summary(env):
    pid = _project(env, "replenish-0001", "alice")
    _append(
        pid,
        {"type": "user_message", "ts": NOW, "text": "合成需求：门店补货建模"},
        _usage(100, 20, 30),
        _usage(50, 10),
        # 逐次记账的 result 只算一轮：token 不再累加，成本照记
        _result(999, 999, cost=0.5, per_call=True),
        {"type": "session_state", "ts": NOW, "state": "idle"},
    )
    other = conversation_store.create(pid)["id"]
    _append(pid, _result(1000, 200, cost=0.25), conversation_id=other)

    assert env.svc.sync() == 4
    assert env.svc.used_today("alice") == 150 + 60 + 1200
    assert env.svc.used_month(pid) == 1410
    view = env.svc.usage_view(username="alice")
    assert view["today_tokens"] == 1410 and view["today_cost_usd"] == 0.75
    assert view["by_project_month"] == {pid: 1410}
    summary = agent_sessions.usage_summary(pid)
    assert summary["input_tokens"] + summary["output_tokens"] == 1410


def test_resync_reads_only_new_complete_lines(env):
    pid = _project(env, "replenish-0002", "alice")
    path = _append(pid, _usage(100))
    assert env.svc.sync() == 1
    assert env.svc.sync() == 0 and env.svc.used_today("alice") == 100

    _append(pid, _usage(40))
    line = json.dumps(_usage(7, ts=NOW + 1))
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line[:20])  # 正在写的半截行不入账
    assert env.svc.sync() == 1 and env.svc.used_today("alice") == 140
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line[20:] + "\n")
    assert env.svc.sync() == 1 and env.svc.used_today("alice") == 147

    # 换一个新实例（重启）重扫全部，同一个台账不重复计数
    restarted = quota.QuotaService(env.data, sync_interval=0)
    restarted.clock = lambda: NOW
    assert restarted.sync() == 0 and restarted.used_today("alice") == 147


def test_truncated_or_rewritten_file_rescans_without_double_count(env):
    pid = _project(env, "replenish-0003", "alice")
    first, second = _usage(100, ts=NOW - 10), _usage(200, ts=NOW - 9)
    path = _append(pid, first, second)
    env.svc.sync()
    assert env.svc.used_today("alice") == 300

    # 截断到只剩第一条再追加：从头重扫，已入账的不重复、也不因截断减少
    path.write_text(json.dumps(first) + "\n", encoding="utf-8")
    _append(pid, _usage(5, ts=NOW - 8))
    env.svc.sync()
    assert env.svc.used_today("alice") == 305

    # 原地改写成更长的内容：头部指纹变了，从头重扫，新事件不会被读位置错开而漏掉
    rewritten = [_usage(1_000 + n, ts=NOW - 100 + n) for n in range(5)]
    path.write_text("".join(json.dumps(event) + "\n" for event in rewritten), encoding="utf-8")
    env.svc.sync()
    assert env.svc.used_today("alice") == 305 + sum(1_000 + n for n in range(5))


def test_deleted_conversation_and_project_usage_still_counts_once(env):
    pid = _project(env, "replenish-0004", "alice")
    cid = conversation_store.create(pid)["id"]
    _append(pid, _usage(300), _result(0, 0, cost=0.1, per_call=True), conversation_id=cid)
    env.svc.sync()
    assert env.svc.used_today("alice") == 300

    conversation_store.delete(pid, cid)  # 用量事件搬进 deleted-usage.jsonl
    assert len(conversation_store.load_deleted_usage(pid)) == 2
    env.svc.sync()
    assert env.svc.used_today("alice") == 300
    assert env.svc.usage_view(project_id=pid)["today_cost_usd"] == 0.1

    env.svc.db_path.unlink()  # 台账丢了从头建：只剩留存文件也照算
    fresh = quota.QuotaService(env.data, sync_interval=0)
    fresh.clock = lambda: NOW
    assert fresh.sync() == 2 and fresh.used_today("alice") == 300

    conversation_store.purge(pid)  # 删项目不能绕开当日上限
    env.svc.sync()
    assert env.svc.used_today("alice") == 300


def test_old_events_and_direct_records(env):
    pid = _project(env, "replenish-0005", "alice")
    last_month = calendar.timegm((2026, 10, 20, 0, 0, 0))
    too_old = calendar.timegm((2026, 9, 20, 0, 0, 0))
    _append(pid, _usage(10, ts=last_month), _usage(20, ts=too_old), _usage(30, ts=NOW - 5 * DAY))
    env.svc.sync()
    assert env.svc.used_month(pid) == 30 and env.svc.used_today("alice") == 0

    env.svc.record(pid, "alice", 70, 0.02, key="turn-1")
    env.svc.record(pid, "alice", 70, 0.02, key="turn-1")  # 同一 key 只记一次
    assert env.svc.used_today("alice") == 70 and env.svc.used_month(pid) == 100


# ---- 限额 ----


def test_limits_defaults_and_corrupt_config(env):
    limits = env.svc.limits()
    assert limits["per_user"] == {"concurrent_sessions": 3, "daily_tokens": 2_000_000,
                                  "collections_per_source_per_hour": 6}
    assert limits["per_project"] == {"online_engines": 1, "monthly_tokens": 20_000_000, "snapshot_git_mb": 20}
    env.svc.limits_path.parent.mkdir(parents=True, exist_ok=True)
    env.svc.limits_path.write_text("{坏的", encoding="utf-8")
    assert env.svc.limits()["per_user"]["daily_tokens"] == 2_000_000
    saved = env.svc.set_limits({"daily_tokens": 10}, {"online_engines": 9, "monthly_tokens": 0})
    assert saved["per_user"]["daily_tokens"] == 10 and saved["per_project"]["online_engines"] == 1
    assert env.svc.limit("monthly_tokens", "project", "any-project") == 0


def test_daily_limit_sums_all_projects_of_the_owner(env):
    first, second = _project(env, "replenish-0006", "alice"), _project(env, "replenish-0007", "alice", "仓储调拨")
    others = _project(env, "replenish-0008", "bob", "排班")
    env.svc.set_limits({"daily_tokens": 1000}, {})
    _append(first, _usage(600))
    _append(second, _usage(399))
    _append(others, _usage(5000, ts=NOW - 2 * DAY))  # bob 前天的用量不算今天
    env.svc.check_send(first, "alice")

    _append(second, _usage(1))
    with pytest.raises(quota.QuotaExceeded) as caught:
        env.svc.check_send(first, "alice")
    exc = caught.value
    assert isinstance(exc, ApiError) and exc.code == "quota_exceeded" and exc.status == 429
    assert exc.details == {"quota": "daily_tokens", "scope": "user", "limit": 1000, "used": 1000,
                           "reset_at": "2026-11-16T00:00:00.000Z"}
    assert "1,000 token" in str(exc) and "明天再试" in str(exc)
    env.svc.check_send(others, "bob")


def test_monthly_limit_is_per_project(env):
    first, second = _project(env, "replenish-0009", "alice"), _project(env, "replenish-0010", "alice", "仓储调拨")
    env.svc.set_limits({"daily_tokens": 0}, {"monthly_tokens": 500})
    _append(first, _usage(300, ts=NOW - 10 * DAY), _usage(200))
    _append(first, _usage(9999, ts=calendar.timegm((2026, 10, 31, 23, 0, 0))))  # 上月不算
    _append(second, _usage(100))
    with pytest.raises(quota.QuotaExceeded) as caught:
        env.svc.check_send(first, "alice")
    assert caught.value.details["quota"] == "monthly_tokens" and caught.value.details["scope"] == "project"
    assert caught.value.details["used"] == 500 and caught.value.details["reset_at"] == "2026-12-01T00:00:00.000Z"
    env.svc.check_send(second, "alice")


def test_concurrent_sessions_count_other_live_engines_of_the_owner(env, monkeypatch):
    ids = [_project(env, f"replenish-01{n:02d}", "alice", f"项目{n}") for n in range(5)]
    bobs = _project(env, "replenish-0199", "bob", "排班")
    env.svc.set_limits({"concurrent_sessions": 2}, {})
    sessions = {
        ids[0]: _session(),
        ids[1]: _session(state="running", engine=False),  # 在跑：马上要占引擎
        ids[2]: _session(state="closed"),
        ids[3]: _session(engine=False),  # 只是加载了历史，没占引擎
        bobs: _session(),
    }
    monkeypatch.setattr(agent_sessions.manager, "_sessions", sessions)
    with pytest.raises(quota.QuotaExceeded) as caught:
        env.svc.check_send(ids[4], "alice")
    assert caught.value.details == {"quota": "concurrent_sessions", "scope": "user", "limit": 2, "used": 2,
                                    "reset_at": None}
    env.svc.check_send(ids[0], "alice")  # 发给已在线的项目不算新会话
    env.svc.set_limits({"concurrent_sessions": 1}, {})
    env.svc.check_send(ids[1], "alice")  # 调低上限后，已在线的项目照样能发
    env.svc.check_send(bobs, "bob")
    sessions[ids[0]] = _session(engine=False, connecting=True)
    with pytest.raises(quota.QuotaExceeded):
        env.svc.check_send(ids[3], "alice")


def test_override_takes_precedence_over_default(env):
    alices, bobs = _project(env, "replenish-0011", "alice"), _project(env, "replenish-0012", "bob", "排班")
    env.svc.set_limits({"daily_tokens": 1000}, {"monthly_tokens": 1000})
    _append(alices, _usage(1500))
    _append(bobs, _usage(1500))
    entry, replaced = env.svc.add_override("user", "alice", "alice", "daily_tokens", 5000, "合成：月底冲刺", "local")
    assert replaced is None and re.fullmatch(r"qo-[0-9a-f]{6}", entry["id"])
    env.svc.add_override("project", alices, "门店补货", "monthly_tokens", 0, "合成：不限", "local")
    env.svc.check_send(alices, "alice")
    with pytest.raises(quota.QuotaExceeded):
        env.svc.check_send(bobs, "bob")

    again, replaced = env.svc.add_override("user", "alice", "alice", "daily_tokens", 1200, "合成：收回", "local")
    assert replaced == entry["id"]
    assert [item["id"] for item in env.svc.limits()["overrides"] if item["item"] == "daily_tokens"] == [again["id"]]
    with pytest.raises(quota.QuotaExceeded):
        env.svc.check_send(alices, "alice")
    assert env.svc.delete_override(again["id"])["item"] == "daily_tokens"
    assert env.svc.delete_override(again["id"]) is None
    assert env.svc.limit("daily_tokens", "user", "alice") == 1000


def test_remaining_and_autobuild_budget_cap(env):
    pid = _project(env, "replenish-0013", "alice")
    env.svc.set_limits({"daily_tokens": 1000}, {"monthly_tokens": 5000})
    _append(pid, _usage(300))
    assert env.svc.remaining(pid, "alice") == {"tokens": 700, "dimension": "daily_tokens"}
    env.svc.autobuild_budget_cap(pid, "alice", 700)
    with pytest.raises(quota.BudgetExceedsQuota) as caught:
        env.svc.autobuild_budget_cap(pid, "alice", 701)
    assert caught.value.code == "budget_exceeds_quota" and caught.value.status == 422
    assert caught.value.details == {"dimension": "tokens", "quota": "daily_tokens", "max": 700, "requested": 701,
                                    "remaining": 700}

    _append(pid, _usage(4500, ts=NOW - 3 * DAY))
    assert env.svc.remaining(pid, "alice") == {"tokens": 200, "dimension": "monthly_tokens"}
    with pytest.raises(quota.BudgetExceedsQuota):
        env.svc.autobuild_budget_cap(pid, "alice", None)  # 预算不限，但配额有限

    env.svc.set_limits({"daily_tokens": 0}, {"monthly_tokens": 0})
    assert env.svc.remaining(pid, "alice") == {"tokens": None, "dimension": None}
    env.svc.autobuild_budget_cap(pid, "alice", None)


def test_collections_per_source_per_hour(env):
    env.svc.set_limits({"collections_per_source_per_hour": 2}, {})
    clock = {"now": NOW}
    env.svc.clock = lambda: clock["now"]
    env.svc.check_collection("ds-orders", "alice", record=False)  # 只查不记
    env.svc.check_collection("ds-orders", "alice")
    clock["now"] += 60
    env.svc.check_collection("ds-orders", "alice")
    with pytest.raises(quota.QuotaExceeded) as caught:
        env.svc.check_collection("ds-orders", "alice")
    assert caught.value.details["quota"] == "collections_per_source_per_hour"
    assert caught.value.details["used"] == 2 and caught.value.details["reset_at"] == "2026-11-15T13:00:00.000Z"
    env.svc.check_collection("ds-stock", "alice")  # 另一个数据源各算各的
    env.svc.check_collection("ds-orders", "bob")  # 另一个用户各算各的
    clock["now"] = NOW + 3600  # 第一次采集滑出窗口
    env.svc.check_collection("ds-orders", "alice")
    with pytest.raises(quota.QuotaExceeded):
        env.svc.check_collection("ds-orders", "alice")


# ---- 管理接口与闸门 ----


def _app() -> TestClient:
    app = FastAPI()
    app.include_router(quota_routes.router)
    errors.install(app)

    @app.post("/api/v1/projects/{project_id}/conversations/{cid}/messages")
    async def send(project_id: str, cid: str):
        return {"sent": project_id}

    @app.post("/api/v1/projects/{project_id}/commands")
    async def command(project_id: str):
        return {"ran": project_id}

    @app.post("/api/v1/projects/{project_id}/conversations")
    async def create_conversation(project_id: str):
        return {"created": project_id}

    app.add_middleware(quota.QuotaGate)

    @app.middleware("http")
    async def fake_auth(request: Request, call_next):
        """鉴权替身：X-Test-User: 用户名:角色；不带即本地模式（三种角色都有）。"""
        raw = request.headers.get("x-test-user")
        if raw:
            name, role = raw.split(":")
            request.state.user = {"sub": name, "role": role}
        return await call_next(request)

    return TestClient(app)


def _as(name: str, role: str = "user") -> dict:
    return {"X-Test-User": f"{name}:{role}"}


def test_get_and_put_quotas(env):
    client = _app()
    body = client.get("/api/v1/admin/quotas").json()
    assert body == {
        "per_user": {"concurrent_sessions": 3, "daily_tokens": 2_000_000, "collections_per_source_per_hour": 6},
        "per_project": {"online_engines": 1, "monthly_tokens": 20_000_000, "snapshot_git_mb": 20},
        "overrides": [],
    }
    saved = client.put("/api/v1/admin/quotas", json={
        "per_user": {"daily_tokens": 5000}, "per_project": {"online_engines": 4, "snapshot_git_mb": 50},
        "overrides": [{"ignored": True}],
    })
    assert saved.status_code == 200, saved.text
    assert saved.json()["per_user"]["daily_tokens"] == 5000
    assert saved.json()["per_project"] == {"online_engines": 1, "monthly_tokens": 20_000_000, "snapshot_git_mb": 50}
    assert env.svc.limit("daily_tokens", "user", "alice") == 5000  # 立即生效
    entry = next(item for item in audit.read(20) if (item.get("detail") or {}).get("action") == "quota.update")
    assert entry["actor"] == "local" and entry["detail"]["per_user.daily_tokens"] == 5000

    bad = client.put("/api/v1/admin/quotas", json={
        "per_user": {"daily_tokens": -1, "concurrent_sessions": "3", "unknown_item": 1},
        "per_project": {"monthly_tokens": True, "snapshot_git_mb": 1.5},
    })
    assert bad.status_code == 400 and bad.json()["code"] == "invalid_argument"
    assert bad.json()["details"]["fields"] == {
        "per_user.daily_tokens": "non_negative_int", "per_user.concurrent_sessions": "non_negative_int",
        "per_user.unknown_item": "unknown", "per_project.monthly_tokens": "non_negative_int",
        "per_project.snapshot_git_mb": "non_negative_int",
    }
    not_object = client.put("/api/v1/admin/quotas", json={"per_user": [1]})
    assert not_object.json()["details"]["fields"] == {"per_user": "object"}
    assert env.svc.limit("daily_tokens", "user", "alice") == 5000


def test_override_endpoints(env):
    pid = _project(env, "replenish-0014", "alice")
    client = _app()
    created = client.post("/api/v1/admin/quotas/overrides", json={
        "subject": {"kind": "user", "id": "alice"}, "item": "daily_tokens", "value": 8000, "reason": "合成：月底冲刺",
    })
    assert created.status_code == 201, created.text
    body = created.json()
    assert re.fullmatch(r"qo-[0-9a-f]{6}", body["id"]) and RFC3339.match(body["created_at"])
    assert body["subject"] == {"kind": "user", "id": "alice", "label": "alice"}
    assert body["value"] == 8000 and body["created_by"] == "local" and body["reason"] == "合成：月底冲刺"

    project = client.post("/api/v1/admin/quotas/overrides", json={
        "subject": {"kind": "project", "id": pid}, "item": "monthly_tokens", "value": 0, "reason": "合成：不限",
    }).json()
    assert project["subject"]["label"] == "门店补货"
    listed = client.get("/api/v1/admin/quotas").json()["overrides"]
    assert [item["id"] for item in listed] == [body["id"], project["id"]]

    missing = client.post("/api/v1/admin/quotas/overrides", json={
        "subject": {"kind": "project", "id": "no-such-project"}, "item": "monthly_tokens", "value": 1, "reason": "x",
    })
    assert missing.status_code == 404 and missing.json()["code"] == "project_not_found"
    invalid = client.post("/api/v1/admin/quotas/overrides", json={
        "subject": {"kind": "user", "id": "alice"}, "item": "monthly_tokens", "value": -5, "reason": "  ",
    })
    assert invalid.status_code == 400
    assert invalid.json()["details"]["fields"] == {"item": "enum", "value": "non_negative_int", "reason": "required"}
    kind = client.post("/api/v1/admin/quotas/overrides", json={"subject": {"kind": "team", "id": "x"}, "item": "x",
                                                                "value": 1, "reason": "x"})
    assert kind.json()["details"]["fields"] == {"subject.kind": "enum"}
    name = client.post("/api/v1/admin/quotas/overrides", json={"subject": {"kind": "user", "id": "bad name"},
                                                                "item": "daily_tokens", "value": 1, "reason": "x"})
    assert name.status_code == 400 and name.json()["details"]["fields"] == {"subject.id": "format"}

    assert client.delete(f"/api/v1/admin/quotas/overrides/{body['id']}").status_code == 204
    gone = client.delete(f"/api/v1/admin/quotas/overrides/{body['id']}")
    assert gone.status_code == 404 and gone.json()["code"] == "not_found"
    actions = [(item.get("detail") or {}).get("action") for item in audit.read(20)]
    assert {"quota.override.create", "quota.override.delete"} <= set(actions)


def test_quota_endpoints_require_roles(env):
    client = _app()
    assert client.get("/api/v1/admin/quotas", headers=_as("ops-1", "ops")).status_code == 200
    assert client.get("/api/v1/admin/quotas", headers=_as("admin-1", "admin")).status_code == 200
    denied = client.get("/api/v1/admin/quotas", headers=_as("alice"))
    assert denied.status_code == 403 and denied.json()["code"] == "forbidden"
    for headers in (_as("ops-1", "ops"), _as("alice")):
        assert client.put("/api/v1/admin/quotas", json={}, headers=headers).status_code == 403
        assert client.post("/api/v1/admin/quotas/overrides", json={}, headers=headers).status_code == 403
        assert client.delete("/api/v1/admin/quotas/overrides/qo-000000", headers=headers).status_code == 403
    assert client.put("/api/v1/admin/quotas", json={}, headers=_as("admin-1", "admin")).status_code == 200


def test_gate_rejects_over_quota_sends_and_passes_others(env):
    local = _project(env, "replenish-0015", "")
    alices = _project(env, "replenish-0016", "alice")
    env.svc.set_limits({"daily_tokens": 1000}, {})
    _append(local, _usage(1000))
    _append(alices, _usage(1000))
    client = _app()

    rejected = client.post(f"/api/v1/projects/{local}/conversations/default/messages", json={"text": "合成"})
    assert rejected.status_code == 429
    body = rejected.json()
    assert body["code"] == "quota_exceeded" and body["details"]["limit"] == 1000 and body["details"]["used"] == 1000
    assert "今天的模型用量已到上限（1,000 token）" in body["message"] and "trace_id" in body
    assert client.post(f"/api/v1/projects/{local}/commands", json={}).status_code == 429
    # 其余路径、其他方法不拦
    assert client.post(f"/api/v1/projects/{local}/conversations", json={}).status_code == 200
    assert client.get(f"/api/v1/projects/{local}/conversations/default/messages").status_code == 405

    assert client.post(f"/api/v1/projects/{alices}/commands", json={}, headers=_as("alice")).status_code == 429
    # 不是所有者、项目不存在：交给路由处理（真实路由回 403、404）
    assert client.post(f"/api/v1/projects/{alices}/commands", json={}, headers=_as("bob")).status_code == 200
    assert client.post("/api/v1/projects/no-such-project/commands", json={}).status_code == 200

    env.svc.set_limits({"daily_tokens": 0}, {})
    assert client.post(f"/api/v1/projects/{alices}/commands", json={}, headers=_as("alice")).status_code == 200


def test_gate_fails_open_on_internal_error(env, monkeypatch):
    pid = _project(env, "replenish-0017", "")
    client = _app()

    def broken():
        raise RuntimeError("合成：台账损坏")

    monkeypatch.setattr(quota, "service", broken)
    ok = client.post(f"/api/v1/projects/{pid}/conversations/default/messages", json={"text": "合成"})
    assert ok.status_code == 200 and ok.json() == {"sent": pid}

    monkeypatch.setattr(quota, "service", lambda: env.svc)
    monkeypatch.setattr(env.svc, "check_send", lambda project_id, username: 1 / 0)
    assert client.post(f"/api/v1/projects/{pid}/commands", json={}).status_code == 200


def test_real_app_gate_blocks_sending_over_quota(env):
    """接进 api/v1 之后：发消息、发命令在鉴权之后、路由之前被配额挡下（429 quota_exceeded）。"""
    from app.main import app

    _project(env, "retail-p1", "local")
    env.svc.set_limits({"daily_tokens": 100}, {})
    env.svc.record("retail-p1", "local", 150, ts=NOW)
    client = TestClient(app)
    for path in ("/api/v1/projects/retail-p1/conversations/default/messages", "/api/v1/projects/retail-p1/commands"):
        response = client.post(path, json={"text": "继续"})
        assert response.status_code == 429, response.text
        body = response.json()
        assert body["code"] == "quota_exceeded" and body["details"]["limit"] == 100 and body["details"]["used"] == 150
    # 读接口不受影响
    assert client.get("/api/v1/projects/retail-p1/conversations").status_code != 429
