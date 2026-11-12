"""F4c：两个引擎都不在主容器里解析资料原件；schema 表头读 Excel 的文本版；锁恢复不再问人。

原则（用户 2026-11-03 定）：解析不可信文件不在主容器里做（ADR-0073）；内部机制不问人（ADR-0074）。
- 内测引擎跑在后端进程里，它自带的读文件工具遇到 PDF 会用 pdfplumber 在进程内解析：读文件工具也要过守卫；
- 稳定引擎的 Read 同样拦；Bash 里拿解析库、转换命令碰资料原件也拦；
- 对象提取核对字段用的 schema 表头改读文本版里的 Markdown 表；资料分析核验也不再解析原件；
- 命令开头遇到上次没做完的同名命令，由 state_helper 按状态定续做还是重来，不问用户。

测试数据全部为合成内容。
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app import config, kernel_guards
from app.services import package_mapping
from app.services.engines.jiuwen_assets import run_hooks_sync
from tests.proc import run as run_cmd
from tests.test_blueprint import env  # noqa: F401 - 夹具
from tests.test_jiuwen_assets import _git_workspace, make_loader
from tests.test_research_review import make_report, row, toolkit, workspace, write_report  # noqa: F401 - 夹具

SCRIPTS = Path(config.ASSETS_DIR) / "workspace-toolkit" / "scripts"
TEXT_HEADER = '<!-- onto:text source="{}" sha256="0000000000000000" converter="convert-xlsx@1.0.0" -->\n'


def _text_version(root: Path, original: str, body: str) -> str:
    rel = f"resource/text/{original.removeprefix('resource/')}.md"
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEXT_HEADER.format(Path(original).name) + body, encoding="utf-8")
    return rel


def _materials(root: Path) -> None:
    others = root / "resource" / "others"
    others.mkdir(parents=True, exist_ok=True)
    (others / "通知.pdf").write_bytes(b"%PDF-1.4 synthetic")
    (others / "表.xlsx").write_bytes(b"PK synthetic")
    (others / "图.png").write_bytes(b"\x89PNG synthetic")
    (others / "说明.md").write_text("合成说明\n", encoding="utf-8")
    _text_version(root, "resource/others/通知.pdf", "<!-- 第 1 页 -->\n合成条款\n")


# ---------------------------------------------------------------------------
# 1. 引擎读原件被拦（内测引擎经同一套守卫；稳定引擎的 Read 在 settings 基线里）
# ---------------------------------------------------------------------------


def test_reading_binary_originals_is_denied_and_points_to_the_text_version(tmp_path):
    entries = make_loader().pre_tool_use_hooks()
    ws = _git_workspace(tmp_path)
    _materials(ws)
    pdf = run_hooks_sync(entries, "read_file", {"file_path": "resource/others/通知.pdf"}, ws)
    assert pdf and "resource/text/others/通知.pdf.md" in pdf
    xlsx = run_hooks_sync(entries, "read_file", {"file_path": "resource/others/表.xlsx"}, ws)
    assert xlsx and "没有文本版" in xlsx
    assert run_hooks_sync(entries, "read_file", {"file_path": str(ws / "resource/others/图.png")}, ws)
    for allowed in ("resource/others/说明.md", "resource/text/others/通知.pdf.md", "ontology/object/订单.md"):
        assert run_hooks_sync(entries, "read_file", {"file_path": allowed}, ws) is None, allowed


@pytest.mark.parametrize("command", [
    "python3 -c \"import pdfplumber; pdfplumber.open('resource/others/通知.pdf')\"",
    "pdftotext resource/others/通知.pdf -",
    "cat resource/others/表.xlsx | head",
    "cd resource/others && python3 - <<'EOF'\nimport openpyxl\nopenpyxl.load_workbook('表.xlsx')\nEOF",
    "unzip -p resource/others/表.xlsx xl/sharedStrings.xml",
])
def test_bash_parsing_originals_is_denied(tmp_path, command):
    entries = make_loader().pre_tool_use_hooks()
    ws = _git_workspace(tmp_path)
    _materials(ws)
    reason = run_hooks_sync(entries, "bash", {"command": command}, ws)
    assert reason and "文本版" in reason, command


@pytest.mark.parametrize("command", [
    "ls -la resource/others",
    "sha256sum resource/others/通知.pdf",
    "python3 .claude/scripts/source_extract.py --sources",
    "python3 .claude/scripts/source_extract.py resource/text/others/通知.pdf.md",
    "wc -c resource/others/通知.pdf && file resource/others/表.xlsx",
])
def test_bash_listing_or_reading_text_is_allowed(tmp_path, command):
    entries = make_loader().pre_tool_use_hooks()
    ws = _git_workspace(tmp_path)
    _materials(ws)
    assert run_hooks_sync(entries, "bash", {"command": command}, ws) is None, command


def test_stable_engine_read_goes_through_the_same_guard():
    hooks = kernel_guards.settings_base()["hooks"]["PreToolUse"]
    read = [item for item in hooks if "Read" in item["matcher"].split("|")]
    assert read and any("resource_read_guard.sh" in hook["command"] for item in read for hook in item["hooks"])
    bash = [item for item in hooks if item["matcher"] == "Bash"][0]
    assert any("resource_read_guard.sh" in hook["command"] for hook in bash["hooks"])


# ---------------------------------------------------------------------------
# 2. schema 表头、取值画像读 Excel 的文本版
# ---------------------------------------------------------------------------

SHEETS = ("<!-- 工作表：订单 -->\n## 订单\n\n| ORDER_ID | AMOUNT | NOTE |\n|---|---|---|\n"
          "| 1 | 20 | 含\\|竖线 |\n| 2 | 35 | 普通 |\n\n"
          "<!-- 工作表：空表 -->\n## 空表\n\n（空表）\n")


def _schema_workspace(root: Path, *, text: bool = True) -> Path:
    (root / "resource" / "excel").mkdir(parents=True)
    (root / "resource" / "excel" / "订单表.xlsx").write_bytes(b"PK synthetic")
    (root / "resource" / "materials.json").write_text(
        json.dumps({"resource/excel/订单表.xlsx": {"purpose": "schema"}}, ensure_ascii=False), encoding="utf-8")
    if text:
        _text_version(root, "resource/excel/订单表.xlsx", SHEETS)
    return root


@pytest.fixture()
def table_profile(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    monkeypatch.setitem(sys.modules, "openpyxl", None)  # 不许再解析原件：一 import 就失败
    return importlib.import_module("table_profile")


def test_schema_headers_and_profiles_come_from_the_text_version(tmp_path, table_profile):
    root = _schema_workspace(tmp_path)
    assert table_profile.table_headers(root) == {"订单": ["ORDER_ID", "AMOUNT", "NOTE"]}
    tables, profiles = table_profile.load_registered_tables(root)
    assert tables == {"订单": ["ORDER_ID", "AMOUNT", "NOTE"]}
    assert profiles["订单"]["rows"] == 2
    assert table_profile.table_profiles(root)["订单"]["rows"] == 2
    assert package_mapping.read_source_tables(root) == {"订单": ["ORDER_ID", "AMOUNT", "NOTE"]}


def test_schema_without_a_text_version_is_reported_not_parsed(tmp_path, table_profile):
    root = _schema_workspace(tmp_path, text=False)
    assert table_profile.table_headers(root) == {}
    with pytest.raises(ValueError, match="文本版"):
        table_profile.load_registered_tables(root)
    assert package_mapping.read_source_tables(root) == {}


def test_release_tree_carries_the_text_version_of_tables():
    """发布从提交导出只读树再读表头：表格的文本版要跟着导出，PDF 的文本版不用。"""
    from app.services.publish.tree import is_package_source

    assert is_package_source("resource/text/excel/结构.xlsx.md")
    assert not is_package_source("resource/text/others/说明.pdf.md")


# ---------------------------------------------------------------------------
# 资料分析核验：旧报告指向原件的出处按文本版核对，不解析原件
# ---------------------------------------------------------------------------


def test_legacy_anchor_on_an_original_is_checked_against_its_text_version(workspace, toolkit, monkeypatch):
    """原件锚点（#p:1）按文本版里记的原件位置核对（ADR-0093），原件不解析。"""
    import conversion_samples as samples
    from test_review_integrity import docx

    checker, _review = toolkit
    docx(workspace / "resource" / "others" / "规范.docx")
    samples.write_text_version(workspace, "resource/others/规范.docx")

    def no_parse(*_args, **_kwargs):
        raise AssertionError("资料区里的原件不解析")

    monkeypatch.setattr(checker.source_extract, "_extract_docx", no_parse)
    write_report(workspace, make_report([row("ITEM-31", "RULE-31", location="resource/others/规范.docx#p:1")]))
    result = checker.run_check(workspace)
    assert checker.check_exit_code(result) == 0, result["issues"]


