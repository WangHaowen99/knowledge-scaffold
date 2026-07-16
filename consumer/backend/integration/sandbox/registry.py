"""Per-run sandbox lifecycle registry."""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Optional

from utils.logging import get_logger
from .client import JiuwenboxClient
from .config import SandboxConfig, load_sandbox_config
from .errors import SandboxCreateError
from .paths import posix_path_string
from .policy_builder import OntoSandboxPolicyBuilder, PolicyContext

logger = get_logger("sandbox.registry")

_registry_lock = threading.Lock()
_registry_instance: Optional["RunSandboxRegistry"] = None


def sandbox_id_for_run(run_id: str) -> str:
    """Derive a jiuwenbox-compatible sandbox id (4..16 chars) from run_id."""
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    # "onto-" (5) + 11 hex = 16 chars max for jiuwenbox.
    return f"onto-{digest[:11]}"


def _policy_scope_hash(ctx: PolicyContext, policy: dict) -> str:
    scope = {
        "run_id": ctx.run_id,
        "workspace_id": ctx.workspace_id,
        "ontology_id": ctx.ontology_id,
        "workspace_root": posix_path_string(ctx.workspace_root),
        "sandbox_runtime_python_dir": posix_path_string(ctx.sandbox_runtime_python_dir),
        "data_proxy_url": ctx.data_proxy_url,
        "policy": policy,
    }
    payload = json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RunSandboxRegistry:
    """Track run_id -> sandbox_id and perform lazy create / release."""

    def __init__(self, config: SandboxConfig) -> None:
        self._client = JiuwenboxClient(
            config.jiuwenbox_url,
            timeout=config.client_timeout_seconds,
        )
        self._condition = threading.Condition(threading.Lock())
        self._run_to_sandbox: dict[str, str] = {}
        self._run_to_scope_hash: dict[str, str] = {}
        self._creating_runs: set[str] = set()

    def ensure(self, ctx: PolicyContext) -> str:
        sandbox_id = sandbox_id_for_run(ctx.run_id)
        policy = OntoSandboxPolicyBuilder.build(ctx)
        scope_hash = _policy_scope_hash(ctx, policy)

        # Single-flight creation per run. Different runs are still free to create
        # concurrently, while duplicate callers for one run wait for the owner.
        with self._condition:
            while ctx.run_id in self._creating_runs:
                self._condition.wait()
            existing = self._run_to_sandbox.get(ctx.run_id)
            if existing is not None:
                if self._run_to_scope_hash.get(ctx.run_id) != scope_hash:
                    raise SandboxCreateError(
                        "sandbox scope changed within the same run: {}".format(ctx.run_id)
                    )
                return existing
            self._creating_runs.add(ctx.run_id)

        created = False
        try:
            self._client.create_sandbox(
                sandbox_id,
                policy,
                policy_mode="override",
            )
            created = True
        except SandboxCreateError:
            raise
        except Exception as exc:
            raise SandboxCreateError(str(exc)) from exc
        finally:
            with self._condition:
                if created:
                    self._run_to_sandbox[ctx.run_id] = sandbox_id
                    self._run_to_scope_hash[ctx.run_id] = scope_hash
                self._creating_runs.discard(ctx.run_id)
                self._condition.notify_all()

        logger.info(
            (
                "Sandbox created: run_id=%s sandbox_id=%s workspace_id=%s "
                "ontology_id=%s policy_scope_hash=%s"
            ),
            ctx.run_id,
            sandbox_id,
            ctx.workspace_id,
            ctx.ontology_id,
            scope_hash,
        )
        return sandbox_id

    def exec_in_sandbox(
        self,
        sandbox_id: str,
        command: list[str],
        *,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
    ):
        return self._client.exec(
            sandbox_id,
            command,
            workdir=workdir,
            env=env,
            timeout_seconds=timeout_seconds,
        )

    def release(self, run_id: str) -> None:
        if not run_id:
            return
        with self._condition:
            while run_id in self._creating_runs:
                self._condition.wait()
            sandbox_id = self._run_to_sandbox.pop(run_id, None)
            self._run_to_scope_hash.pop(run_id, None)
        if not sandbox_id:
            return
        try:
            self._client.delete_sandbox(sandbox_id)
            logger.info("Sandbox released: run_id=%s sandbox_id=%s", run_id, sandbox_id)
        except Exception as exc:
            logger.warning(
                "Failed to release sandbox run_id=%s sandbox_id=%s: %s",
                run_id,
                sandbox_id,
                exc,
            )

def get_run_sandbox_registry() -> RunSandboxRegistry:
    global _registry_instance
    with _registry_lock:
        if _registry_instance is None:
            _registry_instance = RunSandboxRegistry(load_sandbox_config())
        return _registry_instance


def release_run_sandbox(run_id: str | None) -> None:
    """Release sandbox for a run if the registry exists and sandbox is enabled."""
    if not run_id:
        return
    config = load_sandbox_config()
    if not config.enabled:
        return
    try:
        get_run_sandbox_registry().release(run_id)
    except Exception as exc:
        logger.warning("release_run_sandbox failed for run_id=%s: %s", run_id, exc)
    try:
        from .data_proxy import get_data_connection_secret_manager

        get_data_connection_secret_manager().release(run_id)
    except Exception as exc:
        logger.warning("release sandbox data proxy failed for run_id=%s: %s", run_id, exc)

