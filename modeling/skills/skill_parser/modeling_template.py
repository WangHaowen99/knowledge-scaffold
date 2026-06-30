"""Compiler for the modeling-template ontology source layout.

The source layout is intentionally narrow:

- ``object/**/*.md`` describes business objects.
- ``logic/**/*.md`` describes business logic.
- ``action/**/*.py`` registers executable scripts.
"""

from __future__ import annotations

import ast
import io
import json
import re
import shutil
import tokenize
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

from .constants import TAG_PRIORITY
from .fs_utils import collect_md_files, normalize_lines, relative_path
from .markdown_utils import (
    build_md_table,
    extract_first_markdown_table,
    extract_section_by_title,
    extract_section_by_titles,
    parse_header_title,
    split_md_row,
)
from .models import OperationSummary

OBJECT_SOURCE_DIR = "object"
LOGIC_SOURCE_DIR = "logic"
ACTION_SOURCE_DIR = "action"

OBJECTS_TARGET_DIR = "objects"
OPERATIONS_TARGET_DIR = "operations"
SCRIPTS_TARGET_DIR = "scripts"

OBJECT_ATTRIBUTES_SOURCE_TITLE = "对象属性清单"
OBJECT_ATTRIBUTES_SOURCE_TITLES = (OBJECT_ATTRIBUTES_SOURCE_TITLE, "实体属性清单")
OBJECT_ATTRIBUTES_TARGET_TITLE = "实体属性清单"
OBJECT_DESC_TITLE = "对象描述"
OBJECT_ALIAS_TITLE = "对象别名"
OBJECT_DESC_TITLES = (OBJECT_DESC_TITLE, "实体描述")
OBJECT_ALIAS_TITLES = (OBJECT_ALIAS_TITLE, "实体别名")

OPERATION_TAG_TITLE = "管理标签"
OPERATION_BUSINESS_DESC_TITLE = "业务描述"
OPERATION_LOGIC_RELATION_TITLE = "与logic的关联"
OPERATION_INPUT_TITLE = "输入"
OPERATION_OUTPUT_TITLE = "输出"
OPERATION_LOGIC_TITLE = "逻辑描述"
TOOL_FLOW_TAG = "工具流程"
SCRIPT_MAPPING_COMMENT_RE = re.compile(
    r"^\s*#\s*(?:工具流程|对应工具流程|operation|operation_key|operation-name|md|md文件)\s*[：:]\s*(.+?)\s*$",
    re.IGNORECASE,
)
SCRIPT_PARAMETERS_COMMENT_RE = re.compile(
    r"^\s*(?:parameters|params|参数)\s*[：:]\s*(.*)$",
    re.IGNORECASE,
)
MINDSDB_CLIENT_MODULE = "agentic.services.mindsdb"
DATA_PROVIDER_CLIENT_MODULE = "agentic.services.data_provider_client"
MINDSDB_CLIENT_NAME = "MindsDBClient"
DATA_PROVIDER_CLIENT_NAME = "DataProviderClient"

SCRIPT_LIST_HEADER = "\n".join(
    [
        "# 工具流程脚本清单",
        "",
        "当工具流程已在此清单中维护对应脚本时，优先调用 `execute_script`，不要先读 operation markdown 再手工复刻逻辑。",
        "",
        "## 脚本映射",
    ]
)


