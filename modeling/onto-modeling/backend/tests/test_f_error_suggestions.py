"""F 第一批第 6 条：出错（引擎退出、模型流超时）后建议条是空的。

F7 起按规则表：这一轮出错时主推「重试这一轮」（第 3 条），说明行写出错原因；会话不在了、重试不了时，
退到中断的那一步「继续「X」」（第 4 条）。续跑卡上的按钮保留。
"""
from __future__ import annotations

import asyncio

from app.services.flow import view
from app.services.session_host import exec_records
from tests.test_session_host import FakeEngine, host, lock, project_history, result, settle  # noqa: F401 - 夹具


def test_engine_error_offers_resume_like_user_interrupt(host):  # noqa: F811
    manager, project_id, root = host
    (root / "ontology" / "object").mkdir(parents=True, exist_ok=True)
    FakeEngine.scripts = [
        [lock("extract-operation", "operation"),
         {"type": "assistant_message", "blocks": [{"kind": "text", "text": "开始写操作"}]},
         result(subtype="error_during_execution", is_error=True),
         {"type": "_engine_end", "reason": "error", "error": "模型服务暂时不稳定，请稍后重试"}],
    ]

    async def scenario():
        await manager.send(project_id, root, "/ontology-extract-operation")
        await settle(manager, project_id, until=lambda s: s.state == "error")
        await manager.shutdown()

    asyncio.run(scenario())
    envelopes = project_history(manager, project_id)
    interrupted = [item for item in exec_records.query(project_id) if item["state"] == "interrupted"]
    assert len(interrupted) == 1 and interrupted[0]["interrupted"]["reason"] == "engine_exit"
    assert any(item["type"] == "card" and item["data"].get("card_type") == "resume" for item in envelopes)
    suggestions = [item for item in envelopes if item["type"] == "suggestions.updated"][-1]["data"]
    assert suggestions["mode"] == "failed"
    assert suggestions["primary"]["label"] == "重试这一轮" and suggestions["primary"]["action"] == {"type": "retry"}
    assert suggestions["note"] == "模型服务暂时不稳定，请稍后重试"
    # 会话已经关了（重试不了）：刷新页面（FL-02）退到中断的那一步，主推继续、次选放弃
    again = view.build(root, conversation_id="default")["suggestions"]
    assert again["mode"] == "interrupted"
    assert again["primary"]["label"] == "继续「提取操作」"
    assert again["primary"]["action"] == {"type": "resume_exec", "exec_id": interrupted[0]["id"]}
    assert [item["action"]["type"] for item in again["secondary"]] == ["abandon_exec"]
    assert again["note"]


def test_error_without_running_command_keeps_suggestions(host):  # noqa: F811
    manager, project_id, root = host
    FakeEngine.scripts = [
        [{"type": "assistant_message", "blocks": [{"kind": "text", "text": "答"}]},
         result(subtype="error_during_execution", is_error=True),
         {"type": "_engine_end", "reason": "error", "error": "模型服务暂时不稳定，请稍后重试"}],
    ]

    async def scenario():
        await manager.send(project_id, root, "门店有几类？")
        await settle(manager, project_id, until=lambda s: s.state == "error")
        await manager.shutdown()

    asyncio.run(scenario())
    envelopes = project_history(manager, project_id)
    assert all(item["data"].get("mode") != "interrupted" for item in envelopes if item["type"] == "suggestions.updated")
    # 普通对话出错也给「重试这一轮」
    suggestions = [item for item in envelopes if item["type"] == "suggestions.updated"][-1]["data"]
    assert suggestions["mode"] == "failed" and suggestions["primary"]["action"] == {"type": "retry"}
