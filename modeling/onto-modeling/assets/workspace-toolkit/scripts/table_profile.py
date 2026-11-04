#!/usr/bin/env python3
"""表登记：项目里登记过的数据表（schema 表格资料）的表头与取值画像，以及数据表三方对账。

登记的数据表 = resource/materials.json 中 purpose=schema 的表格资料（一个 Sheet = 一张表，首行 = 列名）。
对象「# 数据来源」的表名、存储属性的英文名称以它为准；规则检查、产物核验、资料分析验收与
候选修订都经这里读表，与后端发布包的数据绑定（package_mapping）同一口径。资料区的 Excel 只读
数据接入转出的文本版（resource/text/<分组>/<原文件名>.md），还没有文本版的单独列出，不当作「没有表」。

三方对账（确定性检查，不经模型）：命令行给出的数据表列 ↔ schema 资料字段 ↔ 对象属性英文名
（ontology/object/*.md 的「对象属性清单」表格「英文名称」列）。

用法（工作区根目录执行）：
  python3 .claude/scripts/table_profile.py [--json] 表名或数据表路径 ...
      # 三方对账给出的数据表：schema 资料里的表名（Sheet 名）、csv 路径，或 Excel 的文本版路径
      # （resource/text/excel/业务数据.xlsx.md）；不要写 Excel 原件路径，原件不在这里解析
  python3 .claude/scripts/table_profile.py --headers
      # 全部表头写到 .ontology-state/table-headers.json（{"tables": {Sheet 名: [表头列]}, "missing_text": [...]}，
      # 一列一行），stdout 只给摘要 JSON：status（ok / no_schema 没有 schema 资料 / text_missing 有 schema 资料还没有
      # 文本版）、file、tables（表名 → 列数，最多 200 张）、missing_text（最多 50 份），约 20KB 以内
  python3 .claude/scripts/table_profile.py --profile [--tables 表名 ...]
      # 输出各列取值画像 JSON（每表最多读 2000 行；只有类别计数与推断类型，不含任何单元格原值），
      # 供对象抽取按取值证据定类型；最多 400 列（约 50KB），其余表名列入 omitted_tables；
      # status、missing_text 同 --headers

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
HEADERS_FILE = Path(".ontology-state") / "table-headers.json"
SUMMARY_TABLE_LIMIT = 200
MISSING_LIMIT = 50
EXCEL_DEPENDENCY_MESSAGE = "需要 openpyxl 解析 Excel（pip install openpyxl），字段核验未完成"


def load_directives(root: Path) -> dict:
    path = root / "resource" / "materials.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


NO_TEXT_MESSAGE = "Excel「{}」还没有文本版（转换中、暂不支持或转换失败），字段核验未完成"
_SHEET_RE = re.compile(r"^<!-- 工作表：(.+) -->$")


def resource_text(path: Path) -> Path | None:
    """资料区里的 Excel 原件 → 数据接入转出的文本版（resource/text/<分组>/<原文件名>.md）；不在资料区返回 None。
    资料区的 Excel 不在这里解析（F4c，ADR-0076：解析不可信文件不在主容器里做），只读文本版里的 Markdown 表。"""
    if path.suffix.lower() == ".csv":
        return None
    for parent in path.parents:
        if parent.name == "resource":
            rel = path.relative_to(parent)
            return None if rel.parts[:1] == ("text",) else parent / "text" / f"{rel.as_posix()}.md"
    return None


def _md_row(line: str) -> list[str]:
    body = line.strip()
    body = body[1:] if body.startswith("|") else body
    cells, current, escaped = [], [], False
    for char in body:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "|":
            cells.append("".join(current))
            current = []
        else:
            current.append(char)
    if current and "".join(current).strip():
        cells.append("".join(current))
    return [cell.strip().replace("<br>", "\n") for cell in cells]


def text_sheets(text_path: Path) -> dict[str, list[list[str]]] | None:
    """文本版 → {工作表名: 行}（每个工作表标记后第一张表，表头在第一行）。没有文本版返回 None。
    与后端 app/evidence/convert.py 的 md_tables 同口径（那份在后端，这里只能用标准库另写一份）。"""
    try:
        markdown = text_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    tables: dict[str, list[list[str]]] = {}
    sheet, rows = None, []
    for line in markdown.splitlines() + [""]:
        stripped = line.strip()
        marker = _SHEET_RE.match(stripped)
        if marker or not stripped.startswith("|"):
            if sheet is not None and rows and sheet not in tables:
                tables[sheet] = rows
            rows = []
            if marker:
                sheet = marker.group(1)
            continue
        cells = _md_row(stripped)
        if cells and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        rows.append(cells)
    return tables


def read_tables(path: Path) -> dict[str, list[str]]:
    """表格文件 → {表名: [表头列]}。csv 的表名取文件名（去扩展名）；资料区的 Excel 读文本版，没有文本版返回空。"""
    text = resource_text(path)
    if text is not None:
        sheets = text_sheets(text) or {}
        return {name: headers for name, rows in sheets.items()
                if (headers := [cell.strip() for cell in rows[0] if cell.strip()])}
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


def missing_text_versions(root: Path) -> list[str]:
    """登记了 schema、原件在、却还没有文本版的 Excel（转换中、暂不支持或转换失败）。
    它们的表头读不到，不能当作「项目里没有这张表」，也不能再去请用户把它标为 schema。"""
    missing = []
    for rel in registered_table_files(root):
        path = root / rel
        text = resource_text(path)
        if text is not None and path.is_file() and not text.is_file():
            missing.append(rel)
    return missing


def schema_status(root: Path) -> str:
    """ok；no_schema 没有 schema 表格资料；text_missing 有 schema 资料还没有文本版。"""
    if not registered_table_files(root):
        return "no_schema"
    return "text_missing" if missing_text_versions(root) else "ok"


def write_headers(root: Path) -> dict:
    """--headers：全部表头写到 HEADERS_FILE（子代理按路径读，主助手不转述），返回给 stdout 的摘要。"""
    tables = table_headers(root)
    missing = missing_text_versions(root)
    target = root / HEADERS_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    # 一列一行：读文件的工具按行截断超长的行，宽表的表头也不会被截掉
    target.write_text(json.dumps({"tables": tables, "missing_text": missing}, ensure_ascii=False, indent=1) + "\n",
                      encoding="utf-8")
    names = list(tables)
    return {
        "status": schema_status(root),
        "file": HEADERS_FILE.as_posix(),
        "tables": {name: len(tables[name]) for name in names[:SUMMARY_TABLE_LIMIT]},
        "tables_omitted": max(len(names) - SUMMARY_TABLE_LIMIT, 0),
        "missing_text": missing[:MISSING_LIMIT],
    }


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
        if path.suffix.lower() != ".csv" and resource_text(path) is None and not excel_ok:
            continue
        for name, headers in read_tables(path).items():
            tables.setdefault(name, headers)
    return tables


def read_profiles(path: Path) -> dict[str, dict]:
    """表格文件 → {表名: 取值画像}。每表最多读 PROFILE_ROW_LIMIT 行，画像只含计数、不含原值。

    缺 openpyxl 时抛 ValueError：读不了的表不能当作没有取值证据而放过。资料区的 Excel 读文本版。
    """
    text = resource_text(path)
    if text is not None:
        sheets = text_sheets(text)
        if sheets is None:
            raise ValueError(NO_TEXT_MESSAGE.format(path.name))
        return {name: profile for name, rows in sheets.items()
                if (profile := field_semantics.profile_table(rows))}
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


def within_workspace(root: Path, path: Path) -> bool:
    """path 解析后在工作区内。助手副本里 `resource` 是平台挂的链接（指向主线工作区的资料区），
    资料区下的路径按链接目标判断，其余照旧不许跟着链接出去。"""
    if path.resolve().is_relative_to(root.resolve()):
        return True
    link = root / "resource"
    return (link.is_symlink() and path.absolute().is_relative_to(link.absolute())
            and path.resolve().is_relative_to(link.resolve()))


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
    tables: dict[str, list[str]] = {}
    profiles: dict[str, dict] = {}
    for rel in registered_table_files(root):
        path = root / rel
        if not within_workspace(root, path):
            raise ValueError("核验路径越出工作区")
        if not path.is_file():
            raise ValueError("已登记的数据表不存在，字段核验未完成")
        text = resource_text(path)
        if text is not None and not text.is_file():
            raise ValueError(NO_TEXT_MESSAGE.format(path.name))
        if path.suffix.lower() != ".csv" and text is None and importlib.util.find_spec("openpyxl") is None:
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
        if not path.is_file() or (path.suffix.lower() != ".csv" and resource_text(path) is None and not excel_ok):
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


def target_tables(root: Path, target: str) -> dict[str, list[str]] | None:
    """三方对账的一个对象 → {表名: 表头列}：csv、Excel 的文本版（resource/text/…/x.xlsx.md），
    或 schema 资料里的表名。原件路径照样按它的文本版读。找不到返回 None。"""
    path = root / target
    if path.is_file() and within_workspace(root, path):
        if path.suffix.lower() == ".md":
            sheets = text_sheets(path) or {}
            return {name: headers for name, rows in sheets.items()
                    if (headers := [cell.strip() for cell in rows[0] if cell.strip()])}
        if target.lower().endswith(TABLE_SUFFIXES):
            return read_tables(path)
    headers = table_headers(root)
    return {target: headers[target]} if target in headers else None


def check_file(root: Path, rel: str, schema_columns: set[str], attr_names: set[str]) -> dict:
    tables = target_tables(root, rel)
    if tables is None:
        return {"path": rel, "status": "warn", "sheets": [],
                "reason": "找不到这张表：写 schema 资料里的表名、csv 路径或 Excel 的文本版路径"}
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
        print(json.dumps(write_headers(root), ensure_ascii=False))
        return 0
    if "--profile" in sys.argv:
        names = _option_values(sys.argv, "--tables")
        summary = profile_summary(table_profiles(root), names)
        summary.update(status=schema_status(root), missing_text=missing_text_versions(root)[:MISSING_LIMIT])
        print(json.dumps(summary, ensure_ascii=False))
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
        "missing_text": missing_text_versions(root)[:MISSING_LIMIT],
        "files": [],
    }
    if not schema_columns and not attr_names:
        result["status"] = "no_schema"
    if not targets:
        result["status"] = "no_files"
    for rel in targets:
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
            print(f"\n{mark} {f['path']}" + (f"：{f['reason']}" if f.get("reason") else ""))
            for s in f["sheets"]:
                print(f"  表「{s['name']}」{s['columns']} 列："
                      f"schema 命中 {len(s['schema_matched'])}，属性命中 {len(s['attr_matched'])}")
                if s["data_only"]:
                    print(f"    - schema 中不存在的列：{', '.join(s['data_only'])}")
                if s["attr_unmatched"]:
                    print(f"    - 未映射到对象属性的列：{', '.join(s['attr_unmatched'])}")
        if result["missing_text"]:
            print(f"\n（schema 资料还没有文本版，这次没有用到：{'、'.join(result['missing_text'])}）")
        if result["status"] == "no_schema":
            print("\n（无 schema 资料与对象属性可对账——先给结构表打 schema 标签或完成对象提取）")
        elif result["status"] == "no_files":
            print("\n（没有给出要对账的数据表：在命令行列出表名或文本版路径，如 订单表、resource/text/excel/业务数据.xlsx.md）")
    return 1 if result["status"] == "warn" else 0


if __name__ == "__main__":
    sys.exit(main())
