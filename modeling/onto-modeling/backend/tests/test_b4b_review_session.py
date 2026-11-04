"""B4b 独立审查修复（会话宿主的工作目录、命令的写入范围、下一步）：编号与审查清单一致，每条一个修复前会失败的用例。

复现思路取自审查的脚本（R 编号），改成断言修复后的行为。测试数据全部为合成内容；假引擎不起真实 CLI。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from app import wiring
from app.kernel.changesets import service, store, worktree
from app.kernel_guards import _guard_common as guards
from app.services.flow import model as flow_model
from app.services.flow import view
from app.services.session_host import exec_records, next_steps
from tests.test_changesets import agent_write, main_write, obj
from tests.test_flow_states import env  # noqa: F401
from tests.test_inbox import idle
from tests.test_pack_host_wiring import wait
from tests.test_session_host import FakeEngine, result


# ---- 7 引擎工作目录和 session.cwd 分家 ----


def test_07_engine_reconnects_when_the_working_directory_changes(env, monkeypatch):
    client, workspaces, manager, project_id, root = env
    monkeypatch.setattr(service, "_activity_provider", wiring._session_activity)
    real = worktree.ensure
    calls = {"n": 0}

    def flaky(root_):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("合成的瞬时 git 故障")
        return real(root_)

    monkeypatch.setattr(worktree, "ensure", flaky)
    seen = []

    def turn_one(ctx):
        seen.append(Path(ctx.cwd))

    def turn_two(ctx):
        seen.append(Path(ctx.cwd))
        agent_write(Path(ctx.cwd), "ontology/object/门店.md", obj("门店", "第二轮写的"))

    FakeEngine.scripts = [[turn_one, result()], [turn_two, result()]]
    base = f"/api/v1/projects/{project_id}/conversations/default/messages"
    client.post(base, json={"text": "第一轮"})
    wait(lambda: len(seen) >= 1)
    idle(manager, project_id)
    client.post(base, json={"text": "第二轮"})
    wait(lambda: len(seen) >= 2)
    wait(lambda: store.current(root) is not None)
    assert seen[0] == root and worktree.is_copy(seen[1])
    assert not (root / "ontology/object/门店.md").exists()


# ---- 9 .onto/run/active.json 永远不清 ----


def test_09_stale_active_scope_does_not_outlive_the_command(env):
    _client, workspaces, _manager, project_id, root = env
    main_write(root, "ontology/object/门店.md", obj("门店", "合成门店"))
    copy = service.copy_for_turn(workspaces, project_id, root)
    helper = ["python3", str(copy / ".claude" / "scripts" / "state_helper.py")]
    assert subprocess.run([*helper, "acquire", "mock", "generate"], cwd=copy, capture_output=True,
                          text=True).stdout.strip() == "ok"
    target = copy / "ontology/object/门店.md"

    def scope_reason():
        call = guards.Call(root=copy, cwd=copy, tool="Write", targets=[guards.Target(raw=str(target), path=target)])
        return guards.guard_scope(call)

    assert scope_reason()  # 命令在跑：收窄到它的范围
    # 引擎换了实例（回收、重启）：旧引擎的锁不算数，守卫不再按它收窄
    lock = json.loads((copy / ".ontology-state" / "lock-mock.json").read_text())
    lock.pop("owner_pid", None)
    lock["owner"] = "engine:old-instance"
    (copy / ".ontology-state" / "lock-mock.json").write_text(json.dumps(lock), encoding="utf-8")
    (copy / ".onto" / "run" / "engine.json").write_text(json.dumps({"engine_id": "new-instance"}), encoding="utf-8")
    assert scope_reason() is None
    # 撤回这一轮：锁与范围一起清掉，节点不再「进行中」
    service.open_changeset(workspaces, project_id)
    service.discard(workspaces, project_id, "cs-1", actor="employee_0001")
    assert not (copy / ".onto/run/active.json").exists() and not (copy / ".ontology-state/lock-mock.json").exists()
    assert next(item for item in view.build(root)["nodes"] if item["id"] == "trace.trace")["state"] != "running"


def test_09_abandon_and_startup_clear_the_commands_scope(env):
    client, workspaces, _manager, project_id, root = env
    copy = service.copy_for_turn(workspaces, project_id, root)
    helper = ["python3", str(copy / ".claude" / "scripts" / "state_helper.py")]
    subprocess.run([*helper, "acquire", "extract-object", "objects"], cwd=copy, capture_output=True)
    flow = flow_model.load_for(copy)
    record = exec_records.start(project_id, "default", root=copy, flow=flow, command="/ontology-extract-object",
                                command_name="extract-object")
    exec_records.interrupt(project_id, record["id"], "restart")
    response = client.post(f"/api/v1/projects/{project_id}/execs/{record['id']}/abandon", json={})
    assert response.status_code == 200, response.text
    assert not (copy / ".onto/run/active.json").exists() and not (copy / ".ontology-state/lock-extract-object.json").exists()
    # 启动恢复：执行记录改判为中断时，副本里留下的范围一起清掉
    subprocess.run([*helper, "acquire", "mock", "generate"], cwd=copy, capture_output=True)
    exec_records.start(project_id, "default", root=copy, flow=flow, command="/ontology-mock", command_name="mock")
    from app.services.session_host import recovery

    recovery.sweep_execs(workspaces)
    assert not (copy / ".onto/run/active.json").exists()


# ---- 19 state_helper suggest 误报「确认生效」 ----


def test_19_suggest_does_not_count_mainline_saves_as_copy_changes(env):
    client, workspaces, _manager, project_id, root = env
    main_write(root, "ontology/object/门店.md", obj("门店", "合成门店"))
    copy = service.copy_for_turn(workspaces, project_id, root)
    view.build(root)
    main_write(root, "ontology/object/仓库.md", obj("仓库", "用户在网页里加的"))
    view.build(root)
    from_ui = client.get(f"/api/v1/projects/{project_id}/flow/suggestions").json()
    from_helper = next_steps.suggest(copy)
    assert from_ui["mode"] == "normal" and from_helper["mode"] == "normal"
    assert from_helper == {key: value for key, value in from_ui.items() if key != "rev"}  # 版本号只有后端给
