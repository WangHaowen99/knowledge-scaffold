# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Host-owned, credential-bearing broker for deterministic graph capabilities."""

from __future__ import annotations

import json
import ipaddress
import os
import socket
import struct
import threading
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import SplitResult, urlsplit

from agent.tools.business_data.deterministic import DeterministicProviderBinding
from agent.tools.business_data.providers.graph.backends.base import (
    GraphBackendError,
    GraphBackendTimeoutError,
)
from agent.tools.business_data.providers.graph.models import GraphBackendResult
from agent.tools.business_data.providers.graph.scope import build_graph_scope
from agent.tools.business_data.providers.graph.property_types import (
    GraphFilterTypeResolver,
    normalize_pattern_filters,
)


_MAX_REQUEST_BYTES = 256 * 1024
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_SOCKET_NAME = "provider.sock"
_MAX_QUERY_TIMEOUT_SECONDS = 30.0
_MAX_CONNECT_TIMEOUT_SECONDS = 10.0
_OPERATION_ARGUMENTS = {
    "get_nodes_schema": frozenset({"element_types"}),
    "get_edges_schema": frozenset({"relation_types"}),
    "property_filter": frozenset(
        {
            "element_class",
            "element_type",
            "filter_dict",
            "return_properties",
            "limit",
            "offset",
        }
    ),
    "count_search": frozenset({"element_class", "element_type", "filter_dict"}),
    "aggregate_search": frozenset(
        {"element_class", "element_type", "target_property", "agg", "filter_dict"}
    ),
    "sorted_search": frozenset(
        {
            "element_class",
            "element_type",
            "filter_dict",
            "return_properties",
            "sort_by",
            "ascending",
            "limit",
            "offset",
        }
    ),
    "pattern_search": frozenset(
        {"path_pattern", "return_vars", "limit", "return_properties"}
    ),
    "hop_search": frozenset(
        {"entity_instance_id", "hop_num", "accurate_flag"}
    ),
    "shortest_path_search": frozenset(
        {
            "start_label",
            "start_props",
            "end_label",
            "end_props",
            "max_depth",
            "directed",
        }
    ),
    "list_logic_titles": frozenset({"entity_type"}),
    "get_logic_content": frozenset({"entity_instance_ids", "logic_titles"}),
    "get_element_info": frozenset(
        {"element_instance_id", "element_class", "element_type"}
    ),
}


class DeterministicProviderBrokerError(RuntimeError):
    """Raised when a run-scoped broker cannot be created safely."""


