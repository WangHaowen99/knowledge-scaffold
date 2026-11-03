"""变更集（详设 8.2、4.3；方案 C7～C9、C12、C14、C15；契约 3.10、EP-03、EP-04、4.7 round_changes）。

- 助手副本：固定路径、`resource/` 稀疏排除后挂链接、`.claude` 链到主线、续用与跟上主线、崩溃后修复；
- 确认生效：一个 squash 提交、trailer 写在合入提交上、主线版本只 +1（`--first-parent`）；
  实体冲突进 conflict、选好一边再合入；门禁只看新增错误；
- 撤回这一轮、撤回已合入的变更集（P3 按段撤回用）、助手在跑时不能确认；
- 接口形状（CS-01～CS-06）、对话里的「这一轮改了什么」卡与 S-05 的 open_changeset、Studio 复核草稿进副本。

测试数据全部为合成内容；假引擎不起真实 CLI。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.kernel import metamodel
from app.kernel.changesets import entity_diff, service, store, worktree
from app.main import app
from app.routers import sessions as session_routes
from app.services import agent_sessions, auth, mainline, streams
from app.services.agent_sessions import SessionManager
from app.services.workspace import WorkspaceManager, WorkspaceError
from tests.test_event_streams import buffered
from tests.test_session_host import FakeEngine, result, settle


def obj(name: str, desc: str, rows=(("编号", "code", "主键编号", "是", "字符串"),), relations: str = "无") -> str:
    table = "\n".join(f"| {a} | {b} | {c} | {d} | {e} | 无 | 无 |" for a, b, c, d, e in rows)
    return (f"# {name}\n\n## 对象描述\n\n{desc}\n\n## 对象别名\n\n无\n\n## 英文名称\n\nSynthetic\n\n"
            "## 对象属性清单\n\n| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |\n"
            f"| --- | --- | --- | --- | --- | --- | --- |\n{table}\n\n## 关联对象\n\n{relations}\n")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


def agent_write(copy: Path, rel: str, text: str, *, commit: str | None = None) -> None:
    path = copy / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if commit:
        git(copy, "add", "-A")
        git(copy, "commit", "-qm", commit)


def main_write(root: Path, rel: str, text: str, message: str = "web: 合成保存") -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    git(root, "add", "--", rel)
    git(root, "commit", "-qm", message)
    return git(root, "rev-parse", "HEAD").strip()


def trailers(root: Path, rev: str = "HEAD") -> dict:
    body = git(root, "log", "-1", "--format=%B", rev)
    return dict(line.split(": ", 1) for line in body.splitlines() if line.startswith("Onto-"))


@pytest.fixture()
def project(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    workspaces = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", workspaces)
    monkeypatch.setattr(service, "_activity_provider", None)
    project_id = workspaces.create_project("变更集", owner="employee_0001")["id"]
    root = workspaces.project_root(project_id)
    (root / "resource" / "others").mkdir(parents=True, exist_ok=True)
    main_write(root, "resource/others/资料.md", "合成资料\n", "upload: 合成资料")
    main_write(root, "ontology/object/订单.md", obj("订单", "合成订单"), "web: 订单")
    return workspaces, project_id, root


# ---- 副本：创建、续用、跟上主线、修复 ----


def test_copy_is_created_at_a_fixed_path_with_shared_resource_and_assembly(project):
    workspaces, project_id, root = project
    copy = service.copy_for_turn(workspaces, project_id, root)
    assert copy == workspaces.workspaces_dir.parent / "changesets" / project_id / "assistant"
    assert worktree.healthy(root) and worktree.is_copy(copy) and worktree.main_root(copy) == root
    # resource/ 稀疏排除，挂的是主线工作区那份；.claude 链到主线的 .claude（装配切换一次两边生效）
    assert (copy / "resource").is_symlink() and (copy / "resource" / "others" / "资料.md").read_text() == "合成资料\n"
    assert os.path.realpath(copy / ".claude") == os.path.realpath(root / ".claude")
    assert (copy / "CLAUDE.md").is_symlink()
    # 链接与稀疏外的资料都不算改动：git add -A 带不进去
    assert git(copy, "status", "--porcelain") == ""
    git(copy, "add", "-A")
    assert git(copy, "diff", "--cached", "--name-only") == ""
    # 同一个项目再来一轮还是这个路径（Claude 的续接记录按工作目录存）
    assert service.copy_for_turn(workspaces, project_id, root) == copy


def test_turn_start_follows_mainline(project):
    workspaces, project_id, root = project
    copy = service.copy_for_turn(workspaces, project_id, root)
    # 副本没有改动：直接跟到主线最新
    head = main_write(root, "ontology/object/客户.md", obj("客户", "合成客户"))
    service.copy_for_turn(workspaces, project_id, root)
    assert worktree.rev(copy) == head
    # 副本有提交、主线又前进了：主线合进副本，助手看得到你在范围外的保存
    agent_write(copy, "ontology/object/门店.md", obj("门店", "合成门店"), commit="agent: 门店")
    main_write(root, "ontology/object/仓库.md", obj("仓库", "合成仓库"))
    service.copy_for_turn(workspaces, project_id, root)
    assert (copy / "ontology/object/仓库.md").is_file() and (copy / "ontology/object/门店.md").is_file()
    assert not (root / "ontology/object/门店.md").exists()


def test_repair_recreates_a_lost_copy_on_its_branch_and_clears_stale_locks(project):
    workspaces, project_id, root = project
    copy = service.copy_for_turn(workspaces, project_id, root)
    agent_write(copy, "ontology/object/门店.md", obj("门店", "合成门店"), commit="agent: 门店")
    # 崩溃：副本目录没了（没确认的提交还在分支上），worktree 的索引锁残留
    import shutil

    shutil.rmtree(copy)
    lock = root / ".git" / "worktrees" / "assistant" / "index.lock"
    lock.write_text("")
    old = time.time() - 600
    os.utime(lock, (old, old))
    item = worktree.repair_all(workspaces)
    assert item["id"] == "changeset_copies" and item["result"] == "cleared"
    assert worktree.healthy(root) and (copy / "ontology/object/门店.md").is_file()
    assert not lock.exists()
    # 建到一半的副本（没有 .git）：重建
    shutil.rmtree(copy)
    copy.mkdir(parents=True)
    (copy / "half.txt").write_text("x")
    assert worktree.repair(root) == "重建副本"
    assert worktree.healthy(root) and not (copy / "half.txt").exists()
    assert worktree.repair_all(workspaces) is None  # 都完好时不进启动自检报告


def test_startup_registers_the_copy_repair_step():
    from app.governance import startup_recovery

    assert "changeset_copies" in [step.id for step in startup_recovery.steps()]


# ---- 确认生效 ----


def _pending(workspaces, project_id, root, rel="ontology/object/门店.md", text=None, commit=True):
    copy = service.copy_for_turn(workspaces, project_id, root)
    agent_write(copy, rel, text or obj("门店", "合成门店"), commit="agent: 门店" if commit else None)
    record = service.observe(workspaces, project_id, running=False, conversation_id="default",
                             exec_record={"id": "ex-1", "command_label": "建模",
                                          "node": {"id": "core.model", "version": "core@0.3.0"}})
    assert record is not None and record["state"] == "pending_confirm"
    return copy, record


def test_merge_writes_one_squash_commit_with_trailers(project):
    workspaces, project_id, root = project
    copy, record = _pending(workspaces, project_id, root)
    # 助手又改了一处没提交：确认生效时一并收进来
    agent_write(copy, "ontology/object/订单.md", obj("订单", "合成订单（补充说明）"))
    before = mainline.count(root)
    out = service.merge(workspaces, project_id, record["id"], actor="employee_0001")
    assert out["state"] == "merged" and out["version_label"] == mainline.label(before + 1)
    assert mainline.count(root) == before + 1
    assert (root / "ontology/object/门店.md").is_file()
    assert "补充说明" in (root / "ontology/object/订单.md").read_text(encoding="utf-8")
    values = trailers(root)
    assert values["Onto-Source"] == "agent" and values["Onto-Changeset"] == record["id"]
    assert values["Onto-Author"] == "employee_0001" and values["Onto-Node"] == "core.model@0.3.0"
    assert "core@" in values.get("Onto-Packs", "")
    # 只有一个提交进主线（squash），副本重置到合入后的主线
    assert len(git(root, "rev-list", "--first-parent", f"{out['commit']}^..{out['commit']}").split()) == 1
    assert worktree.rev(copy) == out["commit"] and git(copy, "status", "--porcelain") == ""
    saved = store.get(root, record["id"])
    assert saved["state"] == "merged" and saved["merged"]["commit"] == out["commit"]
    assert service.current(workspaces, project_id) is None
    with pytest.raises(WorkspaceError) as closed:
        service.merge(workspaces, project_id, record["id"], actor="employee_0001")
    assert closed.value.code == "changeset_closed" and closed.value.details == {"state": "merged"}


def test_merge_keeps_user_saves_outside_the_changeset(project):
    workspaces, project_id, root = project
    _copy, record = _pending(workspaces, project_id, root)
    main_write(root, "ontology/object/客户.md", obj("客户", "你在 Studio 里新建"))
    service.merge(workspaces, project_id, record["id"], actor="employee_0001")
    assert (root / "ontology/object/客户.md").is_file() and (root / "ontology/object/门店.md").is_file()


def test_same_entity_changed_on_both_sides_is_a_conflict_until_resolved(project):
    workspaces, project_id, root = project
    _copy, record = _pending(workspaces, project_id, root, rel="ontology/object/订单.md",
                             text=obj("订单", "助手改的说明"))
    main_write(root, "ontology/object/订单.md", obj("订单", "你在主线上改的说明"))
    with pytest.raises(WorkspaceError) as exc:
        service.merge(workspaces, project_id, record["id"], actor="employee_0001")
    assert exc.value.code == "merge_conflict" and exc.value.status == 409
    assert exc.value.details["entities"] == [{"kind": "object", "id": "object:订单", "key": "订单", "name": "订单",
                                             "label": "对象「订单」"}]
    assert exc.value.details["inbox_item_id"] == f"changeset_conflict:{record['id']}"
    assert store.get(root, record["id"])["state"] == "conflict"
    out = service.resolve(workspaces, project_id, record["id"], [{"target": "object:订单", "keep": "changeset"}],
                          actor="employee_0001")
    assert out["state"] == "merged"
    assert "助手改的说明" in (root / "ontology/object/订单.md").read_text(encoding="utf-8")


def test_same_change_on_both_sides_is_not_a_conflict(project):
    workspaces, project_id, root = project
    text = obj("订单", "两边改成一样")
    _copy, record = _pending(workspaces, project_id, root, rel="ontology/object/订单.md", text=text)
    main_write(root, "ontology/object/订单.md", text)
    assert service.merge(workspaces, project_id, record["id"], actor="employee_0001")["state"] == "merged"


def test_checks_record_only_new_errors_and_do_not_block(project):
    """F3：合入时的检查只记新增问题、不挡合入（发布时才拦）。"""
    workspaces, project_id, root = project
    # 主线上已有的错误（缺章节的对象）不算这一轮的
    main_write(root, "ontology/object/旧对象.md", "# 旧对象\n\n## 对象描述\n\n存量\n")
    copy, record = _pending(workspaces, project_id, root)
    assert service.merge(workspaces, project_id, record["id"], actor="employee_0001")["state"] == "merged"
    assert store.get(root, record["id"])["checks"]["errors"] == 0
    # 新增的错误记下来，照样合入
    _copy, bad = _pending(workspaces, project_id, root, rel="ontology/object/坏对象.md",
                          text="# 坏对象\n\n## 对象描述\n\n缺了必需章节\n")
    assert service.merge(workspaces, project_id, bad["id"], actor="employee_0001")["state"] == "merged"
    checks = store.get(root, bad["id"])["checks"]
    assert checks["errors"] and all(item["path"] == "ontology/object/坏对象.md" for item in checks["findings"]
                                    if item["level"] == "error")
    assert (root / "ontology/object/坏对象.md").is_file()


def test_merge_refused_while_the_assistant_is_running(project, monkeypatch):
    workspaces, project_id, root = project
    _copy, record = _pending(workspaces, project_id, root)
    monkeypatch.setattr(service, "_activity_provider", lambda pid: {"running": True, "conversation_id": "default"})
    for action in (service.merge, service.discard):
        with pytest.raises(WorkspaceError) as exc:
            action(workspaces, project_id, record["id"], actor="employee_0001")
        assert exc.value.code == "changeset_in_progress" and exc.value.details == {"conversation_id": "default"}


def test_discard_resets_the_copy_and_leaves_mainline_alone(project):
    workspaces, project_id, root = project
    copy, record = _pending(workspaces, project_id, root)
    agent_write(copy, "ontology/object/草稿.md", "没提交的草稿\n")
    head = git(root, "rev-parse", "HEAD").strip()
    assert service.discard(workspaces, project_id, record["id"], actor="employee_0001")["state"] == "discarded"
    assert git(root, "rev-parse", "HEAD").strip() == head
    assert worktree.rev(copy) == head and not (copy / "ontology/object/门店.md").exists()
    assert not (copy / "ontology/object/草稿.md").exists() and (copy / "resource").is_symlink()
    assert store.get(root, record["id"])["state"] == "discarded"


def test_revert_merged_writes_a_reverse_commit(project):
    workspaces, project_id, root = project
    _copy, record = _pending(workspaces, project_id, root)
    service.merge(workspaces, project_id, record["id"], actor="employee_0001")
    out = service.revert_merged(workspaces, project_id, record["id"], actor="employee_0001")
    assert not (root / "ontology/object/门店.md").exists()
    assert trailers(root)["Onto-Source"] == "revert" and out["commit"] == git(root, "rev-parse", "HEAD").strip()
    assert store.get(root, record["id"])["reverted"]["commit"] == out["commit"]
    with pytest.raises(WorkspaceError):
        service.revert_merged(workspaces, project_id, record["id"], actor="employee_0001")


def test_revert_merged_refuses_when_later_saves_touched_the_same_entity(project):
    workspaces, project_id, root = project
    _copy, record = _pending(workspaces, project_id, root)
    service.merge(workspaces, project_id, record["id"], actor="employee_0001")
    main_write(root, "ontology/object/门店.md", obj("门店", "合入之后你又改了"))
    with pytest.raises(WorkspaceError) as exc:
        service.revert_merged(workspaces, project_id, record["id"], actor="employee_0001")
    assert exc.value.code == "merge_conflict" and exc.value.details["entities"][0]["id"] == "object:门店"


def test_versions_count_only_first_parent_commits(project):
    """C14：主线版本只计合入主线的提交；侧枝上的提交（如副本里的多次提交）不计。"""
    workspaces, project_id, root = project
    before = mainline.count(root)
    git(root, "checkout", "-qb", "side")
    for index in range(3):
        main_write(root, f"notes/side-{index}.md", f"{index}\n", f"side {index}")
    git(root, "checkout", "-q", "-")
    git(root, "merge", "-q", "--no-ff", "-m", "合入侧枝", "side")
    assert mainline.count(root) == before + 1
    versions = workspaces.versions(project_id)
    assert versions[0]["n"] == before + 1 and len(versions) == before + 1


def test_observe_closes_an_emptied_changeset(project):
    workspaces, project_id, root = project
    copy, record = _pending(workspaces, project_id, root, commit=False)
    (copy / "ontology/object/门店.md").unlink()
    assert service.observe(workspaces, project_id, running=False) is None
    assert store.get(root, record["id"])["state"] == "discarded"


# ---- 实体级差异 ----


def test_entity_diff_reports_sections_fields_and_relations():
    model = metamodel.mm
    old = obj("订单", "合成订单", relations="- [[订单]] → 属于 → [[客户]]")
    new = obj("订单", "合成订单（改）", rows=(("编号", "code", "主键编号", "是", "字符串"),
                                          ("金额", "amount", "金额", "否", "数字")),
              relations="- [[订单]] → 属于 → [[门店]]")
    info = entity_diff.changes(model, "object", old, new)
    assert info["change"] == "mod"
    assert [item["title"] for item in info["sections"]] == ["对象描述", "对象属性清单", "关联对象"]
    assert info["fields"]["added"] == ["金额"] and info["relations"] == {"added": ["门店"], "removed": ["客户"]}
    lines = info["sections"][1]["lines"]
    assert lines[0] == {"kind": "header", "text": "对象属性清单"} and any(line["kind"] == "add" for line in lines)
    assert "对象描述" in info["summary"] and "新增 1 行" in info["summary"]
    # 只动了空白行不算改到实体
    assert not entity_diff.entity_touched(model, "object", old, old.replace("\n\n## 对象别名", "\n\n\n## 对象别名"))
    assert entity_diff.changes(model, "object", None, new)["change"] == "add"


def test_entity_conflicts_need_both_sides_to_touch_the_entity():
    model = metamodel.mm
    base = obj("订单", "基准").encode()
    mine = obj("订单", "主线改").encode()
    theirs = obj("订单", "变更集改").encode()
    assert entity_diff.conflicts(model, {"ontology/object/订单.md": ("object", base, mine, theirs)}) == [
        "ontology/object/订单.md"]
    assert entity_diff.conflicts(model, {"ontology/object/订单.md": ("object", base, base, theirs)}) == []
    assert entity_diff.conflicts(model, {"ontology/object/订单.md": ("object", base, theirs, theirs)}) == []


# ---- 接口、对话卡片、Studio 复核草稿 ----


@pytest.fixture()
def api(project, monkeypatch):
    workspaces, project_id, root = project
    monkeypatch.setattr(auth, "ENABLED", False)
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    streams.reset_all()
    with TestClient(app) as client:
        monkeypatch.setattr(service, "_activity_provider", None)
        yield client, workspaces, project_id, root
    streams.reset_all()


def test_changeset_endpoints(api):
    client, workspaces, project_id, root = api
    _copy, record = _pending(workspaces, project_id, root)
    base = f"/api/v1/projects/{project_id}"
    listed = client.get(f"{base}/changesets", params={"state": "pending_confirm"}).json()
    assert [item["id"] for item in listed["items"]] == [record["id"]] and listed["next_cursor"] is None
    assert set(listed["items"][0]) == {"id", "source", "build_mode", "purpose", "state", "base", "conversation_id",
                                       "run_id", "slice_id", "created_at", "updated_at", "merged", "entity_count"}
    assert client.get(f"{base}/changesets", params={"state": "bogus"}).status_code == 400
    detail = client.get(f"{base}/changesets/{record['id']}").json()
    assert detail["groups"][0]["kind"] == "object" and detail["groups"][0]["items"][0]["change"] == "add"
    assert detail["groups"][0]["items"][0]["checks"] == "passed" and detail["next_version_label"]
    assert set(detail["checks"]) == {"errors", "warnings", "findings"}
    diff = client.get(f"{base}/changesets/{record['id']}/diff", params={"target": "object:门店"}).json()
    assert diff["items"][0]["target"]["id"] == "object:门店" and diff["items"][0]["sections"]
    assert client.get(f"{base}/changesets/cs-999").json()["code"] == "changeset_not_found"
    scope = client.get(f"{base}/scope").json()
    assert scope["state"] == "pending_confirm" and scope["changeset_id"] == record["id"]
    assert client.get(f"{base}/scope", params={"target": "object:门店"}).json()["state"] == "pending_confirm"
    # 建模节点声明的范围（全量模式下是整类对象、操作、规则）也一直锁着，直到确认或撤回；范围外的照常
    assert client.get(f"{base}/scope", params={"target": "object:客户"}).json()["state"] == "pending_confirm"
    assert client.get(f"{base}/scope", params={"target": "operation:下单"}).json()["state"] == "pending_confirm"
    assert client.get(f"{base}/scope", params={"target": "script:合成脚本"}).json()["state"] == "none"
    merged = client.post(f"{base}/changesets/{record['id']}/merge", json={"expected_head": "0" * 40})
    assert merged.status_code == 409 and merged.json()["code"] == "head_moved"
    merged = client.post(f"{base}/changesets/{record['id']}/merge", json={})
    assert merged.status_code == 200 and set(merged.json()) == {"state", "commit", "version_label", "merged_at"}
    assert client.get(f"{base}/scope").json()["state"] == "none"
    events = [event["data"] for event in buffered(project_id, "changeset.updated")]
    assert events[-1] == {"changeset_id": record["id"], "state": "merged", "source": "assistant",
                          "conversation_id": "default", "version_label": merged.json()["version_label"]}
    assert buffered(project_id, "scope.updated")[-1]["data"]["state"] == "none"
    assert buffered(project_id, "workspace.changed")[-1]["data"]["changeset_id"] == record["id"]


def test_round_card_open_changeset_and_merge_notice(api, monkeypatch):
    client, workspaces, project_id, root = api
    manager = SessionManager()
    monkeypatch.setattr(manager, "_engine_class_for", lambda pid: FakeEngine)
    monkeypatch.setattr(agent_sessions, "manager", manager)
    monkeypatch.setattr(session_routes, "manager", manager)
    from app import wiring

    monkeypatch.setattr(service, "_activity_provider", wiring._session_activity)

    def write(ctx):
        assert worktree.is_copy(Path(ctx.cwd))
        agent_write(Path(ctx.cwd), "ontology/object/门店.md", obj("门店", "合成门店"), commit="agent: 门店")

    FakeEngine.scripts, FakeEngine.sent = [[write, result()]], []

    async def run_turn():
        await manager.send(project_id, root, "补一个门店对象", username="employee_0001")
        await settle(manager, project_id)

    asyncio.run(run_turn())
    history = manager.get_history(project_id, "default")
    cards = [event["payload"] for event in history if event.get("type") == "card"
             and event["payload"].get("card_type") == "round_changes"]
    assert len(cards) == 1
    card = cards[0]
    # F3：这一轮结束就合入成一个版本，卡片只说改了什么
    assert card["state"] == "merged" and card["changeset_id"] == "cs-1" and card["merged_version_label"]
    assert card["groups"][0]["items"][0]["target"]["id"] == "object:门店"
    # 只读说明不再下发（UI 文案口径 ⑤，前端按写入范围算）
    assert card["checks"] == {"errors": 0, "warnings": 0, "pending": 0} and card["hints"] == []
    assert "lock_note" not in card
    assert (root / "ontology/object/门店.md").is_file()
    state = client.get(f"/api/v1/projects/{project_id}/conversations/default").json()
    assert state["open_changeset"] is None
    # 撤回这一轮：回到上一个版本，卡片原地更新，对话里一条提示行
    reverted = client.post(f"/api/v1/projects/{project_id}/changesets/cs-1/discard", json={})
    assert reverted.status_code == 200, reverted.text
    assert not (root / "ontology/object/门店.md").exists()
    history = manager.get_history(project_id, "default")
    updated = [event["payload"] for event in history if event.get("type") == "card_updated"]
    assert updated and updated[-1]["state"] == "reverted" and updated[-1]["status"]["label"] == "已撤回"
    notices = [event["payload"] for event in history if event.get("type") == "notice"]
    assert notices[-1]["kind"] == "discarded" and notices[-1]["changeset_id"] == "cs-1"
    assert notices[-1]["version_label"] == reverted.json()["version_label"]
    bars = [event["payload"] for event in history if event.get("type") == "suggestions"]
    assert bars and bars[-1]["mode"] != "pending_confirm"
    assert (bars[-1].get("primary") or {}).get("action", {}).get("type") != "merge_changeset"
    asyncio.run(manager.shutdown())


def test_studio_review_draft_is_staged_in_the_copy(api, monkeypatch):
    client, workspaces, project_id, root = api
    from app.routers import studio as studio_routes
    from app.services import studio as studio_service

    sent = []

    class Recorder:
        async def send(self, pid, cwd, text, **kwargs):
            sent.append(text)
            return type("S", (), {"conversation_id": "default"})()

    monkeypatch.setattr(agent_sessions, "manager", Recorder())
    rel = "ontology/object/订单.md"
    studio_service.save_draft(project_id, rel, obj("订单", "模板外章节") + "\n## 自创章节\n\n新内容\n", "", "employee_0001")
    response = client.post(f"/api/v1/projects/{project_id}/studio/entries/object:订单/submit", json={})
    assert response.status_code == 200 and response.json()["result"] == "needs_review", response.text
    copy = worktree.copy_path(root)
    staged = copy / studio_service.REVIEW_DIR / "订单.md"
    assert staged.is_file() and "自创章节" in staged.read_text(encoding="utf-8")
    assert not (root / studio_service.REVIEW_DIR).exists()
    assert studio_service.REVIEW_DIR in sent[0]
    del studio_routes


def test_autobuild_slice_changeset_merges_with_its_own_source(project):
    """给 P3 的接口：每段先占一个变更集，观察后自动合入，trailer 记 autobuild，标「需复核」。"""
    workspaces, project_id, root = project
    copy = service.copy_for_turn(workspaces, project_id, root)
    record = service.open_changeset(workspaces, project_id, source="autobuild", build_mode="L4", run_id="run-1",
                                    slice_id="s1", purpose="自主构建 · 门店一段")
    assert service.open_changeset(workspaces, project_id, source="autobuild")["id"] == record["id"]
    agent_write(copy, "ontology/object/门店.md", obj("门店", "合成门店"), commit="agent: 门店")
    observed = service.observe(workspaces, project_id, running=False)
    assert observed["id"] == record["id"] and observed["source"] == "autobuild" and observed["run_id"] == "run-1"
    out = service.merge(workspaces, project_id, record["id"], actor="employee_0001", needs_review=True)
    assert trailers(root)["Onto-Source"] == "autobuild" and trailers(root)["Onto-Changeset"] == record["id"]
    assert out["record"]["needs_review"] is True and out["record"]["build_mode"] == "L4"


def test_clone_with_history_leaves_the_assistant_copy_behind(project):
    """克隆带历史时，源项目的副本登记与副本分支（没确认的改动）不随过去，新项目的副本从自己的主线建。"""
    workspaces, project_id, root = project
    _pending(workspaces, project_id, root)
    clone = workspaces.clone_project(project_id, owner="employee_0001", include_history=True)
    clone_root = workspaces.project_root(clone["id"])
    assert not (clone_root / ".git" / "worktrees").exists()
    assert subprocess.run(["git", "rev-parse", "--verify", "-q", "refs/heads/onto/assistant"], cwd=clone_root,
                          capture_output=True).returncode != 0
    copy = service.copy_for_turn(workspaces, clone["id"], clone_root)
    assert worktree.healthy(clone_root) and not (copy / "ontology/object/门店.md").exists()
    # 源项目的副本不受影响
    assert worktree.healthy(root) and (worktree.copy_path(root) / "ontology/object/门店.md").is_file()
