"""Execute ontology operation script tool."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set

from agent.tools.business_data.resolver import (
    BusinessDataConnectionError,
    resolve_business_data_source,
)
from agent.tools.business_data.script_env import (
    SCRIPT_BUSINESS_DATA_CONNECTION_ENV,
    serialize_script_business_data_connection,
)
from agent.tools.business_data.effective_connection import build_effective_connection
from utils.logging import get_logger
from ontology.ontology_catalog import (
    find_script_entry,
    get_ontology_manifest,
    load_script_catalog,
    resolve_ontology_script_path,
)
from ontology.ontologies import OntologyManifest, OntologyRegistry
from integration.sandbox.executor import SandboxScriptExecutor
from integration.sandbox.errors import (
    SandboxExecError,
    SandboxUnavailableError,
    SandboxUnsupportedProviderError,
)
from integration.sandbox.config import load_sandbox_config
from integration.sandbox.alerts import log_sandbox_security_fallback
from agent.tools.base import ToolCaller, ToolContext, ToolOutcome
from agent.tools.script_parameters import (
    append_script_parameter_cli_argument,
    is_runtime_parameter,
    runtime_parameter_field,
    script_parameter_argument_keys,
)
from workspace.workspaces import get_workspace_ontologies_root

logger = get_logger("tools.execute_script")

# backend/agentic/tools/execute_script.py -> parents[2] == backend/
BACKEND_DIR = Path(__file__).resolve().parents[2]
LOG_ARG_PREVIEW_LIMIT = 8
LOG_OUTPUT_PREVIEW_LIMIT = 240
MAX_SUCCESSFUL_EXECUTIONS_PER_OPERATION = 4

# Linux ARG_MAX is typically 2 MB. When env + cmdline exceeds this,
# we offload CLI args to a temp file to bypass the kernel limit.
_MAX_ARG_SAFE_TOTAL = 1_500_000

# Windows CreateProcess command-line limit is 32,767 characters.
# When CLI args alone exceed this, also use the tempfile path.
_MAX_CLI_ARGS_SIZE = 25_000

_SANDBOX_TOOL_MISSING_MARKERS = (
    "can't open file",
    "cannot open file",
    "no module named",
    "modulenotfounderror",
    "command not found",
    "executable file not found",
)
_SANDBOX_POLICY_FAILURE_MARKERS = (
    "policy denied",
    "denied by policy",
    "policy rejected",
    "policy validation",
    "invalid policy",
    "policy error",
    "not allowed by policy",
    "landlock",
    "seccomp",
    "mount denied",
)
_SANDBOX_PERMISSION_FAILURE_MARKERS = (
    "permission denied",
    "operation not permitted",
    "access denied",
)

_SCRIPT_EXECUTION_INTERNAL_ERROR = (
    "Error: Script execution failed due to an internal error."
)
_PUBLIC_SCRIPT_ERROR_PREFIX = "ONTO_PUBLIC_ERROR:"
_PUBLIC_SCRIPT_ERROR_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_PUBLIC_SCRIPT_ERROR_MESSAGE_MAX_LENGTH = 240

SCRIPT_LIVE_LOG_ENABLED = (
    os.getenv("SCRIPT_LIVE_LOG_ENABLED", "false").strip().lower()
    in {"1", "true", "yes", "y", "on"}
)


class _ScriptOutputLimitExceeded(RuntimeError):
    def __init__(self, stream_name: str) -> None:
        super().__init__("{} output exceeds limit".format(stream_name))
        self.stream_name = stream_name


class _ScriptExecutionCancelled(RuntimeError):
    pass

EXECUTE_SCRIPT_TOOL_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "execute_script",
        "description": (
            "Execute the maintained script implementation for an ontology operation. "
            "Use this only when the operation appears in the ontology root `script_catalog`. "
            "A successful result is authoritative: reuse it instead of rerunning the same "
            "operation to change rounding, display precision, or informational fields. "
            "If required script arguments are unknown, stop and report the missing "
            "parameters instead of reading object definitions or manually replaying a "
            "script-backed operation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ontology_id": {
                    "type": "string",
                    "description": "Ontology id selected via ontology_context(root).",
                },
                "operation_key": {
                    "type": "string",
                    "description": (
                        "Operation key whose maintained script is registered in script_catalog."
                    ),
                },
                "arguments": {
                    "type": "object",
                    "description": (
                        "Concrete operation input values keyed by the parameter names from "
                        "`script_catalog.parameters`. Required, optional, and multiple-value "
                        "parameters are declared in that catalog; omitted parameters with a "
                        "declared `default` use that value. For `multiple: true` "
                        "parameters, pass an array of values. Runtime values must come "
                        "from the current user request, not ontology examples or history."
                    ),
                },
            },
            "required": ["ontology_id", "operation_key", "arguments"],
        },
    },
}


def build_execute_script_tool_schema(
    selected_ontology_id: Optional[str] = None,
    *,
    selected_ontology_version: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build an immutable request-scoped schema from script descriptors.

    A flat union keeps the schema compatible with the model providers used by the
    runtime. Parameters required by every available operation remain required in
    the union; operation-specific requirements are still validated immediately
    before the selected script is executed.
    """
    schema = deepcopy(EXECUTE_SCRIPT_TOOL_SCHEMA)
    ontology_id = str(selected_ontology_id or "").strip()

    try:
        ontologies, error = _get_schema_ontology_manifests(
            ontology_id,
            ontology_version=selected_ontology_version,
            workspace_id=workspace_id,
        )
        if error or not ontologies:
            logger.warning(
                "Cannot specialize execute_script schema for ontology %s in workspace %s: %s",
                ontology_id or "<workspace fallback>",
                str(workspace_id or "").strip() or "<current>",
                error or "no enabled ontology with bound scripts found",
            )
            return schema

        catalog: List[Dict[str, Any]] = []
        scripted_ontologies: List[OntologyManifest] = []
        for ontology in ontologies:
            ontology_catalog = [
                entry
                for entry in load_script_catalog(Path(ontology.ontology_dir).resolve())
                if bool(entry.get("_is_bound_operation", True))
                and not entry.get("_is_deterministic_handler")
                and str(entry.get("operation_key") or "").strip()
            ]
            if not ontology_catalog:
                continue
            scripted_ontologies.append(ontology)
            catalog.extend(ontology_catalog)
        if not catalog:
            return schema

        parameters = schema["function"]["parameters"]
        properties = parameters["properties"]
        properties["ontology_id"]["enum"] = [
            ontology.id for ontology in scripted_ontologies
        ]
        properties["operation_key"]["enum"] = list(
            dict.fromkeys(
                str(entry.get("operation_key") or "").strip()
                for entry in catalog
                if str(entry.get("operation_key") or "").strip()
            )
        )

        argument_properties: Dict[str, Dict[str, Any]] = {}
        required_by_operation: List[Set[str]] = []
        operation_parameter_summaries: List[str] = []
        for entry in catalog:
            operation_required: Set[str] = set()
            parameter_names: List[str] = []
            for parameter in list(entry.get("parameters") or []):
                if not isinstance(parameter, Mapping):
                    continue
                # Runtime identity/context values are injected by the backend
                # and must never be exposed as model-controlled arguments.
                if is_runtime_parameter(parameter):
                    continue
                name = _parameter_name(parameter)
                if not name:
                    continue
                parameter_names.append(name)
                if bool(parameter.get("required", True)):
                    operation_required.add(name)
                argument_properties[name] = _merge_script_parameter_property_schema(
                    argument_properties.get(name),
                    _script_parameter_property_schema(parameter),
                )
            required_by_operation.append(operation_required)
            operation_parameter_summaries.append(
                "{}: {}".format(
                    str(entry.get("operation_key") or "").strip(),
                    ", ".join(parameter_names) if parameter_names else "no arguments",
                )
            )

        common_required = (
            set.intersection(*required_by_operation)
            if required_by_operation
            else set()
        )
        argument_schema = properties["arguments"]
        argument_schema["properties"] = argument_properties
        argument_schema["required"] = [
            name for name in argument_properties if name in common_required
        ]
        argument_schema["additionalProperties"] = False
        if operation_parameter_summaries:
            argument_schema["description"] = (
                "{} Available operation inputs: {}.".format(
                    str(argument_schema.get("description") or "").rstrip(". "),
                    "; ".join(operation_parameter_summaries),
                ).strip()
            )
        return schema
    except Exception:
        logger.warning(
            "Failed to specialize execute_script schema for ontology %s in workspace %s",
            ontology_id or "<workspace fallback>",
            str(workspace_id or "").strip() or "<current>",
            exc_info=True,
        )
        return schema


def _get_schema_ontology_manifests(
    ontology_id: str,
    *,
    ontology_version: Optional[str],
    workspace_id: Optional[str],
) -> tuple[List[OntologyManifest], Optional[str]]:
    """Resolve schema metadata without mutating the process-wide registry."""
    normalized_workspace_id = str(workspace_id or "").strip()
    if not normalized_workspace_id:
        if not ontology_id:
            return [], None
        kwargs = {}
        if str(ontology_version or "").strip():
            kwargs["version"] = ontology_version
        ontology, error = get_ontology_manifest(ontology_id, **kwargs)
        return ([ontology] if ontology is not None else []), error

    registry = OntologyRegistry(
        ontology_dirs=[get_workspace_ontologies_root(normalized_workspace_id)]
    )
    registry.discover_ontologies()
    if not ontology_id:
        return [
            ontology
            for item in sorted(registry.list_enabled_ontologies())
            if (ontology := registry.get_ontology(item)) is not None
        ], None

    manifest_kwargs = {"workspace_id": workspace_id}
    if str(ontology_version or "").strip():
        manifest_kwargs["version"] = ontology_version
    ontology, error = get_ontology_manifest(ontology_id, **manifest_kwargs)
    if ontology is None:
        return [], error or "Error: Ontology '{}' not found.".format(ontology_id)
    return [ontology], None


