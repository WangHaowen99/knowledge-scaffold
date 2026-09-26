#!/usr/bin/env python3
"""评测数据三方对账：数据表列 ↔ schema 资料字段 ↔ 对象属性英文名。

用途（方案 docs/eval-optimization-integration.md P1）：
- 用户在评测数据源里上传业务数据表后，在数据**导入评测库之前**暴露结构问题；
- 对账是确定性检查，不经模型。

三方来源：
1. 数据表：评测数据源上传的 Excel（.ontology-state/eval-data/<文件ID>/，或命令行显式指定路径）；
2. schema：materials.json 中 purpose=schema 的表格资料的表头（结构契约）；
3. 对象属性：ontology/object/*.md 的「对象属性清单」表格「英文名称」列。

用法（工作区根目录执行）：
  python3 .claude/scripts/check_eval_data.py [--json] [path/to/xx.xlsx ...]
  python3 .claude/scripts/check_eval_data.py --headers   # 输出 {Sheet 名: [表头列]} JSON，供对象抽取取表名与列名

退出码：0=全部通过（或无可对账项）；1=存在差异；2=数据/依赖错误。
"""

from __future__ import annotations

import csv
import importlib.util
import json
import re
import sys
from pathlib import Path

TABLE_SUFFIXES = (".xlsx", ".xlsm", ".xls", ".csv")
# 评测数据源上传的业务数据表：每个文件一个目录，目录名为文件 ID
EVAL_DATA_DIR = Path(".ontology-state") / "eval-data"


def uploaded_eval_files(root: Path) -> list[str]:
    folder = root / EVAL_DATA_DIR
    if not folder.is_dir():
        return []
    return sorted(
        path.relative_to(root).as_posix()
        for path in folder.glob("*/*")
        if path.is_file() and path.suffix.lower() in (".xlsx", ".xlsm")
    )


def load_directives(root: Path) -> dict:
    path = root / "resource" / "materials.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def read_tables(path: Path) -> dict[str, list[str]]:
    """表格文件 → {表名: [表头列]}。csv 的表名取文件名（去扩展名）。"""
    if path.suffix.lower() == ".csv":
        try:
            with open(path, encoding="utf-8-sig", newline="") as f:
                row = next(csv.reader(f), [])
        except (OSError, UnicodeDecodeError):
            return {}
        headers = [str(c).strip() for c in row if str(c).strip()]
        return {path.stem: headers} if headers else {}
    try:
        import openpyxl  # 容器/后端环境自带；缺失时给出明确指引
    except ImportError:
        print("需要 openpyxl 解析 Excel（pip install openpyxl）", file=sys.stderr)
        sys.exit(2)
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception:
        return {}
    tables = {}
    for ws in wb.worksheets:
        row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), ())
        headers = [str(c).strip() for c in row if c is not None and str(c).strip()]
        if headers:
            tables[ws.title] = headers
    return tables


def table_headers(root: Path) -> dict[str, list[str]]:
    """schema 资料 + 评测数据源上传的数据表 → {表名: 表头列}。

    对象「# 数据来源」的表名与存储属性的英文名称以此为准；同名 Sheet 以先读到的为准。
    """
    directives = load_directives(root)
    schema_files = [p for p, d in directives.items() if isinstance(d, dict) and d.get("purpose") == "schema"]
    # 作为库被 ontology_checker 调用时缺 openpyxl 只跳过 Excel，不能像命令行那样退出进程
    excel_ok = importlib.util.find_spec("openpyxl") is not None
    tables: dict[str, list[str]] = {}
    for rel in schema_files + uploaded_eval_files(root):
        path = root / rel
        if not (str(rel).lower().endswith(TABLE_SUFFIXES) and path.is_file()):
            continue
        if path.suffix.lower() != ".csv" and not excel_ok:
            continue
        for name, headers in read_tables(path).items():
            tables.setdefault(name, headers)
    return tables