def compile_modeling_template(
    *,
    source_root: Path,
    target_root: Path,
    write_mapping_json: bool = False,
) -> Dict[str, object]:
    source_root = source_root.resolve()
    target_root = target_root.resolve()

    mapping = build_file_mapping(source_root, target_root)
    objects_result = generate_objects(source_root / OBJECT_SOURCE_DIR, target_root / OBJECTS_TARGET_DIR)
    objects_list_result = generate_objects_list(
        source_root / OBJECT_SOURCE_DIR,
        target_root / "objects-list.md",
    )
    operations_result = generate_operations(
        source_root / LOGIC_SOURCE_DIR,
        target_root / OPERATIONS_TARGET_DIR,
    )
    scripts_result = generate_scripts(
        source_root / ACTION_SOURCE_DIR,
        target_root=target_root,
    )

    output: Dict[str, object] = {
        "mapping": mapping,
        "objects_trimmed": {
            "count": objects_result["count"],
            "files": objects_result["files"],
            "warnings": objects_result["warnings"],
            "section_title": OBJECT_ATTRIBUTES_SOURCE_TITLE,
            "output_title": OBJECT_ATTRIBUTES_TARGET_TITLE,
            "columns": ["中文名称", "英文名称", "描述", "属性别名"],
        },
        "objects_list": {
            "path": objects_list_result["path"],
            "count": objects_list_result["count"],
            "warnings": objects_list_result["warnings"],
            "title": "实体清单",
            "columns": ["中文名称", "别名", "业务定义", "领域分类"],
            "alias_section_title": OBJECT_ALIAS_TITLE,
        },
        "operations_trimmed": {
            "count": operations_result["count"],
            "files": operations_result["files"],
            "warnings": operations_result["warnings"],
            "sections": [
                OPERATION_TAG_TITLE,
                OPERATION_INPUT_TITLE,
                OPERATION_OUTPUT_TITLE,
                OPERATION_LOGIC_TITLE,
            ],
            "table_columns": {
                OPERATION_INPUT_TITLE: ["参数名称", "描述", "输入源"],
                OPERATION_OUTPUT_TITLE: ["参数名称", "描述"],
            },
        },
        "operations_count": operations_result["count"],
        "operations_list_path": operations_result["operations_list_path"],
        "objects_list_path": objects_list_result["path"],
        "scripts": scripts_result,
    }

    if write_mapping_json:
        mapping_path = target_root / "skill-file-mapping.json"
        mapping_path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
        output["mapping_path"] = str(mapping_path)

    return output


def build_file_mapping(source_root: Path, target_root: Path) -> Dict[str, object]:
    source_root = source_root.resolve()
    target_root = target_root.resolve()

    object_dir = source_root / OBJECT_SOURCE_DIR
    logic_dir = source_root / LOGIC_SOURCE_DIR
    action_dir = source_root / ACTION_SOURCE_DIR
    skill_path = source_root / "SKILL.md"

    object_files = collect_md_files(object_dir, recursive=True)
    logic_files = collect_md_files(logic_dir, recursive=True)
    script_files = collect_action_files(action_dir)

    warnings: List[str] = []
    if not object_dir.exists():
        warnings.append("缺少 object 目录")
    if not logic_dir.exists():
        warnings.append("缺少 logic 目录")
    if not skill_path.exists():
        warnings.append("缺少 SKILL.md")

    return {
        "source_root": str(source_root),
        "target_root": str(target_root),
        "objects": [
            {"source": relative_path(path, source_root), "target": "{}/{}".format(OBJECTS_TARGET_DIR, path.name)}
            for path in object_files
        ],
        "operations": [
            {
                "source": relative_path(path, source_root),
                "target": "{}/{}".format(OPERATIONS_TARGET_DIR, path.name),
            }
            for path in logic_files
        ],
        "scripts": [
            {"source": relative_path(path, source_root), "target": "{}/{}".format(SCRIPTS_TARGET_DIR, path.name)}
            for path in script_files
        ],
        "objects_list": {
            "source": None,
            "target": "objects-list.md",
            "generator": "from object/**/*.md",
        },
        "skill_mapping": {
            "source": relative_path(skill_path, source_root) if skill_path.exists() else None,
            "targets": ["manifest.yaml", "ONTOLOGY.md"],
        },
        "operations_list": {
            "source": None,
            "target": "operations-list.md",
            "generator": "from logic/**/*.md",
        },
        "scripts_list": {
            "source": None,
            "target": "scripts-list.md",
            "generator": "from action/**/*.py",
        },
        "warnings": warnings,
    }


def collect_py_files(folder: Path) -> List[Path]:
    if not folder.exists() or not folder.is_dir():
        return []
    return sorted(
        [item for item in folder.rglob("*.py") if item.is_file()],
        key=lambda p: str(p.relative_to(folder)).replace("\\", "/").lower(),
    )


