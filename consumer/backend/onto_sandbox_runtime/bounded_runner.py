"""Run one sandbox child while bounding captured stdout and stderr.

This module is intentionally outside the public ontology script SDK: it owns
process and clock primitives that deterministic knowledge-package code must not
be able to import through the runtime capability contract.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import signal
import subprocess
import sys
import threading
import time
from typing import BinaryIO, Sequence


_OUTPUT_LIMIT_EXIT_CODE = 125
_FILE_SIZE_LIMIT_EXIT_CODE = 126


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    """Terminate the complete session even when its leader already exited."""
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
    if not command:
        raise ValueError("command is required")
    limits = {
        "stdout": max(0, int(stdout_limit_bytes)),
        "stderr": max(0, int(stderr_limit_bytes)),
    }
    outputs = {"stdout": bytearray(), "stderr": bytearray()}
    overflow: list[str] = []
    overflow_event = threading.Event()
    file_size_limit = max(0, int(file_size_limit_bytes))

    def apply_child_limits() -> None:
        if file_size_limit > 0:
            resource.setrlimit(
                resource.RLIMIT_FSIZE,
                (file_size_limit, file_size_limit),
            )

    process = subprocess.Popen(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        preexec_fn=apply_child_limits if file_size_limit > 0 else None,
    )

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

    assert process.stdout is not None
    assert process.stderr is not None
    readers = [
        threading.Thread(target=read_stream, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=read_stream, args=("stderr", process.stderr), daemon=True),
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
        sys.stderr.write(
            "ONTO_PUBLIC_ERROR: {}\n".format(
                json.dumps(payload, sort_keys=True, separators=(",", ":"))
            )
        )
        return _OUTPUT_LIMIT_EXIT_CODE

    file_limit_error = (
        return_code == -signal.SIGXFSZ
        or b"Errno 27" in outputs["stderr"]
        or b"File too large" in outputs["stderr"]
    )
    if file_size_limit > 0 and file_limit_error:
        payload = {
            "code": "ARTIFACT_LIMIT",
            "message": "artifact file exceeds limit",
        }
        sys.stderr.write(
            "ONTO_PUBLIC_ERROR: {}\n".format(
                json.dumps(payload, sort_keys=True, separators=(",", ":"))
            )
        )
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
