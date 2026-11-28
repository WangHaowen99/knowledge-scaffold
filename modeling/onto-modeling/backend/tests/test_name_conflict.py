"""任务名称同名校验：新建任务、导入已有知识包、导入项目（迁移包）、重命名、克隆五个入口。

与知识包建模工具同一口径：去首尾空白、全角半角与大小写差异都算同名；查重范围是
当前用户在列表里看得到的任务（自己的；管理员是全部）；冲突返回 409 与至多 3 个候选名，
前端选名后重新提交。
"""

import io
import json
import threading
import time
import zipfile
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.api import deps
from app.routers import projects
from app.services import auth
from app.services.name_conflict import (
    MAX_DISPLAY_NAME_LENGTH,
    detect_name_conflict,
    normalized_display_name,
)
from app.services.workspace import (
    META_FILE,
    PURGE_MARKER,
    NameConflictError,
    WorkspaceError,
    WorkspaceManager,
)


@pytest.fixture()
def manager(tmp_path, monkeypatch) -> WorkspaceManager:
    workspaces = WorkspaceManager(tmp_path / "data", tmp_path / "assets")
    monkeypatch.setattr(deps, "workspaces", workspaces)
    return workspaces


@pytest.fixture()
def client(manager, monkeypatch) -> TestClient:
    monkeypatch.setattr(auth, "ENABLED", False)
    return TestClient(app)


def _seed(manager, project_id, name, owner="") -> None:
    """手写元数据代替建项目流程：查重只看名称与归属。"""
    root = manager.workspaces_dir / project_id
    root.mkdir()
    meta = {"id": project_id, "name": name, "owner": owner}
    (root / META_FILE).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def _request(user):
    return SimpleNamespace(state=SimpleNamespace(user=user), headers={})


def _json(response) -> dict:
    return json.loads(response.body)


def _create(name: str, user) -> dict:
    return _json(projects.create_project(_request(user), {"name": name, "goal": "合成目标"}))


GOAL = {"goal": "合成目标"}


def _taken(manager, name, viewer) -> bool:
    try:
        manager.check_name_available(name, viewer)
    except NameConflictError:
        return True
    return False


