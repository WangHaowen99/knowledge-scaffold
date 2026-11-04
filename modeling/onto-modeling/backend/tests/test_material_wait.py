"""建模命令开始前等资料转成文本（数据 schema 自动识别、自动处理；session_host/material_wait）：

- 资料分析、对象 / 逻辑 / 规则指标提取、校验交给引擎前，项目里有资料在转文本就先等，状态带 waiting_materials；
- 等完（转成了、失败了、暂不支持）或到点照常开始；等的时候插话照常排着，打断就撤回这条命令；
- 仍没有文本版的资料由宿主注入点名「按未读处理」；不是建模命令不等也不注入；
- 没部署转换网关时「等待转换」等不来，不等。

假引擎，不起真实模型、不连网关。测试数据全部为合成内容。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.services.material_conversion import service as conversion, store
from app.services.session_host import injection, material_wait
from app.services.workspace.layout import _content_revision
from tests.test_session_host import FakeEngine, host, settle  # noqa: F401 - 夹具

PDF = "resource/others/合成说明.pdf"
DATA = b"%PDF-1.4 synthetic"


def put_material(root, project_id, state: str, rel: str = PDF) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(DATA)
    store.put(project_id, rel, {"state": state, "sha": _content_revision(DATA), "updated_at": time.time(),
                                **({"reason": "转换出错"} if state == "failed" else {})})


@pytest.fixture()
def quick(monkeypatch):
    monkeypatch.setattr(material_wait, "POLL_S", 0.01)
    monkeypatch.delenv("ONTO_WEB_CONNECTOR_GATEWAY_URL", raising=False)


def running_reasons(manager, project_id) -> list:
    return [event.get("reason") for event in manager.get_history(project_id, "default")
            if event.get("type") == "session_state" and event.get("state") == "running"]


def test_modeling_command_waits_for_conversion_then_starts(host, quick):
    manager, project_id, root = host
    put_material(root, project_id, "converting")

    async def scenario():
        await manager.send(project_id, root, "/ontology-extract-object")
        await asyncio.sleep(0.05)
        session = manager.get(project_id)
        waiting = (session.state, list(FakeEngine.sent), session.material_wait is not None)
        put_material(root, project_id, "failed")  # 转完了（失败也算完）
        await settle(manager, project_id, until=lambda s: s.state == "idle" and FakeEngine.sent)
        await manager.shutdown()
        return waiting

    assert asyncio.run(scenario()) == ("running", [], True)
    assert FakeEngine.sent == ["/ontology-extract-object"]
    # 状态先带「在等资料」，等完换回普通的运行中
    assert running_reasons(manager, project_id)[:2] == [material_wait.REASON, None]


def test_wait_is_bounded(host, quick, monkeypatch):
    manager, project_id, root = host
    monkeypatch.setattr(material_wait, "WAIT_LIMIT_S", 0.05)
    put_material(root, project_id, "converting")

    async def scenario():
        await manager.send(project_id, root, "/ontology-research")
        await settle(manager, project_id, until=lambda s: s.state == "idle" and FakeEngine.sent)
        await manager.shutdown()

    asyncio.run(scenario())
    assert FakeEngine.sent == ["/ontology-research"]


def test_steer_while_waiting_is_held_and_interrupt_withdraws_the_command(host, quick):
    manager, project_id, root = host
    put_material(root, project_id, "converting")

    async def scenario():
        await manager.send(project_id, root, "/ontology-validate")
        steer = await manager.submit(project_id, root, "顺便看看订单")
        await manager.interrupt(project_id)
        session = manager.get(project_id)
        after = (session.state, list(session.pending_prompts), session.material_wait)
        put_material(root, project_id, "failed")
        await asyncio.sleep(0.1)
        await manager.shutdown()
        return steer[1]["delivery"], after

    delivery, after = asyncio.run(scenario())
    assert delivery == "steer" and after == ("idle", [], None)
    assert FakeEngine.sent == []
    assert [item["text"] for item in manager.returned_steers[(project_id, "default")]] == ["顺便看看订单"]
    assert manager.interrupted_execs[(project_id, "default")]["state"] == "interrupted"


@pytest.mark.parametrize("text, state", [
    ("帮我看看订单对象", "converting"),         # 不是建模命令
    ("/ontology-extract-object", "waiting"),  # 没部署网关：等待转换等不来
])
def test_no_wait(host, quick, text, state):
    manager, project_id, root = host
    put_material(root, project_id, state)

    async def scenario():
        await manager.send(project_id, root, text)
        session = manager.get(project_id)
        snapshot = (session.material_wait, list(session.pending_prompts))
        await settle(manager, project_id)
        await manager.shutdown()
        return snapshot

    waiting, _ = asyncio.run(scenario())
    assert waiting is None and FakeEngine.sent == [text]
    assert material_wait.REASON not in running_reasons(manager, project_id)


def test_injection_names_materials_without_text_for_modeling_commands(host, quick):
    _manager, project_id, root = host
    put_material(root, project_id, "failed")
    put_material(root, project_id, "unsupported", rel="resource/others/合成扫描件.png")
    context = injection.build_context(root, user_text="/ontology-extract-object", session_start=True,
                                      last_injected=None).text
    assert "按未读处理" in context
    assert f"{PDF}（转换失败：转换出错）" in context and "resource/others/合成扫描件.png（暂不支持）" in context
    plain = injection.build_context(root, user_text="你好", session_start=True, last_injected=None).text
    assert "按未读处理" not in plain


def test_unready_skips_materials_that_have_a_text_version(host, quick, monkeypatch):
    """有文本版、记录也不在转换中的不逐份读原件算摘要：命令开始前、等转换时都要调。"""
    _manager, project_id, root = host
    put_material(root, project_id, "converting")
    done = "resource/others/合成规范.docx"
    (root / done).write_bytes(b"PK synthetic")
    text = root / "resource" / "text" / "others" / "合成规范.docx.md"
    text.parent.mkdir(parents=True, exist_ok=True)
    text.write_text('<!-- onto:text source="合成规范.docx" sha256="%s" converter="convert-docx@1.0.0" -->\n\n正文\n'
                    % _content_revision(b"PK synthetic"), encoding="utf-8")
    seen = []
    original = conversion.view
    monkeypatch.setattr(conversion, "view", lambda pid, r, rel, **kw: (seen.append(rel), original(pid, r, rel, **kw))[1])
    found = conversion.unready(project_id, root)
    assert [item["path"] for item in found] == [PDF] and found[0]["state"] == "converting"
    assert seen == [PDF]
    assert material_wait.converting(project_id) == [PDF]
