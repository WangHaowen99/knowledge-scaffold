"""Workspace metadata and path resolution helpers."""

from __future__ import annotations

import copy
import json
import logging
import re
import shutil
import unicodedata
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional
from urllib.parse import unquote

from flask import Request

from config import AppConfig, LimitsConfig
from config.model_profile_refs import model_profile_ref_to_dict, parse_model_profile_ref


WORKSPACE_HEADER = "X-Workspace-Id"
LEGACY_DEFAULT_WORKSPACE_ID = "default"
_WORKSPACE_ID_MAX_LEN = 64
_INVALID_WORKSPACE_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_workspace_var: ContextVar[Optional[str]] = ContextVar("workspace_id", default=None)


class WorkspaceNotFoundError(KeyError):
    """Raised when a request explicitly references a missing workspace."""

    code = "WORKSPACE_NOT_FOUND"

    def __init__(self, workspace_id: str) -> None:
        self.workspace_id = validate_workspace_id(workspace_id)
        self.detail = f"工作空间 '{self.workspace_id}' 不存在"
        super().__init__(self.detail)

    def __str__(self) -> str:
        return self.detail

    def to_dict(self) -> Dict[str, str]:
        return {
            "code": self.code,
            "detail": self.detail,
            "workspace_id": self.workspace_id,
        }


@dataclass(frozen=True)
class WorkspacePaths:
    workspace_id: str
    workspace_dir: Path
    metadata_path: Path
    ontologies_dir: Path
    skills_dir: Path
    files_dir: Path
    runtime_dir: Path
    users_runtime_dir: Path


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_runtime_root() -> Path:
    return Path(AppConfig.from_env().project_root).resolve()


def get_system_root() -> Path:
    return get_runtime_root() / "system"


def get_workspaces_root() -> Path:
    return get_runtime_root() / "workspaces"


def get_workspace_settings_path() -> Path:
    return get_runtime_root() / "workspace-settings.json"


def _ensure_runtime_layout() -> None:
    get_runtime_root().mkdir(parents=True, exist_ok=True)
    get_workspaces_root().mkdir(parents=True, exist_ok=True)
    (get_system_root() / "skills").mkdir(parents=True, exist_ok=True)


def _normalize_workspace_name(name: str) -> str:
    value = str(name or "").strip()
    if not value:
        raise ValueError("工作空间名称不能为空")
    return value


_WORKSPACE_ID_PATTERN = re.compile(r'^[a-z][a-z0-9_-]*$')


def validate_workspace_id(workspace_id: str) -> str:
    value = str(workspace_id or "").strip().lower()
    if not value:
        raise ValueError("工作空间 ID 不能为空")

    if not _WORKSPACE_ID_PATTERN.match(value):
        raise ValueError("工作空间 ID 格式非法：必须以英文小写字母开头，只能包含英文、数字、下划线(_)或中划线(-)")

    if len(value) > _WORKSPACE_ID_MAX_LEN:
        raise ValueError(f"工作空间 ID 过长，最多 {_WORKSPACE_ID_MAX_LEN} 个字符")

    return value


def _slugify_workspace_name(name: str) -> str:
    normalized = unicodedata.normalize("NFKC", _normalize_workspace_name(name)).strip()
    ascii_name = unicodedata.normalize("NFKD", normalized).encode("ascii", "ignore").decode("ascii")
    if ascii_name.strip():
        candidate = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_name).strip("-").lower()
    else:
        candidate = re.sub(r"\s+", "-", normalized).strip("-")
    candidate = re.sub(r"-{2,}", "-", candidate).strip("-")
    if not candidate:
        candidate = f"workspace-{uuid.uuid4().hex[:8]}"
    candidate = candidate[:_WORKSPACE_ID_MAX_LEN]
    return validate_workspace_id(candidate)


def _with_suffix(base_id: str, suffix: str) -> str:
    suffix_part = f"-{suffix}"
    trimmed_base = base_id[: max(1, _WORKSPACE_ID_MAX_LEN - len(suffix_part))]
    return validate_workspace_id(f"{trimmed_base}{suffix_part}")