def collect_env_files(folder: Path) -> List[Path]:
    if not folder.exists() or not folder.is_dir():
        return []
    return sorted(
        [
            item
            for item in folder.rglob("*")
            if item.is_file() and (item.name == ".env" or item.suffix.lower() == ".env")
        ],
        key=lambda p: str(p.relative_to(folder)).replace("\\", "/").lower(),
    )


def collect_action_files(folder: Path) -> List[Path]:
    files: List[Path] = []
    seen = set()
    for item in [*collect_py_files(folder), *collect_env_files(folder)]:
        resolved = item.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        files.append(item)
    return sorted(
        files,
        key=lambda p: str(p.relative_to(folder)).replace("\\", "/").lower(),
    )


def generate_objects(source_objects_dir: Path, target_objects_dir: Path) -> Dict[str, object]:
    target_objects_dir.mkdir(parents=True, exist_ok=True)
    files: List[str] = []
    warnings: List[str] = []

    for source_file in collect_md_files(source_objects_dir, recursive=True):
        markdown, file_warnings = render_object_file(source_file.read_text(encoding="utf-8"))
        target_file = target_objects_dir / source_file.name
        target_file.write_text(markdown, encoding="utf-8")
        files.append(str(target_file))
        warnings.extend("{}: {}".format(source_file.name, warning) for warning in file_warnings)

    return {"count": len(files), "files": files, "warnings": warnings}


def render_object_file(source_markdown: str) -> Tuple[str, List[str]]:
    section = extract_section_by_titles(normalize_lines(source_markdown), OBJECT_ATTRIBUTES_SOURCE_TITLES)
    table_lines = extract_first_markdown_table(section)
    if not table_lines:
        return "# {}\n\n{}\n".format(
            OBJECT_ATTRIBUTES_TARGET_TITLE,
            build_md_table(["中文名称", "英文名称", "描述", "属性别名"], []),
        ), ["未找到对象属性清单/实体属性清单表格"]

    markdown, warnings = filter_table_columns(
        table_lines,
        ["中文名称", "英文名称", "描述", "属性别名"],
    )
    return "# {}\n\n{}\n".format(OBJECT_ATTRIBUTES_TARGET_TITLE, markdown), warnings


def generate_objects_list(source_objects_dir: Path, target_objects_list_path: Path) -> Dict[str, object]:
    rows: List[List[str]] = []
    warnings: List[str] = []
    for source_file in collect_md_files(source_objects_dir, recursive=True):
        content = source_file.read_text(encoding="utf-8")
        rows.append([
            source_file.stem,
            extract_object_alias(content),
            extract_first_section_text(content, OBJECT_DESC_TITLES),
            "",
        ])

    if not rows:
        warnings.append("object目录下未找到任何.md文件")

    target_objects_list_path.parent.mkdir(parents=True, exist_ok=True)
    markdown = "# 实体清单\n\n{}\n".format(
        build_md_table(["中文名称", "别名", "业务定义", "领域分类"], rows)
    )
    target_objects_list_path.write_text(markdown, encoding="utf-8")
    return {"path": str(target_objects_list_path), "count": len(rows), "warnings": warnings}


def generate_operations(source_logic_dir: Path, target_operations_dir: Path) -> Dict[str, object]:
    target_operations_dir.mkdir(parents=True, exist_ok=True)
    files: List[str] = []
    warnings: List[str] = []
    summaries: List[OperationSummary] = []

    for source_file in collect_md_files(source_logic_dir, recursive=True):
        source_markdown = source_file.read_text(encoding="utf-8")
        rendered, summary, file_warnings = render_operation_file(source_file.stem, source_markdown)
        target_file = target_operations_dir / source_file.name
        target_file.write_text(rendered, encoding="utf-8")
        files.append(str(target_file))
        summaries.append(summary)
        warnings.extend("{}: {}".format(source_file.name, warning) for warning in file_warnings)

    operations_list_path = target_operations_dir.parent / "operations-list.md"
    operations_list_path.write_text(render_operations_list(summaries), encoding="utf-8")
    return {
        "count": len(files),
        "files": files,
        "warnings": warnings,
        "operations_list_path": str(operations_list_path),
    }


