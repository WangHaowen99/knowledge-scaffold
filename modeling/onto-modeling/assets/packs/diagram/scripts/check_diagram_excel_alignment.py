#!/usr/bin/env python3
"""Check alignment between split drawio flowcharts and Excel supplements.

Usage:
    python3 .claude/scripts/check_diagram_excel_alignment.py
    python3 .claude/scripts/check_diagram_excel_alignment.py --diagrams ontology/diagrams --excel ontology/excel
"""

from __future__ import annotations

import argparse
import html
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from openpyxl import load_workbook

REPORT_TZ = ZoneInfo("Asia/Shanghai")


DEFAULT_DIAGRAM_DIR = Path("ontology/diagrams")
DEFAULT_EXCEL_DIR = Path("ontology/excel")
DEFAULT_REPORTS_DIR = Path("reports")
# 资料区里的图例（存量项目建项目时从资料区模板复制来的，模板里已去掉），有就用它；没有用随 core 包的
DEFAULT_LEGEND = Path("resource/diagrams/图示说明定义.drawio")
PACKAGED_LEGEND = Path(os.path.realpath(__file__)).parent.parent / "reference" / DEFAULT_LEGEND.name


def find_legend(root: Path | str = ".") -> Path:
    own = Path(root) / DEFAULT_LEGEND
    return own if own.is_file() else PACKAGED_LEGEND


def _clean_text(value: object) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _logical_path(path: Path) -> str:
    parts = path.parts
    for marker in ("ontology", "reports"):
        if marker in parts:
            return str(Path(*parts[parts.index(marker):]))
    return str(path)


