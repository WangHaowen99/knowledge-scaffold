"""I1：上传资料的原文与出处（R-06 原文预览、EV-29 `file:` 锚点、ST-06 证据侧栏）。

抽取走 core 的 source_extract（和助手、资料分析验收同一份），锚点片段即它的位置。测试数据全部为合成内容。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import conversion_samples as samples

from app import config
from app.api import deps
from app.main import app
from app.services import auth, material_text
from app.services.workspace import WorkspaceManager

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"  # NOSONAR(S5332) OOXML 命名空间标识，不是网络请求
RULES_MD = "# 合成补货规则\n\n## 第一章 总则\n\n门店按周补货。\n\n- 安全库存取两周用量\n- 陈列量不超过货架容量\n\n| 项 | 值 |\n| --- | --- |\n| 上限 | 96 |\n"


def docx(paragraphs: list[str], table: list[list[str]] | None = None) -> bytes:
    body = "".join(f'<w:p><w:r><w:t>{text}</w:t></w:r></w:p>' for text in paragraphs)
    if table:
        rows = "".join("<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{cell}</w:t></w:r></w:p></w:tc>" for cell in row)
                       + "</w:tr>" for row in table)
        body += f"<w:tbl>{rows}</w:tbl>"
    xml = f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        # 包结构写全（内容类型、关系），网关的转换器才打得开
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                         '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                         '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument'
                         '.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                         '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
                         'officeDocument" Target="word/document.xml"/></Relationships>')
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(auth, "ENABLED", False)
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    workspaces = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", workspaces)
    with TestClient(app) as client:
        project_id = workspaces.create_project("资料原文", owner="employee_0001")["id"]
        base = f"/api/v1/projects/{project_id}"
        for name, data in (("01-补货规则.md", RULES_MD.encode()),
                           ("02-配送说明.docx", docx(["配送说明", "隔日到货的门店按 1.5 倍备货。", "冷链品单独配送。"],
                                                     [["品类", "到货周期"], ["牛奶", "隔日"]])),
                           ("03-扫描件.pdf", b"%PDF-1.4 synthetic")):
            response = client.post(f"{base}/resources", files={"file": (name, data)})
            assert response.status_code == 201, response.text
        # Word 的位置读多源连接器转出的文本版（ADR-0076、ADR-0093）；测试里没有网关，用同一个转换器写文本版
        samples.write_text_version(workspaces.project_root(project_id), "resource/others/02-配送说明.docx")
        yield client, workspaces, project_id, workspaces.project_root(project_id), base


def test_preview_blocks_follow_source_extract_anchors(env):
    client, _workspaces, _pid, _root, base = env
    md = client.get(f"{base}/resources/preview", params={"path": "resource/others/01-补货规则.md"}).json()
    kinds = [(block["anchor"], block["kind"]) for block in md["blocks"]]
    assert kinds[:4] == [("L1", "heading"), ("L3", "heading"), ("L5", "paragraph"), ("L7", "list")]
    assert md["blocks"][0]["level"] == 1 and md["blocks"][0]["text"] == "合成补货规则"
    assert ("L10", "table") in kinds and md["format"] == "md" and md["next_cursor"] is None
    doc = client.get(f"{base}/resources/preview", params={"path": "resource/others/02-配送说明.docx"}).json()
    anchors = [block["anchor"] for block in doc["blocks"]]
    assert anchors[:3] == ["p:1", "p:2", "p:3"] and "table:1/row:2/cell:1" in anchors
    assert next(b for b in doc["blocks"] if b["anchor"] == "table:1/row:2/cell:2") == {
        "anchor": "table:1/row:2/cell:2", "kind": "table", "text": "隔日"}
    # 分页与定位：around 那一段落在这一页里
    first = client.get(f"{base}/resources/preview", params={"path": "resource/others/01-补货规则.md", "limit": 2}).json()
    assert len(first["blocks"]) == 2 and first["next_cursor"]
    second = client.get(f"{base}/resources/preview", params={"path": "resource/others/01-补货规则.md", "limit": 2,
                                                             "cursor": first["next_cursor"]}).json()
    assert second["blocks"][0]["anchor"] == "L5"
    around = client.get(f"{base}/resources/preview", params={"path": "resource/others/01-补货规则.md", "limit": 2,
                                                             "around": "L10"}).json()
    assert "L10" in [block["anchor"] for block in around["blocks"]]
    # 读不了的格式没有块（前端按扫描件提示上传文本版）；越界、不存在、不是资料
    pdf = client.get(f"{base}/resources/preview", params={"path": "resource/others/03-扫描件.pdf"}).json()
    assert pdf["blocks"] == [] and pdf["format"] == "pdf"
    assert client.get(f"{base}/resources/preview", params={"path": "resource/others/没有.md"}).json()["code"] == "file_not_found"
    assert client.get(f"{base}/resources/preview", params={"path": "ontology/object/x.md"}).json()["code"] == "path_forbidden"
    assert client.get(f"{base}/resources/preview", params={"path": "resource/materials.json"}).json()["code"] == "path_forbidden"
    assert client.get(f"{base}/resources/preview", params={"path": "resource/../.git/config"}).json()["code"] == "path_forbidden"
    bad = client.get(f"{base}/resources/preview", params={"path": "resource/others/01-补货规则.md", "cursor": "x"})
    assert bad.status_code == 400 and bad.json()["code"] == "invalid_cursor"


def test_file_anchor_resolves_with_context_and_highlight(env):
    client, _workspaces, _pid, _root, base = env
    got = client.post(f"{base}/anchors/resolve", json={"anchors": [
        "file:resource/others/02-%E9%85%8D%E9%80%81%E8%AF%B4%E6%98%8E.docx#p:2",
        "resource/others/01-补货规则.md#L5",
        "file:resource/others/02-配送说明.docx#table:1/row:2/cell:2",
        "file:resource/others/01-补货规则.md#L99",
        "file:resource/others/没有.md#L1",
        "file:resource/others/03-扫描件.pdf#p:1",
        "file:ontology/object/门店.md#L1",
        "db:mes-prod/mes.std_limit.limit_max",
        "随便写的",
    ]})
    assert got.status_code == 200, got.text
    items = got.json()["items"]
    first = items[0]
    assert first["resolved"] and first["scheme"] == "file" and first["page"] == "resources"
    assert first["source_label"] == "02-配送说明" and first["location_label"] == "第 2 段"
    assert first["material"] == {"name": "02-配送说明", "type": "Word", "path": "resource/others/02-配送说明.docx"}
    assert first["text"] == "配送说明\n隔日到货的门店按 1.5 倍备货。\n冷链品单独配送。"
    start, end = first["highlights"][0]["start"], first["highlights"][0]["end"]
    assert first["text"][start:end] == "隔日到货的门店按 1.5 倍备货。"
    assert items[1]["resolved"] and items[1]["location_label"] == "第一章 总则" and "门店按周补货。" in items[1]["text"]
    assert items[2]["resolved"] and items[2]["location_label"] == "表 1 · 第 2 行 · 第 2 列"
    assert [item["reason"] for item in items[3:]] == ["not_found", "file_missing", "unreadable", "invalid",
                                                      "not_available", "invalid"]
    assert items[7]["scheme"] == "db" and items[7]["page"] is None
    assert set(first) == {"anchor", "scheme", "resolved", "source_label", "location_label", "material", "text",
                          "highlights", "confidence", "snapshot_id", "page"}
    single = client.get(f"{base}/anchors/resolve", params={"anchor": "file:resource/others/01-补货规则.md"}).json()
    assert single["items"][0]["resolved"] and single["items"][0]["highlights"] == []
    assert client.get(f"{base}/anchors/resolve").json()["code"] == "invalid_argument"
    assert client.post(f"{base}/anchors/resolve", json={"anchors": ["x"] * 101}).json()["code"] == "invalid_argument"
    assert client.post(f"{base}/anchors/resolve", json={"anchors": "x"}).json()["code"] == "invalid_argument"


def test_studio_evidence_lists_file_anchors_in_the_entry(env):
    client, workspaces, _pid, root, base = env
    text = ("# 门店\n\n## 对象描述\n\n合成门店，依据 file:resource/others/01-补货规则.md#L5。\n\n"
            "## 对象属性清单\n\n| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |\n"
            "| --- | --- | --- | --- | --- | --- | --- |\n"
            "| 到货周期 | cycle | 见 resource/others/02-配送说明.docx#table:1/row:2/cell:2 | 否 | 字符串 | 无 | 无 |\n")
    path = root / "ontology/object/门店.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    got = client.get(f"{base}/studio/entries/object:门店/evidence").json()
    assert got["snapshot_basis"] == []
    assert [item["anchor"] for item in got["items"]] == [
        "file:resource/others/01-补货规则.md#L5", "file:resource/others/02-配送说明.docx#table:1/row:2/cell:2"]
    assert got["items"][1]["used_for"] == ["到货周期"] and got["items"][0]["used_for"] == []
    assert all(item["resolved"]["resolved"] for item in got["items"])
    assert client.get(f"{base}/studio/entries/object:没有/evidence").json()["code"] == "not_found"


def test_find_anchors_reads_paths_in_reports_and_entries():
    found = material_text.find_anchors("来源位置 `resource/others/规则.docx#p:2`，另见 file:resource/excel/表.xlsx#sheet:规则!B3。")
    assert found[0][:2] == ("resource/others/规则.docx", "p:2")
    assert found[1][:2] == ("resource/excel/表.xlsx", "sheet:规则!B3")
    assert material_text.parse_anchor("file:resource/a%20b.md#L3") == {"scheme": "file", "path": "resource/a b.md",
                                                                        "fragment": "L3"}


# ---- R-05 被谁引用、R-01 referenced、R-04 broken_references ----


def _commit_entry(root: Path, rel: str, text: str) -> None:
    from tests.test_changesets import main_write

    main_write(root, rel, text, "web: 合成条目")


def test_references_from_entries_and_counts(env):
    client, _workspaces, _pid, root, base = env
    _commit_entry(root, "ontology/rule/超量规则.md",
                  "# 超量规则\n\n## 规则描述\n\n超过上限要审批。\n\n## 来源\n\n"
                  "- resource/others/01-补货规则.md#L7\n- file:resource/others/01-补货规则.md#L10\n")
    _commit_entry(root, "ontology/object/门店.md", "# 门店\n\n## 对象描述\n\n见《01-补货规则.md》。\n")
    _commit_entry(root, "research/20261102-合成.md", "依据 resource/others/01-补货规则.md#L5\n")
    got = client.get(f"{base}/resources/references", params={"path": "resource/others/01-补货规则.md"}).json()
    assert got["path"] == "resource/others/01-补货规则.md" and got["total"] == 2
    by_id = {item["target"]["id"]: item for item in got["items"]}
    assert by_id["rule:超量规则"]["anchors"] == ["L7", "L10"] and by_id["rule:超量规则"]["location"] == "来源"
    assert by_id["object:门店"]["anchors"] == [] and by_id["object:门店"]["location"] == "对象描述"
    assert by_id["rule:超量规则"]["target"] == {"kind": "rule", "id": "rule:超量规则", "key": "超量规则", "name": "超量规则"}
    listed = client.get(f"{base}/resources").json()
    materials = {item["path"]: item for group in listed["groups"] for item in group["items"]}
    assert materials["resource/others/01-补货规则.md"]["referenced"] == 2
    assert materials["resource/others/02-配送说明.docx"]["referenced"] == 0
    assert all(set(option) == {"value", "label"} for option in listed["purposes"])
    assert client.get(f"{base}/resources/references", params={"path": "ontology/rule/超量规则.md"}).json()["code"] == "path_forbidden"
    deleted = client.delete(f"{base}/resources", params={"path": "resource/others/01-补货规则.md"}).json()
    assert deleted["broken_references"] == 2


def test_preview_of_text_version_locates_line_ranges(env):
    """资料芯片打开文本版的出处（P5b）：行号范围按第一行定位；文件头不出块，页标记记在后面的块上。"""
    from app.evidence import convert

    client, _workspaces, _pid, root, base = env
    rel = "resource/text/others/03-扫描件.pdf.md"
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text("\n".join([
        convert.header("03-扫描件.pdf", "0" * 64, "convert-pdf 1.0.0"),
        convert.page_marker(1), "合成手册第一页。",
        convert.page_marker(2), "| 品类 | 周期 |", "|---|---|", "| 牛奶 | 隔日 |", "冷链品单独配送。",
    ]) + "\n", encoding="utf-8")

    data = client.get(f"{base}/resources/preview", params={"path": rel, "around": "L6-L7", "limit": 3}).json()
    assert [block["anchor"] for block in data["blocks"]] == ["L5", "L6", "L7"]
    assert {block["location"] for block in data["blocks"]} == {"第 2 页"}

    whole = client.get(f"{base}/resources/preview", params={"path": rel}).json()["blocks"]
    assert [block["anchor"] for block in whole] == ["L3", "L5", "L6", "L7", "L8"]
    assert [block.get("location") for block in whole] == ["第 1 页", "第 2 页", "第 2 页", "第 2 页", "第 2 页"]
    assert whole[1]["kind"] == "table"

    native = client.get(f"{base}/resources/preview",
                        params={"path": "resource/others/01-补货规则.md", "around": "L5-L7", "limit": 2}).json()
    assert [block["anchor"] for block in native["blocks"]] == ["L3", "L5"]
    assert "location" not in native["blocks"][0]
