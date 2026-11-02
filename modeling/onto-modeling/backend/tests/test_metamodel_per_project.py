"""元模型按项目锁定版本（B3 合并遗留，gaps「B3 合并」第 2 条）：MCP 文档模型、发布 frontmatter 的
`uses.objects`、`package_mapping` 的关系端点、兼容中间件的条目 ID 都改按项目锁定的版本取，不再一律用镜像版本。

做法是 `metamodel.using(root)` 定下本项目的版本、深处的辅助函数取 `metamodel.current()`。这里用一个
「改过的元模型」（镜像版本的副本，改掉一处行为）替换 `for_project`，看四处是否都用了它。测试数据全部合成。
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from app.api import compat, deps
from app.kernel import metamodel
from app.services import package_mapping
from app.services.onto_mcp import workspace as mcp
from app.services.publish import ontology_package
from app.services.workspace import WorkspaceManager, _git


def _variant(**overrides) -> types.ModuleType:
    """镜像元模型的副本，按 overrides 改掉几处（模拟 core 升级后元模型变了）。"""
    module = types.ModuleType("onto_metamodel_variant")
    module.__dict__.update({key: value for key, value in vars(metamodel.mm).items() if not key.startswith("__")})
    module.__dict__.update(overrides)
    return module


@pytest.fixture
def project(tmp_path, monkeypatch):
    assets = tmp_path / "assets"
    (assets / "workspace-toolkit/templates").mkdir(parents=True)
    (assets / "workspace-rules.md").write_text("# synthetic rules")
    manager = WorkspaceManager(tmp_path / "data", assets)
    monkeypatch.setattr(deps, "workspaces", manager)
    meta = manager.create_project("metamodel_demo", owner="operator_a")
    root = manager.project_root(meta["id"])
    (root / "ontology/object/synthetic_a.md").write_text("# 对象描述\n\nsynthetic\n", encoding="utf-8")
    (root / "ontology/rule/synthetic_rule.md").write_text("# 规则描述\n\nsynthetic\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "test: synthetic fixture")
    monkeypatch.setattr(mcp, "editing_issue", lambda *args: None)
    return manager, meta["id"], root


def test_mcp_document_model_uses_project_metamodel(project, monkeypatch):
    manager, project_id, root = project
    image = mcp.execute(manager, project_id, "list_ontology_rules", {}, "operator_a")
    assert image["total"] == 1
    # 本项目锁定的元模型里没有「规则」这类实体：MCP 文档模型跟着它走
    variant = _variant(ENTITY_TYPES=tuple(item for item in metamodel.mm.ENTITY_TYPES if item.kind != "rule"))
    seen = []
    monkeypatch.setattr(metamodel, "for_project", lambda path: seen.append(Path(path)) or variant)
    result = mcp.execute(manager, project_id, "list_ontology_rules", {}, "operator_a")
    assert result["total"] == 0 and seen == [root]
    # with 块之外恢复镜像版本
    assert metamodel.current() is metamodel.mm


def test_publish_used_objects_follow_the_project_metamodel(project, monkeypatch):
    _manager, _project_id, root = project
    body = "引用 [[对象甲]] 与 [[对象乙]]"
    assert ontology_package._used_objects(body, ["对象甲", "对象乙"]) == sorted(["对象甲", "对象乙"])
    variant = _variant(link_names=lambda text, names=None: ["对象乙"])
    with metamodel.using(variant):
        assert ontology_package._used_objects(body, ["对象甲", "对象乙"]) == ["对象乙"]
    # render 入口按项目根目录定下版本（项目根是回执目录的上一级）
    captured = []
    monkeypatch.setattr(metamodel, "for_project", lambda path: variant)
    monkeypatch.setattr(ontology_package.OntologyPackageAdapter, "_render",
                        lambda self, src, out: captured.append(metamodel.current()) or "rendered")
    source = types.SimpleNamespace(state_root=root / ".ontology-state")
    assert ontology_package.OntologyPackageAdapter().render(source, root / "out.zip") == "rendered"
    assert captured == [variant]


def test_relation_endpoints_follow_the_current_metamodel():
    assert package_mapping._relation_node("[[对象甲]]") == "对象甲"
    variant = _variant(parse_link=lambda value: metamodel.mm.parse_link_text("对象丙"))
    with metamodel.using(variant):
        assert package_mapping._relation_node("[[对象甲]]") == "对象丙"
        assert package_mapping._endpoint("x", {"y": 1}) == "x"


def test_compat_entry_ids_use_the_project_metamodel(project, monkeypatch):
    _manager, project_id, root = project
    assert compat.entry_id_for_path("ontology/object/synthetic_a.md", project_id) == "object:synthetic_a"
    variant = _variant(entry_key=lambda path: "object:按项目版本")
    monkeypatch.setattr(metamodel, "for_project", lambda path: variant if Path(path) == root else metamodel.mm)
    assert compat.entry_id_for_path("ontology/object/synthetic_a.md", project_id) == "object:按项目版本"
    # 不存在的项目：退回镜像版本（改写后由 v1 处理器回 404）
    assert compat.entry_id_for_path("ontology/object/synthetic_a.md", "no_such_project") == "object:synthetic_a"