def render_operation_file(operation_name: str, source_markdown: str) -> Tuple[str, OperationSummary, List[str]]:
    lines = normalize_lines(source_markdown)
    tag = extract_operation_tag(lines)
    desc = extract_first_section_text(source_markdown, OPERATION_BUSINESS_DESC_TITLE)
    warnings: List[str] = []

    input_markdown, input_warnings = render_input_section(lines, default_input_source(tag))
    output_markdown, output_warnings = render_output_section(lines)
    logic_markdown = render_plain_section(lines, OPERATION_LOGIC_TITLE)
    warnings.extend(input_warnings)
    warnings.extend(output_warnings)
    if not logic_markdown:
        warnings.append("未找到逻辑描述")

    blocks = [
        "# {}\n#{}".format(OPERATION_TAG_TITLE, tag),
        "# {}\n\n{}".format(OPERATION_INPUT_TITLE, input_markdown),
        "# {}\n\n{}".format(OPERATION_OUTPUT_TITLE, output_markdown),
        "# {}\n{}".format(OPERATION_LOGIC_TITLE, logic_markdown).rstrip(),
    ]
    return "\n\n".join(blocks).strip() + "\n", OperationSummary(
        name=operation_name,
        tag=tag,
        business_desc=desc,
    ), warnings


def render_operations_list(operations: Sequence[OperationSummary]) -> str:
    ordered = sorted(operations, key=lambda item: (TAG_PRIORITY.get(item.tag, 99), item.name))
    rows = [[item.name, item.tag, item.business_desc] for item in ordered]
    return "# Operations 逻辑清单\n\n{}\n".format(
        build_md_table(["名称", "标签", "业务描述"], rows)
    )


def generate_scripts(source_action_dir: Path, *, target_root: Path) -> Dict[str, object]:
    script_files = collect_py_files(source_action_dir)
    env_files = collect_env_files(source_action_dir)
    action_files = collect_action_files(source_action_dir)
    target_scripts_dir = target_root / SCRIPTS_TARGET_DIR
    generated: List[str] = []
    mappings: List[Tuple[str, str, List[Dict[str, object]]]] = []
    warnings: List[str] = []

    if action_files:
        target_scripts_dir.mkdir(parents=True, exist_ok=True)

    for source_file in env_files:
        target_file = target_scripts_dir / source_file.name
        shutil.copy2(source_file, target_file)
        generated.append(str(target_file))

    for source_file in script_files:
        target_file = target_scripts_dir / source_file.name
        target_file.write_text(render_script_source(source_file), encoding="utf-8")
        generated.append(str(target_file))
        operation_key = extract_script_operation_key(source_file) or source_file.stem
        if source_file.stem.startswith("_") and operation_key == source_file.stem:
            continue
        parameters, parameter_warnings = extract_script_parameters(source_file)
        warnings.extend("{}: {}".format(source_file.name, warning) for warning in parameter_warnings)
        mappings.append((operation_key, source_file.name, parameters))

    scripts_list_path = target_root / "scripts-list.md"
    if mappings:
        rows: List[str] = []
        for operation_key, script_name, parameters in mappings:
            rows.append("- `{}` -> `scripts/{}`".format(operation_key, script_name))
            if parameters:
                rows.append(
                    "  `parameters`: `{}`".format(
                        json.dumps(parameters, ensure_ascii=False, separators=(",", ":"))
                    )
                )
        scripts_list_path.write_text("{}\n{}\n".format(SCRIPT_LIST_HEADER, "\n".join(rows)), encoding="utf-8")

    return {
        "count": len(action_files),
        "files": generated,
        "warnings": warnings,
        "scripts_list_path": str(scripts_list_path) if mappings else None,
    }


def render_script_source(source_file: Path) -> str:
    source = source_file.read_text(encoding="utf-8")
    return rewrite_business_data_client_imports(source)