def _read_exact(stream: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = max(0, int(size))
    while remaining:
        chunk = stream.recv(remaining)
        if not chunk:
            raise OSError("provider broker request ended early")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _endpoint_identity_matches(original: SplitResult, pinned: SplitResult) -> bool:
    if not original.scheme:
        return False
    if original.fragment or pinned.fragment:
        return False
    original_identity = (
        original.scheme.casefold(),
        original.port,
        original.path,
        original.query,
    )
    pinned_identity = (
        pinned.scheme.casefold(),
        pinned.port,
        pinned.path,
        pinned.query,
    )
    return original_identity == pinned_identity


def _provider_config(binding: DeterministicProviderBinding) -> dict[str, Any]:
    connection = binding.effective_connection
    provider = str(getattr(binding, "provider", None) or connection.provider).strip().casefold()
    config = dict(connection.options)
    try:
        query_timeout = float(
            config.get("graph_query_timeout_seconds")
            or _MAX_QUERY_TIMEOUT_SECONDS
        )
        connect_timeout = float(
            config.get("graph_connect_timeout_seconds")
            or _MAX_CONNECT_TIMEOUT_SECONDS
        )
    except (TypeError, ValueError) as exc:
        raise DeterministicProviderBrokerError(
            "deterministic provider timeout is invalid"
        ) from exc
    config["graph_query_timeout_seconds"] = min(
        _MAX_QUERY_TIMEOUT_SECONDS,
        max(0.001, query_timeout),
    )
    config["graph_connect_timeout_seconds"] = min(
        _MAX_CONNECT_TIMEOUT_SECONDS,
        max(0.001, connect_timeout),
    )
    config["max_retries"] = 0
    original = urlsplit(connection.endpoint)
    pinned_endpoint = str(getattr(binding, "network_endpoint", None) or "").strip()
    pinned = urlsplit(pinned_endpoint)
    try:
        ipaddress.ip_address(str(pinned.hostname or ""))
    except ValueError as exc:
        raise DeterministicProviderBrokerError(
            "deterministic provider endpoint is not pinned to an IP address"
        ) from exc
    if not _endpoint_identity_matches(original, pinned):
        raise DeterministicProviderBrokerError(
            "deterministic provider endpoint pin does not match its origin"
        )
    if provider == "neo4j":
        config["uri"] = connection.endpoint
        config["resolved_uri"] = pinned_endpoint
    elif provider == "hugegraph":
        config["url"] = connection.endpoint
        config["resolved_url"] = pinned_endpoint
    else:
        raise DeterministicProviderBrokerError("unsupported deterministic provider")
    if connection.auth.type == "bearer":
        config["token"] = connection.auth.token
    elif connection.auth.type == "mindsdb_password":
        config["username"] = connection.auth.username
        config["password"] = connection.auth.password
    return config


def _build_backend(binding: DeterministicProviderBinding) -> Any:
    config = _provider_config(binding)
    provider = str(
        getattr(binding, "provider", None) or binding.effective_connection.provider
    ).strip().casefold()
    if provider == "neo4j":
        from agent.tools.business_data.providers.graph.backends.neo4j import (
            Neo4jGraphBackend,
        )

        return Neo4jGraphBackend(config)
    from agent.tools.business_data.providers.graph.backends.hugegraph import (
        HugeGraphBackend,
    )

    return HugeGraphBackend(config)


def _json_result(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return value


class DeterministicProviderBroker:
    """Serve one binding over a private Unix socket mounted into one sandbox."""

    def __init__(
        self,
        *,
        binding: DeterministicProviderBinding,
        socket_dir: Path,
        backend_factory: Callable[[DeterministicProviderBinding], Any] = _build_backend,
    ) -> None:
        directory = Path(socket_dir)
        if directory.is_symlink() or not directory.is_dir():
            raise DeterministicProviderBrokerError(
                "provider broker directory must be a regular directory"
            )
        if not hasattr(socket, "AF_UNIX"):
            raise DeterministicProviderBrokerError(
                "provider broker requires Unix domain sockets"
            )
        self.binding = binding
        self.socket_dir = directory.resolve()
        self.socket_path = self.socket_dir / _SOCKET_NAME
        self._backend_factory = backend_factory
        self._backend: Any = None
        self._scope: Any = None
        self._max_result_rows = min(
            1000,
            max(1, int(binding.effective_connection.options.get("max_result_rows") or 100)),
        )
        self._stop = threading.Event()
        self._server: socket.socket | None = None
        self._active_connection: socket.socket | None = None
        self._connection_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> "DeterministicProviderBroker":
        if self._server is not None:
            raise DeterministicProviderBrokerError("provider broker is already running")
        server: socket.socket | None = None
        try:
            self.socket_path.unlink(missing_ok=True)
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(self.socket_path))
            if os.name == "posix":
                os.chmod(self.socket_path, 0o666)
            server.listen(4)
            server.settimeout(0.1)
        except OSError as exc:
            if server is not None:
                server.close()
            self.socket_path.unlink(missing_ok=True)
            raise DeterministicProviderBrokerError(
                "provider broker socket could not be created"
            ) from exc
        if server is None:
            raise DeterministicProviderBrokerError(
                "provider broker socket could not be created"
            )
        self._server = server
        self._thread = threading.Thread(
            target=self._serve,
            name="deterministic-provider-broker",
            daemon=True,
        )
        self._thread.start()
        return self

    def close(self) -> None:
        failures: list[Exception] = []
        self._stop.set()
        server = self._server
        self._server = None
        if server is not None:
            server.close()
        with self._connection_lock:
            active_connection = self._active_connection
        if active_connection is not None:
            active_connection.close()
        close_backend = getattr(self._backend, "close", None)
        if callable(close_backend):
            try:
                close_backend()
            except Exception as exc:
                failures.append(exc)
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=_MAX_QUERY_TIMEOUT_SECONDS + 2.0)
            if thread.is_alive():
                failures.append(
                    DeterministicProviderBrokerError(
                        "provider broker did not stop within its query deadline"
                    )
                )
        try:
            self.socket_path.unlink(missing_ok=True)
        except OSError as exc:
            failures.append(exc)
        if failures:
            raise DeterministicProviderBrokerError(
                "provider broker could not be closed safely"
            ) from failures[0]

    def __enter__(self) -> "DeterministicProviderBroker":
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        self.close()

    def _serve(self) -> None:
        while not self._stop.is_set():
            server = self._server
            if server is None:
                return
            try:
                connection, _address = server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            connection.settimeout(1.0)
            with self._connection_lock:
                self._active_connection = connection
            try:
                with connection:
                    self._serve_connection(connection)
            finally:
                with self._connection_lock:
                    if self._active_connection is connection:
                        self._active_connection = None

    def _serve_connection(self, connection: socket.socket) -> None:
        response: dict[str, Any]
        try:
            request_size = struct.unpack("!I", _read_exact(connection, 4))[0]
            if request_size <= 0 or request_size > _MAX_REQUEST_BYTES:
                raise ValueError("request size is invalid")
            request = json.loads(_read_exact(connection, request_size).decode("utf-8"))
            result = self._dispatch(request)
            response = {"version": 1, "ok": True, "result": result}
        except GraphBackendTimeoutError:
            response = {
                "version": 1,
                "ok": False,
                "code": "provider_timeout",
                "message": "Deterministic provider query timed out.",
            }
        except GraphBackendError:
            response = {
                "version": 1,
                "ok": False,
                "code": "provider_query_failed",
                "message": "Deterministic provider query failed.",
            }
        except (
            DeterministicProviderBrokerError,
            OSError,
            TypeError,
            ValueError,
        ):
            response = {
                "version": 1,
                "ok": False,
                "code": "provider_arguments_invalid",
                "message": "Deterministic provider request is invalid.",
            }
        except Exception:
            response = {
                "version": 1,
                "ok": False,
                "code": "provider_query_failed",
                "message": "Deterministic provider query failed.",
            }
        try:
            payload = json.dumps(
                response,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, OverflowError):
            payload = json.dumps(
                {
                    "version": 1,
                    "ok": False,
                    "code": "provider_query_failed",
                    "message": "Deterministic provider query failed.",
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        if len(payload) > _MAX_RESPONSE_BYTES:
            payload = json.dumps(
                {
                    "version": 1,
                    "ok": False,
                    "code": "provider_response_too_large",
                    "message": "Deterministic provider response exceeds the limit.",
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        try:
            connection.sendall(struct.pack("!I", len(payload)) + payload)
        except OSError:
            return

    def _dispatch(self, request: Any) -> Any:
        if not isinstance(request, Mapping) or request.get("version") != 1:
            raise ValueError("request contract is invalid")
        operation = str(request.get("operation") or "").strip()
        allowed_arguments = _OPERATION_ARGUMENTS.get(operation)
        arguments = request.get("arguments")
        if allowed_arguments is None or not isinstance(arguments, Mapping):
            raise ValueError("operation is invalid")
        normalized_arguments = dict(arguments)
        if set(normalized_arguments) - set(allowed_arguments):
            raise ValueError("operation arguments are invalid")
        if "limit" in normalized_arguments:
            normalized_arguments["limit"] = min(
                self._max_result_rows,
                max(1, int(normalized_arguments["limit"])),
            )
        if "offset" in normalized_arguments:
            normalized_arguments["offset"] = max(
                0,
                int(normalized_arguments["offset"]),
            )
        if self._backend is None:
            config = _provider_config(self.binding)
            self._scope = build_graph_scope(config)
            self._backend = self._backend_factory(self.binding)
        method = getattr(self._backend, operation, None)
        if not callable(method):
            raise ValueError("operation is unavailable")
        external_filter = dict(normalized_arguments.get("filter_dict") or {})
        normalized_arguments = _normalize_graph_operation_arguments(
            operation=operation,
            arguments=normalized_arguments,
            backend=self._backend,
            scope=self._scope,
        )
        result = method(**normalized_arguments, scope=self._scope)
        if operation == "aggregate_search":
            result = _with_external_filter_result(result, external_filter)
        return _json_result(result)


def _normalize_graph_operation_arguments(
    *,
    operation: str,
    arguments: dict[str, Any],
    backend: Any,
    scope: Any,
) -> dict[str, Any]:
    """Apply the shared physical-type boundary to broker-direct calls."""

    result = dict(arguments)
    resolver = GraphFilterTypeResolver(
        backend=backend,
        scope=scope,
        provider=getattr(backend, "provider", ""),
    )
    if operation in {"property_filter", "count_search", "aggregate_search", "sorted_search"}:
        result["filter_dict"] = resolver.normalize_filter(
            result.get("filter_dict") or {},
            element_type=str(result.get("element_type") or ""),
            element_class=str(result.get("element_class") or ""),
        ).filter_dict
    elif operation == "pattern_search":
        path_pattern = result.get("path_pattern") or []
        resolver.prepare_pattern_targets(
            path_pattern,
            return_properties=result.get("return_properties"),
        )
        result["path_pattern"] = normalize_pattern_filters(
            path_pattern,
            resolver,
            return_properties=result.get("return_properties"),
        )
    elif operation == "shortest_path_search":
        result["start_props"] = resolver.normalize_properties(
            result.get("start_props") or {},
            element_type="NODE",
            element_class=str(result.get("start_label") or ""),
        )
        result["end_props"] = resolver.normalize_properties(
            result.get("end_props") or {},
            element_type="NODE",
            element_class=str(result.get("end_label") or ""),
        )
    return result


def _with_external_filter_result(result: Any, external_filter: dict[str, Any]) -> Any:
    """Keep broker output JSON-compatible and caller-facing filters unchanged."""

    if not isinstance(result, GraphBackendResult):
        return result
    records = [_with_external_filter(record, external_filter) for record in result.records or []]
    payload = dict(result.payload or {})
    payload_result = payload.get("result")
    if isinstance(payload_result, Mapping):
        payload["result"] = _with_external_filter(payload_result, external_filter)
    return GraphBackendResult(
        records=records,
        total=result.total,
        truncated=result.truncated,
        warnings=list(result.warnings or []),
        payload=payload,
    )


def _with_external_filter(
    record: Mapping[str, Any],
    external_filter: dict[str, Any],
) -> dict[str, Any]:
    copied = dict(record)
    if "applied_filter" in copied:
        copied["applied_filter"] = dict(external_filter)
    if "filter_keys" in copied:
        copied["filter_keys"] = list(external_filter)
    return copied
