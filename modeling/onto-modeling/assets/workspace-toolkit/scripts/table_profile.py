#!/usr/bin/env python3
"""表登记：项目里登记过的数据表（schema 表格资料）的表头与取值画像，以及数据表三方对账。

登记的数据表 = resource/materials.json 中 purpose=schema 的表格资料（一个 Sheet = 一张表，首行 = 列名）。
对象「# 数据来源」的表名、存储属性的英文名称以它为准；规则检查、产物核验、资料分析验收与
候选修订都经这里读表，与后端发布包的数据绑定（package_mapping）同一口径。

三方对账（确定性检查，不经模型）：命令行给出的数据表列 ↔ schema 资料字段 ↔ 对象属性英文名
（ontology/object/*.md 的「对象属性清单」表格「英文名称」列）。

用法（工作区根目录执行）：
  python3 .claude/scripts/table_profile.py [--json] path/to/xx.xlsx ...   # 三方对账给出的数据表
  python3 .claude/scripts/table_profile.py --headers   # 输出 {Sheet 名: [表头列]} JSON，供对象抽取取表名与列名
  python3 .claude/scripts/table_profile.py --profile [--tables 表名 ...]
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
PROFILE_ROW_LIMIT = field_semantics.PROFILE_ROW_LIMIT
PROFILE_COLUMN_LIMIT = 400
EXCEL_DEPENDENCY_MESSAGE = "需要 openpyxl 解析 Excel（pip install openpyxl），字段核验未完成"


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


def schema_files(root: Path) -> list[str]:
    """materials.json 里标了 schema 的资料（工作区相对路径，按登记顺序）。"""
    return [p for p, d in load_directives(root).items() if isinstance(d, dict) and d.get("purpose") == "schema"]


def table_headers(root: Path) -> dict[str, list[str]]:
    """schema 资料 → {表名: 表头列}。

    对象「# 数据来源」的表名与存储属性的英文名称以此为准；同名 Sheet 以先读到的为准。
    """
    # 作为库被 ontology_checker 调用时缺 openpyxl 只跳过 Excel，不能像命令行那样退出进程
    excel_ok = importlib.util.find_spec("openpyxl") is not None
    tables: dict[str, list[str]] = {}
    for rel in schema_files(root):
        path = root / rel
        if not (str(rel).lower().endswith(TABLE_SUFFIXES) and path.is_file()):
            continue
        if path.suffix.lower() != ".csv" and not excel_ok:
            continue
        for name, headers in read_tables(path).items():
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
    """登记的表格文件：schema 资料里的表格（工作区相对路径，去重保序）。"""
    return [rel for rel in dict.fromkeys(schema_files(root)) if str(rel).lower().endswith(TABLE_SUFFIXES)]


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
        headers = read_tables(path)
        if not headers:
            raise ValueError("数据表无法解析或没有表头，字段核验未完成")
        for name, columns in headers.items():
            tables.setdefault(name, columns)
        _merge_profiles(profiles, read_profiles(path))
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

    schema = schema_files(root)
    targets = args

    schema_columns: set[str] = set()
    for rel in schema:
        for headers in read_tables(root / rel).values():
            schema_columns.update(headers)
    attr_names = object_attribute_names(root)

    result = {
        "schema_files": schema,
        "schema_columns": len(schema_columns),
        "object_attributes": len(attr_names),
        "files": [],
    }
    if not schema_columns and not attr_names:
        result["status"] = "no_schema"
    if not targets:
        result["status"] = "no_files"
    for rel in targets:
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
        print(f"schema 资料：{len(schema)} 份，字段 {len(schema_columns)} 个；"
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
        elif result["status"] == "no_files":
            print("\n（没有给出要对账的数据表：在命令行列出数据表路径，如 resource/excel/业务数据.xlsx）")
    return 1 if result["status"] == "warn" else 0


if __name__ == "__main__":
    sys.exit(main())
