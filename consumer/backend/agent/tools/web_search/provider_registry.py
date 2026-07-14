"""Provider registry for public web search."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Type

from agent.tools.web_search.base import WebSearchProvider, WebSearchProviderError
from agent.tools.web_search.providers.tavily_provider import TavilyWebSearchProvider


@dataclass(frozen=True)
class WebSearchProviderSpec:
    name: str
    label: str
    factory: Type[WebSearchProvider]


PROVIDER_SPECS: Dict[str, WebSearchProviderSpec] = {
    "tavily": WebSearchProviderSpec(
        name="tavily",
        label="Tavily",
        factory=TavilyWebSearchProvider,
    ),
}


def normalize_provider_name(provider: object) -> str:
    return str(provider or "").strip().lower()


def get_provider_spec(provider: object) -> WebSearchProviderSpec:
    provider_name = normalize_provider_name(provider)
    if not provider_name:
        raise WebSearchProviderError("web_search.provider 不能为空")
    spec = PROVIDER_SPECS.get(provider_name)
    if spec is None:
        raise WebSearchProviderError(
            "web_search.provider 仅支持 {}".format(" | ".join(sorted(PROVIDER_SPECS)))
        )
    return spec
