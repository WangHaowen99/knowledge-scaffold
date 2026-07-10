"""MCP service catalog used by the management API."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .tool_contracts import CHAT_TOOL_DESCRIPTION, list_search_tools


BACKEND_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CATALOG_PATH = BACKEND_ROOT / "config" / "mcp_services.yaml"
SUPPORTED_TOOLSETS = {"search", "chat"}


@dataclass(frozen=True)
class MCPServiceCatalogItem:
    service_id: str
    toolset: str
    title: str
    summary: str
    description: str
    endpoint_prefix: str
    icon: str
    tags: list[str]


def load_mcp_service_catalog() -> list[MCPServiceCatalogItem]:
    path, explicit = _resolve_catalog_path()
    if path.exists():
        return _load_catalog_from_yaml(path)
    if explicit:
        raise FileNotFoundError("MCP 服务目录配置不存在: {}".format(path))
    return _default_catalog()


def _default_catalog() -> list[MCPServiceCatalogItem]:
    return [
        MCPServiceCatalogItem(
            service_id="meta-wise-search",
            toolset="search",
            title="知识检索",
            summary="原子检索工具集",
            description="提供不调用 LLM 的 schema、属性过滤、路径查询、统计和聚合等原子检索工具。",
            endpoint_prefix="/search",
            icon="search",
            tags=["图谱查询", "原子工具"],
        ),
        MCPServiceCatalogItem(
            service_id="meta-wise-chat",
            toolset="chat",
            title="知识问答",
            summary="Agent级对话工具集",
            description="调用 MetaWise 主 Agent workflow，提供面向知识应用的 Agent 级对话能力。",
            endpoint_prefix="/chat",
            icon="message-circle",
            tags=["Agent", "问答"],
        ),
    ]


def _resolve_catalog_path() -> tuple[Path, bool]:
    raw = str(os.getenv("MCP_SERVICES_CONFIG_FILE", "") or "").strip()
    if not raw:
        return DEFAULT_CATALOG_PATH, False
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (BACKEND_ROOT / path).resolve()
    return path, True


def _load_catalog_from_yaml(path: Path) -> list[MCPServiceCatalogItem]:
    with path.open("r", encoding="utf-8") as fh:
        payload = yaml.safe_load(fh) or {}
    if not isinstance(payload, dict):
        raise ValueError("MCP 服务目录必须是顶层对象: {}".format(path))
    raw_services = payload.get("services")
    if not isinstance(raw_services, list) or not raw_services:
        raise ValueError("MCP 服务目录 services 不能为空: {}".format(path))

    result: list[MCPServiceCatalogItem] = []
    seen_ids: set[str] = set()
    seen_prefixes: set[str] = set()
    for index, raw_item in enumerate(raw_services):
        if not isinstance(raw_item, dict):
            raise ValueError("services[{}] 必须是对象".format(index))
        if not bool(raw_item.get("enabled", True)):
            continue
        item = _parse_catalog_item(raw_item, index=index)
        if item.service_id in seen_ids:
            raise ValueError("MCP 服务 id 重复: {}".format(item.service_id))
        if item.endpoint_prefix in seen_prefixes:
            raise ValueError("MCP endpoint_prefix 重复: {}".format(item.endpoint_prefix))
        seen_ids.add(item.service_id)
        seen_prefixes.add(item.endpoint_prefix)
        result.append(item)
    if not result:
        raise ValueError("MCP 服务目录没有启用项: {}".format(path))
    return result


def _parse_catalog_item(raw: dict[str, Any], *, index: int) -> MCPServiceCatalogItem:
    service_id = str(raw.get("id") or raw.get("service_id") or "").strip()
    if not service_id:
        raise ValueError("services[{}].id 不能为空".format(index))
    toolset = str(raw.get("toolset") or "").strip().lower()
    if toolset not in SUPPORTED_TOOLSETS:
        raise ValueError("services[{}].toolset 必须是 {}".format(index, sorted(SUPPORTED_TOOLSETS)))
    title = str(raw.get("title") or "").strip()
    summary = str(raw.get("summary") or "").strip()
    description = str(raw.get("description") or "").strip()
    if not title:
        raise ValueError("services[{}].title 不能为空".format(index))
    if not summary:
        raise ValueError("services[{}].summary 不能为空".format(index))
    if not description:
        raise ValueError("services[{}].description 不能为空".format(index))
    return MCPServiceCatalogItem(
        service_id=service_id,
        toolset=toolset,
        title=title,
        summary=summary,
        description=description,
        endpoint_prefix=_normalize_endpoint_prefix(raw.get("endpoint_prefix")),
        icon=str(raw.get("icon") or "server").strip() or "server",
        tags=_normalize_tags(raw.get("tags")),
    )


def _normalize_endpoint_prefix(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("endpoint_prefix 不能为空")
    if not text.startswith("/"):
        text = "/{}".format(text)
    return text.rstrip("/") or "/"


def _normalize_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("tags 必须是数组")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def list_mcp_service_cards(config: Any) -> list[dict[str, Any]]:
    base_url = config.effective_public_base_url()
    transport = _normalize_transport(getattr(config, "transport", "streamable-http"))
    sse_enabled = transport == "sse"
    tools = {
        "search": list_search_tools(),
        "chat": [{"name": "chat", "description": CHAT_TOOL_DESCRIPTION}],
    }
    cards = []
    for item in load_mcp_service_catalog():
        if transport == "sse":
            active_url = "{}{}/sse".format(base_url, item.endpoint_prefix)
            streamable_http_url = ""
            sse_url = active_url
        else:
            active_url = "{}{}/mcp".format(base_url, item.endpoint_prefix)
            streamable_http_url = active_url
            sse_url = ""
        cards.append({
            "id": item.service_id,
            "toolset": item.toolset,
            "title": item.title,
            "summary": item.summary,
            "description": item.description,
            "icon": item.icon,
            "tags": list(item.tags),
            "transport": transport,
            "client_config_type": "sse" if transport == "sse" else "http",
            "sse_enabled": sse_enabled,
            "url": active_url,
            "active_url": active_url,
            "streamable_http_url": streamable_http_url,
            "mcp_url": streamable_http_url,
            "sse_url": sse_url,
            "endpoint_prefix": item.endpoint_prefix,
            "tools": tools.get(item.toolset, []),
        })
    return cards


def _normalize_transport(value: Any) -> str:
    transport = str(value or "streamable-http").strip().lower()
    if transport == "http":
        return "streamable-http"
    if transport == "sse":
        return "sse"
    return "streamable-http"