def _read_workspace_metadata_raw(metadata_path: Path) -> Dict[str, str]:
    with metadata_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _write_workspace_metadata(paths: WorkspacePaths, payload: Dict[str, str]) -> None:
    paths.workspace_dir.mkdir(parents=True, exist_ok=True)
    paths.metadata_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _read_workspace_settings() -> Dict[str, str]:
    settings_path = get_workspace_settings_path()
    if not settings_path.exists():
        return {}
    try:
        with settings_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_workspace_settings(payload: Dict[str, str]) -> None:
    settings_path = get_workspace_settings_path()
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _list_workspace_dirs() -> List[Path]:
    _ensure_runtime_layout()
    workspaces_root = get_workspaces_root()
    if not workspaces_root.exists():
        return []
    return sorted(
        [entry for entry in workspaces_root.iterdir() if entry.is_dir() and (entry / "workspace.json").exists()],
        key=lambda item: item.name,
    )


def _workspace_to_public(data: Dict[str, str], *, default_workspace_id: Optional[str] = None) -> Dict[str, str]:
    workspace_id = validate_workspace_id(data["id"])
    return {
        "id": workspace_id,
        "name": data["name"],
        "description": data.get("description", ""),
        "created_at": data["created_at"],
        "updated_at": data["updated_at"],
        "is_default": workspace_id == default_workspace_id,
    }


def _init_workspace_layout(paths: WorkspacePaths, *, name: str, description: str = "") -> Dict[str, str]:
    now = _utc_now()
    payload = {
        "id": paths.workspace_id,
        "name": _normalize_workspace_name(name),
        "description": str(description or "").strip(),
        "created_at": now,
        "updated_at": now,
    }
    paths.ontologies_dir.mkdir(parents=True, exist_ok=True)
    paths.skills_dir.mkdir(parents=True, exist_ok=True)
    paths.files_dir.mkdir(parents=True, exist_ok=True)
    paths.users_runtime_dir.mkdir(parents=True, exist_ok=True)
    _write_workspace_metadata(paths, payload)
    return payload


def get_workspace_paths(workspace_id: Optional[str] = None) -> WorkspacePaths:
    resolved_id = validate_workspace_id(workspace_id or get_current_workspace_id())
    workspace_dir = get_workspaces_root() / resolved_id
    runtime_dir = workspace_dir / "runtime"
    return WorkspacePaths(
        workspace_id=resolved_id,
        workspace_dir=workspace_dir,
        metadata_path=workspace_dir / "workspace.json",
        ontologies_dir=workspace_dir / "ontologies",
        skills_dir=workspace_dir / "skills",
        files_dir=workspace_dir / "files",
        runtime_dir=runtime_dir,
        users_runtime_dir=runtime_dir / "users",
    )


def get_current_workspace_paths() -> WorkspacePaths:
    return get_workspace_paths(get_current_workspace_id())


def get_workspace_project_root(workspace_id: Optional[str] = None) -> Path:
    return get_workspace_paths(workspace_id).workspace_dir.resolve()


def get_workspace_ontologies_root(workspace_id: Optional[str] = None) -> Path:
    return get_workspace_paths(workspace_id).ontologies_dir.resolve()


def get_workspace_skills_root(workspace_id: Optional[str] = None) -> Path:
    return get_workspace_paths(workspace_id).skills_dir.resolve()


def get_system_skills_root() -> Path:
    return (get_system_root() / "skills").resolve()


def get_skill_roots(workspace_id: Optional[str] = None) -> List[Path]:
    return [
        get_workspace_skills_root(workspace_id),
        get_system_skills_root(),
    ]


def workspace_exists(workspace_id: str) -> bool:
    return get_workspace_paths(workspace_id).metadata_path.exists()


