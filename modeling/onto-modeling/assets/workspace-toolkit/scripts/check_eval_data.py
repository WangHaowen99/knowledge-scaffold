#!/usr/bin/env python3
"""评测数据三方对账：数据表列 ↔ schema 资料字段 ↔ 对象属性英文名。

用途：
- 在对象提取与评测优化时暴露数据表、schema 资料与对象属性之间的结构问题；
- 对账是确定性检查，不经模型。

三方来源：
1. 数据表：旧版评测数据源上传的 Excel（.ontology-state/eval-data/<文件ID>/）、评测优化从独立评测服务导入的
   只有表头的 CSV（.ontology-state/eval-data/eval-service/），或命令行显式指定路径；
2. schema：materials.json 中 purpose=schema 的表格资料的表头（结构契约）；
3. 对象属性：ontology/object/*.md 的「对象属性清单」表格「英文名称」列。

用法（工作区根目录执行）：
  python3 .claude/scripts/check_eval_data.py [--json] [path/to/xx.xlsx ...]
  python3 .claude/scripts/check_eval_data.py --headers   # 输出 {Sheet 名: [表头列]} JSON，供对象抽取取表名与列名
  python3 .claude/scripts/check_eval_data.py --profile [--tables 表名 ...]
      # 输出各列取值画像 JSON（每表最多读 2000 行；只有类别计数与推断类型，不含任何单元格原值），
      # 供对象抽取按取值证据定类型；最多 400 列（约 50KB），其余表名列入 omitted_tables

退出码：0=全部通过（或无可对账项）；1=存在差异；2=数据/依赖错误。
"""

from __future__ import annotations

import csv
import importlib.util
import json
import re
import sys
from pathlib import Path

import field_semantics

TABLE_SUFFIXES = (".xlsx", ".xlsm", ".xls", ".csv")
# 评测数据源上传的业务数据表：每个文件一个目录，目录名为文件 ID
EVAL_DATA_DIR = Path(".ontology-state") / "eval-data"
PROFILE_ROW_LIMIT = field_semantics.PROFILE_ROW_LIMIT
PROFILE_COLUMN_LIMIT = 400
EXCEL_DEPENDENCY_MESSAGE = "需要 openpyxl 解析 Excel（pip install openpyxl），字段核验未完成"


def uploaded_eval_files(root: Path) -> list[str]:
    folder = root / EVAL_DATA_DIR
    if not folder.is_dir():
        return []
    return sorted(
        path.relative_to(root).as_posix()
        for path in folder.glob("*/*")
        if path.is_file() and path.suffix.lower() in (".xlsx", ".xlsm", ".csv")
    )


def load_directives(root: Path) -> dict:
    path = root / "resource" / "materials.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


_spec = importlib.util.spec_from_file_location("onto_check_table_sources", Path(__file__).resolve().with_name("table_sources.py"))
_table_sources = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_table_sources)


def read_tables(path: Path, directive: dict | None = None) -> dict[str, list[str]]:
    return _table_sources.read_selected_headers(path, directive or {"purpose": "schema"})


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
        for name, headers in read_tables(path, directives.get(rel)).items():
            tables.setdefault(name, headers)
    return tables


def read_profiles(path: Path) -> dict[str, dict]:
    """表格文件 → {表名: 取值画像}。每表最多读 PROFILE_ROW_LIMIT 行，画像只含计数、不含原值。

    缺 openpyxl 时抛 ValueError：读不了的表不能当作没有取值证据而放过。
    """
    if path.suffix.lower() == ".csv":
        try:
            with open(path, encoding="utf-8-sig", newline="") as f:
                profile = field_semantics.profile_table(csv.reader(f))
        except (OSError, UnicodeDecodeError, csv.Error):
            return {}
        return {path.stem: profile} if profile else {}
    try:
        import openpyxl
    except ImportError as error:
        raise ValueError(EXCEL_DEPENDENCY_MESSAGE) from error
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception:
        return {}
    profiles = {}
    try:
        for ws in wb.worksheets:
            profile = field_semantics.profile_table(ws.iter_rows(values_only=True))
            if profile:
                profiles[ws.title] = profile
    except Exception:
        return {}
    finally:
        wb.close()
    return profiles


def registered_table_files(root: Path) -> list[str]:
    """schema 资料与评测数据源上传的表格文件（工作区相对路径，去重保序）。"""
    directives = load_directives(root)
    schema_files = [p for p, d in directives.items() if isinstance(d, dict) and d.get("purpose") == "schema"]
    return [rel for rel in dict.fromkeys(schema_files + uploaded_eval_files(root))
            if str(rel).lower().endswith(TABLE_SUFFIXES)]


