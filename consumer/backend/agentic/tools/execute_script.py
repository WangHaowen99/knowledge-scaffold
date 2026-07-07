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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set

from ..business_data.resolver import (
    BusinessDataConnectionError,
    resolve_business_data_connection,
)
from ..business_data.script_env import (
    SCRIPT_BUSINESS_DATA_CONNECTION_ENV,
    serialize_script_business_data_connection,
)
from ..utils.logging import get_logger
from ..utils.ontology_catalog import (
    find_script_entry,
    get_ontology_manifest,
    load_script_catalog,
)
from .base import ToolCaller, ToolContext, ToolOutcome

logger = get_logger("tools.execute_script")

# backend/agentic/tools/execute_script.py -> parents[2] == backend/
BACKEND_DIR = Path(__file__).resolve().parents[2]
LOG_ARG_PREVIEW_LIMIT = 8
LOG_OUTPUT_PREVIEW_LIMIT = 240

# Linux ARG_MAX is typically 2 MB. When env + cmdline exceeds this,
# we offload CLI args to a temp file to bypass the kernel limit.
_MAX_ARG_SAFE_TOTAL = 1_500_000

# Windows CreateProcess command-line limit is 32,767 characters.
# When CLI args alone exceed this, also use the tempfile path.
_MAX_CLI_ARGS_SIZE = 25_000

SCRIPT_LIVE_LOG_ENABLED = (
    os.getenv("SCRIPT_LIVE_LOG_ENABLED", "false").strip().lower()
    in {"1", "true", "yes", "y", "on"}
)

EXECUTE_SCRIPT_TOOL_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "execute_script",
        "description": (
            "Execute the maintained script implementation for an ontology operation. "
            "Use this only when the operation appears in the ontology root `script_catalog`. "
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
                        "parameters are declared in that catalog. For `multiple: true` "
                        "parameters, pass an array of values."
                    ),
                },
            },
            "required": ["ontology_id", "operation_key"],
        },
    },
}


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


def _preview_cli_args(args: Sequence[str], limit: int = LOG_ARG_PREVIEW_LIMIT) -> str:
    if not args:
        return "[]"

    preview = [str(item) for item in args[:limit]]
    rendered = repr(preview)
    if len(args) <= limit:
        return rendered
    return "{} (+{} more)".format(rendered, len(args) - limit)


def _preview_output_text(text: str, limit: int = LOG_OUTPUT_PREVIEW_LIMIT) -> str:
    normalized = str(text or "").replace("\r", "\\r").replace("\n", "\\n")
    if not normalized:
        return "<empty>"
    if len(normalized) <= limit:
        return normalized
    return "{}...".format(normalized[:limit])


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
            logger.info("[script:stdout]%s %s", label, line.rstrip("\n\r"))

    def _read_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_lines.append(line)
            logger.info("[script:stderr]%s %s", label, line.rstrip("\n\r"))

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


