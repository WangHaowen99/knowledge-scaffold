# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Workspace-private LLM profile persistence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from config.ops_config import resolve_model_profile_max_count
from config.system_config import (
    _LLM_PROFILE_DEFAULTS,
    _LLM_PROFILES_SECTION,
    SystemConfigValidationError,
    _coerce_bool,
    _coerce_float,
    _coerce_int,
    _coerce_llm_provider,
    _coerce_string,
    _generate_profile_id,
    _is_llm_configured,
    _normalize_profile_id,
    _public_llm_profile,
    test_system_config_connection,
)
from utils.security.secrets import decrypt_secret, encrypt_secret
from workspace.workspaces import WorkspaceNotFoundError, get_workspace_paths, workspace_exists


def _workspace_llm_profiles_path(workspace_id: str) -> Path:
    return get_workspace_paths(workspace_id).workspace_dir / "llm_profiles.json"


def _secret_context(workspace_id: str) -> str:
    return f"workspace_llm_profiles.{workspace_id}.profiles.api_key"


def _read_document(workspace_id: str) -> dict[str, Any]:
    path = _workspace_llm_profiles_path(workspace_id)
    if not path.exists():
        return {"profiles": []}
    try:
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return {"profiles": []}
    return raw if isinstance(raw, dict) else {"profiles": []}


def _normalize_workspace_profile(
    raw_profile: Any,
    *,
    workspace_id: str,
    existing_profile: dict[str, Any] | None = None,
    index: int = 0,
    allow_colon_id: bool = True,
) -> dict[str, Any]:
    if not isinstance(raw_profile, dict):
        raise SystemConfigValidationError("profiles[{}] 必须是对象".format(index))

    profile = dict(_LLM_PROFILE_DEFAULTS)
    if existing_profile:
        for field_name in _LLM_PROFILE_DEFAULTS:
            if field_name in existing_profile:
                profile[field_name] = existing_profile[field_name]

    raw_profile_id = _coerce_string(raw_profile.get("id")) if "id" in raw_profile else ""
    existing_profile_id = _coerce_string(existing_profile.get("id")) if existing_profile else ""
    if raw_profile_id:
        profile["id"] = _normalize_profile_id(
            raw_profile.get("id"),
            field_name="profiles[{}].id".format(index),
            allow_colon=allow_colon_id,
        )
    elif existing_profile_id:
        profile["id"] = _normalize_profile_id(
            existing_profile_id,
            field_name="profiles[{}].id".format(index),
            allow_colon=allow_colon_id,
        )
    else:
        profile["id"] = _generate_profile_id("workspace_model")

    profile["name"] = (
        _coerce_string(raw_profile.get("name")) if "name" in raw_profile else _coerce_string(profile.get("name"))
    ) or profile["id"]
    profile["provider"] = _coerce_llm_provider(
        raw_profile.get("provider") if "provider" in raw_profile else profile.get("provider")
    )

    for field_name in ("base_url", "model"):
        if field_name in raw_profile:
            profile[field_name] = _coerce_string(raw_profile.get(field_name))
        else:
            profile[field_name] = _coerce_string(profile.get(field_name))

    if "api_key" in raw_profile:
        incoming_api_key = decrypt_secret(
            _coerce_string(raw_profile.get("api_key")),
            context=_secret_context(workspace_id),
        )
        if incoming_api_key:
            profile["api_key"] = incoming_api_key
        elif raw_profile.get("clear_api_key"):
            profile["api_key"] = ""
    else:
        profile["api_key"] = decrypt_secret(
            _coerce_string(profile.get("api_key")),
            context=_secret_context(workspace_id),
        )

    if "clear_api_key" in raw_profile and _coerce_bool(
        raw_profile.get("clear_api_key"),
        field_name="profiles[{}].clear_api_key".format(index),
    ):
        profile["api_key"] = ""

    for field_name in ("timeout", "max_tokens"):
        if field_name in raw_profile:
            profile[field_name] = _coerce_int(
                raw_profile.get(field_name),
                field_name="profiles[{}].{}".format(index, field_name),
            )
        else:
            profile[field_name] = int(profile.get(field_name) or _LLM_PROFILE_DEFAULTS[field_name])

    if "temperature" in raw_profile:
        profile["temperature"] = _coerce_float(
            raw_profile.get("temperature"), field_name="profiles[{}].temperature".format(index)
        )
    else:
        profile["temperature"] = float(
            profile.get("temperature")
            if profile.get("temperature") is not None
            else _LLM_PROFILE_DEFAULTS["temperature"]
        )

    if "enabled" in raw_profile:
        profile["enabled"] = _coerce_bool(raw_profile.get("enabled"), field_name="profiles[{}].enabled".format(index))
    else:
        profile["enabled"] = bool(profile.get("enabled"))

    return profile


