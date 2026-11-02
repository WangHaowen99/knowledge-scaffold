"""B5b 独立审查修复（A～M 与推断几条；复现脚本见审查报告）：每条一个修复前会失败的用例。

假引擎按消息现做（tests/autobuild_helpers.py），等待一律按条件。测试数据全部为合成内容。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.api.errors import ApiError
from app.services import agent_sessions
from app.services.autobuild import store
from tests.autobuild_helpers import (AutoEngine, ab, ask, gate, run_of, start, wait_state,  # noqa: F401
                                     wait_until)
from tests.test_changesets import git, main_write, obj

ONE = {"scope": {"mode": "selected", "slice_ids": ["s1"], "until_node": "core.model"}, "budget": {"max_tokens": 500_000}}


def _apply(rel: str, text: str):
    def action(ctx):
        path = Path(ctx.cwd) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return action


def _delete(rel: str):
    def action(ctx):
        (Path(ctx.cwd) / rel).unlink(missing_ok=True)
    return action


# ---------------------------------------------------------------------------
# A：运行中别的入口绕过 409（Studio 转助手复核、取消排队、放弃执行、会话池里直接发）
# ---------------------------------------------------------------------------


def test_A_internal_entry_points_cannot_send_during_a_run(ab):
    client, _workspaces, project_id, root = ab
    main_write(root, "ontology/object/门店.md", obj("门店", "原始描述"), "web: 门店")
    release = gate("/ontology-extract-object:s1")
    run = start(client, project_id, **ONE)
    wait_until(lambda: store.get(project_id, run["run_id"])["cursor"].get("exec_id"))
    sent = len(AutoEngine.sent)
    base = f"/api/v1/projects/{project_id}/studio/entries/object:门店"
    edited = obj("门店", "用户在 Studio 改的描述") + "\n## 备注\n\n用户补充\n"
    assert client.put(f"{base}/draft", json={"content": edited}).status_code == 200
    submitted = client.post(f"{base}/submit", json={})
    assert submitted.status_code == 409 and submitted.json()["code"] == "autobuild_running"
    cancel = client.post(f"/api/v1/projects/{project_id}/conversations/{run['conversation_id']}/queue/cancel")
    assert cancel.json()["code"] == "autobuild_running"
    exec_id = store.get(project_id, run["run_id"])["cursor"]["exec_id"]
    assert client.post(f"/api/v1/projects/{project_id}/execs/{exec_id}/abandon").json()["code"] == "autobuild_running"

    # 会话池这一关：没有运行器豁免的发送一律 409（新加的内部入口也漏不掉）
    async def direct():
        await agent_sessions.manager.send(project_id, root, "你好", run["conversation_id"], username="employee_0001")

    with pytest.raises(ApiError) as caught:
        client.portal.call(direct)
    assert caught.value.code == "autobuild_running"
    assert len(AutoEngine.sent) == sent
    release.set()
    wait_state(client, project_id, run["run_id"], "completed")
    record = store.get(project_id, run["run_id"])
    files = git(root, "-c", "core.quotePath=false", "show", "--name-only", "--format=", record["slices"]["s1"]["commit"])
    assert "ontology/object/门店.md" not in files.split()