def rewrite_business_data_client_imports(source: str) -> str:
    text = str(source or "")
    if not text:
        return text

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except tokenize.TokenError:
        return text

    module_tail_indices = set()
    has_mindsdb_client_import = False
    for index, item in enumerate(tokens):
        if item.type != tokenize.NAME or item.string != "from":
            continue
        import_index, module_tail_index = _find_from_import_module(tokens, index)
        if import_index is None or module_tail_index is None:
            continue
        statement_end = _find_statement_end(tokens, import_index)
        imports_mindsdb_client = any(
            token.type == tokenize.NAME and token.string == MINDSDB_CLIENT_NAME
            for token in tokens[import_index + 1:statement_end]
        )
        if not imports_mindsdb_client:
            continue
        has_mindsdb_client_import = True
        module_tail_indices.add(module_tail_index)

    if not has_mindsdb_client_import:
        return text

    rewritten_tokens = []
    for index, item in enumerate(tokens):
        if index in module_tail_indices:
            rewritten_tokens.append(item._replace(string="data_provider_client"))
            continue
        if item.type == tokenize.NAME and item.string == MINDSDB_CLIENT_NAME:
            rewritten_tokens.append(item._replace(string=DATA_PROVIDER_CLIENT_NAME))
            continue
        rewritten_tokens.append(item)
    return tokenize.untokenize(rewritten_tokens)


def _find_from_import_module(
    tokens: Sequence[tokenize.TokenInfo],
    from_index: int,
) -> Tuple[Optional[int], Optional[int]]:
    module_parts: List[str] = []
    module_name_indices: List[int] = []
    index = from_index + 1
    while index < len(tokens):
        item = tokens[index]
        if item.type == tokenize.NAME and item.string == "import":
            module_name = "".join(module_parts)
            if module_name == MINDSDB_CLIENT_MODULE and module_name_indices:
                return index, module_name_indices[-1]
            return index, None
        if item.type == tokenize.NAME:
            module_parts.append(item.string)
            module_name_indices.append(index)
        elif item.type == tokenize.OP and item.string == ".":
            module_parts.append(item.string)
        elif item.type in {tokenize.NEWLINE, tokenize.ENDMARKER}:
            return None, None
        index += 1
    return None, None


def _find_statement_end(tokens: Sequence[tokenize.TokenInfo], start_index: int) -> int:
    depth = 0
    for index in range(start_index + 1, len(tokens)):
        item = tokens[index]
        if item.type == tokenize.OP:
            if item.string in {"(", "[", "{"}:
                depth += 1
            elif item.string in {")", "]", "}"} and depth > 0:
                depth -= 1
        if item.type == tokenize.NEWLINE and depth == 0:
            return index
        if item.type == tokenize.ENDMARKER:
            return index
    return len(tokens)


def extract_script_operation_key(source_file: Path) -> str:
    for line in read_leading_script_comments(source_file):
        text = str(line or "").strip()
        if not text:
            continue
        match = SCRIPT_MAPPING_COMMENT_RE.match("# " + text)
        if match:
            return normalize_script_operation_key(match.group(1))
    return ""


def read_leading_script_comments(source_file: Path) -> List[str]:
    try:
        lines = source_file.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return []

    comments: List[str] = []
    started = False
    for line in lines[:80]:
        raw = str(line or "")
        text = raw.strip()
        if not text:
            if started:
                comments.append("")
            continue
        left_stripped = raw.lstrip()
        if left_stripped.startswith("#!"):
            started = True
            continue
        if left_stripped.startswith("#"):
            started = True
            payload = left_stripped[1:]
            if payload.startswith(" "):
                payload = payload[1:]
            comments.append(payload.rstrip())
            continue
        if started:
            break
        break
    return comments


def normalize_script_operation_key(value: str) -> str:
    cleaned = str(value or "").strip().strip("`\"' ")
    cleaned = cleaned.replace("\\", "/")
    if "/" in cleaned:
        cleaned = cleaned.rsplit("/", 1)[-1]
    if cleaned.endswith(".md"):
        cleaned = cleaned[:-3]
    return cleaned.strip()


def extract_script_parameters(source_file: Path) -> Tuple[List[Dict[str, object]], List[str]]:
    parameters, warnings = extract_script_comment_parameters(source_file)
    if parameters or warnings:
        return parameters, warnings
    return extract_argparse_parameters(source_file), []


