"""Run-scoped access to the immutable package copy verified by preflight."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Sequence
from uuid import uuid4

from workspace.workspaces import get_workspaces_root
from .config import load_sandbox_config
from .errors import SandboxUnavailableError
from .registry import get_run_sandbox_registry, release_run_sandbox


@dataclass(frozen=True)
class VerifiedPackageExecution:
    package_root: Path
    workspace_id: str
    ontology_id: str
    parent_run_id: str
    sandbox_run_id: str
    read_only_paths: tuple[Path, ...] = ()
    writable_paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class AdmittedPackageRevision:
    package_root: Path
    workspace_id: str
    ontology_id: str
    run_id: str


_VERIFIED_PACKAGE: ContextVar[VerifiedPackageExecution | None] = ContextVar(
    "sandbox_verified_package_execution", default=None
)
_ADMITTED_PACKAGE: ContextVar[AdmittedPackageRevision | None] = ContextVar(
    "sandbox_admitted_package_revision", default=None
)


def _verified_package_root(package_root: Path) -> Path:
    root = Path(package_root)
    staging_parent = get_workspaces_root().resolve().parent / ".ontology-run-staging"
    if (
        root.name != "ontology"
        or not root.parent.name.startswith("run-")
        or root.parent.parent != staging_parent
    ):
        raise SandboxUnavailableError("verified package staging boundary is invalid")
    if (
        any(path.is_symlink() for path in (root, root.parent, staging_parent))
        or not root.is_dir()
    ):
        raise SandboxUnavailableError("verified package staging boundary is invalid")
    return root.resolve(strict=True)


@contextmanager
def admitted_package_revision(
    package_root: Path, *, workspace_id: str, ontology_id: str, run_id: str,
):
    """Authorize only the revision yielded by the runtime's preflight staging context."""
    if not load_sandbox_config().enabled:
        yield
        return
    if not run_id or not workspace_id or not ontology_id:
        raise SandboxUnavailableError("verified package staging boundary is invalid")
    scope = AdmittedPackageRevision(
        _verified_package_root(package_root), workspace_id, ontology_id, run_id,
    )
    token = _ADMITTED_PACKAGE.set(scope)
    try:
        yield
    finally:
        _ADMITTED_PACKAGE.reset(token)


@contextmanager
def verified_package_execution(
    package_root: Path, *, workspace_id: str, ontology_id: str, run_id: str | None,
    owner_run_id: str | None = None,
    read_only_paths: tuple[Path, ...] = (), writable_paths: tuple[Path, ...] = (),
):
    """Bind only a copy yielded by ``stage_verified_package_revision``.

    This internal handoff must follow revision verification. It is never part
    of a tool schema or user-supplied runtime context. Each copy gets its own
    companion sandbox, released before the staging context deletes the copy.
    """
    if not load_sandbox_config().enabled:
        yield
        return
    if not run_id or not workspace_id or not ontology_id:
        raise SandboxUnavailableError("verified package staging boundary is invalid")
    scope = VerifiedPackageExecution(
        package_root=_verified_package_root(package_root), workspace_id=workspace_id,
        ontology_id=ontology_id, parent_run_id=run_id,
        sandbox_run_id=f"{run_id}:package:{uuid4().hex}",
        read_only_paths=read_only_paths, writable_paths=writable_paths,
    )
    get_run_sandbox_registry().register_companion(owner_run_id or run_id, scope.sandbox_run_id)
    token = _VERIFIED_PACKAGE.set(scope)
    try:
        yield
    finally:
        _VERIFIED_PACKAGE.reset(token)
        try:
            release_run_sandbox(scope.sandbox_run_id, strict=True)
        except Exception as exc:
            raise SandboxUnavailableError("verified package sandbox cleanup failed") from exc


def _deterministic_mounts(
    package_root: Path, read_only_paths: Sequence[Path], writable_paths: Sequence[Path],
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """Bind only the handler's private artifact directory and optional broker parent."""
    read_only = tuple(Path(path) for path in read_only_paths)
    writable = tuple(Path(path) for path in writable_paths)
    if not read_only and not writable:
        return (), ()
    if len(writable) != 1 or len(read_only) > 1:
        raise SandboxUnavailableError("verified package artifact mounts are invalid")
    artifact = writable[0]
    if (
        not re.fullmatch(r"run-[0-9a-f]{64}", artifact.name)
        or not artifact.parent.name.startswith("deterministic-artifact-staging-")
    ):
        raise SandboxUnavailableError("verified package artifact mounts are invalid")
    if read_only and read_only != (artifact.parent,):
        raise SandboxUnavailableError("verified package artifact mounts are invalid")
    for path in (artifact.parent, artifact):
        if (
            not path.is_absolute() or path.resolve(strict=True) != path or not path.is_dir()
        ):
            raise SandboxUnavailableError("verified package artifact mounts are invalid")
        if path.is_relative_to(package_root) or package_root.is_relative_to(path):
            raise SandboxUnavailableError("verified package artifact mounts are invalid")
    return read_only, writable


@contextmanager
def verified_deterministic_execution(
    package_root: Path, *, workspace_id: str, ontology_id: str, run_id: str,
    sandbox_run_id: str | None = None,
    read_only_paths: Sequence[Path] = (), writable_paths: Sequence[Path] = (),
):
    """Give each admitted parser/handler call its own sandbox, destroyed before returning."""
    scope = _ADMITTED_PACKAGE.get()
    if scope is None:
        yield
        return
    if (
        workspace_id != scope.workspace_id or ontology_id != scope.ontology_id
        or run_id != scope.run_id
    ):
        raise SandboxUnavailableError("verified package admission scope mismatch")
    if Path(package_root).resolve() != scope.package_root:
        raise SandboxUnavailableError("verified package admission scope mismatch")
    read_only, writable = _deterministic_mounts(scope.package_root, read_only_paths, writable_paths)
    with verified_package_execution(
        scope.package_root, workspace_id=workspace_id, ontology_id=ontology_id,
        run_id=sandbox_run_id or run_id, owner_run_id=run_id,
        read_only_paths=read_only, writable_paths=writable,
    ):
        yield


def current_verified_package_execution(
    *, workspace_id: str | None, ontology_id: str | None,
    run_id: str, ontology_dir: Path | None,
) -> VerifiedPackageExecution | None:
    scope = _VERIFIED_PACKAGE.get()
    if scope is None:
        return None
    if (
        workspace_id != scope.workspace_id or ontology_id != scope.ontology_id
        or run_id != scope.parent_run_id
    ):
        raise SandboxUnavailableError("verified package execution scope mismatch")
    if ontology_dir is None or Path(ontology_dir).resolve() != scope.package_root:
        raise SandboxUnavailableError("verified package execution scope mismatch")
    return scope
