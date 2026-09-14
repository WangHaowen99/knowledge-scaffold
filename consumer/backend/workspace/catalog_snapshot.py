"""Immutable, generation-based workspace catalog snapshots."""

from __future__ import annotations

import logging
import stat
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from typing import Any


logger = logging.getLogger(__name__)


def _is_link_or_junction(path: Path) -> bool:
    candidate = Path(path)
    if candidate.is_symlink():
        return True
    is_junction = getattr(candidate, "is_junction", None)
    return bool(is_junction is not None and is_junction())


def _reject_link_or_junction(path: Path, *, kind: str) -> None:
    if _is_link_or_junction(path):
        raise ValueError(
            "{} must not be a symlink or junction: {}".format(kind, path)
        )


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


copy_value_for_test = _copy_value


@dataclass(frozen=True, slots=True)
class WorkspaceFileSignature:
    """Stable file-system metadata used for out-of-process drift detection."""

    exists: bool
    modified_ns: int | None = None
    size: int | None = None
    inode: int | None = None
    device: int | None = None
    mode: int | None = None

    @classmethod
    def from_path(cls, path: Path) -> "WorkspaceFileSignature":
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
class WorkspaceSourceSignature:
    """Signature of settings, workspace directory, and metadata files."""

    runtime_root: str
    settings: WorkspaceFileSignature
    workspaces_root: WorkspaceFileSignature
    metadata: tuple[tuple[str, WorkspaceFileSignature], ...]

    @classmethod
    def from_root(cls, runtime_root: Path) -> "WorkspaceSourceSignature":
        root = Path(runtime_root).expanduser().resolve()
        workspaces_root = root / "workspaces"
        _reject_link_or_junction(workspaces_root, kind="workspaces root")
        workspaces_signature = WorkspaceFileSignature.from_path(workspaces_root)
        entries: list[tuple[str, WorkspaceFileSignature]] = []
        if workspaces_signature.exists:
            if not stat.S_ISDIR(workspaces_signature.mode or 0):
                raise ValueError(
                    "workspaces root is not a directory: {}".format(
                        workspaces_root
                    )
                )
            if workspaces_root.resolve(strict=True) != workspaces_root:
                raise ValueError(
                    "workspaces root escaped the trusted runtime root: {}".format(
                        workspaces_root
                    )
                )
            try:
                candidates = sorted(workspaces_root.iterdir(), key=lambda item: item.name)
            except FileNotFoundError:
                candidates = []
                workspaces_signature = WorkspaceFileSignature.from_path(workspaces_root)
            for entry in candidates:
                _reject_link_or_junction(entry, kind="workspace directory")
                try:
                    entry_metadata = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not stat.S_ISDIR(entry_metadata.st_mode):
                    continue
                if entry.parent != workspaces_root:
                    raise ValueError(
                        "workspace directory must be a direct child of the trusted "
                        "workspaces root: {}".format(entry)
                    )
                if entry.resolve(strict=True) != workspaces_root / entry.name:
                    raise ValueError(
                        "workspace directory escaped the trusted workspaces root: {}".format(
                            entry
                        )
                    )
                metadata_path = entry / "workspace.json"
                _reject_link_or_junction(metadata_path, kind="workspace metadata")
                metadata_signature = WorkspaceFileSignature.from_path(metadata_path)
                if metadata_signature.exists:
                    if not stat.S_ISREG(metadata_signature.mode or 0):
                        raise ValueError(
                            "workspace metadata is not a regular file: {}".format(
                                metadata_path
                            )
                        )
                    if metadata_path.resolve(strict=True) != entry / "workspace.json":
                        raise ValueError(
                            "workspace metadata escaped its trusted workspace directory: {}".format(
                                metadata_path
                            )
                        )
                    entries.append((entry.name, metadata_signature))
        return cls(
            runtime_root=str(root),
            settings=WorkspaceFileSignature.from_path(
                root / "workspace-settings.json"
            ),
            workspaces_root=workspaces_signature,
            metadata=tuple(entries),
        )


@dataclass(frozen=True, slots=True)
class WorkspaceCatalogData:
    """Validated mutable-load result before immutable publication."""

    source_signature: WorkspaceSourceSignature
    default_workspace_id: str | None
    items: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class WorkspaceCatalogSnapshot:
    """One immutable workspace catalog publication."""

    generation: int
    source_signature: WorkspaceSourceSignature
    default_workspace_id: str | None
    ordered_ids: tuple[str, ...]
    items_by_id: Mapping[str, Mapping[str, Any]] = field(repr=False)

    def __post_init__(self) -> None:
        ordered_ids = tuple(self.ordered_ids)
        if len(set(ordered_ids)) != len(ordered_ids):
            raise ValueError("workspace catalog contains duplicate ids")
        frozen_items: dict[str, Mapping[str, Any]] = {}
        for workspace_id, item in self.items_by_id.items():
            normalized_id = str(workspace_id)
            if str(item.get("id")) != normalized_id:
                raise ValueError("workspace catalog item id does not match its key")
            frozen_items[normalized_id] = _freeze_value(item)
        if set(frozen_items) != set(ordered_ids):
            raise ValueError("workspace catalog order and item keys do not match")
        if (
            self.default_workspace_id is not None
            and self.default_workspace_id not in frozen_items
        ):
            raise ValueError("default workspace is absent from catalog")
        object.__setattr__(self, "ordered_ids", ordered_ids)
        object.__setattr__(self, "items_by_id", MappingProxyType(frozen_items))

    @classmethod
    def from_data(
        cls,
        *,
        generation: int,
        data: WorkspaceCatalogData,
    ) -> "WorkspaceCatalogSnapshot":
        items_by_id: dict[str, Mapping[str, Any]] = {}
        ordered_ids: list[str] = []
        for item in data.items:
            workspace_id = str(item["id"])
            if workspace_id in items_by_id:
                raise ValueError("workspace catalog contains duplicate ids")
            ordered_ids.append(workspace_id)
            items_by_id[workspace_id] = item
        return cls(
            generation=generation,
            source_signature=data.source_signature,
            default_workspace_id=data.default_workspace_id,
            ordered_ids=tuple(ordered_ids),
            items_by_id=items_by_id,
        )

    def contains(self, workspace_id: str) -> bool:
        return workspace_id in self.items_by_id

    def copy_item(self, workspace_id: str) -> dict[str, Any]:
        return _copy_value(self.items_by_id[workspace_id])

    def copy_items(self) -> list[dict[str, Any]]:
        return [self.copy_item(workspace_id) for workspace_id in self.ordered_ids]