def _migrate_legacy_default_workspace() -> Optional[str]:
    _ensure_runtime_layout()
    legacy_dir = get_workspaces_root() / LEGACY_DEFAULT_WORKSPACE_ID
    legacy_metadata = legacy_dir / "workspace.json"
    if not legacy_metadata.exists():
        return None

    raw = _read_workspace_metadata_raw(legacy_metadata)
    target_id = _slugify_workspace_name(raw.get("name") or raw.get("id") or "workspace")
    if target_id == LEGACY_DEFAULT_WORKSPACE_ID:
        target_id = validate_workspace_id(f"workspace-{uuid.uuid4().hex[:8]}")
    while (get_workspaces_root() / target_id).exists():
        target_id = _with_suffix(target_id, uuid.uuid4().hex[:4])

    target_dir = get_workspaces_root() / target_id
    shutil.move(str(legacy_dir), str(target_dir))
    target_paths = get_workspace_paths(target_id)
    raw["id"] = target_id
    raw["updated_at"] = raw.get("updated_at") or _utc_now()
    _write_workspace_metadata(target_paths, raw)

    settings = _read_workspace_settings()
    if settings.get("default_workspace_id") in {None, "", LEGACY_DEFAULT_WORKSPACE_ID}:
        settings["default_workspace_id"] = target_id
        _write_workspace_settings(settings)

    return target_id


def _ensure_workspace_state() -> None:
    _ensure_runtime_layout()
    migrated_id = _migrate_legacy_default_workspace()
    workspace_dirs = _list_workspace_dirs()
    settings = _read_workspace_settings()
    default_workspace_id = settings.get("default_workspace_id")

    if default_workspace_id and workspace_exists(default_workspace_id):
        return

    if migrated_id and workspace_exists(migrated_id):
        settings["default_workspace_id"] = migrated_id
        _write_workspace_settings(settings)
        return

    if len(workspace_dirs) == 1:
        only_id = validate_workspace_id(workspace_dirs[0].name)
        settings["default_workspace_id"] = only_id
        _write_workspace_settings(settings)
        return

    if not workspace_dirs:
        settings.pop("default_workspace_id", None)
        if settings:
            _write_workspace_settings(settings)


def get_default_workspace_id() -> Optional[str]:
    _ensure_workspace_state()
    settings = _read_workspace_settings()
    default_workspace_id = settings.get("default_workspace_id")
    if default_workspace_id and workspace_exists(default_workspace_id):
        return validate_workspace_id(default_workspace_id)

    workspace_dirs = _list_workspace_dirs()
    if len(workspace_dirs) == 1:
        only_id = validate_workspace_id(workspace_dirs[0].name)
        set_default_workspace(only_id)
        return only_id
    return None


def set_default_workspace(workspace_id: str) -> Dict[str, str]:
    resolved_id = validate_workspace_id(workspace_id)
    paths = get_workspace_paths(resolved_id)
    if not paths.metadata_path.exists():
        raise WorkspaceNotFoundError(resolved_id)
    settings = _read_workspace_settings()
    settings["default_workspace_id"] = resolved_id
    _write_workspace_settings(settings)
    return get_workspace(resolved_id)


def get_current_workspace_id() -> str:
    current = _workspace_var.get()
    if current:
        return validate_workspace_id(current)
    default_workspace_id = get_default_workspace_id()
    if default_workspace_id:
        return default_workspace_id
    raise ValueError("当前未配置默认工作空间")


def list_workspaces() -> List[Dict[str, str]]:
    _ensure_workspace_state()
    default_workspace_id = get_default_workspace_id()
    items: List[Dict[str, str]] = []
    for entry in _list_workspace_dirs():
        metadata_path = entry / "workspace.json"
        try:
            payload = _read_workspace_metadata_raw(metadata_path)
            items.append(_workspace_to_public(payload, default_workspace_id=default_workspace_id))
        except Exception:
            continue

    items.sort(key=lambda item: (0 if item.get("is_default") else 1, item["name"].lower(), item["id"]))
    return items


def get_workspace(workspace_id: str) -> Dict[str, str]:
    _ensure_workspace_state()
    paths = get_workspace_paths(workspace_id)
    if not paths.metadata_path.exists():
        raise WorkspaceNotFoundError(workspace_id)
    return _workspace_to_public(
        _read_workspace_metadata_raw(paths.metadata_path),
        default_workspace_id=get_default_workspace_id(),
    )


