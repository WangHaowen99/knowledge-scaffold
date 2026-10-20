#!/usr/bin/env python3
"""求解评估器契约：旧式评估器的接口符号检查，与声明式评估器的完整校验。

单一事实源：后端 `solver_contract.py` 的求解契约检查与 `/ontology-solver-evaluator` 的自检
共用本模块，建模阶段的自检结论与求解面板一致。只解析、从不执行评估器源码；仅依赖标准库。

评估器分两类，判定口径与 Consumer 一致（两类特征同时具备时按旧式处理）：

- 旧式：顶层定义 BatchPlan、build_report、load_tables、normalize_crude_name，
  由 Consumer 在隔离子进程中运行包内代码；
- 声明式：只含 docstring、`from __future__ import annotations` 与对 DECLARATION_NAMES 的
  字面量赋值，其中 TABLE_SOURCES 把 7 张逻辑表映射到本体对象与属性；对象自身没有的列可经
  一跳 lookup 从关联对象取得。取数和指标计算全部由 Consumer 完成，包内没有公式。

声明式规则与 Consumer `integration/solver_declarative_evaluator.py` 及事实 worker 的
`_solver_context_config` 对齐；TABLE_COLUMNS 必须与 Consumer 的同名清单逐项一致。

用法：
    python3 .claude/scripts/solver_evaluator_contract.py ontology/scripts/<文件>.py \\
        [--ontology-dir ontology] [--json]
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import unicodedata
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, NamedTuple

MAX_EVALUATOR_BYTES = 2 * 1024 * 1024
LEGACY_KIND = "legacy"
DECLARATIVE_KIND = "declarative"
# consumer 按这组顶层定义自动发现旧式评估器，加载后还要读模块级 SOLVER_CONTEXT
EVALUATOR_DISCOVERY_SYMBOLS = frozenset(
    {"BatchPlan", "build_report", "load_tables", "normalize_crude_name"}
)
EVALUATOR_CONTEXT_SYMBOL = "SOLVER_CONTEXT"
TABLE_SOURCES_SYMBOL = "TABLE_SOURCES"
ROUTE_OPTIONS = ("OPTION_0", "OPTION_1")
# consumer 事实 worker 每次评估都会读取的模块常量，缺任一项运行时即判契约不兼容
ROUTE_CONSTANT_NAMES = (
    "ATMOS_UNIT_ID",
    "DIESEL_UNIT_IDS",
    "DEFAULT_DIESEL_UNIT_ID",
    "VGO_UNIT_ID",
    "YIELD_ATMOS",
    "YIELD_DIESEL_WITH_VGO",
    "YIELD_DIESEL_WITHOUT_VGO",
    "YIELD_VGO_WITH_VGO",
    "YIELD_VGO_WITHOUT_VGO",
    "ATMOS_DIESEL_FEED",
    "ATMOS_HC_FEED",
    "VGO_INTERMEDIATE_NAMES",
    "RULE_DAILY_CAPACITIES",
    "CORRECTION_FACTORS",
)
EVALUATOR_MODULE_CONSTANTS = frozenset(ROUTE_CONSTANT_NAMES)
DECLARATION_NAMES = ROUTE_CONSTANT_NAMES + (EVALUATOR_CONTEXT_SYMBOL, TABLE_SOURCES_SYMBOL)
# SOLVER_CONTEXT 必填键 → 值为字典字面量时须含的子键；atmospheric_product_tanks 可省略
EVALUATOR_CONTEXT_KEYS = {
    "option_routes": ROUTE_OPTIONS,
    "route_stream_products": (),
    "special_tanks": ("diesel", "vgo"),
    "unit_capacity_keys": ("atmospheric", "diesel_by_unit", "vgo"),
    "correction_factor_prefix_by_unit": (),
    "production_condition": (),
}
# 声明式评估器额外必填的键；旧式评估器自带收率计算，Consumer 不读这个键
DECLARATIVE_CONTEXT_KEYS = ("yield_unit",)
OPTIONAL_CONTEXT_KEYS = ("atmospheric_product_tanks",)
# Consumer 求解引擎与问题 schema 只接受这一个生产工况（接口常量，不是业务文档里的工况名称）
PRODUCTION_CONDITION = "NO_JET"
# 收率列的写法：percent 为百分数（30 表示 30%），fraction 为小数（0.3 表示 30%）；Consumer 统一换算成百分数
YIELD_UNITS = ("percent", "fraction")
# 评估器接口问题沿用稳定错误码，但通用提示（改用契约版本或 Profile）不适用，改给接口提示
EVALUATOR_INTERFACE_HINT = (
    "评估器须按 Consumer 兼容层接口实现：读取业务表、按批次方案给出评估报告，路线由求解器选择；"
    "自行选路线的决策脚本不适用。请改选符合接口的评估器脚本，或按接口补全当前脚本；"
    "也可以执行 /ontology-solver-evaluator，让建模助手生成声明式评估器。"
)
DECLARATIVE_HINT = (
    "按 /ontology-solver-evaluator 的规则修正声明式评估器，"
    "可运行 python3 .claude/scripts/solver_evaluator_contract.py <评估器文件> 自检。"
)
UNPARSEABLE_MESSAGE = "评估器无法解析（非 UTF-8、语法错误或超过 2 MiB）"


class TableColumns(NamedTuple):
    """一张逻辑表可映射到对象属性的封闭列清单。"""

    required: tuple[str, ...]
    optional: tuple[str, ...] = ()
    any_of: tuple[str, ...] = ()
    yield_constants: tuple[str, ...] = ()

    def fixed_columns(self) -> frozenset[str]:
        return frozenset(self.required + self.optional + self.any_of)


# consumer 事实 worker 只读这些列；增删任一项都要同步 Consumer 的 TABLE_COLUMNS
TABLE_COLUMNS: dict[str, TableColumns] = {
    "crude_oil_yard": TableColumns(required=("OilYardID", "CrudeOilYard Name")),
    "refinery_unit": TableColumns(required=("RefineryUnitID",), optional=("price_per_unit",)),
    "vgo_ht_unit": TableColumns(required=("VGOUnitID",), optional=("price_per_unit",)),
    "hydrogenation_product_yield": TableColumns(
        required=("OilYardID", "RefineryUnitID", "ProductName"),
        optional=("LogicTankID", "price_per_unit"),
        yield_constants=("YIELD_ATMOS", "YIELD_DIESEL_WITH_VGO", "YIELD_DIESEL_WITHOUT_VGO"),
    ),
    "vgo_product_yield": TableColumns(
        required=("OilYardID", "VGOUnitID", "ProductName"),
        optional=("LogicTankID", "price_per_unit"),
        yield_constants=("YIELD_VGO_WITH_VGO", "YIELD_VGO_WITHOUT_VGO"),
    ),
    "logic_tank": TableColumns(
        required=("LogicTankID",),
        optional=("CurrentCapacity_Tons", "LowSafetyThrd_Tons"),
        any_of=("AvailableCapacity_Tons", "SafetyStockThrd_Tons"),
    ),
    "energy_consume": TableColumns(
        required=("UnitConsume_Value",),
        optional=("price_per_unit",),
        any_of=("RefineryUnitID", "VGOUnitID"),
    ),
}
# 与路线常量按字符串比对的 ID 列：图谱导入若把它们推断成数字，前导零会丢失
ID_COLUMNS = frozenset({"OilYardID", "RefineryUnitID", "VGOUnitID", "LogicTankID"})
# fields 的值除了属性 key，还可以是一跳关联：本对象没有的列，经关联对象中唯一匹配的那条记录取得
LOOKUP_KEYS = frozenset({"lookup", "match", "field"})
LOOKUP_SHAPE = '{"lookup": 关联对象, "match": {本对象属性: 关联对象属性}, "field": 关联对象属性}'
NUMERIC_TYPE_MARKERS = (
    "整型", "整数", "数值", "数字", "浮点", "小数", "int", "number", "float", "decimal", "double",
)
MAX_DECLARED_TEXT_LENGTH = 128
# 模板占位符形如 <待填写：…>；真实的 ID、列名与产品名都不会带尖括号
PLACEHOLDER_RE = re.compile(r"<[^<>\n]*>")
MAX_REPORTED_PROBLEMS = 50

_UNIT_ID_NAMES = ("ATMOS_UNIT_ID", "DEFAULT_DIESEL_UNIT_ID", "VGO_UNIT_ID")
_YIELD_NAMES = (
    "YIELD_ATMOS",
    "YIELD_DIESEL_WITH_VGO",
    "YIELD_DIESEL_WITHOUT_VGO",
    "YIELD_VGO_WITH_VGO",
    "YIELD_VGO_WITHOUT_VGO",
)
_FEED_NAMES = ("ATMOS_DIESEL_FEED", "ATMOS_HC_FEED", "VGO_INTERMEDIATE_NAMES")
_SHARED_CORRECTION_FACTORS = ("vgo_load_a", "vgo_product_b")
_DEFINITIONS = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)

KIND_INVALID = "invalid"
KIND_REFERENCE = "reference_missing"
KIND_CONTEXT = "context_missing"
SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"


@dataclass(frozen=True)
class Problem:
    """一条校验结论；kind 由后端映射为稳定错误码，warning 只提示、不阻断。"""

    field: str
    message: str
    kind: str = KIND_INVALID
    severity: str = SEVERITY_ERROR

    def as_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "kind": self.kind,
            "field": self.field,
            "message": self.message,
        }


@dataclass
class EvaluatorSymbols:
    defined: set[str]  # 顶层类/函数定义
    assigned: set[str]  # 顶层赋值与导入的名字
    context: ast.expr | None  # SOLVER_CONTEXT 最终绑定的表达式；来自导入或顶层另有改写时为 None


def _parse(source: bytes | None) -> ast.Module | None:
    """只解析不执行；空文件、超过 2 MiB、非 UTF-8 或语法错误返回 None。"""
    if not source or len(source) > MAX_EVALUATOR_BYTES:
        return None
    try:
        return ast.parse(source.decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError, ValueError, RecursionError):
        return None


def _assigned_name(node: ast.stmt) -> str | None:
    """单目标名字赋值的名字；其它语句返回 None。"""
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        return node.targets[0].id
    return None


def evaluator_kind(source: bytes | None) -> str | None:
    """评估器类型：legacy / declarative；都不是（或无法解析）返回 None。"""
    module = _parse(source)
    if module is None:
        return None
    defined = {node.name for node in module.body if isinstance(node, _DEFINITIONS)}
    if EVALUATOR_DISCOVERY_SYMBOLS <= defined:
        return LEGACY_KIND
    if any(_assigned_name(node) == TABLE_SOURCES_SYMBOL for node in module.body):
        return DECLARATIVE_KIND
    return None


def _rebinds(statement: ast.stmt, name: str) -> bool:
    """顶层语句是否可能改写 name：对它或它的下标、属性赋值/删除，或调用它的方法。"""
    for node in ast.walk(statement):
        if isinstance(node, ast.Call):
            node = node.func
        elif not isinstance(getattr(node, "ctx", None), (ast.Store, ast.Del)):
            continue
        while isinstance(node, (ast.Subscript, ast.Attribute)):
            node = node.value
        if isinstance(node, ast.Name) and node.id == name:
            return True
    return False


def module_symbols(source: bytes | None) -> EvaluatorSymbols | None:
    """读出旧式评估器的顶层符号；无法解析返回 None。"""
    module = _parse(source)
    if module is None:
        return None
    symbols = EvaluatorSymbols(set(), set(), None)
    for node in module.body:
        if isinstance(node, _DEFINITIONS):
            symbols.defined.add(node.name)
            continue
        names: list[str] = []
        value = None
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
            names, value = [node.target.id], node.value
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [(alias.asname or alias.name).split(".")[0] for alias in node.names]
        symbols.assigned.update(names)
        if EVALUATOR_CONTEXT_SYMBOL in names:
            symbols.context = value
        elif _rebinds(node, EVALUATOR_CONTEXT_SYMBOL):
            symbols.context = None
    return symbols


def _literal_dict(node: ast.expr | None) -> dict[str, ast.expr] | None:
    """字符串键的字典字面量；含展开或非字符串键时无法静态确定，返回 None。"""
    if not isinstance(node, ast.Dict):
        return None
    entries = {}
    for key, value in zip(node.keys, node.values):
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            return None
        entries[key.value] = value
    return entries


def missing_context_keys(context: ast.expr | None) -> list[str]:
    """SOLVER_CONTEXT 字面量缺少的必填键（子键记作 键.子键）；运行时拼装的留给端到端验证。"""
    entries = _literal_dict(context)
    if entries is None:
        return []
    missing = []
    for key, sub_keys in EVALUATOR_CONTEXT_KEYS.items():
        if key not in entries:
            missing.append(key)
            continue
        nested = _literal_dict(entries[key])
        if nested is not None:
            missing += [f"{key}.{sub}" for sub in sub_keys if sub not in nested]
    return missing


def declared_objects(source: bytes | None) -> list[str]:
    """声明式评估器 TABLE_SOURCES 引用的对象 key，含 lookup 关联对象（去重排序）；读不出时返回空列表。"""
    module = _parse(source)
    if module is None:
        return []
    objects: set[str] = set()
    for node in module.body:
        if _assigned_name(node) != TABLE_SOURCES_SYMBOL:
            continue
        try:
            tables = ast.literal_eval(node.value)
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            continue
        if not isinstance(tables, Mapping):
            continue
        for table in tables.values():
            if not isinstance(table, Mapping):
                continue
            if isinstance(table.get("object"), str):
                objects.add(table["object"])
            fields = table.get("fields")
            for spec in fields.values() if isinstance(fields, Mapping) else ():
                if isinstance(spec, Mapping) and isinstance(spec.get("lookup"), str):
                    objects.add(spec["lookup"])
    return sorted(objects)


def normalize_source_name(value: Any) -> str:
    """来源名归一化（NFKC 后去掉全部空白），与 Consumer 的 normalize_crude_name 一致。"""
    text = unicodedata.normalize("NFKC", str(value))
    return "".join(character for character in text if not character.isspace())


# ---- 声明式评估器校验 -------------------------------------------------------
# 尽量一次报全，供助手按清单逐项修正；某项结构不对时，只跳过依赖它的交叉检查。
# 残留的模板占位符按顶层名字各报一次，其余检查遇到占位符一律放过，避免同一处重复报错。


def _pending(value: Any) -> bool:
    return isinstance(value, str) and PLACEHOLDER_RE.search(value) is not None


def _preview(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def _names(values: Iterable[Any], limit: int = 3) -> str:
    items = [str(value) for value in values]
    shown = "、".join(items[:limit])
    return shown if len(items) <= limit else f"{shown} 等 {len(items)} 项"


def _is_docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _is_future_annotations(node: ast.stmt) -> bool:
    if not isinstance(node, ast.ImportFrom) or node.module != "__future__" or node.level != 0:
        return False
    return [(alias.name, alias.asname) for alias in node.names] == [("annotations", None)]


def _duplicate_keys(node: ast.expr) -> list[Any]:
    """字典字面量里重复的键：literal_eval 会静默保留最后一个，评审读到的可能是被覆盖的值。"""
    duplicates = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Dict):
            continue
        seen: set[Any] = set()
        for key in child.keys:
            value = ast.literal_eval(key)
            if value in seen:
                duplicates.append(value)
            seen.add(value)
    return duplicates


def _declarations(module: ast.Module, out: list[Problem]) -> tuple[dict[str, Any], set[str]]:
    """顶层字面量赋值 → (合格的值, 出现过的名字)。"""
    values: dict[str, Any] = {}
    assigned: set[str] = set()
    for index, node in enumerate(module.body):
        if (index == 0 and _is_docstring(node)) or _is_future_annotations(node):
            continue
        name = _assigned_name(node)
        if name is None:
            out.append(Problem("", (
                f"第 {node.lineno} 行不是受支持的声明：声明式评估器只能写单个名字的字面量赋值"
                "（不带类型标注），不能有函数、类、import 或其它语句"
            )))
            continue
        if name not in DECLARATION_NAMES:
            out.append(Problem(name, (
                f"第 {node.lineno} 行给未约定的名字 {name} 赋值；声明式评估器只能声明模板里的 "
                f"{len(DECLARATION_NAMES)} 个名字"
            )))
            continue
        if name in assigned:
            out.append(Problem(name, f"{name} 被重复赋值（第 {node.lineno} 行）"))
            continue
        assigned.add(name)
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            out.append(Problem(name, (
                f"{name} 须为字面量（第 {node.lineno} 行）：不能引用变量、调用函数或做运算"
            )))
            continue
        duplicates = _duplicate_keys(node.value)
        if duplicates:
            out.append(Problem(name, f"{name} 含重复的键：{_names(map(repr, duplicates))}"))
            continue
        values[name] = value
    return values, assigned


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _strings(item)


def _text_problem(value: Any, *, graph: bool = False) -> str:
    """不是规范文本的原因；合格返回空串。graph=True 时用于图谱 Label 与属性名，不能含反引号。"""
    if not isinstance(value, str) or not value:
        return "须为非空字符串"
    if value != value.strip():
        return "首尾不能有空白"
    if len(value) > MAX_DECLARED_TEXT_LENGTH:
        return f"不能超过 {MAX_DECLARED_TEXT_LENGTH} 个字符"
    if any(unicodedata.category(character).startswith("C") for character in value):
        return "不能含控制字符"
    if graph and "`" in value:
        return "不能含反引号"
    return ""


def _check_text(
    out: list[Problem], field: str, value: Any, *, graph: bool = False, what: str = ""
) -> bool:
    reason = _text_problem(value, graph=graph)
    if reason:
        out.append(Problem(field, f"{what or field}{reason}，当前为 {_preview(value)}"))
    return not reason


def _check_text_mapping(out: list[Problem], field: str, value: Any) -> dict[str, str] | None:
    """文本到文本的非空字典；任一项不合格时返回 None，依赖它的交叉检查随之跳过。"""
    if not isinstance(value, Mapping) or not value:
        out.append(Problem(field, f"{field} 须为非空字典"))
        return None
    valid = True
    for key, item in value.items():
        key_ok = _check_text(out, field, key, what=f"{field} 的键")
        item_ok = _check_text(out, field, item, what=f"{field}[{key!r}]")
        valid = valid and key_ok and item_ok
    return dict(value) if valid else None


def _check_text_collection(out: list[Problem], field: str, value: Any) -> None:
    if not isinstance(value, (list, tuple, set, frozenset)):
        out.append(Problem(field, f"{field} 须为字符串列表（可以为空）"))
        return
    items = sorted(value, key=repr) if isinstance(value, (set, frozenset)) else value
    for item in items:
        _check_text(out, field, item, what=f"{field} 的元素")


def _is_positive_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return False
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return False
    return parsed.is_finite() and parsed > 0


def _check_capacities(out: list[Problem], value: Any) -> set[str] | None:
    """RULE_DAILY_CAPACITIES 的键集合，供 unit_capacity_keys 核对；不是字典时返回 None。"""
    name = "RULE_DAILY_CAPACITIES"
    if not isinstance(value, Mapping) or not value:
        out.append(Problem(name, f"{name} 须为非空字典：能力项名 → 日处理能力（正数）"))
        return None
    for key, item in value.items():
        _check_text(out, name, key, what=f"{name} 的键")
        if not _pending(item) and not _is_positive_number(item):
            out.append(Problem(name, f"{name}[{key!r}] 须为正数，当前为 {_preview(item)}"))
    return set(value)


def _check_capacity_keys(
    out: list[Problem], value: Any, capacities: set[str] | None
) -> dict[str, str] | None:
    """校验 unit_capacity_keys，返回 diesel_by_unit；无法确定时返回 None。"""
    field = f"{EVALUATOR_CONTEXT_SYMBOL}.unit_capacity_keys"
    expected = EVALUATOR_CONTEXT_KEYS["unit_capacity_keys"]
    if not isinstance(value, Mapping):
        out.append(Problem(field, f"{field} 须为字典，键为 {'、'.join(expected)}"))
        return None
    missing = [key for key in expected if key not in value]
    extra = sorted((key for key in value if key not in expected), key=repr)
    if missing:
        out.append(Problem(field, f"{field} 缺少键：{'、'.join(missing)}", KIND_CONTEXT))
    if extra:
        out.append(Problem(field, f"{field} 含不支持的键：{_names(extra)}"))
    referenced: list[tuple[str, Any]] = []
    for key in ("atmospheric", "vgo"):
        if key in value and _check_text(out, f"{field}.{key}", value[key]):
            referenced.append((f"{field}.{key}", value[key]))
    diesel = None
    if "diesel_by_unit" in value:
        diesel = _check_text_mapping(out, f"{field}.diesel_by_unit", value["diesel_by_unit"])
    for unit, capacity_key in (diesel or {}).items():
        referenced.append((f"{field}.diesel_by_unit[{unit!r}]", capacity_key))
    if capacities is not None:
        for where, capacity_key in referenced:
            if not _pending(capacity_key) and capacity_key not in capacities:
                out.append(Problem(where, (
                    f"{where} 指向的能力项 {capacity_key!r} 不在 RULE_DAILY_CAPACITIES 中"
                )))
    return diesel


def _check_distinct_pair(
    out: list[Problem], field: str, value: Any, keys: tuple[str, str]
) -> None:
    pair = _check_text_mapping(out, field, value)
    if pair is not None and (set(pair) != set(keys) or len(set(pair.values())) != 2):
        out.append(Problem(field, f"{field} 须恰好包含 {'、'.join(keys)} 两个键，且取值互不相同"))


def _check_solver_context(
    out: list[Problem],
    value: Any,
    diesel_unit_ids: dict[str, str] | None,
    capacities: set[str] | None,
) -> dict[str, str] | None:
    """SOLVER_CONTEXT 按 Consumer 声明校验与 worker 规则校验；返回修正系数前缀（无法确定时 None）。"""
    name = EVALUATOR_CONTEXT_SYMBOL
    if not isinstance(value, Mapping):
        out.append(Problem(name, f"{name} 须为字典", KIND_CONTEXT))
        return None
    required = (*EVALUATOR_CONTEXT_KEYS, *DECLARATIVE_CONTEXT_KEYS)
    missing = [key for key in required if key not in value]
    allowed = set(required) | set(OPTIONAL_CONTEXT_KEYS)
    extra = sorted((key for key in value if key not in allowed), key=repr)
    if missing:
        out.append(Problem(name, f"{name} 缺少兼容层要求的键：{'、'.join(missing)}", KIND_CONTEXT))
    if extra:
        out.append(Problem(name, f"{name} 含不支持的键：{_names(extra)}"))
    if "option_routes" in value:
        _check_distinct_pair(out, f"{name}.option_routes", value["option_routes"], ROUTE_OPTIONS)
    if "special_tanks" in value:
        _check_distinct_pair(out, f"{name}.special_tanks", value["special_tanks"], ("diesel", "vgo"))
    if "route_stream_products" in value:
        field = f"{name}.route_stream_products"
        streams = value["route_stream_products"]
        if not isinstance(streams, (list, tuple)) or not streams:
            out.append(Problem(field, f"{field} 须为非空字符串列表"))
        else:
            valid = [_check_text(out, field, item, what=f"{field} 的元素") for item in streams]
            if all(valid) and len(set(streams)) != len(streams):
                out.append(Problem(field, f"{field} 不能有重复项"))
    if value.get("atmospheric_product_tanks"):
        _check_text_mapping(out, f"{name}.atmospheric_product_tanks", value["atmospheric_product_tanks"])
    condition = value.get("production_condition", PRODUCTION_CONDITION)
    if not _pending(condition) and condition != PRODUCTION_CONDITION:
        field = f"{name}.production_condition"
        out.append(Problem(field, (
            f"{field} 须为 {PRODUCTION_CONDITION!r}（Consumer 求解引擎只支持这一个工况），当前为 {_preview(condition)}"
        )))
    yield_unit = value.get("yield_unit", YIELD_UNITS[0])
    if not _pending(yield_unit) and yield_unit not in YIELD_UNITS:
        field = f"{name}.yield_unit"
        out.append(Problem(field, (
            f"{field} 须为 {' 或 '.join(map(repr, YIELD_UNITS))}（收率列按百分数还是小数书写），"
            f"当前为 {_preview(yield_unit)}"
        )))
    diesel_capacity_keys = None
    if "unit_capacity_keys" in value:
        diesel_capacity_keys = _check_capacity_keys(out, value["unit_capacity_keys"], capacities)
    prefixes = None
    if "correction_factor_prefix_by_unit" in value:
        prefixes = _check_text_mapping(
            out, f"{name}.correction_factor_prefix_by_unit", value["correction_factor_prefix_by_unit"]
        )
    for unit in diesel_unit_ids or {}:
        if _pending(unit):
            continue
        for where, mapping in (
            ("unit_capacity_keys.diesel_by_unit", diesel_capacity_keys),
            ("correction_factor_prefix_by_unit", prefixes),
        ):
            if mapping is not None and unit not in mapping:
                out.append(Problem(f"{name}.{where}", (
                    f"DIESEL_UNIT_IDS 的键 {unit!r} 在 {name}.{where} 中没有对应项"
                )))
    return prefixes


def _check_correction_factors(
    out: list[Problem], value: Any, prefixes: dict[str, str] | None
) -> None:
    name = "CORRECTION_FACTORS"
    if not isinstance(value, Mapping) or not value:
        out.append(Problem(name, f"{name} 须为非空字典：来源名 → 修正系数"))
        return
    expected = None
    if prefixes is not None and not any(_pending(prefix) for prefix in prefixes.values()):
        expected = set(_SHARED_CORRECTION_FACTORS)
        for prefix in prefixes.values():
            expected.update((f"{prefix}_load_a", f"{prefix}_product_b"))
    seen: dict[str, Any] = {}
    for raw_name, factors in value.items():
        entry = f"{name}[{raw_name!r}]"
        if _check_text(out, name, raw_name, what=f"{name} 的键") and not _pending(raw_name):
            normalized = normalize_source_name(raw_name)
            if not normalized:
                out.append(Problem(name, f"{name} 的键 {raw_name!r} 归一化（NFKC 并去空白）后为空"))
            elif normalized in seen:
                out.append(Problem(name, (
                    f"{name} 的键 {raw_name!r} 与 {seen[normalized]!r} 归一化（NFKC 并去空白）后相同"
                )))
            seen.setdefault(normalized, raw_name)
        if not isinstance(factors, Mapping):
            out.append(Problem(entry, f"{entry} 须为字典：系数名 → 正数"))
            continue
        if expected is not None and set(factors) != expected:
            parts = []
            if expected - set(factors):
                parts.append(f"缺少 {_names(sorted(expected - set(factors)))}")
            if set(factors) - expected:
                parts.append(f"多出 {_names(sorted(set(factors) - expected, key=repr))}")
            out.append(Problem(entry, (
                f"{entry} 须恰好包含每个修正前缀的 _load_a、_product_b 两项及 "
                f"{'、'.join(_SHARED_CORRECTION_FACTORS)}：{'；'.join(parts)}"
            )))
        invalid = [
            key for key, item in factors.items() if not _pending(item) and not _is_positive_number(item)
        ]
        if invalid:
            out.append(Problem(entry, f"{entry} 的系数须为正数：{_names(sorted(invalid, key=repr))}"))


class _Lookup(NamedTuple):
    """一列的 lookup 声明；不合格的部分为 None 或从 match 中略去，引用检查随之跳过。"""

    label: str | None
    match: dict[str, str]
    field: str | None


class _TableRefs(NamedTuple):
    """一张逻辑表引用的对象与属性，供本体引用检查。"""

    label: str | None
    fields: dict[str, str]
    lookups: dict[str, _Lookup]


def _check_lookup(out: list[Problem], where: str, raw: Mapping[Any, Any]) -> _Lookup | None:
    if set(raw) != LOOKUP_KEYS:
        out.append(Problem(where, f"{where} 的关联须写成 {LOOKUP_SHAPE}"))
        return None
    label = raw["lookup"] if _check_text(out, f"{where}.lookup", raw["lookup"], graph=True) else None
    field = raw["field"] if _check_text(out, f"{where}.field", raw["field"], graph=True) else None
    match: dict[str, str] = {}
    raw_match = raw["match"]
    if not isinstance(raw_match, Mapping) or not raw_match:
        out.append(Problem(f"{where}.match", f"{where}.match 须为非空字典：本对象属性 → 关联对象属性"))
        return _Lookup(label, match, field)
    for key, lookup_key in raw_match.items():
        key_ok = _check_text(out, f"{where}.match", key, graph=True, what=f"{where}.match 的键")
        value_ok = _check_text(out, f"{where}.match", lookup_key, graph=True, what=f"{where}.match[{key!r}]")
        if key_ok and value_ok:
            match[key] = lookup_key
    return _Lookup(label, match, field)


def _lookup_identity(lookup: _Lookup) -> str | None:
    """两列声明了完全相同的关联时用于查重；声明不完整或含占位符时返回 None。"""
    parts = [lookup.label, lookup.field, *lookup.match, *lookup.match.values()]
    if not lookup.match or any(part is None or _pending(part) for part in parts):
        return None
    pairs = "、".join(f"{key}={value}" for key, value in lookup.match.items())
    return f"{lookup.label}.{lookup.field}（按 {pairs} 关联）"


def _check_table_source(
    out: list[Problem],
    table: str,
    raw: Any,
    columns: TableColumns,
    yield_columns: dict[str, str] | None,
) -> _TableRefs | None:
    """校验一张逻辑表的映射，返回引用的对象与属性供本体引用检查；结构不对返回 None。"""
    field = f"{TABLE_SOURCES_SYMBOL}.{table}"
    if not isinstance(raw, Mapping) or set(raw) != {"object", "fields"}:
        out.append(Problem(field, f"{field} 须写成 {{\"object\": 对象, \"fields\": {{逻辑列: 属性或关联}}}}"))
        return None
    label = raw["object"] if _check_text(out, f"{field}.object", raw["object"], graph=True) else None
    raw_fields = raw["fields"]
    if not isinstance(raw_fields, Mapping) or not raw_fields:
        out.append(Problem(f"{field}.fields", f"{field}.fields 须为非空字典：逻辑列 → 对象属性"))
        return _TableRefs(label, {}, {})
    fixed = columns.fixed_columns()
    # 收率常量无效时收率列未知：只跳过依赖收率列的检查
    yields: set[str] | None = set()
    if columns.yield_constants:
        yields = {yield_columns[n] for n in columns.yield_constants} if yield_columns else None
    if yields and fixed & yields:
        out.append(Problem(field, (
            f"收率常量指向的列 {_names(sorted(fixed & yields))} 与 {table} 的固定列重名，请换一个列名"
        )))
    fields: dict[str, str] = {}
    lookups: dict[str, _Lookup] = {}
    for column, key in raw_fields.items():
        where = f"{field}.fields[{column!r}]"
        if yields is not None and column not in fixed | yields:
            out.append(Problem(where, (
                f"{field}.fields 含不支持的逻辑列 {column!r}；可用：{'、'.join(sorted(fixed | yields))}"
            )))
        elif isinstance(key, Mapping):
            lookup = _check_lookup(out, where, key)
            if lookup is not None:
                lookups[column] = lookup
        elif _check_text(out, where, key, graph=True):
            fields[column] = key
    required = set(columns.required) | (yields or set())
    missing = sorted(required - set(raw_fields))
    if missing:
        out.append(Problem(f"{field}.fields", f"{field}.fields 缺少必填逻辑列：{'、'.join(missing)}"))
    if columns.any_of and not set(columns.any_of) & set(raw_fields):
        out.append(Problem(f"{field}.fields", f"{field}.fields 须至少映射 {'、'.join(columns.any_of)} 之一"))
    mapped = [key for key in fields.values() if not _pending(key)]
    mapped += [identity for identity in map(_lookup_identity, lookups.values()) if identity]
    duplicated = sorted({key for key in mapped if mapped.count(key) > 1})
    if duplicated:
        out.append(Problem(f"{field}.fields", f"{field}.fields 把同一属性映射了多次：{_names(duplicated)}"))
    return _TableRefs(label, fields, lookups)


def _check_table_sources(
    out: list[Problem], value: Any, yield_columns: dict[str, str] | None
) -> dict[str, _TableRefs]:
    name = TABLE_SOURCES_SYMBOL
    if not isinstance(value, Mapping):
        out.append(Problem(name, f"{name} 须为字典：逻辑表 → {{object, fields}}"))
        return {}
    missing = [table for table in TABLE_COLUMNS if table not in value]
    extra = sorted((table for table in value if table not in TABLE_COLUMNS), key=repr)
    if missing:
        out.append(Problem(name, f"{name} 缺少逻辑表：{'、'.join(missing)}"))
    if extra:
        out.append(Problem(name, f"{name} 含不支持的逻辑表：{_names(extra)}；只能是 {'、'.join(TABLE_COLUMNS)}"))
    tables = {}
    for table, columns in TABLE_COLUMNS.items():
        if table in value:
            source = _check_table_source(out, table, value[table], columns, yield_columns)
            if source is not None:
                tables[table] = source
    return tables


def _is_numeric_type(type_name: str) -> bool:
    lowered = type_name.lower()
    return any(marker in lowered for marker in NUMERIC_TYPE_MARKERS)


def _known_object(out: list[Problem], where: str, label: str | None, known: Mapping[str, Any]) -> bool:
    """对象存在时返回 True；不存在时记一条引用问题。未填或不合格的对象跳过且返回 False。"""
    if label is None or _pending(label):
        return False
    if label not in known:
        out.append(Problem(where, (
            f"{where} 指向的对象 {label!r} 不存在；填 ontology/object/ 下对象文档的文件名（即图谱 Label）"
        ), KIND_REFERENCE))
        return False
    return True


def _attribute_type(
    out: list[Problem], where: str, label: str, key: str, attributes: Mapping[str, str]
) -> str | None:
    """属性存在时返回其类型；不存在时记一条引用问题。占位符跳过，返回 None。"""
    if _pending(key):
        return None
    if key not in attributes:
        out.append(Problem(where, (
            f"{where} 指向的属性 {key!r} 不在对象 {label} 中；填对象文档字段表里的英文名称"
        ), KIND_REFERENCE))
        return None
    return attributes[key]


def _check_numeric_id(
    out: list[Problem], where: str, column: str, label: str, key: str, type_name: str
) -> None:
    if column in ID_COLUMNS and _is_numeric_type(type_name):
        out.append(Problem(where, (
            f"{where} 是 ID 列，但属性 {label}.{key} 的类型为「{type_name}」："
            "图谱导入后 ID 会变成数字并丢失前导零，与路线常量匹配失败；须改为字符串类型"
        )))


def _check_lookup_references(
    out: list[Problem],
    where: str,
    column: str,
    table: tuple[str, Mapping[str, str]],
    lookup: _Lookup,
    known: Mapping[str, Mapping[str, str]],
) -> None:
    if not _known_object(out, f"{where}.lookup", lookup.label, known):
        return
    label, attributes = table
    related = known[lookup.label]
    for key, lookup_key in lookup.match.items():
        own_type = _attribute_type(out, f"{where}.match", label, key, attributes)
        related_type = _attribute_type(out, f"{where}.match", lookup.label, lookup_key, related)
        if own_type is None or related_type is None:
            continue
        if _is_numeric_type(own_type) != _is_numeric_type(related_type):
            out.append(Problem(f"{where}.match", (
                f"{where}.match 用 {label}.{key}（{own_type}）匹配 {lookup.label}.{lookup_key}（{related_type}）："
                "取数按原值等值匹配、不做类型转换，数字与文本永远匹配不上；须把两边改为同一类型"
            )))
    if lookup.field is not None:
        type_name = _attribute_type(out, f"{where}.field", lookup.label, lookup.field, related)
        if type_name is not None:
            _check_numeric_id(out, f"{where}.field", column, lookup.label, lookup.field, type_name)


def _check_references(
    out: list[Problem],
    tables: Mapping[str, _TableRefs],
    objects: Mapping[str, Iterable[Mapping[str, Any]]],
) -> None:
    """核对 TABLE_SOURCES 引用的对象与属性（含 lookup 关联对象及其属性）在本体里存在。"""
    known: dict[str, dict[str, str]] = {}
    for object_key, fields in objects.items():
        known[object_key] = {
            str(item["key"]): str(item.get("type") or "") for item in fields if item.get("key")
        }
    for table, refs in tables.items():
        field = f"{TABLE_SOURCES_SYMBOL}.{table}"
        if not _known_object(out, f"{field}.object", refs.label, known):
            continue
        attributes = known[refs.label]
        for column, key in refs.fields.items():
            where = f"{field}.fields[{column!r}]"
            type_name = _attribute_type(out, where, refs.label, key, attributes)
            if type_name is not None:
                _check_numeric_id(out, where, column, refs.label, key, type_name)
        for column, lookup in refs.lookups.items():
            where = f"{field}.fields[{column!r}]"
            _check_lookup_references(out, where, column, (refs.label, attributes), lookup, known)


def _check_disjoint_products(out: list[Problem], values: Mapping[str, Any]) -> None:
    """常压柴油进料、加氢裂化进料与路线流股三组产品名不能重叠：同一产品名出现在两组里时，
    Consumer 只按先匹配到的角色计算，另一组悄悄落空。不是列表的组由各自的检查报告。"""
    context = values.get(EVALUATOR_CONTEXT_SYMBOL)
    groups = {
        "ATMOS_DIESEL_FEED": values.get("ATMOS_DIESEL_FEED"),
        "ATMOS_HC_FEED": values.get("ATMOS_HC_FEED"),
        f"{EVALUATOR_CONTEXT_SYMBOL}.route_stream_products": (
            context.get("route_stream_products") if isinstance(context, Mapping) else None
        ),
    }
    owners: dict[str, str] = {}
    for group, names in groups.items():
        if not isinstance(names, (list, tuple, set, frozenset)):
            continue
        items = sorted(names, key=repr) if isinstance(names, (set, frozenset)) else names
        for product in items:
            if not isinstance(product, str) or _pending(product):
                continue
            owner = owners.setdefault(product, group)
            if owner != group:
                out.append(Problem(group, (
                    f"{owner} 与 {group} 都列出了 {product!r}：同一产品只能承担一种角色，"
                    "否则 Consumer 只按先匹配到的角色计算"
                )))


def check_declarative(
    source: bytes | None,
    objects: Mapping[str, Iterable[Mapping[str, Any]]] | None = None,
) -> list[Problem]:
    """校验声明式评估器。objects 为 {对象 key: [{key, type}, ...]}，给出时再核对本体引用。"""
    module = _parse(source)
    if module is None:
        return [Problem("", UNPARSEABLE_MESSAGE)]
    out: list[Problem] = []
    values, assigned = _declarations(module, out)
    missing = [n for n in DECLARATION_NAMES if n not in assigned and n != EVALUATOR_CONTEXT_SYMBOL]
    if missing:
        out.append(Problem("、".join(missing), f"声明式评估器缺少：{'、'.join(missing)}"))
    if EVALUATOR_CONTEXT_SYMBOL not in assigned:
        out.append(Problem(
            EVALUATOR_CONTEXT_SYMBOL, f"评估器未在模块顶层导出 {EVALUATOR_CONTEXT_SYMBOL}", KIND_CONTEXT
        ))
    for name in DECLARATION_NAMES:
        pending = sorted({text for text in _strings(values.get(name)) if _pending(text)})
        if pending:
            out.append(Problem(name, (
                f"{name} 仍有未填写的模板占位符：{_names(pending)}；"
                "按本体文档与业务知识填写，文档里没有的值向用户确认"
            )))
    for name in _UNIT_ID_NAMES:
        if name in values:
            _check_text(out, name, values[name])
    yield_columns: dict[str, str] | None = {}
    for name in _YIELD_NAMES:
        if name in values and _check_text(out, name, values[name], graph=True):
            yield_columns[name] = values[name]
    if len(yield_columns) != len(_YIELD_NAMES):
        yield_columns = None
    for name in _FEED_NAMES:
        if name in values:
            _check_text_collection(out, name, values[name])
    diesel_unit_ids = None
    if "DIESEL_UNIT_IDS" in values:
        diesel_unit_ids = _check_text_mapping(out, "DIESEL_UNIT_IDS", values["DIESEL_UNIT_IDS"])
    capacities = None
    if "RULE_DAILY_CAPACITIES" in values:
        capacities = _check_capacities(out, values["RULE_DAILY_CAPACITIES"])
    prefixes = None
    if EVALUATOR_CONTEXT_SYMBOL in values:
        prefixes = _check_solver_context(out, values[EVALUATOR_CONTEXT_SYMBOL], diesel_unit_ids, capacities)
    if "CORRECTION_FACTORS" in values:
        _check_correction_factors(out, values["CORRECTION_FACTORS"], prefixes)
    _check_disjoint_products(out, values)
    tables = {}
    if TABLE_SOURCES_SYMBOL in values:
        tables = _check_table_sources(out, values[TABLE_SOURCES_SYMBOL], yield_columns)
    if objects is not None:
        _check_references(out, tables, objects)
    return out


# ---- CLI --------------------------------------------------------------------
# 对象文档的字段表解析与后端 package_mapping.parse_object_doc 同一口径（有一致性测试守护）

_FIELD_SECTION_NAMES = ("字段", "对象属性清单")
_KEY_HEADERS = ("英文名称", "字段 key", "字段key", "field key", "key")
_TYPE_HEADERS = ("类型",)
_EMPTY_MARKERS = {"", "无", "-", "—", "–", "n/a"}


def _is_empty_cell(value: str) -> bool:
    text = (value or "").strip()
    return text.lower() in _EMPTY_MARKERS or text.startswith("{")


def _section(text: str, name: str) -> str:
    lines = text.replace("\r\n", "\n").split("\n")
    start = next((i for i, line in enumerate(lines) if line.strip() == f"# {name}"), None)
    if start is None:
        return ""
    body: list[str] = []
    for line in lines[start + 1:]:
        if re.match(r"^#\s+", line):
            break
        body.append(line)
    return "\n".join(body).strip()


def _column_index(headers: list[str], names: tuple[str, ...]) -> int:
    normalized = [header.replace(" ", "").lower() for header in headers]
    for name in names:
        target = name.replace(" ", "").lower()
        if target in normalized:
            return normalized.index(target)
    return -1


def parse_object_fields(text: str) -> list[dict[str, str]]:
    """对象文档 → [{key, type}]；key 为属性英文名称，未写类型按「字符串」。"""
    body = next((s for s in (_section(text, n) for n in _FIELD_SECTION_NAMES) if s), "")
    rows = [line.strip() for line in body.split("\n") if line.strip().startswith("|")]
    if len(rows) < 2:
        return []
    cells = [[cell.strip() for cell in row.strip("|").split("|")] for row in rows]
    headers = cells[0]
    data = [row for row in cells[2:] if not all(set(cell) <= set("-: ") for cell in row)]
    key_index = _column_index(headers, _KEY_HEADERS)
    type_index = _column_index(headers, _TYPE_HEADERS)
    if key_index < 0:
        return []
    fields = []
    for row in data:
        key = row[key_index].strip() if key_index < len(row) else ""
        if _is_empty_cell(key):
            continue
        type_name = row[type_index].strip() if 0 <= type_index < len(row) else ""
        fields.append({"key": key, "type": type_name or "字符串"})
    return fields


def read_object_fields(ontology_dir: Path) -> dict[str, list[dict[str, str]]] | None:
    """ontology/object/*.md → {对象 key: 字段}；对象目录不存在返回 None。"""
    object_dir = ontology_dir / "object"
    if not object_dir.is_dir():
        return None
    objects = {}
    for doc in sorted(object_dir.glob("*.md")):
        if doc.name == "index.md":
            continue
        try:
            objects[doc.stem] = parse_object_fields(doc.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
    return objects


def _report(path: str, kind: str | None, problems: list[Problem]) -> dict[str, Any]:
    errors = sum(1 for problem in problems if problem.severity == SEVERITY_ERROR)
    return {
        "file": path,
        "kind": kind,
        "ok": errors == 0,
        "errors": errors,
        "warnings": len(problems) - errors,
        "problems": [problem.as_dict() for problem in problems[:MAX_REPORTED_PROBLEMS]],
        "truncated": len(problems) > MAX_REPORTED_PROBLEMS,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验声明式求解评估器（只解析，不执行）")
    parser.add_argument("file", help="评估器文件，如 ontology/scripts/<文件>.py")
    parser.add_argument("--ontology-dir", default="ontology", help="本体目录，用于核对对象与属性引用")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args(argv)

    try:
        source = Path(args.file).read_bytes()
    except OSError as exc:
        source, read_error = None, f"读取失败：{exc.strerror or exc}"
    else:
        read_error = ""
    kind = evaluator_kind(source)
    if read_error or kind == LEGACY_KIND:
        message = read_error or "这是旧式评估器（定义了 BatchPlan 等函数），本命令不校验也不改写旧式评估器"
        report = _report(args.file, kind, [Problem("", message)])
        exit_code = 2
    else:
        objects = read_object_fields(Path(args.ontology_dir))
        problems = check_declarative(source, objects)
        if objects is None:
            problems.append(Problem("", (
                f"未找到对象目录 {Path(args.ontology_dir) / 'object'}，未核对对象与属性引用"
            ), severity=SEVERITY_WARNING))
        report = _report(args.file, kind, problems)
        exit_code = 0 if report["ok"] else 1

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        status = "通过" if report["ok"] else "未通过"
        print(f"{args.file}：{status}（错误 {report['errors']}，警告 {report['warnings']}）")
        for problem in report["problems"]:
            label = "错误" if problem["severity"] == SEVERITY_ERROR else "警告"
            print(f"- [{label}] {problem['message']}")
        if report["truncated"]:
            print(f"- 仅列出前 {MAX_REPORTED_PROBLEMS} 条，修正后重新运行查看其余问题")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