class WorkspaceCatalogManager:
    """Publish one validated catalog per monotonic generation."""

    def __init__(
        self,
        *,
        loader: Callable[[], WorkspaceCatalogData],
        signature_loader: Callable[[], WorkspaceSourceSignature],
        initial_generation: int = 0,
        check_interval_seconds: float = 1.5,
        clock: Callable[[], float] | None = None,
        lock: RLock | None = None,
        is_security_error: Callable[[Exception], bool] | None = None,
        is_transient_error: Callable[[Exception], bool] | None = None,
    ) -> None:
        if check_interval_seconds < 0:
            raise ValueError("check_interval_seconds must be non-negative")
        if (
            not isinstance(initial_generation, int)
            or isinstance(initial_generation, bool)
            or initial_generation < 0
        ):
            raise ValueError("initial_generation must be a non-negative integer")
        self._loader = loader
        self._signature_loader = signature_loader
        self._generation = initial_generation
        self._check_interval_seconds = float(check_interval_seconds)
        self._clock = clock or time.monotonic
        self._lock = lock or RLock()
        self._is_security_error = is_security_error or (lambda _exc: False)
        self._is_transient_error = is_transient_error or (lambda _exc: False)
        self._snapshot: WorkspaceCatalogSnapshot | None = None
        self._next_signature_check_at = 0.0

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def snapshot_for_test(self) -> WorkspaceCatalogSnapshot | None:
        return self._snapshot

    def generation_after_drift_check(self) -> int:
        """Refresh a published snapshot when its signature check is due."""
        with self._lock:
            if self._snapshot is None:
                return self._generation
            return self.get_snapshot().generation

    def invalidate(self) -> int:
        with self._lock:
            self._generation += 1
            self._snapshot = None
            self._next_signature_check_at = 0.0
            return self._generation

    def get_snapshot(self) -> WorkspaceCatalogSnapshot:
        started = time.perf_counter()
        result = "error"
        snapshot: WorkspaceCatalogSnapshot | None = None
        try:
            with self._lock:
                snapshot, result = self._get_snapshot_locked()
            return snapshot
        finally:
            _record_workspace_catalog_access(result, started, snapshot)

    def _get_snapshot_locked(self) -> tuple[WorkspaceCatalogSnapshot, str]:
        now = self._clock()
        if self._snapshot is None:
            return self._load_locked(now, previous=None, advance=False)
        if now < self._next_signature_check_at:
            return self._snapshot, "hit"

        previous = self._snapshot
        self._next_signature_check_at = now + self._check_interval_seconds
        try:
            signature = self._signature_loader()
        except Exception as exc:
            return self._handle_failure_locked(exc, previous)
        if signature == previous.source_signature:
            return previous, "hit"
        return self._load_locked(now, previous=previous, advance=True)

    def _load_locked(
        self,
        now: float,
        *,
        previous: WorkspaceCatalogSnapshot | None,
        advance: bool,
    ) -> tuple[WorkspaceCatalogSnapshot, str]:
        try:
            data = self._loader()
            next_generation = self._generation + (1 if advance else 0)
            snapshot = WorkspaceCatalogSnapshot.from_data(
                generation=next_generation,
                data=data,
            )
        except Exception as exc:
            return self._handle_failure_locked(exc, previous)
        if advance:
            self._generation = next_generation
        self._snapshot = snapshot
        self._next_signature_check_at = now + self._check_interval_seconds
        return snapshot, "miss"

    def _handle_failure_locked(
        self,
        exc: Exception,
        previous: WorkspaceCatalogSnapshot | None,
    ) -> tuple[WorkspaceCatalogSnapshot, str]:
        if (
            previous is not None
            and not self._is_security_error(exc)
            and self._is_transient_error(exc)
        ):
            return previous, "stale"
        if previous is not None:
            self._generation += 1
        self._snapshot = None
        self._next_signature_check_at = 0.0
        raise exc


def _record_workspace_catalog_access(
    result: str,
    started: float,
    snapshot: WorkspaceCatalogSnapshot | None,
) -> None:
    workspace_id = ""
    if snapshot is not None:
        workspace_id = str(snapshot.default_workspace_id or "")
        if not workspace_id and snapshot.ordered_ids:
            workspace_id = snapshot.ordered_ids[0]
    try:
        from integration.observability.performance import record_cache_access

        record_cache_access(
            "workspace_catalog",
            result,
            max(0.0, (time.perf_counter() - started) * 1000),
            workspace_id=workspace_id,
        )
    except Exception:
        logger.debug("Workspace catalog metric recording failed", exc_info=True)
