"""F4b-paths：界面上不出工作区路径和文件名——路径的业务名、出处的「资料名 · 位置」。"""
from __future__ import annotations

from app.services import material_text, workspace_labels
from tests.test_i1_material_text import env  # noqa: F401 - 夹具

TEXT_VERSION = "\n".join([
    '<!-- onto:text source="04-合成手册.pdf" sha256="' + "0" * 16 + '" converter="pdf@1" -->',
    "<!-- 第 1 页 -->",
    "# 合成手册",
    "封面说明。",
    "<!-- 第 3 页 -->",
    "## 补货",
    "门店按周补货。",
    "冷链品单独配送。",
    "",
])
SHEET_VERSION = "\n".join(["<!-- 工作表：客户 -->", "| 名称 | 等级 |", "| --- | --- |", "| 甲 | A |", ""])


def test_path_labels_use_business_names():
    label = workspace_labels.path_label
    assert label("ontology/object/门店.md") == "对象「门店」"
    assert label("research/blueprint.json") == "建模蓝图"
    assert label("research/20261103-资料分析.md") == "资料分析报告"
    assert label("resource/others/02-合同.pdf") == "资料「02-合同」"
    assert label("reports/rule-check.md") == "检查报告"
    for unknown in ("resource/text/others/02-合同.pdf.md", "notes/草稿.md", ".onto/run/x.json", "reports/autobuild/r1/a.md"):
        assert label(unknown) == ""
    assert workspace_labels.material("resource/text/excel/客户.xlsx.md") == {
        "name": "客户", "type": "Excel", "path": "resource/excel/客户.xlsx"}


def test_text_version_anchor_says_material_name_and_page(env):
    client, _workspaces, _pid, root, base = env
    (root / "resource" / "others" / "04-合成手册.pdf").write_bytes(b"%PDF-1.4 synthetic")
    (root / "resource" / "text" / "others").mkdir(parents=True, exist_ok=True)
    (root / "resource" / "text" / "others" / "04-合成手册.pdf.md").write_text(TEXT_VERSION, encoding="utf-8")
    (root / "resource" / "text" / "excel").mkdir(parents=True, exist_ok=True)
    (root / "resource" / "text" / "excel" / "客户.xlsx.md").write_text(SHEET_VERSION, encoding="utf-8")
    (root / "resource" / "excel").mkdir(parents=True, exist_ok=True)
    (root / "resource" / "excel" / "客户.xlsx").write_bytes(b"synthetic")
    items = client.post(f"{base}/anchors/resolve", json={"anchors": [
        "file:resource/text/others/04-合成手册.pdf.md#L7-L8",
        "resource/text/others/04-合成手册.pdf.md#L4",
        "file:resource/text/excel/客户.xlsx.md#L4",
        "file:resource/others/01-补货规则.md#L1",
        "file:resource/text/others/04-合成手册.pdf.md#L99",
    ]}).json()["items"]
    page = items[0]
    assert page["resolved"] and page["source_label"] == "04-合成手册" and page["location_label"] == "第 3 页"
    assert page["material"] == {"name": "04-合成手册", "type": "PDF", "path": "resource/others/04-合成手册.pdf"}
    start, end = page["highlights"][0]["start"], page["highlights"][0]["end"]
    assert page["text"][start:end] == "门店按周补货。\n冷链品单独配送。"
    assert items[1]["location_label"] == "第 1 页"
    assert items[2]["source_label"] == "客户" and items[2]["location_label"] == "工作表 客户"
    assert items[3]["resolved"] and items[3]["location_label"] == "合成补货规则"
    assert items[4]["reason"] == "not_found" and items[4]["source_label"] == "04-合成手册"
    for item in items:
        assert "/" not in item["source_label"] and ".md" not in item["source_label"]
        assert "行" not in item["location_label"]


def test_line_location_without_markers_or_headings_is_empty(tmp_path):
    (tmp_path / "resource" / "others").mkdir(parents=True)
    (tmp_path / "resource" / "others" / "a.txt").write_text("一\n二\n", encoding="utf-8")
    (tmp_path / "resource" / "others" / "b.md").write_text("正文\n\n更多\n", encoding="utf-8")
    assert material_text.line_location(tmp_path, "resource/others/a.txt", 2) == ""
    assert material_text.line_location(tmp_path, "resource/others/b.md", 3) == ""