def _unique(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _activity_key(name: str) -> str:
    return re.sub(r"\s+", "", name)


def _style_tokens(style: str) -> set[str]:
    return {
        token.strip()
        for token in (style or "").split(";")
        if token.strip() and token.strip() not in {"html=1", "whiteSpace=wrap"}
    }


def _virtual_style_signatures(legend_path: Path | None = None) -> list[set[str]]:
    legend_path = Path(legend_path) if legend_path else find_legend()
    if not legend_path.is_file():
        return []

    try:
        root = ET.parse(legend_path).getroot()
    except Exception:
        return []

    cells = {cell.get("id"): cell for cell in root.iter("mxCell") if cell.get("id")}
    signatures = []
    for cell in cells.values():
        if cell.get("edge") != "1":
            continue
        source = cells.get(cell.get("source"))
        target = cells.get(cell.get("target"))
        if source is None or target is None:
            continue
        if "虚拟项" not in _clean_text(target.get("value", "")):
            continue
        signature = _style_tokens(source.get("style", "") or "")
        if signature:
            signatures.append(signature)
    return signatures


def _is_virtual_node(style: str, virtual_signatures: list[set[str]]) -> bool:
    tokens = _style_tokens(style)
    return any(signature <= tokens for signature in virtual_signatures)


def _classify_drawio_node(style: str) -> str | None:
    if "text;" in style or "edgeLabel" in style:
        return None
    if "mxgraph.flowchart.terminator" in style:
        return "终止"
    if "shape=process" in style:
        return "子流程"
    if "rhombus" in style and "double=1" in style:
        return "子流程判断"
    if "rhombus" in style:
        return "判断"
    if "shape=card" in style:
        return "动作"
    if "shape=tape" in style:
        return "说明"
    if "shape=hexagon" in style:
        return "递归"
    if "swimlane" in style:
        return "循环"
    if "rounded=1" in style:
        return "业务活动"
    if "whiteSpace=wrap" in style and "shape=" not in style:
        return "业务活动"
    return None


def read_drawio_comparable_nodes(path: Path) -> list[str]:
    root = ET.parse(path).getroot()
    virtual_signatures = _virtual_style_signatures()
    nodes = []
    for cell in root.iter("mxCell"):
        if cell.get("vertex") != "1":
            continue
        style = cell.get("style", "") or ""
        if not _classify_drawio_node(style):
            continue
        if _is_virtual_node(style, virtual_signatures):
            continue
        label = _clean_text(cell.get("value", ""))
        if label:
            nodes.append(label)
    return _unique(nodes)


def read_excel_business_activities(path: Path) -> tuple[list[str], bool]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        worksheet = workbook[workbook.sheetnames[0]]
        header_row_index = None
        activity_col = None
        for row_index, row in enumerate(
            worksheet.iter_rows(min_row=1, max_row=min(10, worksheet.max_row), values_only=True),
            start=1,
        ):
            headers = [_clean_text(cell) for cell in row]
            if "业务活动" in headers:
                header_row_index = row_index
                activity_col = headers.index("业务活动")
                break
        if header_row_index is None or activity_col is None:
            return [], False

        activities = []
        for row in worksheet.iter_rows(min_row=header_row_index + 1, values_only=True):
            if activity_col >= len(row):
                continue
            activity = _clean_text(row[activity_col])
            if activity:
                activities.append(activity)
        return _unique(activities), True
    finally:
        workbook.close()


def _issue(scope: str, category: str, message: str) -> dict[str, str]:
    return {"scope": scope, "category": category, "message": message}


def check_alignment(diagrams_dir: Path | str = DEFAULT_DIAGRAM_DIR, excel_dir: Path | str = DEFAULT_EXCEL_DIR) -> dict:
    diagrams_dir = Path(diagrams_dir)
    excel_dir = Path(excel_dir)
    diagram_files = sorted(diagrams_dir.glob("*.drawio")) if diagrams_dir.is_dir() else []
    excel_files = sorted(path for path in excel_dir.glob("*.xlsx") if not path.name.startswith("~$")) if excel_dir.is_dir() else []

    if not excel_files:
        return {
            "skipped": True,
            "reason": f"未发现 Excel 补充表：{_logical_path(excel_dir)}",
            "paired_count": 0,
            "issue_count": 0,
            "issues": [],
        }

    diagrams_by_stem = {path.stem: path for path in diagram_files}
    excels_by_stem = {path.stem: path for path in excel_files}
    issues = []

    for stem, diagram_path in diagrams_by_stem.items():
        if stem not in excels_by_stem:
            expected = Path("ontology/excel") / f"{stem}.xlsx"
            issues.append(_issue(stem, "名称不对应", f"流程图缺少对应 Excel：{diagram_path.name} -> {expected}"))

    for stem, excel_path in excels_by_stem.items():
        if stem not in diagrams_by_stem:
            expected = Path("ontology/diagrams") / f"{stem}.drawio"
            issues.append(_issue(stem, "名称不对应", f"Excel 缺少对应流程图：{excel_path.name} -> {expected}"))

    for stem in sorted(set(diagrams_by_stem) & set(excels_by_stem)):
        diagram_path = diagrams_by_stem[stem]
        excel_path = excels_by_stem[stem]
        drawio_nodes = read_drawio_comparable_nodes(diagram_path)
        excel_activities, has_column = read_excel_business_activities(excel_path)
        if not has_column:
            issues.append(_issue(stem, "缺少业务活动列", f"Excel 缺少「业务活动」列：{_logical_path(excel_path)}"))
            continue

        drawio_set = {_activity_key(name) for name in drawio_nodes}
        excel_set = {_activity_key(name) for name in excel_activities}
        missing_in_excel = [name for name in drawio_nodes if _activity_key(name) not in excel_set]
        missing_in_drawio = [name for name in excel_activities if _activity_key(name) not in drawio_set]
        if missing_in_excel:
            issues.append(_issue(
                stem,
                "节点不对应",
                "drawio 中的非虚拟节点未出现在 Excel「业务活动」列：" + "、".join(missing_in_excel),
            ))
        if missing_in_drawio:
            issues.append(_issue(
                stem,
                "节点不对应",
                "Excel「业务活动」列中的节点未出现在 drawio 非虚拟节点：" + "、".join(missing_in_drawio),
            ))

    return {
        "skipped": False,
        "reason": "",
        "paired_count": len(set(diagrams_by_stem) & set(excels_by_stem)),
        "issue_count": len(issues),
        "issues": issues,
    }


def write_report(result: dict, diagrams_dir: Path, excel_dir: Path, reports_dir: Path) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(REPORT_TZ)
    report_path = reports_dir / f"{now.strftime('%Y%m%d-%H%M%S')}-diagrams-excel.md"

    lines = [
        "# 流程图 Excel 对齐检查报告",
        "",
        f"日期：{now.strftime('%Y-%m-%d %H:%M:%S')} Asia/Shanghai",
        f"流程图目录：`{_logical_path(diagrams_dir)}`",
        f"Excel 目录：`{_logical_path(excel_dir)}`",
        "",
        "## 概览",
        "",
        "| 状态 | 数量 |",
        "|------|------|",
        f"| 配对流程 | {result['paired_count']} |",
        f"| 问题 | {result['issue_count']} |",
        "",
    ]

    if result["skipped"]:
        lines.extend(["## 跳过原因", "", result["reason"], ""])
    elif result["issues"]:
        lines.extend(["## 问题详情", ""])
        for issue in result["issues"]:
            lines.append(f"- **[{issue['category']}] {issue['scope']}**：{issue['message']}")
        lines.append("")
    else:
        lines.extend(["## 检查结果", "", "流程图与 Excel 补充表名称、非虚拟节点均一致。", ""])

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def run(
    diagrams_dir: Path | str = DEFAULT_DIAGRAM_DIR,
    excel_dir: Path | str = DEFAULT_EXCEL_DIR,
    reports_dir: Path | str = DEFAULT_REPORTS_DIR,
) -> tuple[Path, dict]:
    diagrams_dir = Path(diagrams_dir)
    excel_dir = Path(excel_dir)
    reports_dir = Path(reports_dir)
    result = check_alignment(diagrams_dir, excel_dir)
    report_path = write_report(result, diagrams_dir, excel_dir, reports_dir)

    if result["skipped"]:
        print(f"Excel 对齐校验已跳过：{result['reason']}")
    elif result["issue_count"]:
        print(f"Excel 对齐校验发现 {result['issue_count']} 个问题")
    else:
        print("Excel 对齐校验通过")
    print(f"报告已保存：{report_path}")
    return report_path, result


def main() -> None:
    parser = argparse.ArgumentParser(description="Check drawio and Excel supplement alignment.")
    parser.add_argument("--diagrams", default=str(DEFAULT_DIAGRAM_DIR), help="Directory containing split .drawio files")
    parser.add_argument("--excel", default=str(DEFAULT_EXCEL_DIR), help="Directory containing split .xlsx files")
    parser.add_argument("--reports", default=str(DEFAULT_REPORTS_DIR), help="Directory for markdown reports")
    args = parser.parse_args()
    run(args.diagrams, args.excel, args.reports)


if __name__ == "__main__":
    main()
