"""Central registry for ontology search providers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Type

from .base import OntologySearchProvider
from .providers.mindsdb_provider import MindsDBOntologySearchProvider
from .providers.nl2sql_http_provider import NL2SQLHttpProvider
from .providers.nl2sql_template import NL2SQLTemplateProvider


@dataclass(frozen=True)
class OntologySearchProviderSpec:
    name: str
    config_section: str
    factory: Type[OntologySearchProvider]


PROVIDER_SPECS: Dict[str, OntologySearchProviderSpec] = {
    "mindsdb": OntologySearchProviderSpec(
        name="mindsdb",
        config_section="mindsdb",
        factory=MindsDBOntologySearchProvider,
    ),
    "nl2sql_http": OntologySearchProviderSpec(
        name="nl2sql_http",
        config_section="nl2sql_http",
        factory=NL2SQLHttpProvider,
    ),
    "nl2sql_template": OntologySearchProviderSpec(
        name="nl2sql_template",
        config_section="nl2sql_template",
        factory=NL2SQLTemplateProvider,
    ),
}

SUPPORTED_PROVIDER_NAMES = frozenset(PROVIDER_SPECS)


def get_provider_spec(provider_name: str) -> OntologySearchProviderSpec:
    normalized_name = normalize_provider_name(provider_name)
    if not normalized_name:
        raise ValueError("ontology_search.provider is not configured")
    provider_spec = PROVIDER_SPECS.get(normalized_name)
    if provider_spec is None:
        supported = ", ".join(sorted(PROVIDER_SPECS))
        raise ValueError(
            "Unsupported ontology_search provider '{}'. Supported providers: {}".format(
                normalized_name,
                supported,
            )
        )
    return provider_spec


def normalize_provider_name(provider_name: str | None) -> str:
    return str(provider_name or "").strip().lower()


def configured_provider_name(config: dict) -> str:
    ontology_search = dict(config.get("ontology_search") or {})
    return normalize_provider_name(ontology_search.get("provider"))


def apply_runtime_overrides(
    config: dict,
    *,
    api_url: str | None = None,
    timeout: int | None = None,
) -> dict:
    resolved = {section: dict(values or {}) for section, values in config.items()}
    provider_spec = get_provider_spec(configured_provider_name(resolved))
    resolved.setdefault("ontology_search", {})
    os_config = resolved["ontology_search"]
    os_config.setdefault("provider", provider_spec.name)
    os_config.setdefault(provider_spec.config_section, {})

    if api_url:
        os_config[provider_spec.config_section]["url"] = str(api_url).rstrip("/")
    if timeout is not None:
        os_config[provider_spec.config_section]["timeout_seconds"] = int(timeout)

    for provider in PROVIDER_SPECS.values():
        section = os_config.get(provider.config_section)
        if isinstance(section, dict) and "url" in section:
            section["url"] = str(section.get("url") or "").rstrip("/")

    return resolved