def extract_script_comment_parameters(source_file: Path) -> Tuple[List[Dict[str, object]], List[str]]:
    literal = find_script_parameters_literal(read_leading_script_comments(source_file))
    if literal is None:
        return [], []
    if not literal:
        return [], ["parameters声明为空"]

    try:
        raw_parameters = yaml.safe_load(literal)
    except yaml.YAMLError as exc:
        return [], ["parameters声明解析失败: {}".format(exc)]
    return normalize_script_parameters(raw_parameters)


def find_script_parameters_literal(comment_lines: Sequence[str]) -> Optional[str]:
    for index, line in enumerate(comment_lines):
        match = SCRIPT_PARAMETERS_COMMENT_RE.match(str(line or ""))
        if not match:
            continue
        remainder = match.group(1).strip()
        if remainder:
            return remainder

        block: List[str] = []
        for next_line in comment_lines[index + 1:]:
            text = str(next_line or "")
            stripped = text.strip()
            if not stripped:
                break
            if SCRIPT_MAPPING_COMMENT_RE.match("# " + stripped) or SCRIPT_PARAMETERS_COMMENT_RE.match(stripped):
                break
            if block and not text.startswith((" ", "\t", "-")):
                break
            block.append(text)
        return "\n".join(block).strip()
    return None


def normalize_script_parameters(value: Any) -> Tuple[List[Dict[str, object]], List[str]]:
    if value in (None, ""):
        return [], []
    if not isinstance(value, list):
        return [], ["parameters声明必须是数组"]

    parameters: List[Dict[str, object]] = []
    warnings: List[str] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            warnings.append("parameters第{}项不是对象".format(index))
            continue
        parameter = normalize_script_parameter(item, index=index)
        if parameter:
            parameters.append(parameter)
        else:
            warnings.append("parameters第{}项缺少可用名称".format(index))
    return parameters, warnings


def normalize_script_parameter(item: Dict[str, Any], *, index: int) -> Dict[str, object]:
    flag = str(item.get("flag") or "").strip()
    description = str(item.get("description") or "").strip()
    name = str(item.get("name") or "").strip()
    if not name:
        name = infer_parameter_name(description=description, flag=flag, fallback="arg_{}".format(index))
    if not description:
        description = name

    parameter: Dict[str, object] = {
        "name": name,
        "flag": flag,
        "description": description,
        "required": coerce_bool(item.get("required", True), default=True),
        "type": normalize_parameter_type(item.get("type")),
    }

    if coerce_bool(item.get("multiple", False), default=False):
        parameter["multiple"] = True

    aliases = normalize_aliases(item.get("aliases"))
    flag_alias = alias_from_flag(flag)
    if flag_alias:
        aliases.append(flag_alias)
    aliases = dedupe_strings([alias for alias in aliases if alias and alias != name])
    if aliases:
        parameter["aliases"] = aliases
    return parameter


def extract_argparse_parameters(source_file: Path) -> List[Dict[str, object]]:
    try:
        tree = ast.parse(source_file.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_argument"
    ]
    calls = sorted(calls, key=lambda node: getattr(node, "lineno", 0))
    parameters: List[Dict[str, object]] = []
    for index, call in enumerate(calls, start=1):
        parameter = parameter_from_argparse_call(call, index=index)
        if parameter:
            parameters.append(parameter)
    return parameters


def parameter_from_argparse_call(call: ast.Call, *, index: int) -> Optional[Dict[str, object]]:
    option_strings = [
        value
        for value in (literal_string(arg) for arg in call.args)
        if value
    ]
    if not option_strings:
        return None

    long_flags = [item for item in option_strings if item.startswith("--")]
    short_flags = [item for item in option_strings if item.startswith("-")]
    flag = (long_flags or short_flags or [""])[0]
    positional_name = "" if flag else option_strings[0]
    description = str(keyword_value(call, "help") or "").strip()
    dest = str(keyword_value(call, "dest") or "").strip()
    fallback_name = positional_name or dest or name_from_flag(flag) or "arg_{}".format(index)
    name = infer_parameter_name(description=description, flag=flag, fallback=fallback_name)
    required_value = keyword_value(call, "required")
    required = coerce_bool(required_value, default=not flag)
    action = str(keyword_value(call, "action") or "").strip()
    nargs = keyword_value(call, "nargs")

    parameter: Dict[str, object] = {
        "name": name,
        "flag": flag,
        "description": description or name,
        "required": required,
        "type": argparse_parameter_type(call, action=action),
    }
    if is_multiple_argparse_argument(action=action, nargs=nargs):
        parameter["multiple"] = True

    aliases = [dest, alias_from_flag(flag)]
    if positional_name:
        aliases.append(positional_name)
    aliases = dedupe_strings([alias for alias in aliases if alias and alias != name])
    if aliases:
        parameter["aliases"] = aliases
    return parameter


