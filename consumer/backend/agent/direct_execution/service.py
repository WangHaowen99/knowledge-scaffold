"""General deterministic operation execution service."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
import unicodedata
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import quote, quote_plus, urlparse

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry
from referencing.exceptions import NoSuchResource

from agent.direct_execution.arguments import (
    build_direct_cli_args,
    extract_runtime_argument_keys,
    protect_deterministic_script_args,
    protect_deterministic_runtime_parameters,
)
from agent.direct_execution.audit import build_result_envelope, runtime_revision
from agent.direct_execution.contracts import (
    AwaitingInput,
    Completed,
    DeterministicOutcome,
    DeterministicRequest,
    Failed,
    LockedDependency,
    NotApplicable,
)
from agent.tools.business_data.deterministic import (
    DeterministicProviderBinding,
    DeterministicProviderError,
    MIN_DETERMINISTIC_PROVIDER_SECRET_LENGTH,
    resolve_deterministic_provider_binding,
)
from agent.tools.business_data.endpoint_security import endpoint_secret_values
from agent.tools.business_data.script_env import (
    SCRIPT_BUSINESS_DATA_CONNECTION_ENV,
    serialize_script_business_data_connection,
)
from agent.tools.execute_script import handle_execute_script_path
from agent.core.shared.interaction_policy import user_interaction_supported_from_context
from integration.sandbox.registry import (
    register_run_sandbox_companion,
    release_run_sandbox,
)
from integration.sandbox.config import load_sandbox_config
from integration.sandbox.data_proxy import sandbox_reachable_url
from ontology.ontology_catalog import (
    find_markdown_file,
    get_ontology_manifest,
    load_script_entry_from_descriptor,
)
from ontology.operation_execution import (
    DeterministicOperationSpec,
    DeterministicPackageError,
    canonical_json_bytes,
    load_deterministic_operation_specs,
    normalize_operation_token,
    verify_deterministic_package_lock,
)


PARSER_STDOUT_LIMIT = 64 * 1024
HANDLER_STDOUT_LIMIT = 5 * 1024 * 1024
MAX_ARTIFACT_COUNT = 32
MAX_ARTIFACT_BYTES = 10 * 1024 * 1024
MAX_ARTIFACT_TOTAL_BYTES = 20 * 1024 * 1024
MAX_ARTIFACT_TREE_ENTRIES = 128
ARTIFACT_MONITOR_INTERVAL_SECONDS = 0.02
ARTIFACT_STAGING_HEARTBEAT_SECONDS = 60
STALE_ARTIFACT_STAGING_SECONDS = 24 * 60 * 60
DETERMINISTIC_ARTIFACT_STAGING_ROOT_ENV = "DETERMINISTIC_ARTIFACT_STAGING_ROOT"
SCRIPT_STDERR_LIMIT = 64 * 1024
_ARTIFACT_MEDIA_TYPES = {
    ".csv": "text/csv",
    ".html": "text/html",
    ".json": "application/json",
    ".log": "text/plain",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".xml": "application/xml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
}
_SENSITIVE_CONNECTION_KEY_PARTS = (
    "api_key",
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
)
_IMPLICIT_COMMAND_PREFIX = re.compile(
    r"^(?:(?:请|麻烦)?(?:帮我)?(?:执行|运行|调用)|(?:please\s+)?(?:execute|run|invoke))\s*$"
)
_INLINE_ARGUMENT_PREFIX = re.compile(
    r"^(?:(?:参数|args?|arguments?|with)\b|[\w.-]+\s*=|"
    r"\{\s*(?:[\"'][^\"']+[\"']|[\w.-]+)\s*:)",
    re.IGNORECASE,
)
_INLINE_POSITIONAL_ARGUMENT_PREFIX = re.compile(
    r"^(?:(?:19|20)\d{2}\s*(?:[-/]\s*(?:0?[1-9]|1[0-2])|"
    r"年\s*(?:0?[1-9]|1[0-2])月?)\s*(?:[,，:：;；]\s*)?(?=$|[\[{])|"
    r"\[\s*(?:\]|[\[{\"'\d+-]|true\b|false\b|null\b))",
    re.IGNORECASE,
)
_INLINE_ARGUMENT_SEPARATOR = re.compile(r"^[,，:：;；]\s*")
_INLINE_ARGUMENT_GROUP = re.compile(r"^[([]\s*")
_NON_EXECUTION_MARKER = re.compile(
    r"(?:不(?:想|要|再|应|需|必)?|无需|禁止|别|请勿|不得)(?:再)?(?:执行|运行|调用)"
    r"|\b(?:do\s+not|don't|dont|never)\s+(?:execute|run|invoke)\b",
    re.IGNORECASE,
)
_OPEN_QUOTES = frozenset("\"'`“‘「『《〈")
_CLOSE_QUOTES = frozenset("\"'`”’」』》〉")
_TLS_ENDPOINT_SCHEMES = frozenset(
    {
        "https",
        "wss",
        "bolt+s",
        "bolt+ssc",
        "neo4j+s",
        "neo4j+ssc",
    }
)

ArtifactWriter = Callable[[str, str, str], Optional[Mapping[str, Any]]]


def _sandbox_provider_connection_endpoint(
    original_endpoint: str,
    reachable_endpoint: str,
) -> str:
    """Preserve the TLS server name while separately pinning sandbox egress."""
    original = str(original_endpoint or "").strip()
    scheme = urlparse(original).scheme.strip().casefold()
    if scheme in _TLS_ENDPOINT_SCHEMES:
        return original
    return str(reachable_endpoint or "").strip() or original


def _runtime_owned_argument_names(
    arguments: Mapping[str, Any],
    runtime_argument_keys: frozenset[str],
) -> list[str]:
    return sorted(
        {
            str(key)
            for key in arguments
            if str(key) in runtime_argument_keys
        },
        key=lambda item: item.encode("utf-8"),
    )


def _runtime_owned_checkpoint_names(
    checkpoint: Mapping[str, Any],
    runtime_argument_keys: frozenset[str],
) -> list[str]:
    names: set[str] = set()
    arguments = checkpoint.get("arguments")
    if isinstance(arguments, Mapping):
        names.update(_runtime_owned_argument_names(arguments, runtime_argument_keys))
    missing_fields = checkpoint.get("missing_fields")
    if isinstance(missing_fields, list):
        for field in missing_fields:
            normalized = str(field or "").strip()
            if normalized in runtime_argument_keys:
                names.add(normalized)
    pending_parser_issues = checkpoint.get("pending_parser_issues")
    issues = pending_parser_issues if isinstance(pending_parser_issues, list) else []
    for issue in issues:
        if not isinstance(issue, Mapping):
            continue
        field = str(issue.get("field") or "").strip()
        if field in runtime_argument_keys:
            names.add(field)
    return sorted(names, key=lambda item: item.encode("utf-8"))


class _AnyCancelEvent:
    """Expose one read-only cancellation view over multiple event sources."""

    def __init__(self, *events: Any) -> None:
        self._events = tuple(event for event in events if event is not None)

    def is_set(self) -> bool:
        return any(
            callable(getattr(event, "is_set", None)) and event.is_set()
            for event in self._events
        )


def _deny_schema_retrieval(uri: str) -> Any:
    raise NoSuchResource(ref=uri)


_SCHEMA_REGISTRY = Registry(retrieve=_deny_schema_retrieval)


class DeterministicExecutionService:
    """Route and execute one opt-in operation without model involvement."""

    @staticmethod
    def _artifact_staging_parent() -> Optional[Path]:
        """Resolve the optional host/JiuwenBox shared staging directory."""
        configured = str(
            os.environ.get(DETERMINISTIC_ARTIFACT_STAGING_ROOT_ENV) or ""
        ).strip()
        if not configured:
            return None
        path = Path(configured)
        if not path.is_absolute():
            raise ValueError(
                "{} must be an absolute path".format(
                    DETERMINISTIC_ARTIFACT_STAGING_ROOT_ENV
                )
            )
        path.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise ValueError(
                "{} must not be a symbolic link".format(
                    DETERMINISTIC_ARTIFACT_STAGING_ROOT_ENV
                )
            )
        resolved = path.resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError(
                "{} must be a directory".format(
                    DETERMINISTIC_ARTIFACT_STAGING_ROOT_ENV
                )
            )
        return resolved

    @staticmethod
    def _cleanup_stale_artifact_staging(parent: Optional[Path]) -> None:
        """Best-effort cleanup for TemporaryDirectory entries left by exits."""
        if parent is None:
            return
        cutoff = time.time() - STALE_ARTIFACT_STAGING_SECONDS
        try:
            entries = tuple(os.scandir(parent))
        except OSError:
            return
        for entry in entries:
            if not entry.name.startswith("deterministic-artifact-staging-"):
                continue
            try:
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    continue
                if entry.stat(follow_symlinks=False).st_mtime > cutoff:
                    continue
                shutil.rmtree(entry.path)
            except OSError:
                # A concurrent or permission-constrained entry remains eligible
                # for the next execution's cleanup pass.
                continue

    @staticmethod
    def _refresh_artifact_staging_lease(path: Path) -> None:
        """Refresh an active staging directory so peer processes preserve it."""
        if path.is_symlink() or not path.is_dir():
            raise OSError("deterministic artifact staging lease is invalid")
        try:
            os.utime(path, follow_symlinks=False)
        except NotImplementedError:
            # Windows does not implement the follow_symlinks keyword for
            # os.utime.  This directory has an unguessable, process-owned name;
            # re-check it immediately before using the portable call.
            if path.is_symlink() or not path.is_dir():
                raise OSError("deterministic artifact staging lease is invalid")
            os.utime(path)

    def execute(
        self,
        request: DeterministicRequest,
        *,
        enabled: bool,
        project_root: str,
        timeout_seconds: int,
        artifact_writer: Optional[ArtifactWriter] = None,
        observer: Any = None,
        cancel_event: Any = None,
    ) -> DeterministicOutcome:
        ontology_id = str(
            request.runtime_context.selected_ontology_id
            or request.context.get("selected_ontology_id")
            or ""
        ).strip()
        if not ontology_id:
            return NotApplicable("no_selected_ontology")
        ontology_version = str(
            request.runtime_context.selected_ontology_version
            or request.context.get("selected_ontology_version")
            or request.context.get("selected_version")
            or ""
        ).strip()
        manifest_kwargs: dict[str, Any] = {
            "workspace_id": request.runtime_context.workspace_id,
        }
        if ontology_version:
            manifest_kwargs["version"] = ontology_version
        ontology, ontology_error = get_ontology_manifest(ontology_id, **manifest_kwargs)
        if ontology is None:
            if request.context.get("requested_operation_key") or request.checkpoint:
                return Failed(
                    "operation_not_found",
                    "The selected ontology or operation was not found.",
                    internal_detail=ontology_error,
                )
            return NotApplicable("ontology_not_found")

        ontology_dir = Path(ontology.ontology_dir).resolve()
        try:
            specs = load_deterministic_operation_specs(
                ontology_dir,
                require_lock=True,
                verify_integrity=False,
            )
        except DeterministicPackageError as exc:
            return Failed(exc.code, str(exc), internal_detail=str(exc))
        if not specs:
            return NotApplicable("no_deterministic_operations")

        try:
            # Routing metadata is itself executable control data. Verify every
            # deterministic operation descriptor before using it to decide
            # that a request does not belong to the deterministic branch.
            verify_deterministic_package_lock(
                ontology_dir,
                critical_paths={spec.operation_path for spec in specs},
                cancel_event=cancel_event,
            )
        except DeterministicPackageError as exc:
            return Failed(exc.code, str(exc), internal_detail=str(exc))

        selected = self._select_operation(request, specs, ontology_dir)
        if isinstance(selected, (NotApplicable, Failed, AwaitingInput, Completed)):
            return selected
        spec = selected
        try:
            verify_deterministic_package_lock(
                ontology_dir,
                cancel_event=cancel_event,
            )
        except DeterministicPackageError as exc:
            return Failed(exc.code, str(exc), internal_detail=str(exc))
        if callable(observer):
            observer(
                {
                    "type": "selected",
                    "operation_key": spec.operation_key,
                }
            )
        if not enabled:
            return Failed(
                "feature_disabled",
                "Deterministic execution is disabled.",
                operation_key=spec.operation_key,
            )
        if not isinstance(request.arguments, Mapping):
            return Failed(
                "invalid_arguments",
                "Deterministic arguments must be an object.",
                operation_key=spec.operation_key,
            )
        try:
            canonical_json_bytes(dict(request.arguments))
        except (TypeError, ValueError, OverflowError):
            return Failed(
                "invalid_arguments",
                "Deterministic arguments must contain standard JSON values.",
                operation_key=spec.operation_key,
            )

        script_entry = load_script_entry_from_descriptor(
            ontology_dir,
            spec.handler.descriptor_path,
        )
        if not self._handler_descriptor_matches_spec(
            script_entry,
            ontology_dir=ontology_dir,
            spec=spec,
        ):
            return Failed(
                "package_integrity_mismatch",
                "The deterministic handler descriptor is unavailable.",
                operation_key=spec.operation_key,
            )
        script_parameters = protect_deterministic_runtime_parameters(
            list(script_entry.get("parameters") or [])
        )
        script_args = protect_deterministic_script_args(
            list(script_entry.get("arguments") or [])
        )
        runtime_argument_keys = frozenset(
            extract_runtime_argument_keys(
                script_args=script_args,
                script_parameters=script_parameters,
            )
        )
        request_runtime_fields = _runtime_owned_argument_names(
            request.arguments,
            runtime_argument_keys,
        )
        if request_runtime_fields:
            return Failed(
                "invalid_arguments",
                "Runtime-owned deterministic arguments cannot be supplied by the request.",
                operation_key=spec.operation_key,
                internal_detail=",".join(request_runtime_fields),
            )

        try:
            input_schema = self._load_json(ontology_dir / spec.input_schema_path)
        except (OSError, ValueError) as exc:
            return Failed(
                "package_integrity_mismatch",
                "The deterministic package changed before execution.",
                operation_key=spec.operation_key,
                internal_detail=type(exc).__name__,
            )

        try:
            current_runtime_revision = runtime_revision()
        except Exception as exc:
            return Failed(
                "runtime_revision_unavailable",
                "Deterministic runtime revision could not be verified.",
                operation_key=spec.operation_key,
                internal_detail=type(exc).__name__,
            )
        provider_binding: Optional[DeterministicProviderBinding] = None
        provider_dependency: Optional[LockedDependency] = None
        if spec.handler.data_access == "workspace_provider":
            try:
                provider_binding = resolve_deterministic_provider_binding(
                    workspace_id=request.runtime_context.workspace_id,
                    ontology_id=ontology.id,
                    ontology_version=ontology.version,
                )
            except DeterministicProviderError as exc:
                if request.checkpoint:
                    return Failed(
                        "stale_checkpoint",
                        "Deterministic provider no longer matches the pending input.",
                        operation_key=spec.operation_key,
                        internal_detail=exc.code,
                    )
                return Failed(
                    exc.code,
                    str(exc),
                    operation_key=spec.operation_key,
                    internal_detail=str(exc),
                )
            provider_dependency = LockedDependency(
                kind="provider",
                ref=provider_binding.source_ref,
                revision=provider_binding.data_revision,
                scope_digest=provider_binding.scope_digest,
            )
        checkpoint_arguments: dict[str, Any] = {}
        if request.checkpoint:
            checkpoint_error = self._validate_checkpoint(
                request.checkpoint,
                spec=spec,
                ontology_id=ontology.id,
                ontology_version=ontology.version,
                current_runtime_revision=current_runtime_revision,
                provider_dependency=provider_dependency,
                runtime_argument_keys=runtime_argument_keys,
            )
            if checkpoint_error is not None:
                return checkpoint_error
            checkpoint_arguments.update(dict(request.checkpoint.get("arguments") or {}))
            response_error = self._merge_interaction_response(
                checkpoint_arguments,
                checkpoint=request.checkpoint,
                interaction_response=request.interaction_response,
                input_schema=input_schema,
            )
            if response_error is not None:
                return response_error

        parser_arguments: dict[str, Any] = {}
        # A checkpoint already contains the accepted parser output. Re-running
        # the parser on a short interaction reply could reject the resume or
        # overwrite the value the user just confirmed.
        if spec.parser_path and not request.checkpoint:
            parser_result = self._run_parser(
                spec=spec,
                ontology_dir=ontology_dir,
                ontology_id=ontology.id,
                ontology_version=ontology.version,
                project_root=Path(project_root),
                request=request,
                known_arguments={
                    **checkpoint_arguments,
                    **dict(request.arguments),
                },
                runtime_argument_keys=runtime_argument_keys,
                cancel_event=cancel_event,
            )
            if isinstance(parser_result, Failed):
                return parser_result
            runtime_failure = self._runtime_revision_failure(
                current_runtime_revision,
                operation_key=spec.operation_key,
            )
            if runtime_failure is not None:
                return runtime_failure
            parser_arguments = dict(parser_result.get("arguments") or {})
            known_argument_names = set(checkpoint_arguments) | set(request.arguments)
            issues = []
            for issue in parser_result.get("issues") or []:
                if isinstance(issue, Mapping):
                    field = str(issue.get("field") or "").strip()
                    if field in known_argument_names:
                        continue
                issues.append(issue)
            if issues:
                return self._await_for_issue(
                    issue=issues[0],
                    remaining_issues=issues[1:],
                    request=request,
                    spec=spec,
                    ontology_id=ontology.id,
                    ontology_version=ontology.version,
                    arguments={
                        **checkpoint_arguments,
                        **parser_arguments,
                        **dict(request.arguments),
                    },
                    current_runtime_revision=current_runtime_revision,
                    provider_dependency=provider_dependency,
                )

        arguments = {
            **checkpoint_arguments,
            **parser_arguments,
            **dict(request.arguments or {}),
        }
        pending_parser_issues = (
            list(request.checkpoint.get("pending_parser_issues") or [])
            if request.checkpoint
            else []
        )
        unresolved_parser_issues = []
        for issue in pending_parser_issues:
            if isinstance(issue, Mapping):
                field = str(issue.get("field") or "").strip()
                if field in request.arguments:
                    continue
            unresolved_parser_issues.append(issue)
        if unresolved_parser_issues:
            return self._await_for_issue(
                issue=unresolved_parser_issues[0],
                remaining_issues=unresolved_parser_issues[1:],
                request=request,
                spec=spec,
                ontology_id=ontology.id,
                ontology_version=ontology.version,
                arguments=arguments,
                current_runtime_revision=current_runtime_revision,
                provider_dependency=provider_dependency,
            )
        try:
            canonical_json_bytes(arguments)
        except (TypeError, ValueError, OverflowError):
            return Failed(
                "invalid_arguments",
                "Deterministic arguments must contain standard JSON values.",
                operation_key=spec.operation_key,
            )
        try:
            output_schema = self._load_json(ontology_dir / spec.output_schema_path)
        except (OSError, ValueError) as exc:
            return Failed(
                "package_integrity_mismatch",
                "The deterministic package changed before execution.",
                operation_key=spec.operation_key,
                internal_detail=type(exc).__name__,
            )
        schema_argument_names = self._root_schema_argument_names(input_schema)
        runtime_schema_fields = sorted(
            schema_argument_names.intersection(runtime_argument_keys),
            key=lambda item: item.encode("utf-8"),
        )
        if runtime_schema_fields:
            return Failed(
                "execution_schema_invalid",
                "The deterministic input schema declares runtime-owned arguments.",
                operation_key=spec.operation_key,
                internal_detail=",".join(runtime_schema_fields),
            )
        try:
            input_errors = sorted(
                self._schema_validator(input_schema).iter_errors(arguments),
                key=lambda item: list(item.absolute_path),
            )
        except Exception as exc:
            return Failed(
                "execution_schema_invalid",
                "The deterministic input schema could not be resolved.",
                operation_key=spec.operation_key,
                internal_detail=type(exc).__name__,
            )
        missing = self._missing_required(input_errors)
        if missing:
            return self._await_for_missing(
                missing=missing,
                schema=input_schema,
                request=request,
                spec=spec,
                ontology_id=ontology.id,
                ontology_version=ontology.version,
                arguments=arguments,
                current_runtime_revision=current_runtime_revision,
                provider_dependency=provider_dependency,
            )
        if input_errors:
            return Failed(
                "input_schema_violation",
                "Operation arguments do not satisfy the input schema.",
                operation_key=spec.operation_key,
                internal_detail=self._schema_error_detail(input_errors[0]),
            )

        cli_args, missing_cli, cli_error = build_direct_cli_args(
            script_args=script_args,
            script_parameters=script_parameters,
            business_arguments=arguments,
            runtime_context=request.runtime_context,
            canonicalize_structured=True,
        )
        if missing_cli:
            return Failed(
                "input_schema_violation",
                "Handler parameters and the input schema are inconsistent.",
                operation_key=spec.operation_key,
                internal_detail="Missing handler parameters: {}".format(", ".join(missing_cli)),
            )
        if cli_error:
            return Failed(
                "input_schema_violation",
                "Handler arguments could not be constructed.",
                operation_key=spec.operation_key,
                internal_detail=cli_error,
            )

        dependencies = [provider_dependency] if provider_dependency is not None else []
        provider_secrets = self._provider_secret_values(provider_binding)
        if any(
            len(secret) < MIN_DETERMINISTIC_PROVIDER_SECRET_LENGTH
            for secret in provider_secrets
        ):
            return Failed(
                "provider_credentials_unsafe",
                "Deterministic provider credentials do not meet the minimum length.",
                operation_key=spec.operation_key,
            )
        provider_protected_values = self._provider_protected_values(provider_binding)
        provider_network_endpoint = None
        provider_network_domain_endpoint = None
        sandbox_provider_connection = None
        if provider_binding is not None:
            provider_network_endpoint = provider_binding.network_endpoint
            sandbox_provider_connection = provider_binding.effective_connection
            if load_sandbox_config().enabled:
                # Resolve once on the trusted host for the run-scoped egress
                # policy. Clear-text protocols may connect to that pinned IP;
                # TLS protocols keep the original hostname for SNI and
                # certificate verification.
                try:
                    provider_network_endpoint = sandbox_reachable_url(
                        provider_binding.network_endpoint
                    )
                except Exception as exc:
                    return Failed(
                        "provider_endpoint_unresolved",
                        "Deterministic provider endpoint could not be pinned to an IP address.",
                        operation_key=spec.operation_key,
                        internal_detail=type(exc).__name__,
                    )
                sandbox_provider_connection = replace(
                    provider_binding.effective_connection,
                    endpoint=_sandbox_provider_connection_endpoint(
                        provider_binding.effective_connection.endpoint,
                        provider_network_endpoint,
                    ),
                )
                if (
                    urlparse(provider_binding.effective_connection.endpoint)
                    .scheme.strip()
                    .casefold()
                    in _TLS_ENDPOINT_SCHEMES
                ):
                    provider_network_domain_endpoint = (
                        provider_binding.effective_connection.endpoint
                    )
        env_overrides = self._fixed_environment(
            ontology_id=ontology.id,
            ontology_version=ontology.version,
            package_revision=spec.package_revision,
        )
        if sandbox_provider_connection is not None:
            env_overrides[SCRIPT_BUSINESS_DATA_CONNECTION_ENV] = (
                serialize_script_business_data_connection(
                    connection=sandbox_provider_connection,
                )
            )

        try:
            artifact_staging_parent = self._artifact_staging_parent()
            self._cleanup_stale_artifact_staging(artifact_staging_parent)
        except (OSError, ValueError) as exc:
            return Failed(
                "deterministic_isolation_unavailable",
                "Deterministic artifact staging is unavailable.",
                operation_key=spec.operation_key,
                internal_detail=str(exc),
            )
        started_at = time.monotonic()
        try:
            artifact_staging = tempfile.TemporaryDirectory(
                prefix="deterministic-artifact-staging-",
                dir=(
                    str(artifact_staging_parent)
                    if artifact_staging_parent is not None
                    else None
                ),
            )
        except OSError as exc:
            return Failed(
                "deterministic_isolation_unavailable",
                "Deterministic artifact staging is unavailable.",
                operation_key=spec.operation_key,
                internal_detail=type(exc).__name__,
            )
        with artifact_staging as artifact_staging_dir:
            # The sandbox identity may differ from the host process identity.
            # Keep the discoverable parent non-listable, expose only a
            # cryptographically unguessable child with write/traverse access,
            # then freeze that child before inspecting any output.
            os.chmod(artifact_staging_dir, 0o711)
            artifact_root = Path(artifact_staging_dir) / (
                "run-{}".format(secrets.token_hex(32))
            )
            artifact_root.mkdir(mode=0o733)
            os.chmod(artifact_root, 0o733)
            env_overrides["RUN_ARTIFACT_DIR"] = str(artifact_root)
            env_overrides["RUN_ARTIFACT_MAX_FILE_BYTES"] = str(MAX_ARTIFACT_BYTES)
            env_overrides["RUN_ARTIFACT_MAX_TOTAL_BYTES"] = str(
                MAX_ARTIFACT_TOTAL_BYTES
            )
            env_overrides["RUN_ARTIFACT_MAX_COUNT"] = str(MAX_ARTIFACT_COUNT)
            sandbox_release_error: Optional[Exception] = None
            artifact_freeze_error: Optional[Exception] = None
            artifact_monitor_stop = threading.Event()
            artifact_limit_cancel = threading.Event()
            artifact_monitor_failures: list[Failed] = []

            def monitor_artifacts() -> None:
                next_lease_refresh = 0.0
                while not artifact_monitor_stop.is_set():
                    now = time.monotonic()
                    if now >= next_lease_refresh:
                        try:
                            self._refresh_artifact_staging_lease(
                                Path(artifact_staging_dir)
                            )
                        except (OSError, NotImplementedError) as exc:
                            artifact_monitor_failures.append(
                                Failed(
                                    "deterministic_isolation_unavailable",
                                    "Deterministic artifact staging lease could not be refreshed.",
                                    operation_key=spec.operation_key,
                                    internal_detail=type(exc).__name__,
                                )
                            )
                            artifact_limit_cancel.set()
                            return
                        next_lease_refresh = (
                            now + ARTIFACT_STAGING_HEARTBEAT_SECONDS
                        )
                    _paths, failure = self._inspect_artifact_tree(artifact_root)
                    if failure is not None:
                        artifact_monitor_failures.append(failure)
                        artifact_limit_cancel.set()
                        return
                    artifact_monitor_stop.wait(ARTIFACT_MONITOR_INTERVAL_SECONDS)

            artifact_monitor = threading.Thread(
                target=monitor_artifacts,
                name="artifact-monitor-{}".format(request.runtime_context.run_id),
                daemon=True,
            )
            artifact_monitor.start()
            try:
                handler_output = self._run_script(
                    script_relative=spec.handler.entrypoint,
                    cli_args=cli_args,
                    ontology_dir=ontology_dir,
                    project_root=Path(project_root),
                    request=request,
                    timeout_seconds=timeout_seconds,
                    env_overrides=env_overrides,
                    caller="deterministic_service",
                    stdout_limit_bytes=HANDLER_STDOUT_LIMIT,
                    cancel_event=_AnyCancelEvent(
                        cancel_event,
                        artifact_limit_cancel,
                    ),
                    sandbox_prepare_data_connection=False,
                    sandbox_network_endpoint=provider_network_endpoint,
                    sandbox_network_domain_endpoint=(
                        provider_network_domain_endpoint
                    ),
                    sandbox_writable_paths=[artifact_root],
                    file_size_limit_bytes=MAX_ARTIFACT_BYTES,
                )
            finally:
                artifact_monitor_stop.set()
                artifact_monitor.join()
                # Destroy the run cgroup before inspecting authoritative output or
                # artifacts. This also terminates descendants that escaped the
                # bounded runner's process group by creating a new session.
                try:
                    release_run_sandbox(
                        request.runtime_context.run_id,
                        strict=True,
                    )
                except Exception as exc:
                    sandbox_release_error = exc
                try:
                    os.chmod(artifact_root, 0o700)
                except Exception as exc:
                    artifact_freeze_error = exc
            if sandbox_release_error is not None or artifact_freeze_error is not None:
                isolation_error = sandbox_release_error or artifact_freeze_error
                return Failed(
                    "deterministic_isolation_unavailable",
                    "Deterministic sandbox output could not be isolated safely.",
                    operation_key=spec.operation_key,
                    handler_execution_count=1,
                    internal_detail=type(isolation_error).__name__,
                )
            runtime_failure = self._runtime_revision_failure(
                current_runtime_revision,
                operation_key=spec.operation_key,
                handler_execution_count=1,
            )
            if runtime_failure is not None:
                return runtime_failure
            if artifact_monitor_failures:
                failure = artifact_monitor_failures[0]
                return Failed(
                    failure.code,
                    failure.message,
                    operation_key=spec.operation_key,
                    handler_execution_count=1,
                    internal_detail=failure.internal_detail,
                )
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            if handler_output.startswith("Error:"):
                normalized_error = handler_output.casefold()
                if "timed out" in normalized_error:
                    code = "handler_timeout"
                elif "output exceeds" in normalized_error:
                    code = "handler_output_too_large"
                elif "artifact" in normalized_error and "exceeds limit" in normalized_error:
                    code = "artifact_policy_violation"
                elif "sandbox" in normalized_error:
                    code = "deterministic_isolation_unavailable"
                elif "cancelled" in normalized_error:
                    code = "execution_cancelled"
                else:
                    code = "handler_failed"
                return Failed(
                    code,
                    "Deterministic handler execution failed.",
                    operation_key=spec.operation_key,
                    handler_execution_count=1,
                    internal_detail=self._redact_secret_values(
                        handler_output,
                        provider_protected_values,
                    ),
                )
            if len(handler_output.encode("utf-8")) > HANDLER_STDOUT_LIMIT:
                return Failed(
                    "handler_output_too_large",
                    "Deterministic handler output exceeds the size limit.",
                    operation_key=spec.operation_key,
                    handler_execution_count=1,
                )
            try:
                structured_result = self._strict_json_loads(handler_output)
            except ValueError as exc:
                return Failed(
                    "invalid_json_output",
                    "Deterministic handler did not return valid JSON.",
                    operation_key=spec.operation_key,
                    handler_execution_count=1,
                    internal_detail=self._redact_secret_values(
                        str(exc),
                        provider_protected_values,
                    ),
                )
            if not isinstance(structured_result, dict):
                return Failed(
                    "invalid_json_output",
                    "Deterministic handler JSON root must be an object.",
                    operation_key=spec.operation_key,
                    handler_execution_count=1,
                )
            if self._value_contains_secret(
                structured_result,
                provider_protected_values,
            ):
                return Failed(
                    "secret_output_violation",
                    "Deterministic handler output contains protected provider credentials.",
                    operation_key=spec.operation_key,
                    handler_execution_count=1,
                )
            try:
                output_errors = sorted(
                    self._schema_validator(output_schema).iter_errors(structured_result),
                    key=lambda item: list(item.absolute_path),
                )
            except Exception as exc:
                return Failed(
                    "execution_schema_invalid",
                    "The deterministic output schema could not be resolved.",
                    operation_key=spec.operation_key,
                    handler_execution_count=1,
                    internal_detail=type(exc).__name__,
                )
            if output_errors:
                return Failed(
                    "output_schema_violation",
                    "Handler output does not satisfy the operation output schema.",
                    operation_key=spec.operation_key,
                    handler_execution_count=1,
                    internal_detail=self._schema_error_detail(output_errors[0]),
                )
            try:
                verify_deterministic_package_lock(
                    ontology_dir,
                    cancel_event=cancel_event,
                )
            except DeterministicPackageError as exc:
                public_message = (
                    "Deterministic execution was cancelled."
                    if exc.code == "execution_cancelled"
                    else "The deterministic package changed during execution."
                )
                return Failed(
                    exc.code,
                    public_message,
                    operation_key=spec.operation_key,
                    handler_execution_count=1,
                    internal_detail=str(exc),
                )
            artifact_result = self._collect_artifacts(
                artifact_root,
                writer=artifact_writer,
                forbidden_values=provider_protected_values,
            )
            if isinstance(artifact_result, Failed):
                return Failed(
                    artifact_result.code,
                    artifact_result.message,
                    operation_key=spec.operation_key,
                    handler_execution_count=1,
                    internal_detail=artifact_result.internal_detail,
                )

        try:
            envelope = build_result_envelope(
                structured_result=structured_result,
                artifacts=artifact_result,
                ontology_id=ontology.id,
                ontology_version=ontology.version,
                operation_key=spec.operation_key,
                package_revision=spec.package_revision,
                handler_ref=spec.handler.script_key,
                handler_sha256=spec.handler.sha256,
                dependencies=dependencies,
                arguments=arguments,
                elapsed_ms=elapsed_ms,
                runtime_revision_value=current_runtime_revision,
            )
        except (TypeError, ValueError) as exc:
            self._rollback_persisted_artifacts(artifact_writer, artifact_result)
            return Failed(
                "artifact_policy_violation",
                "Deterministic result envelope could not be constructed.",
                operation_key=spec.operation_key,
                handler_execution_count=1,
                internal_detail=str(exc),
            )
        return Completed(
            envelope=envelope,
            operation_key=spec.operation_key,
            elapsed_ms=elapsed_ms,
        )

    @staticmethod
    def _runtime_revision_failure(
        expected: str,
        *,
        operation_key: str,
        handler_execution_count: int = 0,
    ) -> Optional[Failed]:
        """Reject one execution if its attested runtime changes mid-flight."""
        try:
            current = runtime_revision()
        except Exception as exc:
            return Failed(
                "runtime_revision_unavailable",
                "Deterministic runtime revision could not be verified.",
                operation_key=operation_key,
                handler_execution_count=handler_execution_count,
                internal_detail=type(exc).__name__,
            )
        if current == expected:
            return None
        return Failed(
            "runtime_revision_changed",
            "Deterministic runtime changed during execution.",
            operation_key=operation_key,
            handler_execution_count=handler_execution_count,
        )

    def is_potentially_applicable(self, request: DeterministicRequest) -> bool:
        """Perform a read-only preflight used to defer LLM configuration.

        The authoritative validation still happens in :meth:`execute`.  A
        positive answer means the request must enter that branch first; it does
        not mean execution will succeed.
        """
        ontology_id = str(
            request.runtime_context.selected_ontology_id
            or request.context.get("selected_ontology_id")
            or ""
        ).strip()
        if not ontology_id:
            return False
        manifest_kwargs: dict[str, Any] = {
            "workspace_id": request.runtime_context.workspace_id,
        }
        ontology_version = str(
            request.runtime_context.selected_ontology_version
            or request.context.get("selected_ontology_version")
            or request.context.get("selected_version")
            or ""
        ).strip()
        if ontology_version:
            manifest_kwargs["version"] = ontology_version
        ontology, _error = get_ontology_manifest(ontology_id, **manifest_kwargs)
        if ontology is None:
            return False
        ontology_dir = Path(ontology.ontology_dir).resolve()
        try:
            specs = load_deterministic_operation_specs(
                ontology_dir,
                require_lock=True,
                # Preflight only decides whether LLM setup can be deferred. The
                # authoritative execute path performs the complete hash check.
                verify_integrity=False,
            )
        except DeterministicPackageError:
            # Package errors must reach the authoritative execute path even for
            # natural-language requests. Returning False here would turn a
            # malformed deterministic package into an unsafe legacy fallback.
            return True
        if not specs:
            return False
        try:
            # A negative routing decision is trusted only after every
            # descriptor that contributed to it has been checked against the
            # installed lock. Otherwise tampering could erase the matching
            # route and silently send the request to the legacy Agent path.
            verify_deterministic_package_lock(
                ontology_dir,
                critical_paths={spec.operation_path for spec in specs},
            )
        except DeterministicPackageError:
            return True
        selected = self._select_operation(request, specs, ontology_dir)
        return not isinstance(selected, NotApplicable)

    def _select_operation(
        self,
        request: DeterministicRequest,
        specs: tuple[DeterministicOperationSpec, ...],
        ontology_dir: Path,
    ) -> DeterministicOperationSpec | DeterministicOutcome:
        by_token = {
            normalize_operation_token(token): spec
            for spec in specs
            for token in (spec.operation_key, *spec.aliases)
        }
        checkpoint_key = str((request.checkpoint or {}).get("operation_key") or "").strip()
        requested_key = str(request.context.get("requested_operation_key") or "").strip()
        if request.checkpoint is not None and not checkpoint_key:
            return Failed(
                "stale_checkpoint",
                "Deterministic checkpoint does not identify an operation.",
            )
        if checkpoint_key:
            checkpoint_spec = by_token.get(normalize_operation_token(checkpoint_key))
            if checkpoint_spec is None:
                return Failed(
                    "stale_checkpoint",
                    "Deterministic checkpoint operation is no longer available.",
                    operation_key=checkpoint_key,
                )
            requested_spec = (
                by_token.get(normalize_operation_token(requested_key))
                if requested_key
                else None
            )
            if requested_key and requested_spec is not checkpoint_spec:
                return Failed("stale_checkpoint", "The requested operation changed.")
            return checkpoint_spec
        if requested_key:
            spec = by_token.get(normalize_operation_token(requested_key))
            if spec is not None:
                return spec
            legacy_path, _error = find_markdown_file(
                ontology_dir / "operations",
                requested_key,
                "operation",
            )
            if legacy_path is not None:
                return NotApplicable("explicit_legacy_operation")
            return Failed(
                "operation_not_found",
                "Requested operation was not found.",
                operation_key=requested_key,
            )

        token = normalize_operation_token(request.user_request)
        matches: list[DeterministicOperationSpec] = []
        if token:
            for spec in specs:
                if self._request_matches_spec(request.user_request, spec):
                    matches.append(spec)
                    continue
                if self._request_matches_declared_routing(request.user_request, spec):
                    matches.append(spec)
        if not matches:
            return NotApplicable("no_deterministic_match")
        if len(matches) > 1:
            return Failed("operation_ambiguous", "Multiple deterministic operations matched.")
        return matches[0]

    @staticmethod
    def _request_matches_declared_routing(
        user_request: str,
        spec: DeterministicOperationSpec,
    ) -> bool:
        """Match a package-declared all-terms/any-terms routing policy."""
        if not spec.routing_all_terms and not spec.routing_any_terms:
            return False
        normalized_request = normalize_operation_token(user_request)
        if not normalized_request or _NON_EXECUTION_MARKER.search(normalized_request):
            return False
        all_terms = tuple(
            normalize_operation_token(term)
            for term in spec.routing_all_terms
            if normalize_operation_token(term)
        )
        any_terms = tuple(
            normalize_operation_token(term)
            for term in spec.routing_any_terms
            if normalize_operation_token(term)
        )
        return (
            all(term in normalized_request for term in all_terms)
            and (not any_terms or any(term in normalized_request for term in any_terms))
        )

    @staticmethod
    def _request_matches_spec(
        user_request: str,
        spec: DeterministicOperationSpec,
    ) -> bool:
        """Match a declared token only in a deterministic command position.

        This is intentionally a small deterministic matcher, not an intent
        classifier. Exact tokens, command-prefixed tokens and tokens followed by
        an argument-shaped suffix are accepted. Explanations, quotations and
        negated mentions fall through to Agent reasoning.
        """
        normalized_request = unicodedata.normalize("NFKC", str(user_request or "")).casefold()
        normalized_request = " ".join(normalized_request.split())
        if _NON_EXECUTION_MARKER.search(normalized_request):
            return False
        for raw_token in (spec.operation_key, *spec.aliases):
            candidate = unicodedata.normalize("NFKC", str(raw_token or "")).casefold()
            candidate = " ".join(candidate.split())
            if not candidate:
                continue
            if normalized_request == candidate:
                return True
            escaped = re.escape(candidate)
            for match in re.finditer(
                r"(?<![\w]){}(?![\w])".format(escaped),
                normalized_request,
            ):
                prefix = normalized_request[: match.start()].strip()
                suffix = normalized_request[match.end() :].strip()
                previous = normalized_request[match.start() - 1] if match.start() else ""
                following = (
                    normalized_request[match.end()]
                    if match.end() < len(normalized_request)
                    else ""
                )
                if previous in _OPEN_QUOTES or following in _CLOSE_QUOTES:
                    continue
                if prefix and _IMPLICIT_COMMAND_PREFIX.fullmatch(prefix):
                    return True
                if (
                    not prefix
                    and suffix
                    and DeterministicExecutionService._has_inline_arguments(suffix)
                ):
                    return True
        return False

    @staticmethod
    def _has_inline_arguments(suffix: str) -> bool:
        """Require content after the operation token to have an argument shape.

        Besides labelled/key-value input, deterministic parsers commonly accept
        command-style positional values such as ``2028-02 [...]``. Only an
        unambiguous year-month or JSON-array prefix is accepted here;
        ordinary explanatory prose still falls through to Agent reasoning.
        """
        candidate = str(suffix or "").strip()
        separator = _INLINE_ARGUMENT_SEPARATOR.match(candidate)
        if separator is not None:
            candidate = candidate[separator.end() :].strip()
        if candidate and _INLINE_POSITIONAL_ARGUMENT_PREFIX.match(candidate):
            return True
        group = _INLINE_ARGUMENT_GROUP.match(candidate)
        if group is not None:
            candidate = candidate[group.end() :].strip()
        return bool(candidate and _INLINE_ARGUMENT_PREFIX.match(candidate))

    def _run_parser(
        self,
        *,
        spec: DeterministicOperationSpec,
        ontology_dir: Path,
        ontology_id: str,
        ontology_version: str,
        project_root: Path,
        request: DeterministicRequest,
        known_arguments: Mapping[str, Any],
        runtime_argument_keys: frozenset[str],
        cancel_event: Any = None,
    ) -> dict[str, Any] | Failed:
        payload = {
            "text": request.user_request,
            "known_arguments": dict(known_arguments),
        }
        parser_sandbox_run_id = "det-parser-{}".format(secrets.token_hex(32))
        try:
            register_run_sandbox_companion(
                request.runtime_context.run_id,
                parser_sandbox_run_id,
            )
        except Exception as exc:
            return Failed(
                "deterministic_isolation_unavailable",
                "Deterministic parser sandbox could not be reserved safely.",
                operation_key=spec.operation_key,
                internal_detail=type(exc).__name__,
            )
        sandbox_release_error: Optional[Exception] = None
        try:
            output = self._run_script(
                script_relative=str(spec.parser_path),
                cli_args=["--request-json", canonical_json_bytes(payload).decode("utf-8")],
                ontology_dir=ontology_dir,
                project_root=project_root,
                request=request,
                timeout_seconds=5,
                env_overrides=self._fixed_environment(
                    ontology_id=ontology_id,
                    ontology_version=ontology_version,
                    package_revision=spec.package_revision,
                ),
                caller="deterministic_parser",
                stdout_limit_bytes=PARSER_STDOUT_LIMIT,
                cancel_event=cancel_event,
                sandbox_prepare_data_connection=False,
                sandbox_include_data_connection_env=False,
                sandbox_run_id=parser_sandbox_run_id,
            )
        finally:
            try:
                release_run_sandbox(parser_sandbox_run_id, strict=True)
            except Exception as exc:
                sandbox_release_error = exc
        if sandbox_release_error is not None:
            return Failed(
                "deterministic_isolation_unavailable",
                "Deterministic parser sandbox could not be terminated safely.",
                operation_key=spec.operation_key,
                internal_detail=type(sandbox_release_error).__name__,
            )
        if output.startswith("Error:"):
            normalized_error = output.casefold()
            if "timed out" in normalized_error:
                code = "parser_timeout"
            elif "output exceeds" in normalized_error:
                code = "parser_invalid_output"
            elif "sandbox" in normalized_error:
                code = "deterministic_isolation_unavailable"
            elif "cancelled" in normalized_error:
                code = "execution_cancelled"
            else:
                code = "parser_failed"
            return Failed(
                code,
                "Deterministic parser execution failed.",
                operation_key=spec.operation_key,
                internal_detail=output,
            )
        if len(output.encode("utf-8")) > PARSER_STDOUT_LIMIT:
            return Failed(
                "parser_invalid_output",
                "Deterministic parser output exceeds the size limit.",
                operation_key=spec.operation_key,
            )
        try:
            parsed = self._strict_json_loads(output)
        except ValueError as exc:
            return Failed(
                "parser_invalid_output",
                "Deterministic parser did not return valid JSON.",
                operation_key=spec.operation_key,
                internal_detail=str(exc),
            )
        if not isinstance(parsed, dict):
            return Failed(
                "parser_invalid_output",
                "Deterministic parser JSON root must be an object.",
                operation_key=spec.operation_key,
            )
        required_fields = {"matched", "arguments", "issues"}
        unknown = set(parsed) - {"matched", "arguments", "issues"}
        invalid_contract = bool(unknown or not required_fields.issubset(parsed))
        if not invalid_contract:
            invalid_contract = not isinstance(
                parsed.get("matched"), bool
            ) or not isinstance(parsed.get("arguments"), dict)
        if not invalid_contract:
            invalid_contract = not isinstance(parsed.get("issues"), list)
        if invalid_contract:
            return Failed(
                "parser_invalid_output",
                "Deterministic parser output contract is invalid.",
                operation_key=spec.operation_key,
            )
        parser_runtime_fields = _runtime_owned_argument_names(
            parsed["arguments"],
            runtime_argument_keys,
        )
        for issue in parsed["issues"]:
            if not isinstance(issue, Mapping):
                continue
            field = str(issue.get("field") or "").strip()
            if field in runtime_argument_keys:
                parser_runtime_fields.append(field)
        parser_runtime_fields = sorted(
            set(parser_runtime_fields),
            key=lambda item: item.encode("utf-8"),
        )
        if parser_runtime_fields:
            return Failed(
                "parser_invalid_output",
                "Deterministic parser returned runtime-owned arguments.",
                operation_key=spec.operation_key,
                internal_detail=",".join(parser_runtime_fields),
            )
        if parsed["matched"] is not True:
            return Failed(
                "parser_input_not_applicable",
                "The selected deterministic parser rejected the input.",
                operation_key=spec.operation_key,
            )
        return parsed

    @staticmethod
    def _run_script(
        *,
        script_relative: str,
        cli_args: list[str],
        ontology_dir: Path,
        project_root: Path,
        request: DeterministicRequest,
        timeout_seconds: int,
        env_overrides: Mapping[str, str],
        caller: str,
        stdout_limit_bytes: int,
        cancel_event: Any = None,
        sandbox_prepare_data_connection: bool = False,
        sandbox_include_data_connection_env: bool = True,
        sandbox_network_endpoint: Optional[str] = None,
        sandbox_network_domain_endpoint: Optional[str] = None,
        sandbox_writable_paths: list[Path] | None = None,
        sandbox_run_id: Optional[str] = None,
        file_size_limit_bytes: Optional[int] = None,
    ) -> str:
        try:
            script_path = (ontology_dir / script_relative).resolve()
            relative = script_path.relative_to(project_root.resolve()).as_posix()
        except ValueError:
            return "Error: Deterministic script is outside the configured project root."
        return handle_execute_script_path(
            script_path=relative,
            args=cli_args,
            base_path=project_root,
            timeout_seconds=max(1, int(timeout_seconds)),
            tool_call_id="deterministic",
            caller=caller,
            run_id=request.runtime_context.run_id,
            session_id=request.runtime_context.session_id,
            turn_id=request.runtime_context.turn_id,
            workspace_id=request.runtime_context.workspace_id,
            ontology_id=request.runtime_context.selected_ontology_id,
            ontology_dir=ontology_dir,
            extra_pythonpath_dirs=[ontology_dir],
            env_overrides=env_overrides,
            clean_env=True,
            include_stderr_on_success=False,
            stdout_limit_bytes=stdout_limit_bytes,
            stderr_limit_bytes=SCRIPT_STDERR_LIMIT,
            cancel_event=cancel_event,
            sandbox_prepare_data_connection=sandbox_prepare_data_connection,
            sandbox_include_data_connection_env=sandbox_include_data_connection_env,
            sandbox_network_endpoint=sandbox_network_endpoint,
            sandbox_network_domain_endpoint=sandbox_network_domain_endpoint,
            sandbox_writable_paths=tuple(sandbox_writable_paths or ()),
            sandbox_run_id=sandbox_run_id,
            file_size_limit_bytes=file_size_limit_bytes,
            sandbox_required=True,
            sandbox_restrict_filesystem_to_ontology=True,
        ).strip()

    @staticmethod
    def _fixed_environment(
        *,
        ontology_id: str,
        ontology_version: str,
        package_revision: str,
    ) -> dict[str, str]:
        return {
            "PYTHONHASHSEED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TZ": "UTC",
            "LC_ALL": "C.UTF-8",
            "LANG": "C.UTF-8",
            "DETERMINISTIC_EXECUTION": "1",
            "ONTOLOGY_ID": ontology_id,
            "ONTOLOGY_VERSION": ontology_version,
            "PACKAGE_REVISION": package_revision,
        }

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any]:
        value = DeterministicExecutionService._strict_json_loads(
            path.read_text(encoding="utf-8")
        )
        if not isinstance(value, dict):
            raise ValueError("JSON document root must be an object")
        return value

    @staticmethod
    def _strict_json_loads(text: str) -> Any:
        def reject_constant(value: str) -> None:
            raise ValueError("Non-standard JSON constant: {}".format(value))

        def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate JSON object key: {}".format(key))
                result[key] = value
            return result

        return json.loads(
            text,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicate_keys,
        )

    @staticmethod
    def _schema_validator(schema: Mapping[str, Any]) -> Draft202012Validator:
        return Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
            registry=_SCHEMA_REGISTRY,
        )

    @staticmethod
    def _schema_error_detail(error: Any) -> str:
        """Describe a schema failure without copying the rejected value."""
        instance_path = "/".join(str(item) for item in error.absolute_path) or "<root>"
        schema_path = "/".join(str(item) for item in error.absolute_schema_path) or "<root>"
        return "validator={}; path={}; schema_path={}".format(
            str(error.validator or "unknown"),
            instance_path,
            schema_path,
        )

    @staticmethod
    def _handler_descriptor_matches_spec(
        script_entry: Any,
        *,
        ontology_dir: Path,
        spec: DeterministicOperationSpec,
    ) -> bool:
        if not isinstance(script_entry, Mapping):
            return False
        if not bool(script_entry.get("_is_bound_operation")):
            return False
        if str(script_entry.get("script_key") or "").strip() != spec.handler.script_key:
            return False
        if normalize_operation_token(script_entry.get("operation_key")) != normalize_operation_token(
            spec.operation_key
        ):
            return False
        if str(script_entry.get("data_access") or "none").strip().casefold() != spec.handler.data_access:
            return False
        expected_entrypoint = (Path(ontology_dir) / spec.handler.entrypoint).resolve()
        actual_entrypoint = Path(str(script_entry.get("_resolved_script_path") or "")).resolve()
        return actual_entrypoint == expected_entrypoint

    @staticmethod
    def _root_schema_nodes(schema: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
        """Return schemas that apply to the root argument object.

        Property/item subschemas describe nested values and are intentionally
        excluded.  Local references and object-level applicators continue to
        describe the root object and therefore participate in argument
        ownership and missing-field checks.
        """

        def resolve_local_ref(ref: Any) -> Any:
            value = str(ref or "")
            if value == "#":
                return schema
            if not value.startswith("#/"):
                return None
            current: Any = schema
            for raw_part in value[2:].split("/"):
                part = raw_part.replace("~1", "/").replace("~0", "~")
                if isinstance(current, Mapping) and part in current:
                    current = current[part]
                    continue
                if isinstance(current, list) and part.isdigit():
                    index = int(part)
                    if index < len(current):
                        current = current[index]
                        continue
                return None
            return current

        result: list[Mapping[str, Any]] = []
        pending: list[Any] = [schema]
        seen: set[int] = set()
        while pending:
            current = pending.pop()
            if not isinstance(current, Mapping) or id(current) in seen:
                continue
            seen.add(id(current))
            result.append(current)
            for keyword in ("$ref", "$dynamicRef"):
                referenced = resolve_local_ref(current.get(keyword))
                if referenced is not None:
                    pending.append(referenced)
            for keyword in ("allOf", "anyOf", "oneOf"):
                children = current.get(keyword)
                if isinstance(children, list):
                    pending.extend(children)
            # ``if`` and ``not`` are predicates over the instance; their
            # properties/required keywords do not positively declare caller
            # fields.  Only the selected consequence schemas do.
            for keyword in ("then", "else"):
                child = current.get(keyword)
                if isinstance(child, Mapping):
                    pending.append(child)
            dependent_schemas = current.get("dependentSchemas")
            if isinstance(dependent_schemas, Mapping):
                pending.extend(dependent_schemas.values())
        return tuple(result)

    @classmethod
    def _root_schema_argument_names(cls, schema: Mapping[str, Any]) -> set[str]:
        names: set[str] = set()
        for current in cls._root_schema_nodes(schema):
            properties = current.get("properties")
            if isinstance(properties, Mapping):
                names.update(str(name) for name in properties)
            required = current.get("required")
            if isinstance(required, list):
                names.update(str(name) for name in required)
        return names

    @classmethod
    def _root_property_schema(
        cls,
        schema: Mapping[str, Any],
        field: str,
    ) -> Mapping[str, Any]:
        for current in cls._root_schema_nodes(schema):
            properties = current.get("properties")
            if not isinstance(properties, Mapping):
                continue
            property_schema = properties.get(field)
            if isinstance(property_schema, Mapping):
                return property_schema
        return {}

    @staticmethod
    def _missing_required(errors: list[Any]) -> list[str]:
        """Extract unambiguous required fields for the root argument object."""
        missing: list[str] = []
        seen: set[str] = set()
        for error in errors:
            if error.validator != "required" or list(error.absolute_path):
                continue
            instance = error.instance
            required = error.validator_value
            if not isinstance(instance, Mapping) or not isinstance(required, list):
                continue
            for raw_name in required:
                name = str(raw_name)
                if name in instance or name in seen:
                    continue
                seen.add(name)
                missing.append(name)
        return missing

    def _await_for_missing(
        self,
        *,
        missing: list[str],
        schema: Mapping[str, Any],
        request: DeterministicRequest,
        spec: DeterministicOperationSpec,
        ontology_id: str,
        ontology_version: str,
        arguments: Mapping[str, Any],
        current_runtime_revision: str,
        provider_dependency: Optional[LockedDependency],
    ) -> DeterministicOutcome:
        if not user_interaction_supported_from_context(request.context):
            return Failed(
                "missing_arguments",
                "Required deterministic arguments are missing: {}.".format(
                    ", ".join(missing)
                ),
                operation_key=spec.operation_key,
                internal_detail=",".join(missing),
            )
        field = missing[0]
        property_schema = self._root_property_schema(schema, field)
        prompt = str(
            property_schema.get("description")
            or property_schema.get("title")
            or "Please provide '{}'.".format(field)
        )
        enum_values = property_schema.get("enum")
        selectable_values = (
            list(enum_values)
            if isinstance(enum_values, list) and enum_values
            else []
        )
        interaction = {
            "id": "det-{}-{}-{}".format(
                request.runtime_context.run_id,
                spec.operation_key,
                field,
            ),
            "type": "single_choice" if selectable_values else "input",
            "prompt": prompt,
            "metadata": {
                "source": "deterministic_execution",
                "operation_key": spec.operation_key,
                "field": field,
                "missing_fields": missing,
            },
        }
        if selectable_values:
            interaction["options"] = [
                {
                    "id": str(index),
                    "label": self._interaction_option_label(value),
                    "value": value,
                }
                for index, value in enumerate(selectable_values, start=1)
            ]
        return AwaitingInput(
            interaction=interaction,
            checkpoint=self._checkpoint(
                spec=spec,
                ontology_id=ontology_id,
                ontology_version=ontology_version,
                arguments=arguments,
                missing_fields=missing,
                runtime_revision_value=current_runtime_revision,
                provider_dependency=provider_dependency,
            ),
            operation_key=spec.operation_key,
        )

    def _await_for_issue(
        self,
        *,
        issue: Any,
        remaining_issues: list[Any],
        request: DeterministicRequest,
        spec: DeterministicOperationSpec,
        ontology_id: str,
        ontology_version: str,
        arguments: Mapping[str, Any],
        current_runtime_revision: str,
        provider_dependency: Optional[LockedDependency],
    ) -> DeterministicOutcome:
        if not isinstance(issue, Mapping):
            return Failed(
                "parser_invalid_output",
                "Deterministic parser issue is invalid.",
                operation_key=spec.operation_key,
            )
        field = str(issue.get("field") or "").strip()
        if not field:
            return Failed(
                "parser_invalid_output",
                "Deterministic parser issue field is required.",
                operation_key=spec.operation_key,
            )
        if not user_interaction_supported_from_context(request.context):
            return Failed(
                "missing_arguments",
                "Deterministic input requires clarification for '{}'.".format(field),
                operation_key=spec.operation_key,
                internal_detail=str(issue.get("message") or field),
            )
        options = [str(item) for item in (issue.get("options") or []) if str(item).strip()]
        interaction: dict[str, Any] = {
            "id": "det-{}-{}-{}".format(request.runtime_context.run_id, spec.operation_key, field),
            "type": "single_choice" if options else "input",
            "prompt": str(issue.get("message") or "Please provide '{}'.".format(field)),
            "metadata": {
                "source": "deterministic_execution",
                "operation_key": spec.operation_key,
                "field": field,
                "missing_fields": [field],
            },
        }
        if options:
            interaction["options"] = [
                {"id": str(index), "label": value, "value": value}
                for index, value in enumerate(options, start=1)
            ]
        return AwaitingInput(
            interaction=interaction,
            checkpoint=self._checkpoint(
                spec=spec,
                ontology_id=ontology_id,
                ontology_version=ontology_version,
                arguments=arguments,
                missing_fields=[field],
                runtime_revision_value=current_runtime_revision,
                provider_dependency=provider_dependency,
                pending_parser_issues=remaining_issues,
            ),
            operation_key=spec.operation_key,
        )

    @staticmethod
    def _checkpoint(
        *,
        spec: DeterministicOperationSpec,
        ontology_id: str,
        ontology_version: str,
        arguments: Mapping[str, Any],
        missing_fields: list[str],
        runtime_revision_value: str,
        provider_dependency: Optional[LockedDependency],
        pending_parser_issues: Optional[list[Any]] = None,
    ) -> dict[str, Any]:
        checkpoint = {
            "source": "deterministic_execution",
            "operation_key": spec.operation_key,
            "arguments": dict(arguments),
            "missing_fields": list(missing_fields),
            "ontology_id": ontology_id,
            "ontology_version": ontology_version,
            "package_revision": spec.package_revision,
            "runtime_revision": runtime_revision_value,
        }
        if pending_parser_issues:
            checkpoint["pending_parser_issues"] = list(pending_parser_issues)
        if provider_dependency is not None:
            checkpoint["provider_dependency"] = provider_dependency.to_dict()
        return checkpoint

    @staticmethod
    def _validate_checkpoint(
        checkpoint: Mapping[str, Any],
        *,
        spec: DeterministicOperationSpec,
        ontology_id: str,
        ontology_version: str,
        current_runtime_revision: str,
        provider_dependency: Optional[LockedDependency],
        runtime_argument_keys: frozenset[str],
    ) -> Optional[Failed]:
        expected = {
            "operation_key": spec.operation_key,
            "ontology_id": ontology_id,
            "ontology_version": ontology_version,
            "package_revision": spec.package_revision,
            "runtime_revision": current_runtime_revision,
        }
        expected_provider = (
            provider_dependency.to_dict() if provider_dependency is not None else None
        )
        checkpoint_provider = checkpoint.get("provider_dependency")
        checkpoint_arguments = checkpoint.get("arguments")
        missing_fields = checkpoint.get("missing_fields")
        pending_parser_issues = checkpoint.get("pending_parser_issues")
        invalid_shape = checkpoint.get("source") != "deterministic_execution"
        if not invalid_shape:
            invalid_shape = not isinstance(
                checkpoint_arguments, Mapping
            ) or not isinstance(missing_fields, list)
        if not invalid_shape and pending_parser_issues is not None:
            invalid_shape = not isinstance(pending_parser_issues, list)
        revision_mismatch = any(
            str(checkpoint.get(key) or "") != str(value)
            for key, value in expected.items()
        )
        invalid_dependency = checkpoint_provider != expected_provider
        runtime_fields_present = bool(
            _runtime_owned_checkpoint_names(
                checkpoint,
                runtime_argument_keys,
            )
        )
        if invalid_shape or revision_mismatch or invalid_dependency:
            return Failed(
                "stale_checkpoint",
                "Deterministic checkpoint no longer matches the active version.",
                operation_key=spec.operation_key,
            )
        if runtime_fields_present:
            return Failed(
                "stale_checkpoint",
                "Deterministic checkpoint no longer matches the active version.",
                operation_key=spec.operation_key,
            )
        return None

    @staticmethod
    def _interaction_option_label(value: Any) -> str:
        if isinstance(value, str):
            return value
        return canonical_json_bytes(value).decode("utf-8")

    @classmethod
    def _merge_interaction_response(
        cls,
        arguments: dict[str, Any],
        *,
        checkpoint: Mapping[str, Any],
        interaction_response: Optional[Mapping[str, Any]],
        input_schema: Mapping[str, Any],
    ) -> Optional[Failed]:
        missing = [str(item) for item in (checkpoint.get("missing_fields") or []) if str(item)]
        if not missing:
            return None
        if not interaction_response:
            return Failed("missing_arguments", "A pending deterministic input is required.")
        if "value" not in interaction_response:
            return Failed("missing_arguments", "The deterministic interaction response is empty.")
        value = interaction_response.get("value")
        field = missing[0]
        arguments[field] = cls._coerce_interaction_value(
            value,
            property_schema=cls._root_property_schema(input_schema, field),
        )
        return None

    @classmethod
    def _coerce_interaction_value(
        cls,
        value: Any,
        *,
        property_schema: Mapping[str, Any],
    ) -> Any:
        """Parse text only when the field schema accepts the resulting JSON type."""
        if not isinstance(value, str) or not property_schema:
            return value
        try:
            validator = cls._schema_validator(property_schema)
            if validator.is_valid(value):
                return value
            parsed = cls._strict_json_loads(value)
            if isinstance(parsed, str) or not validator.is_valid(parsed):
                return value
        except Exception:
            return value
        return parsed

    @staticmethod
    def _collect_artifacts(
        artifact_root: Path,
        *,
        writer: Optional[ArtifactWriter],
        forbidden_values: tuple[str, ...] = (),
    ) -> list[dict[str, Any]] | Failed:
        paths, inspection_failure = DeterministicExecutionService._inspect_artifact_tree(
            artifact_root
        )
        if inspection_failure is not None:
            return inspection_failure
        pending_artifacts: list[tuple[dict[str, Any], str]] = []
        normalized_names: set[str] = set()
        total_size = 0
        for path in paths:
            try:
                declared_size = path.stat().st_size
            except OSError as exc:
                return Failed(
                    "artifact_policy_violation",
                    "Artifact metadata could not be read.",
                    internal_detail=type(exc).__name__,
                )
            if (
                declared_size > MAX_ARTIFACT_BYTES
                or total_size + declared_size > MAX_ARTIFACT_TOTAL_BYTES
            ):
                return Failed("artifact_policy_violation", "Artifact size exceeds the limit.")
            try:
                with path.open("rb") as stream:
                    data = stream.read(MAX_ARTIFACT_BYTES + 1)
            except OSError as exc:
                return Failed(
                    "artifact_policy_violation",
                    "Artifact could not be read.",
                    internal_detail=type(exc).__name__,
                )
            total_size += len(data)
            if len(data) > MAX_ARTIFACT_BYTES or total_size > MAX_ARTIFACT_TOTAL_BYTES:
                return Failed("artifact_policy_violation", "Artifact size exceeds the limit.")
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                return Failed("artifact_policy_violation", "V1 artifacts must be UTF-8 text.")
            raw_name = path.relative_to(artifact_root).as_posix()
            name = unicodedata.normalize("NFKC", raw_name)
            if DeterministicExecutionService._value_contains_secret(
                (name, text),
                forbidden_values,
            ):
                return Failed(
                    "secret_output_violation",
                    "Deterministic artifact contains protected provider credentials.",
                )
            normalized_key = name.casefold()
            invalid_root = not name or name.startswith("/")
            invalid_parts = any(
                part in {"", ".", ".."} for part in name.split("/")
            )
            duplicate_name = normalized_key in normalized_names
            if invalid_root or invalid_parts or duplicate_name:
                return Failed(
                    "artifact_policy_violation",
                    "Artifact names must be unique and portable.",
                )
            normalized_names.add(normalized_key)
            media_type = _ARTIFACT_MEDIA_TYPES.get(
                Path(name).suffix.casefold(),
                "text/plain",
            )
            record: dict[str, Any] = {
                "name": name,
                "media_type": media_type,
                "size": len(data),
                "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
            }
            pending_artifacts.append((record, text))

        artifacts: list[dict[str, Any]] = []
        prepare_artifacts = getattr(writer, "prepare_artifacts", None)
        if pending_artifacts and callable(prepare_artifacts):
            try:
                prepare_artifacts(
                    [str(record["name"]) for record, _text in pending_artifacts]
                )
            except Exception as exc:
                DeterministicExecutionService._rollback_persisted_artifacts(
                    writer,
                    artifacts,
                )
                return Failed(
                    "artifact_persistence_failed",
                    "Artifact could not be persisted.",
                    internal_detail=type(exc).__name__,
                )
        for record, text in pending_artifacts:
            if writer is not None:
                name = str(record["name"])
                media_type = str(record["media_type"])
                try:
                    stored = writer(name, text, media_type)
                except Exception as exc:
                    DeterministicExecutionService._rollback_persisted_artifacts(
                        writer,
                        artifacts,
                    )
                    return Failed(
                        "artifact_persistence_failed",
                        "Artifact could not be persisted.",
                        internal_detail=type(exc).__name__,
                    )
                stored_ref = (
                    str(stored.get("output_ref") or "").strip()
                    if isinstance(stored, Mapping)
                    else ""
                )
                if (
                    not isinstance(stored, Mapping)
                    or not stored_ref
                    or str(stored.get("sha256") or "").strip()
                    != str(record["sha256"])
                ):
                    rollback_artifacts = list(artifacts)
                    if stored_ref:
                        rollback_artifacts.append({"output_ref": stored_ref})
                    DeterministicExecutionService._rollback_persisted_artifacts(
                        writer,
                        rollback_artifacts,
                    )
                    return Failed(
                        "artifact_persistence_failed",
                        "Artifact could not be persisted.",
                    )
                for key in ("output_ref", "content_type"):
                    if stored.get(key):
                        record[key] = stored[key]
            artifacts.append(record)
        return artifacts

    @staticmethod
    def _inspect_artifact_tree(
        artifact_root: Path,
    ) -> tuple[list[Path], Optional[Failed]]:
        """Bound traversal before sorting or reading sandbox-produced files."""
        pending_directories = [Path(artifact_root)]
        files: list[Path] = []
        entry_count = 0
        total_size = 0
        while pending_directories:
            directory = pending_directories.pop()
            try:
                entries = os.scandir(directory)
            except FileNotFoundError:
                continue
            except OSError as exc:
                return [], Failed(
                    "artifact_policy_violation",
                    "Artifact directory could not be inspected.",
                    internal_detail=type(exc).__name__,
                )
            with entries:
                for entry in entries:
                    entry_count += 1
                    if entry_count > MAX_ARTIFACT_TREE_ENTRIES:
                        return [], Failed(
                            "artifact_policy_violation",
                            "Artifact directory entry count exceeds the limit.",
                        )
                    if entry.is_symlink():
                        return [], Failed(
                            "artifact_policy_violation",
                            "Artifact symbolic links are forbidden.",
                        )
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            pending_directories.append(Path(entry.path))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            return [], Failed(
                                "artifact_policy_violation",
                                "Artifact entries must be regular files or directories.",
                            )
                        declared_size = entry.stat(follow_symlinks=False).st_size
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        return [], Failed(
                            "artifact_policy_violation",
                            "Artifact metadata could not be read.",
                            internal_detail=type(exc).__name__,
                        )
                    files.append(Path(entry.path))
                    if len(files) > MAX_ARTIFACT_COUNT:
                        return [], Failed(
                            "artifact_policy_violation",
                            "Artifact count exceeds the limit.",
                        )
                    total_size += declared_size
                    if (
                        declared_size > MAX_ARTIFACT_BYTES
                        or total_size > MAX_ARTIFACT_TOTAL_BYTES
                    ):
                        return [], Failed(
                            "artifact_policy_violation",
                            "Artifact size exceeds the limit.",
                        )
        files.sort(
            key=lambda item: item.relative_to(artifact_root).as_posix().encode("utf-8")
        )
        return files, None

    @staticmethod
    def _provider_secret_values(
        binding: Optional[DeterministicProviderBinding],
    ) -> tuple[str, ...]:
        if binding is None:
            return ()
        connection = binding.effective_connection
        values: set[str] = set()
        auth = connection.auth
        for candidate in (auth.token, auth.password):
            normalized = str(candidate or "")
            if normalized:
                values.add(normalized)

        # Normal resolution rejects credentialed endpoints. Keep this defense
        # for injected/test bindings and previously persisted data.
        values.update(endpoint_secret_values(connection.endpoint))

        def collect_sensitive_options(value: Any, *, sensitive: bool = False) -> None:
            if isinstance(value, Mapping):
                for raw_key, child in value.items():
                    key = str(raw_key or "").strip().casefold()
                    child_sensitive = sensitive or any(
                        part in key for part in _SENSITIVE_CONNECTION_KEY_PARTS
                    )
                    collect_sensitive_options(child, sensitive=child_sensitive)
                return
            if isinstance(value, (list, tuple)):
                for child in value:
                    collect_sensitive_options(child, sensitive=sensitive)
                return
            if sensitive and isinstance(value, str):
                normalized = value
                if normalized:
                    values.add(normalized)

        collect_sensitive_options(connection.options)
        return tuple(sorted(values, key=lambda item: (-len(item), item)))

    @staticmethod
    def _protected_value_encodings(value: str) -> set[str]:
        """Return deterministic encodings that remain credential-equivalent."""
        raw = str(value or "")
        if not raw:
            return set()
        payload = raw.encode("utf-8")
        variants = {
            raw,
            payload.hex(),
            payload.hex().upper(),
            quote(raw, safe=""),
            quote_plus(raw, safe=""),
            raw.encode("unicode_escape").decode("ascii"),
        }
        for encoder in (
            base64.b16encode,
            base64.b32encode,
            base64.b64encode,
            base64.urlsafe_b64encode,
            base64.b85encode,
            base64.a85encode,
        ):
            encoded = encoder(payload).decode("ascii")
            variants.add(encoded)
            variants.add(encoded.rstrip("="))
        variants.add(base64.encodebytes(payload).decode("ascii").strip())
        # These digests are recognized only as credential-equivalent output;
        # they are never used to make a security decision. Cover every
        # guaranteed fixed-length algorithm so a handler cannot disclose a
        # protected value merely by changing its representation.
        for algorithm in sorted(hashlib.algorithms_guaranteed):
            if algorithm.startswith("shake_"):
                continue
            digest = hashlib.new(algorithm, payload, usedforsecurity=False)
            variants.add(digest.hexdigest())
        return {item for item in variants if item}

    @classmethod
    def _provider_protected_values(
        cls,
        binding: Optional[DeterministicProviderBinding],
    ) -> tuple[str, ...]:
        if binding is None:
            return ()
        raw_values = set(cls._provider_secret_values(binding))
        username = str(binding.effective_connection.auth.username or "")
        if username:
            raw_values.add(username)
        protected_values: set[str] = set()
        for raw_value in raw_values:
            protected_values.update(cls._protected_value_encodings(raw_value))
        return tuple(
            sorted(protected_values, key=lambda item: (-len(item), item))
        )

    @staticmethod
    def _value_contains_secret(value: Any, secret_values: tuple[str, ...]) -> bool:
        if not secret_values:
            return False
        if isinstance(value, Mapping):
            return any(
                DeterministicExecutionService._value_contains_secret(
                    item,
                    secret_values,
                )
                for pair in value.items()
                for item in pair
            )
        if isinstance(value, (list, tuple, set)):
            return any(
                DeterministicExecutionService._value_contains_secret(
                    item,
                    secret_values,
                )
                for item in value
            )
        if not isinstance(value, str):
            return False
        return any(secret in value for secret in secret_values)

    @staticmethod
    def _redact_secret_values(value: str, secret_values: tuple[str, ...]) -> str:
        redacted = str(value or "")
        for secret in secret_values:
            redacted = redacted.replace(secret, "[REDACTED]")
        return redacted

    @staticmethod
    def _rollback_persisted_artifacts(
        writer: Optional[ArtifactWriter],
        artifacts: list[Mapping[str, Any]],
    ) -> None:
        rollback = getattr(writer, "rollback", None)
        if not callable(rollback):
            return
        output_refs = [
            str(item.get("output_ref") or "").strip()
            for item in artifacts
            if str(item.get("output_ref") or "").strip()
        ]
        try:
            rollback(output_refs)
        except Exception:
            # Preserve the deterministic public failure. Rollback is best effort
            # and the scoped store will remove any remaining artifacts with the
            # containing session.
            return
