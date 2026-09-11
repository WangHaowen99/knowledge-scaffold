"""Run-scoped access to the immutable package copy verified by preflight."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
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


_VERIFIED_PACKAGE: ContextVar[VerifiedPackageExecution | None] = ContextVar(
    "sandbox_verified_package_execution", default=None
)


@contextmanager
def verified_package_execution(
    package_root: Path, *, workspace_id: str, ontology_id: str, run_id: str | None,
):
    """Bind only a copy yielded by ``stage_verified_package_revision``.

    This internal handoff must follow revision verification. It is never part
    of a tool schema or user-supplied runtime context. Each copy gets its own
    companion sandbox, released before the staging context deletes the copy.
    """
    if not load_sandbox_config().enabled:
        yield
        return
    root = Path(package_root)
    staging_parent = get_workspaces_root().resolve().parent / ".ontology-run-staging"
    if not run_id or not workspace_id or not ontology_id:
        raise SandboxUnavailableError("verified package staging boundary is invalid")
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
    scope = VerifiedPackageExecution(
        package_root=root.resolve(strict=True), workspace_id=workspace_id,
        ontology_id=ontology_id, parent_run_id=run_id,
        sandbox_run_id=f"{run_id}:package:{uuid4().hex}",
    )
    get_run_sandbox_registry().register_companion(run_id, scope.sandbox_run_id)
    token = _VERIFIED_PACKAGE.set(scope)
    try:
        yield
    finally:
        _VERIFIED_PACKAGE.reset(token)
        release_run_sandbox(scope.sandbox_run_id, strict=True)


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
