"""创建时填写的建模目标：完整保存、开始建模前经确认卡确认、确认后才注入助手提示词。

确认前发消息会被拦下，避免助手在拿不到目标时开工、之后又让用户重说一遍。
两个引擎（Claude / Jiuwen）共用同一段提示词。工作区用真实 WorkspaceManager，
路由测试手写元数据文件代替建项目流程，并直接调用路由函数。
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import config
from app.routers import projects, sessions
from app.services import agent_sessions
from app.services.agent_sessions import Session, SessionManager
from app.services.engines import claude as claude_engine
from app.services.engines.base import EngineContext, project_goal_section
from app.services.engines.claude import ClaudeEngine
from app.services.engines.jiuwen_assets import JiuwenAssetLoader
from app.services.workspace import (
    GOAL_CONFIRMED,
    GOAL_PENDING,
    META_FILE,
    WorkspaceError,
    WorkspaceManager,
    confirmed_goal,
)

PID = "p1"
GOAL_HEADING = "# 用户确认的初始建模目标"
# 合成目标，长度超过旧版的 200 字截断；两条流程分别在开头和结尾
LONG_GOAL = (
    "整理合成仓储场景的库存知识包。流程甲：入库登记与质检。"
    + "补充说明：只覆盖示例仓库的标准作业。" * 12
    + "流程乙：出库复核与盘点。"
)


@pytest.fixture()
def manager(tmp_path: Path) -> WorkspaceManager:
    assets = tmp_path / "assets"
    (assets / "workspace-toolkit" / "templates").mkdir(parents=True)
    (assets / "workspace-rules.md").write_text("# rules", encoding="utf-8")
    (assets / "resource-template" / "others").mkdir(parents=True)
    (assets / "resource-template" / "others" / "README.md").write_text(
        "资料区", encoding="utf-8"
    )
    return WorkspaceManager(tmp_path / "data", assets)


def write_meta(workspaces: WorkspaceManager, **fields) -> Path:
    root = workspaces.workspaces_dir / PID
    root.mkdir(parents=True, exist_ok=True)
    meta = {"id": PID, "name": "示例任务", **fields}
    (root / META_FILE).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return root


# ---- 保存与确认 ----


def test_create_project_keeps_full_goal_pending_confirmation(manager: WorkspaceManager):
    assert len(LONG_GOAL) > 200
    meta = manager.create_project("目标确认", background=f"  {LONG_GOAL}\n")

    stored = manager.get_project(meta["id"])
    assert stored["background"] == LONG_GOAL
    assert stored["goal_status"] == GOAL_PENDING
    assert confirmed_goal(stored) == ""


@pytest.mark.parametrize("background", ["", "  \n "])
def test_create_project_without_goal_skips_confirmation(
    manager: WorkspaceManager, background: str
):
    meta = manager.create_project("无目标", background=background)

    stored = manager.get_project(meta["id"])
    assert stored["background"] == ""
    assert "goal_status" not in stored


def test_confirm_goal_keeps_original_text(manager: WorkspaceManager):
    write_meta(manager, background=LONG_GOAL, goal_status=GOAL_PENDING)

    meta = manager.confirm_goal(PID)

    assert meta["goal_status"] == GOAL_CONFIRMED
    assert meta["background"] == LONG_GOAL
    assert confirmed_goal(manager.get_project(PID)) == LONG_GOAL


def test_confirm_goal_with_new_text_replaces_goal(manager: WorkspaceManager):
    write_meta(manager, background="原目标", goal_status=GOAL_PENDING)

    meta = manager.confirm_goal(PID, "  新目标：只覆盖流程甲。\n")

    assert meta["background"] == "新目标：只覆盖流程甲。"
    assert manager.get_project(PID)["goal_status"] == GOAL_CONFIRMED


def test_confirm_goal_rejects_blank_text_and_stays_pending(manager: WorkspaceManager):
    write_meta(manager, background="原目标", goal_status=GOAL_PENDING)

    with pytest.raises(WorkspaceError) as exc:
        manager.confirm_goal(PID, "   ")

    assert exc.value.status == 400
    stored = manager.get_project(PID)
    assert stored["goal_status"] == GOAL_PENDING
    assert stored["background"] == "原目标"


@pytest.mark.parametrize(
    "fields",
    [
        {"background": "原目标", "goal_status": GOAL_CONFIRMED},  # 重复确认
        {"background": "存量项目目标"},  # 存量项目没有确认卡
        {},
    ],
)
def test_confirm_goal_only_accepts_pending_goal(manager: WorkspaceManager, fields: dict):
    write_meta(manager, **fields)

    with pytest.raises(WorkspaceError) as exc:
        manager.confirm_goal(PID, "新目标")

    assert (exc.value.status, exc.value.code) == (409, "goal_not_pending")
    assert manager.get_project(PID).get("background") == fields.get("background")


def test_confirmed_goal_only_returns_confirmed_text():
    assert confirmed_goal({"background": " 目标 ", "goal_status": GOAL_CONFIRMED}) == "目标"
    assert confirmed_goal({"background": "目标", "goal_status": GOAL_PENDING}) == ""
    assert confirmed_goal({"background": "存量项目目标"}) == ""


# ---- 确认前拦截对话 ----


@pytest.fixture()
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    workspaces = WorkspaceManager(data, tmp_path / "assets")
    monkeypatch.setattr(projects, "workspaces", workspaces)
    sent = []

    async def send(project_id, cwd, text, conversation_id, username=""):
        sent.append((project_id, text))
        return SimpleNamespace(id="s1", state="running", conversation_id="default")

    monkeypatch.setattr(sessions, "manager", SimpleNamespace(send=send))
    write_meta(workspaces, background=LONG_GOAL, goal_status=GOAL_PENDING)
    return SimpleNamespace(
        workspaces=workspaces,
        sent=sent,
        request=SimpleNamespace(state=SimpleNamespace(user=None), headers={}),  # 鉴权关闭
    )


def send_message(env, text: str = "开始建模") -> dict:
    body = sessions.MessageBody(text=text)
    return asyncio.run(sessions.send_message(PID, body, env.request))


def test_message_is_blocked_until_goal_confirmed(env):
    with pytest.raises(WorkspaceError) as exc:
        send_message(env)
    assert (exc.value.status, exc.value.code) == (409, "goal_confirmation_required")
    assert env.sent == []

    meta = projects.confirm_project_goal(PID, projects.GoalBody(), env.request)

    assert meta["goal_status"] == GOAL_CONFIRMED
    assert meta["background"] == LONG_GOAL
    assert send_message(env)["state"] == "running"
    assert env.sent == [(PID, "开始建模")]


def test_modified_goal_is_saved_before_chat_starts(env):
    body = projects.GoalBody(background="新目标：只覆盖流程乙。")

    meta = projects.confirm_project_goal(PID, body, env.request)

    assert meta["background"] == "新目标：只覆盖流程乙。"
    assert confirmed_goal(env.workspaces.get_project(PID)) == "新目标：只覆盖流程乙。"
    send_message(env)
    assert env.sent == [(PID, "开始建模")]


def test_project_without_goal_chats_without_confirmation(env):
    write_meta(env.workspaces, background="")

    send_message(env)

    assert env.sent == [(PID, "开始建模")]


# ---- 注入提示词 ----


def test_project_goal_section_quotes_goal_verbatim():
    section = project_goal_section("流程甲：入库登记\n\n流程乙：出库复核\n")

    assert section.startswith(GOAL_HEADING)
    assert "> 流程甲：入库登记\n>\n> 流程乙：出库复核\n" in section
    assert "不得再要求用户重新说明" in section
    assert project_goal_section("") == ""
    assert project_goal_section("  \n ") == ""


def test_jiuwen_system_prompt_appends_goal_section_last():
    loader = JiuwenAssetLoader(Path(config.ASSETS_DIR))

    plain = loader.system_prompt()
    prompt = loader.system_prompt(LONG_GOAL)

    assert GOAL_HEADING not in plain
    assert prompt == plain + "\n\n" + project_goal_section(LONG_GOAL)


def claude_system_prompt(tmp_path: Path, monkeypatch, goal: str):
    monkeypatch.setattr(claude_engine, "_mcp_servers", lambda project_id="": {})
    monkeypatch.setattr(claude_engine, "_owner_env", lambda owner: {})
    monkeypatch.setattr(claude_engine, "_permission_mode", lambda: "default")
    ctx = EngineContext(
        project_id=PID,
        cwd=tmp_path,
        request_permission=None,
        load_resume_token=lambda: None,
        save_resume_token=lambda token: None,
        clear_resume_token=lambda: None,
        log_error=lambda kind, text: None,
        project_goal=goal,
    )
    return ClaudeEngine(ctx)._build_options(None).system_prompt


def test_claude_options_append_goal_section_only_with_goal(tmp_path, monkeypatch):
    preset = {"type": "preset", "preset": "claude_code"}

    assert claude_system_prompt(tmp_path, monkeypatch, "") == preset
    assert claude_system_prompt(tmp_path, monkeypatch, LONG_GOAL) == {
        **preset,
        "append": project_goal_section(LONG_GOAL),
    }


class CaptureEngine:
    name = "capture"

    def __init__(self, ctx: EngineContext):
        self.ctx = ctx


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"background": LONG_GOAL, "goal_status": GOAL_CONFIRMED}, LONG_GOAL),
        ({"background": LONG_GOAL, "goal_status": GOAL_PENDING}, ""),
        ({"background": "存量项目目标"}, ""),
    ],
)
def test_session_engine_receives_only_confirmed_goal(tmp_path, monkeypatch, fields, expected):
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    root = write_meta(WorkspaceManager(data, tmp_path / "assets"), **fields)
    monkeypatch.setattr(agent_sessions, "get_engine_class", lambda name: CaptureEngine)

    engine = SessionManager()._make_engine(Session(id="s1", project_id=PID, cwd=root))

    assert engine.ctx.project_goal == expected