def create_workspace(
    name: str,
    description: str = "",
    *,
    workspace_id: Optional[str] = None,
    set_as_default: bool = False
) -> Dict[str, str]:
    _ensure_workspace_state()
    normalized_name = _normalize_workspace_name(name)

    if workspace_id:
        candidate = validate_workspace_id(workspace_id)
        if workspace_exists(candidate):
            raise ValueError(f"工作空间 ID '{candidate}' 已存在")
    else:
        candidate = _slugify_workspace_name(normalized_name)
        while workspace_exists(candidate):
            candidate = _with_suffix(candidate, uuid.uuid4().hex[:4])

    max_workspaces = LimitsConfig.from_env().workspace_max_count
    if len(list_workspaces()) >= max_workspaces:
        raise ValueError("工作空间数量已达上限: {}".format(max_workspaces))

    paths = get_workspace_paths(candidate)
    _init_workspace_layout(paths, name=normalized_name, description=description)
    if set_as_default or get_default_workspace_id() is None:
        set_default_workspace(candidate)
    return get_workspace(candidate)


def update_workspace(
    workspace_id: str,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
) -> Dict[str, str]:
    paths = get_workspace_paths(workspace_id)
    if not paths.metadata_path.exists():
        raise WorkspaceNotFoundError(workspace_id)
    payload = _read_workspace_metadata_raw(paths.metadata_path)
    if name is not None:
        payload["name"] = _normalize_workspace_name(name)
    if description is not None:
        payload["description"] = str(description or "").strip()
    payload["updated_at"] = _utc_now()
    _write_workspace_metadata(paths, payload)
    return get_workspace(paths.workspace_id)


def delete_workspace(workspace_id: str) -> None:
    resolved_id = validate_workspace_id(workspace_id)
    default_workspace_id = get_default_workspace_id()
    if default_workspace_id == resolved_id:
        raise ValueError("默认工作空间不可删除，请先切换默认工作空间")

    paths = get_workspace_paths(resolved_id)
    if not paths.metadata_path.exists():
        raise WorkspaceNotFoundError(resolved_id)
    shutil.rmtree(paths.workspace_dir, ignore_errors=True)


def resolve_workspace_id(request: Request) -> str:
    _ensure_workspace_state()
    header_value = request.headers.get(WORKSPACE_HEADER)
    if header_value:
        workspace_id = validate_workspace_id(unquote(header_value))
        if not workspace_exists(workspace_id):
            raise WorkspaceNotFoundError(workspace_id)
        return workspace_id

    default_workspace_id = get_default_workspace_id()
    if default_workspace_id and workspace_exists(default_workspace_id):
        return default_workspace_id

    workspaces = list_workspaces()
    if len(workspaces) == 1:
        return workspaces[0]["id"]
    if not workspaces:
        raise ValueError("当前不存在任何工作空间")
    raise ValueError("X-Workspace-Id 不能为空；当前存在多个工作空间")


@contextmanager
def workspace_scope(workspace_id: Optional[str] = None) -> Iterator[str]:
    resolved_id = validate_workspace_id(workspace_id or get_current_workspace_id())
    token = _workspace_var.set(resolved_id)
    try:
        yield resolved_id
    finally:
        _workspace_var.reset(token)


DEFAULT_RUNTIME_CONFIG: Dict[str, Any] = {
    "lang": {
        "enabled": False,
        "default": "zh",
    },
    "acp_suppress_script_patterns": "",
    "llm": {
        "default_main_profile_ref": None,
        "default_task_profile_ref": None,
    },
}

_RUNTIME_CONFIG_KEYS = set(DEFAULT_RUNTIME_CONFIG.keys())
_LANG_CONFIG_KEYS = set(DEFAULT_RUNTIME_CONFIG["lang"].keys())
_LLM_CONFIG_KEYS = set(DEFAULT_RUNTIME_CONFIG["llm"].keys())


def _get_runtime_config_path(workspace_id: str) -> Path:
    return get_workspace_paths(workspace_id).workspace_dir / "runtime_config.json"


def _migrate_legacy_runtime_llm_ref(workspace_id, config_path, saved):
    """DEPRECATED: rename legacy llm.default_profile_ref to default_main_profile_ref. Remove next release."""
    llm = saved.get("llm")
    if not isinstance(llm, dict) or "default_profile_ref" not in llm:
        return saved
    if "default_main_profile_ref" not in llm:
        llm["default_main_profile_ref"] = llm.pop("default_profile_ref")
    else:
        llm.pop("default_profile_ref", None)
    try:
        config_path.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.getLogger(__name__).warning(
            "Migrated workspace %s legacy llm.default_profile_ref to default_main_profile_ref",
            workspace_id,
        )
    except Exception:
        logging.getLogger(__name__).warning(
            "Failed to persist runtime config migration for workspace %s", workspace_id, exc_info=True
        )
    return saved


