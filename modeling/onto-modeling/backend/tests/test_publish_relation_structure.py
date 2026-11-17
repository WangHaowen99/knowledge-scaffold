"""发布导出的对象关系结构（吸收 develop !594）：语义声明原样保留，结构错误在规范化前拒绝。

同一条关系在两端对象里各写一次（对象模板的写法）归并成一条，发布不做重复关系检查。
合成项目，经发布适配器按提交渲染（与发布同一路径）；两种变体（标准、图）同一口径。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from app.services.publish import gates
from app.services.publish.spi import ReleaseSource
from app.services.publish.tree import release_tree
from app.services.workspace import WorkspaceError, _git
from tests.test_publish_render_regression import PROJECT_ID, _create, _object_doc, _write, manager, render  # noqa: F401

VARIANTS = [{}, {"graph": True}]


def _project(manager, relation: str, *, target: str = "客户", target_columns=("CLIENT_ID", "NAME")) -> Path:
    root = _create(manager, "关系结构")
    _write(root, "ontology/object/订单.md", _object_doc(
        None, ["ORDER_ID", "CLIENT_ID"], "\n# 关联对象\n" + relation,
    ))
    _write(root, f"ontology/object/{target}.md", _object_doc(None, list(target_columns)))
    return root


def _object(data: bytes, key: str) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        name = next(item for item in archive.namelist() if item.endswith(f"objects/{key}.md"))
        return archive.read(name).decode("utf-8")


@pytest.mark.parametrize("options", VARIANTS)
@pytest.mark.parametrize("name", ["关联/包含", "1:N", "属于客户"])
def test_relation_names_with_punctuation_are_exported_unchanged(manager, tmp_path, options, name):
    root = _project(manager, f"- 当前对象 → {name}（CLIENT_ID → CLIENT_ID） → [[客户]]\n")
    data = render(manager, root, tmp_path / "out.zip", {**options, "allow_unbound": True})
    assert f"| {name} | 客户 |" in _object(data, "订单")


@pytest.mark.parametrize("options", VARIANTS)
def test_relation_to_object_without_table_keeps_semantic_edge(manager, tmp_path, options):
    root = _project(manager, "- 当前对象 → 涉及 → [[合成概念]]\n", target="合成概念", target_columns=("CONCEPT_ID",))
    data = render(manager, root, tmp_path / "concept.zip", {**options, "allow_unbound": True})
    assert "| 涉及 | 合成概念 |" in _object(data, "订单")


@pytest.mark.parametrize("options", VARIANTS)
@pytest.mark.parametrize("relation", [
    "- 当前对象 → 属于 → [[不存在的对象]]\n",
    "- 当前对象 → 属于（MISSING_FIELD → CLIENT_ID） → [[客户]]\n",
])
def test_structural_errors_block_export_before_relations_are_dropped(manager, tmp_path, options, relation):
    root = _project(manager, relation)
    dest = tmp_path / "blocked.zip"
    with pytest.raises(WorkspaceError) as error:
        render(manager, root, dest, {**options, "allow_unbound": True})
    assert error.value.code == "relation_structure_invalid" and error.value.status == 422
    assert "ontology/object/订单.md" in str(error.value)
    assert not dest.exists() or dest.stat().st_size == 0


@pytest.mark.parametrize("options", VARIANTS)
def test_relation_declared_on_both_ends_is_merged_not_rejected(manager, tmp_path, options):
    """对象模板让同一条关系在两端各写一次：发布归并成一条，不当重复关系拒绝；同一端写两遍也只留一条。"""
    root = _create(manager, "关系结构")
    relation = "- 当前对象 → 属于（CLIENT_ID → CLIENT_ID） → [[客户]]\n"
    _write(root, "ontology/object/订单.md", _object_doc(
        None, ["ORDER_ID", "CLIENT_ID"], "\n# 关联对象\n" + relation * 2,
    ))
    _write(root, "ontology/object/客户.md", _object_doc(
        None, ["CLIENT_ID", "NAME"], "\n# 关联对象\n- [[订单]] → 属于（CLIENT_ID → CLIENT_ID） → 当前对象\n",
    ))
    data = render(manager, root, tmp_path / "merged.zip", {**options, "allow_unbound": True})
    assert _object(data, "订单").count("| 属于 | 客户 |") == 1
    assert "| 属于 |" not in _object(data, "客户")


def _findings(manager, root):
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "test: 关系结构")
    head = _git(root, "rev-parse", "HEAD").strip()
    with release_tree(root, head) as tree:
        src = ReleaseSource(project_id=PROJECT_ID, commit=head, tree=tree, meta=manager.get_project(PROJECT_ID),
                            version="0.1.0", state_root=root)
        return gates.render_findings(src)


def test_publish_check_lists_relation_structure_problem(manager):
    """发布检查逐条列关系问题：哪个对象、第几行（打开文档用）、什么问题（不带路径）。"""
    root = _project(manager, "- 当前对象 → 属于 → [[不存在的对象]]\n- 当前对象 → 涉及 → [[另一个缺失对象]]\n")
    findings = _findings(manager, root)
    assert [item["code"] for item in findings] == ["relation_structure_invalid"] * 2
    first = findings[0]
    assert first["message"] == "关系端点「不存在的对象」不存在"
    assert first["path"] == "ontology/object/订单.md" and isinstance(first["line"], int)
    assert first["target"] == {"kind": "object", "id": "object:订单", "key": "订单", "name": "订单"}
    gate = gates.gate_chain([], {"enabled": False}, findings)[-1]
    assert gate["id"] == "package_structure" and gate["blocking"]
    assert gate["summary"] == "2 处对象关系写法有误，consumer 导入会拒绝"
    single = gates.gate_chain([], {"enabled": False}, findings[:1])[-1]
    assert single["summary"] == "「订单」关系端点「不存在的对象」不存在"


def test_binding_gate_does_not_claim_unbound_objects_are_bound():
    """一个对象都没绑表、也没声明不落库时，数据绑定不能说「都已绑定」：给提醒（不挡发布）。"""
    objects = {"订单": {"table": "", "declared_none": False}, "附注": {"table": "", "declared_none": True},
               "客户": {"table": "T_CLIENT", "declared_none": False}}
    gate = gates.gate_chain([], {"enabled": False}, [], objects=objects)[0]
    assert gate["id"] == "data_binding" and gate["status"] == "warn" and not gate["blocking"]
    assert gate["summary"] == "1 个对象没有绑定数据表，数据问答查不到它们"
    bound = gates.gate_chain([], {"enabled": False}, [], objects={"客户": objects["客户"], "附注": objects["附注"]})[0]
    assert bound["status"] == "pass" and bound["summary"] == "对象都已绑定数据表或声明不落库"