def _normalize_workspace_profiles_section(
    workspace_id: str,
    payload: Any,
    *,
    allow_profile_id_colon: bool = False,
) -> dict[str, Any]:
    section = payload if isinstance(payload, dict) else {}
    raw_profiles = section.get("profiles") or []
    if not isinstance(raw_profiles, list):
        raise SystemConfigValidationError("profiles 必须是数组")

    current = load_workspace_llm_profiles(workspace_id)
    existing_by_id = {
        _coerce_string(profile.get("id")): profile
        for profile in current.get("profiles", [])
        if isinstance(profile, dict) and _coerce_string(profile.get("id"))
    }
    profiles = []
    seen_ids: set[str] = set()
    for index, raw_profile in enumerate(raw_profiles):
        candidate_id = _coerce_string(raw_profile.get("id")) if isinstance(raw_profile, dict) else ""
        profile = _normalize_workspace_profile(
            raw_profile,
            workspace_id=workspace_id,
            existing_profile=existing_by_id.get(candidate_id),
            index=index,
            allow_colon_id=allow_profile_id_colon,
        )
        if profile["id"] in seen_ids:
            raise SystemConfigValidationError("profiles 中存在重复 id: {}".format(profile["id"]))
        seen_ids.add(profile["id"])
        profiles.append(profile)

    max_profiles = resolve_model_profile_max_count()
    if len(profiles) > max_profiles:
        raise SystemConfigValidationError("工作空间模型数量已达上限: {}".format(max_profiles))
    return {"profiles": profiles}


def _encrypt_document(workspace_id: str, document: dict[str, Any]) -> dict[str, Any]:
    encrypted_profiles = []
    for profile in document.get("profiles") or []:
        next_profile = dict(profile)
        if "api_key" in next_profile:
            next_profile["api_key"] = encrypt_secret(
                _coerce_string(next_profile.get("api_key")),
                context=_secret_context(workspace_id),
            )
        encrypted_profiles.append(next_profile)
    return {"profiles": encrypted_profiles}


def load_workspace_llm_profiles(workspace_id: str) -> dict[str, Any]:
    if not workspace_exists(workspace_id):
        raise WorkspaceNotFoundError(workspace_id)
    document = _read_document(workspace_id)
    profiles = []
    for index, raw_profile in enumerate(document.get("profiles") or []):
        if not isinstance(raw_profile, dict):
            continue
        profiles.append(
            _normalize_workspace_profile(
                raw_profile,
                workspace_id=workspace_id,
                index=index,
            )
        )
    return {"profiles": profiles}


def get_public_workspace_llm_profiles(workspace_id: str) -> list[dict[str, Any]]:
    return [
        _public_llm_profile(profile, include_connection=True)
        for profile in load_workspace_llm_profiles(workspace_id).get("profiles", [])
    ]


def get_enabled_workspace_llm_profiles(workspace_id: str) -> list[dict[str, Any]]:
    return [
        dict(profile)
        for profile in load_workspace_llm_profiles(workspace_id).get("profiles", [])
        if profile.get("enabled") and _is_llm_configured(profile)
    ]


def update_workspace_llm_profiles(workspace_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not workspace_exists(workspace_id):
        raise WorkspaceNotFoundError(workspace_id)
    normalized = _normalize_workspace_profiles_section(workspace_id, payload)
    path = _workspace_llm_profiles_path(workspace_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_encrypt_document(workspace_id, normalized), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {"profiles": get_public_workspace_llm_profiles(workspace_id)}


def test_workspace_llm_profile_connection(workspace_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not workspace_exists(workspace_id):
        raise WorkspaceNotFoundError(workspace_id)
    normalized = _normalize_workspace_profiles_section(workspace_id, payload)
    profiles = normalized.get("profiles") or []
    if not profiles:
        raise SystemConfigValidationError("profiles 至少需要一个模型")

    requested_profile_id = _coerce_string((payload or {}).get("profile_id"))
    profile_ids = {profile["id"] for profile in profiles}
    selected_profile_id = requested_profile_id if requested_profile_id in profile_ids else profiles[0]["id"]
    return test_system_config_connection(
        _LLM_PROFILES_SECTION,
        {
            "profile_id": selected_profile_id,
            "profiles": profiles,
        },
    )
