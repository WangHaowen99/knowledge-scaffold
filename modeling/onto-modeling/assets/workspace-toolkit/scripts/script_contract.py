#!/usr/bin/env python3
"""ontology/scripts/*.py 的 CLI 契约解析：头部「输入参数」与 argparse 声明必须一一对应。

单一事实源：知识包导出（backend `workspace.py`）、脚本描述符（`script_descriptor.py`）与
`ontology_checker.py --include-scripts` 共用本模块，建模阶段的规则检查与导出时的拦截结论一致。

参数类型按 consumer 的调用口径（consumer/backend/agent/tools/execute_script.py 把参数表
转成 JSON Schema，再把智能体给的值转成 `--flag 值`）只用六种：
string / integer / number / boolean / object / array。头部仍兼容旧写法 str / int / float /
bool / json。仅依赖标准库。
"""
from __future__ import annotations

import ast
import io
import json
import re
import tokenize
from dataclasses import dataclass

from script_output import static_outputs

# 头部「输入参数」「输出」里的类型写法 → consumer 类型；json 是旧写法，consumer 只能当字符串传
TYPE_ALIASES = {
    "str": "string", "string": "string", "int": "integer", "integer": "integer",
    "float": "number", "number": "number", "bool": "boolean", "boolean": "boolean",
    "dict": "object", "object": "object", "list": "array", "array": "array", "json": "json",
}
STRUCTURED_TYPES = ("object", "array")

# argparse 的 type= → 脚本实际收到的值；其余可调用对象收到的都是字符串
_SOURCE_TYPES = {"str": "string", "int": "integer", "float": "number", "loads": "json"}
# consumer 传入的都是字符串，这些 type= 会把值解析错
_UNPASSABLE_TYPES = {
    "bool": 'type=bool 会把 "false" 也当成 True；布尔开关请用 action="store_true"',
    "list": "结构化参数请用 type=json.loads，头部类型写 array",
    "tuple": "结构化参数请用 type=json.loads，头部类型写 array",
    "set": "结构化参数请用 type=json.loads，头部类型写 array",
    "dict": "结构化参数请用 type=json.loads，头部类型写 object",
}
_PASSABLE_ACTIONS = {"", "store", "store_true", "store_false", "append", "extend"}
_BOOLEAN_ACTIONS = {"store_true", "store_false"}

PARAMS_SCOPE_HINT = "「输入参数」只列 argparse 声明的 CLI 参数；仅供 import 调用的函数参数写在「用法」段"
TYPE_HINT = (
    '类型写 consumer 的取值：未写 type= 的参数为 string，type=int 为 integer，type=float 为 number，'
    'action="store_true" 为 boolean，type=json.loads 按取值写 object 或 array'
)
# 头部 number 与 argparse int（或反之）只是精度描述不同，不拦导出；导出类型以 argparse 为准，
# integer 让运行端按整数拼参数，避免 type=int 收到 "2026.0"。
NUMERIC_TYPES = {"integer", "number"}

_LABEL_RE = re.compile(r"^([\w一-鿿]+)[：:]\s*(.*)$")
_NO_BINDING_RE = re.compile(r"^(无|-|—)(\s|[（(]|$)")


class ScriptContractError(ValueError):
    """A generated script cannot be exported with an unverifiable CLI contract."""


@dataclass(frozen=True)
class ScriptContract:
    bound: str  # 关联operation（片段已去掉「/ 步骤」后缀）；库模块为空串
    fragment: str  # 片段范围（如「步骤2-4」）；整段脚本为空串
    title: str  # 功能描述
    params: tuple[dict, ...]
    outputs: tuple[dict, ...]  # 头部「输出」逐行声明的字段：name / type / desc
    output_text: str  # 头部「输出」里没写成字段行的原文
    warnings: tuple[str, ...]  # 不阻断导出、但 consumer 调用会吃亏的写法
    output_verified: bool  # CLI 输出字段是否能静态确认；否则导出须有当前源码的运行验证


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
    contract = parse_script(code)
    return contract.bound, list(contract.params)


