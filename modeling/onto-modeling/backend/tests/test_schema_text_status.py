"""数据 schema 自动处理（工作区脚本侧）：

- 已标为 schema、还没有文本版的 Excel 单独列出（status + missing_text），不当作「没有表」，也不再提示去标 schema；
- 全部表头写进 `.ontology-state/table-headers.json`，子代理按路径读，不经主助手转述；
- 三方对账的用法写表名或文本版路径（守卫不放行 Excel 原件路径）；
- 「# 数据来源」是「无」或没写表名、属性却与某张 schema 表对得上几列的对象，给出候选表（只提示）。

测试数据全部为合成内容。
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import sys
from pathlib import Path

import pytest

from app import config
from app.services.engines.jiuwen_assets import run_hooks_sync
from tests.test_jiuwen_assets import _git_workspace, make_loader

SCRIPTS = Path(config.ASSETS_DIR) / "workspace-toolkit" / "scripts"
TEXT_HEADER = '<!-- onto:text source="{}" sha256="0000000000000000" converter="convert-xlsx@1.1.0" -->\n'
ORDERS = "<!-- 工作表：订单 -->\n## 订单\n\n| ORDER_ID | AMOUNT | NOTE |\n|---|---|---|\n| 1 | 20 | 甲 |\n| 2 | 35 | 乙 |\n"


@pytest.fixture()
def scripts(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    monkeypatch.setitem(sys.modules, "openpyxl", None)  # 不许解析原件：一 import 就失败
    return importlib.import_module("table_profile"), importlib.import_module("field_semantics"), \
        importlib.import_module("ontology_checker")


def schema_workspace(root: Path, *, texts: dict[str, str | None]) -> Path:
    """resource/excel 下的合成 Excel 全部标为 schema；texts[文件名] 为 None 时没有文本版。"""
    (root / "resource" / "excel").mkdir(parents=True, exist_ok=True)
    for name, body in texts.items():
        (root / "resource" / "excel" / name).write_bytes(b"PK synthetic")
        if body is not None:
            text = root / "resource" / "text" / "excel" / f"{name}.md"
            text.parent.mkdir(parents=True, exist_ok=True)
            text.write_text(TEXT_HEADER.format(name) + body, encoding="utf-8")
    (root / "resource" / "materials.json").write_text(json.dumps(
        {f"resource/excel/{name}": {"purpose": "schema"} for name in texts}, ensure_ascii=False), encoding="utf-8")
    (root / "ontology" / "object").mkdir(parents=True, exist_ok=True)
    return root


def run_cli(module, root: Path, monkeypatch, capsys, *argv) -> dict:
    monkeypatch.chdir(root)
    monkeypatch.setattr("sys.argv", ["table_profile.py", *argv])
    assert module.main() == 0
    return json.loads(capsys.readouterr().out)


# ---- 还没有文本版：单独列出，不是「没有表」----


def test_headers_and_profile_list_schema_materials_without_text(scripts, tmp_path, monkeypatch, capsys):
    table_profile, _, _ = scripts
    root = schema_workspace(tmp_path, texts={"订单表.xlsx": ORDERS, "宽表.xlsx": None})
    summary = run_cli(table_profile, root, monkeypatch, capsys, "--headers")
    assert summary == {"status": "text_missing", "file": ".ontology-state/table-headers.json",
                       "tables": {"订单": 3}, "tables_omitted": 0, "missing_text": ["resource/excel/宽表.xlsx"]}
    saved = (root / ".ontology-state" / "table-headers.json").read_text(encoding="utf-8")
    # 一列一行：读文件的工具按行截断，宽表也不会丢列
    assert '\n   "ORDER_ID",\n' in saved
    assert json.loads(saved) == {"tables": {"订单": ["ORDER_ID", "AMOUNT", "NOTE"]},
                                 "missing_text": ["resource/excel/宽表.xlsx"]}
    profile = run_cli(table_profile, root, monkeypatch, capsys, "--profile")
    assert profile["status"] == "text_missing" and profile["missing_text"] == ["resource/excel/宽表.xlsx"]
    assert profile["tables"]["订单"]["rows"] == 2


def test_headers_without_any_schema_material_say_no_schema(scripts, tmp_path, monkeypatch, capsys):
    table_profile, _, _ = scripts
    (tmp_path / "resource").mkdir()
    summary = run_cli(table_profile, tmp_path, monkeypatch, capsys, "--headers")
    assert (summary["status"], summary["tables"], summary["missing_text"]) == ("no_schema", {}, [])
    assert json.loads((tmp_path / ".ontology-state" / "table-headers.json").read_text(encoding="utf-8")) == {
        "tables": {}, "missing_text": []}


def test_headers_summary_stays_small_for_many_wide_tables(scripts, tmp_path, monkeypatch, capsys):
    """表头全部进文件；stdout 只有表名与列数，最多 200 张。"""
    table_profile, _, _ = scripts
    columns = " | ".join(f"C{n:03d}" for n in range(300))
    def body(numbers):
        return "".join(f"<!-- 工作表：T{n:03d} -->\n## T{n:03d}\n\n| {columns} |\n|{'---|' * 300}\n" for n in numbers)
    # 一个工作簿最多 200 个 Sheet（与 develop 一致，多了要拆分）：250 张表放在两个工作簿里
    root = schema_workspace(tmp_path, texts={"宽表一.xlsx": body(range(125)), "宽表二.xlsx": body(range(125, 250))})
    monkeypatch.chdir(root)
    monkeypatch.setattr("sys.argv", ["table_profile.py", "--headers"])
    assert table_profile.main() == 0
    out = capsys.readouterr().out
    summary = json.loads(out)
    assert len(summary["tables"]) == 200 and summary["tables_omitted"] == 50 and summary["tables"]["T000"] == 300
    assert len(out) < 20_000
    saved = json.loads((root / ".ontology-state" / "table-headers.json").read_text(encoding="utf-8"))
    assert len(saved["tables"]) == 250 and len(saved["tables"]["T249"]) == 300


def test_types_report_lists_every_material_without_text(scripts, tmp_path):
    _, field_semantics, _ = scripts
    root = schema_workspace(tmp_path, texts={"订单表.xlsx": ORDERS, "宽表.xlsx": None, "明细表.xlsx": None})
    report, code = field_semantics.types_report(root)
    assert (report["status"], code) == ("unverifiable", 2)
    assert report["missing_text"] == ["resource/excel/宽表.xlsx", "resource/excel/明细表.xlsx"]
    assert "2 份 schema 资料还没有文本版" in report["reason"] and "明细表.xlsx" in report["reason"]


def test_types_report_says_when_there_is_no_schema(scripts, tmp_path):
    _, field_semantics, _ = scripts
    (tmp_path / "ontology" / "object").mkdir(parents=True)
    report, _ = field_semantics.types_report(tmp_path)
    assert report["schema"] == "no_schema" and "missing_text" not in report


OBJECT = """# 对象描述
合成订单。

