"""写入范围收窄到当前切片（详设 4.3「落到当前切片」；契约 7.6 ScopeLock、CS-06、2.4；ADR-0052）。

- 助手在跑：建模节点只锁这一片的条目（加上这一轮改过的、决策卡预览列出的），别的片照常能改；
- 这一轮结束、修改待确认：锁变更集里做过的那几片；
- 全量模式（没有蓝图或只有一片）：仍按节点声明的整类条目；
- 守卫一侧：state_helper acquire 按这一片记下片外已有的条目（protect），守卫不让助手改它们，新建的照常。

测试数据全部为合成内容。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.kernel.changesets import service
from app.services.packs import contracts
from app.services.workspace import WorkspaceError
from tests.test_changesets import agent_write, main_write, obj
from tests.test_write_scope import env  # noqa: F401 - 共用 fixture

DRAFT = {"slices": [{"name": "订单受理", "objects": ["订单"], "operations": ["受理订单"]},
                    {"name": "客户档案", "objects": ["客户"], "operations": ["维护客户"]}]}


def _blueprint(root: Path) -> None:
    module = contracts.default().load("blueprint")
    main_write(root, "research/blueprint.json", module.dumps(module.confirm(module.from_draft(DRAFT))),
               "research: 合成蓝图")


def running(slice_ref=None, affected=None):
    record = {"id": "ex-1", "node": {"id": "core.model", "version": "core@0.3.0", "title": "建模"},
              "slice": slice_ref, "step": "object", "started_at": 1.0}
    return lambda pid: {"running": True, "conversation_id": "default", "exec": record, "affected": affected or []}


def test_running_slice_locks_only_its_entries(env, monkeypatch):
    client, workspaces, project_id, root = env
    _blueprint(root)
    monkeypatch.setattr(service, "_activity_provider", running({"id": "s1", "name": "订单受理"}))
    lock = service.scope_lock(workspaces, project_id)
    assert lock["state"] == "assistant_running" and lock["slice"] == {"id": "s1", "name": "订单受理"}
    assert "ontology/object/*" not in lock["paths"] and "ontology/object/订单.md" in lock["paths"]
    assert "ontology/testcase/受理订单.md" in lock["paths"]  # 这一片的场景也在范围里
    assert {ref["id"] for ref in lock["entries"]} >= {"object:订单", "operation:受理订单"}
    assert "「订单受理」这一片" in lock["reason_text"]
    with pytest.raises(WorkspaceError) as exc:
        workspaces.write_file(project_id, "ontology/object/订单.md", obj("订单", "你在网页上改"), author="employee_0001")
    assert exc.value.status == 423
    # 别的片的条目照常改，不再锁整类
    saved = workspaces.write_file(project_id, "ontology/object/客户.md", obj("客户", "别的片照常改"),
                                  author="employee_0001")
    assert saved["commit"]
    base = f"/api/v1/projects/{project_id}/scope"
    assert client.get(base, params={"target": "object:订单"}).json()["state"] == "assistant_running"
    assert client.get(base, params={"target": "object:客户"}).json()["state"] == "none"
    # 决策卡预览列出的片外条目也进范围
    affected = [{"kind": "object", "id": "object:客户", "key": "客户", "name": "客户"}]
    monkeypatch.setattr(service, "_activity_provider", running({"id": "s1", "name": "订单受理"}, affected))
    assert client.get(base, params={"target": "object:客户"}).json()["state"] == "assistant_running"


def test_pending_changeset_locks_the_slices_it_worked_on(env, monkeypatch):
    _client, workspaces, project_id, root = env
    _blueprint(root)
    copy = service.copy_for_turn(workspaces, project_id, root)
    agent_write(copy, "ontology/object/订单.md", obj("订单", "助手改了"), commit="agent: 订单")
    record = service.observe(workspaces, project_id, running=False, conversation_id="default",
                             exec_record={"id": "ex-1", "command_label": "建模",
                                          "node": {"id": "core.model", "version": "core@0.3.0"},
                                          "slice": {"id": "s1", "name": "订单受理"}, "step": "object"})
    assert record["slice_steps"] == [["s1", "object"]]
    lock = service.scope_lock(workspaces, project_id)
    assert lock["state"] == "pending_confirm" and lock["slice"] == {"id": "s1", "name": "订单受理"}
    assert "ontology/operation/受理订单.md" in lock["paths"] and "ontology/object/*" not in lock["paths"]
    workspaces.write_file(project_id, "ontology/object/客户.md", obj("客户", "片外照常改"), author="employee_0001")
    with pytest.raises(WorkspaceError):
        workspaces.write_file(project_id, "ontology/operation/受理订单.md", "# 受理订单\n", author="employee_0001")


def test_full_mode_keeps_the_whole_class_scope(env, monkeypatch):
    _client, workspaces, project_id, _root = env
    monkeypatch.setattr(service, "_activity_provider", running({"id": "all", "name": "全部"}))
    lock = service.scope_lock(workspaces, project_id)
    assert "ontology/object/*" in lock["paths"] and lock["slice"] is None
    with pytest.raises(WorkspaceError):
        workspaces.write_file(project_id, "ontology/object/客户.md", obj("客户", "x"), author="employee_0001")


@pytest.mark.skipif(shutil.which("bash") is None, reason="需要 bash")
def test_guard_keeps_the_assistant_inside_its_slice(env):
    _client, workspaces, project_id, root = env
    _blueprint(root)
    copy = service.copy_for_turn(workspaces, project_id, root)
    agent_write(copy, "ontology/object/新对象.md", obj("新对象", "这一轮新建"))  # 这一轮自己建的不算片外
    (copy / ".onto" / "run").mkdir(parents=True, exist_ok=True)
    (copy / ".onto" / "run" / "exec.json").write_text(json.dumps({"exec_id": "ex-1", "node": "core.model",
                                                                  "slice": {"id": "s1", "name": "订单受理"}}))
    helper = ["python3", str(copy / ".claude" / "scripts" / "state_helper.py")]
    subprocess.run([*helper, "acquire", "extract-object", "objects"], cwd=copy, check=True, capture_output=True)
    spec = json.loads((copy / ".onto" / "run" / "active.json").read_text(encoding="utf-8"))["commands"]["extract-object"]
    assert spec["node"] == "core.model" and spec["slice"] == "s1"
    assert "ontology/object/客户.md" in spec["protect"] and "ontology/object/订单.md" not in spec["protect"]
    assert "ontology/object/新对象.md" not in spec["protect"]

    def guard(rel: str) -> str:
        payload = {"tool_name": "Write", "tool_input": {"file_path": str(copy / rel), "content": "x"}}
        proc = subprocess.run(["bash", str(copy / ".claude" / "scripts" / "ontology_scope_guard.sh")],
                              input=json.dumps(payload, ensure_ascii=False), cwd=copy, capture_output=True,
                              text=True, timeout=30, env={**os.environ, "CLAUDE_PROJECT_DIR": str(copy)})
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()

    def denied(output: str) -> bool:
        return bool(output) and json.loads(output.splitlines()[-1]).get("hookSpecificOutput", {}).get(
            "permissionDecision") == "deny"

    assert denied(guard("ontology/object/客户.md"))
    assert "别的片已有的条目只读" in guard("ontology/object/客户.md")
    assert not denied(guard("ontology/object/订单.md"))
    assert not denied(guard("ontology/object/又一个新对象.md"))
    subprocess.run([*helper, "release", "extract-object"], cwd=copy, check=True, capture_output=True)
