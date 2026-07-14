"""Skill tool for loading reusable skill instructions by semantic keys."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from agent.tools.base import ToolCaller, ToolContext, ToolOutcome
from utils.logging import get_logger
from utils.read_docs import build_read_document_record
from utils.skills import get_skill_registry

logger = get_logger("tools.skill")

_MAX_RESOURCE_BYTES = 100 * 1024
_RESOURCE_KINDS = {"reference", "script", "agent", "file"}


SKILL_TOOL_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "skill",
        "description": (
            "Load reusable skill instructions and supporting files. "
            "Use this when a listed skill materially helps the task. "
            "This tool reads skill guidance only; it does not execute scripts."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skill_name": {
                    "type": "string",
                    "description": "The exact skill name from the Available Skills list.",
                },
                "resource": {
                    "type": "string",
                    "enum": ["root", "reference", "script", "agent", "file"],
                    "description": "The skill resource to load. Use root first.",
                },
                "key": {
                    "type": "string",
                    "description": "A key from the catalog returned by resource='root'. Leave empty for root.",
                },
            },
            "required": ["skill_name", "resource"],
        },
    },
}


def handle_skill(
    *,
    skill_name: str,
    resource: str,
    key: str = "",
) -> str:
    skill, error = _get_skill(skill_name)
    if error:
        return error
    assert skill is not None

    normalized_resource = str(resource or "").strip()
    skill_dir = Path(skill.file_path).resolve().parent
    catalog = _build_supporting_file_catalog(skill_dir)

    if normalized_resource == "root":
        payload = {
            "skill": {
                "name": skill.name,
                "description": skill.description,
                "allowed_tools": skill.allowed_tools or "",
            },
            "content": skill.content,
            "reference_catalog": _public_catalog(catalog, "reference"),
            "script_catalog": _public_catalog(catalog, "script"),
            "agent_catalog": _public_catalog(catalog, "agent"),
            "file_catalog": _public_catalog(catalog, "file"),
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    if normalized_resource not in _RESOURCE_KINDS:
        return "Error: Unsupported skill resource '{}'. Use root, reference, script, agent, or file.".format(
            normalized_resource
        )

    entry = _find_catalog_entry(catalog, normalized_resource, key)
    if entry is None:
        available = [item["key"] for item in _public_catalog(catalog, normalized_resource)]
        suffix = " Available keys: {}.".format(", ".join(available[:30])) if available else ""
        return "Error: {} '{}' not found in skill '{}'.{}".format(
            normalized_resource,
            str(key or "").strip(),
            skill.name,
            suffix,
        )

    content, read_error = _read_supporting_file(Path(entry["path"]))
    if read_error:
        return read_error

    payload = {
        "skill_name": skill.name,
        "resource": normalized_resource,
        "key": entry["key"],
        "filename": entry["filename"],
        "content": content,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


@dataclass(frozen=True)
class SkillTool:
    name: str = "skill"
    schema: Dict[str, Any] = None  # type: ignore[assignment]
    allowed_callers: Set[ToolCaller] = None  # type: ignore[assignment]
    concurrency_safe: bool = True
    max_parallelism: Optional[int] = None
    category: str = "regular"

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema", SKILL_TOOL_SCHEMA)
        object.__setattr__(self, "allowed_callers", {"main", "subagent", "acp"})

    def execute(
        self,
        *,
        args: Dict[str, Any],
        context: ToolContext,
        tool_call_id: Optional[str] = None,
        event_emitter: Optional[Callable[..., Any]] = None,
    ) -> ToolOutcome:
        del tool_call_id, event_emitter
        content = handle_skill(**args)
        state_updates: Dict[str, Any] = {}
        record = _build_read_record(args=args, context=context, content=content)
        if record:
            state_updates["read_document_records"] = [record]
        return ToolOutcome(content=content, state_updates=state_updates)


def _get_skill(skill_name: str):
    normalized_name = str(skill_name or "").strip()
    if not normalized_name:
        return None, "Error: `skill_name` is required."
    registry = get_skill_registry()
    skill = registry.get_skill(normalized_name)
    if skill is None:
        available = sorted(registry.list_skills())
        suffix = " Available skills: {}.".format(", ".join(available)) if available else ""
        return None, "Error: Skill '{}' not found.{}".format(normalized_name, suffix)
    return skill, None


def _build_supporting_file_catalog(skill_dir: Path) -> List[Dict[str, Any]]:
    files = [
        path
        for path in sorted(skill_dir.rglob("*"))
        if path.is_file() and path.name != "SKILL.md" and not _has_hidden_part(path, skill_dir)
    ]
    filename_counts: Dict[str, int] = {}
    for path in files:
        filename_counts[path.name] = filename_counts.get(path.name, 0) + 1

    catalog: List[Dict[str, Any]] = []
    used_keys: Set[str] = set()
    for path in files:
        kind = _classify_supporting_file(path, skill_dir)
        key = _build_catalog_key(path, skill_dir, filename_counts, used_keys)
        catalog.append({
            "key": key,
            "kind": kind,
            "filename": path.name,
            "extension": path.suffix.lower(),
            "size_bytes": path.stat().st_size,
            "path": str(path.resolve()),
        })
    return catalog


def _classify_supporting_file(path: Path, skill_dir: Path) -> str:
    parts = [part.lower() for part in path.relative_to(skill_dir).parts]
    if "scripts" in parts:
        return "script"
    if parts and parts[0] == "references":
        return "reference"
    if parts and parts[0] == "agents":
        return "agent"
    return "file"


def _build_catalog_key(
    path: Path,
    skill_dir: Path,
    filename_counts: Dict[str, int],
    used_keys: Set[str],
) -> str:
    if filename_counts.get(path.name, 0) <= 1:
        candidate = path.name
    else:
        rel_parts = path.relative_to(skill_dir).parts
        parent = rel_parts[-2] if len(rel_parts) > 1 else "file"
        candidate = "{}:{}".format(parent, path.name)

    base = candidate
    index = 2
    while candidate in used_keys:
        candidate = "{}#{}".format(base, index)
        index += 1
    used_keys.add(candidate)
    return candidate


def _public_catalog(catalog: List[Dict[str, Any]], kind: str) -> List[Dict[str, Any]]:
    return [
        {
            "key": str(entry["key"]),
            "filename": str(entry["filename"]),
            "extension": str(entry["extension"]),
            "size_bytes": int(entry["size_bytes"]),
        }
        for entry in catalog
        if entry.get("kind") == kind
    ]


def _find_catalog_entry(catalog: List[Dict[str, Any]], kind: str, key: str) -> Optional[Dict[str, Any]]:
    normalized_key = str(key or "").strip()
    if not normalized_key:
        return None
    for entry in catalog:
        if entry.get("kind") == kind and str(entry.get("key") or "") == normalized_key:
            return dict(entry)
    return None


def _read_supporting_file(path: Path) -> tuple[Optional[str], Optional[str]]:
    try:
        file_size = path.stat().st_size
    except OSError:
        return None, "Error: Skill supporting file is not accessible."
    if file_size > _MAX_RESOURCE_BYTES:
        return None, (
            "Error: Skill supporting file too large ({:,} bytes). Maximum allowed: {:,} bytes.".format(
                file_size,
                _MAX_RESOURCE_BYTES,
            )
        )
    try:
        return path.read_text(encoding="utf-8"), None
    except UnicodeDecodeError:
        return None, "Error: Skill supporting file is not valid UTF-8 text."
    except OSError:
        return None, "Error: Failed to read skill supporting file."


def _has_hidden_part(path: Path, skill_dir: Path) -> bool:
    return any(part.startswith(".") for part in path.relative_to(skill_dir).parts)


def _build_read_record(
    *,
    args: Dict[str, Any],
    context: ToolContext,
    content: str,
) -> Optional[Dict[str, Any]]:
    if not context.project_root or str(content or "").startswith("Error:"):
        return None

    skill, error = _get_skill(str(args.get("skill_name") or ""))
    if error or skill is None:
        return None

    resource = str(args.get("resource") or "").strip()
    skill_dir = Path(skill.file_path).resolve().parent
    if resource == "root":
        resolved_path = Path(skill.file_path).resolve()
    else:
        entry = _find_catalog_entry(
            _build_supporting_file_catalog(skill_dir),
            resource,
            str(args.get("key") or ""),
        )
        if entry is None:
            return None
        resolved_path = Path(entry["path"]).resolve()

    try:
        file_path = str(resolved_path.relative_to(Path(context.project_root).resolve())).replace("\\", "/")
    except ValueError:
        return None

    return build_read_document_record(
        file_path=file_path,
        resolved_path=resolved_path,
        content=content,
        source="skill",
        turn_id=context.turn_id,
        iteration=context.agent_iteration,
    )