def _script_parameter_property_schema(parameter: Mapping[str, Any]) -> Dict[str, Any]:
    parameter_type = str(parameter.get("type") or "string").strip().lower()
    json_type = {
        "int": "integer",
        "integer": "integer",
        "float": "number",
        "number": "number",
        "bool": "boolean",
        "boolean": "boolean",
        "object": "object",
        "array": "array",
        "string": "string",
    }.get(parameter_type, "string")
    if bool(parameter.get("multiple", False)):
        property_schema: Dict[str, Any] = {
            "type": "array",
            "items": {"type": json_type},
        }
    else:
        property_schema = {"type": json_type}
    description = str(parameter.get("description") or "").strip()
    if description:
        property_schema["description"] = description
    return property_schema


def _merge_script_parameter_property_schema(
    existing: Optional[Dict[str, Any]],
    incoming: Dict[str, Any],
) -> Dict[str, Any]:
    if not existing:
        return deepcopy(incoming)
    if existing.get("type") != incoming.get("type") or existing.get("items") != incoming.get("items"):
        # A false constraint is worse than leaving a conflicting union property untyped.
        merged: Dict[str, Any] = {}
    else:
        merged = {
            key: deepcopy(value)
            for key, value in existing.items()
            if key != "description"
        }
    descriptions = list(
        dict.fromkeys(
            part.strip()
            for text in (
                str(existing.get("description") or "").strip(),
                str(incoming.get("description") or "").strip(),
            )
            for part in text.split(" / ")
            if part.strip()
        )
    )
    if descriptions:
        merged["description"] = " / ".join(descriptions)
    return merged


def _parse_tool_call_arguments(raw: Any) -> Optional[Dict[str, Any]]:
    if isinstance(raw, Mapping):
        return dict(raw)
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return dict(parsed) if isinstance(parsed, Mapping) else None


def _is_successful_script_output(content: Any) -> bool:
    text = str(content or "").strip()
    if not text or text.lower().startswith("error:"):
        return False
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return True
    return not (isinstance(payload, Mapping) and payload.get("error"))


def _prior_successful_operation_calls(
    messages: Sequence[Mapping[str, Any]],
    *,
    ontology_id: str,
    operation_key: str,
    current_tool_call_id: Optional[str],
) -> List[Dict[str, Any]]:
    outputs = {
        str(message.get("tool_call_id") or "").strip(): str(message.get("content") or "")
        for message in messages
        if message.get("role") == "tool"
        and message.get("name") == "execute_script"
        and str(message.get("tool_call_id") or "").strip()
    }
    successful: List[Dict[str, Any]] = []
    current_id = str(current_tool_call_id or "").strip()
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for tool_call in message.get("tool_calls") or []:
            if not isinstance(tool_call, Mapping):
                continue
            call_id = str(tool_call.get("id") or "").strip()
            if not call_id or call_id == current_id:
                continue
            function = tool_call.get("function")
            function = function if isinstance(function, Mapping) else tool_call
            if str(function.get("name") or "").strip() != "execute_script":
                continue
            arguments = _parse_tool_call_arguments(function.get("arguments"))
            if not arguments:
                continue
            if str(arguments.get("ontology_id") or "").strip() != ontology_id:
                continue
            if str(arguments.get("operation_key") or "").strip() != operation_key:
                continue
            output = outputs.get(call_id)
            if output is None or not _is_successful_script_output(output):
                continue
            successful.append({"arguments": arguments, "content": output})
    return successful


def _canonical_tool_arguments(arguments: Mapping[str, Any]) -> str:
    return json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _format_call_context(
    *,
    tool_call_id: Optional[str],
    caller: Optional[str],
    run_id: Optional[str],
    session_id: Optional[str],
    turn_id: Optional[int],
) -> str:
    parts: List[str] = []
    if tool_call_id:
        parts.append("tool_call_id={}".format(tool_call_id))
    if caller:
        parts.append("caller={}".format(caller))
    if run_id:
        parts.append("run_id={}".format(run_id))
    if session_id:
        parts.append("session_id={}".format(session_id))
    if turn_id is not None:
        parts.append("turn_id={}".format(turn_id))
    return " ".join(parts) if parts else "tool_call_id=n/a"


def _fallback_or_error(
    *,
    fallback_local: bool,
    mode: str = "prefer",
    reason: str,
    blocked_message: str,
    call_context: str,
    script_path: str,
    run_id: Optional[str],
    error: Any = None,
    provider: str = "",
    workspace_id: Optional[str] = None,
    ontology_id: Optional[str] = None,
) -> Optional[str]:
    """Authorize one explicit transition from sandbox to local execution."""
    if not fallback_local:
        return blocked_message
    log_sandbox_security_fallback(
        reason=reason,
        call_context=call_context,
        script_path=script_path,
        run_id=run_id,
        error=type(error).__name__ if error is not None else None,
        mode=mode,
        provider=provider,
        workspace_id=workspace_id,
        ontology_id=ontology_id,
    )
    return None


def _classify_sandbox_failure_text(value: Any) -> Optional[str]:
    """Classify sandbox exceptions, which already use the fallback gate."""
    text = str(value or "").strip().lower()
    if not text:
        return None
    if any(
        marker in text
        for marker in (
            *_SANDBOX_POLICY_FAILURE_MARKERS,
            *_SANDBOX_PERMISSION_FAILURE_MARKERS,
        )
    ):
        return "sandbox_policy_unavailable"
    if any(marker in text for marker in _SANDBOX_TOOL_MISSING_MARKERS):
        return "sandbox_tool_missing"
    return None


def _classify_sandbox_result_failure(
    completed: subprocess.CompletedProcess[str],
) -> Optional[str]:
    """Classify only unambiguous sandbox startup/policy process failures."""
    if completed.returncode == 0:
        return None
    stderr = str(completed.stderr or "").strip().lower()
    if not stderr:
        return None
    if any(marker in stderr for marker in _SANDBOX_POLICY_FAILURE_MARKERS):
        return "sandbox_policy_unavailable"
    if completed.returncode == 126 and any(
        marker in stderr for marker in _SANDBOX_PERMISSION_FAILURE_MARKERS
    ):
        return "sandbox_policy_unavailable"
    if (
        any(marker in stderr for marker in ("can't open file", "cannot open file"))
        and "no such file or directory" in stderr
    ):
        return "sandbox_tool_missing"
    if "modulenotfounderror" in stderr and "no module named" in stderr:
        return "sandbox_tool_missing"
    if completed.returncode == 127 and any(
        marker in stderr
        for marker in ("command not found", "executable file not found")
    ):
        return "sandbox_tool_missing"
    return None


def _estimate_cmdline_size(env: Dict[str, str], *cmdline_parts: str) -> int:
    """Estimate total size of env + command line for ARG_MAX checking."""
    total = sum(len(k) + len(v) + 1 for k, v in env.items())
    for part in cmdline_parts:
        total += len(part) + 1
    return total


def _strip_child_environ(src: Dict[str, str]) -> Dict[str, str]:
    """Keep only env vars the child process actually needs.

    Uses a strict allowlist: essential vars (PATH, PYTHONPATH, HOME, …)
    plus project config variables (LLM_*, AUTH_*, …).  Everything else
    is Windows Docker noise that serves no purpose inside the container
    but can push the total past ARG_MAX.
    """
    _ALWAYS = frozenset({
        "PATH", "PYTHONPATH", "HOME", "USER", "LANG",
        "TMPDIR", "TEMP", "TMP", "SHELL", "HOSTNAME",
    })
    _CONFIG_PREFIXES = (
        "LLM_", "TASK_LLM_", "AUTH_", "MINDSDB_", "NL2SQL_",
        "PROJECT_", "ONTOLOGY_", "MEMORY_", "RAG_", "AGENT_",
        "APP_", "FLASK_", "OPENAI_", "DOCKER_",
    )
    return {
        k: v for k, v in src.items()
        if k in _ALWAYS or k.startswith(_CONFIG_PREFIXES)
    }


