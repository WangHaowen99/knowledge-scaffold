"""Shared ontology binding rules for MCP token management and tool calls."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

GRAPH_SEARCH_PROVIDERS = frozenset({"neo4j", "hugegraph"})
ONTOLOGY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class MCPOntologyBindingError(ValueError):
    """Raised when an MCP ontology binding is invalid or conflicts with input."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        status_code: int = 400,
        error_type: str = "tool_input_error",
    ) -> None:
        self.code = code
        self.detail = detail
        self.status_code = status_code
        self.error_type = error_type
        super().__init__(detail)


@dataclass(frozen=True)
class MCPTokenOntologyBinding:
    workspace_id: str
    ontology_id: str
    enabled: bool
    active_version: str
    search_ready: bool
    provider: str


def validate_token_ontology_binding(
    *,
    workspace_id: str,
    ontology_id: str,
    scopes: list[str],
) -> MCPTokenOntologyBinding:
    """Validate a logical ontology binding against its current runtime state."""
    from agent.tools.business_data.ontology_sources_store import (
        build_ontology_business_data_summary,
    )
    from ontology.ontologies import (
        get_ontology_registry,
        get_runtime_ontology_manifest,
    )
    from workspace.workspaces import get_workspace_ontologies_root

    normalized_workspace_id = str(workspace_id or "").strip()
    normalized_ontology_id = str(ontology_id or "").strip()
    if not ONTOLOGY_ID_PATTERN.fullmatch(normalized_ontology_id):
        raise MCPOntologyBindingError(
            "MCP_ONTOLOGY_ID_INVALID",
            "ontology_id 格式无效",
        )

    registry = get_ontology_registry(workspace_id=normalized_workspace_id)
    ontology = registry.get_ontology(normalized_ontology_id)
    if ontology is None:
        package_dir = (
            get_workspace_ontologies_root(normalized_workspace_id)
            / normalized_ontology_id
        )
        if package_dir.is_dir() and (package_dir / "manifest.yaml").is_file():
            raise MCPOntologyBindingError(
                "MCP_ONTOLOGY_ACTIVE_VERSION_INVALID",
                "指定知识包没有可加载的激活版本",
                status_code=409,
                error_type="configuration_error",
            )
        raise MCPOntologyBindingError(
            "MCP_ONTOLOGY_NOT_FOUND",
            "当前工作空间不存在指定知识包",
            status_code=404,
            error_type="configuration_error",
        )
    if not bool(ontology.enabled):
        raise MCPOntologyBindingError(
            "MCP_ONTOLOGY_DISABLED",
            "指定知识包当前已禁用",
            status_code=409,
            error_type="configuration_error",
        )

    active_version = str(ontology.active_version or "").strip()
    versions = {
        str(version or "").strip()
        for version in list(ontology.versions or [])
        if str(version or "").strip()
    }
    runtime_manifest = (
        get_runtime_ontology_manifest(
            normalized_ontology_id,
            version=active_version,
            workspace_id=normalized_workspace_id,
        )
        if active_version
        else None
    )
    if not active_version or active_version not in versions or runtime_manifest is None:
        raise MCPOntologyBindingError(
            "MCP_ONTOLOGY_ACTIVE_VERSION_INVALID",
            "指定知识包没有可加载的激活版本",
            status_code=409,
            error_type="configuration_error",
        )

    business_data = build_ontology_business_data_summary(
        normalized_ontology_id,
        workspace_id=normalized_workspace_id,
    )
    provider = str(business_data.get("provider") or "").strip().lower()
    search_ready = bool(business_data.get("ready")) and provider in GRAPH_SEARCH_PROVIDERS
    if "mcp:search" in scopes and not search_ready:
        raise MCPOntologyBindingError(
            "MCP_ONTOLOGY_SEARCH_NOT_READY",
            "指定知识包未配置可用的 Neo4j 或 HugeGraph 数据源",
            status_code=409,
            error_type="configuration_error",
        )

    return MCPTokenOntologyBinding(
        workspace_id=normalized_workspace_id,
        ontology_id=normalized_ontology_id,
        enabled=True,
        active_version=active_version,
        search_ready=search_ready,
        provider=provider,
    )


