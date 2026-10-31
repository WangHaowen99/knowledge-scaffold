#!/usr/bin/env python3
"""脚本入参形态：对齐 consumer 调用脚本前的参数证据校验，保证第一次调用就能通过。

consumer（consumer/backend/agent/tools/execute_script.py）在启动脚本前核对参数：
- 参数里每个 JSON 对象（顶层参数整体也算一个）是一条记录。记录里取值能在本轮查询结果中按
  **同名字段**找到的字段有两个以上、其中有标识字段时，它们必须出自同一条查询结果，否则判
  `evidence_record_mismatch`，脚本不会运行；同一错误再出现一次，整轮对话按停滞终止。
- 标识字段的取值必须出现在用户请求里，或出现在本轮查询结果的同名字段中（`ungrounded_identity`）。

所以记录参数要按来源对象分组：每项是一个对象的一条查询结果，字段名逐字使用对象属性清单的
英文名称，取值原样拷贝；顶层的条件参数不与对象属性同名；标识参数不能必填（用户没给编号时
智能体只能编造）。记录是否提供只看参数传没传：题面只说记录已提供、没给取值时传 {}。
取值原样拷贝意味着脚本要认数据源的表示：图数据源把「是/否」列存成布尔值
（modeling 图导入的 value-inference 把 是/否/true/false/yes/no/y/n 推断为 boolean）。
本模块据此做确定性检查，script_descriptor.py（规则检查与导出共用）调用它。仅依赖标准库。
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from operation_relations import sections

ATTRIBUTE_SECTION = "对象属性清单"
# 从已发布知识包导入的对象沿用包契约格式：`# 字段` 表的「字段 key」列
PACKAGE_ATTRIBUTE_SECTION = ("字段", "字段 key")
STRUCTURED_TYPES = ("object", "array", "json")
SCALAR_TYPES = ("string", "integer", "number", "boolean")
# 与 consumer 的标识字段判定同一口径（execute_script._is_identity_field 的通用部分）
_IDENTITY_SUFFIXES = ("_id", "_no", "_code", "_name")
_IDENTITY_NAMES = frozenset({"id", "code", "identifier", "device", "equipment", "unit"})
_IDENTITY_MARKERS = ("编号", "名称")
_IDENTITY_EXCLUSIONS = frozenset({"batch_id", "batch_no", "record_id", "row_id"})
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)")
_YES_NO_LITERALS = {"是", "否"}
_ENUM_VALUES_RE = re.compile(r"取值[：:]\s*([^；;。\n]+)")
_BOOL_NORMALIZATION_RE = re.compile(r"_yes_no\b|isinstance\([^)]*\bbool\b")
COPY_RULE = (
    "每项原样拷贝同一条查询结果的字段：不要拼入其他记录或其他对象的字段，取值不改写；"
    "查询结果里没有的字段（包括指向其他对象的编号，即外键）直接省略，不要从其他记录补，也不要编造"
)
IDENTITY_NOTE = (
    "只在查询结果或用户问题里出现时才填，没有就省略，不要编造；示例里的编号只是格式示例，不要照抄；"
    "题面用名称或代号说明记录归属（如「属于甲」「属于乙」）时，把这个说法原样填进对应编号字段，脚本据此核对是否一致"
)
# 假设场景常只说「记录已提供」而不给取值：智能体整条不传时，脚本只能判记录缺失。放在参数说明最前面
PRESENCE_RULE = (
    "必须照传：题面或用户说这条记录存在（如「已完整提供」）就要传，没给任何取值时对象参数传 {}、数组参数每条传一个 {}；"
    "只有记录确实不存在时才不传"
)
# 参数说明里写「不传时按缺失/资料不足处理」：智能体在假设场景里会据此整条不传
_ABSENCE_HANDLING_RE = re.compile(r"(?:不传|未传|没传)[^；;。]*?(?:资料不足|缺失|缺少)")


def is_identity_field(name: str) -> bool:
    normalized = str(name or "").strip().casefold()
    if not normalized or normalized in _IDENTITY_EXCLUSIONS:
        return False
    return (
        normalized in _IDENTITY_NAMES
        or normalized.endswith(_IDENTITY_SUFFIXES)
        or any(marker in normalized for marker in _IDENTITY_MARKERS)
    )


def attributes_from_documents(documents: dict[str, str]) -> dict[str, set[str]]:
    """对象名 → 「对象属性清单」的英文名称集合（即查询结果里的字段名）。"""
    attributes: dict[str, set[str]] = {}
    for name, text in documents.items():
        names = {field for field, _cells in _attribute_rows(text)}
        if names:
            attributes[name] = names
    return attributes


def enum_values_from_documents(documents: dict[str, str]) -> dict[str, dict[str, list[str]]]:
    """对象名 → {字段: 允许取值}：属性表里类型为「枚举」且写了「取值：a / b」的字段。
    「是/否」字段不列：_yes_no 已兼容图数据源的布尔值。"""
    enums: dict[str, dict[str, list[str]]] = {}
    for name, text in documents.items():
        for field, cells in _attribute_rows(text):
            if "枚举" not in cells:
                continue
            match = next((found for found in map(_ENUM_VALUES_RE.search, cells) if found), None)
            values = [value for value in re.split(r"\s*[/／]\s*", match.group(1).strip()) if value] if match else []
            if len(values) >= 2 and not set(values) <= _YES_NO_LITERALS:
                enums.setdefault(name, {})[field] = values
    return enums


def _attribute_rows(text: str) -> list[tuple[str, list[str]]]:
    """属性表每一行的 (英文名称, 全部单元格)。工作区格式读「对象属性清单」，导入包读「# 字段」。"""
    doc_sections = sections(text)
    if ATTRIBUTE_SECTION in doc_sections:
        section, column_name = doc_sections[ATTRIBUTE_SECTION], "英文名称"
    else:
        section, column_name = doc_sections.get(PACKAGE_ATTRIBUTE_SECTION[0], ""), PACKAGE_ATTRIBUTE_SECTION[1]
    rows = [line.strip() for line in section.split("\n") if line.strip().startswith("|")]
    if len(rows) < 3:
        return []
    headers = [cell.strip() for cell in rows[0].strip("|").split("|")]
    if column_name not in headers:
        return []
    column = headers.index(column_name)
    result = []
    for row in rows[2:]:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        if column < len(cells) and cells[column] and cells[column] not in ("无", "—", "-"):
            result.append((cells[column], cells))
    return result