# 英文名称
Order

# 数据来源
{source}

# 对象属性清单
| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |
| --- | --- | --- | --- | --- | --- | --- |
{rows}
"""
KEY = "| 订单号 | ORDER_ID | 合成订单号 | 是 | 整数 | 无 | 无 |"
AMOUNT = "| 金额 | AMOUNT | 合成金额 | 否 | 整数 | 无 | 无 |"


def checker_issues(checker, root: Path, docs: dict[str, tuple[str, list[str]]], category: str) -> list[dict]:
    for name, (source, rows) in docs.items():
        (root / "ontology" / "object" / f"{name}.md").write_text(
            OBJECT.format(source=source, rows="\n".join(rows)), encoding="utf-8")
    issues = [dataclasses.asdict(issue) for issue in checker.run_checks(root / "ontology")]
    return [issue for issue in issues if issue["category"] == category]


def test_declared_table_while_schema_has_no_text_does_not_ask_to_mark_schema(scripts, tmp_path):
    _, _, checker = scripts
    root = schema_workspace(tmp_path, texts={"宽表.xlsx": None})
    issues = checker_issues(checker, root, {"订单": ("订单", [KEY])}, "列名对账")
    assert [issue["severity"] for issue in issues] == ["警告"]
    assert "宽表.xlsx还没有文本版" in issues[0]["message"] and "标为 schema" not in issues[0]["message"]


def test_unbound_object_overlapping_a_schema_table_gets_candidate_warning(scripts, tmp_path):
    _, _, checker = scripts
    root = schema_workspace(tmp_path, texts={"订单表.xlsx": ORDERS})
    issues = checker_issues(checker, root, {
        "订单": ("无", [KEY, AMOUNT]),
        "订单汇总": ("", [KEY, AMOUNT, "| 备注 | note | 合成备注 | 否 | 字符串 | 无 | 无 |"]),
    }, "数据来源候选")
    by_file = {issue["file"]: issue for issue in issues}
    assert set(by_file) == {"object/订单.md", "object/订单汇总.md"}
    assert all(issue["severity"] == "警告" for issue in issues)
    assert "写的是「无」" in by_file["object/订单.md"]["message"]
    assert "订单（2 列同名：ORDER_ID、AMOUNT）" in by_file["object/订单.md"]["message"]
    # 不分大小写，提示里写对象自己的英文名
    assert "没有写表名" in by_file["object/订单汇总.md"]["message"]
    assert "订单（3 列同名：ORDER_ID、AMOUNT、note）" in by_file["object/订单汇总.md"]["message"]


@pytest.mark.parametrize("source, rows", [
    ("订单", [KEY, AMOUNT]),                                                    # 已经绑了表
    ("无", [KEY, "| 说明 | REMARK | 合成说明 | 否 | 字符串 | 无 | 无 |"]),         # 只对上一列
    ("无", ["| 批次号 | BATCH_ID | 合成批次 | 是 | 字符串 | 无 | 无 |", "| 订单号 | ORDER_ID | 合成 | 否 | 整数 | 无 | 无 |",
            AMOUNT]),                                                           # 主键不在表里
    ("无", [KEY, "| 金额 | AMOUNT | 合成金额 | 否 | 整数 | 无 | 派生：由 [[计算金额]] 写入 |"]),  # 派生属性不算
])
def test_no_candidate_warning_without_real_overlap(scripts, tmp_path, source, rows):
    _, _, checker = scripts
    root = schema_workspace(tmp_path, texts={"订单表.xlsx": ORDERS})
    assert checker_issues(checker, root, {"订单": (source, rows)}, "数据来源候选") == []


# ---- 用法与守卫一致：写表名或文本版路径 ----


@pytest.mark.parametrize("command, allowed", [
    ("python3 .claude/scripts/table_profile.py --json 订单", True),
    ("python3 .claude/scripts/table_profile.py --json resource/text/excel/订单表.xlsx.md", True),
    ("python3 .claude/scripts/table_profile.py --json resource/excel/订单表.xlsx", False),
])
def test_reconcile_usage_matches_the_original_read_guard(tmp_path, command, allowed):
    entries = make_loader().pre_tool_use_hooks()
    ws = _git_workspace(tmp_path)
    schema_workspace(ws, texts={"订单表.xlsx": ORDERS})
    reason = run_hooks_sync(entries, "bash", {"command": command}, ws)
    assert (reason is None) is allowed, reason
    usage = (SCRIPTS / "table_profile.py").read_text(encoding="utf-8").split('"""')[1]
    assert "resource/excel/" not in usage