def parse_script(code: str) -> ScriptContract:
    header = _script_header(code)
    blocks = _header_blocks(header)
    bound, fragment = _binding(blocks)
    documented = _documented_params(blocks.get("输入参数", []))

    try:
        compile(code, "<script>", "exec", dont_inherit=True)  # Only compile; never execute user code.
        tree = ast.parse(code)
    except (SyntaxError, ValueError) as exc:
        line, column = getattr(exc, "lineno", 1) or 1, getattr(exc, "offset", 1) or 1
        reason = exc.msg if isinstance(exc, SyntaxError) else "源码含无效字符"
        raise ScriptContractError(f"脚本语法错误：{reason}（第 {line} 行，第 {column} 列）") from exc

    argument_calls = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
        ):
            argument_calls.append(node)
    argument_calls.sort(key=lambda item: getattr(item, "lineno", 0))
    source_params = [_source_param(node) for node in argument_calls]

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
    warnings = []
    for item in source_params:
        doc = next((entry for entry in documented if entry["name"] == item["name"]), {})
        source_required = "是" if item["required"] else "否"
        param_type = _resolve_type(item["name"], doc.get("type"), item["type"])
        if doc and doc["required"] != source_required:
            raise ScriptContractError(
                f"参数 {item['name']} 的必填标记与 argparse 不一致：头部为 {doc['required']}，argparse 为 {source_required}"
            )
        desc = doc.get("desc", "")
        if param_type == "json":
            warnings.append(
                f"参数 {item['name']} 的类型写成了 json：consumer 只能把它当字符串传，智能体要自己拼 JSON 文本。"
                "请在头部「输入参数」写明 object（JSON 对象）或 array（JSON 数组）"
            )
        if param_type in STRUCTURED_TYPES and not re.search(r"[\[{]", desc):
            warnings.append(
                f"参数 {item['name']} 是 {param_type}，说明里要写清字段并给一个示例"
                '（如 例：[{"order_id":"A-001","qty":2}]）：consumer 的智能体只看得到这句说明'
            )
        params.append({
            "name": item["name"], "flag": item["flag"], "type": param_type,
            "required": source_required,
            "default": item["default"],
            "multiple": "是" if item["multiple"] else "否", "action": item["action"],
            "desc": desc,
        })

    outputs, output_text = _outputs(blocks.get("输出", []))
    output_errors, output_verified = static_outputs(tree, tuple(outputs))
    if output_errors:
        raise ScriptContractError(output_errors[0])
    if not output_verified:
        warnings.append("输出结构无法静态确认，尚未验证；发布前须完成当前源码的运行验证")
    title = next(iter(blocks.get("功能描述", [])), "")
    return ScriptContract(
        bound=bound,
        fragment=fragment,
        title=title,
        params=tuple(params),
        outputs=tuple(outputs),
        output_text=output_text,
        warnings=tuple(warnings),
        output_verified=output_verified,
    )


def script_warnings(code: str, path: str, contract: ScriptContract) -> list[dict]:
    """Bounded save diagnostics; report marker locations without returning comment contents."""
    warnings = [{"path": path, "line": 1, "column": 1, "code": "script_contract_warning", "message": text}
                for text in contract.warnings]
    texts = [(token.string, *token.start) for token in tokenize.generate_tokens(io.StringIO(code).readline)
             if token.type == tokenize.COMMENT]
    for node in ast.walk(ast.parse(code)):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                texts.append((ast.get_source_segment(code, first.value) or "", first.lineno, first.col_offset))
    for text, line, column in texts:
        for offset, part in enumerate(text.splitlines()):
            for marker in re.finditer(r"\b(?:TODO|FIXME)\b", part, re.IGNORECASE):
                name = marker.group().upper()
                warnings.append({"path": path, "line": line + offset,
                                 "column": (column if offset == 0 else 0) + marker.start() + 1,
                                 "code": "unfinished_marker", "message": f"存在 {name} 未完成标记"})
                if len(warnings) >= 50:
                    return warnings[:49] + [{"path": path, "line": 1, "column": 1,
                                            "code": "warnings_truncated", "message": "告警过多，其余未完成标记已省略"}]
    if len(warnings) > 50:
        warnings = warnings[:49] + [{"path": path, "line": 1, "column": 1,
                                    "code": "warnings_truncated", "message": "告警过多，其余告警已省略"}]
    return sorted(warnings, key=lambda item: (item["line"], item["column"]))


def _header_blocks(header: str) -> dict[str, list[str]]:
    """头部注释按「标签：」切段；标签行冒号后的内容也算本段第一行。"""
    blocks: dict[str, list[str]] = {}
    current = None
    for line in header.splitlines():
        stripped = line.strip()
        match = _LABEL_RE.match(stripped)
        if match and not stripped.startswith("-"):
            current = match.group(1)
            blocks.setdefault(current, [])
            if match.group(2).strip():
                blocks[current].append(match.group(2).strip())
            continue
        if current is not None and stripped:
            blocks[current].append(stripped)
    return blocks


def _binding(blocks: dict[str, list[str]]) -> tuple[str, str]:
    value = next(iter(blocks.get("关联operation", [])), "")
    if not value or _NO_BINDING_RE.match(value):
        return "", ""
    operation, _, fragment = value.partition("/")
    return operation.strip(), fragment.strip()


def _documented_params(lines: list[str]) -> list[dict]:
    documented = []
    for line in lines:
        fields = re.split(r"\s{2,}|\t", line)
        if len(fields) < 2:
            continue
        name = fields[0].strip().lstrip("-").strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            continue
        raw_type = fields[1].strip().lower()
        desc = fields[2].strip() if len(fields) > 2 else ""
        required = "是" if "必填" in desc else "否"
        if re.search(r"默认[值为]?\s*[：:]?\s*([^\s，。；;]+)", desc):
            required = "否"
        documented.append({
            "name": name,
            "type": TYPE_ALIASES.get(raw_type, "string"),
            "required": required,
            "desc": desc.replace("|", "，"),
        })
    return documented