def _spawn_with_args_file(
    resolved_path: Path,
    cli_args: List[str],
    env: Dict[str, str],
    cwd: str,
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
        connection = resolve_business_data_connection(
            workspace_id=workspace_id,
            ontology_id=ontology_id,
        )
    except BusinessDataConnectionError:
        return {}
    return {
        SCRIPT_BUSINESS_DATA_CONNECTION_ENV: serialize_script_business_data_connection(
            connection_id=connection.id,
            name=connection.name,
            provider=connection.provider,
            config=connection.config,
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

    cli_args: List[str] = []
    if args:
        for item in args:
            if item is None:
                continue
            cli_args.append(str(item))

    logger.info(
        "execute_script request received (%s): script_path=%s args=%s",
        call_context,
        script_path,
        _preview_cli_args(cli_args),
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
        # (e.g., Windows platforms with a 32 KB command-line limit)
        cli_args_size = _estimate_cmdline_size({}, *cli_args)
        if total_size > _MAX_ARG_SAFE_TOTAL or cli_args_size > _MAX_CLI_ARGS_SIZE:
            completed = _spawn_with_args_file(
                resolved_path=resolved_path,
                cli_args=cli_args,
                env=child_env,
                cwd=str(base_resolved),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(1, int(timeout_seconds)),
                check=False,
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

        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
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
                    "stdout_preview=%s stderr_preview=%s"
                ),
                call_context,
                resolved_path,
                completed.returncode,
                elapsed_ms,
                _preview_output_text(stdout),
                _preview_output_text(stderr),
            )
            details = _combine_process_output(stdout=stdout, stderr=stderr)
            if details:
                return "Error: Script exited with code {}.\n{}".format(
                    completed.returncode,
                    details,
                )
            return "Error: Script exited with code {}.".format(completed.returncode)

        result = _combine_process_output(stdout=stdout, stderr=stderr)
        if result:
            return result
        return "Script executed successfully with no output."

    except subprocess.TimeoutExpired as exc:
        timeout_stdout = (exc.stdout or "")
        timeout_stderr = (exc.stderr or "")
        logger.warning(
            (
                "Python script timed out (%s): script_path=%s resolved_path=%s "
                "timeout_seconds=%s stdout_preview=%s stderr_preview=%s"
            ),
            call_context,
            script_path,
            resolved_path or "n/a",
            timeout_seconds,
            _preview_output_text(timeout_stdout),
            _preview_output_text(timeout_stderr),
        )
        return "Error: Script execution timed out after {} seconds.".format(timeout_seconds)
    except Exception as exc:
        logger.exception(
            "Error executing script (%s): script_path=%s resolved_path=%s",
            call_context,
            script_path,
            resolved_path or "n/a",
        )
        return "Error executing script '{}': {}".format(script_path, str(exc))


def _combine_process_output(*, stdout: str, stderr: str) -> str:
    if stdout and stderr:
        return "STDOUT:\n{}\n\nSTDERR:\n{}".format(stdout, stderr)
    if stdout:
        return stdout
    if stderr:
        return stderr
    return ""


def handle_execute_script(
    *,
    ontology_id: str,
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
    ontology, error = get_ontology_manifest(ontology_id)
    if error:
        return error
    assert ontology is not None

    ontology_dir = Path(ontology.ontology_dir).resolve()
    script_catalog = load_script_catalog(ontology_dir)
    script_entry = find_script_entry(script_catalog, operation_key)
    if script_entry is None:
        return (
            "Error: Operation '{}' in ontology '{}' has no registered script implementation."
        ).format(operation_key, ontology.id)

    raw_script_path = str(script_entry.get("_script_path") or "").strip()
    if not raw_script_path:
        return "Error: Registered script path missing for operation '{}'.".format(operation_key)

    project_root = base_path.resolve() if base_path else None
    if project_root is None:
        return "Error: Project base path not configured"

    resolved_script_path = (ontology_dir / raw_script_path).resolve()
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

    When ``runtime_context`` is provided, ``<runtime.xxx>`` placeholders are
    filled from it.  When None (the Agent-loop path), runtime placeholders are
    silently skipped so that old scripts and old callers continue to work.
    """
    if script_parameters:
        return _build_cli_args_from_parameters(
            script_parameters=script_parameters,
            arguments=arguments,
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
                # Runtime placeholder skipped (Agent-loop path)
                # Pop the flag that was already added since it has no value
                if result and result[-1] == item:
                    result.pop()
            elif _is_optional_placeholder(placeholder):
                # Optional parameter not provided — remove the flag and skip
                if result and result[-1] == item:
                    result.pop()
            elif placeholder.startswith("<") and placeholder.endswith(">"):
                missing.append(placeholder[1:-1].strip())
            index += 2 if placeholder and not placeholder.startswith("--") else 1
            continue
        index += 1

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
        key = m.group(1)
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
        value, matched_key = _resolve_parameter_value(parameter=parameter, arguments=arguments)

        if matched_key:
            consumed.add(matched_key)
        if value is None:
            if required:
                missing.append(_parameter_label(parameter))
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
                _append_cli_argument(result, flag=flag, value=converted)
            continue

        if isinstance(value, (list, tuple, set)):
            invalid.append("{} does not accept multiple values".format(name))
            continue
        converted, error = _convert_parameter_value(value, parameter)
        if error:
            invalid.append("{}: {}".format(name, error))
            continue
        _append_cli_argument(result, flag=flag, value=converted)

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
    candidates: List[str] = []
    name = _parameter_name(parameter)
    flag = str(parameter.get("flag") or "").strip()
    description = str(parameter.get("description") or "").strip()
    if name:
        candidates.append(name)
    if flag:
        stripped_flag = flag.lstrip("-")
        candidates.extend([flag, stripped_flag, stripped_flag.replace("-", "_")])
    if description:
        candidates.append(description)
    aliases = parameter.get("aliases")
    if isinstance(aliases, list):
        candidates.extend(str(alias).strip() for alias in aliases if str(alias or "").strip())

    for candidate in candidates:
        if candidate in arguments:
            return arguments[candidate], candidate
    return None, None


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
    raw = str(value)
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
    if param_type == "bool":
        if isinstance(value, bool):
            return str(value).lower(), None
        normalized = raw.lower()
        if normalized in {"true", "1", "yes"}:
            return "true", None
        if normalized in {"false", "0", "no"}:
            return "false", None
        return None, "expected boolean"
    return raw, None


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
        call_args = dict(args)
        if not call_args.get("base_path") and context.project_root:
            call_args["base_path"] = Path(context.project_root)
        call_args.setdefault("tool_call_id", tool_call_id)
        call_args.setdefault("caller", context.caller)
        call_args.setdefault("run_id", context.run_id)
        call_args.setdefault("session_id", context.session_id)
        call_args.setdefault("turn_id", context.turn_id)
        call_args.setdefault("timeout_seconds", context.agent_config.execute_script_timeout)
        call_args.setdefault("workspace_id", context.workspace_id)
        if "runtime_context" not in call_args:
            call_args["runtime_context"] = {
                "insight-id": context.session_id,
                "run_id": context.run_id,
                "turn_id": str(context.turn_id),
                "workspace_id": context.workspace_id,
            }
        logger.info(
            (
                "execute_script tool invoked: tool_call_id=%s caller=%s "
                "run_id=%s session_id=%s turn_id=%s ontology_id=%s operation_key=%s"
            ),
            tool_call_id or "n/a",
            context.caller,
            context.run_id or "n/a",
            context.session_id or "n/a",
            context.turn_id,
            call_args.get("ontology_id"),
            call_args.get("operation_key"),
        )
        return ToolOutcome(content=handle_execute_script(**call_args))
