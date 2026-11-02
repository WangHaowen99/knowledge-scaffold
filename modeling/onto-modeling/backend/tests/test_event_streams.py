"""项目流 P-15、用户流 A-12（契约 1.11、4.3、4.4）：事件 id、回放、重置、心跳、令牌到期、连接数，
以及内核各处接到项目流上的事件（workspace.changed、flow.updated、assembly / extensions、对话、项目、维护）。

测试数据全部为合成内容。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.main import app
from app.services import auth, project_events, streams
from app.services.workspace import WorkspaceManager


@pytest.fixture(autouse=True)
def clean_channels():
    streams.reset_all()
    yield
    streams.reset_all()


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    manager = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", manager)
    monkeypatch.setattr(auth, "ENABLED", False)
    monkeypatch.setattr(project_events, "FLOW_DEBOUNCE_SECONDS", 0.0)
    client = TestClient(app)
    project_id = client.post("/api/v1/projects", json={"name": "事件流", "goal": "合成场景：外卖订单建模"}).json()["id"]
    return client, manager, project_id


def frames_of(chunks: list[str]) -> list[dict]:
    """SSE 文本块 → 帧（id 行 + data 解析出来的信封）。"""
    frames = []
    for chunk in chunks:
        if not chunk.startswith(("id:", "data:")):
            continue
        frame = {"id": None, "envelope": None}
        for line in chunk.strip().splitlines():
            if line.startswith("id: "):
                frame["id"] = line[4:]
            elif line.startswith("data: "):
                frame["envelope"] = json.loads(line[6:])
        frames.append(frame)
    return frames


async def collect(iterator, count: int, timeout: float = 3.0) -> list[str]:
    chunks = []
    while len(chunks) < count:
        chunks.append(await asyncio.wait_for(iterator.__anext__(), timeout))
    return chunks


# ---- 频道：id、回放、重置 ----


def test_ids_are_epoch_and_sequence_and_replay_after_last_id():
    channel = streams.project_channel("p1")
    first = channel.publish("flow.updated", {"n": 1}, project_id="p1")
    channel.publish("inbox.updated", {"n": 2}, project_id="p1")
    channel.publish("scope.updated", {"n": 3}, project_id="p1")
    assert first["id"] == f"{streams.EPOCH}.1"
    assert set(first) == {"id", "type", "ts", "project_id", "data"} and first["ts"].endswith("Z")

    async def scenario():
        subscriber, replay, reset = channel.open(first["id"], asyncio.get_running_loop())
        channel.unsubscribe(subscriber)
        return replay, reset

    replay, reset = asyncio.run(scenario())
    assert reset is None and [item["data"]["n"] for item in replay] == [2, 3]


def test_first_open_does_not_replay_and_transient_events_have_no_id():
    channel = streams.project_channel("p1")
    channel.publish("flow.updated", {}, project_id="p1")
    transient = channel.publish("command.progress", {"percent": 40}, transient=True, project_id="p1")
    assert "id" not in transient

    async def scenario():
        subscriber, replay, reset = channel.open("", asyncio.get_running_loop())
        channel.unsubscribe(subscriber)
        return replay, reset

    replay, reset = asyncio.run(scenario())
    assert replay == [] and reset is None


@pytest.mark.parametrize("last_id,reason", [("0123abcd.4", "epoch_changed"), ("garbage", "epoch_changed")])
def test_other_process_ids_reset(last_id, reason):
    channel = streams.project_channel("p1")
    channel.publish("flow.updated", {}, project_id="p1")

    async def scenario():
        return channel.open(last_id, asyncio.get_running_loop())

    _, replay, reset = asyncio.run(scenario())
    assert replay == [] and reset == reason


def test_evicted_events_reset_with_gap(monkeypatch):
    monkeypatch.setattr(streams, "BUFFER_MAX", 3)
    channel = streams.project_channel("p1")
    ids = [channel.publish("flow.updated", {"n": n}, project_id="p1")["id"] for n in range(6)]

    async def scenario():
        old = channel.open(ids[0], asyncio.get_running_loop())
        recent = channel.open(ids[3], asyncio.get_running_loop())
        return old, recent

    (_, replay, reset), (_, recent, recent_reset) = asyncio.run(scenario())
    assert reset == "gap" and replay == []
    assert recent_reset is None and [item["data"]["n"] for item in recent] == [4, 5]


def test_old_events_expire_from_buffer(monkeypatch):
    channel = streams.project_channel("p1")
    first = channel.publish("flow.updated", {}, project_id="p1")
    monkeypatch.setattr(streams, "BUFFER_SECONDS", -1)
    channel.publish("flow.updated", {}, project_id="p1")

    async def scenario():
        return channel.open(first["id"], asyncio.get_running_loop())

    assert asyncio.run(scenario())[2] == "gap"


# ---- 帧序列：retry、重置、回放、实时、心跳、溢出、令牌到期 ----


def test_frames_replay_then_live_then_ping():
    channel = streams.project_channel("p1")
    first = channel.publish("flow.updated", {"n": 1}, project_id="p1")
    channel.publish("flow.updated", {"n": 2}, project_id="p1")

    async def scenario():
        subscriber, replay, reset = channel.open(first["id"], asyncio.get_running_loop())
        frames = streams.channel_frames(channel, subscriber, replay, reset, resync=streams.PROJECT_RESYNC,
                                        heartbeat=0.05)
        head = await collect(frames, 2)
        # 在别的线程发：经事件循环转给订阅者
        threading.Thread(target=channel.publish, args=("inbox.updated", {"n": 3}), kwargs={"project_id": "p1"}).start()
        live = await collect(frames, 2)
        await frames.aclose()
        return head, live, channel.subscriber_count()

    head, live, remaining = asyncio.run(scenario())
    assert head[0] == "retry: 3000\n\n"
    replayed = frames_of(head[1:])
    assert replayed[0]["envelope"]["data"] == {"n": 2} and replayed[0]["id"] == f"{streams.EPOCH}.2"
    parsed = frames_of(live)
    assert parsed[0]["envelope"]["type"] == "inbox.updated" and parsed[0]["id"] == f"{streams.EPOCH}.3"
    assert parsed[1]["envelope"]["type"] == "ping" and parsed[1]["id"] is None
    assert remaining == 0  # 关流后订阅摘掉


def test_reset_frame_lists_resync_snapshots():
    channel = streams.project_channel("p1")

    async def scenario():
        subscriber, replay, reset = channel.open("feedbeef.9", asyncio.get_running_loop())
        frames = streams.channel_frames(channel, subscriber, replay, reset, resync=streams.PROJECT_RESYNC)
        chunks = await collect(frames, 2)
        await frames.aclose()
        return chunks

    reset = frames_of(asyncio.run(scenario())[1:])[0]
    assert reset["id"] is None
    assert reset["envelope"]["type"] == "stream.reset"
    assert reset["envelope"]["data"] == {"reason": "epoch_changed", "resync": ["FL-01", "IN-01", "CS-06", "PL-12", "AB-05"]}


def test_slow_subscriber_overflow_resets(monkeypatch):
    monkeypatch.setattr(streams, "QUEUE_MAX", 2)
    channel = streams.project_channel("p1")

    async def scenario():
        subscriber, replay, reset = channel.open("", asyncio.get_running_loop())
        frames = streams.channel_frames(channel, subscriber, replay, reset, resync=streams.PROJECT_RESYNC)
        await collect(frames, 1)
        for n in range(5):
            channel.publish("flow.updated", {"n": n}, project_id="p1")
        await asyncio.sleep(0)
        chunks = await collect(frames, 2)
        await frames.aclose()
        return chunks

    parsed = frames_of(asyncio.run(scenario()))
    assert parsed[0]["envelope"]["type"] == "stream.reset" and parsed[0]["envelope"]["data"]["reason"] == "overflow"
    assert parsed[1]["envelope"]["data"] == {"n": 0}


def test_expired_token_sends_auth_expired_and_closes():
    channel = streams.user_channel("employee_0001")

    async def scenario():
        subscriber, replay, reset = channel.open("", asyncio.get_running_loop())
        frames = streams.channel_frames(channel, subscriber, replay, reset, resync=streams.USER_RESYNC,
                                        expires_at=time.time() + 0.05, heartbeat=5)
        chunks = [chunk async for chunk in frames]
        return chunks

    chunks = asyncio.run(scenario())
    assert frames_of(chunks)[-1]["envelope"]["type"] == "auth.expired"


# ---- 接口：所有者、连接数 ----


def test_too_many_streams_is_429(env):
    client, _, project_id = env
    for _ in range(streams.MAX_STREAMS_PER_USER):
        streams.acquire_slot("local")
    response = client.get(f"/api/v1/projects/{project_id}/events")
    assert response.status_code == 429 and response.json()["code"] == "too_many_streams"
    assert client.get("/api/v1/me/events").status_code == 429
    streams.release_slot("local")
    assert streams.open_count("local") == streams.MAX_STREAMS_PER_USER - 1


def test_project_stream_requires_owner(env, monkeypatch):
    client, _, _ = env
    assert client.get("/api/v1/projects/no-such-project/events").status_code == 404


# ---- 接到项目流上的事件 ----


def buffered(project_id: str, event_type: str) -> list[dict]:
    channel = streams.project_channel(project_id)

    async def scenario():
        subscriber, replay, _ = channel.open(f"{streams.EPOCH}.0", asyncio.get_running_loop())
        channel.unsubscribe(subscriber)
        return replay

    return [item for item in asyncio.run(scenario()) if item["type"] == event_type]


def test_web_write_publishes_workspace_changed(env):
    client, manager, project_id = env
    root = manager.project_root(project_id)
    rel = "ontology/object/订单.md"
    (root / "ontology" / "object").mkdir(parents=True, exist_ok=True)
    saved = manager.write_file(project_id, rel, "# 订单\n\n## 对象描述\n合成对象\n", author="local")
    events = buffered(project_id, "workspace.changed")
    assert events, "写入事务提交后应发 workspace.changed"
    data = events[-1]["data"]
    assert set(data) == {"commit", "version_label", "source", "changeset_id", "by", "entries", "artifacts", "paths"}
    assert data["source"] == "web" and rel in data["paths"] and data["commit"] == saved.get("commit", data["commit"])
    assert {"kind": "object", "id": "object:订单", "key": "订单", "name": "订单"} in data["entries"]
    assert data["version_label"].startswith("v0.")


def test_flow_refresh_sends_only_changed_nodes(env):
    _, manager, project_id = env
    project_events.ensure_baseline(project_id)
    assert project_events.refresh_flow(project_id) is None  # 没变化不发
    root = manager.project_root(project_id)
    target = root / "ontology" / "object" / "订单.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# 订单\n", encoding="utf-8")
    envelope = project_events.refresh_flow(project_id)
    assert envelope is not None and envelope["type"] == "flow.updated"
    data = envelope["data"]
    assert set(data) == {"flow_hash", "nodes", "slices", "suggestions_changed"}
    assert data["nodes"] and all(set(node) >= {"id", "state", "flags", "progress"} for node in data["nodes"])
    assert {"id": "core.object", "state": "confirmed"}.items() <= next(
        node for node in data["nodes"] if node["id"] == "core.object").items()
    assert len(data["nodes"]) < len(project_events._flow_cache[project_id]["nodes"])


def test_lock_file_commit_publishes_assembly_and_extensions(env, monkeypatch):
    _, manager, project_id = env
    # 没登记比较函数时给全部七类（启动时登记的扩展项注册表那份另有用例）
    monkeypatch.setattr(project_events, "_extension_differs", [])
    project_events.assembly_changed(project_id)
    assembly = buffered(project_id, "assembly.updated")[-1]["data"]
    assert set(assembly) == {"state", "pending_changes", "effective", "error_id"}
    assert assembly["state"] in ("pending", "applied", "failed")
    extensions = buffered(project_id, "extensions.updated")[-1]["data"]
    assert set(extensions) == {"active_hash", "pending_hash", "changed"}
    assert extensions["changed"] == list(project_events.EXTENSION_KINDS)


def test_registered_extension_differ_and_scope_provider(env, monkeypatch):
    _, _, project_id = env
    # 换一张空的登记表：不清掉启动时登记的那份（别的用例还要用）
    monkeypatch.setattr(project_events, "_extension_differs", [])
    project_events.register_extension_differ(lambda pid, before, after: ["nav"])
    project_events.register_scope_provider(lambda pid: {**project_events.empty_scope(), "state": "assistant_running"})
    try:
        project_events.extensions_changed(project_id, "a", "b")
        project_events.scope_changed(project_id)
    finally:
        project_events.register_scope_provider(None)
    assert buffered(project_id, "extensions.updated")[-1]["data"]["changed"] == ["nav"]
    scope = buffered(project_id, "scope.updated")[-1]["data"]
    assert scope["state"] == "assistant_running"
    project_events.scope_changed(project_id)
    assert buffered(project_id, "scope.updated")[-1]["data"] == project_events.empty_scope()


def test_project_and_user_events(env):
    client, manager, project_id = env
    client.patch(f"/api/v1/projects/{project_id}", json={"goal": "合成场景：外卖订单与骑手调度"})
    project = buffered(project_id, "project.updated")[-1]["data"]
    assert project["fields"] == ["goal"] and project["meta"]["id"] == project_id

    channel = streams.user_channel("")

    async def user_events():
        subscriber, replay, _ = channel.open(f"{streams.EPOCH}.0", asyncio.get_running_loop())
        channel.unsubscribe(subscriber)
        return replay

    cards = [item for item in asyncio.run(user_events()) if item["type"] == "project.card.updated"]
    assert cards and cards[-1]["data"]["project_id"] == project_id and cards[-1]["data"]["card"]["id"] == project_id

    meta = manager.get_project(project_id)
    client.request("DELETE", f"/api/v1/projects/{project_id}", json={"confirm_name": meta["name"]})
    removed = [item for item in asyncio.run(user_events()) if item["type"] == "project.removed"]
    assert removed[-1]["data"] == {"project_id": project_id, "reason": "deleted"}


def test_maintenance_change_reaches_project_and_user_streams(env):
    _, _, project_id = env
    from app.governance import ops_state

    streams.project_channel(project_id)
    ops_state.enter_maintenance("reject_new_sessions", "合成维护说明", "ops_0001")
    try:
        entered = buffered(project_id, "maintenance.updated")[-1]["data"]
        assert entered["active"] is True and entered["mode"] == "reject_new_sessions"
        assert entered["started_at"].endswith("Z")
    finally:
        ops_state.exit_maintenance("ops_0001")
    assert buffered(project_id, "maintenance.updated")[-1]["data"]["active"] is False