def test_legacy_anchor_on_an_original_without_a_text_version_cannot_be_verified(workspace, toolkit):
    checker, review = toolkit
    (workspace / "resource" / "others" / "规范.docx").write_bytes(b"PK synthetic")
    write_report(workspace, make_report([row("ITEM-31", "RULE-31", location="resource/others/规范.docx#p:1")]))
    result = checker.run_check(workspace)
    assert result["status"] == review.UNVERIFIABLE
    assert any("文本版" in issue["message"] for issue in result["issues"])


# ---------------------------------------------------------------------------
# 4. 资料分析给人看的地方不露路径：「资料名 · 位置」
# ---------------------------------------------------------------------------


def test_research_texts_show_material_name_and_place_not_paths(tmp_path):
    from app.services import research_view

    _materials(tmp_path)  # 通知.pdf 的文本版：第 1 行文件头、第 2 行「第 1 页」标记、第 3 行正文
    (tmp_path / "resource" / "others" / "说明.md").write_text("# 受理\n\n超过 30 天不受理\n", encoding="utf-8")
    assert research_view.anchor_label(tmp_path, "resource/text/others/通知.pdf.md", "L3") == "通知 · 第 1 页"
    assert research_view.anchor_label(tmp_path, "resource/others/说明.md", "L3") == "说明 · 受理"
    assert research_view.anchor_label(tmp_path, "resource/others/规范.docx", "p:2") == "规范 · 第 2 段"
    text = "资料 resource/text/others/通知.pdf.md#L3 和 resource/others/说明.md#L3 对不上，见 research/报告.md"
    shown = research_view.humanize(tmp_path, text)
    assert "通知 · 第 1 页" in shown and "说明 · 受理" in shown
    assert "resource/" not in shown and ".md" not in shown and ".pdf" not in shown


