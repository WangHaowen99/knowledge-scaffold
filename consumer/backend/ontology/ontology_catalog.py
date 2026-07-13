"""Helpers for ontology context loading and script catalog resolution."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ontology.ontologies import OntologyManifest, get_ontology_registry

ASSET_TEXT_EXTENSIONS = {
    ".conf",
    ".cfg",
    ".ini",
    ".env",
    ".properties",
    ".yaml",
    ".yml",
    ".json",
    ".toml",
    ".txt",
    ".md",
    ".csv",
    ".tsv",
    ".sql",
    ".xml",
}
ASSET_TEXT_FILENAMES = {".env"}
ASSET_MAX_READ_BYTES = 512_000

_SCRIPT_ENTRY_PATTERN = re.compile(r"^-\s*`([^`]+)`\s*->\s*`([^`]+)`\s*$")
_SCRIPT_ARGS_PATTERN = re.compile(r"`args`\s*:\s*`([^`]+)`")
_SCRIPT_PARAMETERS_PATTERN = re.compile(r"`(?:parameters|params)`\s*:\s*`([^`]+)`")
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


def is_supported_asset_file(path: Path) -> bool:
    name = path.name.lower()
    suffix = path.suffix.lower()
    return name in ASSET_TEXT_FILENAMES or suffix in ASSET_TEXT_EXTENSIONS


def supported_asset_formats_text() -> str:
    formats = sorted(ASSET_TEXT_EXTENSIONS)
    return ", ".join(formats)


def find_assets_dir(ontology_dir: Path) -> Optional[Path]:
    preferred = ontology_dir / "assets"
    if preferred.is_dir():
        return preferred
    return None


def build_asset_catalog(ontology_dir: Path) -> List[str]:
    assets_dir = find_assets_dir(ontology_dir)
    if assets_dir is None:
        return []

    keys: List[str] = []
    base = assets_dir.resolve()
    for path in sorted(assets_dir.rglob("*"), key=lambda item: item.relative_to(assets_dir).as_posix().lower()):
        if not path.is_file():
            continue
        try:
            resolved = path.resolve()
            resolved.relative_to(base)
        except ValueError:
            continue
        keys.append(path.relative_to(assets_dir).as_posix())
    return keys


def find_asset_file(ontology_dir: Path, key: str) -> Tuple[Optional[Path], Optional[str]]:
    normalized_key = str(key or "").strip().replace("\\", "/").strip("/")
    if not normalized_key:
        return None, "Error: `key` is required when resource is `asset`."

    assets_dir = find_assets_dir(ontology_dir)
    if assets_dir is None:
        return None, "Error: asset directory not found."

    requested_path = Path(normalized_key)
    if requested_path.is_absolute() or ".." in requested_path.parts:
        return None, "Error: Invalid asset key."

    base = assets_dir.resolve()
    candidate = (base / requested_path).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        return None, "Error: Invalid asset key."

    if not candidate.exists() or not candidate.is_file():
        available = build_asset_catalog(ontology_dir)
        suffix = " Available keys: {}.".format(", ".join(available[:30])) if available else ""
        return None, "Error: asset '{}' not found.{}".format(normalized_key, suffix)

    if not is_supported_asset_file(candidate):
        return None, (
            "Error: Unsupported asset format '{}'. Supported formats: {}."
        ).format(candidate.suffix.lower() or candidate.name, supported_asset_formats_text())

    try:
        if candidate.stat().st_size > ASSET_MAX_READ_BYTES:
            return None, "Error: asset '{}' is too large to read.".format(normalized_key)
    except OSError:
        return None, "Error: failed to inspect asset '{}'.".format(normalized_key)

    return candidate, None


def read_required_asset_text(path: Path, label: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        return path.read_text(encoding="utf-8").strip(), None
    except UnicodeDecodeError:
        return None, "Error: {} is not valid UTF-8 text.".format(label)
    except OSError as exc:
        return None, "Error: failed to read {}: {}".format(label, exc)


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


def public_script_catalog(catalog: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "operation_key": str(entry.get("operation_key") or ""),
            "parameters": _public_script_parameters(entry.get("parameters") or []),
        }
        for entry in catalog
        if str(entry.get("operation_key") or "").strip()
    ]


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
        if text in {"true", "1", "yes", "y"}:
            return True
        if text in {"false", "0", "no", "n"}:
            return False
    return bool(value)


def find_script_entry(catalog: List[Dict[str, Any]], operation_key: str) -> Optional[Dict[str, Any]]:
    normalized_key = str(operation_key or "").strip()
    for entry in catalog:
        if str(entry.get("operation_key") or "").strip() == normalized_key:
            return dict(entry)
    return None


def build_root_payload(*, ontology: OntologyManifest, ontology_dir: Path) -> Dict[str, Any]:
    operations_text = read_optional_text(ontology_dir / "operations-list.md")
    objects_text = read_optional_text(ontology_dir / "objects-list.md")
    scripts_text = read_optional_text(ontology_dir / "scripts-list.md")
    script_catalog = parse_script_catalog(scripts_text, ontology_dir=ontology_dir)

    return {
        "ontology": {
            "name": ontology.name,
            "description": ontology.description,
        },
        "operations_catalog": parse_markdown_table(operations_text) or operations_text,
        "objects_catalog": parse_markdown_table(objects_text) or objects_text,
        "script_catalog": public_script_catalog(script_catalog),
        "assets_catalog": build_asset_catalog(ontology_dir),
    }


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
        "",
        "operations_catalog:",
        *_format_catalog(root_payload.get("operations_catalog")),
        "",
        "objects_catalog:",
        *_format_catalog(root_payload.get("objects_catalog")),
        "",
        "script_catalog:",
        *_format_script_catalog(root_payload.get("script_catalog")),
        "",
        "assets_catalog:",
        *_format_asset_catalog(root_payload.get("assets_catalog")),
    ]


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
        parameters = entry.get("parameters") or []
        if isinstance(parameters, list) and parameters:
            params_text = "; ".join(
                _format_script_parameter(parameter)
                for parameter in parameters
                if isinstance(parameter, dict)
            )
            lines.append("- {} (parameters: {})".format(operation_key, params_text))
        else:
            lines.append("- {} (parameters: none)".format(operation_key))
    return lines or ["- none"]


def _format_asset_catalog(catalog: Any) -> List[str]:
    if not catalog:
        return ["- none"]
    if not isinstance(catalog, list):
        return _format_catalog(catalog)

    keys = sorted(
        {
            str(item or "").strip().replace("\\", "/").strip("/")
            for item in catalog
            if str(item or "").strip()
        }
    )
    if not keys:
        return ["- none"]

    tree: Dict[str, Any] = {}
    for key in keys:
        node = tree
        parts = [part for part in key.split("/") if part]
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = None

    lines = [
        "supported formats: {}".format(supported_asset_formats_text()),
        "key format: pass the shown file key to ontology_context(resource=\"asset\", key=...).",
    ]
    lines.extend(_format_asset_tree(tree, prefix="", parent_key=""))
    return lines


def _format_asset_tree(tree: Dict[str, Any], *, prefix: str, parent_key: str) -> List[str]:
    lines: List[str] = []
    for name in sorted(tree):
        value = tree[name]
        key = "{}/{}".format(parent_key, name).strip("/")
        if isinstance(value, dict):
            lines.append("{}- {}/".format(prefix, name))
            lines.extend(_format_asset_tree(value, prefix=prefix + "  ", parent_key=key))
        elif key == name:
            lines.append("{}- {}".format(prefix, name))
        else:
            lines.append("{}- {} (key: {})".format(prefix, name, key))
    return lines


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

    available = sorted(path.stem for path in directory.glob("*.md") if path.is_file())
    suffix = " Available keys: {}.".format(", ".join(available[:30])) if available else ""
    return None, "Error: {} '{}' not found.{}".format(resource_name, normalized_key, suffix)
