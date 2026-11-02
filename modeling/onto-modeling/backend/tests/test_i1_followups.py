"""I1 联调收尾：插件详情的插入节点（PL-02 flow_insertions）、导出器能不能关（O-11 editable）、对话出处角标（message.assistant anchors）。"""

from __future__ import annotations

from pathlib import Path

from app import config
from app.services.packs import builtin, catalog
from app.services.workspace import WorkspaceManager


def test_flow_insertions_carry_title_and_segment(tmp_path: Path):
    builtin.clear_cache()
    manager = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    builtin.import_core(manager.packs.repository, Path(config.ASSETS_DIR))
    builtin.import_packs(manager.packs.repository, Path(config.ASSETS_DIR))
    try:
        trace = catalog.detail(manager, "trace")
        assert {"id": "trace.trace", "title": "全量回归", "at": "validate"} in trace["flow_insertions"]
        codegen = catalog.detail(manager, "codegen")
        assert all(item["title"] != item["id"] for item in codegen["flow_insertions"])
        assert all(item.get("at") in {"research", "model", "validate", "consistency", "publish"}
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
