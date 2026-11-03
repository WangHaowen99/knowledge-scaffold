"""F4d：Studio 填证据改成「先选资料、再选位置」，位置来自 R-15（只读文本，ADR-0076）。

选出的锚点要经 EV-29 解析得回同一行、同一个位置说法；PDF、Word、Excel 只列文本版，不碰原件。测试数据全部为合成内容。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.evidence import convert
from app.main import app
from app.services import auth, material_text
from app.services.workspace import WorkspaceManager
from app.services.workspace.layout import _content_revision
from tests.test_i1_material_text import docx

NOTES_MD = "# 合成配送说明\n\n门店按周补货。\n\n## 冷链\n\n- 冷藏品单独配送\n\n| 品类 | 到货周期 |\n| --- | --- |\n| 牛奶 | 隔日 |\n"
PDF_TEXT = "\n".join([
    convert.page_marker(1), "退款申请须在签收后 7 天内提交。", "",
    convert.page_marker(2), "## 审批", "单笔超过 500 元由主管审批。", "<!-- 备注 -->", "| 金额 | 审批人 |", "| --- | --- |",
    "| 500 以上 | 主管 |",
])
PDF = "resource/others/03-退款规范.pdf"
PDF_TEXT_PATH = "resource/text/others/03-退款规范.pdf.md"


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(auth, "ENABLED", False)
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    workspaces = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", workspaces)
    with TestClient(app) as client:
        project_id = workspaces.create_project("选出处", owner="employee_0001")["id"]
        base = f"/api/v1/projects/{project_id}"
        pdf = b"%PDF-1.4 synthetic refund"
        for name, data in (("01-配送说明.md", NOTES_MD.encode()), ("02-门店清单.csv", "门店,区域\nS001,一区\n".encode()),
                           ("03-退款规范.pdf", pdf), ("04-补货规则.docx", docx(["补货规则", "隔日到货按 1.5 倍备货。"]))):
            response = client.post(f"{base}/resources", files={"file": (name, data)})
            assert response.status_code == 201, response.text
        root = workspaces.project_root(project_id)
        # 网关转出的文本版：第一行是内核写的文件头，行号按整个文件数
        text_file = root / PDF_TEXT_PATH
        text_file.parent.mkdir(parents=True, exist_ok=True)
        text_file.write_text(convert.header("03-退款规范.pdf", _content_revision(pdf), "fake@1") + "\n" + PDF_TEXT + "\n",
                             encoding="utf-8")
        yield client, root, base


def _positions(client, base, path, **params):
    response = client.get(f"{base}/resources/positions", params={"path": path, **params})
    assert response.status_code == 200, response.text
    return response.json()


def test_md_lists_lines_under_headings_and_skips_headings_and_table_rules(env):
    client, _root, base = env
    data = _positions(client, base, "resource/others/01-配送说明.md")
    assert data["path"] == "resource/others/01-配送说明.md" and data["name"] == "01-配送说明" and data["readable"]
    assert [(item["anchor"], item["location"], item["text"]) for item in data["items"]] == [
        ("file:resource/others/01-配送说明.md#L3", "合成配送说明", "门店按周补货。"),
        ("file:resource/others/01-配送说明.md#L7", "冷链", "冷藏品单独配送"),
        ("file:resource/others/01-配送说明.md#L9", "冷链", "品类 · 到货周期"),
        ("file:resource/others/01-配送说明.md#L11", "冷链", "牛奶 · 隔日"),
    ]
    assert data["next_cursor"] is None


def test_pdf_lists_text_version_lines_by_page(env):
    client, _root, base = env
    data = _positions(client, base, PDF)
    assert data["name"] == "03-退款规范" and data["readable"]
    items = [(item["anchor"], item["location"], item["text"]) for item in data["items"]]
    # 文件头是第 1 行，位置标记、标题、注释、分隔行不列
    assert items == [
        (f"file:{PDF_TEXT_PATH}#L3", "第 1 页", "退款申请须在签收后 7 天内提交。"),
        (f"file:{PDF_TEXT_PATH}#L7", "第 2 页", "单笔超过 500 元由主管审批。"),
        (f"file:{PDF_TEXT_PATH}#L9", "第 2 页", "金额 · 审批人"),
        (f"file:{PDF_TEXT_PATH}#L11", "第 2 页", "500 以上 · 主管"),
    ]


def test_every_position_resolves_back_to_the_same_line_and_location(env):
    client, root, base = env
    for path in ("resource/others/01-配送说明.md", "resource/excel/02-门店清单.csv", PDF):
        for item in _positions(client, base, path)["items"]:
            resolved = material_text.resolve(root, item["anchor"])
            assert resolved["resolved"], item
            assert resolved["location_label"] == item["location"], item
            assert resolved["material"]["path"] == path
            hit = resolved["text"][resolved["highlights"][0]["start"]:resolved["highlights"][0]["end"]]
            assert all(part in hit for part in item["text"].split(" · ")), (item, hit)


def test_document_without_text_version_is_not_readable_and_original_is_never_parsed(env, monkeypatch):
    client, root, base = env
    calls = []
    original = material_text.extract
    monkeypatch.setattr(material_text, "extract", lambda root, rel: calls.append(rel) or original(root, rel))
    data = _positions(client, base, "resource/others/04-补货规则.docx")
    assert data == {"path": "resource/others/04-补货规则.docx", "name": "04-补货规则", "readable": False, "items": [],
                    "next_cursor": None}
    _positions(client, base, PDF)
    assert calls == [PDF_TEXT_PATH]
    # 原文件换了、文本版还是旧的：不列旧行
    (root / PDF).write_bytes(b"%PDF-1.4 replaced")
    assert _positions(client, base, PDF)["readable"] is False


def test_search_paging_and_errors(env):
    client, _root, base = env
    assert [item["anchor"] for item in _positions(client, base, PDF, q="主管")["items"]] == [
        f"file:{PDF_TEXT_PATH}#L7", f"file:{PDF_TEXT_PATH}#L11"]
    assert len(_positions(client, base, PDF, q="第 1 页")["items"]) == 1
    assert _positions(client, base, PDF, q="不存在的字")["items"] == []
    first = _positions(client, base, PDF, limit=3)
    assert len(first["items"]) == 3 and first["next_cursor"]
    rest = _positions(client, base, PDF, limit=3, cursor=first["next_cursor"])
    assert [item["anchor"] for item in rest["items"]] == [f"file:{PDF_TEXT_PATH}#L11"] and rest["next_cursor"] is None
    bad = client.get(f"{base}/resources/positions", params={"path": PDF, "cursor": "zzz"})
    assert bad.status_code == 400 and bad.json()["code"] == "invalid_cursor"
    # 文本版本身、资料区外、不存在的资料
    assert client.get(f"{base}/resources/positions", params={"path": PDF_TEXT_PATH}).status_code == 403
    assert client.get(f"{base}/resources/positions", params={"path": "ontology/objects/a.md"}).status_code == 403
    assert client.get(f"{base}/resources/positions", params={"path": "resource/others/无.md"}).status_code == 404
    assert client.get(f"{base}/resources/positions", params={}).status_code == 400