def load_object_documents(ontology_dir: Path) -> dict[str, str]:
    documents: dict[str, str] = {}
    directory = Path(ontology_dir) / "object"
    if directory.is_dir():
        for path in sorted(directory.rglob("*.md")):
            if path.name.lower() == "index.md" or path.stem in documents:
                continue
            try:
                documents[path.stem] = path.read_text(encoding="utf-8")
            except (OSError, ValueError):
                continue
    return documents


def enum_note(param: dict, enums: dict[str, dict[str, list[str]]]) -> str:
    """记录参数说明里点到的枚举字段及允许取值（来源对象取说明里的 [[对象名]]）；没有时返回空串。
    智能体在假设场景里常不读对象文档，按字面意思自造取值，脚本就会把它当成条件不满足。"""
    desc = param.get("desc", "")
    declared = [obj.strip() for obj in _WIKILINK_RE.findall(desc) if obj.strip() in enums]
    if not declared:
        return ""
    named = [
        f"{field} 取 {' / '.join(values)}"
        for field, values in enums[declared[0]].items()
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(field)}(?![A-Za-z0-9_])", desc)
    ]
    return "枚举字段只能从下列取值里选一项，不要自造或改写：" + "；".join(named) if named else ""


def example_record_keys(desc: str) -> list[str] | None:
    """参数说明里第一个 JSON 示例的记录字段；示例不是记录（如字符串数组）或没有示例时返回 None。"""
    decoder = json.JSONDecoder()
    for position, char in enumerate(desc):
        if char not in "[{":
            continue
        try:
            value, _end = decoder.raw_decode(desc[position:])
        except ValueError:
            continue
        items = [value] if isinstance(value, dict) else value if isinstance(value, list) else []
        records = [item for item in items if isinstance(item, dict)]
        if not records:
            return None
        keys: list[str] = []
        for record in records:
            keys += [str(key) for key in record if str(key) not in keys]
        return keys
    return None


def is_record_param(param: dict) -> bool:
    return param.get("type") in STRUCTURED_TYPES and example_record_keys(param.get("desc", "")) is not None


