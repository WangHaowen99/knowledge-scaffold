"""Execute ontology scripts inside jiuwenbox sandboxes."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Any, Mapping, Sequence

from workspace.workspaces import get_workspace_project_root, get_workspaces_root
from .config import SandboxConfig, load_sandbox_config
from .data_proxy import PreparedDataConnection, get_data_connection_secret_manager
from .env_allowlist import build_script_env
from .errors import SandboxExecError, SandboxUnavailableError
from .paths import posix_path_string, to_sandbox_script_path
from .package_revision import current_verified_package_execution
from .policy_builder import PolicyContext
from .registry import get_run_sandbox_registry
from .runtime_contract import sandbox_runtime_revision, sandbox_runtime_revision_file


_RUNTIME_ATTESTATION_CODE = (
    "from pathlib import Path; import sys; "
    "print(Path(sys.argv[1]).read_text(encoding='utf-8').strip())"
)


def _attest_sandbox_runtime(
    registry: Any,
    sandbox_id: str,
    *,
    config: SandboxConfig,
    timeout_seconds: int,
) -> None:
    """Fail closed unless this sandbox exposes the expected runtime revision."""
    try:
        attestation = registry.exec_in_sandbox(
            sandbox_id,
            [
                config.python_bin,
                "-c",
                _RUNTIME_ATTESTATION_CODE,
                sandbox_runtime_revision_file(),
            ],
            timeout_seconds=min(5, max(1, int(timeout_seconds))),
        )
    except Exception as exc:
        raise SandboxUnavailableError(
            "sandbox runtime revision could not be verified"
        ) from exc
    if (
        int(attestation.exit_code) != 0
        or str(attestation.stdout or "").strip() != sandbox_runtime_revision()
    ):
        raise SandboxUnavailableError("sandbox runtime revision mismatch")


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
        env_overrides: Mapping[str, str] | None = None,
        prepare_data_connection: bool = True,
        include_data_connection_env: bool = True,
        network_endpoint: str | None = None,
        read_only_paths: Sequence[Path] = (),
        writable_paths: Sequence[Path] = (),
        stdout_limit_bytes: int | None = None,
        stderr_limit_bytes: int | None = None,
        file_size_limit_bytes: int | None = None,
        cancel_event: Any = None,
        restrict_filesystem_to_ontology: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        verified_package = current_verified_package_execution(
            workspace_id=workspace_id, ontology_id=ontology_id,
            run_id=run_id, ontology_dir=ontology_dir,
        )
        if verified_package is not None:
            if read_only_paths or writable_paths:
                raise SandboxUnavailableError("verified package cannot add filesystem mounts")
            workspace_root = verified_package.package_root
            run_id = verified_package.sandbox_run_id
            restrict_filesystem_to_ontology = True
        else:
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
        if restrict_filesystem_to_ontology:
            if resolved_ontology_dir is None:
                raise SandboxUnavailableError(
                    "ontology directory is required for restricted sandbox execution"
                )
            try:
                sandbox_script_path.relative_to(resolved_ontology_dir)
            except ValueError as exc:
                raise SandboxUnavailableError(
                    "script path is outside the restricted ontology directory"
                ) from exc

        if prepare_data_connection:
            prepared_connection = get_data_connection_secret_manager().prepare(
                run_id=run_id,
                workspace_id=workspace_id,
                ontology_id=ontology_id,
            )
        else:
            prepared_connection = PreparedDataConnection(env={}, data_proxy_url=None)
        resolved_writable_paths: list[Path] = []
        for raw_path in writable_paths:
            path = Path(raw_path)
            if path.is_symlink() or not path.is_dir():
                raise SandboxUnavailableError("sandbox writable path must be a regular directory")
            resolved_writable_paths.append(path.resolve())
        resolved_read_only_paths: list[Path] = []
        for raw_path in read_only_paths:
            path = Path(raw_path)
            if path.is_symlink() or not path.is_dir():
                raise SandboxUnavailableError(
                    "sandbox read-only path must be a regular directory"
                )
            resolved_read_only_paths.append(path.resolve())
        if restrict_filesystem_to_ontology and resolved_ontology_dir is not None:
            resolved_read_only_paths.insert(0, resolved_ontology_dir)
        else:
            resolved_read_only_paths.insert(0, workspace_root)
        runtime_python_dir = Path(self._config.sandbox_runtime_root) / "python"
        ctx = PolicyContext(
            run_id=run_id,
            workspace_id=workspace_id,
            ontology_id=ontology_id,
            workspace_root=workspace_root,
            sandbox_runtime_python_dir=runtime_python_dir,
            data_proxy_url=(
                str(network_endpoint or "").strip()
                or prepared_connection.data_proxy_url
            ),
            memory_max=self._config.memory_max,
            cpu_max=self._config.cpu_max,
            pids_max=self._config.pids_max,
            writable_paths=tuple(resolved_writable_paths),
            read_only_paths=tuple(dict.fromkeys(resolved_read_only_paths)),
        )
        registry = get_run_sandbox_registry()
        sandbox_id = registry.ensure(ctx)

        if (
            str((env_overrides or {}).get("DETERMINISTIC_EXECUTION") or "").strip()
            == "1"
        ):
            _attest_sandbox_runtime(
                registry,
                sandbox_id,
                config=self._config,
                timeout_seconds=timeout_seconds,
            )

        sandbox_script = posix_path_string(sandbox_script_path)
        command = [self._config.python_bin, "-u", sandbox_script, *cli_args]
        if (
            str((env_overrides or {}).get("DETERMINISTIC_EXECUTION") or "").strip()
            == "1"
        ):
            command = [
                self._config.python_bin,
                "-u",
                "-P",
                "-m",
                "onto_sandbox_runtime.deterministic_runner",
                "--",
                sandbox_script,
                *cli_args,
            ]
        if (
            stdout_limit_bytes is not None
            or stderr_limit_bytes is not None
            or file_size_limit_bytes is not None
        ):
            command = [
                self._config.python_bin,
                "-m",
                "onto_sandbox_runtime.bounded_runner",
                "--stdout-limit-bytes",
                str(max(0, int(stdout_limit_bytes or 0))),
                "--stderr-limit-bytes",
                str(max(0, int(stderr_limit_bytes or 0))),
                "--file-size-limit-bytes",
                str(max(0, int(file_size_limit_bytes or 0))),
                "--",
                *command,
            ]
        ontology_pythonpath = (
            posix_path_string(resolved_ontology_dir)
            if resolved_ontology_dir is not None
            else None
        )
        effective_env_overrides = dict(env_overrides or {})
        if include_data_connection_env:
            # The run-scoped proxy payload is authoritative for generic
            # sandbox calls and must replace any host-side secret envelope.
            effective_env_overrides.update(prepared_connection.env)
        if effective_env_overrides.get("DETERMINISTIC_EXECUTION") == "1":
            from agent.tools.business_data.script_env import (
                SCRIPT_BUSINESS_DATA_CONNECTION_ENV,
            )

            effective_env_overrides.pop(SCRIPT_BUSINESS_DATA_CONNECTION_ENV, None)
        env = build_script_env(
            env_overrides=effective_env_overrides,
            ontology_dir=ontology_pythonpath,
            runtime_python_path=posix_path_string(runtime_python_dir),
        )

        exec_kwargs = {
            "workdir": posix_path_string(
                resolved_ontology_dir
                if restrict_filesystem_to_ontology and resolved_ontology_dir is not None
                else workspace_root
            ),
            "env": env,
            "timeout_seconds": max(1, int(timeout_seconds)),
        }
        if cancel_event is None:
            result = registry.exec_in_sandbox(sandbox_id, command, **exec_kwargs)
        else:
            result_holder: dict[str, Any] = {}
            finished = threading.Event()

            def execute() -> None:
                try:
                    result_holder["result"] = registry.exec_in_sandbox(
                        sandbox_id,
                        command,
                        **exec_kwargs,
                    )
                except BaseException as exc:  # propagated in the caller thread
                    result_holder["error"] = exc
                finally:
                    finished.set()

            worker = threading.Thread(
                target=execute,
                name="sandbox-exec-{}".format(run_id),
                daemon=True,
            )
            worker.start()
            while not finished.wait(0.05):
                if cancel_event.is_set():
                    # Deleting the run-scoped sandbox terminates the process even
                    # though the HTTP exec request may still be unwinding.
                    registry.release(run_id)
                    raise SandboxExecError("exec cancelled in sandbox {}".format(sandbox_id))
            if cancel_event.is_set():
                registry.release(run_id)
                raise SandboxExecError("exec cancelled in sandbox {}".format(sandbox_id))
            error = result_holder.get("error")
            if error is not None:
                raise error
            result = result_holder.get("result")
            if result is None:
                raise SandboxExecError("sandbox exec returned no result")
        return subprocess.CompletedProcess(
            args=command,
            returncode=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
        )
