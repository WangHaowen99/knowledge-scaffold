"""Execute ontology scripts inside jiuwenbox sandboxes."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence

from workspace.workspaces import get_workspace_project_root, get_workspaces_root
from .config import SandboxConfig, load_sandbox_config
from .data_proxy import get_data_connection_secret_manager
from .env_allowlist import build_script_env, collect_script_config_env
from .errors import SandboxUnavailableError
from .paths import posix_path_string, to_sandbox_script_path
from .policy_builder import PolicyContext
from .registry import get_run_sandbox_registry


def _resolve_workspace_mount_root(workspace_id: str | None) -> Path:
    """Resolve the only workspace directory that a sandbox may mount."""
    resolved_id = str(workspace_id or "").strip()
    if not resolved_id:
        raise SandboxUnavailableError(
            "workspace_id is required to build the sandbox filesystem policy"
        )
    try:
        workspace_root = get_workspace_project_root(resolved_id).resolve()
    except (OSError, ValueError) as exc:
        raise SandboxUnavailableError(
            "cannot resolve sandbox workspace '{}'".format(resolved_id)
        ) from exc
    workspaces_root = get_workspaces_root().resolve()
    try:
        workspace_root.relative_to(workspaces_root)
    except ValueError as exc:
        raise SandboxUnavailableError(
            "sandbox workspace resolves outside the workspaces root: {}".format(
                resolved_id
            )
        ) from exc
    if not workspace_root.is_dir():
        raise SandboxUnavailableError(
            "sandbox workspace does not exist: {}".format(resolved_id)
        )
    return workspace_root


def _require_path_within_workspace(
    path: Path,
    workspace_root: Path,
    *,
    label: str,
) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(workspace_root)
    except ValueError as exc:
        raise SandboxUnavailableError(
            "{} is outside the sandbox workspace".format(label)
        ) from exc
    return resolved


class SandboxScriptExecutor:
    """Run a validated script via jiuwenbox exec."""

    def __init__(self, config: SandboxConfig | None = None) -> None:
        self._config = config or load_sandbox_config()

    def run_script(
        self,
        *,
        script_path: str,
        cli_args: Sequence[str],
        project_root: Path,
        ontology_dir: Path | None,
        run_id: str,
        workspace_id: str | None,
        ontology_id: str | None,
        timeout_seconds: int,
    ) -> subprocess.CompletedProcess[str]:
        workspace_root = _resolve_workspace_mount_root(workspace_id)
        sandbox_script_path = Path(
            to_sandbox_script_path(
                script_path,
                project_root=project_root,
            )
        )
        sandbox_script_path = _require_path_within_workspace(
            sandbox_script_path,
            workspace_root,
            label="script path",
        )
        resolved_ontology_dir = (
            _require_path_within_workspace(
                ontology_dir,
                workspace_root,
                label="ontology directory",
            )
            if ontology_dir is not None
            else None
        )

        prepared_connection = get_data_connection_secret_manager().prepare(
            run_id=run_id,
            workspace_id=workspace_id,
            ontology_id=ontology_id,
        )
        runtime_python_dir = Path(self._config.sandbox_runtime_root) / "python"
        ctx = PolicyContext(
            run_id=run_id,
            workspace_id=workspace_id,
            ontology_id=ontology_id,
            workspace_root=workspace_root,
            sandbox_runtime_python_dir=runtime_python_dir,
            data_proxy_url=prepared_connection.data_proxy_url,
            memory_max=self._config.memory_max,
            cpu_max=self._config.cpu_max,
            pids_max=self._config.pids_max,
        )
        registry = get_run_sandbox_registry()
        sandbox_id = registry.ensure(ctx)

        sandbox_script = posix_path_string(sandbox_script_path)
        command = [self._config.python_bin, "-u", sandbox_script, *cli_args]
        ontology_pythonpath = (
            posix_path_string(resolved_ontology_dir)
            if resolved_ontology_dir is not None
            else None
        )
        env_overrides = collect_script_config_env()
        env_overrides.update(prepared_connection.env)
        env = build_script_env(
            env_overrides=env_overrides,
            ontology_dir=ontology_pythonpath,
            runtime_python_path=posix_path_string(runtime_python_dir),
        )

        result = registry.exec_in_sandbox(
            sandbox_id,
            command,
            workdir=posix_path_string(workspace_root),
            env=env,
            timeout_seconds=max(1, int(timeout_seconds)),
        )
        return subprocess.CompletedProcess(
            args=command,
            returncode=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
        )