def _merge_profiles(profiles: dict, found: dict) -> None:
    # 同名表先读到的是只有表头的 schema 资料时，用后读到的带数据的表作取值证据
    for name, profile in found.items():
        current = profiles.get(name)
        if current is None or (not current["rows"] and profile["rows"]):
            profiles[name] = profile


def load_registered_tables(root: Path) -> tuple[dict, dict]:
    """严格读取全部登记表 → ({表名: 表头列}, {表名: 取值画像})。

    登记了却缺失、无法解析、缺解析依赖都抛 ValueError，字段核验不能按「没有表」通过。
    表头同名以先读到的为准（与 table_headers 一致）。
    """
    base = root.resolve()
    directives = load_directives(root)
    tables: dict[str, list[str]] = {}
    profiles: dict[str, dict] = {}
    for rel in registered_table_files(root):
        path = root / rel
        if not path.resolve().is_relative_to(base):
            raise ValueError("核验路径越出工作区")
        if not path.is_file():
            raise ValueError("已登记的数据表不存在，字段核验未完成")
        if path.suffix.lower() != ".csv" and importlib.util.find_spec("openpyxl") is None:
            raise ValueError(EXCEL_DEPENDENCY_MESSAGE)
        directive = directives.get(rel)
        headers = read_tables(path, directive)
        selection = directive.get("schema_selection") if isinstance(directive, dict) else None
        explicitly_empty = (
            isinstance(selection, dict)
            and selection.get("mode") == "manual"
            and selection.get("included_sheets") == []
        )
        if not headers and not explicitly_empty:
            raise ValueError("数据表无法解析或没有表头，字段核验未完成")
        for name, columns in headers.items():
            tables.setdefault(name, columns)
        selected_profiles = {
            name: profile
            for name, profile in read_profiles(path).items()
            if name in headers
        }
        _merge_profiles(profiles, selected_profiles)
    return tables, profiles


def table_profiles(root: Path) -> dict[str, dict]:
    """非严格读取取值画像：与 table_headers 同口径，缺 openpyxl 跳过 Excel，读不了的表跳过。"""
    excel_ok = importlib.util.find_spec("openpyxl") is not None
    profiles: dict[str, dict] = {}
    for rel in registered_table_files(root):
        path = root / rel
        if not path.is_file() or (path.suffix.lower() != ".csv" and not excel_ok):
            continue
        try:
            _merge_profiles(profiles, read_profiles(path))
        except ValueError:
            continue
    return profiles


def _compact_column(column: dict) -> dict:
    return {key: value for key, value in column.items() if value not in (0, "", [], {}, False)}


def profile_summary(profiles: dict, names: list[str]) -> dict:
    """--profile 输出：每列只留非零项；总列数超过 PROFILE_COLUMN_LIMIT 的表名列入 omitted_tables。"""
    selected = sorted(name for name in profiles if not names or name in names)
    result, omitted, budget = {}, [], PROFILE_COLUMN_LIMIT
    for name in selected:
        profile = profiles[name]
        if len(profile["columns"]) > budget:
            omitted.append(name)
            continue
        budget -= len(profile["columns"])
        result[name] = {
            "rows": profile["rows"],
            "truncated": profile["truncated"],
            "skipped_rows": profile["skipped_rows"],
            "columns": {column: _compact_column(stats) for column, stats in profile["columns"].items()},
        }
    return {
        "tables": result,
        "omitted_tables": omitted[:50],
        "missing_tables": [name for name in names if name not in profiles][:50],
        "row_limit": PROFILE_ROW_LIMIT,
    }


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


def _option_values(argv: list[str], option: str) -> list[str]:
    if option not in argv:
        return []
    values = []
    for arg in argv[argv.index(option) + 1:]:
        if arg.startswith("--"):
            break
        values.append(arg)
    return values


def main() -> int:
    args = [a for a in sys.argv[1:] if a not in ("--json", "--headers")]
    as_json = "--json" in sys.argv
    root = Path.cwd()
    if "--headers" in sys.argv:
        print(json.dumps(table_headers(root), ensure_ascii=False))
        return 0
    if "--profile" in sys.argv:
        names = _option_values(sys.argv, "--tables")
        print(json.dumps(profile_summary(table_profiles(root), names), ensure_ascii=False))
        return 0

    directives = load_directives(root)
    schema_files = [p for p, d in directives.items() if d.get("purpose") == "schema"]
    eval_files = args or uploaded_eval_files(root)

    schema_columns: set[str] = set()
    for rel in schema_files:
        for headers in read_tables(root / rel, directives.get(rel)).values():
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
    try:
        sys.exit(main())
    except _table_sources.SheetSelectionError as exc:
        print(f"Sheet 范围需要确认：{exc}", file=sys.stderr)
        sys.exit(2)
