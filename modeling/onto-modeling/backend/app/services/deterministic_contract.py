"""确定性 Operation 的建模态受管契约、静态检查与发布投影。

工作区中的事实源位于 ``ext/deterministic-execution/contracts/<key>/``。它只声明
``none`` 或 ``workspace_provider``，不保存数据库类型、连接地址或凭据。发布器从冻结
提交读取这些文件，并转换成 Consumer schema 0.1 认识的执行声明。
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import re
import unicodedata
import yaml
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .workspace.errors import WorkspaceError

CONTRACT_DIR = "ext/deterministic-execution/contracts"
CONTRACT_FILE = "contract.json"
PARSER_FILE = "parser.py"
INPUT_SCHEMA_FILE = "input.schema.json"
OUTPUT_SCHEMA_FILE = "output.schema.json"
TESTS_FILE = "tests.json"
VERIFICATION_FILE = "verification.json"
REQUIRED_CONTRACT_FILES = frozenset({
    CONTRACT_FILE, PARSER_FILE, INPUT_SCHEMA_FILE, OUTPUT_SCHEMA_FILE, TESTS_FILE,
})
CONTRACT_FILES = REQUIRED_CONTRACT_FILES | {VERIFICATION_FILE}
SCHEMA_VERSION = "deterministic-contract.v1"
DATA_ACCESS_VALUES = ("none", "workspace_provider")
# Consumer deterministic Provider Broker v1 的只读能力契约。Onto 不导入
# Consumer 私有模块，避免部署耦合；跨侧测试负责锁定两份能力表一致。
PROVIDER_CAPABILITY_VERSION = 1
PROVIDER_OPERATION_ARGUMENTS = {
    "get_nodes_schema": frozenset({"element_types"}),
    "get_edges_schema": frozenset({"relation_types"}),
    "property_filter": frozenset({
        "element_class", "element_type", "filter_dict", "return_properties", "limit", "offset",
    }),
    "count_search": frozenset({"element_class", "element_type", "filter_dict"}),
    "aggregate_search": frozenset({
        "element_class", "element_type", "target_property", "agg", "filter_dict",
    }),
    "sorted_search": frozenset({
        "element_class", "element_type", "filter_dict", "return_properties", "sort_by",
        "ascending", "limit", "offset",
    }),
    "pattern_search": frozenset({"path_pattern", "return_vars", "limit", "return_properties"}),
    "hop_search": frozenset({"entity_instance_id", "hop_num", "accurate_flag"}),
    "shortest_path_search": frozenset({
        "start_label", "start_props", "end_label", "end_props", "max_depth", "directed",
    }),
    "list_logic_titles": frozenset({"entity_type"}),
    "get_logic_content": frozenset({"entity_instance_ids", "logic_titles"}),
    "get_element_info": frozenset({"element_instance_id", "element_class", "element_type"}),
}
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
HANDLER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
MAX_PARSER_BYTES = 256 * 1024
MAX_SCHEMA_BYTES = 256 * 1024
MAX_TESTS_BYTES = 256 * 1024
MAX_CONTRACTS = 128
MAX_TEST_VECTORS = 16
MAX_TERM_COUNT = 64
MAX_TERM_LENGTH = 256
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 100_000

CODE_INVALID = "deterministic_contract_invalid"
CODE_REFERENCE_MISSING = "deterministic_contract_reference_missing"
CODE_CONFLICT = "deterministic_solver_conflict"
CODE_UNVERIFIED = "deterministic_contract_unverified"

_FORBIDDEN_IMPORTS = frozenset({
    "asyncio", "concurrent", "ctypes", "datetime", "glob", "http", "importlib", "inspect",
    "marshal", "multiprocessing", "os", "pathlib", "pickle", "platform", "random", "requests",
    "resource", "secrets", "signal", "socket", "subprocess", "sys", "tempfile", "threading",
    "time", "urllib", "uuid",
})
_FORBIDDEN_CALLS = frozenset({
    "__import__", "breakpoint", "compile", "delattr", "dir", "eval", "exec", "getattr",
    "globals", "id", "input", "locals", "open", "setattr", "vars",
})
_FORBIDDEN_ATTRIBUTES = frozenset({
    "__base__", "__bases__", "__builtins__", "__class__", "__closure__", "__code__",
    "__dict__", "__getattr__", "__getattribute__", "__globals__", "__mro__", "__reduce__",
    "__reduce_ex__", "__subclasses__",
})
_NONDETERMINISTIC_CALL_PREFIXES = ("random.", "secrets.", "tempfile.", "uuid.")
_NONDETERMINISTIC_CALLS = frozenset({
    "argparse.FileType", "datetime.date.today", "datetime.datetime.now", "datetime.datetime.today",
    "datetime.datetime.utcnow", "importlib.import_module", "io.FileIO", "io.open", "io.open_code",
    "operator.attrgetter", "time.monotonic", "time.monotonic_ns", "time.perf_counter",
    "time.perf_counter_ns", "time.process_time", "time.process_time_ns", "time.time", "time.time_ns",
})
_ALLOWED_STDLIB_IMPORTS = frozenset({
    "__future__", "argparse", "base64", "binascii", "bisect", "collections", "collections.abc",
    "csv", "dataclasses", "decimal", "enum", "fractions", "functools", "hashlib", "heapq",
    "hmac", "html", "html.entities", "html.parser", "itertools", "json", "math", "operator",
    "re", "statistics", "string", "textwrap", "typing", "unicodedata", "urllib.parse",
})
_RUNTIME_MODULE = "shared_capabilities.ontology_script_sdk"
_PROVIDER_EXPORTS = frozenset({"DeterministicProviderCallError", "DeterministicProviderClient"})
_PROVIDER_CLIENT = "DeterministicProviderClient"
_CONSUMER_RUNTIME_ARGUMENT_FIELDS = frozenset({
    "ontology_id", "ontology_version", "run_id", "selected_ontology_id",
    "selected_ontology_version", "selected_version", "session_id", "turn_id",
    "user_id", "workspace_id",
})


class DeterministicContractRejected(ValueError):
    def __init__(self, findings: list[dict]):
        self.findings = findings
        super().__init__(findings[0]["message"] if findings else "确定性执行契约不合法")


@dataclass(frozen=True)
class DeterministicContract:
    key: str
    operation: str
    handler: str
    data_access: str
    aliases: tuple[str, ...] = ()
    routing_all_terms: tuple[str, ...] = ()
    routing_any_terms: tuple[str, ...] = ()
    enabled: bool = True
    parser: bytes = b""
    input_schema: bytes = b""
    output_schema: bytes = b""
    tests: bytes = b"[]\n"

    @property
    def rel_dir(self) -> str:
        return f"{CONTRACT_DIR}/{self.key}"


@dataclass
class DeterministicCheck:
    contracts: tuple[DeterministicContract, ...] = ()
    findings: list[dict] = field(default_factory=list)
    revision: str = ""

    @property
    def enabled(self) -> bool:
        return any(item.enabled for item in self.contracts)


def _finding(code: str, message: str, *, path: str = "", field: str = "") -> dict:
    item = {"code": code, "message": message}
    if path:
        item["path"] = path
    if field:
        item["field"] = field
    return item


def _normalize_token(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).strip().casefold().split())


def _invalid_unicode(value: str) -> bool:
    return any(0xD800 <= ord(character) <= 0xDFFF for character in value)


def _json_tree_error(value: Any, label: str) -> str:
    """迭代检查 JSON 树，避免深层或非法 Unicode 在序列化时才触发 500。"""
    stack = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES:
            return f"{label}节点数量不能超过 {MAX_JSON_NODES}"
        if depth > MAX_JSON_DEPTH:
            return f"{label}嵌套深度不能超过 {MAX_JSON_DEPTH} 层"
        if isinstance(current, str):
            if _invalid_unicode(current):
                return f"{label}包含非法 Unicode 代理字符"
            continue
        if isinstance(current, bool) or current is None:
            continue
        if isinstance(current, (int, float)):
            if isinstance(current, float) and not math.isfinite(current):
                return f"{label}包含超出有限范围的数值"
            continue
        if isinstance(current, Mapping):
            for key, child in current.items():
                if not isinstance(key, str):
                    return f"{label}的 object 字段名必须是字符串"
                if _invalid_unicode(key):
                    return f"{label}包含非法 Unicode 代理字符"
                stack.append((child, depth + 1))
            continue
        if isinstance(current, (list, tuple)):
            stack.extend((child, depth + 1) for child in current)
            continue
        return f"{label}包含不能表示为 JSON 的类型 {type(current).__name__}"
    return ""


def _json_equal(left: Any, right: Any) -> bool:
    """按 JSON 类型比较；number 内允许 0 与 0.0 等价，但不混淆 boolean。"""
    pending = [(left, right)]
    while pending:
        first, second = pending.pop()
        if isinstance(first, bool) or isinstance(second, bool):
            if not isinstance(first, bool) or not isinstance(second, bool) or first is not second:
                return False
            continue
        first_number = isinstance(first, (int, float)) and not isinstance(first, bool)
        second_number = isinstance(second, (int, float)) and not isinstance(second, bool)
        if first_number or second_number:
            if not first_number or not second_number or first != second:
                return False
            continue
        if type(first) is not type(second):
            return False
        if isinstance(first, Mapping):
            if set(first) != set(second):
                return False
            pending.extend((first[key], second[key]) for key in first)
        elif isinstance(first, list):
            if len(first) != len(second):
                return False
            pending.extend(zip(first, second))
        elif first != second:
            return False
    return True


def _json_text_depth_error(text: str, label: str) -> str:
    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                return f"{label}嵌套深度不能超过 {MAX_JSON_DEPTH} 层"
        elif character in "]}":
            depth -= 1
    return ""


def _strict_json(data: str | bytes, label: str) -> Any:
    text = data.decode("utf-8") if isinstance(data, bytes) else str(data)
    depth_error = _json_text_depth_error(text, label)
    if depth_error:
        raise ValueError(depth_error)

    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError(f"{label}包含重复字段 {key}")
            out[key] = value
        return out

    def reject_constant(value: str):
        raise ValueError(f"{label}包含非标准数值 {value}")

    def finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError(f"{label}包含超出有限范围的数值 {value}")
        return parsed

    try:
        value = json.loads(
            text,
            object_pairs_hook=pairs,
            parse_constant=reject_constant,
            parse_float=finite_float,
        )
        tree_error = _json_tree_error(value, label)
        if tree_error:
            raise ValueError(tree_error)
        return value
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"{label}不是合法 JSON") from exc


def _schema_findings(data: bytes, label: str, path: str) -> list[dict]:
    if not data:
        return [_finding(CODE_REFERENCE_MISSING, f"缺少{label}", path=path)]
    if len(data) > MAX_SCHEMA_BYTES:
        return [_finding(CODE_INVALID, f"{label}不能超过 {MAX_SCHEMA_BYTES // 1024} KiB", path=path)]
    try:
        value = _strict_json(data, label)
    except ValueError as exc:
        return [_finding(CODE_INVALID, str(exc), path=path)]
    if not isinstance(value, dict) or value.get("type") != "object":
        return [_finding(CODE_INVALID, f"{label}根节点必须是 object", path=path)]
    refs: list[str] = []
    nested_ids: list[str] = []

    def walk(item, *, root: bool = False):
        if isinstance(item, dict):
            for key, child in item.items():
                if key in ("$ref", "$dynamicRef") and isinstance(child, str):
                    refs.append(child)
                if key == "$id" and not root:
                    nested_ids.append(str(child))
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    def resolve_pointer(ref: str) -> Any:
        current: Any = value
        for raw_part in ref[2:].split("/"):
            part = raw_part.replace("~1", "/").replace("~0", "~")
            if isinstance(current, Mapping) and part in current:
                current = current[part]
            elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
                current = current[int(part)]
            else:
                return None
        return current

    walk(value, root=True)
    if next((ref for ref in refs if not ref.startswith("#/")), ""):
        return [_finding(CODE_INVALID, f"{label}必须自包含，$ref 只能引用同一文件内的 #/...", path=path)]
    unresolved = next((ref for ref in refs if resolve_pointer(ref) is None), "")
    if unresolved:
        return [_finding(CODE_INVALID, f"{label}包含无法解析的本地引用：{unresolved}", path=path)]
    invalid_target = next((
        ref for ref in refs
        if not isinstance(resolve_pointer(ref), (Mapping, bool))
    ), "")
    if invalid_target:
        return [_finding(
            CODE_INVALID,
            f"{label}的本地引用必须指向 object 或 boolean Schema：{invalid_target}",
            path=path,
        )]

    def direct_reference_target(schema: Any) -> Any:
        """解析不消耗实例层级的纯引用节点；属性/items 中的递归不在此列。"""
        if not isinstance(schema, Mapping):
            return None
        reference_keys = [key for key in ("$ref", "$dynamicRef") if key in schema]
        if len(reference_keys) != 1 or any(
            key not in {reference_keys[0], "$comment", "title", "description", "default", "examples"}
            for key in schema
        ):
            return None
        ref = schema.get(reference_keys[0])
        return resolve_pointer(ref) if isinstance(ref, str) and ref.startswith("#/") else None

    for ref in refs:
        current = resolve_pointer(ref)
        seen: set[int] = set()
        while isinstance(current, Mapping):
            marker = id(current)
            if marker in seen:
                return [_finding(
                    CODE_INVALID,
                    f"{label}包含不消耗实例结构的本地引用循环：{ref}",
                    path=path,
                )]
            seen.add(marker)
            current = direct_reference_target(current)
            if current is None:
                break
    if nested_ids:
        return [_finding(CODE_INVALID, f"{label}不能包含嵌套 $id", path=path)]
    try:
        Draft202012Validator.check_schema(value)
    except SchemaError as exc:
        return [_finding(CODE_INVALID, f"{label}不符合 JSON Schema Draft 2020-12：{exc.message}", path=path)]
    return []


def _root_schema_nodes(schema: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """返回 Consumer 会视为根参数约束的所有 Schema 节点。"""
    def resolve_local_ref(ref: Any) -> Any:
        value = str(ref or "")
        if value == "#":
            return schema
        if not value.startswith("#/"):
            return None
        current: Any = schema
        for raw_part in value[2:].split("/"):
            part = raw_part.replace("~1", "/").replace("~0", "~")
            if isinstance(current, Mapping) and part in current:
                current = current[part]
            elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
                current = current[int(part)]
            else:
                return None
        return current

    nodes: list[Mapping[str, Any]] = []
    pending: list[Any] = [schema]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if not isinstance(current, Mapping) or id(current) in seen:
            continue
        seen.add(id(current))
        nodes.append(current)
        for keyword in ("$ref", "$dynamicRef"):
            referenced = resolve_local_ref(current.get(keyword))
            if referenced is not None:
                pending.append(referenced)
        for keyword in ("allOf", "anyOf", "oneOf"):
            children = current.get(keyword)
            if isinstance(children, list):
                pending.extend(children)
        for keyword in ("then", "else"):
            child = current.get(keyword)
            if isinstance(child, Mapping):
                pending.append(child)
        dependent = current.get("dependentSchemas")
        if isinstance(dependent, Mapping):
            pending.extend(dependent.values())
    return tuple(nodes)


def _root_schema_argument_names(schema: Mapping[str, Any]) -> set[str]:
    """返回 Consumer 视为根业务参数的字段。"""
    names: set[str] = set()
    for current in _root_schema_nodes(schema):
        properties = current.get("properties")
        if isinstance(properties, Mapping):
            names.update(str(name) for name in properties)
        required = current.get("required")
        if isinstance(required, list):
            names.update(str(name) for name in required)
    return names


def _root_schema_required_names(schema: Mapping[str, Any]) -> set[str]:
    """返回每个通过 Schema 的根对象都必须具备的字段。"""
    def resolve_local_ref(ref: Any) -> Any:
        value = str(ref or "")
        if value == "#":
            return schema
        if not value.startswith("#/"):
            return None
        current: Any = schema
        for raw_part in value[2:].split("/"):
            part = raw_part.replace("~1", "/").replace("~0", "~")
            if isinstance(current, Mapping) and part in current:
                current = current[part]
            elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
                current = current[int(part)]
            else:
                return None
        return current

    def guaranteed(node: Any, resolving: frozenset[int] = frozenset()) -> set[str]:
        if not isinstance(node, Mapping) or id(node) in resolving:
            return set()
        active = resolving | {id(node)}
        required = node.get("required")
        result = {str(value) for value in required} if isinstance(required, list) else set()
        for keyword in ("$ref", "$dynamicRef"):
            referenced = resolve_local_ref(node.get(keyword))
            if referenced is not None:
                result.update(guaranteed(referenced, active))
        all_of = node.get("allOf")
        if isinstance(all_of, list):
            for child in all_of:
                result.update(guaranteed(child, active))
        for keyword in ("anyOf", "oneOf"):
            children = node.get(keyword)
            if isinstance(children, list) and children:
                branch_required = [guaranteed(child, active) for child in children]
                result.update(set.intersection(*branch_required))
        then_required = guaranteed(node.get("then"), active)
        else_required = guaranteed(node.get("else"), active)
        if then_required and else_required:
            result.update(then_required & else_required)
        return result

    return guaranteed(schema)


def _runtime_schema_findings(data: bytes, path: str) -> list[dict]:
    try:
        schema = _strict_json(data, "输入 Schema")
    except ValueError:
        return []
    if not isinstance(schema, Mapping):
        return []
    reserved = sorted((
        name for name in _root_schema_argument_names(schema)
        if name.replace("-", "_") in _CONSUMER_RUNTIME_ARGUMENT_FIELDS
    ), key=lambda item: item.encode("utf-8"))
    if not reserved:
        return []
    return [_finding(
        CODE_INVALID,
        "输入 Schema 不能声明 Consumer 保留的运行时参数：" + "、".join(reserved),
        path=path,
    )]


def _handler_schema_findings(script_contract: Any, data: bytes, path: str) -> list[dict]:
    """提前拦截 Consumer 构造 Handler CLI 时才会暴露的参数不一致。"""
    try:
        schema = _strict_json(data, "输入 Schema")
    except ValueError:
        return []
    if not isinstance(schema, Mapping):
        return []
    schema_names = _root_schema_argument_names(schema)

    def aliases(parameter: Mapping[str, Any]) -> set[str]:
        name = str(parameter.get("name") or "").strip()
        flag = str(parameter.get("flag") or "").strip().lstrip("-")
        values = {name, flag}
        return {
            candidate
            for value in values if value
            for candidate in (value, value.replace("-", "_"), value.replace("_", "-"))
        }

    business_parameters = []
    for parameter in script_contract.params:
        accepted = aliases(parameter)
        normalized = {value.replace("-", "_") for value in accepted}
        if normalized & _CONSUMER_RUNTIME_ARGUMENT_FIELDS:
            continue
        business_parameters.append((parameter, accepted))

    findings = []
    unmatched_schema = sorted(
        name for name in schema_names
        if not any(name in accepted for _parameter, accepted in business_parameters)
    )
    if unmatched_schema:
        findings.append(_finding(
            CODE_INVALID,
            "输入 Schema 字段没有对应的 Handler 参数：" + "、".join(unmatched_schema),
            path=path,
        ))

    required_schema = _root_schema_required_names(schema)
    missing_required = sorted(
        str(parameter.get("name") or "")
        for parameter, accepted in business_parameters
        if str(parameter.get("required") or "否") == "是" and not (accepted & required_schema)
    )
    if missing_required:
        findings.append(_finding(
            CODE_INVALID,
            "Handler 必填参数必须在输入 Schema required 中声明：" + "、".join(missing_required),
            path=path,
        ))
    return findings


def _python_findings(
    source: bytes, path: str, *, parser: bool, data_access: str = "none",
    local_modules: frozenset[str] = frozenset(), require_request_argument: bool = True,
) -> list[dict]:
    label = "Parser" if parser else "Handler"
    if not source:
        return [_finding(CODE_REFERENCE_MISSING, f"缺少{label}", path=path)]
    if len(source) > MAX_PARSER_BYTES:
        return [_finding(CODE_INVALID, f"{label}不能超过 {MAX_PARSER_BYTES // 1024} KiB", path=path)]
    try:
        text = source.decode("utf-8")
        tree = ast.parse(text, filename=path)
    except (UnicodeDecodeError, SyntaxError):
        return [_finding(CODE_INVALID, f"{label}不是合法 UTF-8 Python 文件", path=path)]
    findings = []
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name.split(".", 1)[0]] = item.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for item in node.names:
                if item.name != "*":
                    aliases[item.asname or item.name] = f"{node.module}.{item.name}"

    def call_name(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            parent = call_name(node.value)
            return f"{parent}.{node.attr}" if parent else node.attr
        return ""

    def nondeterministic(name: str) -> bool:
        return name in _NONDETERMINISTIC_CALLS or any(
            name.startswith(prefix) or f".{prefix}" in f".{name}" for prefix in _NONDETERMINISTIC_CALL_PREFIXES
        )

    request_json_argument = False
    for node in ast.walk(tree):
        roots = []
        if isinstance(node, ast.Import):
            roots = [item.name.split(".", 1)[0] for item in node.names]
        elif isinstance(node, ast.ImportFrom):
            roots = [str(node.module or "").split(".", 1)[0]]
        bad = next((root for root in roots if root in _FORBIDDEN_IMPORTS), "")
        if bad:
            findings.append(_finding(CODE_INVALID, f"{label}不能导入 {bad}", path=path))
            break
        if isinstance(node, ast.Import):
            unsupported = next((
                item.name for item in node.names
                if item.name not in _ALLOWED_STDLIB_IMPORTS
                and item.name.split(".", 1)[0] not in local_modules
            ), "")
            if unsupported:
                findings.append(_finding(
                    CODE_INVALID,
                    f"{label}导入的 {unsupported} 不在 Consumer 沙箱依赖契约中",
                    path=path,
                ))
                break
        elif isinstance(node, ast.ImportFrom):
            module = str(node.module or "")
            names = {item.name for item in node.names}
            if "*" in names:
                findings.append(_finding(CODE_INVALID, f"{label}不能使用通配符导入", path=path))
                break
            if module == _RUNTIME_MODULE:
                unsupported = names - _PROVIDER_EXPORTS
                if unsupported or parser or data_access != "workspace_provider":
                    capability = sorted(unsupported or names)[0] if names else module
                    findings.append(_finding(CODE_INVALID, f"{label}不能使用运行时能力 {capability}", path=path))
                    break
            elif (
                module not in _ALLOWED_STDLIB_IMPORTS
                and module.split(".", 1)[0] not in local_modules
            ):
                findings.append(_finding(
                    CODE_INVALID,
                    f"{label}导入的 {module or '相对模块'} 不在 Consumer 沙箱依赖契约中",
                    path=path,
                ))
                break
        if isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_ATTRIBUTES:
            findings.append(_finding(CODE_INVALID, f"{label}不能访问 {node.attr}", path=path))
            break
        if isinstance(node, ast.Name) and node.id == "__builtins__":
            findings.append(_finding(CODE_INVALID, f"{label}不能访问 __builtins__", path=path))
            break
        if isinstance(node, ast.Call):
            name = call_name(node.func)
            if name in _FORBIDDEN_CALLS or nondeterministic(name):
                findings.append(_finding(CODE_INVALID, f"{label}不能调用 {name}", path=path))
                break
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"
                and any(isinstance(arg, ast.Constant) and arg.value == "--request-json" for arg in node.args)
            ):
                request_json_argument = True
    if parser and require_request_argument and not request_json_argument:
        findings.append(_finding(CODE_INVALID, "Parser 必须声明 --request-json 参数", path=path))
    return findings


def _local_imports(source: bytes, local_modules: frozenset[str]) -> set[str]:
    try:
        tree = ast.parse(source.decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError):
        return set()
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [item.name.split(".", 1)[0] for item in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module.split(".", 1)[0]]
        else:
            continue
        found.update(name for name in names if name in local_modules)
    return found


def _python_closure_findings(
    entry_source: bytes, entry_path: str, *, parser: bool, data_access: str,
    module_sources: Mapping[str, tuple[str, bytes]], local_modules: frozenset[str],
) -> list[dict]:
    findings, pending, visited = [], [(entry_path, entry_source, True)], set()
    while pending:
        path, source, is_entry = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        findings += _python_findings(
            source, path, parser=parser, data_access=data_access, local_modules=local_modules,
            require_request_argument=is_entry,
        )
        for module in _local_imports(source, local_modules):
            dependency = module_sources.get(module)
            if dependency is not None and dependency[0] not in visited:
                pending.append((*dependency, False))
    return findings


def _operation_stems(root: Path, *, allow_symlinks: bool = True) -> set[str]:
    out = set()
    for rel in ("ontology/operation", "ontology/logic"):
        directory = root / rel
        if directory.is_dir():
            out.update(
                path.stem for path in directory.glob("*.md")
                if path.name != "index.md" and (allow_symlinks or not path.is_symlink())
            )
    return out


def _candidate_key(handler: str, used: set[str] | frozenset[str] = frozenset()) -> str:
    value = re.sub(r"[^a-z0-9_]+", "_", handler.casefold()).strip("_")
    if not value or not value[0].isalpha():
        value = f"operation_{value}" if value else "operation"
    value = value[:64]
    if value not in used:
        return value
    for index in range(2, MAX_CONTRACTS + 2):
        suffix = f"_{index}"
        candidate = value[:64 - len(suffix)] + suffix
        if candidate not in used:
            return candidate
    return value


def _script_contracts(root: Path) -> tuple[dict[str, Any], dict[str, str]]:
    """读取可作为确定性 Handler 的脚本契约，同时保留无效原因供候选解释。"""
    from .packs import contracts as contract_modules

    module = contract_modules.module_for(root, "script_contract")
    valid: dict[str, Any] = {}
    invalid: dict[str, str] = {}
    directory = root / "ontology" / "scripts"
    if not directory.is_dir():
        return valid, invalid
    for path in sorted(directory.glob("*.py")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            valid[path.stem] = module.parse_script(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            invalid[path.stem] = str(exc)
    return valid, invalid


def _handler_data_access(root: Path, handler: str) -> str:
    """按 Handler 的本地依赖闭包推导抽象数据能力，不绑定具体 Provider。"""
    scripts = root / "ontology" / "scripts"
    modules = {
        path.stem: path for path in scripts.glob("*.py")
        if path.is_file() and not path.is_symlink()
    } if scripts.is_dir() else {}
    pending = [handler]
    visited: set[str] = set()
    while pending:
        module = pending.pop()
        if module in visited or module not in modules:
            continue
        visited.add(module)
        try:
            source = modules[module].read_bytes()
            tree = ast.parse(source.decode("utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        if any(
            isinstance(node, ast.ImportFrom)
            and node.module == _RUNTIME_MODULE
            and any(alias.name == _PROVIDER_CLIENT for alias in node.names)
            for node in ast.walk(tree)
        ):
            return "workspace_provider"
        pending.extend(_local_imports(source, frozenset(modules)) - visited)
    return "none"


def candidate_scan(root: Path) -> dict:
    """以稳定四档结构扫描确定性候选，不用模型分数制造虚假精度。"""
    from . import solver_contract

    operations = sorted(_operation_stems(root), key=lambda value: value.encode("utf-8"))
    scripts, invalid_scripts = _script_contracts(root)
    scripts_dir = root / "ontology" / "scripts"
    module_sources = {
        path.stem: (str(path.relative_to(root)), path.read_bytes())
        for path in scripts_dir.glob("*.py") if path.is_file() and not path.is_symlink()
    } if scripts_dir.is_dir() else {}
    local_modules = frozenset(module_sources)
    by_operation: dict[str, list[tuple[str, Any]]] = {}
    for name, contract in scripts.items():
        if contract.bound:
            by_operation.setdefault(contract.bound, []).append((name, contract))
    solver_items, solver_problems = solver_contract.load_workspace_solver(root, operations)
    solver_operations = {item.operation for item in solver_items if item.enabled}
    loaded_contracts = load_contracts(root)[0]
    configured = {item.operation: item for item in loaded_contracts}
    candidates = []
    for operation in operations:
        handlers = by_operation.get(operation, [])
        reasons: list[str] = []
        blockers: list[str] = []
        confirmations: list[str] = []
        handler = handlers[0][0] if len(handlers) == 1 else ""
        data_access = "none"
        if operation in solver_operations:
            if operation in configured:
                blockers.append("该 Operation 已同时存在确定性契约和 solver_workflow，必须先解除冲突")
            else:
                blockers.append("该 Operation 已启用 solver_workflow，首版不能同时制作成确定性执行")
            recommendation = "not_recommended"
        elif operation in configured:
            existing = configured[operation]
            handler = existing.handler
            data_access = existing.data_access
            reasons.append("已经建立确定性执行契约，可继续编辑或重新验证")
            recommendation = "configured"
        elif not handlers:
            blockers.append("缺少唯一且契约有效的 Handler，请先通过代码生成完成脚本")
            if invalid_scripts:
                confirmations.append("项目中存在无效脚本，请先修复脚本契约")
            recommendation = "needs_info"
        elif len(handlers) > 1:
            blockers.append("存在多个绑定到该 Operation 的 Handler，必须先收敛为一个")
            confirmations.append("候选 Handler：" + "、".join(name for name, _item in handlers))
            recommendation = "needs_info"
        else:
            _name, contract = handlers[0]
            unsupported = [
                str(item.get("name") or "") for item in contract.params
                if item.get("type") not in {"string", "integer", "number", "boolean"}
            ]
            if unsupported:
                blockers.append("基础 Parser 不能可靠提取结构化参数：" + "、".join(unsupported))
                confirmations.append("请为这些字段提供专用 Parser 规则后再制作")
            unsupported_outputs = [
                str(item.get("name") or "") for item in contract.outputs
                if item.get("type") not in {"string", "integer", "number", "boolean"}
            ]
            if not contract.outputs:
                blockers.append("Handler 没有声明结构化输出字段，无法生成输出 Schema")
            elif unsupported_outputs:
                blockers.append("无法自动推导结构化输出的内部 Schema：" + "、".join(unsupported_outputs))
                confirmations.append("请先为这些输出字段补充自包含 JSON Schema")
            data_access = _handler_data_access(root, handler)
            handler_path, handler_source = module_sources[handler]
            handler_findings = _python_closure_findings(
                handler_source,
                handler_path,
                parser=False,
                data_access=data_access,
                module_sources=module_sources,
                local_modules=local_modules,
            )
            blockers.extend(
                "Handler 不满足确定性沙箱约束：" + item["message"]
                for item in handler_findings
            )
            reasons.extend(("Operation 已唯一绑定有效 Handler", "输入参数可从 Handler 契约生成 Schema"))
            if contract.outputs:
                reasons.append("Handler 已声明结构化输出，可生成输出 Schema")
            if data_access == "workspace_provider":
                reasons.append("Handler 使用受限工作空间 Provider，只生成能力声明而不绑定数据库")
            confirmations.append("确认自然语言示例、字段表达方式和测试向量符合业务语义")
            recommendation = "recommended" if not blockers else "needs_info"
        candidates.append({
            "operation": operation, "recommendation": recommendation,
            "handler": handler, "data_access": data_access,
            "reasons": reasons, "blockers": blockers, "confirmations": confirmations,
            "can_create": recommendation == "recommended",
        })
    return {
        "candidates": candidates,
        "counts": {
            key: sum(1 for item in candidates if item["recommendation"] == key)
            for key in ("recommended", "needs_info", "not_recommended", "configured")
        },
        "solver_findings": [str(item) for item in solver_problems],
    }


def _schema_property(item: Mapping[str, Any]) -> dict:
    value = {"type": item["type"]}
    description = str(item.get("desc") or "").strip()
    if description:
        value["description"] = description
    return value


def _sample_value(kind: str) -> Any:
    return {"string": "示例值", "integer": 1, "number": 1.5, "boolean": True}[kind]


def _alternate_sample_value(kind: str) -> Any:
    return {"string": "文本示例值", "integer": 2, "number": 2.5, "boolean": False}[kind]


def _field_aliases(item: Mapping[str, Any]) -> list[str]:
    """从字段说明提取保守业务别名，不做分词或语义猜测。"""
    name = str(item.get("name") or "").strip()
    description = str(item.get("desc") or "").strip()
    description = re.sub(r"\s*[，,；;]\s*(?:必填|可选)\s*$", "", description).strip()
    if (
        not description
        or _normalize_token(description) == _normalize_token(name)
        or len(description) > 64
        or any(character in description for character in "\r\n:=：")
    ):
        return []
    return [description]


def _basic_parser_source(_operation: str, fields: list[dict]) -> str:
    """生成保守的基础 Parser；路由由 Consumer 负责，这里只提取参数。"""
    specs = [
        {"name": str(item["name"]), "type": str(item["type"]),
         "required": item.get("required") == "是", "description": str(item.get("desc") or item["name"]),
         "aliases": _field_aliases(item)}
        for item in fields
    ]
    encoded_specs = json.dumps(specs, ensure_ascii=False)
    return f'''import argparse
import json
import math
import re
FIELDS = json.loads({encoded_specs!r})
INVALID = object()


def extract(text, field):
    names = [field["name"], *field.get("aliases", [])]
    name = "(?:" + "|".join(re.escape(value) for value in sorted(names, key=len, reverse=True)) + ")"
    prefix = rf"(?:^|[，,；;\\s]){{name}}\\s*(?:为|是|=|:|：)\\s*"
    present = re.search(prefix, text, re.IGNORECASE)
    if not present:
        return None
    other_names = "|".join(sorted({{
        re.escape(value)
        for item in FIELDS if item["name"] != field["name"]
        for value in [item["name"], *item.get("aliases", [])]
    }}, key=len, reverse=True))
    next_field = rf"|\\s+(?:{{other_names}})\\s*(?:为|是|=|:|：)" if other_names else ""
    # ASCII 逗号后紧跟数字时可能是千位分隔符，不能把它误当字段结束并
    # 静默截取前半段。首尾空白属于正常输入，在确认完整值后再忽略。
    end = r"\\s*(?=[，；;\\n]|,(?!\\d)|$" + next_field + ")"
    kind = field["type"]
    if kind == "integer":
        match = re.search(prefix + r"([-+]?\\d+)" + end, text, re.IGNORECASE)
        if not match:
            return INVALID
        return int(match.group(1))
    if kind == "number":
        match = re.search(
            prefix + r"([-+]?(?:\\d+(?:\\.\\d*)?|\\.\\d+)(?:[eE][-+]?\\d+)?)" + end,
            text,
            re.IGNORECASE,
        )
        if not match:
            return INVALID
        value = float(match.group(1))
        return value if math.isfinite(value) else INVALID
    if kind == "boolean":
        match = re.search(prefix + r"(true|false|是|否|开启|关闭)" + end, text, re.IGNORECASE)
        if not match:
            return INVALID
        return match.group(1).casefold() in ("true", "是", "开启")
    match = re.search(
        prefix + r"[\\\"']?(.+?)[\\\"']?" + end,
        text,
        re.IGNORECASE,
    )
    return match.group(1).strip() if match else INVALID


cli = argparse.ArgumentParser()
cli.add_argument("--request-json", required=True)
request = json.loads(cli.parse_args().request_json)
text = str(request.get("text") or "")
arguments = dict(request.get("known_arguments") or {{}})
# Consumer 在调用 Parser 前已经按 Operation key、alias 或 routing 选择了唯一
# Operation。Parser 不是第二套路由器，只负责从已选中的请求中提取参数；否则
# alias/routing 命中的请求可能被只认识 Operation 原名的 Parser 错误拒绝。
matched = True
issues = []
for field in FIELDS:
    if field["name"] in arguments:
        continue
    value = extract(text, field)
    if value is INVALID:
        issues.append({{"field": field["name"], "message": field["description"] + "格式不正确"}})
    elif value is not None:
        arguments[field["name"]] = value
issues.extend(
    {{"field": field["name"], "message": "请提供" + field["description"]}}
    for field in FIELDS if field["required"] and field["name"] not in arguments
    and not any(issue["field"] == field["name"] for issue in issues)
)
print(json.dumps({{"matched": matched, "arguments": arguments, "issues": issues}}, ensure_ascii=False))
'''


def candidate_draft(root: Path, operation: str, handler: str = "", data_access: str = "") -> dict:
    """从唯一 Handler 契约生成可编辑草稿；不写工作区，也不替用户确认启用。"""
    scan = candidate_scan(root)
    candidate = next((item for item in scan["candidates"] if item["operation"] == operation), None)
    if candidate is None:
        raise WorkspaceError("Operation 不存在", 404, "deterministic_candidate_not_found")
    if not candidate["handler"] or candidate["recommendation"] == "not_recommended":
        raise WorkspaceError(
            "该 Operation 尚不满足基础确定性制作条件：" + "；".join(candidate["blockers"]),
            422, "deterministic_candidate_incomplete",
        )
    selected_handler = handler or candidate["handler"]
    if selected_handler != candidate["handler"]:
        raise WorkspaceError("Handler 与候选扫描结果不一致，请重新扫描", 409, "deterministic_candidate_changed")
    selected_access = data_access or candidate["data_access"]
    if selected_access not in DATA_ACCESS_VALUES:
        raise WorkspaceError("data_access 只能是 none 或 workspace_provider", 400, CODE_INVALID)
    if selected_access != candidate["data_access"]:
        raise WorkspaceError(
            "data_access 与 Handler 能力扫描结果不一致，请重新扫描",
            409,
            "deterministic_candidate_changed",
        )
    from .deterministic_drafts import read_proposal

    proposal = read_proposal(root, operation, selected_handler, selected_access)
    if proposal is not None:
        return {
            "candidate": candidate, "draft": proposal,
            "notices": ["已读取助手生成的业务草稿；请确认业务示例后保存，再运行真实沙箱验证"],
            "draft_origin": "agent",
        }
    if candidate["recommendation"] == "configured":
        raise WorkspaceError(
            "尚无助手新版草稿，请先让助手完善，再读取草稿", 422, "deterministic_draft_missing",
        )
    if not candidate["can_create"]:
        raise WorkspaceError(
            "该 Operation 需要助手提供完整草稿：" + "；".join(candidate["blockers"]),
            422, "deterministic_candidate_incomplete",
        )
    scripts, _invalid = _script_contracts(root)
    contract = scripts[selected_handler]
    fields = [dict(item) for item in contract.params]
    outputs = [dict(item) for item in contract.outputs]
    required = [str(item["name"]) for item in fields if item.get("required") == "是"]
    output_required = [str(item["name"]) for item in outputs]
    input_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
        "additionalProperties": False,
        "properties": {str(item["name"]): _schema_property(item) for item in fields},
        "required": required,
    }
    output_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
        "additionalProperties": False,
        "properties": {str(item["name"]): _schema_property(item) for item in outputs},
        "required": output_required,
    }
    arguments = {str(item["name"]): _sample_value(str(item["type"])) for item in fields}
    text_arguments = {
        str(item["name"]): _alternate_sample_value(str(item["type"])) for item in fields
    }
    expected_output = {str(item["name"]): _sample_value(str(item["type"])) for item in outputs}
    phrases = "，".join(f"{name}为{value}" for name, value in arguments.items())
    override_phrases = "，".join(f"{name}为{value}" for name, value in text_arguments.items())
    missing_issues = [
        {"field": str(item["name"]), "message": "请提供" + str(item.get("desc") or item["name"])}
        for item in fields if item.get("required") == "是"
    ]
    notices = [
        "基础 Parser 复用 Consumer 的 Operation 路由，只负责识别字段名或字段说明对应的明确参数表达；复杂表达需要补充专用规则",
        "expected_output 是类型正确的合成占位值，保存后必须按 Handler 真实结果修正并完成沙箱验证",
    ]
    if selected_access == "workspace_provider":
        notices.append(
            f"工作空间数据用例必须补充合成 Provider fixture，并且只能使用 Consumer Provider v{PROVIDER_CAPABILITY_VERSION} 的只读操作"
        )
    return {
        "candidate": candidate,
        "draft": {
            "key": _candidate_key(selected_handler, {item.key for item in load_contracts(root)[0]}),
            "operation": operation, "handler": selected_handler,
            "data_access": selected_access, "enabled": True, "aliases": [],
            "routing": {"all_terms": [], "any_terms": [operation]},
            "parser_source": _basic_parser_source(operation, fields),
            "input_schema": json.dumps(input_schema, ensure_ascii=False, indent=2) + "\n",
            "output_schema": json.dumps(output_schema, ensure_ascii=False, indent=2) + "\n",
            "test_vectors": [
                {
                    "name": "基础自然语言提参", "text": operation + (f"，{phrases}" if phrases else ""),
                    "known_arguments": {}, "expected_matched": True, "expected_arguments": arguments,
                    "expected_issues": [], "expected_output": expected_output, "provider_fixtures": [],
                },
                {
                    "name": "缺少必填参数", "text": operation,
                    "known_arguments": {}, "expected_matched": True, "expected_arguments": {},
                    "expected_issues": missing_issues, "expected_output": {}, "provider_fixtures": [],
                },
                {
                    "name": "显式参数优先", "text": operation + (f"，{override_phrases}" if override_phrases else ""),
                    "known_arguments": arguments, "expected_matched": True,
                    "expected_arguments": arguments, "expected_issues": [],
                    "expected_output": expected_output, "provider_fixtures": [],
                },
            ],
            "verification": {"status": "missing", "current": False},
        },
        "notices": notices,
    }


def _package_direct_child(path: str, directory: str, suffix: str) -> str | None:
    prefix = f"{directory}/"
    if not path.startswith(prefix):
        return None
    name = path[len(prefix):]
    if not name or "/" in name or not name.endswith(suffix):
        return None
    return name


def extract_package_contracts(
    files: Mapping[str, bytes],
    *,
    managed_package_paths: set[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """从已发布 schema 0.1 包还原建模态 Contract。

    发布包故意不带测试向量和验证报告，因此回导后保留启用语义，但 tests.json 为空，
    必须由用户补齐测试并在当前工作区重新验证后才能再发布。
    """
    operations: list[dict] = []
    findings: list[dict] = []
    if ".deterministic-lock.json" in files:
        findings.append(_finding(
            CODE_INVALID,
            "发布包不能携带 .deterministic-lock.json，该文件只能由 Consumer 安装时生成",
            path=".deterministic-lock.json",
        ))
    module_sources = {
        Path(path).stem: (path, data)
        for path, data in files.items()
        if _package_direct_child(path, "scripts", ".py") is not None
    }
    local_modules = frozenset(module_sources)
    descriptors: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for path, data in files.items():
        if _package_direct_child(path, "scripts", ".md") is None or path == "scripts/index.md":
            continue
        try:
            text = data.decode("utf-8")
            match = re.match(r"\A---\s*\n(?P<body>.*?)\n---\s*(?:\n|\Z)", text, re.DOTALL)
            value = yaml.safe_load(match.group("body")) if match else None
        except (UnicodeDecodeError, yaml.YAMLError):
            continue
        if isinstance(value, Mapping):
            operation = str(value.get("bound_operation") or "").strip()
            if not operation:
                continue
            if operation in descriptors:
                findings.append(_finding(
                    CODE_INVALID, f"Operation「{operation}」存在多个 Handler 描述符", path=path,
                ))
            else:
                descriptors[operation] = (path, value)
    for path, data in sorted(files.items()):
        if _package_direct_child(path, "operations", ".md") is None or path == "operations/index.md":
            continue
        try:
            text = data.decode("utf-8")
            match = re.match(r"\A---\s*\n(?P<body>.*?)\n---\s*(?:\n|\Z)", text, re.DOTALL)
            front = yaml.safe_load(match.group("body")) if match else None
        except (UnicodeDecodeError, yaml.YAMLError):
            continue
        execution = front.get("execution") if isinstance(front, Mapping) else None
        if not isinstance(execution, Mapping) or str(execution.get("type") or "").strip() != "deterministic":
            continue
        operation = str(front.get("key") or Path(path).stem).strip()
        if operation != Path(path).stem:
            findings.append(_finding(
                CODE_INVALID, "Operation key 必须与文件名一致", path=path, field="key",
            ))
            continue
        descriptor_path, descriptor = descriptors.get(operation) or ("", {})
        entrypoint = str(descriptor.get("entrypoint") or "").strip()
        handler_path = (
            entrypoint if _package_direct_child(entrypoint, "scripts", ".py") is not None
            else f"scripts/{entrypoint}" if entrypoint and "/" not in entrypoint and entrypoint.endswith(".py")
            else ""
        )
        handler = str(descriptor.get("key") or Path(entrypoint).stem).strip()
        parser_path = str(execution.get("parser") or "").strip()
        parser_name = _package_direct_child(parser_path, "scripts", ".py")
        key = Path(parser_name or "").stem.removesuffix("_parser")
        required = {
            "parser": parser_path,
            "input_schema": str(execution.get("input_schema") or "").strip(),
            "output_schema": str(execution.get("output_schema") or "").strip(),
        }
        invalid_paths = []
        if parser_name is None:
            invalid_paths.append("parser")
        if _package_direct_child(required["input_schema"], "schemas", ".json") is None:
            invalid_paths.append("input_schema")
        if _package_direct_child(required["output_schema"], "schemas", ".json") is None:
            invalid_paths.append("output_schema")
        if not handler_path:
            invalid_paths.append("handler")
        if invalid_paths:
            findings.append(_finding(
                CODE_INVALID,
                f"确定性 Operation「{operation}」的引用路径不合法："
                + "、".join(invalid_paths)
                + " 只能引用 scripts/ 或 schemas/ 下的直接子文件",
                path=path,
            ))
            continue
        missing = [name for name, rel in required.items() if not rel or rel not in files]
        if handler_path and handler_path not in files:
            missing.append("handler")
        if not descriptor_path:
            missing.append("handler_descriptor")
        invalid_key = not key or not KEY_RE.fullmatch(key)
        invalid_handler = not handler or not HANDLER_RE.fullmatch(handler)
        if invalid_key or invalid_handler or missing:
            findings.append(_finding(
                CODE_INVALID,
                f"确定性 Operation「{operation}」的发布契约不完整"
                + (f"：缺少{'/'.join(dict.fromkeys(missing))}" if missing else "")
                + ("：确定性标识不合法" if invalid_key else "")
                + ("：Handler 标识不合法" if invalid_handler else ""),
                path=path,
            ))
            continue
        data_access = str(descriptor.get("data_access") or "none").strip().casefold()
        if data_access not in DATA_ACCESS_VALUES:
            findings.append(_finding(
                CODE_INVALID, "data_access 只能是 none 或 workspace_provider",
                path=descriptor_path, field="data_access",
            ))
            continue
        try:
            parser_source = files[required["parser"]].decode("utf-8")
            input_schema = files[required["input_schema"]].decode("utf-8")
            output_schema = files[required["output_schema"]].decode("utf-8")
            files[handler_path].decode("utf-8")
        except UnicodeDecodeError:
            findings.append(_finding(
                CODE_INVALID,
                f"确定性 Operation「{operation}」的 Parser、Handler 和 Schema 必须是 UTF-8 文本",
                path=path,
            ))
            continue
        package_findings = _python_closure_findings(
            parser_source.encode("utf-8"), required["parser"], parser=True, data_access="none",
            module_sources=module_sources, local_modules=local_modules,
        )
        package_findings += _python_closure_findings(
            files[handler_path], handler_path, parser=False, data_access=data_access,
            module_sources=module_sources, local_modules=local_modules,
        )
        package_findings += _schema_findings(
            input_schema.encode("utf-8"), "输入 Schema", required["input_schema"],
        )
        package_findings += _runtime_schema_findings(
            input_schema.encode("utf-8"), required["input_schema"],
        )
        package_findings += _schema_findings(
            output_schema.encode("utf-8"), "输出 Schema", required["output_schema"],
        )
        if package_findings:
            findings.extend(package_findings)
            continue
        policy = front.get("execution_policy") or {}
        routing = (policy.get("routing") or {}) if isinstance(policy, Mapping) else None
        aliases = front.get("aliases") or []
        if not isinstance(aliases, list) or any(not isinstance(value, str) for value in aliases):
            findings.append(_finding(CODE_INVALID, "aliases 必须是字符串数组", path=path, field="aliases"))
            continue
        if not isinstance(routing, Mapping):
            findings.append(_finding(CODE_INVALID, "execution_policy.routing 必须是 object", path=path))
            continue
        routing_values = {}
        invalid_routing = False
        for field_name in ("all_terms", "any_terms"):
            values = routing.get(field_name) or []
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                findings.append(_finding(
                    CODE_INVALID, f"execution_policy.routing.{field_name} 必须是字符串数组",
                    path=path, field=f"routing.{field_name}",
                ))
                invalid_routing = True
            else:
                routing_values[field_name] = values
        if invalid_routing:
            continue
        term_values = {
            "aliases": aliases,
            "routing.all_terms": routing_values["all_terms"],
            "routing.any_terms": routing_values["any_terms"],
        }
        invalid_terms = False
        for field_name, values in term_values.items():
            normalized = [_normalize_token(value) for value in values]
            if any(not value for value in normalized) or len(set(normalized)) != len(values):
                findings.append(_finding(
                    CODE_INVALID, f"{field_name} 不能包含空值或重复值", path=path, field=field_name,
                ))
                invalid_terms = True
            if len(values) > MAX_TERM_COUNT or any(len(value) > MAX_TERM_LENGTH for value in values):
                findings.append(_finding(
                    CODE_INVALID,
                    f"{field_name} 最多 {MAX_TERM_COUNT} 项，单项最多 {MAX_TERM_LENGTH} 个字符",
                    path=path, field=field_name,
                ))
                invalid_terms = True
        if invalid_terms:
            continue
        if managed_package_paths is not None:
            managed_package_paths.add(required["parser"])
        operations.append({
            "key": key,
            "operation": operation,
            "handler": handler,
            "data_access": data_access,
            "aliases": aliases,
            "routing": {
                "all_terms": routing_values["all_terms"],
                "any_terms": routing_values["any_terms"],
            },
            "enabled": True,
            "parser_source": parser_source,
            "input_schema": input_schema,
            "output_schema": output_schema,
            "test_vectors": [],
        })
    seen_keys: set[str] = set()
    seen_operations: set[str] = set()
    for item in operations:
        if item["key"] in seen_keys:
            findings.append(_finding(CODE_INVALID, f"确定性标识 {item['key']} 重复", field="key"))
        if item["operation"] in seen_operations:
            findings.append(_finding(
                CODE_INVALID, f"Operation「{item['operation']}」只能绑定一个确定性 Contract",
                field="operation",
            ))
        seen_keys.add(item["key"])
        seen_operations.add(item["operation"])
    return operations, findings


def materialize_package_contracts(
    root: Path,
    files: Mapping[str, bytes],
    *,
    managed_package_paths: set[str] | None = None,
) -> int:
    operations, findings = extract_package_contracts(
        files,
        managed_package_paths=managed_package_paths,
    )
    if findings:
        raise WorkspaceError("；".join(item["message"] for item in findings), 422, findings[0]["code"])
    for raw in operations:
        item = _contract_from_payload(raw)
        directory = root / item.rel_dir
        directory.mkdir(parents=True, exist_ok=True)
        (directory / CONTRACT_FILE).write_bytes(_contract_json(item))
        (directory / PARSER_FILE).write_bytes(item.parser)
        (directory / INPUT_SCHEMA_FILE).write_bytes(item.input_schema)
        (directory / OUTPUT_SCHEMA_FILE).write_bytes(item.output_schema)
        (directory / TESTS_FILE).write_bytes(item.tests)
    return len(operations)


def _contract_from_payload(item: Mapping[str, Any]) -> DeterministicContract:
    tree_error = _json_tree_error(item, "确定性执行契约")
    if tree_error:
        raise ValueError(tree_error)
    routing = item.get("routing") if isinstance(item.get("routing"), Mapping) else {}
    parser = item.get("parser_source", "")
    input_schema = item.get("input_schema", "")
    output_schema = item.get("output_schema", "")
    tests = item.get("test_vectors", item.get("test_vectors_source", []))
    if isinstance(input_schema, Mapping):
        input_schema = json.dumps(input_schema, ensure_ascii=False, indent=2) + "\n"
    if isinstance(output_schema, Mapping):
        output_schema = json.dumps(output_schema, ensure_ascii=False, indent=2) + "\n"
    if isinstance(tests, list):
        tests = json.dumps(tests, ensure_ascii=False, indent=2) + "\n"
    aliases = item.get("aliases") or ()
    all_terms = routing.get("all_terms") or ()
    any_terms = routing.get("any_terms") or ()
    if not isinstance(aliases, (list, tuple)):
        aliases = ()
    if not isinstance(all_terms, (list, tuple)):
        all_terms = ()
    if not isinstance(any_terms, (list, tuple)):
        any_terms = ()
    return DeterministicContract(
        key=str(item.get("key") or "").strip(), operation=str(item.get("operation") or "").strip(),
        handler=str(item.get("handler") or "").strip(),
        data_access=str(item.get("data_access") or "none").strip().casefold(),
        aliases=tuple(str(value).strip() for value in aliases),
        routing_all_terms=tuple(str(value).strip() for value in all_terms),
        routing_any_terms=tuple(str(value).strip() for value in any_terms),
        enabled=item.get("enabled", True) is not False,
        parser=str(parser).encode("utf-8"), input_schema=str(input_schema).encode("utf-8"),
        output_schema=str(output_schema).encode("utf-8"), tests=str(tests).encode("utf-8"),
    )


def _contract_json(contract: DeterministicContract) -> bytes:
    value = {
        "schema_version": SCHEMA_VERSION, "key": contract.key, "operation": contract.operation,
        "handler": contract.handler, "data_access": contract.data_access,
        "aliases": list(contract.aliases),
        "routing": {"all_terms": list(contract.routing_all_terms), "any_terms": list(contract.routing_any_terms)},
        "enabled": contract.enabled,
    }
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _load_directory(directory: Path) -> DeterministicContract:
    entries = {path.name for path in directory.iterdir()}
    unknown = sorted(entries - CONTRACT_FILES)
    missing = sorted(REQUIRED_CONTRACT_FILES - entries)
    if unknown:
        raise ValueError(f"Contract 目录包含不支持的文件：{'、'.join(unknown)}")
    if missing:
        raise ValueError(f"Contract 目录缺少文件：{'、'.join(missing)}")
    present_managed = entries & CONTRACT_FILES
    if any(not (directory / name).is_file() or (directory / name).is_symlink() for name in present_managed):
        raise ValueError("Contract 目录中的受管文件必须是普通文件")
    raw = _strict_json((directory / CONTRACT_FILE).read_bytes(), "确定性执行 Contract")
    if not isinstance(raw, Mapping):
        raise ValueError("确定性执行 Contract 根节点必须是 object")
    contract = _contract_from_payload({
        **raw,
        "parser_source": (directory / PARSER_FILE).read_text(encoding="utf-8"),
        "input_schema": (directory / INPUT_SCHEMA_FILE).read_text(encoding="utf-8"),
        "output_schema": (directory / OUTPUT_SCHEMA_FILE).read_text(encoding="utf-8"),
        "test_vectors": (directory / TESTS_FILE).read_text(encoding="utf-8"),
    })
    if contract.key != directory.name:
        raise ValueError("Contract key 必须与目录名一致")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"Contract schema_version 必须是 {SCHEMA_VERSION}")
    return contract


def _test_vectors(contract: DeterministicContract) -> tuple[list[dict], list[dict]]:
    path = f"{contract.rel_dir}/{TESTS_FILE}"
    if len(contract.tests) > MAX_TESTS_BYTES:
        return [], [_finding(CODE_INVALID, f"测试向量不能超过 {MAX_TESTS_BYTES // 1024} KiB", path=path)]
    try:
        raw = _strict_json(contract.tests, "确定性执行测试向量")
    except ValueError as exc:
        return [], [_finding(CODE_INVALID, str(exc), path=path)]
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_TEST_VECTORS:
        return [], [_finding(CODE_INVALID, f"测试向量必须是 1~{MAX_TEST_VECTORS} 项组成的数组", path=path)]
    findings, vectors, names, successful = [], [], set(), 0
    vector_fields = {
        "name", "text", "known_arguments", "expected_matched", "expected_arguments",
        "expected_issues", "expected_output", "provider_fixtures",
    }
    for index, vector in enumerate(raw):
        base = f"test_vectors[{index}]"
        if not isinstance(vector, Mapping):
            findings.append(_finding(CODE_INVALID, f"{base} 必须是 object", path=path))
            continue
        unknown = sorted(set(vector) - vector_fields)
        if unknown:
            findings.append(_finding(CODE_INVALID, f"{base} 包含不支持的字段：{'、'.join(unknown)}", path=path))
        name = str(vector.get("name") or "").strip()
        text = vector.get("text")
        matched = vector.get("expected_matched", True)
        arguments = vector.get("expected_arguments", {})
        known = vector.get("known_arguments", {})
        issues = vector.get("expected_issues", [])
        fixtures = vector.get("provider_fixtures", [])
        if not name or len(name) > 128 or name in names:
            findings.append(_finding(CODE_INVALID, f"{base}.name 必须是唯一且不超过 128 字符的非空名称", path=path))
        names.add(name)
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            findings.append(_finding(CODE_INVALID, f"{base}.text 必须是 1~4000 字符的自然语言请求", path=path))
        if not isinstance(matched, bool):
            findings.append(_finding(CODE_INVALID, f"{base}.expected_matched 必须是 boolean", path=path))
        if not isinstance(arguments, Mapping) or not isinstance(known, Mapping):
            findings.append(_finding(CODE_INVALID, f"{base} 的参数字段必须是 object", path=path))
        if not isinstance(issues, list) or not all(isinstance(item, Mapping) for item in issues):
            findings.append(_finding(CODE_INVALID, f"{base}.expected_issues 必须是 object 数组", path=path))
        elif any(not isinstance(item.get("field"), str) or not item.get("field", "").strip() for item in issues):
            findings.append(_finding(
                CODE_INVALID,
                f"{base}.expected_issues 每项都必须包含非空字符串 field",
                path=path,
            ))
        elif any(str(item.get("field") or "").replace("-", "_") in _CONSUMER_RUNTIME_ARGUMENT_FIELDS
                 for item in issues):
            findings.append(_finding(
                CODE_INVALID,
                f"{base}.expected_issues 不能要求 Consumer 保留的运行时参数",
                path=path,
            ))
        if not isinstance(fixtures, list) or not all(
            isinstance(item, Mapping)
            and isinstance(item.get("operation"), str)
            and "arguments" in item
            and isinstance(item.get("arguments"), Mapping)
            and "result" in item
            for item in fixtures
        ):
            findings.append(_finding(
                CODE_INVALID,
                f"{base}.provider_fixtures 必须是 operation、arguments、result 组成的数组",
                path=path,
            ))
        elif contract.data_access == "none" and fixtures:
            findings.append(_finding(
                CODE_INVALID,
                f"{base}.provider_fixtures 在 data_access:none 下必须为空",
                path=path,
            ))
        elif contract.data_access == "workspace_provider":
            for fixture_index, fixture in enumerate(fixtures):
                operation = str(fixture.get("operation") or "")
                if operation != operation.strip():
                    findings.append(_finding(
                        CODE_INVALID,
                        f"{base}.provider_fixtures[{fixture_index}].operation 不能包含首尾空格",
                        path=path,
                    ))
                    continue
                provider_arguments = PROVIDER_OPERATION_ARGUMENTS.get(operation)
                fixture_arguments = fixture.get("arguments")
                if provider_arguments is None:
                    findings.append(_finding(
                        CODE_INVALID,
                        f"{base}.provider_fixtures[{fixture_index}].operation 不在 Consumer Provider v{PROVIDER_CAPABILITY_VERSION} 白名单中",
                        path=path,
                    ))
                elif isinstance(fixture_arguments, Mapping):
                    unknown_arguments = sorted(set(fixture_arguments) - set(provider_arguments))
                    if unknown_arguments:
                        findings.append(_finding(
                            CODE_INVALID,
                            f"{base}.provider_fixtures[{fixture_index}].arguments 包含该 Provider 操作不支持的参数："
                            + "、".join(unknown_arguments),
                            path=path,
                        ))
        unresolved_issues = [
            issue for issue in issues
            if not isinstance(issue, Mapping) or str(issue.get("field") or "").strip() not in known
        ] if isinstance(issues, list) and isinstance(known, Mapping) else [None]
        if matched is True and not unresolved_issues:
            successful += 1
            if "expected_output" not in vector or not isinstance(vector.get("expected_output"), Mapping):
                findings.append(_finding(
                    CODE_INVALID,
                    f"{base} 的成功用例必须包含 object 类型 expected_output",
                    path=path,
                ))
        vectors.append(dict(vector))
    if successful == 0:
        findings.append(_finding(CODE_INVALID, "至少需要一个 Parser 命中且可执行 Handler 的成功测试向量", path=path))
    return vectors, findings


def load_contracts(root: Path) -> tuple[tuple[DeterministicContract, ...], list[dict]]:
    base = Path(root) / CONTRACT_DIR
    contracts, findings = [], []
    if not base.is_dir():
        return (), []
    for directory in sorted(base.iterdir()):
        if not directory.is_dir() or directory.is_symlink():
            findings.append(_finding(
                CODE_INVALID,
                "确定性执行契约目录包含非法条目",
                path=str(directory.relative_to(root)),
            ))
            continue
        try:
            contracts.append(_load_directory(directory))
        except (OSError, ValueError) as exc:
            findings.append(_finding(CODE_INVALID, str(exc), path=str(directory.relative_to(root))))
    return tuple(contracts), findings


def contract_identity_findings(
    root: Path, contracts: tuple[DeterministicContract, ...], *, allow_symlinks: bool = True,
) -> list[dict]:
    """Check shared identities without requiring every contract to be executable."""
    findings: list[dict] = []
    operations = _operation_stems(root, allow_symlinks=allow_symlinks)
    operation_tokens: dict[str, list[str]] = {}
    for operation in operations:
        operation_tokens.setdefault(_normalize_token(operation), []).append(operation)
    seen_keys, seen_operations, seen_tokens = set(), set(), {}
    for item in contracts:
        base = item.rel_dir
        if not KEY_RE.fullmatch(item.key):
            findings.append(_finding(
                CODE_INVALID,
                "确定性标识须以小写字母开头，只含小写字母、数字和下划线",
                path=f"{base}/{CONTRACT_FILE}",
                field="key",
            ))
        if item.key in seen_keys:
            findings.append(_finding(CODE_INVALID, f"确定性标识 {item.key} 重复", field="key"))
        seen_keys.add(item.key)
        if not item.enabled:
            # 停用项是保留在项目中的草稿，不进入发布态执行闭包。仅保留目录安全所需的 key 检查；
            # Operation、Handler 或 Schema 后续被删除时，用户仍应能够先停用而不是被旧引用卡死。
            continue
        if item.operation in seen_operations:
            findings.append(_finding(
                CODE_INVALID,
                f"Operation「{item.operation}」只能绑定一个确定性 Contract",
                field="operation",
            ))
        seen_operations.add(item.operation)
        own_normalized = _normalize_token(item.operation)
        for token in (item.operation, *item.aliases):
            normalized = _normalize_token(token)
            plain_operations = sorted(
                (operation for operation in operation_tokens.get(normalized, ()) if operation != item.operation),
                key=lambda value: value.encode("utf-8"),
            )
            if plain_operations:
                findings.append(_finding(
                    CODE_INVALID,
                    f"路由名称「{token}」与另一个 Operation「{plain_operations[0]}」冲突",
                    field="aliases",
                ))
            owner = seen_tokens.get(normalized)
            if normalized and owner is not None and _normalize_token(owner[0]) != own_normalized:
                findings.append(_finding(
                    CODE_INVALID,
                    f"路由名称「{token}」同时指向「{owner[0]}」和「{item.operation}」",
                    field="aliases",
                ))
            elif normalized and owner is not None:
                findings.append(_finding(CODE_INVALID, f"Operation key 与别名「{token}」重复", field="aliases"))
            elif normalized:
                seen_tokens[normalized] = (item.operation, token)
    return findings


def validate_contracts(
    root: Path, contracts: tuple[DeterministicContract, ...], *, allow_symlinks: bool = True,
) -> list[dict]:
    from .packs import contracts as contract_modules

    findings = contract_identity_findings(root, contracts, allow_symlinks=allow_symlinks)
    operations = _operation_stems(root, allow_symlinks=allow_symlinks)
    script_contract = contract_modules.module_for(root, "script_contract")
    scripts_dir = root / "ontology" / "scripts"
    module_sources = {
        path.stem: (str(path.relative_to(root)), path.read_bytes())
        for path in scripts_dir.glob("*.py") if path.is_file() and not path.is_symlink()
    } if scripts_dir.is_dir() else {}
    local_modules = frozenset(module_sources)
    for item in contracts:
        if not item.enabled:
            continue
        base = item.rel_dir
        parser_stem = f"{item.key}_parser"
        if parser_stem in local_modules:
            findings.append(_finding(
                CODE_INVALID,
                f"确定性标识 {item.key} 的发布态 Parser 会与脚本 {parser_stem}.py 冲突，请更换标识或脚本名",
                path=f"{base}/{CONTRACT_FILE}", field="key",
            ))
        if item.operation not in operations:
            findings.append(_finding(
                CODE_REFERENCE_MISSING,
                f"绑定的 Operation「{item.operation}」不存在",
                path=f"{base}/{CONTRACT_FILE}",
                field="operation",
            ))
        if item.data_access not in DATA_ACCESS_VALUES:
            findings.append(_finding(
                CODE_INVALID,
                "data_access 只能是 none 或 workspace_provider",
                path=f"{base}/{CONTRACT_FILE}",
                field="data_access",
            ))
        term_groups = (
            ("aliases", item.aliases),
            ("routing.all_terms", item.routing_all_terms),
            ("routing.any_terms", item.routing_any_terms),
        )
        for field_name, values in term_groups:
            normalized_values = {_normalize_token(value) for value in values}
            if any(not value for value in values) or len(normalized_values) != len(values):
                findings.append(_finding(
                    CODE_INVALID,
                    f"{field_name} 不能包含空值或重复值",
                    path=f"{base}/{CONTRACT_FILE}",
                    field=field_name,
                ))
            if len(values) > MAX_TERM_COUNT or any(len(value) > MAX_TERM_LENGTH for value in values):
                findings.append(_finding(
                    CODE_INVALID,
                    f"{field_name} 最多 {MAX_TERM_COUNT} 项，单项最多 {MAX_TERM_LENGTH} 个字符",
                    path=f"{base}/{CONTRACT_FILE}", field=field_name,
                ))
        if not HANDLER_RE.fullmatch(item.handler):
            findings.append(_finding(
                CODE_INVALID, "Handler 必须是 ontology/scripts 下不带扩展名的 Python 标识符",
                path=f"{base}/{CONTRACT_FILE}", field="handler",
            ))
            handler_path = None
        else:
            handler_path = root / "ontology" / "scripts" / f"{item.handler}.py"
        if item.handler == f"{item.key}_parser":
            findings.append(_finding(CODE_INVALID, "Handler 文件名不能与发布态 Parser 文件名冲突", field="handler"))
        if handler_path is None or not handler_path.is_file() or handler_path.is_symlink():
            findings.append(_finding(
                CODE_REFERENCE_MISSING,
                f"Handler 脚本 {item.handler or '（空）'} 不存在",
                field="handler",
            ))
        else:
            parsed = None
            try:
                parsed = script_contract.parse_script(handler_path.read_text(encoding="utf-8"))
                if parsed.bound != item.operation:
                    findings.append(_finding(
                        CODE_INVALID,
                        f"Handler「{item.handler}」绑定的是「{parsed.bound or '无'}」，不是「{item.operation}」",
                        path=str(handler_path.relative_to(root)),
                        field="handler",
                    ))
            except (OSError, ValueError) as exc:
                findings.append(_finding(
                    CODE_INVALID,
                    f"Handler 契约无效：{exc}",
                    path=str(handler_path.relative_to(root)),
                ))
            if parsed is not None:
                findings += _handler_schema_findings(
                    parsed, item.input_schema, f"{base}/{INPUT_SCHEMA_FILE}",
                )
            findings += _python_closure_findings(
                handler_path.read_bytes(), str(handler_path.relative_to(root)), parser=False,
                data_access=item.data_access, module_sources=module_sources, local_modules=local_modules,
            )
        findings += _python_closure_findings(
            item.parser, f"{base}/{PARSER_FILE}", parser=True, data_access="none",
            module_sources=module_sources, local_modules=local_modules,
        )
        findings += _schema_findings(item.input_schema, "输入 Schema", f"{base}/{INPUT_SCHEMA_FILE}")
        findings += _runtime_schema_findings(item.input_schema, f"{base}/{INPUT_SCHEMA_FILE}")
        findings += _schema_findings(item.output_schema, "输出 Schema", f"{base}/{OUTPUT_SCHEMA_FILE}")
        _vectors, vector_findings = _test_vectors(item)
        findings += vector_findings
    return findings


def _operation_path(root: Path, operation: str) -> Path | None:
    for rel in ("ontology/operation", "ontology/logic"):
        path = root / rel / f"{operation}.md"
        if path.is_file() and not path.is_symlink():
            return path
    return None


def _handler_dependencies(root: Path, handler: str) -> list[Path]:
    scripts = root / "ontology" / "scripts"
    entry = scripts / f"{handler}.py"
    if not entry.is_file() or entry.is_symlink():
        return []
    modules = {
        path.stem: path for path in scripts.glob("*.py")
        if path.is_file() and not path.is_symlink()
    }
    pending, found = [entry], {}
    while pending:
        path = pending.pop()
        if path.name in found:
            continue
        found[path.name] = path
        for module in _local_imports(path.read_bytes(), frozenset(modules)):
            dependency = modules.get(module)
            if dependency is not None and dependency.name not in found:
                pending.append(dependency)
    return [found[name] for name in sorted(found)]


def _parser_dependencies(root: Path, parser: bytes) -> list[Path]:
    """Parser 从 ontology/scripts 引用的本地模块传递闭包（不包含 Parser 自身）。"""
    scripts = root / "ontology" / "scripts"
    modules = {
        path.stem: path for path in scripts.glob("*.py")
        if path.is_file() and not path.is_symlink()
    } if scripts.is_dir() else {}
    pending = list(_local_imports(parser, frozenset(modules)))
    found = {}
    while pending:
        module = pending.pop()
        path = modules.get(module)
        if path is None or path.name in found:
            continue
        found[path.name] = path
        pending.extend(_local_imports(path.read_bytes(), frozenset(modules)))
    return [found[name] for name in sorted(found)]


def source_revision(root: Path, item: DeterministicContract) -> str:
    """绑定验证证据的完整建模态摘要；任一执行依赖变化都会使旧报告失效。"""
    digest = hashlib.sha256()
    managed = (
        (CONTRACT_FILE, _contract_json(item)), (PARSER_FILE, item.parser),
        (INPUT_SCHEMA_FILE, item.input_schema), (OUTPUT_SCHEMA_FILE, item.output_schema),
        (TESTS_FILE, item.tests),
    )
    for name, data in managed:
        digest.update(f"contract/{name}\0".encode("utf-8"))
        digest.update(data)
    operation_path = _operation_path(root, item.operation)
    if operation_path is not None:
        digest.update(b"operation\0" + operation_path.read_bytes())
    for path in _parser_dependencies(root, item.parser):
        digest.update(f"parser-script/{path.name}\0".encode("utf-8"))
        digest.update(path.read_bytes())
    for path in _handler_dependencies(root, item.handler):
        digest.update(f"script/{path.name}\0".encode("utf-8"))
        digest.update(path.read_bytes())
    return "sha256:" + digest.hexdigest()


def verification_status(root: Path, item: DeterministicContract) -> dict:
    path = root / item.rel_dir / VERIFICATION_FILE
    if not path.is_file() or path.is_symlink():
        return {"status": "missing", "current": False}
    try:
        report = _strict_json(path.read_bytes(), "确定性执行验证报告")
    except (OSError, ValueError) as exc:
        return {"status": "invalid", "current": False, "message": str(exc)}
    if not isinstance(report, Mapping) or report.get("version") != 1:
        return {"status": "invalid", "current": False, "message": "验证报告格式无效"}
    current = report.get("source_revision") == source_revision(root, item)
    return {
        "status": str(report.get("status") or "invalid"),
        "current": current,
        "verified_at": str(report.get("verified_at") or ""),
        "runtime": str(report.get("runtime") or ""),
        "passed": int(report.get("passed") or 0),
        "failed": int(report.get("failed") or 0),
        "cases": report.get("cases") if isinstance(report.get("cases"), list) else [],
    }


def verification_finding(root: Path, item: DeterministicContract) -> dict | None:
    status = verification_status(root, item)
    if status.get("status") == "passed" and status.get("current") is True:
        return None
    if status.get("status") == "missing":
        message = f"确定性 Operation「{item.operation}」尚未完成执行闭包验证"
    elif not status.get("current"):
        message = f"确定性 Operation「{item.operation}」的验证报告已因依赖变化失效，请重新验证"
    else:
        message = f"确定性 Operation「{item.operation}」的执行闭包验证未通过"
    return _finding(CODE_UNVERIFIED, message, path=f"{item.rel_dir}/{VERIFICATION_FILE}")


def _revision(contracts: tuple[DeterministicContract, ...]) -> str:
    if not contracts:
        return ""
    digest = hashlib.sha256()
    for item in sorted(contracts, key=lambda value: value.key):
        for name, data in ((CONTRACT_FILE, _contract_json(item)), (PARSER_FILE, item.parser),
                           (INPUT_SCHEMA_FILE, item.input_schema), (OUTPUT_SCHEMA_FILE, item.output_schema),
                           (TESTS_FILE, item.tests)):
            digest.update(f"{item.key}/{name}\0".encode("utf-8"))
            digest.update(data)
    return "sha256:" + digest.hexdigest()


def check_at(root: Path, *, allow_symlinks: bool = True) -> DeterministicCheck:
    contracts, load_findings = load_contracts(root)
    return DeterministicCheck(
        contracts,
        load_findings + validate_contracts(root, contracts, allow_symlinks=allow_symlinks),
        _revision(contracts),
    )


def check_payload(root: Path, operations: list) -> DeterministicCheck:
    findings = []
    contracts = []
    if len(operations) > MAX_CONTRACTS:
        findings.append(_finding(CODE_INVALID, f"确定性 Operation 最多 {MAX_CONTRACTS} 个"))
        operations = operations[:MAX_CONTRACTS]
    allowed = {
        "key", "operation", "handler", "data_access", "aliases", "routing", "enabled",
        "parser_source", "input_schema", "output_schema", "test_vectors", "test_vectors_source",
    }
    for index, item in enumerate(operations):
        if not isinstance(item, Mapping):
            findings.append(_finding(CODE_INVALID, f"operations[{index}] 必须是 object"))
            continue
        tree_error = _json_tree_error(item, f"operations[{index}]")
        if tree_error:
            findings.append(_finding(CODE_INVALID, tree_error))
            continue
        unknown = sorted(set(item) - allowed)
        if unknown:
            findings.append(_finding(
                CODE_INVALID,
                f"operations[{index}] 包含不支持的字段：{'、'.join(unknown)}；具体数据源和连接配置不能进入建模契约",
            ))
        routing = item.get("routing", {})
        shapes = (
            ("aliases", item.get("aliases", []), list),
            ("routing", routing, Mapping),
            ("routing.all_terms", routing.get("all_terms", []) if isinstance(routing, Mapping) else [], list),
            ("routing.any_terms", routing.get("any_terms", []) if isinstance(routing, Mapping) else [], list),
        )
        for field_name, value, expected in shapes:
            if not isinstance(value, expected):
                findings.append(_finding(
                    CODE_INVALID, f"operations[{index}].{field_name} 类型不正确", field=field_name,
                ))
        if "enabled" in item and not isinstance(item.get("enabled"), bool):
            findings.append(_finding(
                CODE_INVALID, f"operations[{index}].enabled 必须是 boolean", field="enabled",
            ))
        for field_name in ("parser_source", "input_schema", "output_schema"):
            value = item.get(field_name, "")
            if not isinstance(value, (str, Mapping)) or field_name == "parser_source" and not isinstance(value, str):
                findings.append(_finding(
                    CODE_INVALID, f"operations[{index}].{field_name} 类型不正确", field=field_name,
                ))
        test_vectors = item.get("test_vectors", item.get("test_vectors_source", []))
        if not isinstance(test_vectors, (list, str)):
            findings.append(_finding(
                CODE_INVALID, f"operations[{index}].test_vectors 类型不正确", field="test_vectors",
            ))
        try:
            contracts.append(_contract_from_payload(item))
        except (TypeError, UnicodeError, ValueError) as exc:
            findings.append(_finding(CODE_INVALID, f"operations[{index}] 无法转换为契约：{exc}"))
    values = tuple(contracts)
    findings += validate_contracts(root, values)
    return DeterministicCheck(values, findings, _revision(values))


def public_contract(item: DeterministicContract) -> dict:
    try:
        tests = _strict_json(item.tests, "确定性执行测试向量")
    except ValueError:
        tests = []
    return {
        "key": item.key, "operation": item.operation, "handler": item.handler,
        "data_access": item.data_access, "aliases": list(item.aliases),
        "routing": {"all_terms": list(item.routing_all_terms), "any_terms": list(item.routing_any_terms)},
        "enabled": item.enabled, "parser_source": item.parser.decode("utf-8"),
        "input_schema": item.input_schema.decode("utf-8"), "output_schema": item.output_schema.decode("utf-8"),
        "test_vectors": tests, "test_vectors_source": item.tests.decode("utf-8"),
    }


def contract_view(root: Path) -> dict:
    check = check_at(root)
    handlers = []
    scripts = root / "ontology" / "scripts"
    if scripts.is_dir():
        handlers = [path.stem for path in sorted(scripts.glob("*.py")) if path.is_file() and not path.is_symlink()]
    return {
        "revision": check.revision, "enabled": check.enabled,
        "operations": [
            {**public_contract(item), "verification": verification_status(root, item)}
            for item in check.contracts
        ],
        "findings": check.findings,
        "available_operations": sorted(_operation_stems(root)), "available_handlers": handlers,
        "data_access_options": list(DATA_ACCESS_VALUES),
    }


def release_status_at(root: Path) -> dict:
    check = check_at(root, allow_symlinks=False)
    if not check.contracts:
        return {"enabled": False}
    findings = list(check.findings)
    findings += [
        finding for item in check.contracts if item.enabled
        if (finding := verification_finding(root, item)) is not None
    ]
    return {
        "enabled": check.enabled, "revision": check.revision,
        "operations": [{"key": item.key, "operation": item.operation, "data_access": item.data_access}
                       for item in check.contracts if item.enabled],
        "findings": findings,
    }


def flow_complete(root: Path) -> bool:
    """启用契约全部静态合法且当前验证通过时，确定性流程节点才算完成。"""
    status = release_status_at(root)
    return bool(status.get("enabled")) and not status.get("findings")


def execution_mode_conflicts(
    deterministic_operations: set[str], solver_operations: set[str],
) -> list[dict]:
    """返回首版 deterministic/solver 同包互斥问题；空集合代表对应模式未启用。"""
    conflicts = sorted(deterministic_operations & solver_operations, key=lambda value: value.encode("utf-8"))
    findings = [
        _finding(CODE_CONFLICT, f"Operation「{operation}」不能同时启用 deterministic 与 solver_workflow")
        for operation in conflicts
    ]
    if deterministic_operations and solver_operations and not conflicts:
        findings.append(_finding(
            CODE_CONFLICT,
            "第一版不能在同一知识包中混合 deterministic 与 solver_workflow；请停用其中一种执行方式",
        ))
    return findings


def enabled_operations(root: Path) -> set[str]:
    """读取工作区中可解析且已启用的确定性 Operation。"""
    contracts, _findings = load_contracts(root)
    return {item.operation for item in contracts if item.enabled}


def solver_conflicts(root: Path, contracts: tuple[DeterministicContract, ...]) -> list[dict]:
    """保存阶段拒绝启用的 deterministic/solver 冲突；停用草稿可并存。"""
    from . import solver_contract

    solver_items, _problems = solver_contract.load_workspace_solver(root, _operation_stems(root))
    return execution_mode_conflicts(
        {item.operation for item in contracts if item.enabled},
        {item.operation for item in solver_items if item.enabled},
    )


def check_payload_with_conflicts(root: Path, operations: list) -> DeterministicCheck:
    """执行页面预检查和保存共同使用的完整工作区检查。"""
    draft = check_payload(root, operations)
    draft.findings += solver_conflicts(root, draft.contracts)
    return draft


def save_contracts(manager, project_id: str, operations: list, base_revision: str, *, author: str = "") -> dict:
    from ..kernel.writepath import ChangeTx, Origin

    root = manager.project_root(project_id)
    with ChangeTx(manager, project_id, Origin("web", author), summary="保存确定性执行契约") as tx:
        # revision 检查、引用校验和写入共用项目锁，避免检查后 Operation、Handler 或 Contract 被并发修改。
        draft = check_payload_with_conflicts(root, operations)
        if draft.findings:
            raise DeterministicContractRejected(draft.findings)
        current = check_at(root)
        if (base_revision or "") != current.revision:
            raise WorkspaceError(
                "确定性执行契约已被修改，请刷新后重试",
                409,
                "deterministic_contract_revision_conflict",
            )
        base = root / CONTRACT_DIR
        wanted = {item.key for item in draft.contracts}
        if base.is_dir():
            for directory in base.iterdir():
                if not directory.is_dir() or directory.is_symlink():
                    tx.remove(directory.relative_to(root).as_posix())
                    continue
                for path in sorted(directory.rglob("*"), reverse=True):
                    rel_name = path.relative_to(directory).as_posix()
                    if directory.name not in wanted or rel_name not in CONTRACT_FILES:
                        if path.is_file() or path.is_symlink():
                            tx.remove(path.relative_to(root).as_posix())
        for item in draft.contracts:
            tx.write(f"{item.rel_dir}/{CONTRACT_FILE}", _contract_json(item))
            tx.write(f"{item.rel_dir}/{PARSER_FILE}", item.parser)
            tx.write(f"{item.rel_dir}/{INPUT_SCHEMA_FILE}", item.input_schema)
            tx.write(f"{item.rel_dir}/{OUTPUT_SCHEMA_FILE}", item.output_schema)
            tx.write(f"{item.rel_dir}/{TESTS_FILE}", item.tests)
            current_item = next((value for value in current.contracts if value.key == item.key), None)
            current_report = root / item.rel_dir / VERIFICATION_FILE
            if (
                current_item is not None and current_report.is_file()
                and source_revision(root, current_item) == source_revision(root, item)
            ):
                tx.write(f"{item.rel_dir}/{VERIFICATION_FILE}", current_report.read_bytes())
        result = tx.commit(f"web: 保存确定性执行契约 by {author}" if author else "web: 保存确定性执行契约")
    # Git 不记录目录。提交成功后再清理空目录，避免提交失败时目录结构先被破坏而无法回滚。
    base = root / CONTRACT_DIR
    if base.is_dir():
        for directory in sorted((path for path in base.rglob("*") if path.is_dir()), reverse=True):
            if directory.is_dir():
                with suppress(OSError):
                    directory.rmdir()
        with suppress(OSError):
            base.rmdir()
    return {**contract_view(root), "commit": result.commit}


def ensure_publishable(root: Path, *, solver_enabled: bool = False) -> DeterministicCheck:
    check = check_at(root, allow_symlinks=False)
    enabled = [item for item in check.contracts if item.enabled]
    findings = list(check.findings)
    if enabled and solver_enabled:
        findings.append(_finding(CODE_CONFLICT, "第一版不能在同一知识包中混合 deterministic 与 solver_workflow"))
    findings += [
        finding for item in enabled
        if (finding := verification_finding(root, item)) is not None
    ]
    if findings:
        raise WorkspaceError("确定性执行契约未通过发布检查：" + "；".join(item["message"] for item in findings[:3]),
                             code=findings[0]["code"])
    return check


def write_verification_report(
    manager, project_id: str, key: str, expected_revision: str, report: dict, *, author: str = "",
) -> dict:
    """在验证执行结束后短暂持锁写报告；不在项目锁内等待沙箱。"""
    from ..kernel.writepath import ChangeTx, Origin

    root = manager.project_root(project_id)
    with ChangeTx(manager, project_id, Origin("web", author), summary="保存确定性执行验证报告") as tx:
        check = check_at(root)
        item = next((value for value in check.contracts if value.key == key and value.enabled), None)
        if item is None:
            raise WorkspaceError("确定性执行契约不存在或已停用", 409, "deterministic_contract_changed")
        if source_revision(root, item) != expected_revision:
            raise WorkspaceError("验证期间执行闭包已变化，请重新验证", 409, "deterministic_contract_changed")
        payload = {
            "version": 1, "source_revision": expected_revision,
            "verified_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            **report,
        }
        tx.write(
            f"{item.rel_dir}/{VERIFICATION_FILE}",
            (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        result = tx.commit(f"web: 验证确定性执行契约 by {author}" if author else "web: 验证确定性执行契约")
    return {"report": payload, "commit": result.commit, "contract": contract_view(root)}
