"""Ontology package business-context loading for GraphQueryAgent."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping

from ontology.ontologies import get_ontology_version_dir
from .models import GraphEntityMapping, GraphSearchRuntimeContext

_MAX_CATALOG_CHARS = 4000
_MAX_ITEM_CHARS = 6000
_MAX_ITEMS_PER_KIND = 5
_SUPPORTED_TEXT_SUFFIXES = {".md", ".txt", ".yaml", ".yml", ".json", ".py"}


def load_graph_business_context(
    *,
    context: GraphSearchRuntimeContext,
    mapping: GraphEntityMapping,
) -> dict[str, Any]:
    """Load bounded operation/rule/script context from the selected ontology package."""

    version_dir = _resolve_ontology_version_dir(
        context.workspace_root,
        context.ontology_id,
        context.ontology_version,
    )
    if version_dir is None:
        return {"operations": [], "rules": [], "scripts": [], "catalogs": []}

    keywords = _keywords(context=context, mapping=mapping)
    return {
        "catalogs": _load_catalogs(version_dir),
        "operations": _load_relevant_items(
            version_dir / "operations",
            keywords=keywords,
            max_items=_MAX_ITEMS_PER_KIND,
            kind="operation",
        ),
        "rules": _load_relevant_items(
            version_dir / "rules",
            keywords=keywords,
            max_items=_MAX_ITEMS_PER_KIND,
            kind="rule",
        ),
        "scripts": _load_relevant_items(
            version_dir / "scripts",
            keywords=keywords,
            max_items=_MAX_ITEMS_PER_KIND,
            kind="script",
        ),
    }


def _resolve_ontology_version_dir(
    workspace_root: str,
    ontology_id: str,
    ontology_version: str = "",
) -> Path | None:
    root = str(workspace_root or "").strip()
    ontology = str(ontology_id or "").strip()
    if not root or not ontology:
        return None
    ontology_root = (Path(root) / ontology).resolve()
    if not ontology_root.is_dir():
        return None
    try:
        return get_ontology_version_dir(
            ontology_root,
            str(ontology_version or "").strip() or None,
            create=False,
        ) or ontology_root
    except Exception:
        return ontology_root


def _keywords(
    *,
    context: GraphSearchRuntimeContext,
    mapping: GraphEntityMapping,
) -> list[str]:
    values: list[Any] = [
        context.entity,
        context.query,
        mapping.entity,
        mapping.title,
        mapping.label,
        mapping.edge_type,
        *list(mapping.property_map.keys()),
        *list(mapping.property_map.values()),
        *list(mapping.return_properties or []),
    ]
    if mapping.path_pattern:
        values.extend(_path_pattern_values(mapping.path_pattern))

    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        for token in _split_keyword_value(value):
            if token in seen:
                continue
            seen.add(token)
            result.append(token)
    return result


def _path_pattern_values(path_pattern: Iterable[Mapping[str, Any]]) -> list[str]:
    values: list[str] = []
    for segment in path_pattern or []:
        if not isinstance(segment, Mapping):
            continue
        for key in ("label", "type", "relation_type", "relation_type_id", "relation_name", "entity_name"):
            value = str(segment.get(key) or "").strip()
            if value:
                values.append(value)
        filters = segment.get("filters_from_query")
        if isinstance(filters, Mapping):
            values.extend(str(item).strip() for item in filters if str(item).strip())
            values.extend(str(item).strip() for item in filters.values() if str(item).strip())
    return values


def _split_keyword_value(value: Any) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    tokens = [text]
    for delimiter in (" ", "\t", "\n", ",", "，", ";", "；", ":", "：", "|", "/", "\\"):
        next_tokens: list[str] = []
        for token in tokens:
            next_tokens.extend(part for part in token.split(delimiter) if part)
        tokens = next_tokens
    return [
        token.strip()
        for token in tokens
        if len(token.strip()) >= 2 and token.strip() not in {"query", "entity", "NODE", "EDGE"}
    ]


def _load_catalogs(version_dir: Path) -> list[dict[str, str]]:
    catalogs: list[dict[str, str]] = []
    for name in ("operations/index.md", "rules/index.md", "scripts/index.md"):
        path = version_dir / Path(name)
        if not path.is_file():
            continue
        text = _read_limited(path, _MAX_CATALOG_CHARS)
        if text:
            catalogs.append({"path": name, "content": text})
    return catalogs


def _load_relevant_items(
    directory: Path,
    *,
    keywords: list[str],
    max_items: int,
    kind: str,
) -> list[dict[str, Any]]:
    if not directory.is_dir():
        return []
    candidates: list[tuple[int, str, Path, str]] = []
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        if path.suffix.lower() not in _SUPPORTED_TEXT_SUFFIXES:
            continue
        text = _read_limited(path, _MAX_ITEM_CHARS)
        if not text:
            continue
        relative = path.relative_to(directory.parent).as_posix()
        score = _relevance_score(relative=relative, text=text, keywords=keywords)
        if score <= 0 and kind != "script":
            continue
        if score <= 0 and kind == "script":
            score = 1 if path.name in text or "arg" in text.lower() else 0
        if score <= 0:
            continue
        candidates.append((score, relative, path, text))

    candidates.sort(key=lambda item: (-item[0], item[1]))
    return [
        {
            "path": relative,
            "score": score,
            "content": text,
        }
        for score, relative, _path, text in candidates[:max_items]
    ]


def _relevance_score(*, relative: str, text: str, keywords: list[str]) -> int:
    score = 0
    for keyword in keywords:
        if not keyword:
            continue
        if keyword in relative:
            score += 5
        if keyword in text:
            score += 2
    return score


def _read_limited(path: Path, limit: int) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    text = text.strip()
    if len(text) <= limit:
        return text
    return "{}\n...[truncated {} chars]".format(text[:limit], len(text) - limit)