def literal_string(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def keyword_value(call: ast.Call, name: str) -> Any:
    for keyword in call.keywords:
        if keyword.arg == name:
            return literal_value(keyword.value)
    return None


def literal_value(node: ast.AST) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.List):
        return [literal_value(item) for item in node.elts]
    if isinstance(node, ast.Tuple):
        return [literal_value(item) for item in node.elts]
    return None


def argparse_parameter_type(call: ast.Call, *, action: str) -> str:
    if action in {"store_true", "store_false"}:
        return "boolean"
    raw_type = str(keyword_value(call, "type") or "").strip().lower()
    return normalize_parameter_type(raw_type)


def normalize_parameter_type(value: Any) -> str:
    raw = str(value or "string").strip().lower()
    if raw in {"int", "integer"}:
        return "integer"
    if raw in {"float", "number", "decimal"}:
        return "number"
    if raw in {"bool", "boolean"}:
        return "boolean"
    return "string"


def is_multiple_argparse_argument(*, action: str, nargs: Any) -> bool:
    if action in {"append", "extend"}:
        return True
    if isinstance(nargs, int):
        return nargs != 1
    return str(nargs or "") in {"*", "+"}


def infer_parameter_name(*, description: str, flag: str, fallback: str) -> str:
    desc = re.sub(r"^(?:optional|required)\s+", "", str(description or "").strip(), flags=re.IGNORECASE)
    if contains_cjk(desc):
        candidate = re.split(r"[，,；;。.(（]", desc, maxsplit=1)[0].strip()
        if contains_cjk(candidate) and not re.search(r"\s", candidate):
            return candidate
        match = re.search(r"[A-Za-z0-9_-]*[\u4e00-\u9fff][A-Za-z0-9_\-\u4e00-\u9fff]*", candidate)
        if match:
            return match.group(0)
        match = re.search(r"[A-Za-z0-9_-]*[\u4e00-\u9fff][A-Za-z0-9_\-\u4e00-\u9fff]*", desc)
        if match:
            return match.group(0)
    return fallback or name_from_flag(flag)


