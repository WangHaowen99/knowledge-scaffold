"""Helpers for ontology context loading and script catalog resolution."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

from ontology.ontologies import (
    OntologyManifest,
    get_ontology_registry,
    get_runtime_ontology_manifest,
)
from ontology.package_browser import (
    PackageBrowserError,
    list_package_files,
    resolve_package_file,
)

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

_OBJECT_KEY_HEADERS = ("object key", "object_key", "key", "中文名称")
_OBJECT_TITLE_HEADERS = ("标题", "title")
_OBJECT_ALIAS_HEADERS = ("别名", "aliases", "alias")
_OBJECT_ALIAS_SPLIT_RE = re.compile(r"[,，、;/；]")
_MARKDOWN_TABLE_SEPARATOR_CELL_RE = re.compile(r"^:?-+:?$")
_OBJECT_CANONICAL_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class ObjectReferenceResolution:
    requested_key: str
    object_key: str
    matched_by: str
    path: Path


@dataclass(frozen=True)
class ObjectAliasValidationResult:
    errors: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()


class ObjectReferenceStatus(str, Enum):
    RESOLVED = "resolved"
    NOT_FOUND = "not_found"
    MISSING_DIRECTORY = "missing_directory"
    INVALID_REQUEST = "invalid_request"
    AMBIGUOUS = "ambiguous"
    READ_ERROR = "read_error"
    INVALID_PACKAGE = "invalid_package"


@dataclass(frozen=True)
class ObjectReferenceLookupResult:
    status: ObjectReferenceStatus
    resolution: Optional[ObjectReferenceResolution] = None
    error: Optional[str] = None


class ObjectDataMappingStatus(str, Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    FILE_MISSING = "file_missing"
    INVALID = "invalid"


@dataclass(frozen=True)
class ObjectDataMappingLookupResult:
    status: ObjectDataMappingStatus
    mapping: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class _ObjectDataMappingsLoadResult:
    status: ObjectDataMappingStatus
    payload: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class _ObjectCatalogEntry:
    key: str
    title: str
    aliases: Tuple[str, ...]
    raw_aliases: Tuple[str, ...]


@dataclass(frozen=True)
class _ObjectDefinition:
    key: str
    canonical_key: str
    title: str
    aliases: Tuple[str, ...]
    raw_aliases: Tuple[str, ...]
    path: Path


@dataclass(frozen=True)
class _ObjectReferenceIndex:
    definitions: Tuple[_ObjectDefinition, ...]
    matches_by_kind: Dict[str, Dict[str, Tuple[_ObjectDefinition, ...]]]


class _ObjectCatalogReadError(RuntimeError):
    pass


class _ObjectDefinitionLoadError(RuntimeError):
    def __init__(self, message: str, *, invalid_package: bool = False) -> None:
        super().__init__(message)
        self.invalid_package = invalid_package


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


def find_exact_object_data_mapping(
    ontology_dir: Path,
    object_key: str,
) -> Optional[Dict[str, Any]]:
    return find_exact_object_data_mapping_result(
        ontology_dir,
        object_key,
    ).mapping


def find_exact_object_data_mapping_result(
    ontology_dir: Path,
    object_key: str,
) -> ObjectDataMappingLookupResult:
    key = str(object_key or "").strip()
    if not key:
        return ObjectDataMappingLookupResult(
            status=ObjectDataMappingStatus.INVALID,
            error="Error: object key is required for data mapping lookup.",
        )
    document = _load_object_data_mappings(ontology_dir)
    if document.status != ObjectDataMappingStatus.FOUND:
        return ObjectDataMappingLookupResult(
            status=document.status,
            error=document.error,
        )
    payload = document.payload or {}

    objects = payload.get("objects")
    if isinstance(objects, dict):
        mapping = objects.get(key)
        if mapping is None:
            return ObjectDataMappingLookupResult(
                status=ObjectDataMappingStatus.NOT_FOUND,
            )
        return ObjectDataMappingLookupResult(
            status=ObjectDataMappingStatus.FOUND,
            mapping=dict(mapping),
        )
    if isinstance(objects, list):
        for mapping in objects:
            mapping_key = str(
                mapping.get("object_key") or mapping.get("key") or ""
            ).strip()
            if mapping_key == key:
                return ObjectDataMappingLookupResult(
                    status=ObjectDataMappingStatus.FOUND,
                    mapping=dict(mapping),
                )
        return ObjectDataMappingLookupResult(
            status=ObjectDataMappingStatus.NOT_FOUND,
        )
    payload_key = str(payload.get("object_key") or "").strip()
    if payload_key == key:
        return ObjectDataMappingLookupResult(
            status=ObjectDataMappingStatus.FOUND,
            mapping=dict(payload),
        )
    return ObjectDataMappingLookupResult(
        status=ObjectDataMappingStatus.NOT_FOUND,
    )


def _load_object_data_mappings(
    ontology_dir: Path,
) -> _ObjectDataMappingsLoadResult:
    mapping_path = (
        Path(ontology_dir).resolve()
        / "mappings"
        / "object-data-mappings.json"
    )
    if not mapping_path.exists():
        return _ObjectDataMappingsLoadResult(
            status=ObjectDataMappingStatus.FILE_MISSING,
        )
    if not mapping_path.is_file():
        return _ObjectDataMappingsLoadResult(
            status=ObjectDataMappingStatus.INVALID,
            error="Error: mappings/object-data-mappings.json is not a file.",
        )
    try:
        payload = json.loads(mapping_path.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        return _ObjectDataMappingsLoadResult(
            status=ObjectDataMappingStatus.INVALID,
            error=(
                "Error: Failed to decode mappings/object-data-mappings.json "
                "as UTF-8."
            ),
        )
    except OSError:
        return _ObjectDataMappingsLoadResult(
            status=ObjectDataMappingStatus.INVALID,
            error="Error: Failed to read mappings/object-data-mappings.json.",
        )
    except json.JSONDecodeError:
        return _ObjectDataMappingsLoadResult(
            status=ObjectDataMappingStatus.INVALID,
            error=(
                "Error: mappings/object-data-mappings.json contains invalid JSON."
            ),
        )
    if not isinstance(payload, dict):
        return _ObjectDataMappingsLoadResult(
            status=ObjectDataMappingStatus.INVALID,
            error=(
                "Error: mappings/object-data-mappings.json must contain a JSON object."
            ),
        )

    objects = payload.get("objects")
    if isinstance(objects, dict):
        invalid_keys = sorted(
            str(mapping_key)
            for mapping_key, mapping in objects.items()
            if not isinstance(mapping, dict)
        )
        if invalid_keys:
            return _ObjectDataMappingsLoadResult(
                status=ObjectDataMappingStatus.INVALID,
                error=(
                    "Error: Object mappings must contain JSON objects: {}."
                ).format(", ".join(invalid_keys)),
            )
    elif isinstance(objects, list):
        if any(not isinstance(mapping, dict) for mapping in objects):
            return _ObjectDataMappingsLoadResult(
                status=ObjectDataMappingStatus.INVALID,
                error=(
                    "Error: 'objects' entries in mappings/object-data-mappings.json "
                    "must be JSON objects."
                ),
            )
    elif "objects" in payload:
        return _ObjectDataMappingsLoadResult(
            status=ObjectDataMappingStatus.INVALID,
            error=(
                "Error: 'objects' in mappings/object-data-mappings.json "
                "must be a JSON object or array."
            ),
        )
    return _ObjectDataMappingsLoadResult(
        status=ObjectDataMappingStatus.FOUND,
        payload=dict(payload),
    )


def validate_object_data_mappings(ontology_dir: Path) -> Optional[str]:
    result = _load_object_data_mappings(ontology_dir)
    if result.status != ObjectDataMappingStatus.INVALID:
        return None
    return str(result.error or "Error: Invalid object data mapping.").removeprefix(
        "Error: "
    )


def get_ontology_manifest(
    ontology_id: str,
    *,
    version: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> Tuple[Optional[OntologyManifest], Optional[str]]:
    normalized_id = str(ontology_id or "").strip()
    if not normalized_id:
        return None, "Error: `ontology_id` is required."

    registry = (
        get_ontology_registry(workspace_id=workspace_id)
        if str(workspace_id or "").strip()
        else get_ontology_registry()
    )
    ontology = get_runtime_ontology_manifest(
        normalized_id,
        version=version,
        workspace_id=workspace_id,
    )
    if ontology is None:
        available = sorted(registry.list_enabled_ontologies())
        suffix = " Available ontologies: {}.".format(", ".join(available)) if available else ""
        version_text = str(version or "").strip()
        if version_text:
            return None, "Error: Ontology '{}' version '{}' not found.{}".format(
                normalized_id,
                version_text,
                suffix,
            )
        return None, "Error: Ontology '{}' not found.{}".format(normalized_id, suffix)
    if not ontology.enabled:
        available = sorted(registry.list_enabled_ontologies())
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


def build_asset_catalog(
    ontology_dir: Path,
    *,
    trusted_root: Optional[Path] = None,
) -> List[str]:
    assets_dir = find_assets_dir(ontology_dir)
    if assets_dir is None:
        return []

    try:
        files = list_package_files(
            assets_dir,
            recursive=True,
            include_hidden_files=True,
            trusted_root=trusted_root or ontology_dir,
        )
    except PackageBrowserError:
        return []
    return [str(item["name"]) for item in files]


def find_asset_file(
    ontology_dir: Path,
    key: str,
    *,
    trusted_root: Optional[Path] = None,
) -> Tuple[Optional[Path], Optional[str]]:
    normalized_key = str(key or "").strip().replace("\\", "/").strip("/")
    if not normalized_key:
        return None, "Error: `key` is required when resource is `asset`."

    assets_dir = find_assets_dir(ontology_dir)
    if assets_dir is None:
        return None, "Error: asset directory not found."

    try:
        candidate = resolve_package_file(
            assets_dir,
            normalized_key,
            allow_hidden_file=True,
            trusted_root=trusted_root or ontology_dir,
        )
    except PackageBrowserError as exc:
        if exc.code == "not_found":
            available = build_asset_catalog(ontology_dir, trusted_root=trusted_root)
            suffix = " Available keys: {}.".format(", ".join(available[:30])) if available else ""
            return None, "Error: asset '{}' not found.{}".format(normalized_key, suffix)
        return None, "Error: Invalid asset key."

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
        if cells and all(
            _MARKDOWN_TABLE_SEPARATOR_CELL_RE.fullmatch(cell.replace(" ", ""))
            for cell in cells
        ):
            continue
        if not headers:
            headers = cells
            continue
        if len(cells) != len(headers):
            continue
        rows.append({headers[index]: cells[index] for index in range(len(headers))})
    return rows


def normalize_object_identifier(value: Any) -> str:
    normalized = unicodedata.normalize("NFC", str(value or "").strip())
    return normalized.casefold() if normalized.isascii() else normalized


def split_object_aliases(value: Any) -> List[str]:
    text = str(value or "").strip()
    if not text or text == "-":
        return []
    return [item.strip() for item in _OBJECT_ALIAS_SPLIT_RE.split(text) if item.strip()]


def _split_object_alias_tokens(value: Any) -> Tuple[str, ...]:
    text = str(value or "").strip()
    if text == "-":
        return ()
    if not text:
        return ("",)
    return tuple(item.strip() for item in _OBJECT_ALIAS_SPLIT_RE.split(text))


def resolve_object_reference(
    ontology_dir: Path,
    requested_key: str,
) -> Tuple[Optional[ObjectReferenceResolution], Optional[str]]:
    result = resolve_object_reference_result(ontology_dir, requested_key)
    return result.resolution, result.error


def resolve_object_reference_result(
    ontology_dir: Path,
    requested_key: str,
) -> ObjectReferenceLookupResult:
    requested = str(requested_key or "").strip()
    if not requested:
        return ObjectReferenceLookupResult(
            status=ObjectReferenceStatus.INVALID_REQUEST,
            error="Error: object reference is required.",
        )

    root = Path(ontology_dir).resolve()
    objects_dir = root / "objects"
    if not objects_dir.is_dir():
        return ObjectReferenceLookupResult(
            status=ObjectReferenceStatus.MISSING_DIRECTORY,
            error="Error: object directory not found.",
        )

    try:
        reference_index = _load_object_reference_index(root)
    except _ObjectCatalogReadError as exc:
        return ObjectReferenceLookupResult(
            status=ObjectReferenceStatus.READ_ERROR,
            error="Error: {}".format(exc),
        )
    except _ObjectDefinitionLoadError as exc:
        return ObjectReferenceLookupResult(
            status=(
                ObjectReferenceStatus.INVALID_PACKAGE
                if exc.invalid_package
                else ObjectReferenceStatus.READ_ERROR
            ),
            error="Error: {}".format(exc),
        )
    definitions = reference_index.definitions
    requested_compare = normalize_object_identifier(requested)

    for matched_by in ("key", "canonical_key", "title", "alias", "filename"):
        matches = reference_index.matches_by_kind[matched_by].get(
            requested_compare,
            (),
        )
        if not matches:
            continue
        if len(matches) > 1:
            candidates = sorted({item.key for item in matches}, key=normalize_object_identifier)
            return ObjectReferenceLookupResult(
                status=ObjectReferenceStatus.AMBIGUOUS,
                error=(
                    "Error: object reference '{}' is ambiguous. "
                    "Candidate object keys: {}."
                ).format(requested, ", ".join(candidates)),
            )
        match = matches[0]
        return ObjectReferenceLookupResult(
            status=ObjectReferenceStatus.RESOLVED,
            resolution=ObjectReferenceResolution(
                requested_key=requested,
                object_key=match.key,
                matched_by=matched_by,
                path=match.path,
            ),
        )

    available = sorted({item.key for item in definitions}, key=normalize_object_identifier)
    suffix = (
        " Available object keys: {}.".format(", ".join(available[:30]))
        if available
        else ""
    )
    return ObjectReferenceLookupResult(
        status=ObjectReferenceStatus.NOT_FOUND,
        error="Object reference '{}' was not found.{}".format(requested, suffix),
    )


def _load_object_reference_index(ontology_dir: Path) -> _ObjectReferenceIndex:
    root = Path(ontology_dir).resolve()
    signature = _object_reference_signature(root / "objects")
    return _load_object_reference_index_cached(str(root), signature)


def _object_reference_signature(objects_dir: Path) -> Tuple[Tuple[str, int, int], ...]:
    if not objects_dir.is_dir():
        return ()
    signature: List[Tuple[str, int, int]] = []
    for path in sorted(objects_dir.glob("*.md"), key=lambda item: item.name.lower()):
        try:
            stat_result = path.stat()
        except OSError:
            continue
        signature.append((path.name, stat_result.st_mtime_ns, stat_result.st_size))
    return tuple(signature)


@lru_cache(maxsize=64)
def _load_object_reference_index_cached(
    ontology_dir: str,
    signature: Tuple[Tuple[str, int, int], ...],
) -> _ObjectReferenceIndex:
    del signature
    root = Path(ontology_dir)
    catalog, _has_alias_column, _catalog_warnings = _load_object_catalog(root)
    definitions = tuple(_load_object_definitions(root / "objects"))
    return _ObjectReferenceIndex(
        definitions=definitions,
        matches_by_kind=_build_object_reference_matches(definitions, catalog),
    )


def _build_object_reference_matches(
    definitions: Tuple[_ObjectDefinition, ...],
    catalog: List[_ObjectCatalogEntry],
) -> Dict[str, Dict[str, Tuple[_ObjectDefinition, ...]]]:
    mutable: Dict[str, Dict[str, List[_ObjectDefinition]]] = {
        kind: {}
        for kind in ("key", "canonical_key", "title", "alias", "filename")
    }

    def add(kind: str, value: str, definition: _ObjectDefinition) -> None:
        compare = normalize_object_identifier(value)
        if compare:
            mutable[kind].setdefault(compare, []).append(definition)

    definitions_by_key: Dict[str, List[_ObjectDefinition]] = {}
    for definition in definitions:
        definitions_by_key.setdefault(
            normalize_object_identifier(definition.key), []
        ).append(definition)
        add("key", definition.key, definition)
        add("canonical_key", definition.canonical_key, definition)
        add("title", definition.title, definition)
        for alias in definition.aliases:
            add("alias", alias, definition)
        add("filename", definition.path.stem, definition)
        add("filename", definition.path.name, definition)

    for entry in catalog:
        for definition in definitions_by_key.get(
            normalize_object_identifier(entry.key), []
        ):
            add("key", entry.key, definition)
            add("title", entry.title, definition)
            for alias in entry.aliases:
                add("alias", alias, definition)

    return {
        kind: {
            compare: tuple(_dedupe_object_definitions(matches))
            for compare, matches in by_identifier.items()
        }
        for kind, by_identifier in mutable.items()
    }


def validate_object_aliases(ontology_dir: Path) -> ObjectAliasValidationResult:
    root = Path(ontology_dir).resolve()
    objects_dir = root / "objects"
    try:
        definitions = _load_object_definitions(objects_dir)
    except _ObjectDefinitionLoadError as exc:
        return ObjectAliasValidationResult(errors=(str(exc),))
    try:
        catalog, has_alias_column, catalog_warnings = _load_object_catalog(root)
    except _ObjectCatalogReadError as exc:
        return ObjectAliasValidationResult(errors=(str(exc),))
    errors: List[str] = []
    warnings: List[str] = list(catalog_warnings)

    definitions_by_exact_key: Dict[str, List[_ObjectDefinition]] = {}
    definitions_by_compare_key: Dict[str, List[_ObjectDefinition]] = {}
    definitions_by_filename: Dict[str, _ObjectDefinition] = {}
    for definition in definitions:
        definitions_by_exact_key.setdefault(definition.key, []).append(definition)
        definitions_by_compare_key.setdefault(
            normalize_object_identifier(definition.key), []
        ).append(definition)
        definitions_by_filename[normalize_object_identifier(definition.path.name)] = definition

    for object_key, matches in definitions_by_compare_key.items():
        if len(matches) > 1:
            errors.append(
                "Duplicate object key '{}': {}.".format(
                    object_key,
                    ", ".join(sorted(str(item.path.name) for item in matches)),
                )
            )

    for entry in catalog:
        exact_matches = definitions_by_exact_key.get(entry.key, [])
        normalized_matches = definitions_by_compare_key.get(
            normalize_object_identifier(entry.key), []
        )
        filename_match = definitions_by_filename.get(
            normalize_object_identifier("{}.md".format(entry.key))
        )
        if not exact_matches:
            if normalized_matches:
                errors.append(
                    "Object catalog key '{}' does not exactly match detail key '{}'.".format(
                        entry.key,
                        normalized_matches[0].key,
                    )
                )
            elif filename_match is not None:
                errors.append(
                    "Object catalog key '{}' does not exactly match detail key '{}'.".format(
                        entry.key,
                        filename_match.key,
                    )
                )
            else:
                errors.append(
                    "Object catalog key '{}' has no matching detail.".format(entry.key)
                )
            continue
        if len(exact_matches) > 1:
            errors.append(
                "Object catalog key '{}' matches multiple details.".format(entry.key)
            )
            continue
        definition = exact_matches[0]
        if has_alias_column:
            catalog_aliases = {
                normalize_object_identifier(alias) for alias in entry.aliases if alias
            }
            detail_aliases = {
                normalize_object_identifier(alias) for alias in definition.aliases if alias
            }
            if catalog_aliases != detail_aliases:
                catalog_alias_display = ", ".join(
                    sorted(entry.aliases, key=normalize_object_identifier)
                ) or "（无）"
                detail_alias_display = ", ".join(
                    sorted(definition.aliases, key=normalize_object_identifier)
                ) or "（无）"
                errors.append(
                    (
                        "对象 key='{}'、title='{}' 别名不一致："
                        "objects/index.md = [{}]；{} = [{}]"
                    ).format(
                        definition.key,
                        definition.title,
                        catalog_alias_display,
                        definition.path.name,
                        detail_alias_display,
                    )
                )
        warnings.extend(_duplicate_alias_warnings(entry.key, entry.aliases, source="catalog"))
        if any(not alias for alias in entry.raw_aliases):
            warnings.append(
                "Object '{}' catalog aliases contain an empty value.".format(entry.key)
            )

    for definition in definitions:
        warnings.extend(
            _duplicate_alias_warnings(definition.key, definition.raw_aliases, source="detail")
        )
        if any(not str(alias or "").strip() for alias in definition.raw_aliases):
            warnings.append(
                "Object '{}' detail aliases contain an empty value.".format(definition.key)
            )
        if definition.canonical_key and not _OBJECT_CANONICAL_KEY_RE.fullmatch(
            definition.canonical_key
        ):
            errors.append(
                (
                    "Object '{}' canonical_key '{}' must contain only ASCII letters, "
                    "digits, underscores, or hyphens."
                ).format(definition.key, definition.canonical_key)
            )

    _validate_object_identifier_conflicts(definitions, catalog, errors)
    return ObjectAliasValidationResult(
        errors=tuple(sorted(set(errors))),
        warnings=tuple(sorted(set(warnings))),
    )


def _load_object_catalog(
    ontology_dir: Path,
) -> Tuple[List[_ObjectCatalogEntry], bool, List[str]]:
    index_path = Path(ontology_dir) / "objects" / "index.md"
    try:
        markdown = read_optional_text(index_path)
    except UnicodeDecodeError as exc:
        raise _ObjectCatalogReadError(
            "Failed to decode objects/index.md as UTF-8."
        ) from exc
    except OSError as exc:
        raise _ObjectCatalogReadError(
            "Failed to read objects/index.md."
        ) from exc
    rows = parse_markdown_table(markdown)
    headers = _first_markdown_table_headers(markdown)
    has_key_column = _has_table_header(headers, _OBJECT_KEY_HEADERS)
    has_alias_column = _has_table_header(headers, _OBJECT_ALIAS_HEADERS)
    warnings: List[str] = []
    if headers and not has_key_column:
        warnings.append(
            "Object catalog table has no supported key column; alias routing will use detail fallback."
        )

    entries: List[_ObjectCatalogEntry] = []
    if not has_key_column:
        return entries, has_alias_column, warnings
    for row in rows:
        key = _object_row_value(row, _OBJECT_KEY_HEADERS)
        if not key:
            continue
        raw_aliases = (
            _split_object_alias_tokens(_object_row_value(row, _OBJECT_ALIAS_HEADERS))
            if has_alias_column
            else ()
        )
        entries.append(
            _ObjectCatalogEntry(
                key=key,
                title=_object_row_value(row, _OBJECT_TITLE_HEADERS),
                aliases=tuple(alias for alias in raw_aliases if alias),
                raw_aliases=raw_aliases,
            )
        )
    return entries, has_alias_column, warnings


def _load_object_definitions(objects_dir: Path) -> List[_ObjectDefinition]:
    definitions: List[_ObjectDefinition] = []
    if not objects_dir.is_dir():
        return definitions
    for path in sorted(objects_dir.glob("*.md"), key=lambda item: item.name.lower()):
        if path.name.lower() == "index.md":
            continue
        frontmatter = _read_object_definition_frontmatter(path)
        raw_alias_value = frontmatter.get("aliases")
        if isinstance(raw_alias_value, list):
            raw_aliases = tuple(
                token
                for alias in raw_alias_value
                for token in _split_object_alias_tokens(alias)
            )
            aliases = tuple(alias for alias in raw_aliases if alias)
        elif raw_alias_value is None:
            raw_aliases = ()
            aliases = ()
        else:
            raw_aliases = _split_object_alias_tokens(raw_alias_value)
            aliases = tuple(alias for alias in raw_aliases if alias)
        key = str(frontmatter.get("key") or path.stem).strip() or path.stem
        definitions.append(
            _ObjectDefinition(
                key=key,
                canonical_key=str(frontmatter.get("canonical_key") or "").strip(),
                title=str(frontmatter.get("title") or "").strip(),
                aliases=aliases,
                raw_aliases=raw_aliases,
                path=path.resolve(),
            )
        )
    return definitions


def _read_object_definition_frontmatter(path: Path) -> Dict[str, Any]:
    try:
        markdown = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise _ObjectDefinitionLoadError(
            "Failed to decode object detail '{}' as UTF-8.".format(path.name)
        ) from exc
    except OSError as exc:
        raise _ObjectDefinitionLoadError(
            "Failed to read object detail '{}'.".format(path.name)
        ) from exc

    if not markdown.startswith("---"):
        return {}
    end = markdown.find("\n---", 3)
    if end < 0:
        raise _ObjectDefinitionLoadError(
            "Object detail '{}' has unclosed YAML frontmatter.".format(path.name),
            invalid_package=True,
        )
    raw_frontmatter = markdown[3:end].strip()
    try:
        parsed = yaml.safe_load(raw_frontmatter) or {}
    except yaml.YAMLError as exc:
        raise _ObjectDefinitionLoadError(
            "Object detail '{}' has invalid YAML frontmatter.".format(path.name),
            invalid_package=True,
        ) from exc
    if not isinstance(parsed, dict):
        raise _ObjectDefinitionLoadError(
            "Object detail '{}' YAML frontmatter must be a mapping.".format(
                path.name
            ),
            invalid_package=True,
        )
    return parsed


def _validate_object_identifier_conflicts(
    definitions: List[_ObjectDefinition],
    catalog: List[_ObjectCatalogEntry],
    errors: List[str],
) -> None:
    identity_claims: Dict[str, List[Tuple[str, str]]] = {}
    alias_claims: Dict[str, List[str]] = {}
    for definition in definitions:
        for kind, value in (
            ("key", definition.key),
            ("canonical_key", definition.canonical_key),
            ("title", definition.title),
        ):
            compare = normalize_object_identifier(value)
            if compare:
                identity_claims.setdefault(compare, []).append((definition.key, kind))
        for alias in definition.aliases:
            compare = normalize_object_identifier(alias)
            if compare:
                alias_claims.setdefault(compare, []).append(definition.key)

    known_keys = {definition.key for definition in definitions}
    for entry in catalog:
        if entry.key not in known_keys:
            continue
        title_compare = normalize_object_identifier(entry.title)
        if title_compare:
            identity_claims.setdefault(title_compare, []).append((entry.key, "title"))
        for alias in entry.aliases:
            compare = normalize_object_identifier(alias)
            if compare:
                alias_claims.setdefault(compare, []).append(entry.key)

    for compare, object_keys in alias_claims.items():
        distinct_alias_objects = sorted(set(object_keys), key=normalize_object_identifier)
        if len(distinct_alias_objects) > 1:
            errors.append(
                "Object alias '{}' points to multiple objects: {}.".format(
                    compare,
                    ", ".join(distinct_alias_objects),
                )
            )
        for identity_object, identity_kind in _highest_priority_identity_claims(
            identity_claims.get(compare, [])
        ):
            if identity_object in distinct_alias_objects:
                continue
            errors.append(
                "Object alias '{}' for '{}' conflicts with {} of object '{}'.".format(
                    compare,
                    distinct_alias_objects[0] if distinct_alias_objects else "",
                    identity_kind,
                    identity_object,
                )
            )

    canonical_claims: Dict[str, List[str]] = {}
    for definition in definitions:
        compare = normalize_object_identifier(definition.canonical_key)
        if compare:
            canonical_claims.setdefault(compare, []).append(definition.key)
    for compare, object_keys in canonical_claims.items():
        distinct = sorted(set(object_keys), key=normalize_object_identifier)
        if len(distinct) > 1:
            errors.append(
                "Canonical object key '{}' is shared by: {}.".format(
                    compare,
                    ", ".join(distinct),
                )
            )
        identity_conflicts = [
            (identity_object, identity_kind)
            for identity_object, identity_kind in _highest_priority_identity_claims(
                identity_claims.get(compare, [])
            )
            if identity_object not in distinct and identity_kind != "canonical_key"
        ]
        if len(distinct) > 1:
            for identity_object, identity_kind in identity_conflicts:
                errors.append(
                    "Canonical object key '{}' shared by {} conflicts with {} of object '{}'.".format(
                        compare,
                        ", ".join(distinct),
                        identity_kind,
                        identity_object,
                    )
                )
            continue
        canonical_object = distinct[0]
        for identity_object, identity_kind in identity_conflicts:
            errors.append(
                "Canonical object key '{}' for '{}' conflicts with {} of object '{}'.".format(
                    compare,
                    canonical_object,
                    identity_kind,
                    identity_object,
                )
            )


def _highest_priority_identity_claims(
    claims: List[Tuple[str, str]],
) -> List[Tuple[str, str]]:
    priority = {"key": 0, "canonical_key": 1, "title": 2}
    by_object: Dict[str, str] = {}
    for object_key, kind in claims:
        current = by_object.get(object_key)
        if current is None or priority[kind] < priority[current]:
            by_object[object_key] = kind
    return sorted(
        by_object.items(),
        key=lambda item: (normalize_object_identifier(item[0]), priority[item[1]]),
    )


def _duplicate_alias_warnings(
    object_key: str,
    aliases: Tuple[str, ...],
    *,
    source: str,
) -> List[str]:
    seen: Set[str] = set()
    duplicates: Set[str] = set()
    for alias in aliases:
        normalized = normalize_object_identifier(alias)
        if not normalized:
            continue
        if normalized in seen:
            duplicates.add(normalized)
        else:
            seen.add(normalized)
    return [
        "Object '{}' {} aliases contain duplicate '{}'.".format(object_key, source, alias)
        for alias in sorted(duplicates)
    ]


def _dedupe_object_definitions(
    definitions: List[_ObjectDefinition],
) -> List[_ObjectDefinition]:
    result: List[_ObjectDefinition] = []
    seen: Set[str] = set()
    for definition in sorted(definitions, key=lambda item: item.path.name.lower()):
        path_key = str(definition.path)
        if path_key in seen:
            continue
        seen.add(path_key)
        result.append(definition)
    return result


def _normalize_table_header(value: Any) -> str:
    return "".join(str(value or "").strip().casefold().split())


def _has_table_header(headers: List[str], candidates: Tuple[str, ...]) -> bool:
    normalized = {_normalize_table_header(header) for header in headers}
    return any(_normalize_table_header(candidate) in normalized for candidate in candidates)


def _object_row_value(row: Dict[str, str], candidates: Tuple[str, ...]) -> str:
    normalized = {_normalize_table_header(key): value for key, value in row.items()}
    for candidate in candidates:
        value = normalized.get(_normalize_table_header(candidate))
        if value is not None:
            return str(value or "").strip()
    return ""


def _first_markdown_table_headers(markdown: str) -> List[str]:
    for raw_line in str(markdown or "").splitlines():
        line = raw_line.strip()
        if line.startswith("|") and line.endswith("|"):
            return [cell.strip() for cell in line.strip("|").split("|")]
    return []


def load_script_catalog(ontology_dir: Path) -> List[Dict[str, Any]]:
    """Load executable script metadata from v0.1 ``scripts/*.md`` descriptors."""
    catalog: List[Dict[str, Any]] = []
    seen: Set[str] = set()

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


def _normalize_script_entrypoint(value: Any) -> Optional[str]:
    """Return a portable scripts-relative path, or None for an unsafe path."""
    raw_path = str(value or "").strip().replace("\\", "/")
    if not raw_path or raw_path.startswith("/") or re.match(r"^[A-Za-z]:", raw_path):
        return None
    parts = [part for part in raw_path.split("/") if part not in {"", "."}]
    if not parts or ".." in parts:
        return None
    if parts[0] != "scripts":
        parts.insert(0, "scripts")
    return "/".join(parts)


def resolve_ontology_script_path(
    ontology_dir: Path,
    entrypoint: Any,
) -> Optional[Path]:
    """Resolve an entrypoint while containing scripts and symlinks to one ontology."""
    normalized = _normalize_script_entrypoint(entrypoint)
    if normalized is None:
        return None
    ontology_root = Path(ontology_dir).resolve()
    scripts_root = (ontology_root / "scripts").resolve()
    candidate = (ontology_root / normalized).resolve()
    try:
        scripts_root.relative_to(ontology_root)
        candidate.relative_to(scripts_root)
    except ValueError:
        return None
    return candidate


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

    raw_path = _normalize_script_entrypoint(entrypoint)
    if raw_path is None:
        return None
    resolved_script_path = resolve_ontology_script_path(ontology_dir, raw_path)
    if resolved_script_path is None:
        return None

    parameters = _parse_script_parameters_from_markdown(body)
    entry: Dict[str, Any] = {
        "operation_key": operation_key or script_key,
        "script_key": script_key,
        "title": str(frontmatter.get("title") or script_key).strip(),
        "arguments": [],
        "parameters": parameters,
        "_script_path": raw_path,
        "_script_doc_path": str(path.resolve()),
        "_resolved_script_path": str(resolved_script_path),
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
        action_text = _first_row_value(row, "Action", "action")
        runtime_text = _first_row_value(row, "runtime", "Runtime")
        source_text = _first_row_value(row, "source", "Source")
        placeholder_text = _first_row_value(row, "placeholder", "Placeholder")
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
        action = _normalize_boolean_action(action_text)
        if action:
            parameter["action"] = action
        if runtime_text:
            parameter["runtime"] = _as_bool(runtime_text, default=False)
        if source_text:
            parameter["source"] = source_text
        if placeholder_text:
            parameter["placeholder"] = placeholder_text
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
    return [
        item.strip()
        for item in re.split(r"[,，、;/；]+", str(value))
        if item.strip()
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
        action = _normalize_boolean_action(item.get("action"))
        if action:
            parameter["action"] = action
        if "runtime" in item:
            parameter["runtime"] = _as_bool(item.get("runtime"), default=False)
        source = str(item.get("source") or "").strip()
        if source:
            parameter["source"] = source
        placeholder = str(item.get("placeholder") or "").strip()
        if placeholder:
            parameter["placeholder"] = placeholder
        parameters.append(parameter)
    return parameters


def _public_script_parameters(parameters: Any) -> List[Dict[str, Any]]:
    public: List[Dict[str, Any]] = []
    if not isinstance(parameters, list):
        return public
    # Import lazily: ``agent.tools`` imports business-data helpers which refer
    # back to this module during application startup.
    from agent.tools.script_parameters import is_runtime_parameter

    for item in parameters:
        if not isinstance(item, dict):
            continue
        if is_runtime_parameter(item):
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
        action = _normalize_boolean_action(item.get("action"))
        if action:
            parameter["action"] = action
        public.append(parameter)
    return public


def _normalize_boolean_action(value: Any) -> str:
    action = str(value or "").strip().lower()
    return action if action in {"store_true", "store_false"} else ""


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


def find_script_entry(
    catalog: List[Dict[str, Any]],
    operation_key: str,
    *,
    bound_only: bool = False,
) -> Optional[Dict[str, Any]]:
    """Find a script entry, optionally requiring a public bound operation.

    The default preserves catalog lookup compatibility for internal callers.
    Execution-facing callers should set ``bound_only`` so helper scripts that
    are not attached to a business operation cannot be invoked by key.
    """
    normalized_key = str(operation_key or "").strip()
    for entry in catalog:
        if bound_only and not bool(entry.get("_is_bound_operation")):
            continue
        if str(entry.get("operation_key") or "").strip() == normalized_key:
            return dict(entry)
        if str(entry.get("script_key") or "").strip() == normalized_key:
            return dict(entry)
    return None


def build_root_payload(*, ontology: OntologyManifest, ontology_dir: Path) -> Dict[str, Any]:
    root_index_text = read_optional_text(ontology_dir / "index.md")
    operations_text = read_optional_text(ontology_dir / "operations" / "index.md")
    objects_text = read_optional_text(ontology_dir / "objects" / "index.md")
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
    requested_key: str = "",
    matched_by: str = "",
) -> str:
    lines = ["{}: {}".format(key_field, _compact_inline_text(key))]
    normalized_requested = _compact_inline_text(requested_key)
    normalized_matched_by = _compact_inline_text(matched_by)
    if normalized_requested and normalized_requested != _compact_inline_text(key):
        lines.append("requested_key: {}".format(normalized_requested))
    if normalized_matched_by and normalized_matched_by != "key":
        lines.append("matched_by: {}".format(normalized_matched_by))
    lines.extend(["", "content:", str(content or "").strip()])
    return "\n".join(lines).strip()


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
    if candidate.name.lower() != "index.md" and candidate.exists() and candidate.is_file():
        return candidate, None

    if directory.is_dir():
        for path in sorted(directory.glob("*.md"), key=lambda item: item.name.lower()):
            if path.name.lower() == "index.md":
                continue
            frontmatter = read_markdown_frontmatter(path)
            candidates = {
                str(frontmatter.get("key") or "").strip(),
                str(frontmatter.get("canonical_key") or "").strip(),
                str(frontmatter.get("title") or "").strip(),
                path.stem,
                path.name,
            }
            aliases = frontmatter.get("aliases")
            if isinstance(aliases, list):
                candidates.update(str(alias or "").strip() for alias in aliases)
            if normalized_key in {candidate for candidate in candidates if candidate}:
                return path, None

    available = sorted(
        _markdown_available_key(path)
        for path in directory.glob("*.md")
        if path.is_file() and path.name.lower() != "index.md"
    )
    suffix = " Available keys: {}.".format(", ".join(available[:30])) if available else ""
    return None, "Error: {} '{}' not found.{}".format(resource_name, normalized_key, suffix)


def _markdown_available_key(path: Path) -> str:
    frontmatter = read_markdown_frontmatter(path)
    return str(frontmatter.get("key") or path.stem).strip() or path.stem
