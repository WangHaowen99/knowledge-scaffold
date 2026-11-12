"""原始资料分段预览（source_preview）与规则「原始资料来源」章节登记的测试。全部为合成数据。"""

import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import conversion_samples as samples
from app import config
from app.api import deps
from app.main import app
from app.services import auth, source_preview, studio
from app.services.toolkit import source_extract
from app.services.workspace import WorkspaceManager

# OOXML 命名空间是格式标识符，不是网络请求；复用抽取器常量，保持测试与标准一致。
W = source_extract.W.removeprefix("{").removesuffix("}")


def _paragraph(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" if text else "<w:p/>"


def _table(rows: list[list[str]]) -> str:
    body = "".join(
        "<w:tr>" + "".join(f"<w:tc>{_paragraph(cell)}</w:tc>" for cell in row) + "</w:tr>"
        for row in rows
    )
    return f"<w:tbl>{body}</w:tbl>"


def write_docx(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'
    with zipfile.ZipFile(path, "w") as archive:
        # 本分支：包结构写全，网关的转换器才打得开（ADR-0093）
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", RELS)
        archive.writestr("word/document.xml", document)
    # 本分支：资料区里的 Word 由多源连接器转成带原件锚点的文本版，主服务只读它（ADR-0076、ADR-0093）；
    # 测试里没有网关，用同一个转换器写文本版
    parts = path.parts
    if "resource" in parts:
        at = len(parts) - 1 - parts[::-1].index("resource")
        samples.write_text_version(Path(*parts[:at]), "/".join(parts[at:]))
    return path


CONTENT_TYPES = ('<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                 '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                 '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument'
                 '.wordprocessingml.document.main+xml"/></Types>')
RELS = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
        'officeDocument" Target="word/document.xml"/></Relationships>')


@pytest.fixture()
def docx(tmp_path: Path) -> Path:
    # 空段也占段号：第 2 段为空，所以「合成规则正文」是 p:3
    body = (
        _paragraph("合成资料标题")
        + _paragraph("")
        + _paragraph("合成规则正文 R-01")
        + _table([["编号", "说明"], ["R-02", "合成表格规则"]])
        + _paragraph("结尾段落")
    )
    return write_docx(tmp_path / "resource" / "others" / "合成规则.docx", body)


def test_docx_segments_keep_source_extract_anchors(docx: Path):
    result = source_preview.preview(docx, "p:3")
    assert result["status"] == "ok"
    assert result["anchor_found"] is True
    anchors = [segment["anchor"] for segment in result["segments"]]
    assert anchors == [
        "p:1",
        "p:3",
        "table:1/row:1/cell:1",
        "table:1/row:1/cell:2",
        "table:1/row:2/cell:1",
        "table:1/row:2/cell:2",
        "p:4",
    ]
    assert source_preview.preview(docx, "table:1/row:2/cell:2")["anchor_found"] is True


def test_missing_anchor_reports_and_starts_from_top(docx: Path):
    result = source_preview.preview(docx, "p:99")
    assert result["anchor_found"] is False
    assert result["offset"] == 0
    assert result["segments"][0]["anchor"] == "p:1"


def test_no_anchor_means_no_location_check(docx: Path):
    assert source_preview.preview(docx)["anchor_found"] is None


def test_text_lines_and_window_around_anchor(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(source_preview, "WINDOW", 5)
    monkeypatch.setattr(source_preview, "CONTEXT_BEFORE", 2)
    text = tmp_path / "说明.md"
    text.write_text("\n".join(f"合成第 {n} 行" for n in range(1, 21)), encoding="utf-8")
    result = source_preview.preview(text, "L12")
    assert [segment["anchor"] for segment in result["segments"]] == ["L10", "L11", "L12", "L13", "L14"]
    assert result["has_more_before"] and result["has_more_after"]
    assert result["total"] == 20


def test_long_segment_is_truncated(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(source_preview, "MAX_TEXT", 10)
    text = tmp_path / "长段.txt"
    text.write_text("合" * 30, encoding="utf-8")
    segment = source_preview.preview(text, "L1")["segments"][0]
    assert segment["text"] == "合" * 10 and segment["truncated"] is True


def test_oversized_file_is_unavailable(docx: Path, monkeypatch):
    monkeypatch.setattr(source_preview.source_extract, "MAX_BYTES", 10)
    result = source_preview.preview(docx, "p:3")
    assert result["status"] == "unavailable"
    assert "下载原文件" in result["reason"]


def test_unsupported_format_is_rejected(tmp_path: Path):
    target = tmp_path / "手册.pdf"
    target.write_bytes(b"%PDF-1.4 synthetic")
    with pytest.raises(source_preview.SourcePreviewError) as exc:
        source_preview.preview(target)
    assert exc.value.status == 415


def test_broken_docx_is_unavailable_not_error(tmp_path: Path):
    target = tmp_path / "损坏.docx"
    target.write_bytes(b"not a zip")
    result = source_preview.preview(target, "p:1")
    assert result["status"] == "unavailable"
    assert result["reason"].startswith("无法预览")


def test_route_previews_and_reports_missing_file(tmp_path: Path, monkeypatch):
    workspaces = WorkspaceManager(tmp_path / "data", tmp_path / "assets")
    monkeypatch.setattr(deps, "workspaces", workspaces)
    monkeypatch.setattr(auth, "ENABLED", False)
    meta = workspaces.create_project("synthetic-source")
    root = workspaces.project_root(meta["id"])
    write_docx(root / "resource" / "others" / "合成规则.docx", _paragraph("合成规则正文"))
    client = TestClient(app)

    ok = client.get(
        f"/api/v1/projects/{meta['id']}/resources/source-preview",
        params={"path": "resource/others/合成规则.docx", "anchor": "p:1"},
    )
    assert ok.status_code == 200
    assert ok.json()["anchor_found"] is True

    missing = client.get(
        f"/api/v1/projects/{meta['id']}/resources/source-preview",
        params={"path": "resource/others/已删除.docx", "anchor": "p:1"},
    )
    assert missing.status_code == 404


def test_rule_schema_matches_template_and_accepts_source_section(tmp_path: Path):
    template = Path(config.ASSETS_DIR) / "workspace-toolkit" / "templates" / "rule-template.md"
    headings = [section["title"] for section in studio.parse_sections(template.read_text(encoding="utf-8"))]
    assert headings == [spec["title"] for spec in studio.RULE_SCHEMA]

    content = (
        "# 规则描述\n\n合成规则\n\n# 适用对象\n\n- [[合成对象]]：金额\n\n"
        "# 约束表达\n\n金额 > 0\n\n# 来源\n\n资料分析报告的规则ID R-01\n\n"
        "# 原始资料来源\n\n- `resource/others/合成规则.docx#p:3`（规则ID R-01）\n"
    )
    checks = studio.deterministic_check(tmp_path, "ontology/rule/合成规则.md", content)
    assert not any("模板外" in item for item in checks["review"])
