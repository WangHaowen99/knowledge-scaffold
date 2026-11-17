"""I1 联调收尾：插件详情的插入节点（PL-02 flow_insertions）、导出器能不能关（O-11）、对话出处角标（message.assistant anchors）、发布记录事件（EP-19）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import config
from app.services.packs import builtin, catalog
from app.services.workspace import WorkspaceManager
from tests.test_release_publish import manager  # noqa: F401  （夹具按名字注入）


def test_flow_insertions_carry_title_and_segment(tmp_path: Path):
    builtin.clear_cache()
    manager = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    builtin.import_core(manager.packs.repository, Path(config.ASSETS_DIR))
    builtin.import_packs(manager.packs.repository, Path(config.ASSETS_DIR))
    try:
        trace = catalog.detail(manager, "trace")
        assert {"id": "trace.trace", "title": "场景模拟", "at": "validate"} in trace["flow_insertions"]
        codegen = catalog.detail(manager, "codegen")
        assert all(item["title"] != item["id"] for item in codegen["flow_insertions"])
        assert all(item.get("at") in {"research", "model", "validate", "publish"}
                   for item in codegen["flow_insertions"])
    finally:
        builtin.clear_cache()


def test_exporters_mark_required_and_builtin():
    from app.services import ops_center

    items = {item["id"]: item for item in ops_center.exporter_items()}
    assert items["file"]["required"] is True and items["prometheus"]["required"] is False
    assert {item["implementation"] for item in items.values()} == {"builtin"}


def test_assistant_citations_become_anchors():
    from app.services.session_host import timeline

    text = ("补货上限按门店分级[^1]，紧急补货要店长审批[^2]。\n\n"
            "[^1]: resource/补货制度.docx#p:3\n[^2]: file:resource/others/审批流程.md#L12\n")
    body, anchors = timeline.split_citations(text)
    assert body == "补货上限按门店分级[^1]，紧急补货要店长审批[^2]。"
    assert anchors == [{"n": 1, "anchor": "resource/补货制度.docx#p:3"},
                       {"n": 2, "anchor": "file:resource/others/审批流程.md#L12"}]
    # 不是锚点的脚注原样留着；没有脚注时不动
    plain = "见说明[^1]\n\n[^1]: 这是一句补充"
    assert timeline.split_citations(plain) == (plain, [])
    assert timeline.split_citations("没有出处") == ("没有出处", [])


def test_release_updated_on_publish(manager, monkeypatch, hub_login):
    """EP-19：发布之后项目流发 release.updated（发布页按它实时合并记录）。"""
    from fastapi.testclient import TestClient

    from app.api import deps
    from app.main import app
    from app.services import auth, streams
    from tests.test_event_streams import buffered
    from tests.test_release_publish import rich_project

    monkeypatch.setattr(deps, "workspaces", manager)
    streams.reset_all()
    pid, _root = rich_project(manager)
    manager._update_meta(pid, owner="operator_a")
    response = TestClient(app, headers=hub_login).post(f"/api/v1/projects/{pid}/releases", json={})
    assert response.status_code == 201
    events = buffered(pid, "release.updated")
    assert events and events[-1]["data"]["release"]["version"] == response.json()["version"]


def test_web_research_action_needs_websearch_pack():
    """联网资料分析要联网调研包：没启用时研究节点不出这个动作（用不了的入口不出现），启用了照常。"""
    import yaml

    from app.services.flow import assemble

    toolkit = Path(config.ASSETS_DIR) / "workspace-toolkit"
    core = yaml.safe_load((toolkit / "flow.yaml").read_text(encoding="utf-8"))

    def labels(*packs):
        contributions = [("core", "test", core)] + [(name, "test", {}) for name in packs]
        flow = assemble.assemble(contributions)
        return [action["label"] for action in next(n for n in flow["nodes"] if n["id"] == "core.research")["actions"]]

    assert "联网资料分析" not in labels() and labels()[0] == "资料分析"
    assert labels("websearch")[0] == "联网资料分析"


def test_template_object_sections_are_not_out_of_template(tmp_path: Path):
    """条目模板里的「数据来源」「查询边界」登记在元模型里：按模板新建的对象在 Studio 保存时不因这两节转助手复核。"""
    from app.services import studio as studio_service

    template = (Path(config.ASSETS_DIR) / "workspace-toolkit" / "templates" / "object-template.md").read_text(encoding="utf-8")
    review = studio_service.deterministic_check(tmp_path, "ontology/object/借阅证.md", template)["review"]
    assert not any("数据来源" in item or "查询边界" in item for item in review), review


def test_template_placeholders_are_not_unresolved_links(tmp_path: Path):
    """按模板新建的对象、操作、规则直接保存：模板占位 `[[{待填写：…}]]` 与行内代码里的写法示例不算指向不存在的文档。"""
    from app.services import studio as studio_service

    templates = Path(config.ASSETS_DIR) / "workspace-toolkit" / "templates"
    for kind, folder in (("object", "object"), ("operation", "operation"), ("rule", "rule")):
        text = (templates / f"{kind}-template.md").read_text(encoding="utf-8")
        checks = studio_service.deterministic_check(tmp_path, f"ontology/{folder}/合成.md", text)
        assert not any("指向不存在的文档" in item for item in checks["review"] + checks["hints"]), (kind, checks)
    # 真的写错了的链接照样提示（F3：只提示，不转复核）
    text = (templates / "object-template.md").read_text(encoding="utf-8") + "\n见 [[不存在的对象]]\n"
    hints = studio_service.deterministic_check(tmp_path, "ontology/object/合成.md", text)["hints"]
    assert any("不存在的对象" in item for item in hints)



def test_action_conditions_must_use_declared_probes():
    """动作的条件 when 用到的探针要声明：装配时没声明的报问题（端到端验证：「追加分析」只在有资料分析报告时出现）。"""
    import yaml

    from app.services.flow import assemble

    toolkit = Path(config.ASSETS_DIR) / "workspace-toolkit"
    core = yaml.safe_load((toolkit / "flow.yaml").read_text(encoding="utf-8"))
    research = next(node for node in core["nodes"] if node["id"] == "core.research")
    assert next(action for action in research["actions"] if action["label"] == "追加分析")["when"] == ["research_report"]
    bad = {"nodes": [{"id": "sample.check", "title": "合成检查", "kind": "agent", "placement": "on_demand",
                      "section": "on_demand", "entry": {"command": "/sample-check"},
                      "actions": [{"label": "检查", "command": "/sample-check", "when": ["no_such_probe"]}]}]}
    with pytest.raises(assemble.FlowAssemblyError) as caught:
        assemble.assemble([("core", "test", core), ("sample", "0.1.0", bad)])
    assert "no_such_probe" in str(caught.value)
