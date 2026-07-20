"""Shared web search provider types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol


class WebSearchProviderError(ValueError):
    """Raised when a web search provider cannot complete a request."""


@dataclass(frozen=True)
class WebSearchRequest:
    query: str
    max_results: int
    freshness: str = "any"
    include_domains: List[str] = field(default_factory=list)
    exclude_domains: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class WebSearchResult:
    rank: int
    title: str
    url: str
    domain: str
    snippet: str
    published_at: Optional[str] = None
    score: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "rank": self.rank,
            "title": self.title,
            "url": self.url,
            "domain": self.domain,
            "snippet": self.snippet,
        }
        if self.published_at:
            payload["published_at"] = self.published_at
        if self.score is not None:
            payload["score"] = self.score
        return payload


@dataclass(frozen=True)
class WebSearchResponse:
    query: str
    provider: str
    results: List[WebSearchResult]
    request_id: Optional[str] = None
    response_time: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "query": self.query,
            "provider": self.provider,
            "results": [result.to_dict() for result in self.results],
        }
        if self.request_id:
            payload["request_id"] = self.request_id
        if self.response_time is not None:
            payload["response_time"] = self.response_time
        return payload


class WebSearchProvider(Protocol):
    name: str

    def search(self, request: WebSearchRequest) -> WebSearchResponse:
        """Execute a normalized public web search request."""
