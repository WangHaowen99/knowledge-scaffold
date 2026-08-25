"""One-release converter from the legacy ontology layout to package schema v0.1.

This module is intentionally isolated from runtime loading.  It is called only
at the upload/import boundary and can be deleted once legacy package intake is
retired.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from ontology.ontology_catalog import (
    parse_markdown_table,
    read_markdown_frontmatter,
    split_object_aliases,
)


V01_SCHEMA_VERSION = "0.1"
LEGACY_ROOT_DOCUMENT = "ONTOLOGY.md"
LEGACY_CATALOGS = {
    "objects-list.md": ("objects/index.md", "object_index", "objects", "对象目录"),
    "operations-list.md": ("operations/index.md", "operation_index", "operations", "操作目录"),
    "rules-list.md": ("rules/index.md", "rule_index", "rules", "规则目录"),
    "metrics-list.md": ("metrics/index.md", "metric_index", "metrics", "指标目录"),
}
LEGACY_SCRIPT_CATALOG = "scripts-list.md"
LEGACY_FILE_NAMES = frozenset({LEGACY_ROOT_DOCUMENT, LEGACY_SCRIPT_CATALOG, *LEGACY_CATALOGS})
_SCRIPT_ENTRY_PATTERN = re.compile(r"^-\s*`([^`]+)`\s*->\s*`([^`]+)`\s*$")
_SCRIPT_ARGS_PATTERN = re.compile(r"`args`\s*:\s*`([^`]+)`")
_SCRIPT_PARAMETERS_PATTERN = re.compile(r"`(?:parameters|params)`\s*:\s*`([^`]+)`")
_FIELD_ALIAS_HEADER = "属性别名"
_EMPTY_FIELD_ALIAS_VALUES = frozenset({"-", "无"})
_EMPTY_OBJECT_ALIAS_VALUES = frozenset({"-", "无"})
_OBJECT_KEY_HEADERS = ("object key", "object_key", "key", "中文名称")
_OBJECT_TITLE_HEADERS = ("标题", "title")
_OBJECT_ALIAS_HEADERS = ("别名", "aliases", "alias")
_OBJECT_DESCRIPTION_HEADERS = ("业务定义", "说明", "description")


def convert_legacy_package_in_place(root: Path) -> tuple[list[str], list[str]]:
    """Convert a copied legacy package in place and remove every legacy list file."""
    package_root = Path(root)
    converted: list[str] = []
    warnings: list[str] = []

    manifest = _load_manifest(package_root)
    _write_v01_manifest(package_root, manifest)
    converted.append("manifest.yaml")

    root_index = package_root / "index.md"
    legacy_root = package_root / LEGACY_ROOT_DOCUMENT
    if not root_index.is_file() and legacy_root.is_file():
        _write_markdown_resource(
            root_index,
            resource="root",
            key="root",
            title=str(manifest.get("name") or "本体说明"),
            body=legacy_root.read_text(encoding="utf-8"),
        )
        converted.append("index.md")

    for legacy_name, (index_name, resource, key, title) in LEGACY_CATALOGS.items():
        legacy_path = package_root / legacy_name
        index_path = package_root / index_name
        if not index_path.is_file() and legacy_path.is_file():
            legacy_markdown = legacy_path.read_text(encoding="utf-8")
            if legacy_name == "objects-list.md":
                legacy_markdown, _ = _normalize_legacy_object_catalog(
                    legacy_markdown
                )
            _write_markdown_resource(
                index_path,
                resource=resource,
                key=key,
                title=title,
                body=legacy_markdown,
            )
            converted.append(index_name)
            if legacy_name == "objects-list.md":
                converted.extend(
                    _convert_legacy_object_details(package_root, legacy_markdown)
                )

    legacy_scripts = package_root / LEGACY_SCRIPT_CATALOG
    if legacy_scripts.is_file():
        descriptor_paths, script_warnings = _convert_script_catalog(
            package_root,
            legacy_scripts.read_text(encoding="utf-8"),
        )
        converted.extend(descriptor_paths)
        warnings.extend(script_warnings)

    converted.extend(_normalize_legacy_object_field_aliases(package_root))

    removed = _remove_legacy_artifacts(package_root)
    converted.extend("removed:{}".format(path) for path in removed)
    return converted, warnings


def finalize_v01_package_in_place(root: Path) -> tuple[list[str], list[str]]:
    """Make the imported copy a complete v0.1 package without legacy artifacts."""
    package_root = Path(root)
    generated: list[str] = []
    warnings: list[str] = []
    manifest = _load_manifest(package_root)
    _write_v01_manifest(package_root, manifest)

    required_indexes = (
        ("index.md", "root", "root", str(manifest.get("name") or "本体说明")),
        ("objects/index.md", "object_index", "objects", "对象目录"),
        ("operations/index.md", "operation_index", "operations", "操作目录"),
        ("rules/index.md", "rule_index", "rules", "规则目录"),
        ("metrics/index.md", "metric_index", "metrics", "指标目录"),
    )
    for relative, resource, key, title in required_indexes:
        path = package_root / relative
        if path.is_file():
            continue
        _write_markdown_resource(path, resource=resource, key=key, title=title, body="")
        generated.append(relative)

    scripts_dir = package_root / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    scripts_index = scripts_dir / "index.md"
    if not scripts_index.is_file():
        _write_scripts_index(package_root)
        generated.append("scripts/index.md")

    mappings_path = package_root / "mappings" / "object-data-mappings.json"
    if not mappings_path.is_file():
        mappings_path.parent.mkdir(parents=True, exist_ok=True)
        mappings_path.write_text(
            json.dumps(
                {"schema_version": V01_SCHEMA_VERSION, "objects": {}},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        generated.append("mappings/object-data-mappings.json")

    removed = _remove_legacy_artifacts(package_root)
    if removed:
        warnings.append("Removed legacy artifacts: {}".format(", ".join(removed)))
    return generated, warnings


def _load_manifest(root: Path) -> dict[str, Any]:
    manifest_path = root / "manifest.yaml"
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError("Invalid ontology package manifest") from exc
    if not isinstance(manifest, dict):
        raise ValueError("Ontology package manifest must be a mapping")
    return dict(manifest)


def _write_v01_manifest(root: Path, manifest: dict[str, Any]) -> None:
    normalized = {
        "kind": "ontology-package",
        "schema_version": 0.1,
        **{
            key: value
            for key, value in manifest.items()
            if key not in {"kind", "schema_version"}
        },
    }
    normalized.setdefault("language", "zh-CN")
    (root / "manifest.yaml").write_text(
        yaml.safe_dump(normalized, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _write_markdown_resource(
    path: Path,
    *,
    resource: str,
    key: str,
    title: str,
    body: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = yaml.safe_dump(
        {"resource": resource, "key": key, "title": title},
        allow_unicode=True,
        sort_keys=False,
    ).strip()
    clean_body = _without_frontmatter(body).strip()
    suffix = "\n\n{}\n".format(clean_body) if clean_body else "\n"
    path.write_text("---\n{}\n---{}".format(frontmatter, suffix), encoding="utf-8")


def _without_frontmatter(markdown: str) -> str:
    text = str(markdown or "")
    if not text.startswith("---"):
        return text
    match = re.match(r"^---\s*\r?\n.*?\r?\n---\s*(?:\r?\n|$)(.*)$", text, re.DOTALL)
    return match.group(1) if match else text


def _convert_legacy_object_details(root: Path, catalog_markdown: str) -> list[str]:
    """Project legacy object catalog metadata into object detail frontmatter."""
    converted: list[str] = []
    objects_dir = root / "objects"
    if not objects_dir.is_dir():
        return converted

    rows = parse_markdown_table(catalog_markdown)
    rows_by_key: dict[str, dict[str, str]] = {}
    for row in rows:
        object_key = _first_row_value(row, _OBJECT_KEY_HEADERS)
        if object_key:
            rows_by_key[object_key] = row
    for path in sorted(objects_dir.glob("*.md"), key=lambda item: item.name.lower()):
        if path.name.lower() == "index.md":
            continue
        markdown = path.read_text(encoding="utf-8")
        metadata, _frontmatter, body = _read_object_frontmatter(root, path, markdown)
        existing_key = str(metadata.get("key") or "").strip()
        row = rows_by_key.get(existing_key or path.stem)
        if row is None:
            continue

        object_key = _first_row_value(row, _OBJECT_KEY_HEADERS)
        metadata["resource"] = "object"
        metadata["key"] = object_key
        metadata.setdefault(
            "title",
            _first_row_value(row, _OBJECT_TITLE_HEADERS) or object_key,
        )
        description = _first_row_value(row, _OBJECT_DESCRIPTION_HEADERS)
        if description:
            metadata.setdefault("description", description)

        if _row_has_header(row, _OBJECT_ALIAS_HEADERS):
            aliases = split_object_aliases(
                _first_row_value(row, _OBJECT_ALIAS_HEADERS)
            )
            if aliases:
                metadata["aliases"] = aliases
            else:
                metadata.pop("aliases", None)

        _write_markdown_with_frontmatter(
            path,
            metadata=metadata,
            body=body,
        )
        converted.append(path.relative_to(root).as_posix())
    return converted


def _read_object_frontmatter(
    root: Path,
    path: Path,
    markdown: str,
) -> tuple[dict[str, Any], str, str]:
    text = str(markdown or "")
    opening = re.match(r"^---[ \t]*\r?\n", text)
    if opening is None:
        return {}, "", text

    closing = re.search(
        r"^---[ \t]*(?:\r?\n|$)",
        text[opening.end() :],
        re.MULTILINE,
    )
    if closing is None:
        relative = path.relative_to(root).as_posix()
        raise ValueError(
            "Invalid frontmatter in {}: missing closing delimiter.".format(relative)
        )

    frontmatter_start = opening.end()
    frontmatter_end = frontmatter_start + closing.start()
    body_start = frontmatter_start + closing.end()
    raw_frontmatter = text[frontmatter_start:frontmatter_end]
    try:
        parsed = yaml.safe_load(raw_frontmatter)
    except yaml.YAMLError as exc:
        relative = path.relative_to(root).as_posix()
        raise ValueError(
            "Invalid frontmatter in {}: invalid YAML.".format(relative)
        ) from exc

    if parsed is None:
        metadata: dict[str, Any] = {}
    elif isinstance(parsed, dict):
        metadata = dict(parsed)
    else:
        relative = path.relative_to(root).as_posix()
        raise ValueError(
            "Invalid frontmatter in {}: expected a YAML mapping.".format(relative)
        )
    return metadata, text[:body_start], text[body_start:]


def _first_row_value(row: dict[str, str], candidates: tuple[str, ...]) -> str:
    normalized = {
        str(header or "").strip().casefold(): str(value or "").strip()
        for header, value in row.items()
    }
    for candidate in candidates:
        value = normalized.get(candidate.casefold())
        if value:
            return value
    return ""


def _row_has_header(row: dict[str, str], candidates: tuple[str, ...]) -> bool:
    headers = {str(header or "").strip().casefold() for header in row}
    return any(candidate.casefold() in headers for candidate in candidates)


def _write_markdown_with_frontmatter(
    path: Path,
    *,
    metadata: dict[str, Any],
    body: str,
) -> None:
    frontmatter = yaml.safe_dump(
        metadata,
        allow_unicode=True,
        sort_keys=False,
    ).strip()
    clean_body = str(body or "").strip()
    suffix = "\n\n{}\n".format(clean_body) if clean_body else "\n"
    path.write_text(
        "---\n{}\n---{}".format(frontmatter, suffix),
        encoding="utf-8",
    )


def _normalize_legacy_object_field_aliases(root: Path) -> list[str]:
    """Remove legacy no-value markers from object field alias cells."""
    normalized: list[str] = []
    objects_dir = root / "objects"
    if not objects_dir.is_dir():
        return normalized

    for path in sorted(objects_dir.glob("*.md"), key=lambda item: item.name.lower()):
        if path.name.lower() == "index.md":
            continue
        markdown = path.read_text(encoding="utf-8")
        _metadata, frontmatter, body = _read_object_frontmatter(
            root,
            path,
            markdown,
        )
        updated_body, changed = _normalize_field_alias_table(body)
        if not changed:
            continue
        path.write_text(frontmatter + updated_body, encoding="utf-8")
        normalized.append(path.relative_to(root).as_posix())
    return normalized


def _normalize_field_alias_table(markdown: str) -> tuple[str, bool]:
    return _normalize_first_legacy_table(
        markdown,
        required_headers=(_FIELD_ALIAS_HEADER,),
        alias_headers=(_FIELD_ALIAS_HEADER,),
        empty_values=_EMPTY_FIELD_ALIAS_VALUES,
        replacement="  ",
        canonicalize=False,
    )


def _normalize_legacy_object_catalog(markdown: str) -> tuple[str, bool]:
    return _normalize_first_legacy_table(
        markdown,
        required_headers=_OBJECT_KEY_HEADERS,
        alias_headers=_OBJECT_ALIAS_HEADERS,
        empty_values=_EMPTY_OBJECT_ALIAS_VALUES,
        replacement=" - ",
        canonicalize=True,
    )


def _normalize_first_legacy_table(
    markdown: str,
    *,
    required_headers: tuple[str, ...],
    alias_headers: tuple[str, ...],
    empty_values: frozenset[str],
    replacement: str,
    canonicalize: bool,
) -> tuple[str, bool]:
    lines = str(markdown or "").splitlines(keepends=True)
    ignored_lines = _ignored_markdown_lines(lines)
    table = _find_first_legacy_table(
        lines,
        ignored_lines,
        required_headers,
    )
    if table is None:
        return "".join(lines), False

    start, end, header_cells = table
    normalized_alias_headers = {header.casefold() for header in alias_headers}
    alias_column = next(
        (
            column
            for column, header in enumerate(header_cells)
            if header.strip().casefold() in normalized_alias_headers
        ),
        None,
    )
    changed = False
    for index in range(start, end):
        cells = _split_markdown_table_row(lines[index])
        if cells is None:
            continue
        if (
            index >= start + 2
            and alias_column is not None
            and cells[alias_column].strip() in empty_values
        ):
            cells[alias_column] = replacement
        updated = (
            _canonicalize_markdown_table_row(lines[index], cells)
            if canonicalize
            else _rebuild_markdown_table_row(lines[index], cells)
        )
        if updated != lines[index]:
            lines[index] = updated
            changed = True

    return "".join(lines), changed


def _find_first_legacy_table(
    lines: list[str],
    ignored_lines: list[bool],
    required_headers: tuple[str, ...],
) -> tuple[int, int, list[str]] | None:
    normalized_required = {header.casefold() for header in required_headers}
    for index in range(len(lines) - 1):
        if ignored_lines[index] or ignored_lines[index + 1]:
            continue
        header_cells = _split_markdown_table_row(lines[index])
        separator_cells = _split_markdown_table_row(lines[index + 1])
        if header_cells is None or separator_cells is None:
            continue
        if len(header_cells) != len(separator_cells):
            continue
        if not _is_markdown_separator_row(separator_cells):
            continue
        has_required_header = any(
            header.strip().casefold() in normalized_required
            for header in header_cells
        )
        if not has_required_header:
            continue

        end = index + 2
        while end < len(lines) and not ignored_lines[end]:
            cells = _split_markdown_table_row(lines[end])
            if (
                cells is None
                or len(cells) != len(header_cells)
                or _is_markdown_separator_row(cells)
            ):
                break
            end += 1
        return index, end, header_cells
    return None


def _split_markdown_table_row(line: str) -> list[str] | None:
    raw = str(line or "")
    newline_stripped = raw.rstrip("\r\n")
    stripped = newline_stripped.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if _has_unescaped_trailing_pipe(stripped):
        stripped = stripped[:-1]
    cells = re.split(r"(?<!\\)\|", stripped)
    if len(cells) < 2:
        return None
    return cells


def _rebuild_markdown_table_row(line: str, cells: list[str]) -> str:
    raw = str(line or "")
    newline = raw[len(raw.rstrip("\r\n")) :]
    indentation = raw[: len(raw) - len(raw.lstrip())]
    stripped = raw.rstrip("\r\n").strip()
    leading_pipe = "|" if stripped.startswith("|") else ""
    trailing_pipe = "|" if _has_unescaped_trailing_pipe(stripped) else ""
    return "{}{}{}{}{}".format(
        indentation,
        leading_pipe,
        "|".join(cells),
        trailing_pipe,
        newline,
    )


def _canonicalize_markdown_table_row(line: str, cells: list[str]) -> str:
    raw = str(line or "")
    newline = raw[len(raw.rstrip("\r\n")) :]
    return "| {} |{}".format(" | ".join(cell.strip() for cell in cells), newline)


def _has_unescaped_trailing_pipe(value: str) -> bool:
    text = str(value or "").rstrip()
    if not text.endswith("|"):
        return False
    preceding = text[:-1]
    backslash_count = len(preceding) - len(preceding.rstrip("\\"))
    return backslash_count % 2 == 0


def _ignored_markdown_lines(lines: list[str]) -> list[bool]:
    ignored = [False] * len(lines)
    fence_character = ""
    fence_length = 0
    in_html_comment = False

    for index, line in enumerate(lines):
        if fence_character:
            ignored[index] = True
            closing = re.match(
                r"^\s*({}{{{},}})\s*$".format(
                    re.escape(fence_character),
                    fence_length,
                ),
                line.rstrip("\r\n"),
            )
            if closing is not None:
                fence_character = ""
                fence_length = 0
            continue

        if in_html_comment:
            ignored[index] = True
            if "-->" in line:
                in_html_comment = False
            continue

        if line.startswith("    ") or line.startswith("\t"):
            ignored[index] = True
            continue

        fence = re.match(r"^\s*(`{3,}|~{3,})", line)
        if fence is not None:
            marker = fence.group(1)
            fence_character = marker[0]
            fence_length = len(marker)
            ignored[index] = True
            continue

        comment_start = line.find("<!--")
        if comment_start >= 0:
            ignored[index] = True
            if line.find("-->", comment_start + 4) < 0:
                in_html_comment = True

    return ignored


def _is_markdown_separator_row(cells: list[str]) -> bool:
    return bool(cells) and all(
        re.fullmatch(r"\s*:?-{3,}:?\s*", cell) is not None for cell in cells
    )


def _convert_script_catalog(root: Path, markdown: str) -> tuple[list[str], list[str]]:
    generated: list[str] = []
    warnings: list[str] = []
    used_keys = {
        str(read_markdown_frontmatter(path).get("key") or path.stem).strip()
        for path in (root / "scripts").glob("*.md")
        if path.name.lower() != "index.md"
    }

    for entry in _parse_legacy_script_catalog(markdown, ontology_dir=root):
        operation_key = str(entry.get("operation_key") or "").strip()
        raw_script_path = str(entry.get("_script_path") or "").strip().replace("\\", "/")
        script_path = _safe_script_path(root, raw_script_path)
        if script_path is None:
            warnings.append("Skipped unsafe or missing legacy script: {}".format(raw_script_path))
            continue

        script_key = _unique_script_key(script_path.stem, used_keys)
        used_keys.add(script_key)
        descriptor = root / "scripts" / "{}.md".format(script_key)
        if descriptor.is_file():
            continue
        _write_script_descriptor(
            descriptor,
            script_key=script_key,
            operation_key=operation_key,
            entrypoint=script_path.relative_to(
                (root / "scripts").resolve(strict=True)
            ).as_posix(),
            parameters=entry.get("parameters") or [],
        )
        generated.append(descriptor.relative_to(root).as_posix())

    _write_scripts_index(root)
    generated.append("scripts/index.md")
    return generated, warnings


def _safe_script_path(root: Path, raw_path: str) -> Path | None:
    if not raw_path or re.match(r"^[A-Za-z]:", raw_path):
        return None
    relative = Path(raw_path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        return None
    if relative.parts[0] != "scripts":
        relative = Path("scripts") / relative
    candidate = (root / relative).resolve(strict=False)
    try:
        candidate.relative_to((root / "scripts").resolve(strict=True))
    except (OSError, ValueError):
        return None
    if candidate.is_symlink() or not candidate.is_file():
        return None
    return candidate


def _unique_script_key(candidate: str, used: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(candidate or "script")).strip("_.-") or "script"
    key = base
    index = 2
    while key in used:
        key = "{}_{}".format(base, index)
        index += 1
    return key


def _write_script_descriptor(
    path: Path,
    *,
    script_key: str,
    operation_key: str,
    entrypoint: str,
    parameters: list[dict[str, Any]],
) -> None:
    frontmatter: dict[str, Any] = {
        "resource": "script",
        "key": script_key,
        "title": script_key,
        "runtime": "python",
        "entrypoint": entrypoint,
    }
    if operation_key:
        frontmatter["bound_operation"] = operation_key
    lines = [
        "---",
        yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip(),
        "---",
        "",
        "# 输入参数",
        "",
        "| 参数 key | CLI 参数 | 类型 | 必填 | 默认值 | 是否多值 | 说明 | 别名 | Action |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for parameter in parameters:
        aliases = parameter.get("aliases") or []
        lines.append(
            "| {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
                _table_cell(parameter.get("name")),
                _table_cell(parameter.get("flag")),
                _table_cell(parameter.get("type") or "string"),
                "是" if parameter.get("required", True) else "否",
                _table_cell(_format_parameter_default(parameter)),
                "是" if parameter.get("multiple", False) else "否",
                _table_cell(parameter.get("description")),
                _table_cell("，".join(str(alias) for alias in aliases)),
                _table_cell(parameter.get("action")),
            )
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_scripts_index(root: Path) -> None:
    scripts_dir = root / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    rows: list[str] = []
    for descriptor in sorted(scripts_dir.glob("*.md"), key=lambda item: item.name.lower()):
        if descriptor.name.lower() == "index.md":
            continue
        frontmatter = read_markdown_frontmatter(descriptor)
        script_key = str(frontmatter.get("key") or descriptor.stem).strip()
        title = str(frontmatter.get("title") or script_key).strip()
        operation = str(
            frontmatter.get("bound_operation") or frontmatter.get("operation_key") or ""
        ).strip()
        entrypoint = str(frontmatter.get("entrypoint") or frontmatter.get("path") or "").strip()
        rows.append(
            "| {} | {} | {} | {} |".format(
                _table_cell(script_key),
                _table_cell(title),
                _table_cell(operation),
                _table_cell(entrypoint),
            )
        )
    body = "\n".join(
        [
            "# 脚本目录",
            "",
            "| script key | 标题 | 绑定 operation | 入口 |",
            "| --- | --- | --- | --- |",
            *rows,
        ]
    )
    _write_markdown_resource(
        scripts_dir / "index.md",
        resource="script_index",
        key="scripts",
        title="脚本目录",
        body=body,
    )


def _table_cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip()


def _parse_legacy_script_catalog(markdown: str, *, ontology_dir: Path) -> list[dict[str, Any]]:
    catalog: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None
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
                "_resolved_script_path": str((ontology_dir / raw_path).resolve()),
            }
            catalog.append(pending)
            continue
        if pending is None:
            continue

        parameters_match = _SCRIPT_PARAMETERS_PATTERN.search(line)
        args_match = _SCRIPT_ARGS_PATTERN.search(line)
        if not parameters_match and not args_match:
            continue
        raw_value = (parameters_match or args_match).group(1).strip()
        try:
            parsed = json.loads(raw_value)
        except json.JSONDecodeError:
            pending = None
            continue
        if parameters_match:
            pending["parameters"] = _normalize_legacy_parameters(parsed)
        elif isinstance(parsed, list):
            pending["arguments"] = [str(item) for item in parsed if item is not None]
            pending["parameters"] = _legacy_args_to_parameters(pending["arguments"])
        pending = None
    return catalog


def _normalize_legacy_parameters(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    parameters: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            continue
        flag = str(item.get("flag") or "").strip()
        name = str(item.get("name") or "").strip() or _name_from_flag(flag) or "arg_{}".format(index + 1)
        parameter: dict[str, Any] = {
            "name": name,
            "flag": flag,
            "required": _as_bool(item.get("required", True), default=True),
            "type": str(item.get("type") or "string").strip() or "string",
            "multiple": _as_bool(item.get("multiple", False), default=False),
        }
        description = str(item.get("description") or "").strip()
        if description:
            parameter["description"] = description
        if "default" in item and item.get("default") is not None:
            parameter["default"] = item.get("default")
        aliases = item.get("aliases")
        if isinstance(aliases, list):
            clean_aliases = [str(alias).strip() for alias in aliases if str(alias or "").strip()]
            if clean_aliases:
                parameter["aliases"] = clean_aliases
        action = str(item.get("action") or "").strip().lower()
        if action in {"store_true", "store_false"}:
            parameter["action"] = action
        parameters.append(parameter)
    return parameters


def _format_parameter_default(parameter: dict[str, Any]) -> str:
    if "default" not in parameter or parameter.get("default") is None:
        return ""
    value = parameter.get("default")
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _legacy_args_to_parameters(args: list[str]) -> list[dict[str, Any]]:
    parameters: list[dict[str, Any]] = []
    index = 0
    while index < len(args):
        flag = str(args[index] or "").strip()
        if not flag.startswith("--"):
            index += 1
            continue
        placeholder = str(args[index + 1] or "").strip() if index + 1 < len(args) else ""
        placeholder_name = _placeholder_name(placeholder)
        parameter: dict[str, Any] = {
            "name": placeholder_name or _name_from_flag(flag) or "arg_{}".format(len(parameters) + 1),
            "flag": flag,
            "required": bool(placeholder_name),
            "type": "string",
            "multiple": False,
        }
        if placeholder_name:
            parameter["description"] = placeholder_name
        parameters.append(parameter)
        index += 2 if placeholder and not placeholder.startswith("--") else 1
    return parameters


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


def _remove_legacy_artifacts(root: Path) -> list[str]:
    removed: list[str] = []
    candidates = {
        *LEGACY_FILE_NAMES,
        *(path.name for path in root.glob("*-list.md") if path.is_file()),
    }
    for name in sorted(candidates):
        path = root / name
        if path.is_file():
            path.unlink()
            removed.append(name)
    return removed