def object_attribute_names(root: Path) -> set[str]:
    """全部对象文档「对象属性清单」表格的英文名称集合。"""
    names: set[str] = set()
    obj_dir = root / "ontology" / "object"
    if not obj_dir.is_dir():
        return names
    for doc in obj_dir.glob("*.md"):
        if doc.name == "index.md":
            continue
        try:
            text = doc.read_text(encoding="utf-8")
        except OSError:
            continue
        section = re.search(r"#\s*对象属性清单\s*\n(.*?)(?=\n#\s|\Z)", text, re.S)
        if not section:
            continue
        rows = [line for line in section.group(1).splitlines() if line.strip().startswith("|")]
        if len(rows) < 2:
            continue
        headers = [c.strip() for c in rows[0].strip().strip("|").split("|")]
        try:
            idx = headers.index("英文名称")
        except ValueError:
            continue
        for line in rows[2:]:  # 跳过分隔行
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if idx < len(cells) and cells[idx] and cells[idx] not in ("---", "无"):
                names.add(cells[idx])
    return names


def check_file(root: Path, rel: str, schema_columns: set[str], attr_names: set[str]) -> dict:
    tables = read_tables(root / rel)
    sheets = []
    status = "ok"
    for name, headers in tables.items():
        cols = set(headers)
        data_only = sorted(cols - schema_columns) if schema_columns else []
        attr_unmatched = sorted(cols - attr_names) if attr_names else []
        sheet = {
            "name": name,
            "columns": len(headers),
            "schema_matched": sorted(cols & schema_columns),
            "data_only": data_only,  # 数据里有、schema 没有的列
            "attr_matched": sorted(cols & attr_names),
            "attr_unmatched": attr_unmatched,  # 未映射到任何对象属性的列
        }
        if data_only or attr_unmatched:
            status = "warn"
        sheets.append(sheet)
    if not tables:
        status = "warn"
    return {"path": rel, "status": status, "sheets": sheets}


def main() -> int:
    args = [a for a in sys.argv[1:] if a not in ("--json", "--headers")]
    as_json = "--json" in sys.argv
    root = Path.cwd()
    if "--headers" in sys.argv:
        print(json.dumps(table_headers(root), ensure_ascii=False))
        return 0

    directives = load_directives(root)
    schema_files = [p for p, d in directives.items() if d.get("purpose") == "schema"]
    eval_files = args or uploaded_eval_files(root)

    schema_columns: set[str] = set()
    for rel in schema_files:
        for headers in read_tables(root / rel).values():
            schema_columns.update(headers)
    attr_names = object_attribute_names(root)

    result = {
        "schema_files": schema_files,
        "schema_columns": len(schema_columns),
        "object_attributes": len(attr_names),
        "files": [],
    }
    if not schema_columns and not attr_names:
        result["status"] = "no_schema"
    for rel in eval_files:
        if not str(rel).lower().endswith(TABLE_SUFFIXES) or not (root / rel).exists():
            continue
        result["files"].append(check_file(root, str(rel), schema_columns, attr_names))
    if "status" not in result:
        result["status"] = (
            "warn" if any(f["status"] == "warn" for f in result["files"]) else "ok"
        )

    if as_json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"schema 资料：{len(schema_files)} 份，字段 {len(schema_columns)} 个；"
              f"对象属性 {len(attr_names)} 个")
        for f in result["files"]:
            mark = "✅" if f["status"] == "ok" else "⚠️"
            print(f"\n{mark} {f['path']}")
            for s in f["sheets"]:
                print(f"  表「{s['name']}」{s['columns']} 列："
                      f"schema 命中 {len(s['schema_matched'])}，属性命中 {len(s['attr_matched'])}")
                if s["data_only"]:
                    print(f"    - schema 中不存在的列：{', '.join(s['data_only'])}")
                if s["attr_unmatched"]:
                    print(f"    - 未映射到对象属性的列：{', '.join(s['attr_unmatched'])}")
        if result["status"] == "no_schema":
            print("\n（无 schema 资料与对象属性可对账——先给结构表打 schema 标签或完成对象提取）")
    return 0 if result["status"] == "ok" else (0 if result["status"] == "no_schema" else 1)


if __name__ == "__main__":
    sys.exit(main())