def _run_with_live_logging(
    cmd: List[str],
    *,
    cwd: str,
    env: Dict[str, str],
    timeout_seconds: int,
    script_name: str = "",
) -> subprocess.CompletedProcess:
    """Run subprocess, stream stdout/stderr line-by-line to logger.

    Uses Popen with per-line reading threads so the server log reflects
    script progress in real time.  Callers still get a CompletedProcess
    with accumulated full output — the return value is unchanged.
    """
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    stdout_lines: List[str] = []
    stderr_lines: List[str] = []

    label = " [{}]".format(script_name) if script_name else ""

    def _read_stdout() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            stdout_lines.append(line)
            logger.info(
                "[script:stdout]%s line_chars=%s",
                label,
                len(line.rstrip("\n\r")),
            )

    def _read_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_lines.append(line)
            logger.info(
                "[script:stderr]%s line_chars=%s",
                label,
                len(line.rstrip("\n\r")),
            )

    t_out = threading.Thread(target=_read_stdout, daemon=True)
    t_err = threading.Thread(target=_read_stderr, daemon=True)
    t_out.start()
    t_err.start()

    try:
        proc.wait(timeout=max(1, int(timeout_seconds)))
    except subprocess.TimeoutExpired:
        proc.kill()
        t_out.join(timeout=5)
        t_err.join(timeout=5)
        raise subprocess.TimeoutExpired(
            cmd=cmd,
            timeout=timeout_seconds,
            output="".join(stdout_lines),
            stderr="".join(stderr_lines),
        )

    t_out.join(timeout=10)
    t_err.join(timeout=10)

    return subprocess.CompletedProcess(
        args=cmd,
        returncode=proc.returncode or 0,
        stdout="".join(stdout_lines),
        stderr="".join(stderr_lines),
    )


