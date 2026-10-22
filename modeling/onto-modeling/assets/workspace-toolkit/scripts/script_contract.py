#!/usr/bin/env python3
"""ontology/scripts/*.py 的 CLI 契约解析：头部「输入参数」与 argparse 声明必须一一对应。

单一事实源：知识包导出（backend `workspace.py`）与 `ontology_checker.py --include-scripts`
共用本模块，建模阶段的规则检查与导出时的拦截结论一致。仅依赖标准库。
"""
from __future__ import annotations

import ast
import re

SCRIPT_TYPE_MAP = {
    "str": "string", "string": "string", "int": "integer", "integer": "integer",
    "float": "number", "number": "number", "bool": "boolean", "boolean": "boolean",
    "list": "array", "tuple": "array", "dict": "object", "object": "object", "json": "json", "loads": "json",
}

PARAMS_SCOPE_HINT = "「输入参数」只列 argparse 声明的 CLI 参数；仅供 import 调用的函数参数写在「用法」段"
TYPE_HINT = '未写 type= 的参数按 str；布尔开关用 action="store_true" 并标 bool'
# 头部 number 与 argparse int（或反之）只是精度描述不同，不拦导出；导出类型以 argparse 为准，
# integer 让运行端按整数拼参数，避免 type=int 收到 "2026.0"。
NUMERIC_TYPES = {"integer", "number"}


class ScriptContractError(ValueError):
    """A generated script cannot be exported with an unverifiable CLI contract."""


def _type_conflicts(documented: str, declared: str) -> bool:
    """头部类型与 argparse 类型不一致；integer 与 number 互写不算冲突（见 NUMERIC_TYPES）。"""
    return documented != declared and {documented, declared} != NUMERIC_TYPES


def _script_header(code: str) -> str:
    doc = code.split('"""')
    return doc[1] if len(doc) >= 2 else ""


def script_bound_operation(code: str) -> str:
    """头部「关联operation」的 operation 名（片段写法去掉「/ 步骤X」）；「无」或未写时为空。"""
    match = re.search(r"关联operation[：:]\s*(.+)", _script_header(code))
    if not match:
        return ""
    bound = match.group(1).split("/")[0].strip()
    return "" if bound in ("无", "-") else bound


