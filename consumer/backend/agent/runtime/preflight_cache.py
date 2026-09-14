# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Bounded single-flight cache for immutable run preflight products."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import os
import threading
from types import MappingProxyType
from typing import Any, Generic, TypeVar


T = TypeVar("T")


def _non_empty_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    return value.strip()


def _generation(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class PreflightCacheKey:
    workspace_id: str
    workspace_config_generation: int
    system_config_generation: int
    ontology_id: str
    ontology_version: str
    mutation_revision: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "workspace_id",
            _non_empty_text(self.workspace_id, "workspace_id"),
        )
        object.__setattr__(
            self,
            "workspace_config_generation",
            _generation(
                self.workspace_config_generation,
                "workspace_config_generation",
            ),
        )
        object.__setattr__(
            self,
            "system_config_generation",
            _generation(self.system_config_generation, "system_config_generation"),
        )
        object.__setattr__(
            self,
            "ontology_id",
            _text(self.ontology_id, "ontology_id"),
        )
        object.__setattr__(
            self,
            "ontology_version",
            _text(self.ontology_version, "ontology_version"),
        )
        object.__setattr__(
            self,
            "mutation_revision",
            _generation(self.mutation_revision, "mutation_revision"),
        )


def freeze_preflight_product(value: Any) -> Any:
    if isinstance(value, MappingProxyType):
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("preflight product mapping keys must be strings")
        return MappingProxyType(
            {key: freeze_preflight_product(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(freeze_preflight_product(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(freeze_preflight_product(item) for item in value)
    if isinstance(value, (str, int, float, bool, bytes)) or value is None:
        return value
    raise TypeError(
        "preflight products must contain only immutable structured values"
    )


def materialize_preflight_product(value: Any) -> Any:
    """Detach a frozen cache product into pickle-safe run-local containers."""

    if isinstance(value, Mapping):
        return {
            str(key): materialize_preflight_product(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(materialize_preflight_product(item) for item in value)
    if isinstance(value, list):
        return [materialize_preflight_product(item) for item in value]
    if isinstance(value, frozenset):
        return frozenset(materialize_preflight_product(item) for item in value)
    return value


def preflight_cache_enabled() -> bool:
    value = str(os.getenv("PERF_RUN_PREFLIGHT_CACHE_ENABLED", "true") or "")
    return value.strip().lower() not in {"0", "false", "no", "off"}


@dataclass(slots=True)
class _Flight(Generic[T]):
    event: threading.Event = field(default_factory=threading.Event)
    result: T | None = None
    error: BaseException | None = None


class ImmutablePreflightCache:
    def __init__(self, *, max_entries: int | None = None) -> None:
        resolved_max = (
            int(os.getenv("PERF_RUN_PREFLIGHT_CACHE_MAX_ENTRIES", "128"))
            if max_entries is None
            else int(max_entries)
        )
        if resolved_max < 1:
            raise ValueError("max_entries must be positive")
        self._max_entries = resolved_max
        self._entries: OrderedDict[PreflightCacheKey, Any] = OrderedDict()
        self._flights: dict[PreflightCacheKey, _Flight[Any]] = {}
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0
        self._loads = 0
        self._waiters = 0
        self._evictions = 0

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._entries)

    def get_or_load(
        self,
        key: PreflightCacheKey,
        loader: Callable[[], T],
    ) -> T:
        if not isinstance(key, PreflightCacheKey):
            raise TypeError("key must be a PreflightCacheKey")
        if not callable(loader):
            raise TypeError("loader must be callable")

        if not preflight_cache_enabled():
            with self._lock:
                self._misses += 1
                self._loads += 1
            return freeze_preflight_product(loader())

        with self._lock:
            if key in self._entries:
                self._hits += 1
                product = self._entries.pop(key)
                self._entries[key] = product
                return product
            flight = self._flights.get(key)
            leader = flight is None
            if leader:
                flight = _Flight()
                self._flights[key] = flight
                self._misses += 1
                self._loads += 1
            else:
                self._waiters += 1

        if not leader:
            flight.event.wait()
            if flight.error is not None:
                raise flight.error
            return flight.result

        try:
            product = freeze_preflight_product(loader())
        except BaseException as exc:
            with self._lock:
                self._flights.pop(key, None)
                flight.error = exc
                flight.event.set()
            raise

        with self._lock:
            self._entries[key] = product
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
                self._evictions += 1
            self._flights.pop(key, None)
            flight.result = product
            flight.event.set()
        return product

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def metrics(self) -> dict[str, int]:
        with self._lock:
            return {
                "size": len(self._entries),
                "hits": self._hits,
                "misses": self._misses,
                "loads": self._loads,
                "waiters": self._waiters,
                "evictions": self._evictions,
            }