def argument_shape_issues(params: list[dict], attributes: dict[str, set[str]]) -> list[tuple[str, str]]:
    """(严重度, 说明)：会被 consumer 参数证据校验拒绝的入参形态。"""
    issues: list[tuple[str, str]] = []
    owners: dict[str, set[str]] = {}
    for obj, names in attributes.items():
        for field in names:
            owners.setdefault(field, set()).add(obj)

    for param in params:
        if param.get("type") not in STRUCTURED_TYPES:
            continue
        keys = example_record_keys(param.get("desc", ""))
        if keys is None:
            continue
        absence = _ABSENCE_HANDLING_RE.search(param.get("desc", ""))
        if absence:
            issues.append(("警告", (
                f"参数 {param['name']} 的说明写了不传时的处理（「{absence.group(0)}」）：用户只说记录已提供、没给取值时，"
                "智能体会据此整条不传，脚本只能判记录缺失。描述符已自动写明「记录存在就照传，没取值传 {}」，请删掉这句"
            )))
        declared = [obj for obj in _WIKILINK_RE.findall(param.get("desc", "")) if obj.strip() in attributes]
        attribute_keys = [key for key in keys if key in owners]
        if not declared:
            issues.append(("警告", (
                f"参数 {param['name']} 是记录参数，说明里没有写明来源对象（[[对象名]]）：consumer 的智能体要按它去查哪个对象、"
                "原样拷贝哪一条查询结果"
            )))
            candidates = set.intersection(*(owners.get(key, set()) for key in attribute_keys)) if attribute_keys else set()
        else:
            candidates = {declared[0].strip()}
            missing = [key for key in keys if key not in attributes[declared[0].strip()]]
            if missing:
                issues.append(("警告", (
                    f"参数 {param['name']} 的示例字段 {'、'.join(missing)} 不在 [[{declared[0].strip()}]] 的属性清单里"
                    "（注意大小写）：consumer 按字段名逐字核对查询结果，这些字段核对不到，其中的标识字段还会被判为没有依据"
                )))
        foreign = (
            [key for key in attribute_keys if not (owners.get(key, set()) & candidates)] if candidates else attribute_keys
        )
        if len(attribute_keys) >= 2 and foreign and any(is_identity_field(key) for key in attribute_keys):
            groups = "；".join(
                f"{key} 属于「{'、'.join(sorted(owners.get(key, set())))}」" for key in attribute_keys
            )
            issues.append(("错误", (
                f"参数 {param['name']} 的一条记录混入了不同对象的字段（{groups}）：consumer 要求一条记录里的字段出自同一条查询结果，"
                "否则判 evidence_record_mismatch、脚本不运行，再错一次整轮对话就会终止。请按对象拆成多个记录参数"
                "（每项原样拷贝该对象的一条查询结果），在脚本里按关联字段关联"
            )))

    top_level = [
        param["name"] for param in params
        if param.get("type") in SCALAR_TYPES and param.get("multiple") != "是" and param["name"] in owners
    ]
    if len(top_level) >= 2 and any(is_identity_field(name) for name in top_level):
        issues.append(("错误", (
            f"顶层参数 {'、'.join(top_level)} 与对象属性同名：consumer 把顶层参数当作一条记录核对，这些取值必须出自同一条查询结果，"
            "否则判 evidence_record_mismatch。同一对象的字段请放进该对象的记录参数；用户给的条件参数改用不与对象属性同名的名字"
            "（如 query_month、min_amount）"
        )))
    issues += _required_identity_issues(params)
    return issues


def identity_keys(param: dict) -> list[str]:
    """记录参数示例里的标识字段；标量参数本身是标识字段时返回它自己。"""
    keys = example_record_keys(param.get("desc", "")) if param.get("type") in STRUCTURED_TYPES else None
    if keys is not None:
        return [key for key in keys if is_identity_field(key)]
    return [param["name"]] if is_identity_field(param.get("name", "")) else []


def _required_identity_issues(params: list[dict]) -> list[tuple[str, str]]:
    """标识参数必填时，用户没给编号（如只给事实的假设场景）、查询结果里也没有，智能体只能编造，
    consumer 以 ungrounded_identity 拒绝，脚本不运行。"""
    issues = []
    for param in params:
        if param.get("required") != "是":
            continue
        if param.get("type") in SCALAR_TYPES and is_identity_field(param["name"]):
            issues.append(("错误", (
                f"参数 {param['name']} 是标识字段却设为必填：用户没给编号、查询结果里也没有时，智能体只能编造，"
                "consumer 判 ungrounded_identity、脚本不运行。请改为可选：缺编号时按调用范围关联，"
                "资料不全就在输出里给资料不足（退出码 0），不要报错退出——consumer 遇到非 0 退出会直接结束对话"
            )))
            continue
        keys = example_record_keys(param.get("desc", "")) if param.get("type") in STRUCTURED_TYPES else None
        if keys and all(is_identity_field(key) for key in keys):
            issues.append(("错误", (
                f"必填的记录参数 {param['name']} 只含标识字段（{'、'.join(keys)}）：没有编号时只能整条编造，"
                "consumer 判 ungrounded_identity、脚本不运行。请改为可选，或在示例里写出脚本实际要用的非编号字段"
            )))
    return issues


def yes_no_issues(source: str) -> list[tuple[str, str]]:
    """脚本按「是/否」字符串判断取值，却没有兼容布尔值：图数据源把是/否存成 true/false，原样拷贝时会被当成未知。"""
    if _BOOL_NORMALIZATION_RE.search(source):
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        literals = set()
        for operand in operands:
            elements = operand.elts if isinstance(operand, (ast.Tuple, ast.List, ast.Set)) else [operand]
            literals |= {
                element.value for element in elements
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            }
        if literals & _YES_NO_LITERALS:
            return [("警告", (
                "脚本按「是/否」字符串判断取值，但没有兼容布尔值：图数据源把是/否列存成 true/false，智能体原样拷贝查询结果时，"
                "这些取值会被当成未知（如判资料不足）。请先用 _yes_no 归一再判断（写法见 .claude/templates/script-template.py）"
            ))]
    return []
