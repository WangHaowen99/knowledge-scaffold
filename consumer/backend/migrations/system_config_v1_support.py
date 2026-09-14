# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Issue #14 system-config migration planning and durable file primitives.

The two registered migrations remain separate commit steps.  This temporary
support module contains their shared, pure in-memory model projection so the
second dry-run can inspect the document that the first migration would write.
"""

from __future__ import annotations

import copy
import json
import os
import stat
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from utils.security.secrets import (
    SecretEncryptionError,
    SecretEncryptionKeyUnavailableError,
    decrypt_secret,
    encrypt_secret,
    is_encrypted_secret,
    validate_secret_encryption_key,
)


SYSTEM_CONFIG_PATH_ENV = "SYSTEM_CONFIG_PATH"
LEGACY_TASK_SECTION = "task_llm"
PROFILES_SECTION = "llm_profiles"
LEGACY_DEFAULT_FIELD = "default_profile_id"
MAIN_REF_FIELD = "default_main_profile_ref"
TASK_REF_FIELD = "default_task_profile_ref"
PROFILES_FIELD = "profiles"
LEGACY_TASK_PROFILE_ID = "legacy-task-llm"
LLM_MAX_TOKENS_ENV = "LLM_MAX_TOKENS"
LEGACY_MAIN_PROFILE_ID = "default"

PROFILE_SECRET_CONTEXT = "llm_profiles.profiles.api_key"
LEGACY_TASK_SECRET_CONTEXT = "task_llm.api_key"
LEGACY_MAIN_SECRET_CONTEXT = "llm.api_key"

SUPPORTED_PROVIDERS = {"deepseek", "glm", "openai_compatible"}
_WINDOWS = os.name == "nt"
PROFILE_FIELDS = {
    "id",
    "name",
    "provider",
    "base_url",
    "model",
    "api_key",
    "timeout",
    "max_tokens",
    "temperature",
    "enabled",
}
LEGACY_TASK_FIELDS = PROFILE_FIELDS - {"id", "name"}

PROFILE_DEFAULTS: dict[str, Any] = {
    "name": "",
    "provider": "openai_compatible",
    "base_url": "",
    "model": "",
    "api_key": "",
    "timeout": 600,
    "max_tokens": 32768,
    "temperature": 0.7,
    "enabled": True,
}

# Legacy task_llm used 4096 as its implicit default. Keep that value only
# while interpreting the removed section so default-only legacy documents and
# configured task models retain their original behavior during migration.
LEGACY_TASK_DEFAULTS: dict[str, Any] = {
    **PROFILE_DEFAULTS,
    "max_tokens": 4096,
}

SECRET_LOCATIONS = (
    ("llm", "api_key", "llm.api_key"),
    ("auth", "auth_secret", "auth.auth_secret"),
    ("auth", "auth_server_admin_token", "auth.auth_server_admin_token"),
    ("web_search", "api_key", "web_search.api_key"),
)


@dataclass(frozen=True)
class MigrationProblem:
    code: str
    object_id: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "object_id": self.object_id,
            "detail": self.detail,
        }


@dataclass
class DocumentPlan:
    target_document: dict[str, Any]
    changed: bool
    counts: dict[str, int] = field(default_factory=dict)
    problems: list[MigrationProblem] = field(default_factory=list)


@dataclass(frozen=True)
class LoadedDocument:
    path: Path
    object_id: str
    exists: bool
    raw_bytes: bytes
    document: dict[str, Any]


Planner = Callable[[dict[str, Any], Path, bool, str], DocumentPlan]


class _SourceChangedError(OSError):
    """Raised when an external writer changes the preflight source."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def system_config_path(runtime_root: Path) -> Path:
    configured_path = str(os.getenv(SYSTEM_CONFIG_PATH_ENV) or "").strip()
    if configured_path:
        candidate = Path(configured_path).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        # Preserve the final component so _load_document can reject a symlink
        # instead of silently migrating the file it points at.
        return candidate.absolute()
    return runtime_root / "config" / "system_config.json"


def _object_id(path: Path, runtime_root: Path) -> str:
    try:
        return str(path.absolute().relative_to(runtime_root.absolute()))
    except ValueError:
        return path.name


def _problem(code: str, object_id: str, detail: str) -> MigrationProblem:
    return MigrationProblem(code=code, object_id=object_id, detail=detail)


def _operation_problem(
    code: str,
    object_id: str,
    action: str,
    exc: BaseException,
) -> MigrationProblem:
    return _problem(code, object_id, f"{action}失败（{type(exc).__name__}）")


def _open_regular_file(path: Path, flags: int) -> int:
    if path.is_symlink():
        raise OSError("file must not be a symbolic link")
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, mode=0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("file is not regular")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _open_regular_file_readonly(path: Path) -> int:
    return _open_regular_file(
        path,
        os.O_RDONLY | getattr(os, "O_BINARY", 0),
    )


def _read_regular_file_bytes(path: Path) -> bytes:
    descriptor = _open_regular_file_readonly(path)
    with os.fdopen(descriptor, "rb") as stream:
        return stream.read()


def _load_document(runtime_root: Path) -> tuple[LoadedDocument, list[MigrationProblem]]:
    path = system_config_path(runtime_root)
    object_id = _object_id(path, runtime_root)
    if not path.exists() and not path.is_symlink():
        return LoadedDocument(path, object_id, False, b"", {}), []
    if path.is_symlink():
        return (
            LoadedDocument(path, object_id, True, b"", {}),
            [_problem("SYSTEM_CONFIG_UNSAFE_FILE", object_id, "system config 不能是符号链接")],
        )
    try:
        raw_bytes = _read_regular_file_bytes(path)
        payload = json.loads(raw_bytes.decode("utf-8"))
    except json.JSONDecodeError as exc:
        return (
            LoadedDocument(path, object_id, True, b"", {}),
            [_operation_problem("SYSTEM_CONFIG_INVALID_JSON", object_id, "解析 system config", exc)],
        )
    except (OSError, UnicodeDecodeError) as exc:
        return (
            LoadedDocument(path, object_id, True, b"", {}),
            [_operation_problem("SYSTEM_CONFIG_READ_FAILED", object_id, "读取 system config", exc)],
        )
    if not isinstance(payload, dict):
        return (
            LoadedDocument(path, object_id, True, raw_bytes, {}),
            [_problem("SYSTEM_CONFIG_NOT_OBJECT", object_id, "system config 必须是 JSON object")],
        )
    return LoadedDocument(path, object_id, True, raw_bytes, payload), []