def get_workspace_runtime_config(workspace_id: str) -> dict:
    if not workspace_exists(workspace_id):
        raise WorkspaceNotFoundError(workspace_id)
    config_path = _get_runtime_config_path(workspace_id)
    if not config_path.exists():
        return copy.deepcopy(DEFAULT_RUNTIME_CONFIG)
    try:
        with config_path.open("r", encoding="utf-8") as f:
            saved = json.load(f)
    except Exception:
        return copy.deepcopy(DEFAULT_RUNTIME_CONFIG)
    if not isinstance(saved, dict):
        return copy.deepcopy(DEFAULT_RUNTIME_CONFIG)
    saved = _migrate_legacy_runtime_llm_ref(workspace_id, config_path, saved)
    merged = copy.deepcopy(DEFAULT_RUNTIME_CONFIG)
    try:
        _deep_update_runtime_config(merged, _normalize_runtime_config_patch(saved, strict=False))
    except ValueError:
        return copy.deepcopy(DEFAULT_RUNTIME_CONFIG)
    return merged


def update_workspace_runtime_config(workspace_id: str, data: dict) -> dict:
    if not workspace_exists(workspace_id):
        raise WorkspaceNotFoundError(workspace_id)
    normalized = _normalize_runtime_config_patch(data, strict=True)
    current = get_workspace_runtime_config(workspace_id)
    _deep_update_runtime_config(current, normalized)
    config_path = _get_runtime_config_path(workspace_id)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(current, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return current


def get_current_workspace_runtime_config() -> dict:
    return get_workspace_runtime_config(get_current_workspace_id())


def _deep_update_runtime_config(target: dict, source: dict) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update_runtime_config(target[key], value)
        else:
            target[key] = value


def _normalize_runtime_config_patch(data: dict, *, strict: bool) -> dict:
    if not isinstance(data, dict):
        raise ValueError("Runtime config patch must be an object")

    normalized: dict = {}
    for key, value in data.items():
        if key not in _RUNTIME_CONFIG_KEYS:
            if strict:
                raise ValueError(f"Unknown runtime config key: {key}")
            continue

        if key == "lang":
            if not isinstance(value, dict):
                raise ValueError("lang must be an object")
            normalized_lang = {}
            for lang_key, lang_value in value.items():
                if lang_key not in _LANG_CONFIG_KEYS:
                    if strict:
                        raise ValueError(f"Unknown runtime config key: lang.{lang_key}")
                    continue
                if lang_key == "enabled" and not isinstance(lang_value, bool):
                    raise ValueError("lang.enabled must be a boolean")
                if lang_key == "default" and not isinstance(lang_value, str):
                    raise ValueError("lang.default must be a string")
                normalized_lang[lang_key] = lang_value
            normalized[key] = normalized_lang

        if key == "acp_suppress_script_patterns":
            if isinstance(value, str):
                normalized[key] = value
            elif isinstance(value, list) and all(isinstance(item, str) for item in value):
                normalized[key] = value
            else:
                raise ValueError("acp_suppress_script_patterns must be a string or list of strings")

        if key == "llm":
            if not isinstance(value, dict):
                raise ValueError("llm must be an object")
            normalized_llm = {}
            for llm_key, llm_value in value.items():
                if llm_key not in _LLM_CONFIG_KEYS:
                    if strict:
                        raise ValueError(f"Unknown runtime config key: llm.{llm_key}")
                    continue
                if llm_key in ("default_main_profile_ref", "default_task_profile_ref"):
                    if llm_value in (None, ""):
                        normalized_llm[llm_key] = None
                    else:
                        try:
                            normalized_llm[llm_key] = model_profile_ref_to_dict(
                                parse_model_profile_ref(llm_value)
                            )
                        except ValueError as exc:
                            raise ValueError(str(exc)) from exc
            normalized[key] = normalized_llm
    return normalized