def _source_param(node: ast.Call) -> dict:
    flags = [_literal_string(arg) for arg in node.args]
    flags = [flag for flag in flags if flag]
    if not flags:
        raise ScriptContractError("argparse.add_argument 必须声明 option flag（例如 --records）")
    if not any(flag.startswith("-") for flag in flags):
        raise ScriptContractError(
            f"参数 {flags[0]} 是位置参数：consumer 只按 --长选项 传参，请改成 --{flags[0].replace('_', '-')}"
        )
    long_flags = [flag for flag in flags if flag.startswith("--")]
    flag = (long_flags or flags)[0]
    name = str(_literal_keyword(node, "dest") or flag.lstrip("-").replace("-", "_"))
    action = _literal_keyword(node, "action") or ""
    if action not in _PASSABLE_ACTIONS:
        raise ScriptContractError(
            f"参数 {name} 的 action={action!r} consumer 传不进去：只支持 store_true、store_false、append、extend"
        )
    nargs = _literal_keyword(node, "nargs")
    if _collects_several(nargs) and action != "extend":
        raise ScriptContractError(
            f"参数 {name} 用 nargs={nargs!r} 收多个值：consumer 传多个值时会重复 {flag}，argparse 只保留最后一组。"
            '请改用 action="append"，或改成一个 JSON 数组参数（type=json.loads，头部类型写 array）'
        )
    raw_type = _ast_type_name(_keyword_node(node, "type"))
    if raw_type in _UNPASSABLE_TYPES:
        raise ScriptContractError(
            f"参数 {name} 的 type={raw_type} consumer 传不进去（consumer 传入的都是字符串）：{_UNPASSABLE_TYPES[raw_type]}"
        )
    required = _literal_keyword(node, "required")
    if required is None and _keyword_node(node, "required") is not None:
        raise ScriptContractError(f"参数 {name} 的 required 必须是字面量布尔值")
    required = bool(required) if required is not None else False
    if action in _BOOLEAN_ACTIONS:
        if required:
            raise ScriptContractError(f"参数 {name} 是布尔开关，不能设为必填")
        source_type = "boolean"
    else:
        source_type = _SOURCE_TYPES.get(raw_type, "string")
    return {
        "name": name,
        "flag": flag,
        "type": source_type,
        "required": required,
        "default": _descriptor_default(_keyword_node(node, "default"), action),
        "multiple": action in {"append", "extend"},
        "action": action if action in _BOOLEAN_ACTIONS else "",
    }


def _resolve_type(name: str, documented: str | None, source: str) -> str:
    """头部类型与 argparse 实际接收的类型对账，得出 consumer 调用时用的类型。"""
    if source == "boolean":
        allowed = {source}
    elif source in NUMERIC_TYPES:
        allowed = set(NUMERIC_TYPES)  # 互写不算冲突，下面按 argparse 定类型
    else:  # string / json：脚本收到字符串，对象、数组由 consumer 序列化成 JSON 文本
        allowed = {"string", "json", *STRUCTURED_TYPES}
    if documented is not None and documented not in allowed:
        raise ScriptContractError(
            f"参数 {name} 的类型与 argparse 不一致：头部为 {documented}，argparse 为 {source}（{TYPE_HINT}）"
        )
    if documented in STRUCTURED_TYPES:
        return documented
    if source == "json" or documented == "json":
        return "json"
    return source


def _descriptor_default(node: ast.AST | None, action: str) -> str:
    """描述符「默认值」列：consumer 按 JSON 解析，智能体不传时原样补上。

    只写与 argparse 默认值等价的字面量；引用常量、调用函数等写法留空，让脚本自己的默认值生效。
    """
    if action in _BOOLEAN_ACTIONS:
        return "true" if action == "store_false" else "false"
    value = _literal_node(node)
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    if isinstance(value, str):
        # consumer 先按 JSON 解析默认值：「true」「12」这类字符串要加引号才不会被转成布尔或数字
        try:
            json.loads(value)
        except ValueError:
            return value.replace("|", "，")
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple, dict)):
        try:
            return json.dumps(list(value) if isinstance(value, tuple) else value, ensure_ascii=False).replace("|", "，")
        except TypeError:
            return ""
    return ""


def _outputs(lines: list[str]) -> tuple[list[dict], str]:
    outputs = []
    text = []
    for line in lines:
        fields = re.split(r"\s{2,}|\t", line)
        output_type = TYPE_ALIASES.get(fields[1].strip().lower()) if len(fields) >= 2 else None
        if output_type and re.fullmatch(r"[^\W\d][\w.]*", fields[0].strip()):
            outputs.append({
                "name": fields[0].strip(),
                "type": output_type,
                "desc": (fields[2].strip() if len(fields) > 2 else "").replace("|", "，"),
            })
        else:
            text.append(line)
    return outputs, "\n".join(text)


def _literal_string(node: ast.AST) -> str:
    value = _literal_node(node)
    return value if isinstance(value, str) else ""


def _collects_several(nargs) -> bool:
    """nargs 让一个 flag 收多个值（"*"、"+" 或大于 1 的整数）。"""
    if isinstance(nargs, int):
        return nargs != 1
    return nargs in ("*", "+")


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
