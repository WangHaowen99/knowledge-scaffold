"""统一变更事务 ChangeTx（详设 4.3、方案 C1、C2、C6）：四种入口、来源 trailer、只提交本次路径、崩溃恢复。

项目、对象与用户名全部合成。
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from app import config
from app.kernel import writepath
from app.kernel.writepath import tx as writepath_tx
from app.services.onto_mcp import workspace as mcp
from app.services.workspace import WorkspaceError, WorkspaceManager, _commit_paths, _git

OBJECT = (
    "# 对象描述\n\n合成对象\n\n# 对象属性清单\n\n"
    "| 中文名称 | 英文名称 | 类型 | 描述 | 主键 | 属性别名 | 属性规则 |\n"
    "| --- | --- | --- | --- | --- | --- | --- |\n"
    "| 编号 | item_id | string | 合成编号 | 是 | 无 | 无 |\n\n# 关联对象\n\n无\n"
)


@pytest.fixture()
def manager(tmp_path: Path, monkeypatch) -> WorkspaceManager:
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    assets = tmp_path / "assets"
    (assets / "workspace-toolkit" / "templates").mkdir(parents=True)
    (assets / "workspace-rules.md").write_text("# synthetic rules", encoding="utf-8")
    return WorkspaceManager(data, assets)


@pytest.fixture()
def project(manager):
    meta = manager.create_project("writepath_demo", owner="operator_a")
    root = manager.project_root(meta["id"])
    (root / "ontology" / "object" / "item_a.md").write_text(OBJECT, encoding="utf-8")
    manager._refresh_indexes(root)  # 基线里的 index.md 与条目一致
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "test: synthetic fixture")
    return meta["id"], root


@pytest.fixture()
def events():
    seen: list = []
    unsubscribe = writepath.subscribe(seen.append)
    yield seen
    unsubscribe()


def _trailers(root: Path, rev: str = "HEAD") -> dict:
    body = _git(root, "log", "-1", "--format=%B", rev)
    return dict(line.split(": ", 1) for line in body.splitlines() if line.startswith("Onto-"))


def _packs(root: Path) -> dict:
    """提交 trailer 的 Onto-Packs：本项目生效中的装配里的包与版本（插件运行时登记的提供方）。"""
    from app.services.packs import governance

    return governance.packs_trailer(root)


def _files(root: Path, rev: str = "HEAD") -> list[str]:
    return sorted(filter(None, _git(root, "-c", "core.quotePath=false", "show", "--name-only", "--format=", rev).splitlines()))


def _stage_agent_change(root: Path) -> Path:
    """模拟 agent 已暂存、还没提交的改动。"""
    other = root / "research" / "agent-note.md"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("agent draft\n", encoding="utf-8")
    _git(root, "add", "--", "research/agent-note.md")
    return other


# ---- 四种入口 ----


def test_web_save_commits_only_its_path_with_trailer(manager, project, events):
    project_id, root = project
    _stage_agent_change(root)
    saved = manager.write_file(project_id, "ontology/object/item_a.md", OBJECT + "\n补充\n",
                               base_revision="", author="operator_a")
    assert saved["commit"] == _git(root, "rev-parse", "HEAD").strip()
    assert _files(root) == ["ontology/object/item_a.md"]
    assert _trailers(root) == _packs(root) | {"Onto-Source": "web", "Onto-Author": "operator_a"}
    # agent 的暂存改动原样留在索引里，没被带进提交
    assert _git(root, "diff", "--cached", "--name-only").split() == ["research/agent-note.md"]
    assert events[-1].source == "web" and events[-1].paths == ("ontology/object/item_a.md",)
    assert events[-1].project_id == project_id and events[-1].target == "mainline"


def test_studio_save_records_studio_source(manager, project):
    project_id, root = project
    manager.write_file(project_id, "ontology/object/item_a.md", OBJECT + "\nStudio\n",
                       author="operator_a", source="studio")
    assert _git(root, "log", "-1", "--format=%s").startswith("studio: 保存 ontology/object/item_a.md")
    assert _trailers(root)["Onto-Source"] == "studio"


def test_mcp_write_goes_through_tx(manager, project, monkeypatch, events):
    project_id, root = project
    monkeypatch.setattr(mcp, "editing_issue", lambda *args: None)
    other = root / "research" / "agent-note.md"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("agent draft\n", encoding="utf-8")  # 未暂存的 agent 改动
    revision = mcp.revision(root)
    mcp.execute(manager, project_id, "update_ontology_object",
                {"object_id": "object:item_a", "expected_revision": revision,
                 "metadata": {"description": "合成描述已改"}}, "operator_a")
    assert _files(root) == ["ontology/object/item_a.md"]
    assert _trailers(root) == _packs(root) | {"Onto-Source": "mcp", "Onto-Author": "operator_a"}
    assert "research/agent-note.md" in _git(root, "status", "--porcelain")
    assert events[-1].source == "mcp"


def test_import_commits_carry_import_source(manager, tmp_path):
    archive = tmp_path / "synthetic.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("ontology/object/item_b.md", OBJECT)
    meta = manager.import_zip("导入演示", archive, owner="operator_a")
    root = manager.project_root(meta["id"])
    assert _trailers(root) == _packs(root) | {"Onto-Source": "import", "Onto-Author": "operator_a"}
    assert "ontology/object/item_b.md" in _files(root)


def test_resource_upload_and_delete_use_tx(manager, project):
    project_id, root = project
    _stage_agent_change(root)
    saved = manager.save_resource(project_id, "excel", "表.csv", b"a,b\n1,2\n", author="operator_a")
    assert _files(root) == ["resource/excel/表.csv", "resource/materials.json"]
    assert saved["commit"] and _trailers(root)["Onto-Source"] == "web"
    manager.delete_resource(project_id, "resource/excel/表.csv", author="operator_a")
    assert "resource/excel/表.csv" in _files(root)
    assert not (root / "resource/excel/表.csv").exists()


# ---- 版本校验与回滚 ----


def test_revision_conflict_leaves_file_untouched(manager, project):
    project_id, root = project
    target = root / "ontology/object/item_a.md"
    head = _git(root, "rev-parse", "HEAD").strip()
    with pytest.raises(WorkspaceError) as caught:
        manager.write_file(project_id, "ontology/object/item_a.md", "新内容\n", base_revision="0" * 16)
    assert caught.value.code == "revision_conflict"
    assert caught.value.details["expected"] == "0" * 16 and caught.value.details["changed_by"]
    assert target.read_text(encoding="utf-8") == OBJECT
    assert _git(root, "rev-parse", "HEAD").strip() == head


def test_failure_during_commit_restores_previous_content(manager, project, monkeypatch):
    project_id, root = project
    target = root / "ontology/object/item_a.md"

    def broken(*args, **kwargs):
        raise WorkspaceError("synthetic commit failure", 500)

    monkeypatch.setattr(writepath_tx, "commit_paths", broken)
    with pytest.raises(WorkspaceError):
        manager.write_file(project_id, "ontology/object/item_a.md", "不会落盘\n")
    assert target.read_text(encoding="utf-8") == OBJECT
    assert not list((Path(config.DATA_DIR) / "writepath" / "intents").glob("*.json"))


def test_expected_head_guards_against_moved_mainline(manager, project):
    project_id, root = project
    (root / "notes.md").write_text("合成\n", encoding="utf-8")
    with pytest.raises(WorkspaceError) as caught:
        writepath.commit_paths(root, ["notes.md"], "web: 保存", {"Onto-Source": "web"}, expected_head="0" * 40)
    assert caught.value.code == "head_moved"


def test_head_moved_by_agent_is_rebased(manager, project, monkeypatch):
    """agent 在临时索引构造与 update-ref 之间提交：基于新 HEAD 重做，两边改动都在。"""
    project_id, root = project
    (root / "ontology/object/item_a.md").write_text(OBJECT + "\nweb\n", encoding="utf-8")
    original = writepath_tx._run_git
    moved = []

    def agent_commits_first(cwd, args, *rest, **kwargs):
        if args[0] == "update-ref" and not moved:
            moved.append(True)
            agent_file = root / "research" / "agent.md"
            agent_file.parent.mkdir(parents=True, exist_ok=True)
            agent_file.write_text("agent\n", encoding="utf-8")
            _git(root, "add", "--", "research/agent.md")
            _git(root, "commit", "-q", "-m", "agent: 资料分析", "--", "research/agent.md")
        return original(cwd, args, *rest, **kwargs)

    monkeypatch.setattr(writepath_tx, "_run_git", agent_commits_first)
    with writepath.ChangeTx(manager, project_id, writepath.Origin("web", "operator_a")) as tx:
        tx.touch("ontology/object/item_a.md", snapshot=False)
        result = tx.commit("web: 保存 item_a")
    assert _git(root, "log", "-2", "--format=%s").splitlines() == ["web: 保存 item_a", "agent: 资料分析"]
    assert result.paths == ["ontology/object/item_a.md"]
    assert (root / "research/agent.md").is_file()


# ---- trailer 扩展与提交目标扩展点 ----


def test_trailer_provider_adds_pack_versions(manager, project, monkeypatch):
    project_id, root = project
    monkeypatch.setattr(writepath_tx, "_trailer_providers", [])
    writepath.register_trailer_provider(lambda root: {"Onto-Packs": "core@1.0.0, trace@0.9.1", "Other": "x"})
    manager.write_file(project_id, "ontology/object/item_a.md", OBJECT + "\n包\n", author="operator_a")
    assert _trailers(root) == {"Onto-Source": "web", "Onto-Author": "operator_a",
                               "Onto-Packs": "core@1.0.0, trace@0.9.1"}


def test_packs_trailer_names_the_active_assembly(manager, project):
    """接上插件运行时之后，提交都带 Onto-Packs（B3 合并留下的登记点）。"""
    project_id, root = project
    manager.write_file(project_id, "ontology/object/item_a.md", OBJECT + "\n包\n", author="operator_a")
    packs = _trailers(root).get("Onto-Packs", "")
    active = json.loads((root / ".claude" / "MANIFEST.json").read_text(encoding="utf-8"))["packs"]
    assert packs == ", ".join(sorted(f"{item['name']}@{item['version']}" for item in active))
    assert packs.startswith("core@")


def test_change_target_is_pluggable(manager, project):
    """P2 变更集副本实现同一接口：事务把提交交给 target，不直接写主线。"""
    project_id, root = project

    class RecordingTarget:
        kind = "changeset"

        def __init__(self):
            self.root = root
            self.calls = []

        def head(self):
            return "base"

        def commit(self, paths, message, values, expected_head=None):
            self.calls.append((list(paths), message, values))
            return "c0ffee", "base", list(paths)

    target = RecordingTarget()
    origin = writepath.Origin("agent", "operator_a", changeset="cs-1", node="core.object@1.0.0")
    head = _git(root, "rev-parse", "HEAD").strip()
    with writepath.ChangeTx(manager, project_id, origin, target=target) as tx:
        tx.write("ontology/object/item_c.md", OBJECT)
        result = tx.commit("agent: 抽取对象")
    assert result.commit == "c0ffee" and _git(root, "rev-parse", "HEAD").strip() == head
    paths, message, values = target.calls[0]
    assert "ontology/object/item_c.md" in paths and "ontology/object/index.md" in paths  # 派生的索引一起
    assert values == _packs(root) | {"Onto-Source": "agent", "Onto-Author": "operator_a",
                      "Onto-Changeset": "cs-1", "Onto-Node": "core.object@1.0.0"}


def test_unknown_source_is_rejected():
    with pytest.raises(ValueError):
        writepath.Origin("somewhere")


def test_compat_commit_paths_infers_source_from_message(manager, project):
    project_id, root = project
    (root / "feedback").mkdir(exist_ok=True)
    (root / "feedback" / "batch.json").write_text("{}", encoding="utf-8")
    with manager._project_lock(project_id):
        commit = _commit_paths(root, ["feedback"], "feedback: 接收外部反馈 by service")
    assert commit and _trailers(root) == _packs(root) | {"Onto-Source": "plugin", "Onto-Author": "service"}


# ---- 派生文件（C6） ----


def test_index_is_refreshed_inside_the_same_commit(manager, project):
    project_id, root = project
    manager.write_file(project_id, "ontology/object/item_d.md", "# 合成对象丁\n\n" + OBJECT)
    assert _files(root) == ["ontology/object/index.md", "ontology/object/item_d.md"]
    assert "| item_d.md | 合成对象丁 |" in (root / "ontology/object/index.md").read_text(encoding="utf-8")


# ---- 回退与示例注入：只提交相关路径 ----


def test_revert_only_touches_paths_that_differ(manager, project):
    project_id, root = project
    base = _git(root, "rev-parse", "HEAD").strip()
    manager.write_file(project_id, "ontology/object/item_a.md", OBJECT + "\n改动\n")
    manager.write_file(project_id, "ontology/object/item_e.md", OBJECT)
    agent_file = root / "research" / "agent-untracked.md"
    agent_file.parent.mkdir(parents=True, exist_ok=True)
    agent_file.write_text("agent\n", encoding="utf-8")
    staged = _stage_agent_change(root)

    result = manager.revert_to(project_id, base, author="operator_a")
    assert result["reverted_to"] == base
    assert _files(root) == ["ontology/object/index.md", "ontology/object/item_a.md", "ontology/object/item_e.md"]
    assert not (root / "ontology/object/item_e.md").exists()  # 目标版本之后新增的文件删掉
    assert (root / "ontology/object/item_a.md").read_text(encoding="utf-8") == OBJECT
    assert _trailers(root) == _packs(root) | {"Onto-Source": "revert", "Onto-Author": "operator_a"}
    assert agent_file.exists() and staged.exists()
    assert "research/agent-note.md" in _git(root, "diff", "--cached", "--name-only")
    with pytest.raises(WorkspaceError) as caught:
        manager.revert_to(project_id, base)
    assert caught.value.code == "nothing_to_revert"


def test_seed_example_commits_only_what_the_script_wrote(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    manager = WorkspaceManager(data, Path(config.ASSETS_DIR))
    meta = manager.create_project("seed_demo", owner="operator_a")
    root = manager.project_root(meta["id"])
    if not (root / ".claude" / "scripts" / "seed_example.py").exists():
        pytest.skip("工具箱资产未挂载")
    agent_file = root / "research" / "agent-untracked.md"
    agent_file.parent.mkdir(parents=True, exist_ok=True)
    agent_file.write_text("agent\n", encoding="utf-8")
    manager.seed_example(meta["id"], author="operator_a")
    committed = _files(root)
    assert committed and "research/agent-untracked.md" not in committed
    assert _trailers(root)["Onto-Source"] == "plugin"
    assert "research/agent-untracked.md" in _git(root, "status", "--porcelain", "--untracked-files=all")


# ---- 崩溃恢复：写入意图 ----


def test_interrupted_write_is_committed_on_startup(manager, project):
    project_id, root = project
    tx = writepath.ChangeTx(manager, project_id, writepath.Origin("web", "operator_a"), summary="保存 item_a")
    tx.__enter__()
    tx.write("ontology/object/item_a.md", OBJECT + "\n停机前写的\n")
    intents = list((Path(config.DATA_DIR) / "writepath" / "intents").glob("*.json"))
    assert len(intents) == 1 and json.loads(intents[0].read_text())["paths"] == ["ontology/object/item_a.md"]
    tx._lock.__exit__(None, None, None)  # 模拟进程在提交前退出：锁随进程释放，意图留在盘上

    item = writepath.recover_intents(manager)
    assert item["id"] == "write_intents" and item["result"] == "committed"
    assert _files(root) == ["ontology/object/item_a.md"]
    assert _git(root, "log", "-1", "--format=%s").startswith("recover: 补提交中断的写入")
    assert _trailers(root) == _packs(root) | {"Onto-Source": "web", "Onto-Author": "operator_a"}
    assert not list((Path(config.DATA_DIR) / "writepath" / "intents").glob("*.json"))
    assert writepath.recover_intents(manager)["result"] == "ok"