def _run_with_bounded_output(
    cmd: List[str],
    *,
    cwd: str,
    env: Dict[str, str],
    timeout_seconds: int,
    stdout_limit_bytes: int,
    stderr_limit_bytes: int,
    cancel_event: Optional[threading.Event] = None,
) -> subprocess.CompletedProcess[str]:
    """Capture deterministic script output without unbounded allocation."""
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
    )
    outputs = {"stdout": bytearray(), "stderr": bytearray()}
    limits = {
        "stdout": max(1, int(stdout_limit_bytes)),
        "stderr": max(1, int(stderr_limit_bytes)),
    }
    overflow: list[str] = []
    overflow_event = threading.Event()

    def read_stream(stream_name: str, pipe: Any) -> None:
        try:
            while True:
                chunk = pipe.read(8192)
                if not chunk:
                    return
                remaining = limits[stream_name] + 1 - len(outputs[stream_name])
                if remaining > 0:
                    outputs[stream_name].extend(chunk[:remaining])
                if len(outputs[stream_name]) > limits[stream_name]:
                    if not overflow:
                        overflow.append(stream_name)
                    overflow_event.set()
                    try:
                        proc.terminate()
                    except OSError:
                        pass
                    return
        finally:
            try:
                pipe.close()
            except OSError:
                pass

    assert proc.stdout is not None
    assert proc.stderr is not None
    threads = [
        threading.Thread(target=read_stream, args=("stdout", proc.stdout), daemon=True),
        threading.Thread(target=read_stream, args=("stderr", proc.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()

    deadline = time.monotonic() + max(1, int(timeout_seconds))
    cancelled = False
    timed_out = False
    while proc.poll() is None:
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            proc.terminate()
            break
        if overflow_event.is_set():
            break
        if time.monotonic() >= deadline:
            timed_out = True
            proc.terminate()
            break
        time.sleep(0.02)

    if proc.poll() is None:
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)
    for thread in threads:
        thread.join(timeout=2)

    stdout = bytes(outputs["stdout"]).decode("utf-8", errors="replace")
    stderr = bytes(outputs["stderr"]).decode("utf-8", errors="replace")
    if cancelled:
        raise _ScriptExecutionCancelled("script execution cancelled")
    if timed_out:
        raise subprocess.TimeoutExpired(
            cmd=cmd,
            timeout=timeout_seconds,
            output=stdout,
            stderr=stderr,
        )
    if overflow:
        raise _ScriptOutputLimitExceeded(overflow[0])
    return subprocess.CompletedProcess(
        args=cmd,
        returncode=int(proc.returncode or 0),
        stdout=stdout,
        stderr=stderr,
    )


def _spawn_with_args_file(
    resolved_path: Path,
    cli_args: List[str],
    env: Dict[str, str],
    cwd: str,
    bounded_output: Optional[Mapping[str, Any]] = None,
    **kwargs: Any,
) -> subprocess.CompletedProcess:
    """Write oversize CLI args to a temp file and run a loader script.

    The loader reads the file, patches ``sys.argv`` to match what the target
    script would have received on the command line, then execs the target.
    This is **transparent** to the target script.

    When the environment itself is too large, we strip oversized or
    non-essential variables — the loader only needs stdlib imports and
    project config.
    """
    # Write CLI args to a temp JSON file
    args_fd, args_path = tempfile.mkstemp(suffix=".args.json")
    os.close(args_fd)
    with open(args_path, "w", encoding="utf-8") as f:
        json.dump(cli_args, f)

    # Write a tiny loader script
    loader_fd, loader_path = tempfile.mkstemp(suffix=".py")
    os.close(loader_fd)
    target = str(resolved_path)
    try:
        with open(loader_path, "w", encoding="utf-8") as f:
            f.write(
                'import json,os,sys,runpy\n'
                'with open({!r}) as _f:\n'
                '  sys.argv = [{!r}] + json.load(_f)\n'
                'os.unlink({!r})\n'
                'runpy.run_path({!r}, run_name="__main__")\n'
                .format(args_path, target, args_path, target)
            )

        # Strip oversized env vars — the loader doesn't need them and they
        # can push the total past ARG_MAX even with minimal CLI args.
        child_env = _strip_child_environ(env)

        logger.info(
            "CLI args too large (%d bytes), using tempfile loader: %s -> %s",
            _estimate_cmdline_size(env, sys.executable, str(resolved_path), *cli_args),
            loader_path,
            target,
        )

        try:
            if bounded_output is not None:
                return _run_with_bounded_output(
                    [sys.executable, "-u", loader_path],
                    cwd=cwd,
                    env=child_env,
                    timeout_seconds=int(kwargs.get("timeout", 300)),
                    stdout_limit_bytes=int(bounded_output["stdout_limit_bytes"]),
                    stderr_limit_bytes=int(bounded_output["stderr_limit_bytes"]),
                    cancel_event=bounded_output.get("cancel_event"),
                )
            if SCRIPT_LIVE_LOG_ENABLED:
                timeout_seconds = int(kwargs.get("timeout", 300))
                return _run_with_live_logging(
                    [sys.executable, "-u", loader_path],
                    cwd=cwd,
                    env=child_env,
                    timeout_seconds=timeout_seconds,
                    script_name=resolved_path.name,
                )
            return subprocess.run(
                [sys.executable, loader_path],
                env=child_env,
                cwd=cwd,
                **kwargs,
            )
        finally:
            try:
                os.unlink(args_path)
            except OSError:
                pass
    finally:
        try:
            os.unlink(loader_path)
        except OSError:
            pass


def _build_script_env_overrides(
    *,
    workspace_id: Optional[str],
    ontology_id: str,
) -> Dict[str, str]:
    if not workspace_id:
        return {}
    try:
        connection = resolve_business_data_source(
            workspace_id=workspace_id,
            ontology_id=ontology_id,
        )
    except BusinessDataConnectionError:
        return {}
    connection_config = dict(connection.config)
    connection_config["workspace_id"] = str(workspace_id or "").strip()
    connection_config["ontology_id"] = str(ontology_id or "").strip()
    effective_connection = build_effective_connection(connection)
    effective_connection = replace(
        effective_connection,
        options={
            **effective_connection.options,
            "workspace_id": connection_config["workspace_id"],
            "ontology_id": connection_config["ontology_id"],
        },
    )
    return {
        SCRIPT_BUSINESS_DATA_CONNECTION_ENV: serialize_script_business_data_connection(
            connection=effective_connection,
        )
    }


def handle_execute_script_path(
    script_path: str,
    args: Optional[Sequence[Any]] = None,
    base_path: Optional[Path] = None,
    timeout_seconds: int = 300,
    tool_call_id: Optional[str] = None,
    caller: Optional[str] = None,
    run_id: Optional[str] = None,
    session_id: Optional[str] = None,
    turn_id: Optional[int] = None,
    extra_pythonpath_dirs: Optional[Sequence[Path]] = None,
    env_overrides: Optional[Mapping[str, str]] = None,
    workspace_id: Optional[str] = None,
    ontology_id: Optional[str] = None,
    ontology_dir: Optional[Path] = None,
    clean_env: bool = False,
    include_stderr_on_success: bool = True,
    stdout_limit_bytes: Optional[int] = None,
    stderr_limit_bytes: Optional[int] = None,
    file_size_limit_bytes: Optional[int] = None,
    cancel_event: Optional[threading.Event] = None,
    sandbox_prepare_data_connection: bool = True,
    sandbox_include_data_connection_env: bool = True,
    sandbox_network_endpoint: Optional[str] = None,
    sandbox_writable_paths: Sequence[Path] = (),
    sandbox_run_id: Optional[str] = None,
    sandbox_required: bool = False,
    sandbox_restrict_filesystem_to_ontology: bool = False,
) -> str:
    """Run a Python script relative to project root and return its output.

    ``extra_pythonpath_dirs`` — additional directories appended to ``PYTHONPATH``
    so that scripts can import sibling packages (e.g., an ontology directory
    containing a ``scripts/`` package).
    """
    call_context = _format_call_context(
        tool_call_id=tool_call_id,
        caller=caller,
        run_id=run_id,
        session_id=session_id,
        turn_id=turn_id,
    )

    if not base_path:
        logger.warning(
            "execute_script missing project root (%s): script_path=%s",
            call_context,
            script_path,
        )
        return "Error: Project base path not configured"
    # Tool adapters may carry the project root as a string (especially across
    # JSON/provider boundaries); normalize once at the filesystem boundary.
    base_path = Path(base_path)

    cli_args: List[str] = []
    if args:
        for item in args:
            if item is None:
                continue
            cli_args.append(str(item))

    logger.info(
        "execute_script request received (%s): script_path=%s arg_count=%s",
        call_context,
        script_path,
        len(cli_args),
    )

    resolved_path: Optional[Path] = None
    try:
        base_resolved = base_path.resolve()
        requested_path = Path(script_path)

        if requested_path.is_absolute():
            logger.warning(
                "Blocked absolute script path (%s): script_path=%s",
                call_context,
                script_path,
            )
            return "Error: Absolute paths not allowed. Use paths relative to project root."

        if ".." in requested_path.parts:
            logger.warning(
                "Blocked script path traversal (%s): script_path=%s",
                call_context,
                script_path,
            )
            return "Error: Path traversal ('..') not allowed."

        resolved_path = (base_resolved / requested_path).resolve()
        try:
            resolved_path.relative_to(base_resolved)
        except ValueError:
            logger.warning(
                "Blocked script escape attempt (%s): %s resolved to %s outside %s",
                call_context,
                script_path,
                resolved_path,
                base_resolved,
            )
            return "Error: Access denied - path must be within project directory"

        if ontology_dir is not None:
            ontology_root = Path(ontology_dir).resolve()
            ontology_scripts_root = (ontology_root / "scripts").resolve()
            try:
                ontology_scripts_root.relative_to(ontology_root)
                resolved_path.relative_to(ontology_scripts_root)
            except ValueError:
                logger.warning(
                    (
                        "Blocked script outside ontology scripts (%s): "
                        "script_path=%s resolved_path=%s ontology_dir=%s"
                    ),
                    call_context,
                    script_path,
                    resolved_path,
                    ontology_root,
                )
                return (
                    "Error: Access denied - script must be within current ontology scripts directory"
                )
            if caller != "deterministic_service":
                try:
                    from ontology.operation_execution import (
                        protected_handler_paths,
                    )

                    protected_paths = protected_handler_paths(ontology_root)
                except Exception:
                    logger.exception(
                        "Failed to classify deterministic handler protection (%s)",
                        call_context,
                    )
                    return "Error: deterministic_operation_protection_unavailable"
                if resolved_path in protected_paths:
                    logger.warning(
                        "Blocked deterministic handler bypass (%s): script_path=%s",
                        call_context,
                        script_path,
                    )
                    return "Error: deterministic_operation_protected"

        if resolved_path.suffix.lower() != ".py":
            logger.warning(
                "Blocked non-python script (%s): script_path=%s resolved_path=%s",
                call_context,
                script_path,
                resolved_path,
            )
            return "Error: Only .py scripts can be executed"

        if not resolved_path.exists():
            logger.warning(
                "Python script not found (%s): script_path=%s resolved_path=%s",
                call_context,
                script_path,
                resolved_path,
            )
            return "Error: File not found: {}".format(script_path)

        if not resolved_path.is_file():
            logger.warning(
                "Python script path is not a file (%s): script_path=%s resolved_path=%s",
                call_context,
                script_path,
                resolved_path,
            )
            return "Error: '{}' is not a file".format(script_path)

        if clean_env:
            allowed_host_names = {
                "PATH",
                "SYSTEMROOT",
                "WINDIR",
                "TEMP",
                "TMP",
                "TMPDIR",
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
            }
            child_env = {
                key: value
                for key, value in os.environ.items()
                if key in allowed_host_names
            }
        else:
            child_env = os.environ.copy()
        pythonpath_parts: List[str] = [str(BACKEND_DIR)]
        if extra_pythonpath_dirs:
            for d in extra_pythonpath_dirs:
                resolved_p = d.resolve()
                if resolved_p.is_dir():
                    pythonpath_parts.append(str(resolved_p))
        existing_pp = child_env.get("PYTHONPATH", "")
        if existing_pp:
            pythonpath_parts.append(existing_pp)
        child_env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
        child_env.setdefault("PYTHONIOENCODING", "utf-8")
        child_env.setdefault("PYTHONUTF8", "1")
        if env_overrides:
            child_env.update({str(key): str(value) for key, value in env_overrides.items()})

        logger.info(
            "Launching python script (%s): resolved_path=%s cwd=%s timeout_seconds=%s",
            call_context,
            resolved_path,
            base_resolved,
            timeout_seconds,
        )

        started_at = time.monotonic()
        total_size = _estimate_cmdline_size(
            child_env, sys.executable, str(resolved_path), *cli_args
        )
        # CLI args alone — catches cases where env is small but args are huge
        # (e.g., native Windows with 32 KB cmdline limit)
        cli_args_size = _estimate_cmdline_size({}, *cli_args)
        oversize_args = (
            total_size > _MAX_ARG_SAFE_TOTAL or cli_args_size > _MAX_CLI_ARGS_SIZE
        )

        completed: Optional[subprocess.CompletedProcess[str]] = None
        sandbox_config = load_sandbox_config()
        execution_mode = str(
            getattr(
                sandbox_config,
                "execution_mode",
                "prefer" if sandbox_config.enabled else "legacy",
            )
        )
        run_locally = not sandbox_config.enabled
        fallback_reason: Optional[str] = None
        fallback_error: Any = None
        fallback_blocked_message = ""

        if sandbox_required and not sandbox_config.enabled:
            logger.error(
                "Sandbox is required for this script execution (%s): script_path=%s",
                call_context,
                script_path,
            )
            return "Error: Sandbox required for deterministic execution."

        if sandbox_config.enabled:
            if sys.platform != "linux":
                fallback_reason = "unsupported_platform"
                fallback_blocked_message = (
                    "Error: Sandbox unavailable: sandbox execution requires Linux "
                    "(current platform: {})".format(sys.platform)
                )
            elif not run_id:
                fallback_reason = "missing_run_id"
                fallback_blocked_message = (
                    "Error: Sandbox unavailable: run_id is required for sandbox execution"
                )
            elif oversize_args:
                fallback_reason = "oversize_args"
                fallback_blocked_message = (
                    "Error: Sandbox unavailable: oversized script arguments are not "
                    "supported by sandbox execution"
                )
            else:
                try:
                    completed = SandboxScriptExecutor().run_script(
                        script_path=script_path.replace("\\", "/"),
                        cli_args=cli_args,
                        project_root=base_resolved,
                        ontology_dir=ontology_dir,
                        run_id=str(sandbox_run_id or run_id),
                        workspace_id=workspace_id,
                        ontology_id=ontology_id,
                        timeout_seconds=max(1, int(timeout_seconds)),
                        env_overrides=env_overrides,
                        prepare_data_connection=sandbox_prepare_data_connection,
                        include_data_connection_env=sandbox_include_data_connection_env,
                        network_endpoint=sandbox_network_endpoint,
                        writable_paths=sandbox_writable_paths,
                        stdout_limit_bytes=stdout_limit_bytes,
                        stderr_limit_bytes=stderr_limit_bytes,
                        file_size_limit_bytes=file_size_limit_bytes,
                        cancel_event=cancel_event,
                        restrict_filesystem_to_ontology=(
                            sandbox_restrict_filesystem_to_ontology
                        ),
                    )
                except SandboxUnsupportedProviderError as exc:
                    fallback_reason = "unsupported_provider"
                    fallback_error = exc
                    fallback_blocked_message = "Error: Sandbox provider is not supported."
                except SandboxUnavailableError as exc:
                    fallback_reason = "sandbox_unavailable"
                    fallback_error = exc
                    fallback_blocked_message = "Error: Sandbox unavailable."
                except SandboxExecError as exc:
                    if "timed out" in str(exc).lower():
                        return "Error: Script execution timed out after {} seconds.".format(
                            timeout_seconds
                        )
                    fallback_reason = (
                        _classify_sandbox_failure_text(exc)
                        or "sandbox_exec_failed"
                    )
                    fallback_error = exc
                    fallback_blocked_message = (
                        "Error: Sandbox policy unavailable."
                        if fallback_reason == "sandbox_policy_unavailable"
                        else "Error: Sandbox execution failed."
                    )
                except Exception as exc:
                    fallback_reason = (
                        _classify_sandbox_failure_text(exc)
                        or "sandbox_execution_error"
                    )
                    fallback_error = exc
                    fallback_blocked_message = (
                        "Error: Sandbox policy unavailable."
                        if fallback_reason == "sandbox_policy_unavailable"
                        else "Error: Sandbox execution failed."
                    )

            if completed is not None:
                result_failure_reason = _classify_sandbox_result_failure(completed)
                if result_failure_reason:
                    fallback_reason = result_failure_reason
                    fallback_error = SandboxExecError(result_failure_reason)
                    fallback_blocked_message = (
                        "Error: Sandbox policy unavailable."
                        if result_failure_reason == "sandbox_policy_unavailable"
                        else "Error: Sandbox tool is unavailable."
                    )
                    completed = None

            if completed is None:
                if cancel_event is not None and cancel_event.is_set():
                    return "Error: Script execution cancelled."
                if fallback_reason is None:
                    logger.error(
                        "Sandbox executor returned no process result (%s): script_path=%s",
                        call_context,
                        script_path,
                    )
                    return _SCRIPT_EXECUTION_INTERNAL_ERROR
                blocked = _fallback_or_error(
                    fallback_local=(
                        sandbox_config.fallback_local and not sandbox_required
                    ),
                    mode=execution_mode,
                    reason=fallback_reason,
                    blocked_message=fallback_blocked_message,
                    call_context=call_context,
                    script_path=script_path,
                    run_id=run_id,
                    error=fallback_error,
                    provider=str(getattr(fallback_error, "provider", "")),
                    workspace_id=workspace_id,
                    ontology_id=ontology_id,
                )
                if blocked is not None:
                    return blocked
                run_locally = True

        if completed is None and run_locally:
            bounded_output = None
            if stdout_limit_bytes is not None or stderr_limit_bytes is not None:
                bounded_output = {
                    "stdout_limit_bytes": max(1, int(stdout_limit_bytes or 1)),
                    "stderr_limit_bytes": max(1, int(stderr_limit_bytes or 1)),
                    "cancel_event": cancel_event,
                }
            if oversize_args:
                completed = _spawn_with_args_file(
                    resolved_path=resolved_path,
                    cli_args=cli_args,
                    env=child_env,
                    cwd=str(base_resolved),
                    bounded_output=bounded_output,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=max(1, int(timeout_seconds)),
                    check=False,
                )
            elif bounded_output is not None:
                completed = _run_with_bounded_output(
                    [sys.executable, "-u", str(resolved_path), *cli_args],
                    cwd=str(base_resolved),
                    env=child_env,
                    timeout_seconds=max(1, int(timeout_seconds)),
                    stdout_limit_bytes=bounded_output["stdout_limit_bytes"],
                    stderr_limit_bytes=bounded_output["stderr_limit_bytes"],
                    cancel_event=cancel_event,
                )
            elif SCRIPT_LIVE_LOG_ENABLED:
                completed = _run_with_live_logging(
                    [sys.executable, "-u", str(resolved_path), *cli_args],
                    cwd=str(base_resolved),
                    env=child_env,
                    timeout_seconds=max(1, int(timeout_seconds)),
                    script_name=resolved_path.name,
                )
            else:
                completed = subprocess.run(
                    [sys.executable, str(resolved_path), *cli_args],
                    cwd=str(base_resolved),
                    env=child_env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=max(1, int(timeout_seconds)),
                    check=False,
                )

        if completed is None:
            logger.error(
                "Script execution reached result stage without a process result (%s)",
                call_context,
            )
            return _SCRIPT_EXECUTION_INTERNAL_ERROR

        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        if cancel_event is not None and cancel_event.is_set():
            return "Error: Script execution cancelled."
        if stdout_limit_bytes is not None and len(stdout.encode("utf-8")) > int(
            stdout_limit_bytes
        ):
            return "Error: Script stdout output exceeds the configured limit."
        if stderr_limit_bytes is not None and len(stderr.encode("utf-8")) > int(
            stderr_limit_bytes
        ):
            return "Error: Script stderr output exceeds the configured limit."
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        logger.info(
            (
                "Python script completed (%s): resolved_path=%s return_code=%s "
                "elapsed_ms=%s stdout_chars=%s stderr_chars=%s"
            ),
            call_context,
            resolved_path,
            completed.returncode,
            elapsed_ms,
            len(stdout),
            len(stderr),
        )

        if completed.returncode != 0:
            logger.warning(
                (
                    "Python script exited with non-zero status (%s): "
                    "resolved_path=%s return_code=%s elapsed_ms=%s "
                    "stdout_chars=%s stderr_chars=%s"
                ),
                call_context,
                resolved_path,
                completed.returncode,
                elapsed_ms,
                len(stdout),
                len(stderr),
            )
            error = "Error: Script exited with code {}.".format(completed.returncode)
            public_error = _extract_public_script_error(stderr)
            return "{} {}".format(error, public_error) if public_error else error

        if stderr and not include_stderr_on_success:
            logger.info(
                "Successful script wrote diagnostic stderr (%s): stderr_chars=%s",
                call_context,
                len(stderr),
            )
        result = (
            _combine_process_output(stdout=stdout, stderr=stderr)
            if include_stderr_on_success
            else stdout
        )
        if result:
            return result
        return "Script executed successfully with no output."

    except _ScriptOutputLimitExceeded as exc:
        logger.warning(
            "Python script %s output exceeded limit (%s): script_path=%s",
            exc.stream_name,
            call_context,
            script_path,
        )
        return "Error: Script {} output exceeds the configured limit.".format(
            exc.stream_name
        )
    except _ScriptExecutionCancelled:
        logger.info("Python script cancelled (%s): script_path=%s", call_context, script_path)
        return "Error: Script execution cancelled."
    except subprocess.TimeoutExpired as exc:
        timeout_stdout = (exc.stdout or "")
        timeout_stderr = (exc.stderr or "")
        logger.warning(
            (
                "Python script timed out (%s): script_path=%s resolved_path=%s "
                "timeout_seconds=%s stdout_chars=%s stderr_chars=%s"
            ),
            call_context,
            script_path,
            resolved_path or "n/a",
            timeout_seconds,
            len(timeout_stdout),
            len(timeout_stderr),
        )
        return "Error: Script execution timed out after {} seconds.".format(timeout_seconds)
    except Exception as exc:
        logger.error(
            "Error executing script (%s): error_type=%s",
            call_context,
            type(exc).__name__,
        )
        return _SCRIPT_EXECUTION_INTERNAL_ERROR


def _combine_process_output(*, stdout: str, stderr: str) -> str:
    if stdout and stderr:
        return "STDOUT:\n{}\n\nSTDERR:\n{}".format(stdout, stderr)
    if stdout:
        return stdout
    if stderr:
        return stderr
    return ""


_REQUEST_ITEM_SPLIT_PATTERN = re.compile(r"[\r\n;；]+")
_EVIDENCE_NOISE_PATTERN = re.compile(r"[\W_]+", re.UNICODE)


def _normalize_argument_evidence(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return _EVIDENCE_NOISE_PATTERN.sub("", normalized)


def _flatten_item_evidence(value: Any) -> List[str]:
    values: List[str] = []
    if isinstance(value, Mapping):
        for nested in value.values():
            values.extend(_flatten_item_evidence(nested))
    elif isinstance(value, (list, tuple)):
        for nested in value:
            values.extend(_flatten_item_evidence(nested))
    elif value is not None and not isinstance(value, bool):
        normalized = _normalize_argument_evidence(value)
        if len(normalized) >= 2:
            values.append(normalized)
    return list(dict.fromkeys(values))


def _flatten_item_field_evidence(
    value: Any,
    path: str = "",
) -> List[tuple[str, str]]:
    fields: List[tuple[str, str]] = []
    if isinstance(value, Mapping):
        for key, nested in value.items():
            child_path = "{}.{}".format(path, key) if path else str(key)
            fields.extend(_flatten_item_field_evidence(nested, child_path))
    elif isinstance(value, (list, tuple)):
        child_path = "{}[]".format(path) if path else "[]"
        for nested in value:
            fields.extend(_flatten_item_field_evidence(nested, child_path))
    elif value is not None and not isinstance(value, bool):
        normalized = _normalize_argument_evidence(value)
        if path and len(normalized) >= 2:
            fields.append((path, normalized))
    return list(dict.fromkeys(fields))


def _structured_collections(
    value: Any,
    path: str = "arguments",
) -> List[tuple[str, List[Mapping[str, Any]]]]:
    collections: List[tuple[str, List[Mapping[str, Any]]]] = []
    if isinstance(value, Mapping):
        for key, nested in value.items():
            child_path = "{}.{}".format(path, key)
            parsed = nested
            if isinstance(nested, str) and nested.lstrip().startswith("["):
                try:
                    parsed = json.loads(nested)
                except (json.JSONDecodeError, TypeError):
                    parsed = nested
            if (
                isinstance(parsed, list)
                and len(parsed) >= 2
                and all(isinstance(item, Mapping) for item in parsed)
            ):
                collections.append((child_path, list(parsed)))
            else:
                collections.extend(_structured_collections(parsed, child_path))
    return collections


def _validate_structured_arguments_against_request(
    arguments: Mapping[str, Any],
    request_text: str,
    trusted_tool_evidence: str = "",
) -> List[str]:
    """Reject cross-item value pairing when the current request enumerates a collection.

    The check is deliberately conservative: it activates only when multiple scalar
    values from most items locate a unique request segment. It then verifies fields
    shared by the collection against each located segment. Collections built from
    internal data, or requests without item-level evidence, are ignored.
    """
    full_request = _normalize_argument_evidence(request_text)
    if not full_request:
        return []
    raw_segments = [
        part.strip()
        for part in _REQUEST_ITEM_SPLIT_PATTERN.split(request_text)
        if _normalize_argument_evidence(part)
    ]
    segments = [_normalize_argument_evidence(part) for part in raw_segments]
    if len(segments) < 2:
        return []

    normalized_tool_evidence = _normalize_argument_evidence(trusted_tool_evidence)
    errors: List[str] = []
    for path, items in _structured_collections(arguments):
        if normalized_tool_evidence and all(
            _item_is_grounded_in_tool_evidence(item, normalized_tool_evidence)
            for item in items
        ):
            continue
        item_fields = [_flatten_item_field_evidence(item) for item in items]
        anchored: List[tuple[int, int, List[tuple[str, str]]]] = []
        for index, fields in enumerate(item_fields):
            grounded_values = list(
                dict.fromkeys(value for _field, value in fields if value in full_request)
            )
            if len(grounded_values) < 2:
                continue
            scores = [
                sum(1 for value in grounded_values if value in segment)
                for segment in segments
            ]
            best_score = max(scores, default=0)
            if best_score < 2:
                continue
            best_positions = [
                position for position, score in enumerate(scores) if score == best_score
            ]
            if len(best_positions) == 1:
                anchored.append((index, best_positions[0], fields))

        minimum_anchor_count = max(2, (len(items) + 1) // 2)
        if len(anchored) >= minimum_anchor_count:
            field_item_counts: Dict[str, int] = {}
            field_segment_support: Set[str] = set()
            field_cross_segment_support: Dict[str, int] = {}
            field_values: Dict[str, Set[str]] = {}
            anchor_segment_positions = {position for _index, position, _fields in anchored}
            for _index, segment_position, fields in anchored:
                item_field_paths = {field for field, _value in fields}
                for field in item_field_paths:
                    field_item_counts[field] = field_item_counts.get(field, 0) + 1
                segment = segments[segment_position]
                field_segment_support.update(
                    field for field, value in fields if value in segment
                )
                cross_supported_fields: Set[str] = set()
                for field, value in fields:
                    field_values.setdefault(field, set()).add(value)
                    if value in segment:
                        continue
                    if any(
                        value in segments[other_position]
                        for other_position in anchor_segment_positions
                        if other_position != segment_position
                    ):
                        cross_supported_fields.add(field)
                for field in cross_supported_fields:
                    field_cross_segment_support[field] = (
                        field_cross_segment_support.get(field, 0) + 1
                    )

            comparable_fields = {
                field
                for field, item_count in field_item_counts.items()
                if item_count >= 2
                and (
                    field in field_segment_support
                    or (
                        field_cross_segment_support.get(field, 0) >= 2
                        and len(field_values.get(field, set())) >= 2
                    )
                )
            }
            conflicting_items: List[tuple[int, int]] = []
            conflicting_fields: Set[str] = set()
            for index, segment_position, fields in anchored:
                segment = segments[segment_position]
                mismatched = {
                    field
                    for field, value in fields
                    if field in comparable_fields and value not in segment
                }
                if mismatched:
                    conflicting_items.append((index + 1, segment_position))
                    conflicting_fields.update(mismatched)

            if conflicting_items:
                request_evidence = " | ".join(
                    "item {}: {}".format(
                        item_number,
                        raw_segments[segment_position].replace("\n", " ")[:240],
                    )
                    for item_number, segment_position in conflicting_items
                )
                errors.append(
                    "{} item {} fields {} conflict with the corresponding "
                    "current-request items. Request evidence: {}".format(
                        path,
                        ", ".join(str(index) for index, _position in conflicting_items),
                        ", ".join(sorted(conflicting_fields)),
                        request_evidence,
                    )
                )
                continue

            anchored_positions = [
                position
                for _index, position, _fields in sorted(
                    anchored,
                    key=lambda entry: entry[0],
                )
            ]
            if any(
                current <= previous
                for previous, current in zip(
                    anchored_positions,
                    anchored_positions[1:],
                )
            ):
                errors.append("{} item order conflicts with the current request".format(path))
                continue

        evidenced: List[tuple[int, List[int]]] = []
        for index, item in enumerate(items):
            values = [value for value in _flatten_item_evidence(item) if value in full_request]
            if len(values) < 2:
                continue
            candidates = [
                segment_index
                for segment_index, segment in enumerate(segments)
                if all(value in segment for value in values)
            ]
            evidenced.append((index, candidates))

        if len(evidenced) < 2:
            continue
        matched_count = sum(1 for _index, candidates in evidenced if candidates)
        unmatched = [index + 1 for index, candidates in evidenced if not candidates]
        if unmatched and matched_count >= max(1, (len(evidenced) + 1) // 2):
            errors.append(
                "{} item {} combines values that do not co-occur in the current request".format(
                    path,
                    ", ".join(str(index) for index in unmatched),
                )
            )
            continue
        if unmatched:
            continue

        candidate_positions = {position for _index, candidates in evidenced for position in candidates}
        if len(candidate_positions) < len(evidenced):
            continue
        previous = -1
        ordered = True
        for _index, candidates in evidenced:
            next_position = next((position for position in candidates if position > previous), None)
            if next_position is None:
                ordered = False
                break
            previous = next_position
        if not ordered:
            errors.append("{} item order conflicts with the current request".format(path))
    return errors


def _item_is_grounded_in_tool_evidence(
    item: Mapping[str, Any],
    normalized_evidence: str,
) -> bool:
    """Return whether a structured item is substantially present in trusted tool output.

    Business query results are legitimate script inputs even when they do not occur in
    the user's prose. Requiring every usable scalar avoids treating a collection as
    trusted merely because it shares generic IDs or labels with a query result.
    """
    values = _flatten_item_evidence(item)
    if len(values) < 2:
        return False
    supported = sum(1 for value in values if value in normalized_evidence)
    return supported / len(values) >= 0.75


def _current_turn_business_data_evidence(
    messages: Sequence[Mapping[str, Any]],
) -> str:
    """Collect business-data tool output produced after the latest user request."""
    latest_user_index = -1
    for index, message in enumerate(messages):
        if str(message.get("role") or "").lower() == "user":
            latest_user_index = index

    evidence: List[str] = []
    for message in messages[latest_user_index + 1:]:
        if str(message.get("role") or "").lower() != "tool":
            continue
        tool_name = str(message.get("name") or message.get("tool_name") or "")
        if tool_name != "business_data_search":
            continue
        content = message.get("content")
        if isinstance(content, str):
            evidence.append(content)
        elif isinstance(content, list):
            evidence.extend(
                str(item.get("text") or item.get("content") or "")
                for item in content
                if isinstance(item, Mapping)
            )
    return "\n".join(part for part in evidence if part)


def _latest_user_request(messages: Sequence[Mapping[str, Any]]) -> str:
    for message in reversed(messages):
        if str(message.get("role") or "").lower() != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                str(item.get("text") or "")
                for item in content
                if isinstance(item, Mapping) and item.get("type") in {None, "text"}
            ]
            return "\n".join(part for part in parts if part)
    return ""


def _extract_public_script_error(stderr: str) -> Optional[str]:
    """Read the script's explicit, bounded public error contract."""
    for raw_line in str(stderr or "").splitlines():
        line = raw_line.strip()
        if not line.startswith(_PUBLIC_SCRIPT_ERROR_PREFIX):
            continue
        serialized = line[len(_PUBLIC_SCRIPT_ERROR_PREFIX):].strip()
        try:
            payload = json.loads(serialized)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, Mapping):
            continue
        code = str(payload.get("code") or "").strip()
        message = payload.get("message")
        if not _PUBLIC_SCRIPT_ERROR_CODE_PATTERN.fullmatch(code):
            continue
        if not isinstance(message, str):
            continue
        normalized_message = " ".join(message.split())[
            :_PUBLIC_SCRIPT_ERROR_MESSAGE_MAX_LENGTH
        ].strip()
        return "[{}]{}".format(
            code,
            " {}".format(normalized_message) if normalized_message else "",
        )
    return None


def handle_execute_script(
    *,
    ontology_id: str,
    ontology_version: Optional[str] = None,
    operation_key: str,
    arguments: Optional[Mapping[str, Any]] = None,
    base_path: Optional[Path] = None,
    timeout_seconds: int = 300,
    tool_call_id: Optional[str] = None,
    caller: Optional[str] = None,
    run_id: Optional[str] = None,
    session_id: Optional[str] = None,
    turn_id: Optional[int] = None,
    workspace_id: Optional[str] = None,
    runtime_context: Optional[Mapping[str, Any]] = None,
) -> str:
    manifest_kwargs = {"workspace_id": workspace_id}
    if str(ontology_version or "").strip():
        manifest_kwargs["version"] = ontology_version
    ontology, error = get_ontology_manifest(ontology_id, **manifest_kwargs)
    if error:
        return error
    assert ontology is not None

    ontology_dir = Path(ontology.ontology_dir).resolve()
    try:
        from ontology.operation_execution import is_deterministic_operation

        if is_deterministic_operation(ontology_dir, operation_key):
            return "Error: deterministic_operation_protected"
    except Exception:
        logger.exception(
            "Failed to classify deterministic operation protection: ontology_id=%s operation_key=%s",
            ontology.id,
            operation_key,
        )
        return "Error: deterministic_operation_protection_unavailable"
    script_catalog = load_script_catalog(ontology_dir)
    script_entry = find_script_entry(script_catalog, operation_key, bound_only=True)
    if script_entry is None:
        return (
            "Error: Operation '{}' in ontology '{}' has no registered script implementation or is not a bound operation."
        ).format(operation_key, ontology.id)

    raw_script_path = str(script_entry.get("_script_path") or "").strip()
    if not raw_script_path:
        return "Error: Registered script path missing for operation '{}'.".format(operation_key)

    project_root = Path(base_path).resolve() if base_path else None
    if project_root is None:
        return "Error: Project base path not configured"

    resolved_script_path = resolve_ontology_script_path(ontology_dir, raw_script_path)
    if resolved_script_path is None:
        return "Error: Registered script for operation '{}' is outside ontology scripts directory.".format(
            operation_key
        )
    try:
        script_path = str(resolved_script_path.relative_to(project_root)).replace("\\", "/")
    except ValueError:
        return "Error: Registered script for operation '{}' is outside project root.".format(
            operation_key
        )

    cli_args, missing_args, invalid_args = _build_cli_args(
        script_args=list(script_entry.get("arguments") or []),
        script_parameters=list(script_entry.get("parameters") or []),
        arguments=dict(arguments or {}),
        runtime_context=runtime_context,
    )
    if invalid_args:
        return "Error: Invalid script argument(s) for operation '{}': {}.".format(
            operation_key,
            "; ".join(invalid_args),
        )
    if missing_args:
        return "Error: Missing required script argument(s) for operation '{}': {}.".format(
            operation_key,
            ", ".join(missing_args),
        )
    return handle_execute_script_path(
        script_path=script_path,
        args=cli_args,
        base_path=base_path,
        timeout_seconds=timeout_seconds,
        tool_call_id=tool_call_id,
        caller=caller,
        run_id=run_id,
        session_id=session_id,
        turn_id=turn_id,
        extra_pythonpath_dirs=[ontology_dir],
        env_overrides=_build_script_env_overrides(
            workspace_id=workspace_id,
            ontology_id=ontology_id,
        ),
        workspace_id=workspace_id,
        ontology_id=ontology_id,
        ontology_dir=ontology_dir,
    )


def _build_cli_args(
    *,
    script_args: Sequence[str],
    script_parameters: Sequence[Mapping[str, Any]],
    arguments: Mapping[str, Any],
    runtime_context: Optional[Mapping[str, Any]] = None,
) -> tuple[List[str], List[str], List[str]]:
    """Build CLI argument list from script arg definitions.

    Returns (result, missing, invalid) where *missing* lists required parameter
    labels not supplied and *invalid* lists arguments with unknown keys or
    invalid values.

    ``<runtime.xxx>`` placeholders are filled only from ``runtime_context``;
    caller-supplied arguments are never used as a fallback for them.
    """
    if script_parameters:
        return _build_cli_args_from_parameters(
            script_parameters=script_parameters,
            arguments=arguments,
            runtime_context=runtime_context,
        )

    # Legacy path: no structured parameters declared.
    # If no positional args are defined either, reject any caller-supplied arguments.
    if not script_args and arguments:
        unknown = sorted(str(k) for k in arguments.keys() if arguments[k] is not None)
        if unknown:
            return [], [], ["Operation '{}' declares no parameters".format(unknown[0])]

    result: List[str] = []
    consumed: Set[str] = set()
    missing: List[str] = []
    invalid: List[str] = []
    index = 0
    while index < len(script_args):
        item = str(script_args[index])
        if item.startswith("--"):
            result.append(item)
            placeholder = str(script_args[index + 1]) if index + 1 < len(script_args) else ""
            value, matched_key = _resolve_placeholder(
                placeholder=placeholder,
                arguments=arguments,
                runtime_context=runtime_context,
            )
            if value is not None:
                result.append(str(value))
                if matched_key:
                    consumed.add(matched_key)
            elif _is_runtime_placeholder(placeholder):
                # Runtime values are backend-owned.  Pop the flag that was
                # already added and report an incomplete backend context.
                if result and result[-1] == item:
                    result.pop()
                runtime_match = _RUNTIME_PLACEHOLDER_PATTERN.match(placeholder.strip())
                declared_runtime_key = (
                    runtime_match.group(1).replace("-", "_")
                    if runtime_match
                    else placeholder.strip()
                )
                runtime_key = runtime_parameter_field(
                    {"runtime": True, "placeholder": placeholder}
                )
                if runtime_key is None:
                    invalid.append(
                        "Unsupported runtime parameter '{}'".format(declared_runtime_key)
                    )
                elif runtime_context is None or runtime_context.get(runtime_key) is None:
                    invalid.append("Runtime context missing '{}'".format(runtime_key))
                for candidate in {
                    declared_runtime_key,
                    declared_runtime_key.replace("_", "-"),
                    "--" + declared_runtime_key.replace("_", "-"),
                }:
                    if candidate in arguments:
                        consumed.add(candidate)
            elif _is_optional_placeholder(placeholder):
                # Optional parameter not provided — remove the flag and skip
                if result and result[-1] == item:
                    result.pop()
            elif placeholder.startswith("<") and placeholder.endswith(">"):
                missing.append(placeholder[1:-1].strip())
            index += 2 if placeholder and not placeholder.startswith("--") else 1
            continue
        index += 1

    if invalid:
        return [], missing, invalid
    if result:
        return result, missing, []

    for key, value in arguments.items():
        if value is None:
            continue
        if str(key) in consumed:
            continue
        flag = str(key)
        result.append(flag if flag.startswith("--") else "--{}".format(flag.replace("_", "-")))
        result.append(str(value))
    return result, missing, []


_RUNTIME_PLACEHOLDER_PATTERN = re.compile(r"^<runtime\.([\w\-]+)>$")
_OPTIONAL_PLACEHOLDER_PATTERN = re.compile(r"^<\?([^>]+)>$")


def _is_runtime_placeholder(value: str) -> bool:
    return bool(_RUNTIME_PLACEHOLDER_PATTERN.match(value.strip()))


def _is_optional_placeholder(value: str) -> bool:
    return bool(_OPTIONAL_PLACEHOLDER_PATTERN.match(value.strip()))


def _resolve_placeholder(
    *,
    placeholder: str,
    arguments: Mapping[str, Any],
    runtime_context: Optional[Mapping[str, Any]] = None,
) -> tuple[Optional[Any], Optional[str]]:
    """Resolve a single placeholder — either business or runtime.

    Returns (value, matched_key).  *value* is None when the placeholder cannot
    be resolved (caller decides whether it should be reported as missing or
    silently skipped).
    """
    stripped = placeholder.strip()
    m = _RUNTIME_PLACEHOLDER_PATTERN.match(stripped)
    if m:
        key = runtime_parameter_field({"runtime": True, "placeholder": stripped})
        if key is None:
            return None, None
        if runtime_context is not None:
            val = runtime_context.get(key)
            if val is not None:
                return val, None
        return None, None

    # Business argument lookup (existing logic)
    candidates: List[str] = []
    if stripped.startswith("<") and stripped.endswith(">"):
        placeholder_key = stripped[1:-1].strip()
        # Strip ? prefix for optional <?xxx> placeholders
        if placeholder_key.startswith("?"):
            placeholder_key = placeholder_key[1:]
        candidates.append(placeholder_key)
        candidates.append(placeholder_key.replace("-", "_"))
        candidates.append(placeholder_key.replace("_", "-"))

    for candidate in candidates:
        if candidate in arguments:
            return arguments[candidate], candidate
    return None, None


def _resolve_argument_value(
    *,
    flag: str,
    placeholder: str,
    arguments: Mapping[str, Any],
) -> tuple[Optional[Any], Optional[str]]:
    candidates: List[str] = []
    stripped_flag = flag.lstrip("-")
    candidates.extend([flag, stripped_flag, stripped_flag.replace("-", "_")])
    placeholder_key = placeholder.strip()
    if placeholder_key.startswith("<") and placeholder_key.endswith(">"):
        placeholder_key = placeholder_key[1:-1].strip()
    if placeholder_key:
        candidates.append(placeholder_key)

    for candidate in candidates:
        if candidate in arguments:
            return arguments[candidate], candidate
    return None, None


def _build_cli_args_from_parameters(
    *,
    script_parameters: Sequence[Mapping[str, Any]],
    arguments: Mapping[str, Any],
    runtime_context: Optional[Mapping[str, Any]] = None,
) -> tuple[List[str], List[str], List[str]]:
    """Build CLI arguments from structured parameter definitions.

    Returns (result, missing, invalid) where *missing* lists required parameter
    labels the caller did not supply and *invalid* lists unknown or malformed
    arguments.
    """
    result: List[str] = []
    missing: List[str] = []
    invalid: List[str] = []
    consumed: Set[str] = set()

    for parameter in script_parameters:
        name = _parameter_name(parameter)
        if not name:
            continue
        flag = str(parameter.get("flag") or "").strip()
        required = bool(parameter.get("required", True))
        multiple = bool(parameter.get("multiple", False))

        runtime_owned = is_runtime_parameter(parameter)
        runtime_field = runtime_parameter_field(parameter)
        if runtime_owned:
            matched_key = None
            # User-provided runtime aliases are deliberately consumed but
            # ignored; backend context remains authoritative.
            for candidate in _parameter_argument_keys(parameter):
                if candidate in arguments:
                    consumed.add(candidate)
            if not runtime_field:
                invalid.append("Unsupported runtime parameter '{}'".format(name))
                continue
            value = runtime_context.get(runtime_field) if runtime_context else None
        else:
            value, matched_key = _resolve_parameter_value(parameter=parameter, arguments=arguments)

        if matched_key:
            consumed.add(matched_key)
        if value is None:
            if runtime_field:
                if required:
                    invalid.append("Runtime context missing '{}'".format(runtime_field))
                continue
            if "default" in parameter and parameter.get("default") is not None:
                value = parameter.get("default")
            elif required:
                missing.append(_parameter_label(parameter))
                continue
            else:
                continue

        if multiple:
            values = _coerce_multiple_values(value)
            if not values:
                if required:
                    missing.append(_parameter_label(parameter))
                continue
            for item in values:
                converted, error = _convert_parameter_value(item, parameter)
                if error:
                    invalid.append("{}: {}".format(name, error))
                    continue
                append_script_parameter_cli_argument(
                    result,
                    parameter=parameter,
                    flag=flag,
                    value=converted,
                )
            continue

        if isinstance(value, (list, tuple, set, dict)) and not _accepts_structured_value(parameter):
            invalid.append("{} does not accept multiple values".format(name))
            continue
        converted, error = _convert_parameter_value(value, parameter)
        if error:
            invalid.append("{}: {}".format(name, error))
            continue
        append_script_parameter_cli_argument(
            result,
            parameter=parameter,
            flag=flag,
            value=converted,
        )

    for key, value in arguments.items():
        if value is None or str(key) in consumed:
            continue
        invalid.append("Unknown argument '{}'".format(key))

    return result, missing, invalid


def _parameter_name(parameter: Mapping[str, Any]) -> str:
    return str(parameter.get("name") or "").strip()


def _parameter_label(parameter: Mapping[str, Any]) -> str:
    description = str(parameter.get("description") or "").strip()
    return description or _parameter_name(parameter)


def _resolve_parameter_value(
    *,
    parameter: Mapping[str, Any],
    arguments: Mapping[str, Any],
) -> tuple[Optional[Any], Optional[str]]:
    for candidate in script_parameter_argument_keys(parameter):
        if candidate in arguments:
            return arguments[candidate], candidate
    return None, None


def _parameter_argument_keys(parameter: Mapping[str, Any]) -> Set[str]:
    """Return all user-facing aliases for a structured parameter."""
    return set(script_parameter_argument_keys(parameter))


def _coerce_multiple_values(value: Any) -> List[Any]:
    if isinstance(value, (list, tuple, set)):
        return [item for item in value if item is not None]
    return [value]


def _convert_parameter_value(
    value: Any,
    parameter: Mapping[str, Any],
) -> tuple[Optional[str], Optional[str]]:
    """Convert a resolved parameter value to its CLI string representation.

    Returns (converted_value, error).  Exactly one is None.
    """
    param_type = str(parameter.get("type") or "string").lower()
    if param_type == "string" and isinstance(value, (list, tuple, set, dict)):
        serializable = list(value) if isinstance(value, (tuple, set)) else value
        try:
            return json.dumps(serializable, ensure_ascii=False, separators=(",", ":")), None
        except (TypeError, ValueError):
            return None, "expected string or JSON-serializable structured value"
    raw = str(value)
    if param_type in {"array", "list"} or param_type.endswith("[]"):
        parsed = value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return None, "expected array"
        if isinstance(parsed, (tuple, set)):
            parsed = list(parsed)
        if not isinstance(parsed, list):
            return None, "expected array"
        return json.dumps(parsed, ensure_ascii=False, separators=(",", ":")), None
    if param_type in {"object", "dict"}:
        parsed = value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return None, "expected object"
        if not isinstance(parsed, dict):
            return None, "expected object"
        return json.dumps(parsed, ensure_ascii=False, separators=(",", ":")), None
    if param_type == "json":
        parsed = value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return None, "expected JSON"
        try:
            return json.dumps(parsed, ensure_ascii=False, separators=(",", ":")), None
        except (TypeError, ValueError):
            return None, "expected JSON"
    if param_type in ("int", "integer"):
        try:
            return str(int(value)), None
        except (TypeError, ValueError):
            return None, "expected integer"
    if param_type in ("float", "number"):
        try:
            return str(float(value)), None
        except (TypeError, ValueError):
            return None, "expected number"
    if param_type in {"bool", "boolean"}:
        if isinstance(value, bool):
            return str(value).lower(), None
        normalized = raw.lower()
        if normalized in {"true", "1", "yes"}:
            return "true", None
        if normalized in {"false", "0", "no"}:
            return "false", None
        return None, "expected boolean"
    return raw, None


def _is_json_collection_parameter(parameter: Mapping[str, Any]) -> bool:
    param_type = str(parameter.get("type") or "string").strip().lower()
    return param_type in {"array", "list", "object", "dict", "json"} or param_type.endswith("[]")


def _accepts_structured_value(parameter: Mapping[str, Any]) -> bool:
    param_type = str(parameter.get("type") or "string").strip().lower()
    return param_type == "string" or _is_json_collection_parameter(parameter)


def _append_cli_argument(result: List[str], *, flag: str, value: str) -> None:
    """Append a flag-value pair to the CLI argument list."""
    if flag:
        result.append(flag)
    result.append(value)


@dataclass(frozen=True)
class ExecuteScriptTool:
    name: str = "execute_script"
    schema: Dict[str, Any] = None  # type: ignore[assignment]
    allowed_callers: Set[ToolCaller] = None  # type: ignore[assignment]
    concurrency_safe: bool = False
    max_parallelism: Optional[int] = None
    category: str = "regular"

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema", EXECUTE_SCRIPT_TOOL_SCHEMA)
        object.__setattr__(self, "allowed_callers", {"main", "subagent", "acp"})

    def execute(
        self,
        *,
        args: Dict[str, Any],
        context: ToolContext,
        tool_call_id: Optional[str] = None,
        event_emitter: Optional[Callable[..., Any]] = None,
    ) -> ToolOutcome:
        del event_emitter
        ontology_id = str(args.get("ontology_id") or "").strip()
        operation_key = str(args.get("operation_key") or "").strip()
        context_messages = getattr(context, "messages", ()) or ()
        if ontology_id and operation_key:
            successful_calls = _prior_successful_operation_calls(
                context_messages,
                ontology_id=ontology_id,
                operation_key=operation_key,
                current_tool_call_id=tool_call_id,
            )
            current_fingerprint = _canonical_tool_arguments(args)
            repeated_identically = any(
                _canonical_tool_arguments(call["arguments"]) == current_fingerprint
                for call in successful_calls
            )
            if repeated_identically:
                logger.warning(
                    "blocked identical successful execute_script replay: run_id=%s operation_key=%s",
                    context.run_id or "n/a",
                    operation_key,
                )
                return ToolOutcome(content=(
                    "This deterministic operation already succeeded with exactly the same inputs. "
                    "Reuse the previous successful tool result and produce the final user answer now. "
                    "Do not call execute_script again for rounding, formatting, or verification."
                ))
            if len(successful_calls) >= MAX_SUCCESSFUL_EXECUTIONS_PER_OPERATION:
                logger.warning(
                    "blocked excessive successful execute_script replays: run_id=%s operation_key=%s count=%s",
                    context.run_id or "n/a",
                    operation_key,
                    len(successful_calls),
                )
                return ToolOutcome(content=(
                    "This deterministic operation has already returned successful results multiple times. "
                    "Stop changing informational or display-only inputs. Reuse the latest successful "
                    "result and produce the final user answer now. Only a new, explicitly requested "
                    "independent scenario justifies another execution."
                ))
        argument_values = args.get("arguments")
        if isinstance(argument_values, Mapping):
            consistency_errors = _validate_structured_arguments_against_request(
                argument_values,
                _latest_user_request(context_messages),
                _current_turn_business_data_evidence(context_messages),
            )
            if consistency_errors:
                return ToolOutcome(content=(
                    "Error: Script argument consistency check failed: {}. "
                    "The script did not run. "
                    "Rebuild the complete collection from the current user request, "
                    "preserve item order, and retry the complete call without using "
                    "ontology examples or history. Do not provide computed results or "
                    "claim success until execute_script returns a successful result."
                ).format("; ".join(consistency_errors)))
        call_args = {
            key: args[key]
            for key in ("ontology_id", "operation_key", "arguments")
            if key in args
        }
        if context.project_root:
            # Keep the caller's path representation intact; normalize it at the
            # execution boundary so POSIX-style paths remain observable on
            # Windows test/adapter seams.
            call_args["base_path"] = context.project_root
        call_args["tool_call_id"] = tool_call_id
        call_args["caller"] = context.caller
        call_args["run_id"] = context.run_id
        call_args["session_id"] = context.session_id
        call_args["turn_id"] = context.turn_id
        call_args["timeout_seconds"] = context.agent_config.execute_script_timeout
        call_args["workspace_id"] = context.workspace_id
        call_args["runtime_context"] = {
            "session_id": context.session_id,
            "run_id": context.run_id,
            "turn_id": str(context.turn_id),
            "user_id": context.user_id,
            "workspace_id": context.workspace_id,
            "selected_ontology_id": context.selected_ontology_id,
            "ontology_id": context.selected_ontology_id,
        }
        if context.selected_ontology_version:
            call_args["runtime_context"]["selected_ontology_version"] = context.selected_ontology_version
            call_args["runtime_context"]["ontology_version"] = context.selected_ontology_version
        if context.selected_ontology_id and context.selected_ontology_id == str(call_args.get("ontology_id") or "").strip():
            call_args["ontology_version"] = context.selected_ontology_version
        logger.info(
            (
                "execute_script tool invoked: tool_call_id=%s caller=%s "
                "run_id=%s session_id=%s turn_id=%s ontology_id=%s ontology_version=%s operation_key=%s"
            ),
            tool_call_id or "n/a",
            context.caller,
            context.run_id or "n/a",
            context.session_id or "n/a",
            context.turn_id,
            call_args.get("ontology_id"),
            call_args.get("ontology_version") or "n/a",
            call_args.get("operation_key"),
        )
        return ToolOutcome(content=handle_execute_script(**call_args))
