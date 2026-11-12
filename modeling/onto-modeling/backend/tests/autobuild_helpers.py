"""自主构建测试共用的假引擎、项目与等待（测试数据全部为合成内容）。

F8 起不分段：项目是全量模式，一次运行只有一段「全部」（后面可以再跟校验等主线节点）。假引擎按收到的消息现做：
对象提取一次写全部合成对象、操作提取写全部操作、场景模拟写场景文件，命令锁开、放都按工具箱的样子；测试可以往
`AutoEngine.hooks` 里放「在某条命令里先做的动作」（提问、等测试放行），键是「命令:段」（`/ontology-extract-object:all`）
或只写命令。不起真实 CLI。
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.kernel.changesets import gates
from app.kernel.changesets import service as changesets
from app.main import app
from app.routers import sessions as session_routes
from app.services import agent_sessions, auth, streams
from app.services.agent_sessions import SessionManager
from app.services.autobuild.runner import runner
from app.services.packs import contracts
from app.services.workspace import WorkspaceManager
from tests.test_changesets import main_write, obj

ENTRIES = 5
OBJECTS = [f"对象{n}" for n in range(1, ENTRIES + 1)]
OPERATIONS = [f"操作{n}" for n in range(1, ENTRIES + 1)]
# 草稿写成老样子的多段也行：from_draft 合成一段「全部」（F8）
DRAFT = {"goal_md": "合成目标", "slices": [{"name": "合成业务", "objects": OBJECTS, "operations": OPERATIONS}]}
UNIT = "all"
COMMANDS = {"/ontology-extract-object": "extract-object", "/ontology-extract-operation": "extract-operation",
            "/ontology-trace": "trace", "/ontology-validate": "validate"}


def op(name: str) -> str:
    return f"# {name}\n\n## 业务描述\n\n合成操作\n"


def result(**extra) -> dict:
    return {"type": "result", "subtype": "success", "is_error": False, "duration_ms": 10, "num_turns": 1,
            "total_cost_usd": 0.01, "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 5},
            **extra}


def _write(rel: str, text: str):
    def action(ctx):
        path = Path(ctx.cwd) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return action


def _lock(command: str, phase: str = "write"):
    def action(ctx):
        path = Path(ctx.cwd) / ".ontology-state" / f"lock-{command}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"command": command, "phase": phase}), encoding="utf-8")
    return action


def _release_all(ctx):
    for path in (Path(ctx.cwd) / ".ontology-state").glob("lock-*.json"):
        path.unlink(missing_ok=True)


def gate(name: str) -> threading.Event:
    """测试放行之前，命令停在这里（引擎这一轮一直在跑）。"""
    event = threading.Event()

    async def wait(ctx):
        await asyncio.to_thread(event.wait, 30)
    AutoEngine.gates[name] = wait
    return event


def ask(questions: list[dict], *, into: list | None = None, tool: str = "AskUserQuestion"):
    """引擎在这一步里提问（AskUserQuestion）或请求确认，答案记进 into。"""
    async def action(ctx):
        decision = await ctx.request_permission(tool, {"questions": questions} if tool == "AskUserQuestion" else
                                                {"file_path": "ontology/object/x.md"}, None, None)
        if into is not None:
            into.append(decision)
    return action


class AutoEngine:
    name = "fake"
    sent: list[str] = []
    hints: list[str] = []
    hooks: dict[str, list] = {}
    gates: dict[str, object] = {}
    leave_open: set[str] = set()

    @classmethod
    def available(cls) -> bool:
        return True

    @classmethod
    def reset(cls) -> None:
        cls.sent, cls.hints, cls.hooks, cls.gates, cls.leave_open = [], [], {}, {}, set()

    def __init__(self, ctx):
        self.ctx = ctx
        self.client = object()
        self.queue: asyncio.Queue = asyncio.Queue()

    async def connect(self) -> bool:
        return False

    def _script(self, text: str) -> list:
        from app.services.session_host import injection

        AutoEngine.hints.append(injection.autobuild_hint(Path(self.ctx.cwd), self.ctx.conversation_id))
        head = text.split()[0] if text.strip() else ""
        found = re.search(r"slice=(s\d+|all)", text)
        unit = found.group(1) if found else self._marker_slice()
        command = COMMANDS.get(head)
        key = f"{head}:{unit}"
        items = list(AutoEngine.hooks.pop(key, [])) + list(AutoEngine.hooks.pop(head, []))
        for name in (key, head):
            if name in AutoEngine.gates:
                items.insert(0, AutoEngine.gates.pop(name))
                break
        if command is None:
            return [*items, _release_all, result()]
        writes = {"extract-object": [(f"ontology/object/{name}.md", obj(name, "合成对象")) for name in OBJECTS],
                  "extract-operation": [(f"ontology/operation/{name}.md", op(name)) for name in OPERATIONS],
                  "trace": [(f"ontology/testcase/{name}.md", f"# 测试场景：{name}\n\n## 场景 1：合成\n")
                            for name in OPERATIONS],
                  "validate": [("reports/20260101-rule.md", "# 规则检查\n\n- [x] 合成\n")]}[command]
        steps = [_lock(command), *items, *(_write(rel, body) for rel, body in writes)]
        if key not in AutoEngine.leave_open:
            steps.append(_release_all)
        return [*steps, result()]

    def _marker_slice(self) -> str:
        """指令不带分段：按执行标记里的这一段（全量模式是「全部」，与工具箱 blueprint.py brief 一样）。"""
        try:
            marker = json.loads((Path(self.ctx.cwd) / ".onto" / "run" / "exec.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return UNIT
        return str((marker.get("slice") or {}).get("id") or UNIT)

    async def send(self, text: str, context: str | None = None) -> None:
        AutoEngine.sent.append(text)
        for item in self._script(text):
            await self.queue.put(item)

    async def events(self):
        while True:
            event = await self.queue.get()
            if callable(event):
                outcome = event(self.ctx)
                if asyncio.iscoroutine(outcome):
                    await outcome
                continue
            yield event

    async def interrupt(self) -> None:
        await self.queue.put(result(subtype="interrupted"))

    async def disconnect(self) -> None:
        self.client = None

    async def kill(self) -> None:
        self.client = None

    def model_fingerprint(self) -> str:
        return "fake"

    def config_stale(self) -> bool:
        return False

    def context_limits(self):
        return 200_000, 160_000

    def add_observer(self, observer) -> None:
        pass


def blueprint(root: Path) -> None:
    """资料分析写下的蓝图草案（F8 起不用确认，自主构建照样能开始）。"""
    module = contracts.default().load("blueprint")
    main_write(root, "research/blueprint.json", module.dumps(module.from_draft(DRAFT)), "research: 合成蓝图")


def wait_until(predicate, timeout: float = 90.0, message: str = "条件没等到"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(message)


def run_of(client, project_id: str, run_id: str) -> dict:
    return client.get(f"/api/v1/projects/{project_id}/autobuild/runs/{run_id}").json()


def wait_state(client, project_id: str, run_id: str, *states: str, timeout: float = 120.0) -> dict:
    return wait_until(lambda: (lambda run: run if run.get("state") in states else None)(run_of(client, project_id, run_id)),
                      timeout, f"运行没到 {states}")


def _expect_status(response, status: int) -> None:
    if response.status_code != status:
        raise AssertionError(response.text)


def start(client, project_id: str, **policy) -> dict:
    base = f"/api/v1/projects/{project_id}/autobuild"
    if policy:
        saved = client.put(f"{base}/policy", json=policy)
        _expect_status(saved, 200)
    preview = client.post(f"{base}/runs/preview", json={})
    _expect_status(preview, 200)
    started = client.post(f"{base}/runs", json={"preview_token": preview.json()["preview_token"]})
    _expect_status(started, 201)
    return started.json()


def project_events(project_id: str, kind: str | None = None) -> list[dict]:
    return [envelope for _n, _ts, envelope in list(streams.project_channel(project_id)._buffer)
            if kind is None or envelope["type"] == kind]


def history(client, project_id: str, conversation_id: str) -> list[dict]:
    return client.get(f"/api/v1/projects/{project_id}/conversations/{conversation_id}/messages",
                      params={"limit": 500}).json()["items"]


def _no_gate(*args, **kwargs):
    return {"errors": 0, "warnings": 0, "findings": []}


@pytest.fixture()
def ab(tmp_path: Path, monkeypatch):
    """一个有蓝图（全量模式，一段「全部」）的项目；门禁放行（合成文档不全，门禁另有用例）。"""
    from app import wiring

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(auth, "ENABLED", False)
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    workspaces = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", workspaces)
    manager = SessionManager()
    monkeypatch.setattr(manager, "_engine_class_for", lambda pid: AutoEngine)
    monkeypatch.setattr(agent_sessions, "manager", manager)
    monkeypatch.setattr(session_routes, "manager", manager)
    monkeypatch.setattr(gates, "compare", _no_gate)
    AutoEngine.reset()
    streams.reset_all()
    runner._tasks.clear()
    runner._admit_lock = None
    with TestClient(app) as client:
        monkeypatch.setattr(changesets, "_activity_provider", wiring._session_activity)
        project_id = workspaces.create_project("自主构建", owner="employee_0001")["id"]
        root = workspaces.project_root(project_id)
        blueprint(root)
        yield client, workspaces, project_id, root
    runner._tasks.clear()
    runner._admit_lock = None
    streams.reset_all()