def _zip(entries: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, content in entries.items():
            archive.writestr(path, content)
    return buffer.getvalue()


def _upload(client, archive, filename="package.zip") -> str:
    files = {"file": (filename, archive, "application/zip")}
    response = client.post("/api/v1/uploads", data={"purpose": "project_import"}, files=files)
    assert response.status_code == 201
    return response.json()["upload_id"]


def _import(client, name, archive, *, dry_run=False):
    """导入向导：先暂存（U-01），再检查（P-10）或导入（P-11）。"""
    upload_id = _upload(client, archive)
    if dry_run:
        return client.post("/api/v1/projects/import/inspect", json={"upload_id": upload_id, "name": name})
    return client.post("/api/v1/projects/import", json={"upload_id": upload_id, "name": name})


def _transfer(client, archive, project_name=None):
    """迁移包：名称缺省取检查结果里的建议名（迁移包里记录的原名）。"""
    upload_id = _upload(client, archive, "transfer.zip")
    if project_name is None:
        inspected = client.post("/api/v1/projects/import/inspect", json={"upload_id": upload_id}).json()
        assert inspected["kind"] == "transfer"
        project_name = inspected["suggested_name"]
    return client.post("/api/v1/projects/import", json={"upload_id": upload_id, "name": project_name})


# ---- 判定口径与候选名 ----


def test_width_case_and_surrounding_spaces_count_as_same_name():
    assert normalized_display_name("  Ｏｒｄｅｒ建模 ") == "order建模"
    assert detect_name_conflict(["order建模"], "order建模2") is None

    conflict = detect_name_conflict(["order建模"], " ＯＲＤＥＲ建模 ")

    assert conflict == {
        "original_name": "ＯＲＤＥＲ建模",
        "attempted_name": "ＯＲＤＥＲ建模",
        "candidates": ["ＯＲＤＥＲ建模（2）", "ＯＲＤＥＲ建模（3）", "ＯＲＤＥＲ建模（4）"],
    }


def test_candidates_skip_taken_names_and_number_from_original_name():
    """改名重试仍冲突时，候选名以原名为基础编号，不叠成「名称（2）（2）」；
    半角括号的已占用序号同样跳过。"""
    taken = ["示例任务", "示例任务（2）", "示例任务(3)"]

    conflict = detect_name_conflict(taken, "示例任务（2）", "示例任务")

    assert conflict == {
        "original_name": "示例任务",
        "attempted_name": "示例任务（2）",
        "candidates": ["示例任务（4）", "示例任务（5）", "示例任务（6）"],
    }


def test_candidates_respect_name_length_limit():
    long_name = "长" * MAX_DISPLAY_NAME_LENGTH

    first = detect_name_conflict([long_name], long_name)["candidates"][0]

    assert len(first) == MAX_DISPLAY_NAME_LENGTH and first.endswith("（2）")


# ---- 查重范围 ----


@pytest.mark.parametrize(
    "viewer, name, taken",
    [
        (("alice", False), "订单建模", True),  # 自己的任务
        (("alice", False), "库存建模", False),  # 别人的任务看不到，不算重名
        (("alice", False), "历史任务", False),  # 无主任务普通用户看不到
        (("alice", True), "库存建模", True),  # 管理员在列表里看得到全部任务
        (("alice", True), "历史任务", True),
        (None, "库存建模", True),  # 鉴权关闭：全部任务都算
    ],
)
def test_scope_is_tasks_visible_to_current_user(manager, viewer, name, taken):
    _seed(manager, "p-alice", "订单建模", owner="alice")
    _seed(manager, "p-bob", "库存建模", owner="bob")
    _seed(manager, "p-legacy", "历史任务")

    assert _taken(manager, name, viewer) is taken


def test_deleted_and_unreadable_tasks_do_not_hold_names(manager):
    """已删除（移出或原地清除中）的任务名立即可用；元数据损坏的目录不拖垮查重。"""
    _seed(manager, "p-deleted", "已删除任务")
    manager.stage_delete("p-deleted")
    purging = manager.workspaces_dir / "p-purging"
    purging.mkdir()
    (purging / PURGE_MARKER).write_text(json.dumps({"name": "清除中任务"}), encoding="utf-8")
    broken = manager.workspaces_dir / "p-broken"
    broken.mkdir()
    (broken / META_FILE).write_text("{", encoding="utf-8")

    assert not _taken(manager, "已删除任务", None)
    assert not _taken(manager, "清除中任务", None)


def test_conflict_error_and_length_limit(manager):
    _seed(manager, "p1", "示例任务")

    with pytest.raises(NameConflictError) as created:
        manager.check_name_available("示例任务", None)
    with pytest.raises(NameConflictError) as imported:
        manager.check_name_available("示例任务", None, importing=True)
    with pytest.raises(WorkspaceError) as too_long:
        manager.check_name_available("长" * (MAX_DISPLAY_NAME_LENGTH + 1), None)

    assert (created.value.status, created.value.code) == (409, "project_name_conflict")
    assert "不可重复导入" in str(imported.value) and "不可重复导入" not in str(created.value)
    assert (too_long.value.status, too_long.value.code) == (400, "project_name_too_long")
    # 迁移包里沿用的原名不受长度上限约束（只校验用户输入的新名称）
    manager.check_name_available("长" * (MAX_DISPLAY_NAME_LENGTH + 1), None, check_length=False)


# ---- 新建任务 ----


def test_create_rejects_duplicate_name_with_candidates(client, manager):
    assert client.post("/api/v1/projects", json={"name": "电商订单建模", **GOAL}).status_code == 201

    response = client.post("/api/v1/projects", json={"name": " 电商订单建模 ", **GOAL})

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "project_name_conflict"
    assert body["details"]["conflict"] == {
        "original_name": "电商订单建模",
        "attempted_name": "电商订单建模",
        "candidates": ["电商订单建模（2）", "电商订单建模（3）", "电商订单建模（4）"],
    }
    assert len(manager.list_projects()) == 1  # 冲突时不落盘

    retried = client.post("/api/v1/projects", json={"name": "电商订单建模（2）", **GOAL})

    assert retried.status_code == 201 and retried.json()["name"] == "电商订单建模（2）"
    # 名称可用检查（P-3）与新建同一口径
    check = client.get("/api/v1/projects/name-check", params={"name": "电商订单建模"}).json()
    assert check["available"] is False and check["reason"] == "conflict"
    assert check["conflict"]["candidates"][0] == "电商订单建模（3）"


def test_create_checks_only_tasks_visible_to_user(manager):
    _seed(manager, "p-alice", "订单建模", owner="alice")

    with pytest.raises(NameConflictError):
        _create("订单建模", {"sub": "alice"})
    created = _create("订单建模", {"sub": "bob"})

    assert created["owner"] == "bob"


def test_concurrent_same_name_creates_only_one(manager, monkeypatch):
    """查重与落盘在同一把锁里串行：两个同名请求同时到达，只建成一个。"""
    create = manager.create_project

    def slow_create(*args, **kwargs):
        time.sleep(0.2)  # 拉长查重到落盘的窗口
        return create(*args, **kwargs)

    monkeypatch.setattr(manager, "create_project", slow_create)
    barrier = threading.Barrier(2)
    outcomes = []

    def submit():
        barrier.wait()
        try:
            _create("并发任务", None)
            outcomes.append("created")
        except NameConflictError:
            outcomes.append("conflict")

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["conflict", "created"]
    assert [project["name"] for project in manager.list_projects()] == ["并发任务"]


# ---- 导入已有知识包 ----


def test_import_checks_name_at_preview_and_again_on_confirm(client, manager):
    archive = _zip({"ontology/object/订单.md": "# 订单\n\n一笔订单\n"})
    _seed(manager, "p1", "订单知识包")

    preview = _import(client, "订单知识包", archive, dry_run=True)

    # 检查导入包（P-10）不报错，名称检查结果随摘要返回
    assert preview.status_code == 200
    assert preview.json()["name_check"]["reason"] == "conflict"
    assert _import(client, "订单知识包（2）", archive, dry_run=True).json()["name_check"]["available"] is True

    # 预览通过后、确认导入前被别人抢先用了这个名字：确认时再拦一次
    _seed(manager, "p2", "订单知识包（2）")
    confirm = _import(client, "订单知识包（2）", archive)

    assert confirm.status_code == 409
    assert "不可重复导入" in confirm.json()["message"]
    assert confirm.json()["details"]["conflict"]["candidates"][0] == "订单知识包（3）"
    assert len(manager.list_projects()) == 2  # 冲突时不建任务

    imported = _import(client, "订单知识包（3）", archive)

    assert imported.status_code == 201 and imported.json()["name"] == "订单知识包（3）"


def test_import_preview_shows_blockers_before_name_conflict(client, manager):
    """压缩包本身不能导入时，先让用户看到阻断原因，不提示改名。"""
    _seed(manager, "p1", "订单知识包")

    preview = _import(client, "订单知识包", _zip({"misc/readme.txt": "说明"}), dry_run=True)

    assert preview.status_code == 200 and preview.json()["blockers"]
    assert preview.json()["kind"] == "unknown"


# ---- 导入项目（迁移包） ----


def test_transfer_import_conflict_then_rename(client, manager, tmp_path):
    source = manager.create_project("迁移源")
    archive_path = tmp_path / "transfer.zip"
    manager.export_zip(source["id"], "workspace", archive_path)
    archive = archive_path.read_bytes()

    conflicted = _transfer(client, archive)

    assert conflicted.status_code == 409
    assert conflicted.json()["details"]["conflict"] == {
        "original_name": "迁移源",
        "attempted_name": "迁移源",
        "candidates": ["迁移源（2）", "迁移源（3）", "迁移源（4）"],
    }
    assert len(manager.list_projects()) == 1

    renamed = _transfer(client, archive, "迁移源（2）")

    assert renamed.status_code == 201 and renamed.json()["name"] == "迁移源（2）"

    # 改名后仍重名：候选名仍以迁移包里的原名为基础编号
    again = _transfer(client, archive, "迁移源（2）")

    assert again.status_code == 409
    assert again.json()["details"]["conflict"] == {
        "original_name": "迁移源",
        "attempted_name": "迁移源（2）",
        "candidates": ["迁移源（3）", "迁移源（4）", "迁移源（5）"],
    }


# ---- 重命名任务 ----


def test_rename_conflict_then_rename_with_candidate(client, manager):
    _seed(manager, "p1", "订单建模")
    _seed(manager, "p2", "库存建模")

    conflicted = client.patch("/api/v1/projects/p1", json={"name": " 库存建模 "})

    assert conflicted.status_code == 409
    assert conflicted.json()["code"] == "project_name_conflict"
    assert conflicted.json()["details"]["conflict"] == {
        "original_name": "库存建模",
        "attempted_name": "库存建模",
        "candidates": ["库存建模（2）", "库存建模（3）", "库存建模（4）"],
    }
    assert manager.get_project("p1")["name"] == "订单建模"  # 冲突时不改名

    renamed = client.patch("/api/v1/projects/p1", json={"name": "库存建模（2）"})

    assert renamed.status_code == 200 and renamed.json()["name"] == "库存建模（2）"
    assert manager.get_project("p1")["name"] == "库存建模（2）"


def test_rename_does_not_conflict_with_the_task_itself(client, manager):
    """只改自己名称的大小写或全半角不算重名；名称没变时，与历史同名任务并存也不拦。"""
    _seed(manager, "p1", "order建模")
    _seed(manager, "p2", "历史重名")
    _seed(manager, "p3", "历史重名")

    variant = client.patch("/api/v1/projects/p1", json={"name": "ＯＲＤＥＲ建模"})
    unchanged = client.patch("/api/v1/projects/p2", json={"name": " 历史重名 "})

    assert variant.status_code == 200 and variant.json()["name"] == "ＯＲＤＥＲ建模"
    assert unchanged.status_code == 200 and unchanged.json()["name"] == "历史重名"


def test_rename_checks_only_tasks_visible_to_user(manager):
    _seed(manager, "p-alice", "订单建模", owner="alice")
    _seed(manager, "p-alice-2", "库存草稿", owner="alice")
    _seed(manager, "p-bob", "库存建模", owner="bob")
    _seed(manager, "p-root", "运维任务", owner="root")

    def rename(project_id, name, user):
        return projects.update_project(project_id, _request(user), {"name": name})

    with pytest.raises(NameConflictError):
        rename("p-alice-2", "订单建模", {"sub": "alice"})  # 与自己的项目重名
    # 名称只在同一用户下唯一（契约 P-02）：管理员也只和自己的项目比
    assert rename("p-root", "库存建模", {"sub": "root", "role": "admin"})["name"] == "库存建模"
    renamed = rename("p-alice", "库存建模", {"sub": "alice"})  # 别人的任务看不到，不算重名

    assert renamed["name"] == "库存建模"


def test_rename_rejects_too_long_name(client, manager):
    _seed(manager, "p1", "订单建模")

    response = client.patch(
        "/api/v1/projects/p1", json={"name": "长" * (MAX_DISPLAY_NAME_LENGTH + 1)}
    )

    assert response.status_code == 400 and response.json()["code"] == "project_name_too_long"


# ---- 克隆任务 ----


def test_clone_conflict_then_clone_with_new_name(client, manager):
    _seed(manager, "p1", "订单建模")

    first = client.post("/api/v1/projects/p1/clone")

    assert first.status_code == 201 and first.json()["name"] == "订单建模（副本）"

    # 再克隆一次：默认名称已被上一个副本占用
    conflicted = client.post("/api/v1/projects/p1/clone", json={"name": ""})

    assert conflicted.status_code == 409
    assert conflicted.json()["code"] == "project_name_conflict"
    assert conflicted.json()["details"]["conflict"] == {
        "original_name": "订单建模（副本）",
        "attempted_name": "订单建模（副本）",
        "candidates": ["订单建模（副本）（2）", "订单建模（副本）（3）", "订单建模（副本）（4）"],
    }
    assert len(manager.list_projects()) == 2  # 冲突时不建任务

    renamed = client.post("/api/v1/projects/p1/clone", json={"name": "订单建模（副本）（2）"})

    assert renamed.status_code == 201 and renamed.json()["name"] == "订单建模（副本）（2）"

    # 改名后仍重名：候选名仍以默认的副本名为基础编号
    again = client.post("/api/v1/projects/p1/clone", json={"name": "订单建模"})

    assert again.status_code == 409
    assert again.json()["details"]["conflict"] == {
        "original_name": "订单建模（副本）",
        "attempted_name": "订单建模",
        "candidates": ["订单建模（副本）（3）", "订单建模（副本）（4）", "订单建模（副本）（5）"],
    }


def _clone(project_id: str, user) -> dict:
    import asyncio

    return _json(asyncio.run(projects.clone_project(project_id, _request(user), {})))


def test_clone_checks_only_tasks_visible_to_user(manager):
    _seed(manager, "p-alice", "订单建模", owner="alice")
    _seed(manager, "p-bob", "订单建模（副本）", owner="bob")

    cloned = _clone("p-alice", {"sub": "alice"})  # 看不到 bob 的同名任务
    assert (cloned["name"], cloned["owner"]) == ("订单建模（副本）", "alice")
    with pytest.raises(NameConflictError):
        _clone("p-alice", {"sub": "alice"})  # 与自己的副本重名


def test_clone_default_name_respects_length_limit(manager):
    """原名已到长度上限时截断原名再加后缀，一键克隆不会因超长被拒。"""
    _seed(manager, "p1", "长" * MAX_DISPLAY_NAME_LENGTH)

    cloned = _clone("p1", None)

    assert len(cloned["name"]) == MAX_DISPLAY_NAME_LENGTH and cloned["name"].endswith("（副本）")
