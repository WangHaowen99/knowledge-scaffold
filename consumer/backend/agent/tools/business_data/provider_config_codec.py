# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Shared provider config normalization and secret handling."""

from __future__ import annotations

from typing import Any, Dict

from agent.tools.business_data.provider_registry import (
    BusinessDataProviderError,
    get_provider_spec,
    normalize_provider_config,
    normalize_provider_name,
    publicize_provider_config,
)
from utils.security.secrets import decrypt_secret_fields, encrypt_secret_fields, is_encrypted_secret


def provider_secret_fields(provider: str) -> set[str]:
    try:
        spec = get_provider_spec(provider)
    except BusinessDataProviderError:
        return set()
    return {field.name for field in spec.fields if field.secret}


def provider_secret_context(provider: str) -> str:
    return "business_data.{}".format(normalize_provider_name(provider) or "unknown")


def encrypt_provider_config(provider: str, config: Dict[str, Any]) -> Dict[str, Any]:
    return encrypt_secret_fields(
        dict(config or {}),
        provider_secret_fields(provider),
        context=provider_secret_context(provider),
    )


def decrypt_provider_config(provider: str, config: Dict[str, Any]) -> Dict[str, Any]:
    return decrypt_secret_fields(
        dict(config or {}),
        provider_secret_fields(provider),
        context=provider_secret_context(provider),
    )


def publicize_source_config(provider: str, config: Dict[str, Any] | None) -> Dict[str, Any]:
    return publicize_provider_config(provider, dict(config or {}))


def normalize_source_config(
    provider: Any,
    incoming_config: Dict[str, Any] | None,
    *,
    existing_config: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    return normalize_provider_config(
        provider,
        dict(incoming_config or {}),
        existing_config=dict(existing_config or {}) if existing_config is not None else None,
    )


def normalize_database_config(
    mode: str,
    incoming_database: Dict[str, Any] | None,
    *,
    existing_database: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    del mode, existing_database
    return dict(incoming_database or {})


def provider_config_has_plaintext_secrets(provider: str, config: Dict[str, Any] | None) -> bool:
    values = dict(config or {})
    for field_name in provider_secret_fields(provider):
        value = str(values.get(field_name) or "").strip()
        if value and not is_encrypted_secret(value):
            return True
    return False


def document_has_plaintext_secrets(document: Dict[str, Any]) -> bool:
    sources = document.get("sources")
    if not isinstance(sources, dict):
        return False
    for payload in sources.values():
        if not isinstance(payload, dict):
            continue
        provider = normalize_provider_name(payload.get("provider"))
        config = payload.get("config")
        if isinstance(config, dict) and provider_config_has_plaintext_secrets(provider, config):
            return True
    return False
