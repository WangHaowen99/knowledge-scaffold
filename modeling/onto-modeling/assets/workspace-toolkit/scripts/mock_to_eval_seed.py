#!/usr/bin/env python3
"""mock 示例数据 → 评测库导入种子（Excel 工作簿）。

方案 docs/eval-optimization-integration.md P1：没有真实业务数据时，
`ontology/data-mock/` 的示例数据（本就按对象属性生成）直接转为评测数据库
的导入种子，零额外准备即可跑通评测链。

转换规则：每个 data-mock/*.md 的首个 markdown 表格 → 工作簿的一个 Sheet
（Sheet 名 = 文件名，即对象名），表头与数据行原样搬运。

用法（工作区根目录执行）：
  python3 .claude/scripts/mock_to_eval_seed.py [--out tmp/eval-seed.xlsx]
"""

from __future__ import annotations

import sys
from pathlib import Path

MOCK_DIR = "ontology/data-mock"
DEFAULT_OUT = "tmp/eval-seed.xlsx"


def parse_first_table(text: str) -> tuple[list[str], list[list[str]]]:
    """取文档中第一个 markdown 表格 → (表头, 数据行)。"""
    lines = text.splitlines()
    headers: list[str] = []
    rows: list[list[str]] = []
    in_table = False
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            if in_table:
                break
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if not in_table:
            headers = cells
            in_table = True
            continue
        if all(set(c) <= set("-: ") for c in cells):
            continue  # 分隔行
        rows.append(cells)
    return headers, rows


def main() -> int:
    try:
        import openpyxl
    except ImportError:
        print("需要 openpyxl 生成 Excel（pip install openpyxl）", file=sys.stderr)
        return 2

    out = DEFAULT_OUT
    if "--out" in sys.argv:
        out = sys.argv[sys.argv.index("--out") + 1]
    root = Path.cwd()
    mock_dir = root / MOCK_DIR
    if not mock_dir.is_dir():
        print(f"{MOCK_DIR}/ 不存在——先运行示例数据阶段（/ontology-mock）", file=sys.stderr)
        return 2

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    converted = 0
    for doc in sorted(mock_dir.glob("*.md")):
        if doc.name == "index.md":
            continue
        headers, rows = parse_first_table(doc.read_text(encoding="utf-8"))
        if not headers or not rows:
            print(f"跳过 {doc.name}（无有效表格）")
            continue
        ws = wb.create_sheet(title=doc.stem[:31])  # Excel Sheet 名上限 31 字符
        ws.append(headers)
        for row in rows:
            ws.append(row[: len(headers)])
        converted += 1
        print(f"✅ {doc.stem}：{len(headers)} 列 × {len(rows)} 行")

    if not converted:
        print("没有可转换的 mock 表格", file=sys.stderr)
        return 1
    out_path = root / out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    print(f"\n评测种子已生成：{out}（{converted} 个 Sheet）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
