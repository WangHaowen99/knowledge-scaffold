"""插件运行时接到会话宿主与项目流（ADR-0045，`app/wiring.py`）：合并 B4c 后的五个衔接点。

- S-20 的 `action` 由扩展项注册表换成包里的命令，包停用后 409 pack_not_enabled；
- agent 一轮提交了产物文件，确认生效后项目流 workspace.changed 的 artifacts 有值，并发 artifacts.updated；
- 停用、启用带页面的包，extensions.updated.changed 只含变了的类；
- assembly.updated 每次（锁文件提交、装配切换）只发一次；
- 只有启用了声明提示规则的包（如领域包扩展）的项目注入对应提示，两个引擎一致。

用仓库里真实的官方包；假引擎，不起真实 CLI；测试数据全部为合成内容。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.main import app
from app.routers import sessions as session_routes
from app.services import agent_sessions, auth, streams
from app.services.agent_sessions import SessionManager
from app.services.engines.base import EngineContext
from app.services.packs import builtin
from app.services.session_host import fingerprint, injection
from app.services.workspace import WorkspaceManager
from tests.proc import run as run_cmd
from tests.test_event_streams import buffered
from tests.test_session_host import FakeEngine, result

SIGNAL = "/domain-extend-add"
SCENARIO_FILE = "ontology/testcase/补货编制主流程.md"
SCENARIO_TEXT = ("# 测试场景：补货编制主流程\n\n## 场景 1：正常补货\n\n- **输入**：\n  - `qty` = `12`\n"
                 "- **预期路径**：正常完成\n- **数据来源**：自定义\n")


@pytest.fixture()
def host(tmp_path: Path, monkeypatch):
    builtin.clear_cache()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    workspaces = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", workspaces)
    monkeypatch.setattr(auth, "ENABLED", False)
    manager = SessionManager()
    monkeypatch.setattr(manager, "_engine_class_for", lambda project_id: FakeEngine)
    monkeypatch.setattr(agent_sessions, "manager", manager)
    monkeypatch.setattr(session_routes, "manager", manager)
    FakeEngine.scripts, FakeEngine.sent = [], []
    streams.reset_all()
    with TestClient(app) as client:
        project_id = client.post("/api/v1/projects", json={"name": "衔接", "goal": "合成场景：门店补货"}).json()["id"]
        yield client, workspaces, project_id, workspaces.project_root(project_id)
    streams.reset_all()
    builtin.clear_cache()


def wait(predicate, timeout: float = 10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("等待超时")


def _pack(client, project_id: str, name: str, *, enable: bool) -> None:
    if enable:
        response = client.post(f"/api/v1/projects/{project_id}/packs", json={"name": name})
        assert response.status_code == 201, response.text
    else:
        response = client.post(f"/api/v1/projects/{project_id}/packs/{name}/disable")
        assert response.status_code == 200, response.text


def _count(project_id: str, event_type: str) -> int:
    return len(buffered(project_id, event_type))


# ---- S-20：页面按钮 → 包里的命令 ----


def test_page_action_resolves_to_pack_command_and_disabled_pack_is_409(host):
    client, _, project_id, _ = host
    action = {"pack": "trace", "page": "scenarios", "action_id": "batch"}
    body = {"action": action, "args": {"scope": "all"}, "origin": {"page": "scenarios", "label": "场景模拟"}}
    sent = client.post(f"/api/v1/projects/{project_id}/commands", json=body)
    assert sent.status_code == 202, sent.text
    assert wait(lambda: FakeEngine.sent)[0] == "/ontology-trace-batch scope=all"
    assert sent.json()["task"]["title"].startswith("助手正在执行「")

    _pack(client, project_id, "trace", enable=False)
    blocked = client.post(f"/api/v1/projects/{project_id}/commands", json=body)
    assert blocked.status_code == 409 and blocked.json()["code"] == "pack_not_enabled"
    assert blocked.json()["details"]["pack"] == "trace"

    # 停用生效（下一条消息前切换）后再启用：按钮按锁文件认得出，但装配要等下一条消息才切过去
    fingerprint.prepare(project_id)
    _pack(client, project_id, "trace", enable=True)
    pending = client.post(f"/api/v1/projects/{project_id}/commands", json=body)
    assert pending.status_code == 409 and pending.json()["details"] == {
        "command": "/ontology-trace-batch", "effective": "next_message"}


# ---- workspace.changed.artifacts ----


def test_agent_round_commit_reports_artifacts_on_project_stream(host):
    client, _, project_id, root = host

    def write_and_commit(ctx):
        path = Path(ctx.cwd) / SCENARIO_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(SCENARIO_TEXT, encoding="utf-8")
        run_cmd(["git", "add", SCENARIO_FILE], cwd=ctx.cwd, check=True, capture_output=True)
        run_cmd(["git", "commit", "-qm", "agent: 补场景"], cwd=ctx.cwd, check=True, capture_output=True)

    FakeEngine.scripts = [[write_and_commit, result()]]
    client.post(f"/api/v1/projects/{project_id}/conversations/default/messages", json={"text": "补一个补货场景"})

    # 助手在变更集副本里提交（B4b）；F3 起一轮结束就合入成一个版本
    def merged():
        session = agent_sessions.manager.get(project_id)
        if session is None or session.state != "idle":
            return None
        found = client.get(f"/api/v1/projects/{project_id}/changesets", params={"state": "merged"}).json()
        return found["items"][0] if found["items"] else None

    changeset = wait(merged)
    assert (root / SCENARIO_FILE).is_file()

    def agent_change():
        return next((event["data"] for event in buffered(project_id, "workspace.changed")
                     if event["data"]["source"] == "assistant"), None)

    data = wait(agent_change)
    assert data["changeset_id"] == changeset["id"]
    assert SCENARIO_FILE in data["paths"]
    assert [(ref["type"], ref["key"]) for ref in data["artifacts"]] == [("trace.scenario", "补货编制主流程--1")]
    assert set(data["artifacts"][0]) == {"type", "key", "title"}
    updated = [event["data"] for event in buffered(project_id, "artifacts.updated")]
    assert {"type": "trace.scenario", "change": "updated", "keys": ["补货编制主流程--1"]} in updated


def test_web_artifact_write_sends_one_artifacts_updated(host):
    """网页经 AR-06 新建：项目流只有一条 created（提交事件触发的产物映射不再补一条 updated），
    workspace.changed 里照样带上这个产物。"""
    client, _, project_id, _ = host
    created = client.post(f"/api/v1/projects/{project_id}/artifacts/trace.scenario",
                          json={"key": "补货编制主流程", "fields": {"title": "正常补货"}})
    assert created.status_code == 201, created.text
    key = created.json()["artifact"]["key"]
    events = [event["data"] for event in buffered(project_id, "artifacts.updated")]
    assert events == [{"type": "trace.scenario", "change": "created", "keys": [key]}]
    change = buffered(project_id, "workspace.changed")[-1]["data"]
    assert change["source"] == "web" and [ref["key"] for ref in change["artifacts"]] == [key]


# ---- extensions.updated、assembly.updated ----


def test_extensions_changed_lists_only_changed_kinds(host):
    client, _, project_id, _ = host
    _pack(client, project_id, "trace", enable=False)
    disabled = buffered(project_id, "extensions.updated")[-1]["data"]
    # 场景模拟包有左栏页面、产物类型与视图，没有资料子页、入口、门禁
    assert disabled["changed"] == ["nav", "pages", "artifact_types", "views"]
    # 停用在下一条消息前生效：左栏、产物类型早就跟着锁文件变了，这次没有哪一类再变
    fingerprint.prepare(project_id)
    assert buffered(project_id, "extensions.updated")[-1]["data"]["changed"] == []

    _pack(client, project_id, "trace", enable=True)
    enabled = buffered(project_id, "extensions.updated")[-1]["data"]
    assert enabled["changed"] == ["nav", "pages", "artifact_types", "views"] and enabled["pending_hash"]

    # 下一条消息前切换装配：只有页面按钮从「待生效」变成可用
    fingerprint.prepare(project_id)
    switched = buffered(project_id, "extensions.updated")[-1]["data"]
    assert switched["changed"] == ["pages"] and switched["pending_hash"] is None


def test_assembly_updated_once_per_lock_commit_and_once_per_switch(host):
    client, _, project_id, _ = host
    assert _count(project_id, "assembly.updated") == 0

    # 能力包（H1）：锁文件提交时一次「待生效」，下一条消息前切换时一次「已生效」
    _pack(client, project_id, "codegen", enable=False)
    events = buffered(project_id, "assembly.updated")
    assert [(event["data"]["state"], event["data"]["effective"]) for event in events] == [("pending", "next_message")]
    fingerprint.prepare(project_id)
    fingerprint.prepare(project_id)  # 已经切过了：不再发
    events = buffered(project_id, "assembly.updated")
    assert [event["data"]["state"] for event in events] == ["pending", "applied"]

    # 纯数据的行业包（H0）：锁文件提交一次，当场切换一次，之后的消息前不再发
    _pack(client, project_id, "industry-retail", enable=True)
    fingerprint.prepare(project_id)
    events = buffered(project_id, "assembly.updated")
    assert [(event["data"]["state"], event["data"]["effective"]) for event in events[2:]] == [
        ("pending", "immediate"), ("applied", "immediate")]
    # 每次 assembly.updated 配一次 extensions.updated（没有插件运行时自己再发一份）
    assert _count(project_id, "extensions.updated") == _count(project_id, "assembly.updated") == 4


# ---- 包的提示规则：只在启用了声明规则的包的项目里注入 ----


def _ctx(root: Path) -> EngineContext:
    return EngineContext(project_id=root.name, cwd=root, request_permission=None, load_resume_token=lambda: None,
                         save_resume_token=lambda token: None, clear_resume_token=lambda: None,
                         log_error=lambda kind, text: None, injector=injection.Injector(root), engine_instance="e1")


def _both_engines(root: Path, message: str) -> tuple[str, str]:
    from app.services.engines.claude import ClaudeEngine
    from app.services.engines.jiuwen import JiuwenEngine

    claude, jiuwen = ClaudeEngine(_ctx(root)), JiuwenEngine(_ctx(root))

    async def scenario():
        hook = await claude._on_prompt({"prompt": message})
        return (hook.get("hookSpecificOutput") or {}).get("additionalContext", ""), await jiuwen._model_query(message)

    return asyncio.run(scenario())
def test_pack_prompt_rule_only_with_declaring_pack_for_both_engines(host):
    client, manager, project_id, root = host
    message = "这一轮就到这，结束本轮"
    plain_id = manager.create_project("无提示", packs=[])["id"]
    plain = manager.project_root(plain_id)

    # 缺省能力包都没有声明这条规则：两个引擎都不带
    for target in (root, plain):
        claude_text, query = _both_engines(target, message)
        assert SIGNAL not in claude_text and SIGNAL not in query
    assert injection.prompt_hint(plain, message) == ""

    # 启用后等装配切过去（下一条消息前）才注入
    _pack(client, plain_id, "domain-extend", enable=True)
    assert injection.prompt_hint(plain, message) == ""
    fingerprint.prepare(plain_id)
    assert SIGNAL in injection.prompt_hint(plain, message)
    claude_text, query = _both_engines(plain, message)
    assert SIGNAL in claude_text and query == injection.compose_query(message, claude_text)
    # 不匹配规则的消息不带
    assert injection.prompt_hint(plain, "帮我画一张流程图") == ""
