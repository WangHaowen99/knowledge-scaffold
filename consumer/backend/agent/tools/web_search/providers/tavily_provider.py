# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Tavily public web search provider."""

from __future__ import annotations

from typing import Any, Dict
from urllib.parse import urlparse

import requests

from agent.tools.web_search.base import (
    WebSearchProviderError,
    WebSearchRequest,
    WebSearchResponse,
    WebSearchResult,
)


class TavilyWebSearchProvider:
    name = "tavily"

    def __init__(self, config: dict):
        self.base_url = str(config.get("base_url") or "https://api.tavily.com").rstrip("/")
        self.api_key = str(config.get("api_key") or "").strip()
        self.timeout_seconds = int(config.get("timeout_seconds") or 20)
        self.search_depth = str(config.get("search_depth") or "basic").strip().lower() or "basic"
        self._session = requests.Session()

    def search(self, request: WebSearchRequest) -> WebSearchResponse:
        if not self.api_key:
            raise WebSearchProviderError("Tavily API Key 未配置")

        payload: Dict[str, Any] = {
            "query": request.query,
            "max_results": request.max_results,
            "search_depth": self.search_depth,
            "include_answer": False,
            "include_raw_content": False,
        }
        time_range = _normalize_time_range(request.freshness)
        if time_range:
            payload["time_range"] = time_range
        if request.include_domains:
            payload["include_domains"] = request.include_domains
        if request.exclude_domains:
            payload["exclude_domains"] = request.exclude_domains

        try:
            response = self._session.post(
                "{}/search".format(self.base_url),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer {}".format(self.api_key),
                },
                json=payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.HTTPError as exc:
            raise WebSearchProviderError(_format_http_error(exc)) from exc
        except requests.exceptions.RequestException as exc:
            raise WebSearchProviderError("Tavily 请求失败: {}".format(str(exc))) from exc
        except ValueError as exc:
            raise WebSearchProviderError("Tavily 返回了无效 JSON") from exc

        raw_results = data.get("results") if isinstance(data, dict) else []
        results = [
            _normalize_result(item, index=index + 1)
            for index, item in enumerate(raw_results if isinstance(raw_results, list) else [])
            if isinstance(item, dict)
        ]
        return WebSearchResponse(
            query=str(data.get("query") or request.query),
            provider=self.name,
            results=results,
            request_id=str(data.get("request_id") or "").strip() or None,
            response_time=_coerce_float(data.get("response_time")),
        )

    def close(self) -> None:
        self._session.close()


def _normalize_result(item: Dict[str, Any], *, index: int) -> WebSearchResult:
    url = str(item.get("url") or "").strip()
    return WebSearchResult(
        rank=index,
        title=str(item.get("title") or "").strip(),
        url=url,
        domain=_extract_domain(url),
        snippet=_truncate(str(item.get("content") or item.get("snippet") or "").strip(), 1200),
        published_at=(
            str(item.get("published_date") or item.get("published_at") or "").strip()
            or None
        ),
        score=_coerce_float(item.get("score")),
    )


def _extract_domain(url: str) -> str:
    parsed = urlparse(url)
    return str(parsed.netloc or "").lower()


def _normalize_time_range(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"", "any", "all"}:
        return ""
    aliases = {"d": "day", "w": "week", "m": "month", "y": "year"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"day", "week", "month", "year"}:
        raise WebSearchProviderError("freshness 仅支持 any | day | week | month | year")
    return normalized


def _format_http_error(exc: requests.exceptions.HTTPError) -> str:
    response = exc.response
    status_code = response.status_code if response is not None else "unknown"
    detail = ""
    try:
        payload = response.json() if response is not None else {}
        if isinstance(payload, dict):
            error_payload = payload.get("error")
            if isinstance(error_payload, dict):
                detail = str(
                    error_payload.get("message")
                    or error_payload.get("detail")
                    or ""
                ).strip()
            else:
                detail = str(payload.get("message") or payload.get("detail") or error_payload or "").strip()
    except Exception:
        detail = str(getattr(response, "text", "") or "").strip()

    message = "Tavily 搜索失败（HTTP {}）".format(status_code)
    if detail:
        message = "{}: {}".format(message, _truncate(detail, 240))
    return message


def _coerce_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return "{}...".format(value[:limit])
