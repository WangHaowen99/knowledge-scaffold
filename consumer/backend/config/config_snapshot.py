"""Immutable, generation-based snapshots for effective configuration."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from typing import Any


logger = logging.getLogger(__name__)


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {deepcopy(key): _freeze_value(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze_value(item) for item in value)
    return deepcopy(value)


def _copy_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {deepcopy(key): _copy_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_copy_value(item) for item in value]
    if isinstance(value, frozenset):
        return {_copy_value(item) for item in value}
    return deepcopy(value)


@dataclass(frozen=True, slots=True)
class ConfigSourceSignature:
    """Metadata used to detect an out-of-process config replacement."""

    exists: bool
    modified_ns: int | None = None
    size: int | None = None
    inode: int | None = None
    device: int | None = None
    mode: int | None = None

    @classmethod
    def from_path(cls, path: Path) -> "ConfigSourceSignature":
        try:
            metadata = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            return cls(exists=False)
        return cls(
            exists=True,
            modified_ns=metadata.st_mtime_ns,
            size=metadata.st_size,
            inode=metadata.st_ino,
            device=metadata.st_dev,
            mode=metadata.st_mode,
        )


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    """One immutable effective config publication."""

    generation: int
    source_signature: ConfigSourceSignature
    value: Any = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _freeze_value(self.value))

    def copy_value(self) -> Any:
        return _copy_value(self.value)

    def copy_section(self, section: str) -> Any:
        if not isinstance(self.value, Mapping):
            raise TypeError("snapshot value does not contain named sections")
        return _copy_value(self.value[section])


class ConfigSnapshotManager:
    """Publish one validated config value per monotonic generation."""

    def __init__(
        self,
        *,
        source_path: Path,
        loader: Callable[[Mapping[str, str]], Any],
        environment: Mapping[str, str] | None = None,
        initial_generation: int = 0,
        check_interval_seconds: float = 1.5,
        clock: Callable[[], float] = time.monotonic,
        is_security_error: Callable[[Exception], bool] | None = None,
        is_transient_error: Callable[[Exception], bool] | None = None,
        signature_loader: Callable[[Path], ConfigSourceSignature] = ConfigSourceSignature.from_path,
    ) -> None:
        if check_interval_seconds < 0:
            raise ValueError("check_interval_seconds must be non-negative")
        if (
            not isinstance(initial_generation, int)
            or isinstance(initial_generation, bool)
            or initial_generation < 0
        ):
            raise ValueError("initial_generation must be a non-negative integer")
        self._source_path = Path(source_path)
        self._loader = loader
        self._environment = MappingProxyType(
            dict(os.environ if environment is None else environment)
        )
        self._check_interval_seconds = float(check_interval_seconds)
        self._clock = clock
        self._is_security_error = is_security_error or (lambda _exc: False)
        self._is_transient_error = is_transient_error or (lambda _exc: False)
        self._signature_loader = signature_loader
        self._lock = RLock()
        self._generation = initial_generation
        self._snapshot: ConfigSnapshot | None = None
        self._next_signature_check_at = 0.0

    @property
    def source_path(self) -> Path:
        return self._source_path

    @property
    def check_interval_seconds(self) -> float:
        return self._check_interval_seconds

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def invalidate(self) -> int:
        with self._lock:
            self._generation += 1
            self._snapshot = None
            self._next_signature_check_at = 0.0
            return self._generation

    def load(self) -> Any:
        started = time.perf_counter()
        result = "error"
        try:
            with self._lock:
                snapshot, result = self._refresh_locked()
            return snapshot.copy_value()
        finally:
            _record_system_config_access(result, started)

    def load_section(self, section: str) -> Any:
        started = time.perf_counter()
        result = "error"
        try:
            with self._lock:
                snapshot, result = self._refresh_locked()
            return snapshot.copy_section(section)
        finally:
            _record_system_config_access(result, started)

    def refresh_generation(self) -> int:
        started = time.perf_counter()
        result = "error"
        try:
            with self._lock:
                _snapshot, result = self._refresh_locked()
                return self._generation
        finally:
            _record_system_config_access(result, started)

    def _refresh_locked(self) -> tuple[ConfigSnapshot, str]:
        now = self._clock()
        if self._snapshot is None:
            return self._load_current_generation(now), "miss"

        if now < self._next_signature_check_at:
            return self._snapshot, "hit"

        self._next_signature_check_at = now + self._check_interval_seconds
        try:
            source_signature = self._signature_loader(self._source_path)
        except Exception as exc:
            if self._is_security_error(exc):
                self._snapshot = None
                raise
            if self._is_transient_error(exc):
                return self._snapshot, "stale"
            self._next_signature_check_at = now
            raise

        if source_signature == self._snapshot.source_signature:
            return self._snapshot, "hit"

        previous_snapshot = self._snapshot
        try:
            value = self._loader(self._environment)
        except Exception as exc:
            if self._is_security_error(exc):
                self._snapshot = None
                raise
            if self._is_transient_error(exc):
                return previous_snapshot, "stale"
            self._next_signature_check_at = now
            raise

        self._generation += 1
        self._snapshot = ConfigSnapshot(
            generation=self._generation,
            source_signature=source_signature,
            value=value,
        )
        return self._snapshot, "miss"

    def _load_current_generation(self, now: float) -> ConfigSnapshot:
        source_signature = self._signature_loader(self._source_path)
        value = self._loader(self._environment)
        self._snapshot = ConfigSnapshot(
            generation=self._generation,
            source_signature=source_signature,
            value=value,
        )
        self._next_signature_check_at = now + self._check_interval_seconds
        return self._snapshot


def _record_system_config_access(result: str, started: float) -> None:
    try:
        from integration.observability.performance import record_cache_access

        record_cache_access(
            "system_config",
            result,
            max(0.0, (time.perf_counter() - started) * 1000),
        )
    except Exception:
        logger.debug("System config cache metric recording failed", exc_info=True)
