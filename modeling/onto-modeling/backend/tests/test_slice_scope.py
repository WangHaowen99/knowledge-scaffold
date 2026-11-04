"""写入范围（详设 4.3；契约 7.6 ScopeLock、CS-06、2.4；ADR-0052、ADR-0078）。

F8（ADR-0078）起不分段：写入范围一律按节点声明的整类条目，不再收窄到某一段。F8 之前按段建的项目（蓝图里存着多段）、
带老分段的执行记录与变更集照样按整类条目锁：
- 助手在跑：建模节点锁整类条目，别的条目也要等这一轮结束；
- 这一轮结束、修改还没合入：锁变更集做过的节点的整类条目；
- 守卫一侧：state_helper acquire 不再记 protect，守卫不按段挡助手。

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
    """F8 之前写下的两段蓝图（现在的脚本不再写多段，按老格式拼出来）。"""
    module = contracts.default().load("blueprint")
    doc = module.from_draft(DRAFT)
    base = doc["slices"][0]
    doc["slices"] = [{**base, "id": f"s{n}", "name": item["name"], "objects": item["objects"],
                      "operations": item["operations"], "rules": [], "depends_on": []}
                     for n, item in enumerate(DRAFT["slices"], start=1)]
    doc["next_ids"]["slice"] = 3
    main_write(root, "research/blueprint.json", module.dumps(module.confirm(doc)), "research: 合成蓝图")


def running(slice_ref=None, affected=None):
    record = {"id": "ex-1", "node": {"id": "core.model", "version": "core@0.3.0", "title": "建模"},
              "slice": slice_ref, "step": "object", "started_at": 1.0}
    return lambda pid: {"running": True, "conversation_id": "default", "exec": record, "affected": affected or []}


def test_running_old_slice_record_locks_the_whole_class(env, monkeypatch):
    client, workspaces, project_id, root = env
    _blueprint(root)
    monkeypatch.setattr(service, "_activity_provider", running({"id": "s1", "name": "订单受理"}))
    lock = service.scope_lock(workspaces, project_id)
    assert lock["state"] == "assistant_running" and lock["slice"] is None
    assert "ontology/object/*" in lock["paths"] and "这一段" not in lock["reason_text"]
    for name in ("订单", "客户"):  # 老蓝图里另一段的条目也一样锁住
        with pytest.raises(WorkspaceError) as exc:
            workspaces.write_file(project_id, f"ontology/object/{name}.md", obj(name, "你在网页上改"),
                                  author="employee_0001")
        assert exc.value.status == 423
    base = f"/api/v1/projects/{project_id}/scope"
    assert client.get(base, params={"target": "object:客户"}).json()["state"] == "assistant_running"


def test_pending_changeset_with_old_slice_steps_locks_the_whole_class(env, monkeypatch):
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
    assert lock["state"] == "pending_confirm" and lock["slice"] is None and "ontology/object/*" in lock["paths"]
    with pytest.raises(WorkspaceError):
        workspaces.write_file(project_id, "ontology/object/客户.md", obj("客户", "另一段的也锁住"), author="employee_0001")


def test_full_mode_keeps_the_whole_class_scope(env, monkeypatch):
    _client, workspaces, project_id, _root = env
    monkeypatch.setattr(service, "_activity_provider", running({"id": "all", "name": "全部"}))
    lock = service.scope_lock(workspaces, project_id)
    assert "ontology/object/*" in lock["paths"] and lock["slice"] is None
    with pytest.raises(WorkspaceError):
        workspaces.write_file(project_id, "ontology/object/客户.md", obj("客户", "x"), author="employee_0001")


@pytest.mark.skipif(shutil.which("bash") is None, reason="需要 bash")
def test_guard_does_not_narrow_to_old_slices(env):
    _client, workspaces, project_id, root = env
    _blueprint(root)
    copy = service.copy_for_turn(workspaces, project_id, root)
    assert (copy / "ontology/object/客户.md").is_file()  # 老蓝图里另一段已有的对象
    (copy / ".onto" / "run").mkdir(parents=True, exist_ok=True)
    (copy / ".onto" / "run" / "exec.json").write_text(json.dumps({"exec_id": "ex-1", "node": "core.model",
                                                                  "slice": {"id": "s1", "name": "订单受理"}}))
    helper = ["python3", str(copy / ".claude" / "scripts" / "state_helper.py")]
    subprocess.run([*helper, "acquire", "extract-object", "objects"], cwd=copy, check=True, capture_output=True)
    spec = json.loads((copy / ".onto" / "run" / "active.json").read_text(encoding="utf-8"))["commands"]["extract-object"]
    assert spec["node"] == "core.model" and "slice" not in spec and "protect" not in spec

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

    # 老蓝图里另一段的对象照常能改（一次做完整个项目）
    assert not denied(guard("ontology/object/客户.md")) and not denied(guard("ontology/object/订单.md"))
    subprocess.run([*helper, "release", "extract-object"], cwd=copy, check=True, capture_output=True)