def validate_runtime_ontology_availability(
    *,
    workspace_id: str,
    ontology_id: str,
    ontology_version: str | None = None,
) -> str:
    """Revalidate a bound ontology and return the requested or active version."""
    from ontology.ontologies import (
        get_ontology_registry,
        get_runtime_ontology_manifest,
    )

    normalized_workspace_id = str(workspace_id or "").strip()
    normalized_ontology_id = str(ontology_id or "").strip()
    registry = get_ontology_registry(workspace_id=normalized_workspace_id)
    ontology = registry.get_ontology(normalized_ontology_id)
    active_version = str(getattr(ontology, "active_version", "") or "").strip()
    selected_version = str(ontology_version or active_version).strip()
    versions = {
        str(version or "").strip()
        for version in list(getattr(ontology, "versions", []) or [])
        if str(version or "").strip()
    }
    runtime_manifest = (
        get_runtime_ontology_manifest(
            normalized_ontology_id,
            version=selected_version,
            workspace_id=normalized_workspace_id,
        )
        if ontology is not None and selected_version
        else None
    )
    availability_checks = (
        ontology is not None,
        bool(getattr(ontology, "enabled", False)),
        bool(selected_version),
        selected_version in versions,
        runtime_manifest is not None,
    )
    if not all(availability_checks):
        raise MCPOntologyBindingError(
            "MCP_ONTOLOGY_UNAVAILABLE",
            "Token 或请求指定的知识包当前不可用",
            status_code=409,
            error_type="configuration_error",
        )
    return selected_version


def resolve_effective_ontology_id(
    *,
    token_ontology_id: str,
    requested_ontology_id: Any,
) -> str:
    """Resolve a request ontology without allowing a bound token to switch it."""
    token_value = str(token_ontology_id or "").strip()
    if requested_ontology_id is None:
        requested_value = ""
    elif not isinstance(requested_ontology_id, str):
        raise MCPOntologyBindingError(
            "MCP_ONTOLOGY_ID_INVALID",
            "ontology_id 必须是字符串",
        )
    else:
        requested_value = requested_ontology_id.strip()
    if requested_value and not ONTOLOGY_ID_PATTERN.fullmatch(requested_value):
        raise MCPOntologyBindingError(
            "MCP_ONTOLOGY_ID_INVALID",
            "ontology_id 格式无效",
        )

    if token_value:
        if requested_value and requested_value != token_value:
            raise MCPOntologyBindingError(
                "MCP_ONTOLOGY_SCOPE_MISMATCH",
                "请求知识包与 MCP Token 绑定知识包不一致",
                status_code=403,
                error_type="auth_error",
            )
        return token_value
    if requested_value:
        return requested_value
    raise MCPOntologyBindingError(
        "MCP_ONTOLOGY_REQUIRED",
        "旧版未绑定 Token 必须显式传入 ontology_id",
    )


def bind_chat_arguments(
    *,
    arguments: Mapping[str, Any] | None,
    token_ontology_id: str,
) -> dict[str, Any] | None:
    """Copy chat arguments and inject a token binding when one is present."""
    result = dict(arguments or {})
    requested = result.get("selected_ontology_id")
    token_value = str(token_ontology_id or "").strip()
    if requested is not None and not isinstance(requested, str):
        raise MCPOntologyBindingError(
            "MCP_ONTOLOGY_ID_INVALID",
            "arguments.selected_ontology_id 必须是字符串",
        )
    requested_value = str(requested or "").strip()
    if requested_value and not ONTOLOGY_ID_PATTERN.fullmatch(requested_value):
        raise MCPOntologyBindingError(
            "MCP_ONTOLOGY_ID_INVALID",
            "arguments.selected_ontology_id 格式无效",
        )
    if token_value:
        if requested_value and requested_value != token_value:
            raise MCPOntologyBindingError(
                "MCP_ONTOLOGY_SCOPE_MISMATCH",
                "Chat 请求知识包与 MCP Token 绑定知识包不一致",
                status_code=403,
                error_type="auth_error",
            )
        result["selected_ontology_id"] = token_value
    elif "selected_ontology_id" in result:
        result["selected_ontology_id"] = requested_value
    return result or None
