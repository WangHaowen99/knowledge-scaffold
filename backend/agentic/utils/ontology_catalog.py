"""Helpers for ontology context loading and script catalog resolution."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

from .ontologies import OntologyManifest, get_ontology_registry

_SCRIPT_ENTRY_PATTERN = re.compile(r"^-\s*`([^`]+)`\s*->\s*`([^`]+)`\s*$")
_SCRIPT_ARGS_PATTERN = re.compile(r"`args`\s*:\s*`([^`]+)`")
_SCRIPT_PARAMETERS_PATTERN = re.compile(r"`(?:parameters|params)`\s*:\s*`([^`]+)`")


def split_markdown_frontmatter(markdown: str) -> Tuple[Dict[str, Any], str]:
    text = str(markdown or "")
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    raw_frontmatter = text[3:end].strip()
    body = text[end + 4 :].lstrip("\r\n")
    try:
        parsed = yaml.safe_load(raw_frontmatter) or {}
    except Exception:
        return {}, body
    return (parsed if isinstance(parsed, dict) else {}), body


def read_markdown_frontmatter(path: Path) -> Dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {}
    try:
        frontmatter, _body = split_markdown_frontmatter(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return frontmatter
def get_ontology_manifest(ontology_id: str) -> Tuple[Optional[OntologyManifest], Optional[str]]:
    normalized_id = str(ontology_id or "").strip()
    if not normalized_id:
        return None, "Error: `ontology_id` is required."

    ontology = get_ontology_registry().get_ontology(normalized_id)
    if ontology is None:
        available = sorted(get_ontology_registry().list_enabled_ontologies())
        suffix = " Available ontologies: {}.".format(", ".join(available)) if available else ""
        return None, "Error: Ontology '{}' not found.{}".format(normalized_id, suffix)
    if not ontology.enabled:
        available = sorted(get_ontology_registry().list_enabled_ontologies())
        suffix = " Available ontologies: {}.".format(", ".join(available)) if available else ""
        return None, "Error: Ontology '{}' is disabled.{}".format(normalized_id, suffix)
    return ontology, None


def read_optional_text(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def read_required_text(path: Path, label: str) -> Tuple[Optional[str], Optional[str]]:
    if not path.exists():
        return None, "Error: {} not found.".format(label)
    if not path.is_file():
        return None, "Error: {} is not a file.".format(label)
    return path.read_text(encoding="utf-8").strip(), None


def parse_markdown_table(markdown: str) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    headers: List[str] = []
    for raw_line in str(markdown or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells and all(set(cell.replace(" ", "")) <= {"-"} for cell in cells):
            continue
        if not headers:
            headers = cells
            continue
        if len(cells) != len(headers):
            continue
        rows.append({headers[index]: cells[index] for index in range(len(headers))})
    return rows


def parse_script_catalog(markdown: str, *, ontology_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    catalog: List[Dict[str, Any]] = []
    pending: Optional[Dict[str, Any]] = None
    for raw_line in str(markdown or "").splitlines():
        line = raw_line.strip()
        entry_match = _SCRIPT_ENTRY_PATTERN.match(line)
        if entry_match:
            raw_path = entry_match.group(2).strip()
            pending = {
                "operation_key": entry_match.group(1).strip(),
                "arguments": [],
                "parameters": [],
                "_script_path": raw_path,
                "_is_bound_operation": True,
            }
            if ontology_dir is not None:
                pending["_resolved_script_path"] = str((ontology_dir / raw_path).resolve())
            catalog.append(pending)
            continue

        if pending is None:
            continue

        parameters_match = _SCRIPT_PARAMETERS_PATTERN.search(line)
        args_match = _SCRIPT_ARGS_PATTERN.search(line)
        if not parameters_match and not args_match:
            continue

        raw_args = (parameters_match or args_match).group(1).strip()
        try:
            parsed = json.loads(raw_args)
        except json.JSONDecodeError:
            pending["arguments"] = []
            pending["parameters"] = []
            pending = None
            continue

        if parameters_match:
            pending["parameters"] = _normalize_script_parameters(parsed)
        elif isinstance(parsed, list):
            pending["arguments"] = [str(item) for item in parsed if item is not None]
            pending["parameters"] = _legacy_args_to_parameters(pending["arguments"])
        pending = None
    return catalog


def load_script_catalog(ontology_dir: Path) -> List[Dict[str, Any]]:
    """Load script entries from legacy scripts-list.md and v0.1 scripts/*.md."""
    catalog: List[Dict[str, Any]] = []
    seen: Set[str] = set()

    legacy_entries = parse_script_catalog(
        read_optional_text(ontology_dir / "scripts-list.md"),
        ontology_dir=ontology_dir,
    )
    for entry in legacy_entries:
        key = _script_entry_dedupe_key(entry)
        if key in seen:
            continue
        seen.add(key)
        catalog.append(entry)

    scripts_dir = ontology_dir / "scripts"
    if scripts_dir.is_dir():
        for script_doc in sorted(scripts_dir.glob("*.md"), key=lambda item: item.name.lower()):
            if script_doc.name == "index.md":
                continue
            entry = _script_entry_from_markdown(script_doc, ontology_dir=ontology_dir)
            if not entry:
                continue
            key = _script_entry_dedupe_key(entry)
            if key in seen:
                continue
            seen.add(key)
            catalog.append(entry)

    return catalog


def public_script_catalog(catalog: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        _drop_empty_values({
            "operation_key": str(entry.get("operation_key") or ""),
            "script_key": str(entry.get("script_key") or ""),
            "title": str(entry.get("title") or ""),
            "parameters": _public_script_parameters(entry.get("parameters") or []),
        })
        for entry in catalog
        if entry.get("_is_bound_operation")
        and str(entry.get("operation_key") or entry.get("script_key") or "").strip()
    ]


def _drop_empty_values(value: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, item in value.items():
        if item in ("", None, []):
            continue
        result[key] = item
    return result


def _script_entry_dedupe_key(entry: Dict[str, Any]) -> str:
    operation_key = str(entry.get("operation_key") or "").strip()
    script_key = str(entry.get("script_key") or "").strip()
    return "{}\n{}".format(operation_key, script_key)


def _script_entry_from_markdown(path: Path, *, ontology_dir: Path) -> Optional[Dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    frontmatter, body = split_markdown_frontmatter(text)

    script_key = str(frontmatter.get("key") or path.stem).strip()
    if not script_key:
        return None
    operation_key = str(frontmatter.get("bound_operation") or frontmatter.get("operation_key") or "").strip()
    is_bound_operation = bool(operation_key)
    entrypoint = str(frontmatter.get("entrypoint") or frontmatter.get("path") or "").strip()
    if not entrypoint:
        return None

    entrypoint_path = Path(entrypoint)
    if entrypoint_path.is_absolute() or ".." in entrypoint_path.parts:
        return None
    raw_path = entrypoint
    if not raw_path.replace("\\", "/").startswith("scripts/"):
        raw_path = "scripts/{}".format(raw_path.replace("\\", "/").lstrip("/"))

    parameters = _parse_script_parameters_from_markdown(body)
    entry: Dict[str, Any] = {
        "operation_key": operation_key or script_key,
        "script_key": script_key,
        "title": str(frontmatter.get("title") or script_key).strip(),
        "arguments": [],
        "parameters": parameters,
        "_script_path": raw_path,
        "_script_doc_path": str(path.resolve()),
        "_resolved_script_path": str((ontology_dir / raw_path).resolve()),
        "_is_bound_operation": is_bound_operation,
    }
    return entry


def _parse_script_parameters_from_markdown(markdown: str) -> List[Dict[str, Any]]:
    rows = parse_markdown_table(markdown)
    parameters: List[Dict[str, Any]] = []
    for index, row in enumerate(rows):
        name = _first_row_value(row, "参数 key", "参数key", "参数名称", "name", "Name")
        flag = _first_row_value(row, "CLI 参数", "CLI参数", "flag", "Flag")
        description = _first_row_value(row, "说明", "描述", "description", "Description")
        param_type = _first_row_value(row, "类型", "type", "Type") or "string"
        required_text = _first_row_value(row, "必填", "required", "Required")
        aliases_text = _first_row_value(row, "别名", "aliases", "Aliases")
        if not name and not flag:
            continue
        parameter = {
            "name": name or _name_from_flag(flag) or "arg_{}".format(index + 1),
            "flag": flag,
            "required": _as_bool(required_text, default=True),
            "type": param_type,
            "multiple": _as_bool(_first_row_value(row, "multiple", "是否多值"), default=False),
        }
        if description:
            parameter["description"] = description
        aliases = _split_aliases(aliases_text)
        if aliases:
            parameter["aliases"] = aliases
        parameters.append(parameter)
    return _normalize_script_parameters(parameters)


def _first_row_value(row: Dict[str, str], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value is not None:
            return str(value or "").strip()
    normalized = {str(k).strip().lower().replace(" ", ""): v for k, v in row.items()}
    for key in keys:
        lookup = key.strip().lower().replace(" ", "")
        if lookup in normalized:
            return str(normalized[lookup] or "").strip()
    return ""


def _split_aliases(value: str) -> List[str]:
    if not value:
        return []
    aliases = [
        item.strip()
        for item in re.split(r"[,，、;/；]+", str(value))
        if item.strip()
    ]
    return aliases


def _normalize_script_parameters(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []

    parameters: List[Dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        flag = str(item.get("flag") or "").strip()
        description = str(item.get("description") or "").strip()
        if not name:
            name = _name_from_flag(flag) or "arg_{}".format(index + 1)
        parameter: Dict[str, Any] = {
            "name": name,
            "flag": flag,
            "required": _as_bool(item.get("required", True), default=True),
            "type": str(item.get("type") or "string").strip() or "string",
            "multiple": _as_bool(item.get("multiple", False), default=False),
        }
        if description:
            parameter["description"] = description
        aliases = item.get("aliases")
        if isinstance(aliases, list):
            clean_aliases = [
                str(alias).strip()
                for alias in aliases
                if str(alias or "").strip()
            ]
            if clean_aliases:
                parameter["aliases"] = clean_aliases
        parameters.append(parameter)
    return parameters


def _legacy_args_to_parameters(args: List[str]) -> List[Dict[str, Any]]:
    parameters: List[Dict[str, Any]] = []
    index = 0
    while index < len(args):
        item = str(args[index] or "").strip()
        if not item.startswith("--"):
            index += 1
            continue

        placeholder = str(args[index + 1] or "").strip() if index + 1 < len(args) else ""
        name = _placeholder_name(placeholder) or _name_from_flag(item) or "arg_{}".format(
            len(parameters) + 1
        )
        parameter: Dict[str, Any] = {
            "name": name,
            "flag": item,
            "required": bool(_placeholder_name(placeholder)),
            "type": "string",
            "multiple": False,
        }
        if _placeholder_name(placeholder):
            parameter["description"] = _placeholder_name(placeholder)
        parameters.append(parameter)
        index += 2 if placeholder and not placeholder.startswith("--") else 1
    return parameters


def _public_script_parameters(parameters: Any) -> List[Dict[str, Any]]:
    public: List[Dict[str, Any]] = []
    if not isinstance(parameters, list):
        return public
    for item in parameters:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        parameter: Dict[str, Any] = {
            "name": name,
            "required": _as_bool(item.get("required", True), default=True),
            "type": str(item.get("type") or "string").strip() or "string",
            "multiple": _as_bool(item.get("multiple", False), default=False),
        }
        description = str(item.get("description") or "").strip()
        if description:
            parameter["description"] = description
        aliases = item.get("aliases")
        if isinstance(aliases, list):
            clean_aliases = [
                str(alias).strip()
                for alias in aliases
                if str(alias or "").strip()
            ]
            if clean_aliases:
                parameter["aliases"] = clean_aliases
        public.append(parameter)
    return public


def _placeholder_name(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("<") and text.endswith(">"):
        return text[1:-1].strip()
    return ""


def _name_from_flag(value: str) -> str:
    text = str(value or "").strip().lstrip("-")
    return text.replace("-", "_") if text else ""


def _as_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "y", "是", "必填", "需要", "required"}:
            return True
        if text in {"false", "0", "no", "n", "否", "可选", "不需要", "optional"}:
            return False
    return bool(value)


def find_script_entry(catalog: List[Dict[str, Any]], operation_key: str) -> Optional[Dict[str, Any]]:
    normalized_key = str(operation_key or "").strip()
    for entry in catalog:
        if str(entry.get("operation_key") or "").strip() == normalized_key:
            return dict(entry)
        if str(entry.get("script_key") or "").strip() == normalized_key:
            return dict(entry)
    return None


def build_root_payload(*, ontology: OntologyManifest, ontology_dir: Path) -> Dict[str, Any]:
    root_index_text = read_optional_text(ontology_dir / "index.md")
    operations_text = read_optional_text(ontology_dir / "operations" / "index.md") or read_optional_text(
        ontology_dir / "operations-list.md"
    )
    objects_text = read_optional_text(ontology_dir / "objects" / "index.md") or read_optional_text(
        ontology_dir / "objects-list.md"
    )
    rules_text = read_optional_text(ontology_dir / "rules" / "index.md")
    metrics_text = read_optional_text(ontology_dir / "metrics" / "index.md")
    script_catalog = load_script_catalog(ontology_dir)

    return {
        "ontology": {
            "name": ontology.name,
            "description": ontology.description,
        },
        "root_index": _compact_markdown_body(root_index_text),
        "operations_catalog": parse_markdown_table(operations_text) or operations_text,
        "objects_catalog": parse_markdown_table(objects_text) or objects_text,
        "rules_catalog": parse_markdown_table(rules_text) or rules_text,
        "metrics_catalog": parse_markdown_table(metrics_text) or metrics_text,
        "script_catalog": public_script_catalog(script_catalog),
    }


def _compact_markdown_body(markdown: str) -> str:
    _frontmatter, body = split_markdown_frontmatter(markdown)
    return body.strip()


def format_root_payload_compact(
    *,
    ontology_id: str,
    root_payload: Dict[str, Any],
) -> List[str]:
    ontology = root_payload.get("ontology") if isinstance(root_payload, dict) else {}
    ontology_name = _compact_inline_text(_get_dict_value(ontology, "name"))
    ontology_description = _compact_inline_text(_get_dict_value(ontology, "description"))

    return [
        "id: {}".format(ontology_id),
        "ontology: {}".format(
            " - ".join(part for part in [ontology_name, ontology_description] if part) or "(unnamed)"
        ),
        *_format_optional_block("root_index", root_payload.get("root_index")),
        "",
        "operations_catalog:",
        *_format_catalog(root_payload.get("operations_catalog")),
        "",
        "objects_catalog:",
        *_format_catalog(root_payload.get("objects_catalog")),
        "",
        "rules_catalog:",
        *_format_catalog(root_payload.get("rules_catalog")),
        "",
        "metrics_catalog:",
        *_format_catalog(root_payload.get("metrics_catalog")),
        "",
        "script_catalog:",
        *_format_script_catalog(root_payload.get("script_catalog")),
    ]


def _format_optional_block(label: str, value: Any) -> List[str]:
    text = str(value or "").strip()
    if not text:
        return []
    return ["", "{}:".format(label), *[line.rstrip() for line in text.splitlines() if line.strip()]]


def format_markdown_payload_compact(
    *,
    key_field: str,
    key: str,
    content: str,
) -> str:
    return "{}: {}\n\ncontent:\n{}".format(
        key_field,
        _compact_inline_text(key),
        str(content or "").strip(),
    ).strip()


def _format_catalog(catalog: Any) -> List[str]:
    if not catalog:
        return ["- none"]

    if isinstance(catalog, list):
        rows = [row for row in catalog if isinstance(row, dict)]
        if rows:
            columns = _catalog_columns(rows)
            lines = ["columns: {}".format(" | ".join(columns))]
            for row in rows:
                values = [_compact_inline_text(row.get(column)) for column in columns]
                lines.append("- {}".format(" | ".join(values)))
            return lines

        return [
            "- {}".format(_compact_inline_text(row))
            for row in catalog
            if _compact_inline_text(row)
        ] or ["- none"]

    text = str(catalog or "").strip()
    if not text:
        return ["- none"]
    return [line.rstrip() for line in text.splitlines() if line.strip()]


def _format_script_catalog(catalog: Any) -> List[str]:
    if not catalog:
        return ["- none"]
    if not isinstance(catalog, list):
        return _format_catalog(catalog)

    lines: List[str] = [
        "parameter format: name=description required|optional type; type[] means pass an array value."
    ]
    for entry in catalog:
        if not isinstance(entry, dict):
            text = _compact_inline_text(entry)
            if text:
                lines.append("- {}".format(text))
            continue
        operation_key = _compact_inline_text(entry.get("operation_key"))
        if not operation_key:
            continue
        title = _compact_inline_text(entry.get("title"))
        label = "{} - {}".format(operation_key, title) if title and title != operation_key else operation_key
        parameters = entry.get("parameters") or []
        if isinstance(parameters, list) and parameters:
            params_text = "; ".join(
                _format_script_parameter(parameter)
                for parameter in parameters
                if isinstance(parameter, dict)
            )
            lines.append("- {} (parameters: {})".format(label, params_text))
        else:
            lines.append("- {} (parameters: none)".format(label))
    return lines or ["- none"]


def _format_script_parameter(parameter: Dict[str, Any]) -> str:
    name = _compact_inline_text(parameter.get("name")) or "arg"
    required = "required" if parameter.get("required", True) else "optional"
    param_type = _compact_inline_text(parameter.get("type")) or "string"
    if parameter.get("multiple"):
        param_type = "{}[]".format(param_type)
    description = _compact_inline_text(parameter.get("description"))
    if description and description != name:
        return "{}={} {} {}".format(name, description, required, param_type)
    return "{} {} {}".format(name, required, param_type)


def _catalog_columns(rows: List[Dict[str, Any]]) -> List[str]:
    columns: List[str] = []
    for row in rows:
        for key in row:
            key_text = str(key or "").strip()
            if key_text and key_text not in columns:
                columns.append(key_text)
    return columns


def _get_dict_value(value: Any, key: str) -> Any:
    if not isinstance(value, dict):
        return ""
    return value.get(key)


def _compact_inline_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def find_markdown_file(directory: Path, key: str, resource_name: str) -> Tuple[Optional[Path], Optional[str]]:
    normalized_key = str(key or "").strip()
    if not normalized_key:
        return None, "Error: `key` is required when resource is `{}`.".format(resource_name)

    candidate = (directory / "{}.md".format(normalized_key)).resolve()
    try:
        candidate.relative_to(directory.resolve())
    except ValueError:
        return None, "Error: Invalid {} key.".format(resource_name)
    if candidate.exists() and candidate.is_file():
        return candidate, None

    if directory.is_dir():
        for path in sorted(directory.glob("*.md"), key=lambda item: item.name.lower()):
            frontmatter = read_markdown_frontmatter(path)
            candidates = {
                str(frontmatter.get("key") or "").strip(),
                str(frontmatter.get("canonical_key") or "").strip(),
                str(frontmatter.get("title") or "").strip(),
                path.stem,
            }
            aliases = frontmatter.get("aliases")
            if isinstance(aliases, list):
                candidates.update(str(alias or "").strip() for alias in aliases)
            if normalized_key in {candidate for candidate in candidates if candidate}:
                return path, None

    available = sorted(_markdown_available_key(path) for path in directory.glob("*.md") if path.is_file())
    suffix = " Available keys: {}.".format(", ".join(available[:30])) if available else ""
    return None, "Error: {} '{}' not found.{}".format(resource_name, normalized_key, suffix)


def _markdown_available_key(path: Path) -> str:
    frontmatter = read_markdown_frontmatter(path)
    return str(frontmatter.get("key") or path.stem).strip() or path.stem