def parse_script_contract(code: str) -> tuple[str, list[dict]]:
    """Read the operation binding and verify the documented CLI against argparse.

    The Python source is the single source of truth for flags and argparse semantics.
    The docstring supplies descriptions, but never invents or renames a flag.
    """
    header = _script_header(code)
    bound = script_bound_operation(code)

    documented: list[dict] = []
    lines = header.splitlines()
    in_params = False
    for line in lines:
        stripped = line.strip()
        if re.match(r"^输入参数[：:]\s*$", stripped):
            in_params = True
            continue
        if in_params:
            # 下一个段落标题（形如 "输出：" "依赖："）结束参数段
            if re.match(r"^[\w一-鿿]+[：:]", stripped) and not stripped.startswith("-"):
                break
            if not stripped:
                continue
            fields = re.split(r"\s{2,}|\t", stripped)
            if len(fields) < 2:
                continue
            name = fields[0].strip().lstrip("-").strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                continue
            raw_type = fields[1].strip().lower()
            desc = fields[2].strip() if len(fields) > 2 else ""
            required = "是" if "必填" in desc else "否"
            default = ""
            dmatch = re.search(r"默认[值为]?\s*[：:]?\s*([^\s，。；;]+)", desc)
            if dmatch:
                default = dmatch.group(1)
                required = "否"
            documented.append({
                "name": name,
                "type": SCRIPT_TYPE_MAP.get(raw_type, "string"),
                "required": required,
                "default": default,
                "desc": desc.replace("|", "，"),
            })

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise ScriptContractError(f"脚本语法错误：{exc.msg}（第 {exc.lineno} 行）") from exc

    source_params: list[dict] = []
    argument_calls = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
        ):
            argument_calls.append(node)
    argument_calls.sort(key=lambda item: getattr(item, "lineno", 0))

    for node in argument_calls:
        flags = [_literal_string(arg) for arg in node.args]
        flags = [flag for flag in flags if flag]
        long_flags = [flag for flag in flags if flag.startswith("--")]
        flag = (long_flags or flags[:1])
        if not flag:
            raise ScriptContractError("argparse.add_argument 必须声明 option flag（例如 --records）")
        flag = flag[0]
        name = _literal_keyword(node, "dest") or flag.lstrip("-").replace("-", "_")
        raw_type = _ast_type_name(_keyword_node(node, "type"))
        action = _literal_keyword(node, "action") or ""
        nargs = _literal_keyword(node, "nargs")
        required = _literal_keyword(node, "required")
        if required is None and _keyword_node(node, "required") is not None:
            raise ScriptContractError(f"参数 {name} 的 required 必须是字面量布尔值")
        required = bool(required) if required is not None else False
        default_node = _keyword_node(node, "default")
        default = _literal_node(default_node)
        if default_node is not None and default is None:
            default = ast.unparse(default_node)
        if action in {"store_true", "store_false"}:
            required = False
            default = action == "store_false"
            raw_type = "boolean"
        multiple = action in {"append", "extend"} or nargs in {"*", "+"} or isinstance(nargs, int) and nargs != 1
        source_params.append({
            "name": str(name),
            "flag": flag,
            "type": SCRIPT_TYPE_MAP.get(raw_type.lower(), "string"),
            "required": required,
            "default": default,
            "multiple": multiple,
            "action": action,
        })

    if documented and not source_params:
        raise ScriptContractError(f"头部声明了输入参数，但脚本没有 argparse.add_argument()（{PARAMS_SCOPE_HINT}）")
    if documented:
        documented_by_name = {item["name"]: item for item in documented}
        source_names = {item["name"] for item in source_params}
        if set(documented_by_name) != source_names:
            missing = sorted(source_names - set(documented_by_name))
            extra = sorted(set(documented_by_name) - source_names)
            raise ScriptContractError(
                f"头部参数与 argparse 不一致：缺少 {missing}，多余 {extra}（{PARAMS_SCOPE_HINT}）"
            )

    params = []
    for item in source_params:
        doc = next((entry for entry in documented if entry["name"] == item["name"]), {})
        source_required = "是" if item["required"] else "否"
        if doc and _type_conflicts(doc["type"], item["type"]):
            raise ScriptContractError(
                f"参数 {item['name']} 的类型与 argparse 不一致：头部为 {doc['type']}，"
                f"argparse 为 {item['type']}（{TYPE_HINT}）"
            )
        if doc and doc["required"] != source_required:
            raise ScriptContractError(
                f"参数 {item['name']} 的必填标记与 argparse 不一致：头部为 {doc['required']}，argparse 为 {source_required}"
            )
        params.append({
            "name": item["name"], "flag": item["flag"], "type": item["type"],
            "required": source_required,
            "default": "" if item["default"] is None else str(item["default"]),
            "multiple": "是" if item["multiple"] else "否", "action": item["action"],
            "desc": doc.get("desc", ""),
        })
    return bound, params


def _literal_string(node: ast.AST) -> str:
    value = _literal_node(node)
    return value if isinstance(value, str) else ""


def _literal_keyword(call: ast.Call, name: str):
    node = _keyword_node(call, name)
    return _literal_node(node) if node is not None else None


def _keyword_node(call: ast.Call, name: str):
    return next((keyword.value for keyword in call.keywords if keyword.arg == name), None)


def _literal_node(node: ast.AST | None):
    if node is None:
        return None
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return None


def _ast_type_name(node: ast.AST | None) -> str:
    if node is None:
        return "str"
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    value = _literal_node(node)
    return str(value or "str")
