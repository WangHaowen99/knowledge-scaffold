"""I1 联调收尾：插件详情的插入节点（PL-02 flow_insertions）、导出器能不能关（O-11）、对话出处角标（message.assistant anchors）、发布记录事件（EP-19）。"""

from __future__ import annotations

from pathlib import Path

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


def test_release_updated_on_publish(manager, monkeypatch):
    """EP-19：发布之后项目流发 release.updated（发布页按它实时合并记录）。"""
    from fastapi.testclient import TestClient

    from app.api import deps
    from app.main import app
    from app.services import auth, streams
    from tests.test_event_streams import buffered
    from tests.test_release_publish import rich_project

    monkeypatch.setattr(deps, "workspaces", manager)
    monkeypatch.setattr(auth, "ENABLED", False)
    streams.reset_all()
    pid, _root = rich_project(manager)
    response = TestClient(app).post(f"/api/v1/projects/{pid}/releases", json={})
    assert response.status_code == 201
    events = buffered(pid, "release.updated")
    assert events and events[-1]["data"]["release"]["version"] == response.json()["version"]


def test_client_config_says_evidence_unavailable():
    """C-01 features.evidence=false：前端据此不再调 EV-01 探测（本期 501，探测会在控制台留错误）。"""
    from fastapi.testclient import TestClient

    from app.main import app

    features = TestClient(app).get("/api/v1/client-config").json()["features"]
    assert features["evidence"] is False and "drawio" in features
