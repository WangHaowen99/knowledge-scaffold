"""会话配置指纹与生效时机（方案 B2、B15；详设 3.5、8.1 第 4 步、P1 验收「升级 core 包后从下一条消息
开始生效，不再出现三版本并存」）。

用真实的工作区与装配器、可控的假引擎驱动 SessionManager：测试自己投递 result，决定一轮何时结束。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from app import config
from app.api import deps
from app.services import agent_sessions
from app.services.agent_sessions import SessionManager
from app.services.packs import builtin, contracts, lockfile
from app.services.session_host import fingerprint
from app.services.workspace import WorkspaceManager
from tests.pack_helpers import make_assets, make_pack


class FakeEngine:
    name = "jiuwen"
    instances: list["FakeEngine"] = []
    model = "m1"

    def __init__(self, ctx):
        self.ctx = ctx
        self.client = None
        self.resume_attempted = False
        self.sent: list[str] = []
        self.queue: asyncio.Queue = asyncio.Queue()
        self.disconnected = False
        # 连上时看到的装配目录：引擎读的就是它
        self.assembly_at_connect = None
        FakeEngine.instances.append(self)

    @classmethod
    def available(cls):
        return True

    async def connect(self):
        self.client = object()
        self.assembly_at_connect = (Path(self.ctx.cwd) / ".claude").resolve()
        return True

    async def send(self, text, context=None):
        self.sent.append(text)

    async def events(self):
        while True:
            event = await self.queue.get()
            yield event
            if event.get("type") == "_engine_end":
                return

    async def interrupt(self):
        pass

    async def disconnect(self):
        self.disconnected = True
        self.client = None

    async def kill(self):
        self.client = None

    def model_fingerprint(self):
        return FakeEngine.model

    def config_stale(self):
        return False

    def context_limits(self):
        return None, None


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    builtin.clear_cache()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "MAX_ACTIVE_SESSIONS", 4)
    monkeypatch.setattr(agent_sessions, "SDK_AVAILABLE", True)
    workspaces = WorkspaceManager(tmp_path / "data", make_assets(tmp_path))
    monkeypatch.setattr(deps, "workspaces", workspaces)
    FakeEngine.instances = []
    FakeEngine.model = "m1"
    manager = SessionManager()
    monkeypatch.setattr(manager, "_engine_class_for", lambda project_id: FakeEngine)
    root = workspaces.project_root(workspaces.create_project("指纹")["id"])
    # 第二个 core 版本：多一个命令（H1 变化）
    source = make_pack(tmp_path / "src", "core", "0.2.0", required=True,
                       files={"commands/ontology.md": "core 0.2\n", "commands/ontology-new.md": "新命令\n"})
    workspaces.packs.repository.install(source, origin="upload")
    yield manager, workspaces, root
    builtin.clear_cache()


def lock_core(workspaces: WorkspaceManager, root: Path, version: str) -> None:
    lock = lockfile.read(root)
    lock.upsert(workspaces.packs.entry_for("core", version, follow=False))
    lockfile.write(root, lock)


async def settle(manager, project_id):
    session = manager.get(project_id)
    for _ in range(100):
        if session.connect_task:
            await session.connect_task
        await asyncio.sleep(0)
        if session.engine is not None and session.connect_task is None:
            break
    return session


async def drain(times: int = 30):
    for _ in range(times):
        await asyncio.sleep(0.005)


def events_of(session, kind: str) -> list[dict]:
    return [event for event in session.events if event.get("type") == kind]


# ---- 指纹本身 ----


def test_fingerprint_parts_and_diff(env):
    _, workspaces, root = env
    engine = FakeEngine(type("Ctx", (), {"cwd": root})())
    before = fingerprint.compute(engine, root.name, "")
    assert set(before.parts) == {"owner", "model", "assembly", "flow"}  # mcp、权限模式只对 Claude
    lock_core(workspaces, root, "0.2.0")
    after = fingerprint.compute(engine, root.name, "")
    assert after.diff(before) == ["assembly"]
    FakeEngine.model = "m2"
    assert fingerprint.compute(engine, root.name, "alice").diff(before) == ["model", "owner", "assembly"]


def test_knowledge_pack_change_does_not_touch_fingerprint(env, tmp_path):
    _, workspaces, root = env
    engine = FakeEngine(type("Ctx", (), {"cwd": root})())
    before = fingerprint.compute(engine, root.name, "")
    kb = make_pack(tmp_path / "kb", "retail-kb", "1.0.0", kind="knowledge", files={"reference/术语.md": "x\n"})
    workspaces.packs.repository.install(kb, origin="upload")
    lock = lockfile.read(root)
    lock.upsert(workspaces.packs.entry_for("retail-kb", "1.0.0"))
    lockfile.write(root, lock)
    workspaces.packs.build(lock)
    assert fingerprint.compute(engine, root.name, "").diff(before) == []


def test_claude_fingerprint_includes_mcp_and_permission_mode(env):
    _, _, root = env

    class Claude:
        name = "claude"

        def model_fingerprint(self):
            return "x"

    parts = fingerprint.compute(Claude(), root.name, "").parts
    assert {"mcp", "permission_mode"} <= set(parts)


# ---- 生效时机 ----


def test_upgrade_while_idle_takes_effect_on_next_message(env):
    manager, workspaces, root = env

    async def scenario():
        await manager.send(root.name, root, "第一条")
        session = await settle(manager, root.name)
        first = session.engine
        old = first.assembly_at_connect
        first.queue.put_nowait({"type": "result"})
        await drain()
        assert session.state == "idle"

        lock_core(workspaces, root, "0.2.0")
        # 下一条消息之前：没切换，引擎还连着
        assert (root / ".claude").resolve() == old and not first.disconnected

        await manager.send(root.name, root, "第二条")
        session = await settle(manager, root.name)
        second = session.engine
        assert first.disconnected and second is not first
        assert second.assembly_at_connect != old
        assert (second.assembly_at_connect / "commands" / "ontology-new.md").is_file()
        assert second.sent == ["第二条"]
        reconnected = events_of(session, "session_reconnected")
        assert reconnected and reconnected[-1]["changes"] == ["assembly"] and reconnected[-1]["context_kept"]
        # 后端契约库与 agent 看到的是同一个版本
        assert contracts.for_project(root).scripts_dir == second.assembly_at_connect / "scripts"
        await manager.shutdown()

    asyncio.run(scenario())


def test_upgrade_while_running_finishes_turn_on_old_version(env):
    manager, workspaces, root = env

    async def scenario():
        await manager.send(root.name, root, "第一条")
        session = await settle(manager, root.name)
        first = session.engine
        old = first.assembly_at_connect
        assert session.state == "running"

        lock_core(workspaces, root, "0.2.0")
        await manager.send(root.name, root, "补充一句")
        # 正在跑的一轮不切断、不换目录：消息交给旧引擎，.claude 仍指旧装配目录
        assert session.engine is first and not first.disconnected
        assert first.sent == ["第一条", "补充一句"]
        assert (root / ".claude").resolve() == old
        pending = events_of(session, "session_config_pending")
        assert len(pending) == 1 and pending[0]["changes"][0]["kind"] == "assembly"
        assert pending[0]["effective"] == "next_message"

        first.queue.put_nowait({"type": "result"})
        first.queue.put_nowait({"type": "result"})
        await drain()
        assert first.disconnected and session.engine is None
        assert (root / ".claude").resolve() == old  # 断开不切，下一条消息前才切

        await manager.send(root.name, root, "第三条")
        session = await settle(manager, root.name)
        assert session.engine.assembly_at_connect != old
        assert (root / ".claude").resolve() == session.engine.assembly_at_connect
        assert session.engine.sent == ["第三条"]
        await manager.shutdown()

    asyncio.run(scenario())


def test_follow_channel_upgrades_before_next_message(env):
    manager, workspaces, root = env

    async def scenario():
        await manager.send(root.name, root, "第一条")
        session = await settle(manager, root.name)
        session.engine.queue.put_nowait({"type": "result"})
        await drain()
        old = session.engine.assembly_at_connect
        # 运维把 stable 指到 0.2.0：跟随通道的项目下一条消息前升级并提交锁文件
        workspaces.packs.repository.set_channel("core", "stable", "0.2.0")
        await manager.send(root.name, root, "第二条")
        session = await settle(manager, root.name)
        assert lockfile.read(root).get("core").version == "0.2.0"
        assert session.engine.assembly_at_connect != old
        log = os.popen(f"git -C '{root}' log -1 --format=%s%n%b").read()
        assert "跟随通道升级能力包" in log and "Onto-Source: plugin" in log
        await manager.shutdown()

    asyncio.run(scenario())


def test_model_change_still_reconnects(env):
    manager, _, root = env

    async def scenario():
        await manager.send(root.name, root, "第一条")
        session = await settle(manager, root.name)
        first = session.engine
        first.queue.put_nowait({"type": "result"})
        await drain()
        FakeEngine.model = "m2"
        await manager.send(root.name, root, "第二条")
        session = await settle(manager, root.name)
        assert first.disconnected and session.engine is not first
        assert events_of(session, "session_reconnected")[-1]["changes"] == ["model"]
        await manager.shutdown()

    asyncio.run(scenario())


def test_engine_observers_receive_events():
    from app.services.engines.base import ObservableEngine

    class Engine(ObservableEngine):
        pass

    seen = []
    engine = Engine()
    engine.add_observer(seen.append)
    engine.add_observer(lambda event: 1 / 0)  # 观察者出错不影响别人
    engine._notify({"type": "result"})
    assert seen == [{"type": "result"}]
