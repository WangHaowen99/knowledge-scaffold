"""Credential-free client for the run-scoped deterministic provider broker."""

from __future__ import annotations

import json
import os
import socket
import struct
from collections.abc import Mapping
from typing import Any


DETERMINISTIC_PROVIDER_SOCKET_ENV = "ONTO_DETERMINISTIC_PROVIDER_SOCKET"
_MAX_REQUEST_BYTES = 256 * 1024
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class DeterministicProviderCallError(RuntimeError):
    """Stable failure raised when the trusted provider broker rejects a call."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code or "provider_call_failed")
        super().__init__(str(message or "Deterministic provider call failed."))


def _read_exact(stream: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = max(0, int(size))
    while remaining:
        chunk = stream.recv(remaining)
        if not chunk:
            raise DeterministicProviderCallError(
                "provider_protocol_error",
                "Deterministic provider broker closed the connection.",
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class DeterministicProviderClient:
    """Invoke allowlisted, read-only graph operations without receiving credentials."""

    def __init__(self, *, timeout_seconds: float = 30.0) -> None:
        socket_path = str(os.getenv(DETERMINISTIC_PROVIDER_SOCKET_ENV) or "").strip()
        if not socket_path or not os.path.isabs(socket_path):
            raise DeterministicProviderCallError(
                "provider_capability_unavailable",
                "Deterministic provider capability is unavailable.",
            )
        self._socket_path = socket_path
        self._timeout_seconds = max(0.1, float(timeout_seconds))

    def execute(
        self,
        operation: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> Any:
        request = {
            "version": 1,
            "operation": str(operation or "").strip(),
            "arguments": dict(arguments or {}),
        }
        try:
            payload = json.dumps(
                request,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, OverflowError) as exc:
            raise DeterministicProviderCallError(
                "provider_arguments_invalid",
                "Deterministic provider arguments must be standard JSON values.",
            ) from exc
        if not request["operation"] or len(payload) > _MAX_REQUEST_BYTES:
            raise DeterministicProviderCallError(
                "provider_arguments_invalid",
                "Deterministic provider request is invalid.",
            )

        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
                stream.settimeout(self._timeout_seconds)
                stream.connect(self._socket_path)
                stream.sendall(struct.pack("!I", len(payload)) + payload)
                response_size = struct.unpack("!I", _read_exact(stream, 4))[0]
                if response_size <= 0 or response_size > _MAX_RESPONSE_BYTES:
                    raise DeterministicProviderCallError(
                        "provider_protocol_error",
                        "Deterministic provider response is invalid.",
                    )
                response_payload = _read_exact(stream, response_size)
        except DeterministicProviderCallError:
            raise
        except (OSError, TimeoutError) as exc:
            raise DeterministicProviderCallError(
                "provider_transport_error",
                "Deterministic provider broker is unavailable.",
            ) from exc

        try:
            response = json.loads(response_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DeterministicProviderCallError(
                "provider_protocol_error",
                "Deterministic provider response is invalid.",
            ) from exc
        if not isinstance(response, dict) or response.get("version") != 1:
            raise DeterministicProviderCallError(
                "provider_protocol_error",
                "Deterministic provider response is invalid.",
            )
        if response.get("ok") is not True:
            raise DeterministicProviderCallError(
                str(response.get("code") or "provider_call_failed"),
                str(response.get("message") or "Deterministic provider call failed."),
            )
        return response.get("result")