def _coerce_text(value: Any, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise TypeError("text field must be a string")
    normalized = value.strip()
    if not allow_empty and not normalized:
        raise ValueError("text field must not be empty")
    return normalized


def _coerce_int(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("integer field has invalid type")
    return value


def _coerce_float(value: Any) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError("number field has invalid type")
    return float(value)


def _legacy_main_max_tokens_default() -> int:
    """Preserve an explicit environment override while materializing legacy llm."""

    raw_value = str(os.getenv(LLM_MAX_TOKENS_ENV) or "").strip()
    if len(raw_value) >= 2 and raw_value[0] == raw_value[-1] and raw_value[0] in "'\"":
        raw_value = raw_value[1:-1].strip()
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return int(PROFILE_DEFAULTS["max_tokens"])
    return value if value > 0 else int(PROFILE_DEFAULTS["max_tokens"])


def _normalize_profile_id(value: Any) -> str:
    profile_id = _coerce_text(value, allow_empty=False)
    if any(character.isspace() for character in profile_id):
        raise ValueError("profile id contains whitespace")
    if len(profile_id) > 80:
        raise ValueError("profile id is too long")
    return profile_id


def _normalize_provider(value: Any) -> str:
    provider = _coerce_text(value).lower() or "openai_compatible"
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError("unsupported provider")
    return provider


def _key_error_code(runtime_root: Path) -> Optional[str]:
    env_value = os.getenv("SECRET_ENCRYPTION_KEY")
    key_path = runtime_root / "config" / "secret.key"
    if not env_value and not key_path.exists() and not key_path.is_symlink():
        return "SYSTEM_CONFIG_SECRET_KEY_MISSING"
    if not env_value and (key_path.is_symlink() or not key_path.is_file()):
        return "SYSTEM_CONFIG_SECRET_KEY_INVALID"
    try:
        validate_secret_encryption_key(runtime_root=runtime_root)
    except SecretEncryptionKeyUnavailableError:
        return "SYSTEM_CONFIG_SECRET_KEY_MISSING"
    except (OSError, UnicodeDecodeError, SecretEncryptionError):
        return "SYSTEM_CONFIG_SECRET_KEY_INVALID"
    return None


def _decrypt_for_comparison(
    value: str,
    *,
    context: str,
    runtime_root: Path,
    object_id: str,
) -> tuple[str, Optional[MigrationProblem]]:
    if not value or not is_encrypted_secret(value):
        return value, None
    key_error = _key_error_code(runtime_root)
    if key_error:
        return "", _problem(key_error, object_id, "模型 secret 校验需要可用的既有密钥")
    try:
        return (
            decrypt_secret(
                value,
                context=context,
                allow_key_creation=False,
                runtime_root=runtime_root,
            ),
            None,
        )
    except SecretEncryptionError:
        return "", _problem(
            "SYSTEM_CONFIG_MODEL_SECRET_DECRYPT_FAILED",
            object_id,
            "模型 secret 无法按原 context 解密",
        )


def _rebind_secret(
    value: str,
    *,
    source_context: str,
    target_context: str,
    runtime_root: Path,
    object_id: str,
) -> tuple[str, Optional[MigrationProblem]]:
    if not value or not is_encrypted_secret(value):
        return value, None
    plaintext, problem = _decrypt_for_comparison(
        value,
        context=source_context,
        runtime_root=runtime_root,
        object_id=object_id,
    )
    if problem:
        return "", problem
    try:
        rebound = encrypt_secret(
            plaintext,
            context=target_context,
            allow_key_creation=False,
            runtime_root=runtime_root,
        )
    except SecretEncryptionError:
        return "", _problem(
            "SYSTEM_CONFIG_MODEL_SECRET_REBIND_FAILED",
            object_id,
            "模型 secret context 换绑失败",
        )
    return rebound, None


def _normalize_model_semantics(
    payload: dict[str, Any],
    *,
    secret_context: str,
    runtime_root: Path,
    object_id: str,
    defaults: Optional[Mapping[str, Any]] = None,
) -> tuple[dict[str, Any], Optional[MigrationProblem]]:
    effective_defaults = PROFILE_DEFAULTS if defaults is None else defaults
    try:
        provider = _normalize_provider(
            payload.get("provider", effective_defaults["provider"])
        )
        base_url = _coerce_text(
            payload.get("base_url", effective_defaults["base_url"])
        )
        model = _coerce_text(payload.get("model", effective_defaults["model"]))
        raw_secret = payload.get("api_key", effective_defaults["api_key"])
        if not isinstance(raw_secret, str):
            raise TypeError("api_key must be a string")
        timeout = _coerce_int(
            payload.get("timeout", effective_defaults["timeout"])
        )
        max_tokens = _coerce_int(
            payload.get("max_tokens", effective_defaults["max_tokens"])
        )
        temperature = _coerce_float(
            payload.get("temperature", effective_defaults["temperature"])
        )
        enabled_value = payload.get("enabled", effective_defaults["enabled"])
        if not isinstance(enabled_value, bool):
            raise TypeError("enabled must be a boolean")
    except (TypeError, ValueError, OverflowError):
        return {}, _problem(
            "SYSTEM_CONFIG_MODEL_FIELD_TYPE_INVALID",
            object_id,
            "模型配置字段类型无效",
        )
    secret, problem = _decrypt_for_comparison(
        raw_secret,
        context=secret_context,
        runtime_root=runtime_root,
        object_id=object_id,
    )
    if problem:
        return {}, problem
    return (
        {
            "provider": provider,
            "base_url": base_url,
            "model": model,
            "api_key": secret,
            "timeout": timeout,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "enabled": enabled_value,
        },
        None,
    )


def _validate_model_field_shapes(
    payload: dict[str, Any],
    *,
    object_id: str,
) -> Optional[MigrationProblem]:
    try:
        if "provider" in payload:
            _normalize_provider(payload["provider"])
        for field_name in ("base_url", "model", "api_key"):
            if field_name in payload and not isinstance(payload[field_name], str):
                raise TypeError(f"{field_name} must be a string")
        for field_name in ("timeout", "max_tokens"):
            if field_name in payload:
                _coerce_int(payload[field_name])
        if "temperature" in payload:
            _coerce_float(payload["temperature"])
        if "enabled" in payload and not isinstance(payload["enabled"], bool):
            raise TypeError("enabled must be a boolean")
    except (TypeError, ValueError, OverflowError):
        return _problem(
            "SYSTEM_CONFIG_MODEL_FIELD_TYPE_INVALID",
            object_id,
            "模型配置字段类型无效",
        )
    return None


def _validate_profiles(
    section: dict[str, Any],
    *,
    runtime_root: Path,
    object_id: str,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[MigrationProblem]]:
    raw_profiles = section.get(PROFILES_FIELD, [])
    if not isinstance(raw_profiles, list):
        return [], {}, [
            _problem(
                "SYSTEM_CONFIG_PROFILES_TYPE_INVALID",
                object_id,
                "llm_profiles.profiles 必须是数组",
            )
        ]
    profiles: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    problems: list[MigrationProblem] = []
    for raw_profile in raw_profiles:
        if not isinstance(raw_profile, dict):
            problems.append(
                _problem(
                    "SYSTEM_CONFIG_PROFILE_TYPE_INVALID",
                    object_id,
                    "llm profile 必须是 object",
                )
            )
            continue
        try:
            profile_id = _normalize_profile_id(raw_profile.get("id"))
            if "name" in raw_profile:
                _coerce_text(raw_profile["name"])
        except (TypeError, ValueError):
            problems.append(
                _problem(
                    "SYSTEM_CONFIG_PROFILE_ID_INVALID",
                    object_id,
                    "llm profile id 无效",
                )
            )
            continue
        if profile_id in by_id:
            problems.append(
                _problem(
                    "SYSTEM_CONFIG_PROFILE_ID_DUPLICATE",
                    object_id,
                    "llm_profiles.profiles 存在重复 id",
                )
            )
            continue
        shape_problem = _validate_model_field_shapes(raw_profile, object_id=object_id)
        if shape_problem:
            problems.append(shape_problem)
            continue
        next_profile = copy.deepcopy(raw_profile)
        next_profile["id"] = profile_id
        profiles.append(next_profile)
        by_id[profile_id] = next_profile
    return profiles, by_id, problems


def _normalize_global_ref(
    value: Any,
    *,
    object_id: str,
) -> tuple[Optional[dict[str, str]], Optional[MigrationProblem]]:
    if value is None or value == "":
        return None, None
    try:
        if isinstance(value, dict):
            if set(value) - {"scope", "id"}:
                raise ValueError("unsupported ref field")
            scope = _coerce_text(value.get("scope"), allow_empty=False).lower()
            profile_id = _normalize_profile_id(value.get("id"))
        elif isinstance(value, str):
            raw = value.strip()
            if raw.startswith("global:"):
                scope = "global"
                profile_id = _normalize_profile_id(raw.split(":", 1)[1])
            elif raw.startswith("workspace:"):
                scope = "workspace"
                profile_id = _normalize_profile_id(raw.split(":", 1)[1])
            else:
                scope = "global"
                profile_id = _normalize_profile_id(raw)
        else:
            raise TypeError("ref must be an object or string")
        if scope != "global":
            raise ValueError("global ref must use global scope")
    except (TypeError, ValueError):
        return None, _problem(
            "SYSTEM_CONFIG_PROFILE_REF_INVALID",
            object_id,
            "全局模型引用格式无效",
        )
    return {"scope": "global", "id": profile_id}, None


def _profile_from_legacy(
    profile_id: str,
    name: str,
    payload: dict[str, Any],
    *,
    source_context: str,
    runtime_root: Path,
    object_id: str,
    defaults: Optional[Mapping[str, Any]] = None,
) -> tuple[dict[str, Any], Optional[MigrationProblem]]:
    semantics, problem = _normalize_model_semantics(
        payload,
        secret_context=source_context,
        runtime_root=runtime_root,
        object_id=object_id,
        defaults=defaults,
    )
    if problem:
        return {}, problem
    raw_secret = payload.get("api_key", "")
    rebound_secret, problem = _rebind_secret(
        raw_secret,
        source_context=source_context,
        target_context=PROFILE_SECRET_CONTEXT,
        runtime_root=runtime_root,
        object_id=object_id,
    )
    if problem:
        return {}, problem
    return (
        {
            "id": profile_id,
            "name": name,
            "provider": semantics["provider"],
            "base_url": semantics["base_url"],
            "model": semantics["model"],
            "api_key": rebound_secret,
            "timeout": semantics["timeout"],
            "max_tokens": semantics["max_tokens"],
            "temperature": semantics["temperature"],
            "enabled": semantics["enabled"],
        },
        None,
    )


def _profiles_are_semantically_equal(
    left: dict[str, Any],
    *,
    left_context: str,
    right: dict[str, Any],
    right_context: str,
    runtime_root: Path,
    object_id: str,
    left_defaults: Optional[Mapping[str, Any]] = None,
    right_defaults: Optional[Mapping[str, Any]] = None,
) -> tuple[bool, Optional[MigrationProblem]]:
    left_values, problem = _normalize_model_semantics(
        left,
        secret_context=left_context,
        runtime_root=runtime_root,
        object_id=object_id,
        defaults=left_defaults,
    )
    if problem:
        return False, problem
    right_values, problem = _normalize_model_semantics(
        right,
        secret_context=right_context,
        runtime_root=runtime_root,
        object_id=object_id,
        defaults=right_defaults,
    )
    if problem:
        return False, problem
    return left_values == right_values, None


def plan_model_document(
    document: dict[str, Any],
    runtime_root: Path,
    _preview_prerequisites: bool = False,
    object_id: str = "config/system_config.json",
) -> DocumentPlan:
    target = copy.deepcopy(document)
    counts = {
        "legacy_task_sections": int(LEGACY_TASK_SECTION in document),
        "legacy_default_fields": 0,
        "profiles_created": 0,
        "refs_normalized": 0,
        "files_written": 0,
    }
    problems: list[MigrationProblem] = []

    llm_section = document.get("llm", {})
    if "llm" in document and not isinstance(llm_section, dict):
        problems.append(
            _problem("SYSTEM_CONFIG_MODEL_SECTION_TYPE_INVALID", object_id, "llm 必须是 object")
        )
        llm_section = {}
    llm_shape_problem = _validate_model_field_shapes(llm_section, object_id=object_id)
    if llm_shape_problem:
        problems.append(llm_shape_problem)

    profiles_section_value = document.get(PROFILES_SECTION, {})
    if PROFILES_SECTION in document and not isinstance(profiles_section_value, dict):
        problems.append(
            _problem(
                "SYSTEM_CONFIG_MODEL_SECTION_TYPE_INVALID",
                object_id,
                "llm_profiles 必须是 object",
            )
        )
        profiles_section_value = {}
    profiles_section = copy.deepcopy(profiles_section_value)
    counts["legacy_default_fields"] = int(LEGACY_DEFAULT_FIELD in profiles_section)

    profiles, by_id, profile_problems = _validate_profiles(
        profiles_section,
        runtime_root=runtime_root,
        object_id=object_id,
    )
    problems.extend(profile_problems)

    main_ref, main_ref_problem = _normalize_global_ref(
        profiles_section.get(MAIN_REF_FIELD),
        object_id=object_id,
    )
    task_ref, task_ref_problem = _normalize_global_ref(
        profiles_section.get(TASK_REF_FIELD),
        object_id=object_id,
    )
    if main_ref_problem:
        problems.append(main_ref_problem)
    if task_ref_problem:
        problems.append(task_ref_problem)

    task_value = document.get(LEGACY_TASK_SECTION)
    if LEGACY_TASK_SECTION in document and not isinstance(task_value, dict):
        problems.append(
            _problem(
                "SYSTEM_CONFIG_LEGACY_TASK_TYPE_INVALID",
                object_id,
                "task_llm 必须是 object",
            )
        )
        task_value = {}
    task_section = task_value if isinstance(task_value, dict) else {}
    if set(task_section) - LEGACY_TASK_FIELDS:
        problems.append(
            _problem(
                "SYSTEM_CONFIG_LEGACY_TASK_FIELD_UNSUPPORTED",
                object_id,
                "task_llm 包含无法安全迁移的字段",
            )
        )

    legacy_present = (
        LEGACY_TASK_SECTION in document or LEGACY_DEFAULT_FIELD in profiles_section
    )
    if problems:
        return DocumentPlan(target, False, counts, problems)

    if legacy_present and not profiles:
        main_defaults = dict(PROFILE_DEFAULTS)
        if "max_tokens" not in llm_section:
            # A pre-profile document omitted this field intentionally; the
            # old resolver consulted LLM_MAX_TOKENS before its built-in
            # default. Preserve that precedence in the materialized profile.
            main_defaults["max_tokens"] = _legacy_main_max_tokens_default()
        main_semantics, main_problem = _normalize_model_semantics(
            llm_section,
            secret_context=LEGACY_MAIN_SECRET_CONTEXT,
            runtime_root=runtime_root,
            object_id=object_id,
            defaults=main_defaults,
        )
        if main_problem:
            problems.append(main_problem)
        elif main_semantics["base_url"] and main_semantics["model"]:
            main_profile, problem = _profile_from_legacy(
                LEGACY_MAIN_PROFILE_ID,
                "默认模型",
                llm_section,
                source_context=LEGACY_MAIN_SECRET_CONTEXT,
                runtime_root=runtime_root,
                object_id=object_id,
                defaults=main_defaults,
            )
            if problem:
                problems.append(problem)
            else:
                profiles.append(main_profile)
                by_id[LEGACY_MAIN_PROFILE_ID] = main_profile
                counts["profiles_created"] += 1
                if main_ref is None:
                    main_ref = {"scope": "global", "id": LEGACY_MAIN_PROFILE_ID}

    if LEGACY_TASK_SECTION in document:
        task_semantics, task_problem = _normalize_model_semantics(
            task_section,
            secret_context=LEGACY_TASK_SECRET_CONTEXT,
            runtime_root=runtime_root,
            object_id=object_id,
            defaults=LEGACY_TASK_DEFAULTS,
        )
        if task_problem:
            problems.append(task_problem)
        else:
            configured = bool(task_semantics["base_url"] and task_semantics["model"])
            partially_configured = bool(
                task_semantics["base_url"]
                or task_semantics["model"]
                or task_semantics["api_key"]
                or task_semantics["provider"] != LEGACY_TASK_DEFAULTS["provider"]
                or task_semantics["timeout"] != LEGACY_TASK_DEFAULTS["timeout"]
                or task_semantics["max_tokens"] != LEGACY_TASK_DEFAULTS["max_tokens"]
                or task_semantics["temperature"] != LEGACY_TASK_DEFAULTS["temperature"]
                or task_semantics["enabled"] != LEGACY_TASK_DEFAULTS["enabled"]
            )
            if partially_configured and not configured:
                problems.append(
                    _problem(
                        "SYSTEM_CONFIG_LEGACY_TASK_INCOMPLETE",
                        object_id,
                        "task_llm 连接字段不完整",
                    )
                )
            elif configured:
                if task_ref:
                    target_profile = by_id.get(task_ref["id"])
                    if target_profile is None:
                        problems.append(
                            _problem(
                                "SYSTEM_CONFIG_TASK_PROFILE_TARGET_MISSING",
                                object_id,
                                "默认轻量模型引用的目标不存在",
                            )
                        )
                    else:
                        equal, problem = _profiles_are_semantically_equal(
                            task_section,
                            left_context=LEGACY_TASK_SECRET_CONTEXT,
                            right=target_profile,
                            right_context=PROFILE_SECRET_CONTEXT,
                            runtime_root=runtime_root,
                            object_id=object_id,
                            left_defaults=LEGACY_TASK_DEFAULTS,
                        )
                        if problem:
                            problems.append(problem)
                        elif not equal:
                            problems.append(
                                _problem(
                                    "SYSTEM_CONFIG_TASK_PROFILE_CONFLICT",
                                    object_id,
                                    "task_llm 与默认轻量模型 profile 冲突",
                                )
                            )
                else:
                    target_profile = by_id.get(LEGACY_TASK_PROFILE_ID)
                    if target_profile is not None:
                        equal, problem = _profiles_are_semantically_equal(
                            task_section,
                            left_context=LEGACY_TASK_SECRET_CONTEXT,
                            right=target_profile,
                            right_context=PROFILE_SECRET_CONTEXT,
                            runtime_root=runtime_root,
                            object_id=object_id,
                            left_defaults=LEGACY_TASK_DEFAULTS,
                        )
                        if problem:
                            problems.append(problem)
                        elif not equal:
                            problems.append(
                                _problem(
                                    "SYSTEM_CONFIG_TASK_PROFILE_ID_CONFLICT",
                                    object_id,
                                    "task_llm 的稳定目标 profile id 已被占用",
                                )
                            )
                    else:
                        target_profile, problem = _profile_from_legacy(
                            LEGACY_TASK_PROFILE_ID,
                            "轻量模型",
                            task_section,
                            source_context=LEGACY_TASK_SECRET_CONTEXT,
                            runtime_root=runtime_root,
                            object_id=object_id,
                            defaults=LEGACY_TASK_DEFAULTS,
                        )
                        if problem:
                            problems.append(problem)
                        else:
                            profiles.append(target_profile)
                            by_id[LEGACY_TASK_PROFILE_ID] = target_profile
                            counts["profiles_created"] += 1
                    task_ref = {"scope": "global", "id": LEGACY_TASK_PROFILE_ID}

    old_default = profiles_section.get(LEGACY_DEFAULT_FIELD)
    if LEGACY_DEFAULT_FIELD in profiles_section:
        try:
            old_default_id = _coerce_text(old_default)
        except (TypeError, ValueError):
            problems.append(
                _problem(
                    "SYSTEM_CONFIG_LEGACY_DEFAULT_TYPE_INVALID",
                    object_id,
                    "default_profile_id 必须是字符串",
                )
            )
            old_default_id = ""
        if old_default_id:
            if old_default_id not in by_id:
                problems.append(
                    _problem(
                        "SYSTEM_CONFIG_LEGACY_DEFAULT_TARGET_MISSING",
                        object_id,
                        "default_profile_id 指向的 profile 不存在",
                    )
                )
            elif main_ref and main_ref["id"] != old_default_id:
                problems.append(
                    _problem(
                        "SYSTEM_CONFIG_DEFAULT_PROFILE_REF_CONFLICT",
                        object_id,
                        "default_profile_id 与 default_main_profile_ref 冲突",
                    )
                )
            else:
                main_ref = {"scope": "global", "id": old_default_id}

    if main_ref and main_ref["id"] not in by_id:
        problems.append(
            _problem(
                "SYSTEM_CONFIG_MAIN_PROFILE_TARGET_MISSING",
                object_id,
                "默认主模型引用的目标不存在",
            )
        )
    if task_ref and task_ref["id"] not in by_id:
        problems.append(
            _problem(
                "SYSTEM_CONFIG_TASK_PROFILE_TARGET_MISSING",
                object_id,
                "默认轻量模型引用的目标不存在",
            )
        )

    if problems:
        return DocumentPlan(target, False, counts, problems)

    if legacy_present and main_ref is None and profiles:
        main_ref = {"scope": "global", "id": profiles[0]["id"]}

    if PROFILES_SECTION in document or profiles or legacy_present:
        profiles_section[PROFILES_FIELD] = profiles
        if main_ref is None:
            profiles_section.pop(MAIN_REF_FIELD, None)
        else:
            if profiles_section.get(MAIN_REF_FIELD) != main_ref:
                counts["refs_normalized"] += 1
            profiles_section[MAIN_REF_FIELD] = main_ref
        if task_ref is None:
            profiles_section.pop(TASK_REF_FIELD, None)
        else:
            if profiles_section.get(TASK_REF_FIELD) != task_ref:
                counts["refs_normalized"] += 1
            profiles_section[TASK_REF_FIELD] = task_ref
        profiles_section.pop(LEGACY_DEFAULT_FIELD, None)
        target[PROFILES_SECTION] = profiles_section
    target.pop(LEGACY_TASK_SECTION, None)
    changed = target != document
    return DocumentPlan(target, changed, counts, [])


def _collect_secret_slots(
    document: dict[str, Any],
    *,
    object_id: str,
) -> tuple[list[tuple[dict[str, Any], str, str, str]], list[MigrationProblem]]:
    slots: list[tuple[dict[str, Any], str, str, str]] = []
    problems: list[MigrationProblem] = []
    for section_name, field_name, context in SECRET_LOCATIONS:
        section = document.get(section_name)
        if section is None:
            continue
        if not isinstance(section, dict):
            problems.append(
                _problem(
                    "SYSTEM_CONFIG_SECRET_SECTION_TYPE_INVALID",
                    object_id,
                    "包含 secret 的 section 必须是 object",
                )
            )
            continue
        if field_name not in section:
            continue
        raw_value = section[field_name]
        if not isinstance(raw_value, str):
            problems.append(
                _problem(
                    "SYSTEM_CONFIG_SECRET_FIELD_TYPE_INVALID",
                    object_id,
                    "secret 字段必须是字符串",
                )
            )
            continue
        slots.append((section, field_name, context, raw_value))

    profiles_section = document.get(PROFILES_SECTION)
    if profiles_section is not None:
        if not isinstance(profiles_section, dict):
            problems.append(
                _problem(
                    "SYSTEM_CONFIG_SECRET_SECTION_TYPE_INVALID",
                    object_id,
                    "llm_profiles 必须是 object",
                )
            )
        else:
            profiles = profiles_section.get(PROFILES_FIELD, [])
            if not isinstance(profiles, list):
                problems.append(
                    _problem(
                        "SYSTEM_CONFIG_PROFILES_TYPE_INVALID",
                        object_id,
                        "llm_profiles.profiles 必须是数组",
                    )
                )
            else:
                for profile in profiles:
                    if not isinstance(profile, dict):
                        problems.append(
                            _problem(
                                "SYSTEM_CONFIG_PROFILE_TYPE_INVALID",
                                object_id,
                                "llm profile 必须是 object",
                            )
                        )
                        continue
                    if "api_key" not in profile:
                        continue
                    raw_value = profile["api_key"]
                    if not isinstance(raw_value, str):
                        problems.append(
                            _problem(
                                "SYSTEM_CONFIG_SECRET_FIELD_TYPE_INVALID",
                                object_id,
                                "secret 字段必须是字符串",
                            )
                        )
                        continue
                    slots.append((profile, "api_key", PROFILE_SECRET_CONTEXT, raw_value))
    return slots, problems


def plan_secret_document(
    document: dict[str, Any],
    runtime_root: Path,
    preview_prerequisites: bool = False,
    object_id: str = "config/system_config.json",
) -> DocumentPlan:
    model_plan = plan_model_document(document, runtime_root, False, object_id)
    counts = {
        "secret_fields_scanned": 0,
        "plaintext_secrets": 0,
        "encrypted_secrets": 0,
        "projected_model_changes": int(model_plan.changed),
        "files_written": 0,
    }
    if model_plan.problems:
        return DocumentPlan(copy.deepcopy(document), False, counts, model_plan.problems)
    if model_plan.changed and not preview_prerequisites:
        return DocumentPlan(
            copy.deepcopy(document),
            False,
            counts,
            [
                _problem(
                    "SYSTEM_CONFIG_MODEL_MIGRATION_REQUIRED",
                    object_id,
                    "必须先完成 system config 模型字段迁移",
                )
            ],
        )

    target = copy.deepcopy(model_plan.target_document if preview_prerequisites else document)
    slots, problems = _collect_secret_slots(target, object_id=object_id)
    counts["secret_fields_scanned"] = len(slots)
    nonempty_slots = [slot for slot in slots if slot[3]]
    if nonempty_slots:
        key_error = _key_error_code(runtime_root)
        if key_error:
            problems.append(
                _problem(key_error, object_id, "system config secret 迁移需要可用的既有密钥")
            )
    if problems:
        return DocumentPlan(target, False, counts, problems)

    for container, field_name, context, raw_value in slots:
        if not raw_value:
            continue
        if is_encrypted_secret(raw_value):
            counts["encrypted_secrets"] += 1
            try:
                decrypt_secret(
                    raw_value,
                    context=context,
                    allow_key_creation=False,
                    runtime_root=runtime_root,
                )
            except SecretEncryptionError:
                problems.append(
                    _problem(
                        "SYSTEM_CONFIG_SECRET_DECRYPT_FAILED",
                        object_id,
                        "system config 密文无法按目标 context 解密",
                    )
                )
            continue
        counts["plaintext_secrets"] += 1
        try:
            container[field_name] = encrypt_secret(
                raw_value,
                context=context,
                allow_key_creation=False,
                runtime_root=runtime_root,
            )
        except SecretEncryptionError:
            problems.append(
                _problem(
                    "SYSTEM_CONFIG_SECRET_ENCRYPTION_FAILED",
                    object_id,
                    "system config 明文 secret 加密失败",
                )
            )
    return DocumentPlan(target, target != document, counts, problems)


def _fsync_directory(path: Path) -> None:
    # Windows does not support opening a directory with os.open().  The file
    # itself is flushed before os.replace(); directory fsync remains the POSIX
    # durability enhancement where the platform exposes it.
    if _WINDOWS:
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_private_file(path: Path) -> None:
    # Windows only maps chmod's write bits and otherwise relies on the
    # runtime directory's inherited DACL.  Use a writable descriptor there so
    # os.fsync maps to a valid FlushFileBuffers call.  POSIX keeps strict 0600
    # enforcement and mode verification.
    flags = (
        os.O_RDWR | getattr(os, "O_BINARY", 0)
        if _WINDOWS
        else os.O_RDONLY
    )
    descriptor = _open_regular_file(path, flags)
    try:
        if _WINDOWS:
            path.chmod(0o600)
        else:
            os.fchmod(descriptor, 0o600)
            if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o600:
                raise OSError("private file mode verification failed")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_bytes_atomic(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError("target must not be a symbolic link")
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor: Optional[int] = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            mode,
        )
        stream = os.fdopen(descriptor, "wb")
        descriptor = None
        with stream:
            if not _WINDOWS:
                os.fchmod(stream.fileno(), mode)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _ensure_private_file(path)
        _fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    _write_bytes_atomic(
        path,
        (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def _backup_base(path: Path, backup_suffix: str) -> Path:
    return path.with_name(f"{path.name}.{backup_suffix}.bak")


def _candidate_backup_path(base_path: Path, index: int) -> Path:
    return base_path if index == 0 else base_path.with_name(f"{base_path.name}.{index}")


def _create_verified_backup(
    source_path: Path,
    source_bytes: bytes,
    base_path: Path,
) -> tuple[Path, bool]:
    for index in range(1000):
        candidate = _candidate_backup_path(base_path, index)
        if candidate.exists() or candidate.is_symlink():
            if candidate.is_symlink() or not candidate.is_file():
                raise OSError("backup target is not a regular file")
            if _read_regular_file_bytes(candidate) == source_bytes:
                _ensure_private_file(candidate)
                return candidate, False
            continue
        _write_bytes_atomic(candidate, source_bytes)
        if _read_regular_file_bytes(candidate) != source_bytes:
            raise OSError("backup content verification failed")
        _ensure_private_file(candidate)
        if _read_regular_file_bytes(source_path) != source_bytes:
            raise OSError("source changed while creating backup")
        return candidate, True
    raise OSError("too many conflicting backup files")


def _marker_path(runtime_root: Path, migration_id: str) -> Path:
    return runtime_root / "config" / "migrations" / f"{migration_id}.json"


def _load_marker(
    runtime_root: Path,
    migration_id: str,
) -> tuple[Optional[dict[str, Any]], Optional[MigrationProblem]]:
    path = _marker_path(runtime_root, migration_id)
    object_id = _object_id(path, runtime_root)
    if not path.exists() and not path.is_symlink():
        return None, None
    if path.is_symlink() or not path.is_file():
        return None, _problem("SYSTEM_CONFIG_MIGRATION_MARKER_INVALID", object_id, "迁移 marker 无效")
    try:
        marker = json.loads(_read_regular_file_bytes(path).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, _operation_problem(
            "SYSTEM_CONFIG_MIGRATION_MARKER_INVALID",
            object_id,
            "读取迁移 marker",
            exc,
        )
    if not isinstance(marker, dict):
        return None, _problem("SYSTEM_CONFIG_MIGRATION_MARKER_INVALID", object_id, "迁移 marker 无效")
    if marker.get("migration_id") != migration_id or marker.get("status") != "completed":
        return None, _problem("SYSTEM_CONFIG_MIGRATION_MARKER_INVALID", object_id, "迁移 marker 无效")
    recovery_paths = marker.get("manual_recovery_backups", [])
    if not isinstance(recovery_paths, list) or any(
        not isinstance(path, str) or not path.strip() for path in recovery_paths
    ):
        return None, _problem(
            "SYSTEM_CONFIG_MIGRATION_MARKER_INVALID",
            object_id,
            "migration marker recovery paths are invalid",
        )
    return marker, None


def _discover_backups(path: Path, backup_suffix: str) -> list[Path]:
    base = _backup_base(path, backup_suffix)
    candidates = []
    for candidate in sorted(path.parent.glob(f"{base.name}*")):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        if candidate.name != base.name:
            suffix = candidate.name.removeprefix(f"{base.name}.")
            if not suffix.isdigit():
                continue
        candidates.append(candidate)
    return candidates


def _merge_backup_paths(*groups: list[Path]) -> list[Path]:
    """Return recovery paths in stable order without dropping prior backups."""

    merged: list[Path] = []
    for group in groups:
        for path in group:
            candidate = Path(path)
            if candidate not in merged:
                merged.append(candidate)
    return merged


def _write_marker(
    runtime_root: Path,
    migration_id: str,
    *,
    backup_paths: list[Path],
    counts: dict[str, int],
) -> None:
    marker_path = _marker_path(runtime_root, migration_id)
    _write_json_atomic(
        marker_path,
        {
            "migration_id": migration_id,
            "status": "completed",
            "completed_at": _utc_now(),
            "manual_recovery_backups": [str(path) for path in backup_paths],
            "counts": counts,
        },
    )
    marker, problem = _load_marker(runtime_root, migration_id)
    if problem or marker is None:
        raise OSError("marker verification failed")


def _render_report(
    migration_id: str,
    *,
    dry_run: bool,
    phase: str,
    status: str,
    counts: dict[str, int],
    problems: list[MigrationProblem],
    backup_paths: list[Path],
    planned_backup_paths: list[Path],
    from_version: str,
    to_version: str,
) -> tuple[int, dict[str, Any]]:
    report = {
        "schema_version": 1,
        "migration_id": migration_id,
        "ok": not problems,
        "dry_run": dry_run,
        "phase": phase,
        "status": status,
        "scope": {"type": "runtime", "objects": ["config/system_config.json"]},
        "from_version": from_version,
        "to_version": to_version,
        "counts": counts,
        "backup_paths": [str(path) for path in backup_paths],
        "planned_backup_paths": [str(path) for path in planned_backup_paths],
        "error_codes": sorted({problem.code for problem in problems}),
        "failed_object_ids": sorted({problem.object_id for problem in problems}),
        "errors": [problem.as_dict() for problem in problems],
        "warnings": [],
    }
    return (0 if report["ok"] else 2), report


def execute_document_migration(
    runtime_root: Path,
    *,
    migration_id: str,
    backup_suffix: str,
    planner: Planner,
    dry_run: bool,
    writes_stopped: bool,
    from_version: str = "",
    to_version: str = "",
) -> tuple[int, dict[str, Any]]:
    resolved_root = Path(runtime_root).expanduser().resolve()
    if not dry_run and not writes_stopped:
        return _render_report(
            migration_id,
            dry_run=False,
            phase="apply-precheck",
            status="blocked",
            counts={},
            problems=[
                _problem(
                    "WRITES_NOT_STOPPED",
                    _object_id(resolved_root, resolved_root),
                    "正式迁移前必须确认旧版本已停止写入",
                )
            ],
            backup_paths=[],
            planned_backup_paths=[],
            from_version=from_version,
            to_version=to_version,
        )

    loaded, load_problems = _load_document(resolved_root)
    marker, marker_problem = _load_marker(resolved_root, migration_id)
    problems = list(load_problems)
    if marker_problem:
        problems.append(marker_problem)
    if problems:
        return _render_report(
            migration_id,
            dry_run=dry_run,
            phase="dry-run" if dry_run else "apply-precheck",
            status="blocked",
            counts={},
            problems=problems,
            backup_paths=[],
            planned_backup_paths=[],
            from_version=from_version,
            to_version=to_version,
        )
    if not loaded.exists:
        missing_problems = []
        if marker is not None:
            missing_problems.append(
                _problem(
                    "SYSTEM_CONFIG_TARGET_MISSING",
                    loaded.object_id,
                    "迁移 marker 存在但 system config 目标缺失",
                )
            )
        return _render_report(
            migration_id,
            dry_run=dry_run,
            phase="dry-run" if dry_run else "apply",
            status="blocked" if missing_problems else "noop",
            counts={},
            problems=missing_problems,
            backup_paths=[],
            planned_backup_paths=[],
            from_version=from_version,
            to_version=to_version,
        )

    try:
        plan = planner(
            loaded.document,
            resolved_root,
            dry_run,
            loaded.object_id,
        )
    except Exception as exc:
        plan = DocumentPlan(
            copy.deepcopy(loaded.document),
            False,
            {},
            [
                _operation_problem(
                    "SYSTEM_CONFIG_MIGRATION_PLANNING_FAILED",
                    loaded.object_id,
                    "规划 system config 迁移",
                    exc,
                )
            ],
        )

    planned_backup = _backup_base(loaded.path, backup_suffix)
    planned_backup_paths = [planned_backup] if plan.changed else []
    marker_backup_paths = [
        Path(path)
        for path in (marker or {}).get("manual_recovery_backups", [])
        if isinstance(path, str) and path.strip()
    ]
    discovered_backup_paths = _discover_backups(loaded.path, backup_suffix)
    recovery_backup_paths = _merge_backup_paths(
        marker_backup_paths,
        discovered_backup_paths,
    )
    if plan.problems:
        return _render_report(
            migration_id,
            dry_run=dry_run,
            phase="dry-run" if dry_run else "apply-precheck",
            status="blocked",
            counts=plan.counts,
            problems=plan.problems,
            backup_paths=[],
            planned_backup_paths=planned_backup_paths,
            from_version=from_version,
            to_version=to_version,
        )
    if dry_run:
        return _render_report(
            migration_id,
            dry_run=True,
            phase="dry-run",
            status=("dry-run-ready" if plan.changed else "already-applied" if marker else "noop"),
            counts=plan.counts,
            problems=[],
            backup_paths=[],
            planned_backup_paths=planned_backup_paths,
            from_version=from_version,
            to_version=to_version,
        )

    if not plan.changed:
        existing_backups = recovery_backup_paths
        if marker is None and existing_backups:
            try:
                for backup in existing_backups:
                    _ensure_private_file(backup)
                _write_marker(
                    resolved_root,
                    migration_id,
                    backup_paths=existing_backups,
                    counts=plan.counts,
                )
            except Exception as exc:
                return _render_report(
                    migration_id,
                    dry_run=False,
                    phase="apply",
                    status="blocked",
                    counts=plan.counts,
                    problems=[
                        _operation_problem(
                            "SYSTEM_CONFIG_MIGRATION_MARKER_WRITE_FAILED",
                            loaded.object_id,
                            "补写迁移 marker",
                            exc,
                        )
                    ],
                    backup_paths=existing_backups,
                    planned_backup_paths=[],
                    from_version=from_version,
                    to_version=to_version,
                )
            return _render_report(
                migration_id,
                dry_run=False,
                phase="apply",
                status="completed-after-retry",
                counts=plan.counts,
                problems=[],
                backup_paths=existing_backups,
                planned_backup_paths=[],
                from_version=from_version,
                to_version=to_version,
            )
        return _render_report(
            migration_id,
            dry_run=False,
            phase="apply",
            status="already-applied" if marker else "noop",
            counts=plan.counts,
            problems=[],
            backup_paths=recovery_backup_paths,
            planned_backup_paths=[],
            from_version=from_version,
            to_version=to_version,
        )

    # Keep every previously verified recovery path when a completed migration
    # is intentionally re-applied after an operator changes the target.
    backup_paths: list[Path] = list(recovery_backup_paths)

    def apply_failure(
        code: str,
        action: str,
        exc: BaseException,
        *,
        counts: dict[str, int] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        return _render_report(
            migration_id,
            dry_run=False,
            phase="apply",
            status="blocked",
            counts=counts or plan.counts,
            problems=[_operation_problem(code, loaded.object_id, action, exc)],
            backup_paths=backup_paths,
            planned_backup_paths=planned_backup_paths,
            from_version=from_version,
            to_version=to_version,
        )

    try:
        backup_path, backup_created = _create_verified_backup(
            loaded.path,
            loaded.raw_bytes,
            planned_backup,
        )
        if backup_path not in backup_paths:
            backup_paths.append(backup_path)
        if _read_regular_file_bytes(loaded.path) != loaded.raw_bytes:
            raise _SourceChangedError("source changed after precheck")
    except _SourceChangedError as exc:
        return apply_failure(
            "SYSTEM_CONFIG_TARGET_CONFLICT",
            "验证 system config 源版本",
            exc,
        )
    except Exception as exc:
        return apply_failure(
            "SYSTEM_CONFIG_BACKUP_FAILED",
            "创建并验证 system config 人工恢复备份",
            exc,
        )

    target_bytes = (
        json.dumps(plan.target_document, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    temporary = loaded.path.with_name(f"{loaded.path.name}.{uuid.uuid4().hex}.migration.tmp")
    try:
        _write_bytes_atomic(temporary, target_bytes)
        temporary_payload = json.loads(_read_regular_file_bytes(temporary).decode("utf-8"))
        if temporary_payload != plan.target_document:
            raise OSError("temporary target verification failed")
        verification = planner(
            temporary_payload,
            resolved_root,
            False,
            loaded.object_id,
        )
        if verification.problems or verification.changed:
            raise OSError("temporary target is not canonical")
        if _read_regular_file_bytes(loaded.path) != loaded.raw_bytes:
            raise _SourceChangedError("source changed before atomic replace")
        os.replace(temporary, loaded.path)
        _ensure_private_file(loaded.path)
        _fsync_directory(loaded.path.parent)
    except _SourceChangedError as exc:
        return apply_failure(
            "SYSTEM_CONFIG_TARGET_CONFLICT",
            "验证 system config 源版本",
            exc,
        )
    except Exception as exc:
        return apply_failure(
            "SYSTEM_CONFIG_ATOMIC_WRITE_FAILED",
            "原子写入 system config",
            exc,
        )
    finally:
        temporary.unlink(missing_ok=True)

    counts = dict(plan.counts)
    counts["files_written"] = 1
    counts["backups_created"] = int(backup_created)
    try:
        persisted = json.loads(_read_regular_file_bytes(loaded.path).decode("utf-8"))
        if persisted != plan.target_document:
            raise OSError("persisted target verification failed")
        verification = planner(persisted, resolved_root, False, loaded.object_id)
        if verification.problems or verification.changed:
            raise OSError("persisted target is not canonical")
    except Exception as exc:
        return apply_failure(
            "SYSTEM_CONFIG_TARGET_VERIFICATION_FAILED",
            "回读验证 system config 目标",
            exc,
            counts=counts,
        )

    try:
        _write_marker(
            resolved_root,
            migration_id,
            backup_paths=backup_paths,
            counts=counts,
        )
    except Exception as exc:
        return apply_failure(
            "SYSTEM_CONFIG_MIGRATION_MARKER_WRITE_FAILED",
            "写入并验证 system config 迁移 marker",
            exc,
            counts=counts,
        )

    return _render_report(
        migration_id,
        dry_run=False,
        phase="apply",
        status="applied",
        counts=counts,
        problems=[],
        backup_paths=backup_paths,
        planned_backup_paths=planned_backup_paths,
        from_version=from_version,
        to_version=to_version,
    )