def contains_cjk(value: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", str(value or "")))


def name_from_flag(flag: str) -> str:
    stripped = str(flag or "").strip().lstrip("-")
    return stripped.replace("-", "_")


def alias_from_flag(flag: str) -> str:
    return name_from_flag(flag)


def normalize_aliases(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


def dedupe_strings(values: Sequence[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def coerce_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y", "是"}:
        return True
    if text in {"false", "0", "no", "n", "否"}:
        return False
    return default


def extract_object_alias(markdown: str) -> str:
    text = extract_first_section_text(markdown, OBJECT_ALIAS_TITLES)
    if not text:
        return ""
    tokens = [
        token.strip()
        for token in text.replace("\n", "，").replace("、", "，").replace(",", "，").split("，")
        if token.strip()
    ]
    result: List[str] = []
    seen = set()
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        result.append(token)
    return "、".join(result)


def extract_operation_tag(lines: Sequence[str]) -> str:
    if has_leading_tool_flow_marker(lines):
        return TOOL_FLOW_TAG
    if has_parent_logic_reference(lines):
        return "子流程"
    return "主流程"


def has_leading_tool_flow_marker(lines: Sequence[str]) -> bool:
    for line in lines:
        text = str(line or "").strip()
        if not text:
            continue

        title = parse_header_title(text)
        if title.startswith(TOOL_FLOW_TAG):
            return True
        if title in {
            OPERATION_BUSINESS_DESC_TITLE,
            OPERATION_LOGIC_RELATION_TITLE,
            "与object的关联",
            OPERATION_INPUT_TITLE,
            OPERATION_OUTPUT_TITLE,
            OPERATION_LOGIC_TITLE,
        }:
            return False
        return False
    return False


def has_parent_logic_reference(lines: Sequence[str]) -> bool:
    section = extract_section_by_title(lines, OPERATION_LOGIC_RELATION_TITLE)
    for line in section:
        text = str(line or "").strip()
        if "被流程使用" not in text:
            continue
        relation_text = extract_relation_value(text)
        if is_empty_relation_value(relation_text):
            return False
        return True
    return False


def extract_relation_value(line: str) -> str:
    if ">" in line:
        return line.split(">", 1)[1].strip()
    if "：" in line:
        return line.split("：", 1)[1].strip()
    if ":" in line:
        return line.split(":", 1)[1].strip()
    return ""


def is_empty_relation_value(value: str) -> bool:
    normalized = str(value or "").strip().strip(" ，,;；。")
    return not normalized or normalized == "无" or normalized.startswith("无；") or normalized.startswith("无，")


def extract_first_section_text(markdown: str, title: str | Sequence[str]) -> str:
    if isinstance(title, str):
        section = extract_section_by_title(normalize_lines(markdown), title)
    else:
        section = extract_section_by_titles(normalize_lines(markdown), title)
    for line in section:
        text = str(line or "").strip()
        if not text:
            continue
        if text.startswith("{说明："):
            continue
        return text
    return ""


def render_input_section(lines: Sequence[str], input_source: str) -> Tuple[str, List[str]]:
    section = extract_section_by_title(lines, OPERATION_INPUT_TITLE)
    if is_none_section(section):
        return "无", []

    table_lines = extract_first_markdown_table(section)
    if not table_lines:
        return build_md_table(["参数名称", "描述", "输入源"], []), ["输入下未找到表格"]

    headers = split_md_row(table_lines[0])
    rows = []
    for row_line in table_lines[2:]:
        values = row_values_by_header(headers, split_md_row(row_line))
        name = values.get("参数名称", "")
        desc = values.get("描述", "")
        source = values.get("输入源", "") or input_source
        if name or desc or source:
            rows.append([name, desc, source])
    return build_md_table(["参数名称", "描述", "输入源"], rows), []


def render_output_section(lines: Sequence[str]) -> Tuple[str, List[str]]:
    section = extract_section_by_title(lines, OPERATION_OUTPUT_TITLE)
    if is_none_section(section):
        return "无", []

    table_lines = extract_first_markdown_table(section)
    if not table_lines:
        return build_md_table(["参数名称", "描述"], []), ["输出下未找到表格"]

    markdown, warnings = filter_table_columns(table_lines, ["参数名称", "描述"])
    return markdown, warnings


def render_plain_section(lines: Sequence[str], title: str) -> str:
    section = extract_section_by_title(lines, title)
    return "\n".join(line.rstrip() for line in section if line.strip()).strip()


def filter_table_columns(table_lines: Sequence[str], columns: Sequence[str]) -> Tuple[str, List[str]]:
    headers = split_md_row(table_lines[0]) if table_lines else []
    warnings: List[str] = []
    missing = [column for column in columns if column not in headers]
    if missing:
        warnings.append("缺少字段: {}".format("、".join(missing)))

    rows: List[List[str]] = []
    for row_line in table_lines[2:]:
        values = row_values_by_header(headers, split_md_row(row_line))
        rows.append([values.get(column, "") for column in columns])
    return build_md_table(list(columns), rows), warnings


def row_values_by_header(headers: Sequence[str], row: Sequence[str]) -> Dict[str, str]:
    return {
        header: (row[index] if index < len(row) else "")
        for index, header in enumerate(headers)
    }


def default_input_source(tag: str) -> str:
    if tag == "主流程":
        return "用户输入"
    return "父流程传递"


def is_none_section(section: Sequence[str]) -> bool:
    meaningful = [
        str(line or "").strip()
        for line in section
        if str(line or "").strip() and not str(line or "").strip().startswith("{说明：")
    ]
    return bool(meaningful) and all(line == "无" for line in meaningful)
