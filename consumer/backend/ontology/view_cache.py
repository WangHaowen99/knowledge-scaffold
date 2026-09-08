"""Revisioned, workspace-scoped caches for ontology read views."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import logging
import os
from pathlib import Path
import stat
import shutil
import tempfile
import threading
import time
from typing import Any, Callable

from ontology.package_browser import EXCLUDED_DIRECTORY_NAMES


_DEFAULT_MAX_ENTRIES = 128
_MAX_TREE_BUILD_ATTEMPTS = 3
_CONTENT_READ_CHUNK_BYTES = 1024 * 1024
_INCLUDED_HIDDEN_CONTROL_FILES = frozenset({".deterministic-lock.json"})
logger = logging.getLogger(__name__)


class OntologyPackageRevisionError(RuntimeError):
    """The visible executable package cannot be revisioned safely."""


@dataclass
class _TreeEntry:
    value: list[dict[str, Any]]
    source_signature: object | None
    next_signature_check_at: float


def _tree_entry_matches_signature(
    entry: _TreeEntry | None,
    *,
    signature_available: bool,
    signature_loader: Callable[[], object] | None,
    source_signature: object | None,
) -> bool:
    if entry is None:
        return False
    if not signature_available:
        return False
    if signature_loader is None:
        return False
    return entry.source_signature == source_signature


@dataclass
class _TreeFlight:
    event: threading.Event = field(default_factory=threading.Event)
    value: list[dict[str, Any]] | None = None
    error: BaseException | None = None


@dataclass
class _RevisionSourceState:
    signature: object
    next_check_at: float


@dataclass
class _RevisionSourceFlight:
    event: threading.Event = field(default_factory=threading.Event)
    revision: int = 0
    error: BaseException | None = None


class OntologyViewCache:
    """Keep bounded package-tree views isolated by mutation revision."""

    def __init__(
        self,
        *,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
        check_interval_seconds: float = 1.5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lock = threading.RLock()
        self._max_entries = max(1, int(max_entries))
        self._check_interval_seconds = max(0.0, float(check_interval_seconds))
        self._clock = clock
        self._next_revision = 0
        self._workspace_revisions: dict[str, int] = {}
        self._ontology_revisions: dict[tuple[str, str], int] = {}
        self._trees: OrderedDict[
            tuple[str, str, str, int],
            _TreeEntry,
        ] = OrderedDict()
        self._flights: dict[tuple[str, str, str, int], _TreeFlight] = {}
        self._revision_sources: dict[
            tuple[str, str],
            _RevisionSourceState,
        ] = {}
        self._revision_source_flights: dict[
            tuple[str, str],
            _RevisionSourceFlight,
        ] = {}

    def get_revision(self, workspace_id: str, ontology_id: str) -> int:
        workspace, ontology = _normalized_scope(workspace_id, ontology_id)
        with self._lock:
            return max(
                self._workspace_revisions.get(workspace, 0),
                self._ontology_revisions.get((workspace, ontology), 0),
            )

    def invalidate(self, *, workspace_id: str, ontology_id: str | None = None) -> int:
        workspace = _normalized_value(workspace_id, "workspace_id")
        ontology = (
            _normalized_value(ontology_id, "ontology_id")
            if ontology_id is not None
            else None
        )
        with self._lock:
            self._next_revision += 1
            revision = self._next_revision
            if ontology is None:
                self._workspace_revisions[workspace] = revision
                for scope in tuple(self._revision_sources):
                    if scope[0] == workspace:
                        self._revision_sources.pop(scope, None)
            else:
                self._ontology_revisions[(workspace, ontology)] = revision
                self._revision_sources.pop((workspace, ontology), None)
            for key in list(self._trees):
                if key[0] == workspace and (ontology is None or key[1] == ontology):
                    self._trees.pop(key, None)
            return revision

    def observe_revision_source(
        self,
        *,
        workspace_id: str,
        ontology_id: str,
        signature_loader: Callable[[], object],
    ) -> int:
        """Throttle external package checks and publish a revision on drift."""

        workspace, ontology = _normalized_scope(workspace_id, ontology_id)
        scope = (workspace, ontology)
        with self._lock:
            revision = max(
                self._workspace_revisions.get(workspace, 0),
                self._ontology_revisions.get(scope, 0),
            )
            state = self._revision_sources.get(scope)
            if state is not None and self._clock() < state.next_check_at:
                return revision
            flight = self._revision_source_flights.get(scope)
            owner = flight is None
            if flight is None:
                flight = _RevisionSourceFlight(revision=revision)
                self._revision_source_flights[scope] = flight

        if not owner:
            flight.event.wait()
            if flight.error is not None:
                raise flight.error
            return flight.revision

        try:
            signature = signature_loader()
        except BaseException as exc:
            with self._lock:
                flight.error = exc
                self._revision_source_flights.pop(scope, None)
                flight.event.set()
            raise

        with self._lock:
            previous = self._revision_sources.get(scope)
            if previous is not None and previous.signature != signature:
                self._next_revision += 1
                revision = self._next_revision
                self._ontology_revisions[scope] = revision
                for key in tuple(self._trees):
                    if key[0] == workspace and key[1] == ontology:
                        self._trees.pop(key, None)
            else:
                revision = max(
                    self._workspace_revisions.get(workspace, 0),
                    self._ontology_revisions.get(scope, 0),
                )
            self._revision_sources[scope] = _RevisionSourceState(
                signature=signature,
                next_check_at=self._clock() + self._check_interval_seconds,
            )
            flight.revision = revision
            self._revision_source_flights.pop(scope, None)
            flight.event.set()
            return revision

    def get_package_tree(
        self,
        *,
        workspace_id: str,
        ontology_id: str,
        version: str,
        loader: Callable[[], list[dict[str, Any]]],
        signature_loader: Callable[[], object] | None = None,
    ) -> list[dict[str, Any]]:
        workspace, ontology = _normalized_scope(workspace_id, ontology_id)
        resolved_version = _normalized_value(version, "version")
        started = time.perf_counter()
        immediate_value: list[dict[str, Any]] | None = None
        with self._lock:
            revision = max(
                self._workspace_revisions.get(workspace, 0),
                self._ontology_revisions.get((workspace, ontology), 0),
            )
            key = (workspace, ontology, resolved_version, revision)
            entry = self._trees.get(key)
            if entry is not None and (
                signature_loader is None
                or self._clock() < entry.next_signature_check_at
            ):
                self._trees.move_to_end(key)
                immediate_value = deepcopy(entry.value)
            else:
                flight = self._flights.get(key)
                owner = flight is None
                if flight is None:
                    flight = _TreeFlight()
                    self._flights[key] = flight

        if immediate_value is not None:
            _record_ontology_view_access("hit", started, workspace)
            return immediate_value

        if not owner:
            flight.event.wait()
            if flight.error is not None:
                _record_ontology_view_access("error", started, workspace)
                raise flight.error
            _record_ontology_view_access("hit", started, workspace)
            return deepcopy(flight.value or [])

        result = "miss"
        try:
            source_signature = None
            signature_available = True
            if signature_loader is not None:
                try:
                    source_signature = signature_loader()
                except Exception:
                    signature_available = False
            with self._lock:
                current_entry = self._trees.get(key)
            if _tree_entry_matches_signature(
                current_entry,
                signature_available=signature_available,
                signature_loader=signature_loader,
                source_signature=source_signature,
            ):
                cached_value = current_entry.value
                source_signature = current_entry.source_signature
                result = "hit"
            else:
                if signature_loader is None:
                    loaded = loader()
                    if not isinstance(loaded, list):
                        raise TypeError("package tree loader must return a list")
                    cached_value = deepcopy(loaded)
                elif not signature_available:
                    # Still execute the trusted loader so path/symlink validation
                    # errors are never hidden by an advisory signature failure.
                    loaded = loader()
                    if not isinstance(loaded, list):
                        raise TypeError("package tree loader must return a list")
                    if current_entry is None:
                        raise OntologyPackageRevisionError(
                            "package tree source signature is unavailable"
                        )
                    cached_value = current_entry.value
                    source_signature = current_entry.source_signature
                    result = "stale"
                else:
                    before_signature = source_signature
                    for attempt in range(_MAX_TREE_BUILD_ATTEMPTS):
                        loaded = loader()
                        if not isinstance(loaded, list):
                            raise TypeError("package tree loader must return a list")
                        try:
                            after_signature = signature_loader()
                        except Exception as exc:
                            raise OntologyPackageRevisionError(
                                "package tree source signature is unavailable"
                            ) from exc
                        if before_signature == after_signature:
                            source_signature = after_signature
                            cached_value = deepcopy(loaded)
                            break
                        if attempt + 1 >= _MAX_TREE_BUILD_ATTEMPTS:
                            raise OntologyPackageRevisionError(
                                "ontology package changed during package tree build"
                            )
                        before_signature = after_signature
        except BaseException as exc:
            with self._lock:
                flight.error = exc
                self._flights.pop(key, None)
                flight.event.set()
            _record_ontology_view_access("error", started, workspace)
            raise

        with self._lock:
            self._trees[key] = _TreeEntry(
                value=cached_value,
                source_signature=source_signature,
                next_signature_check_at=(
                    self._clock() + self._check_interval_seconds
                ),
            )
            self._trees.move_to_end(key)
            while len(self._trees) > self._max_entries:
                self._trees.popitem(last=False)
            flight.value = cached_value
            self._flights.pop(key, None)
            flight.event.set()
        _record_ontology_view_access(result, started, workspace)
        return deepcopy(cached_value)


def get_ontology_mutation_revision(workspace_id: str, ontology_id: str) -> int:
    return _view_cache.get_revision(workspace_id, ontology_id)


def invalidate_ontology_views(
    *,
    workspace_id: str,
    ontology_id: str | None = None,
) -> int:
    return _view_cache.invalidate(
        workspace_id=workspace_id,
        ontology_id=ontology_id,
    )


def observe_ontology_source_revision(
    *,
    workspace_id: str,
    ontology_id: str,
    ontology_root: Path,
) -> int:
    return _view_cache.observe_revision_source(
        workspace_id=workspace_id,
        ontology_id=ontology_id,
        signature_loader=lambda: build_package_revision_source_signature(
            ontology_root
        ),
    )


def get_package_tree_cached(
    *,
    workspace_id: str,
    ontology_id: str,
    version: str,
    loader: Callable[[], list[dict[str, Any]]],
    signature_loader: Callable[[], object] | None = None,
) -> list[dict[str, Any]]:
    if not _view_cache_enabled():
        started = time.perf_counter()
        result = "error"
        try:
            value = deepcopy(loader())
            result = "bypass"
            return value
        finally:
            _record_ontology_view_access(result, started, workspace_id)
    return _view_cache.get_package_tree(
        workspace_id=workspace_id,
        ontology_id=ontology_id,
        version=version,
        loader=loader,
        signature_loader=signature_loader,
    )


def build_package_tree_source_signature(root: Path) -> tuple[object, ...]:
    """Return the visible package-tree shape without following symlinks."""
    package_root = Path(root)
    rows: list[tuple[object, ...]] = []

    def visit(path: Path, *, is_root: bool = False) -> None:
        stat_result = path.lstat()
        if not is_root:
            if path.name.startswith("."):
                return
            if path.name.lower() in EXCLUDED_DIRECTORY_NAMES:
                return
            if stat.S_ISLNK(stat_result.st_mode):
                return
        if not stat.S_ISDIR(stat_result.st_mode):
            if not is_root and stat.S_ISREG(stat_result.st_mode):
                rows.append(
                    (
                        "file",
                        path.relative_to(package_root).as_posix(),
                        int(stat_result.st_size),
                    )
                )
            return
        if not is_root:
            rows.append(("dir", path.relative_to(package_root).as_posix()))
        for child in sorted(path.iterdir(), key=lambda item: item.name.lower()):
            visit(child)

    visit(package_root, is_root=True)
    return tuple(rows)


def build_package_revision_source_signature(root: Path) -> tuple[object, ...]:
    """Return a metadata signature for immutable package-definition files."""

    package_root = Path(root)
    try:
        root_stat = package_root.lstat()
    except OSError:
        return (("missing", ""),)
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        return (
            (
                "unsafe-root",
                "",
                int(root_stat.st_mtime_ns),
                int(root_stat.st_size),
            ),
        )

    rows: list[tuple[object, ...]] = []

    def visit(path: Path) -> None:
        for child in sorted(path.iterdir(), key=lambda item: item.name.lower()):
            child_stat = child.lstat()
            relative = child.relative_to(package_root).as_posix()
            if (
                (
                    child.name.startswith(".")
                    and child.name not in _INCLUDED_HIDDEN_CONTROL_FILES
                )
                or child.name.lower() in EXCLUDED_DIRECTORY_NAMES
            ):
                continue
            if stat.S_ISLNK(child_stat.st_mode):
                rows.append(
                    (
                        "symlink",
                        relative,
                        int(child_stat.st_mtime_ns),
                        int(child_stat.st_size),
                    )
                )
            elif stat.S_ISDIR(child_stat.st_mode):
                rows.append(("dir", relative))
                visit(child)
            elif stat.S_ISREG(child_stat.st_mode):
                rows.append(
                    (
                        "file",
                        relative,
                        int(child_stat.st_mtime_ns),
                        int(child_stat.st_size),
                    )
                )
            else:
                rows.append(
                    (
                        "unsafe",
                        relative,
                        int(child_stat.st_mtime_ns),
                        int(child_stat.st_size),
                    )
                )

    visit(package_root)
    return tuple(rows)


def build_package_content_revision(root: Path) -> str:
    """Hash visible package bytes plus the platform deterministic lock."""

    package_root = Path(root)
    try:
        root_stat = package_root.lstat()
    except OSError as exc:
        raise OntologyPackageRevisionError(
            "ontology package root is unavailable"
        ) from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise OntologyPackageRevisionError(
            "ontology package root must be a regular directory"
        )

    digest = hashlib.sha256()
    digest.update(b"ontology-package-content-v1\0")

    def add_field(value: bytes) -> None:
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)

    def visit(path: Path) -> None:
        try:
            children = sorted(
                path.iterdir(),
                key=lambda item: (item.name.casefold(), item.name),
            )
        except OSError as exc:
            raise OntologyPackageRevisionError(
                "ontology package directory cannot be inspected"
            ) from exc
        for child in children:
            if (
                (
                    child.name.startswith(".")
                    and child.name not in _INCLUDED_HIDDEN_CONTROL_FILES
                )
                or child.name.lower() in EXCLUDED_DIRECTORY_NAMES
            ):
                continue
            try:
                child_stat = child.lstat()
            except OSError as exc:
                raise OntologyPackageRevisionError(
                    "ontology package entry cannot be inspected"
                ) from exc
            relative = child.relative_to(package_root).as_posix().encode("utf-8")
            if stat.S_ISLNK(child_stat.st_mode):
                raise OntologyPackageRevisionError(
                    "symbolic links are not allowed in executable ontology packages"
                )
            if stat.S_ISDIR(child_stat.st_mode):
                digest.update(b"D")
                add_field(relative)
                visit(child)
                continue
            if not stat.S_ISREG(child_stat.st_mode):
                raise OntologyPackageRevisionError(
                    "special files are not allowed in executable ontology packages"
                )

            file_digest = hashlib.sha256()
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(child, flags, 0o600)
            except OSError as exc:
                raise OntologyPackageRevisionError(
                    "ontology package file cannot be opened safely"
                ) from exc
            try:
                opened_stat = os.fstat(descriptor)
                if not stat.S_ISREG(opened_stat.st_mode):
                    raise OntologyPackageRevisionError(
                        "ontology package entry changed while being read"
                    )
                while True:
                    chunk = os.read(descriptor, _CONTENT_READ_CHUNK_BYTES)
                    if not chunk:
                        break
                    file_digest.update(chunk)
            except OSError as exc:
                raise OntologyPackageRevisionError(
                    "ontology package file cannot be read safely"
                ) from exc
            finally:
                os.close(descriptor)
            try:
                final_stat = child.lstat()
            except OSError as exc:
                raise OntologyPackageRevisionError(
                    "ontology package entry changed while being read"
                ) from exc
            if stat.S_ISLNK(final_stat.st_mode) or not stat.S_ISREG(
                final_stat.st_mode
            ):
                raise OntologyPackageRevisionError(
                    "ontology package entry changed while being read"
                )
            digest.update(b"F")
            add_field(relative)
            add_field(int(opened_stat.st_size).to_bytes(8, "big"))
            add_field(file_digest.digest())

    visit(package_root)
    return "sha256:" + digest.hexdigest()


def _copy_visible_package(source_root: Path, staged_root: Path) -> None:
    def copy_directory(source: Path, destination: Path) -> None:
        try:
            children = sorted(
                source.iterdir(),
                key=lambda item: (item.name.casefold(), item.name),
            )
        except OSError as exc:
            raise OntologyPackageRevisionError(
                "ontology package directory cannot be copied safely"
            ) from exc
        for child in children:
            if (
                (
                    child.name.startswith(".")
                    and child.name not in _INCLUDED_HIDDEN_CONTROL_FILES
                )
                or child.name.lower() in EXCLUDED_DIRECTORY_NAMES
            ):
                continue
            try:
                child_stat = child.lstat()
            except OSError as exc:
                raise OntologyPackageRevisionError(
                    "ontology package entry cannot be copied safely"
                ) from exc
            target = destination / child.name
            if stat.S_ISLNK(child_stat.st_mode):
                raise OntologyPackageRevisionError(
                    "symbolic links are not allowed in executable ontology packages"
                )
            if stat.S_ISDIR(child_stat.st_mode):
                target.mkdir(mode=0o700)
                copy_directory(child, target)
                continue
            if not stat.S_ISREG(child_stat.st_mode):
                raise OntologyPackageRevisionError(
                    "special files are not allowed in executable ontology packages"
                )

            read_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            read_flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                source_descriptor = os.open(child, read_flags, 0o600)
            except OSError as exc:
                raise OntologyPackageRevisionError(
                    "ontology package file cannot be opened safely"
                ) from exc
            try:
                opened_stat = os.fstat(source_descriptor)
                if not stat.S_ISREG(opened_stat.st_mode):
                    raise OntologyPackageRevisionError(
                        "ontology package entry changed while being copied"
                    )
                write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                write_flags |= getattr(os, "O_BINARY", 0)
                target_descriptor = os.open(target, write_flags, 0o600)
                try:
                    while True:
                        chunk = os.read(
                            source_descriptor,
                            _CONTENT_READ_CHUNK_BYTES,
                        )
                        if not chunk:
                            break
                        view = memoryview(chunk)
                        while view:
                            written = os.write(target_descriptor, view)
                            if written <= 0:
                                raise OSError("short package staging write")
                            view = view[written:]
                finally:
                    os.close(target_descriptor)
            except OSError as exc:
                raise OntologyPackageRevisionError(
                    "ontology package file cannot be copied safely"
                ) from exc
            finally:
                os.close(source_descriptor)
            try:
                final_stat = child.lstat()
            except OSError as exc:
                raise OntologyPackageRevisionError(
                    "ontology package entry changed while being copied"
                ) from exc
            if stat.S_ISLNK(final_stat.st_mode) or not stat.S_ISREG(
                final_stat.st_mode
            ):
                raise OntologyPackageRevisionError(
                    "ontology package entry changed while being copied"
                )

    copy_directory(source_root, staged_root)


def _set_staged_tree_mode(root: Path, *, writable: bool) -> None:
    file_mode = 0o600 if writable else 0o400
    directory_mode = 0o700 if writable else 0o500
    paths = sorted(
        root.rglob("*"),
        key=lambda item: len(item.parts),
        reverse=writable,
    )
    for path in paths:
        try:
            path_stat = path.lstat()
            if stat.S_ISLNK(path_stat.st_mode):
                continue
            os.chmod(
                path,
                directory_mode if stat.S_ISDIR(path_stat.st_mode) else file_mode,
            )
        except OSError:
            if not writable:
                raise
    os.chmod(root, directory_mode)


def resolve_package_staging_boundary(
    *,
    project_root: Path,
    workspace_id: str,
) -> Path:
    """Keep admitted revisions outside a deletable workspace directory."""

    root = Path(project_root)
    try:
        root_stat = root.lstat()
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            raise OntologyPackageRevisionError("project root is unsafe")
        root = root.resolve(strict=True)
    except OSError as exc:
        raise OntologyPackageRevisionError("project root is unsafe") from exc

    workspace = str(workspace_id or "").strip()
    if root.name != workspace or root.parent.name != "workspaces":
        return root

    runtime_root = root.parent.parent
    try:
        workspaces_stat = root.parent.lstat()
        runtime_stat = runtime_root.lstat()
        if stat.S_ISLNK(workspaces_stat.st_mode):
            raise OntologyPackageRevisionError("runtime staging root is unsafe")
        if not stat.S_ISDIR(workspaces_stat.st_mode):
            raise OntologyPackageRevisionError("runtime staging root is unsafe")
        if stat.S_ISLNK(runtime_stat.st_mode):
            raise OntologyPackageRevisionError("runtime staging root is unsafe")
        if not stat.S_ISDIR(runtime_stat.st_mode):
            raise OntologyPackageRevisionError("runtime staging root is unsafe")
        runtime_root = runtime_root.resolve(strict=True)
        root.relative_to(runtime_root)
    except (OSError, ValueError) as exc:
        raise OntologyPackageRevisionError("runtime staging root is unsafe") from exc
    return runtime_root


@contextmanager
def stage_verified_package_revision(
    *,
    source_root: Path,
    expected_content_revision: str,
    staging_root: Path,
    revision_guard: Callable[[], bool] | None = None,
) -> Iterator[Path]:
    """Yield a private immutable copy below the containment boundary.

    ``staging_root`` is the trusted project/workspace boundary. Copies live in
    its dedicated hidden staging directory so replacing or deleting a package,
    version, or workspace cannot move an active run's admitted revision.
    """

    expected = str(expected_content_revision or "").strip()
    if (
        len(expected) != 71
        or not expected.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in expected[7:])
    ):
        raise OntologyPackageRevisionError(
            "admitted ontology content revision is invalid"
        )
    source = Path(source_root)
    boundary = Path(staging_root)
    try:
        boundary_stat = boundary.lstat()
        source_stat = source.lstat()
        if stat.S_ISLNK(boundary_stat.st_mode) or not stat.S_ISDIR(
            boundary_stat.st_mode
        ):
            raise OntologyPackageRevisionError("staging root is unsafe")
        if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISDIR(
            source_stat.st_mode
        ):
            raise OntologyPackageRevisionError("ontology package root is unsafe")
        boundary = boundary.resolve(strict=True)
        source = source.resolve(strict=True)
        source.relative_to(boundary)
    except (OSError, ValueError) as exc:
        raise OntologyPackageRevisionError(
            "ontology package must be inside the staging root"
        ) from exc

    def guarded() -> None:
        if revision_guard is not None and not revision_guard():
            raise OntologyPackageRevisionError(
                "ontology mutation revision changed after run admission"
            )

    guarded()
    if build_package_content_revision(source) != expected:
        raise OntologyPackageRevisionError(
            "ontology package content changed after run admission"
        )

    parent = boundary / ".ontology-run-staging"
    container: Path | None = None
    try:
        parent.mkdir(mode=0o700, exist_ok=True)
        parent_stat = parent.lstat()
        if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(
            parent_stat.st_mode
        ):
            raise OntologyPackageRevisionError("package staging directory is unsafe")
        parent = parent.resolve(strict=True)
        parent.relative_to(boundary)
        os.chmod(parent, 0o700)
        container = Path(tempfile.mkdtemp(prefix="run-", dir=parent))
        staged = container / "ontology"
        staged.mkdir(mode=0o700)
    except (OSError, ValueError) as exc:
        if container is not None:
            shutil.rmtree(container, ignore_errors=True)
        raise OntologyPackageRevisionError(
            "ontology package staging is unavailable"
        ) from exc

    try:
        _copy_visible_package(source, staged)
        if build_package_content_revision(source) != expected:
            raise OntologyPackageRevisionError(
                "ontology package changed while being staged"
            )
        if build_package_content_revision(staged) != expected:
            raise OntologyPackageRevisionError(
                "staged ontology package does not match admission"
            )
        guarded()
        _set_staged_tree_mode(staged, writable=False)
        yield staged
    finally:
        try:
            if staged.exists() and not staged.is_symlink():
                _set_staged_tree_mode(staged, writable=True)
            if container is not None and container.exists() and not container.is_symlink():
                shutil.rmtree(container)
            elif container is not None and container.is_symlink():
                container.unlink()
        finally:
            try:
                parent.rmdir()
            except OSError:
                pass


def _view_cache_enabled() -> bool:
    value = str(os.getenv("PERF_ONTOLOGY_VIEW_CACHE_ENABLED", "true") or "").strip()
    return value.lower() not in {"0", "false", "no", "off"}


def _configured_max_entries() -> int:
    raw = str(
        os.getenv("PERF_ONTOLOGY_VIEW_CACHE_MAX_ENTRIES", _DEFAULT_MAX_ENTRIES)
        or ""
    ).strip()
    try:
        return max(1, min(4096, int(raw)))
    except ValueError:
        return _DEFAULT_MAX_ENTRIES


def _normalized_scope(workspace_id: str, ontology_id: str) -> tuple[str, str]:
    return (
        _normalized_value(workspace_id, "workspace_id"),
        _normalized_value(ontology_id, "ontology_id"),
    )


def _normalized_value(value: object, name: str) -> str:
    resolved = str(value or "").strip()
    if not resolved:
        raise ValueError("{} is required".format(name))
    return resolved


def _record_ontology_view_access(
    result: str,
    started: float,
    workspace_id: str,
) -> None:
    try:
        from integration.observability.performance import record_cache_access

        record_cache_access(
            "ontology_view",
            result,
            max(0.0, (time.perf_counter() - started) * 1000),
            workspace_id=workspace_id,
        )
    except Exception:
        logger.debug("Ontology view cache metric recording failed", exc_info=True)


_view_cache = OntologyViewCache(max_entries=_configured_max_entries())
