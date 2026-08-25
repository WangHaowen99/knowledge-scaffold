"""Run one sandbox child while bounding captured stdout and stderr.

This module is intentionally outside the public ontology script SDK: it owns
process and clock primitives that deterministic knowledge-package code must not
be able to import through the runtime capability contract.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from typing import BinaryIO, Sequence

try:
    import resource
except ImportError:  # pragma: no cover - exercised by Windows CI
    resource = None  # type: ignore[assignment]


_OUTPUT_LIMIT_EXIT_CODE = 125
_FILE_SIZE_LIMIT_EXIT_CODE = 126
logger = logging.getLogger("onto_sandbox_runtime.bounded_runner")


def _validated_python_argv(command: Sequence[str]) -> list[str]:
    """Return a shell-free argv restricted to the sandbox Python runtime."""
    argv = list(command)
    if not argv:
        raise ValueError("command is required")
    for argument in argv:
        if not isinstance(argument, str):
            raise ValueError("command contains an invalid argument")
        if "\x00" in argument:
            raise ValueError("command contains an invalid argument")
    requested_executable = os.path.normcase(os.path.realpath(argv[0]))
    trusted_executable = os.path.normcase(os.path.realpath(sys.executable))
    if requested_executable != trusted_executable:
        raise ValueError("command must use the trusted Python interpreter")
    return argv


def _log_public_error(payload: dict[str, str]) -> None:
    logger.error(
        "ONTO_PUBLIC_ERROR: %s",
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
    )


def _process_group_exists(process_group_id: int) -> bool:
    if not hasattr(os, "killpg"):
        return False
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    """Terminate the complete session even when its leader already exited."""
    if not hasattr(os, "killpg"):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
        return
    process_group_id = process.pid
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        if process.poll() is None:
            process.terminate()

    deadline = time.monotonic() + 1.0
    while _process_group_exists(process_group_id) and time.monotonic() < deadline:
        if process.poll() is None:
            try:
                process.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                continue
        else:
            time.sleep(0.01)

    if not _process_group_exists(process_group_id):
        return
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        if process.poll() is None:
            process.kill()


def run_bounded(
    command: Sequence[str],
    *,
    stdout_limit_bytes: int,
    stderr_limit_bytes: int,
    file_size_limit_bytes: int = 0,
) -> int:
    """Execute ``command`` and forward output only when both streams fit."""
    argv = _validated_python_argv(command)
    limits = {
        "stdout": max(0, int(stdout_limit_bytes)),
        "stderr": max(0, int(stderr_limit_bytes)),
    }
    outputs = {"stdout": bytearray(), "stderr": bytearray()}
    overflow: list[str] = []
    overflow_event = threading.Event()
    file_size_limit = max(0, int(file_size_limit_bytes))

    def apply_child_limits() -> None:
        if file_size_limit > 0 and resource is not None:
            resource.setrlimit(
                resource.RLIMIT_FSIZE,
                (file_size_limit, file_size_limit),
            )

    # Security review: execution is confined to the sandbox Python runtime;
    # argv is validated, passed as a sequence, and never parsed by a shell.
    process_options = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "shell": False,
        "close_fds": True,
    }
    if os.name == "posix":
        process_options["start_new_session"] = True
        if file_size_limit > 0 and resource is not None:
            process_options["preexec_fn"] = apply_child_limits
    elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(argv, **process_options)  # type: ignore[arg-type]  # NOSONAR(S4721)

    def read_stream(name: str, stream: BinaryIO) -> None:
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    return
                limit = limits[name]
                if limit <= 0:
                    outputs[name].extend(chunk)
                    continue
                remaining = limit + 1 - len(outputs[name])
                if remaining > 0:
                    outputs[name].extend(chunk[:remaining])
                if len(outputs[name]) > limit:
                    if not overflow:
                        overflow.append(name)
                    overflow_event.set()
                    _terminate_process_group(process)
                    return
        finally:
            stream.close()

    stdout_pipe = process.stdout
    stderr_pipe = process.stderr
    if stdout_pipe is None or stderr_pipe is None:
        _terminate_process_group(process)
        raise RuntimeError("sandbox runner output pipes are unavailable")
    readers = [
        threading.Thread(target=read_stream, args=("stdout", stdout_pipe), daemon=True),
        threading.Thread(target=read_stream, args=("stderr", stderr_pipe), daemon=True),
    ]
    for reader in readers:
        reader.start()
    process.wait()
    return_code = int(process.returncode or 0)
    # A handler may leave descendants behind after its leader exits.  They can
    # keep output pipes open or mutate the artifact directory after collection
    # starts, so every terminal path must close the complete process group.
    _terminate_process_group(process)
    for reader in readers:
        reader.join()

    if overflow_event.is_set():
        stream_name = overflow[0] if overflow else "script"
        payload = {
            "code": "OUTPUT_LIMIT",
            "message": "{} output exceeds limit".format(stream_name),
        }
        _log_public_error(payload)
        return _OUTPUT_LIMIT_EXIT_CODE

    file_limit_error = (
        return_code == -getattr(signal, "SIGXFSZ", 10**9)
        or b"Errno 27" in outputs["stderr"]
        or b"File too large" in outputs["stderr"]
    )
    if file_size_limit > 0 and resource is not None and file_limit_error:
        payload = {
            "code": "ARTIFACT_LIMIT",
            "message": "artifact file exceeds limit",
        }
        _log_public_error(payload)
        return _FILE_SIZE_LIMIT_EXIT_CODE

    sys.stdout.buffer.write(outputs["stdout"])
    sys.stderr.buffer.write(outputs["stderr"])
    return return_code


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--stdout-limit-bytes", type=int, default=0)
    parser.add_argument("--stderr-limit-bytes", type=int, default=0)
    parser.add_argument("--file-size-limit-bytes", type=int, default=0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    parsed = parser.parse_args(argv)
    command = list(parsed.command)
    if command[:1] == ["--"]:
        command = command[1:]
    return run_bounded(
        command,
        stdout_limit_bytes=parsed.stdout_limit_bytes,
        stderr_limit_bytes=parsed.stderr_limit_bytes,
        file_size_limit_bytes=parsed.file_size_limit_bytes,
    )


if __name__ == "__main__":
    raise SystemExit(main())
