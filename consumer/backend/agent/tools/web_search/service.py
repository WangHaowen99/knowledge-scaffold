"""Public web search service."""

from __future__ import annotations

import ipaddress
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

from config import WebSearchConfig
from utils.logging import get_logger
from agent.tools.web_search.base import WebSearchProviderError, WebSearchRequest
from agent.tools.web_search.provider_registry import get_provider_spec

logger = get_logger("web_search.service")

WebSearchEventEmitter = Callable[[Dict[str, str]], None]

_DOMAIN_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_FRESHNESS_VALUES = {"any", "day", "week", "month", "year", "d", "w", "m", "y"}


def query_web_search(
    *,
    query: str,
    max_results: Optional[int] = None,
    freshness: str = "any",
    include_domains: Optional[List[str]] = None,
    exclude_domains: Optional[List[str]] = None,
    event_emitter: Optional[WebSearchEventEmitter] = None,
    tool_call_id: Optional[str] = None,
) -> str:
    config = WebSearchConfig.from_env()
    normalized_query = str(query or "").strip()
    if not config.enabled:
        return _error_payload("web_search is disabled")
    if not normalized_query:
        return _error_payload("query cannot be empty")
    if config.provider != "tavily":
        return _error_payload("unsupported web_search provider: {}".format(config.provider))

    resolved_max_results, max_error = _resolve_max_results(max_results, config)
    if max_error:
        return _error_payload(max_error)

    normalized_freshness = str(freshness or "any").strip().lower()
    if normalized_freshness not in _FRESHNESS_VALUES:
        return _error_payload("freshness only supports any | day | week | month | year")

    domain_result = _resolve_domains(
        include_domains=include_domains,
        exclude_domains=exclude_domains,
        config=config,
    )
    if domain_result.get("error"):
        return _error_payload(str(domain_result["error"]))

    request = WebSearchRequest(
        query=normalized_query,
        max_results=resolved_max_results,
        freshness=normalized_freshness,
        include_domains=domain_result["include_domains"],
        exclude_domains=domain_result["exclude_domains"],
    )

    _emit(event_emitter, "start", "开始联网搜索", tool_call_id=tool_call_id)
    logger.info(
        "Sending web_search query: provider=%s query=%s max_results=%s freshness=%s tool_call_id=%s",
        config.provider,
        normalized_query[:100],
        resolved_max_results,
        normalized_freshness,
        tool_call_id or "n/a",
    )

    try:
        spec = get_provider_spec(config.provider)
        provider = spec.factory(config.to_provider_config())
        try:
            response = provider.search(request)
        finally:
            close = getattr(provider, "close", None)
            if callable(close):
                close()
    except WebSearchProviderError as exc:
        logger.warning("web_search provider failed: %s", str(exc))
        _emit(event_emitter, "error", str(exc), tool_call_id=tool_call_id)
        return _error_payload(str(exc))
    except Exception:
        logger.exception("Unexpected error during web_search")
        _emit(event_emitter, "error", "Unexpected error during web search", tool_call_id=tool_call_id)
        return _error_payload("Unexpected error during web search")

    result_payload = {
        "ok": True,
        "query": response.query,
        "provider": response.provider,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "results": [result.to_dict() for result in response.results],
    }
    if response.request_id:
        result_payload["request_id"] = response.request_id
    if response.response_time is not None:
        result_payload["response_time"] = response.response_time

    message = "联网搜索完成，返回 {} 条结果".format(len(response.results))
    _emit(event_emitter, "final", message, tool_call_id=tool_call_id)
    logger.info(
        "web_search completed: provider=%s result_count=%s tool_call_id=%s",
        response.provider,
        len(response.results),
        tool_call_id or "n/a",
    )
    return json.dumps(result_payload, ensure_ascii=False)


def _resolve_max_results(
    max_results: Optional[int],
    config: WebSearchConfig,
) -> tuple[int, Optional[str]]:
    if max_results is None:
        value = config.default_max_results
    else:
        try:
            value = int(max_results)
        except (TypeError, ValueError):
            return config.default_max_results, "max_results must be an integer"
    if value < 1:
        return config.default_max_results, "max_results must be >= 1"
    if value > config.max_results_limit:
        return config.default_max_results, "max_results must be <= {}".format(config.max_results_limit)
    return value, None


def _resolve_domains(
    *,
    include_domains: Optional[List[str]],
    exclude_domains: Optional[List[str]],
    config: WebSearchConfig,
) -> Dict[str, Any]:
    configured_allowed = _parse_domain_list(config.allowed_domains)
    configured_blocked = _parse_domain_list(config.blocked_domains)
    requested_include = _normalize_domain_list(include_domains or [])
    requested_exclude = _normalize_domain_list(exclude_domains or [])

    for domain in [*configured_allowed, *configured_blocked, *requested_include, *requested_exclude]:
        if not _is_valid_public_domain(domain):
            return {"error": "invalid domain for web_search: {}".format(domain)}

    if configured_allowed:
        outside = sorted(set(requested_include) - set(configured_allowed))
        if outside:
            return {
                "error": "include_domains must be within allowed_domains: {}".format(
                    ", ".join(outside)
                )
            }
        include = requested_include or configured_allowed
    else:
        include = requested_include

    exclude = sorted(set(configured_blocked) | set(requested_exclude))
    return {
        "include_domains": include,
        "exclude_domains": exclude,
    }


def _parse_domain_list(value: str) -> List[str]:
    return _normalize_domain_list(str(value or "").split(","))


def _normalize_domain_list(values: Iterable[Any]) -> List[str]:
    normalized: List[str] = []
    seen: set[str] = set()
    for value in values:
        domain = str(value or "").strip().lower()
        if not domain:
            continue
        domain = domain[4:] if domain.startswith("www.") else domain
        if domain in seen:
            continue
        seen.add(domain)
        normalized.append(domain)
    return normalized


def _is_valid_public_domain(domain: str) -> bool:
    has_invalid_shape = (
        not domain
        or "/" in domain
        or ":" in domain
        or domain.startswith(".")
        or domain.endswith(".")
    )
    if has_invalid_shape:
        return False
    if "." not in domain:
        return False
    if not _DOMAIN_PATTERN.match(domain):
        return False
    try:
        ipaddress.ip_address(domain)
        return False
    except ValueError:
        return True


def _emit(
    event_emitter: Optional[WebSearchEventEmitter],
    event_type: str,
    content: str,
    *,
    tool_call_id: Optional[str],
) -> None:
    del tool_call_id
    if not event_emitter:
        return
    try:
        event_emitter({"type": event_type, "content": content})
    except Exception:
        logger.warning("Failed to emit web_search event", exc_info=True)


def _error_payload(message: str) -> str:
    return json.dumps(
        {
            "ok": False,
            "status": "error",
            "error": message,
        },
        ensure_ascii=False,
    )