def test_blueprint_question_titles_hide_paths(env):
    from tests.test_blueprint import DRAFT, _write_mainline
    from app.services.packs import contracts

    client, _workspaces, _manager, project_id, root = env
    _materials(root)
    question = {"title": "resource/text/others/通知.pdf.md#L3 说的额度是累计还是单笔？",
                "background": "见 resource/others/说明.md", "options": ["累计", "单笔"]}
    _write_mainline(root, contracts.default().load("blueprint").from_draft({**DRAFT, "questions": [question]}))
    got = client.get(f"/api/v1/projects/{project_id}/blueprint").json()["questions"][0]
    assert got["title"].startswith("通知 · 第 1 页") and "resource/" not in got["title"] + got["background"]
    on_disk = json.loads((root / "research" / "blueprint.json").read_text(encoding="utf-8"))["questions"][0]
    assert on_disk["title"].startswith("resource/text/others/通知.pdf.md#L3")  # 助手读的原文不变


# ---------------------------------------------------------------------------
# 3. 锁恢复不问人
# ---------------------------------------------------------------------------


def _helper(root: Path, *args) -> subprocess.CompletedProcess:
    return run_cmd([sys.executable, str(SCRIPTS / "state_helper.py"), *args], cwd=root,
                   capture_output=True, text=True, timeout=30)


def _legacy_lock(root: Path, command: str, phase: str) -> Path:
    import time

    path = root / ".ontology-state" / f"lock-{command}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"command": command, "phase": phase,
                                "started_at": time.strftime("%Y-%m-%d %H:%M:%S")}), encoding="utf-8")
    return path


def test_recover_resumes_a_named_phase_and_restarts_otherwise(tmp_path):
    (tmp_path / "ontology" / "object").mkdir(parents=True)
    lock = _legacy_lock(tmp_path, "extract-object", "objects")
    assert _helper(tmp_path, "check-lock", "extract-object").stdout.strip().startswith("locked:")
    assert _helper(tmp_path, "recover", "extract-object").stdout.strip() == "resume:objects"
    assert json.loads(lock.read_text(encoding="utf-8"))["phase"] == "objects"
    for phase in ("start", "confirm", "unknown"):
        lock = _legacy_lock(tmp_path, "research", phase)
        assert _helper(tmp_path, "recover", "research").stdout.strip() == "restart", phase
        assert not lock.exists()
    assert _helper(tmp_path, "recover", "validate").stdout.strip() == "ok"


def test_no_command_asks_the_user_to_resume_or_clean_up():
    root = Path(config.ASSETS_DIR)
    texts = [root / "workspace-toolkit" / "STEP0_TEMPLATE.md", root / "workspace-toolkit" / "INTERACTION_CONVENTIONS.md",
             *sorted((root / "workspace-toolkit" / "commands").glob("*.md")),
             *sorted(root.glob("packs/*/commands/*.md"))]
    for path in texts:
        text = path.read_text(encoding="utf-8")
        assert "让用户选 **续执**" not in text and "回复 **续执**" not in text and "**清理重来** →" not in text, path
