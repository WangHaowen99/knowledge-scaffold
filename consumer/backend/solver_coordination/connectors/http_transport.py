"""Bounded, authenticated HTTP transport for solver services."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
import logging
import math
import time
from typing import Any

import requests

from .helpers import validate_traceparent
from .endpoint_policy import EndpointPolicy, EndpointPolicyError
from .errors import (
    ConnectorAuthenticationError,
    ConnectorCapacityError,
    ConnectorContractError,
    ConnectorExecutionError,
    ConnectorResultError,
    ConnectorTransportError,
)


CredentialResolver = Callable[[str], Mapping[str, str] | str | None]
logger = logging.getLogger(__name__)


class HttpSolverTransport:
    """Perform safe JSON requests without knowing a solver-service DTO."""

    def __init__(
        self,
        *,
        session: requests.Session | Any | None = None,
        credential_resolver: CredentialResolver | None = None,
        max_retries: int = 2,
        backoff_seconds: float = 0.0,
        sleep: Callable[[float], None] | None = None,
        timeout_seconds: float = 30.0,
        max_retry_after_seconds: float = 60.0,
        max_response_bytes: int = 4 * 1024 * 1024,
        tls_ca_bundle: str | None = None,
        endpoint_policy: EndpointPolicy | None = None,
    ) -> None:
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or not 0 <= max_retries <= 5:
            raise ValueError("max_retries must be between 0 and 5")
        for value, name, allow_zero in (
            (timeout_seconds, "timeout_seconds", False),
            (backoff_seconds, "backoff_seconds", True),
            (max_retry_after_seconds, "max_retry_after_seconds", False),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                qualifier = "non-negative" if allow_zero else "positive"
                raise ValueError(f"{name} must be a finite {qualifier} number")
            if not math.isfinite(float(value)):
                qualifier = "non-negative" if allow_zero else "positive"
                raise ValueError(f"{name} must be a finite {qualifier} number")
            if value < 0 if allow_zero else value <= 0:
                qualifier = "non-negative" if allow_zero else "positive"
                raise ValueError(f"{name} must be a finite {qualifier} number")
        if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int) or max_response_bytes < 1:
            raise ValueError("max_response_bytes must be a positive integer")
        if tls_ca_bundle is not None:
            if not isinstance(tls_ca_bundle, str) or not tls_ca_bundle:
                raise ValueError("tls_ca_bundle must be a clean path")
            if tls_ca_bundle != tls_ca_bundle.strip() or any(
                ord(char) < 0x20 or ord(char) == 0x7F for char in tls_ca_bundle
            ):
                raise ValueError("tls_ca_bundle must be a clean path")
        self.session = session or requests.Session()
        self.credential_resolver = credential_resolver or (lambda _ref: None)
        self.max_retries = max_retries
        self.backoff_seconds = float(backoff_seconds)
        self.sleep = sleep or time.sleep
        self.timeout_seconds = float(timeout_seconds)
        self.max_retry_after_seconds = float(max_retry_after_seconds)
        self.max_response_bytes = max_response_bytes
        self.tls_ca_bundle = tls_ca_bundle
        self.endpoint_policy = endpoint_policy or EndpointPolicy()

    @staticmethod
    def _binding_value(binding: Any, name: str, *aliases: str) -> Any:
        if isinstance(binding, Mapping):
            for candidate in (name, *aliases):
                if candidate in binding:
                    return binding[candidate]
        else:
            for candidate in (name, *aliases):
                value = getattr(binding, candidate, None)
                if value is not None:
                    return value
        return None

    def _endpoint(self, binding: Any, path: str) -> str:
        if not isinstance(path, str) or not path.startswith("/api/v1/"):
            raise ConnectorContractError("connector request path is invalid")
        if "?" in path or "#" in path or "\\" in path:
            raise ConnectorContractError("connector request path is invalid")
        if ".." in path.split("/"):
            raise ConnectorContractError("connector request path is invalid")
        endpoint = self._binding_value(binding, "endpoint")
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ConnectorContractError("binding endpoint is missing")
        try:
            return self.endpoint_policy.validate(endpoint) + path
        except EndpointPolicyError as exc:
            raise ConnectorContractError(str(exc)) from exc

    def _headers(
        self,
        binding: Any,
        *,
        idempotency_key: str | None,
        traceparent: str | None,
    ) -> dict[str, str]:
        reference = self._binding_value(
            binding, "service_credential_ref", "service_credential_reference"
        )
        if not isinstance(reference, str) or not reference.strip():
            raise ConnectorContractError("service credential reference is missing")
        try:
            resolved = self.credential_resolver(reference)
        except Exception as exc:
            raise ConnectorAuthenticationError("service credential resolution failed") from exc
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if isinstance(resolved, str):
            if not resolved:
                raise ConnectorAuthenticationError("service credential is empty")
            headers["Authorization"] = "Bearer " + resolved
        elif isinstance(resolved, Mapping):
            reserved = {
                "accept", "content-type", "content-length", "host",
                "idempotency-key", "prefer", "traceparent",
            }
            for key, value in resolved.items():
                if not isinstance(key, str) or not key:
                    raise ConnectorAuthenticationError("service credential headers are invalid")
                if not isinstance(value, str) or not value:
                    raise ConnectorAuthenticationError("service credential headers are invalid")
                if key.casefold() in reserved:
                    raise ConnectorAuthenticationError("service credential headers are invalid")
                headers[key] = value
        elif resolved is None:
            raise ConnectorAuthenticationError("service credential is unavailable")
        else:
            raise ConnectorAuthenticationError("service credential is invalid")
        if idempotency_key is not None:
            if not isinstance(idempotency_key, str) or not idempotency_key.startswith("hmac-sha256:"):
                raise ConnectorContractError("idempotency key is invalid")
            headers["Idempotency-Key"] = idempotency_key
        if traceparent is not None:
            try:
                headers["traceparent"] = validate_traceparent(traceparent)
            except ValueError as exc:
                raise ConnectorContractError("traceparent is invalid") from exc
        return headers

    @staticmethod
    def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON object key")
            value[key] = item
        return value

    def _payload(self, response: Any) -> Any:
        headers = getattr(response, "headers", {}) or {}
        try:
            declared = int(headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            declared = 0
        if declared > self.max_response_bytes:
            self._close(response)
            raise ConnectorContractError("connector response exceeds size limit")
        try:
            iter_content = getattr(response, "iter_content", None)
            if callable(iter_content):
                body = bytearray()
                for chunk in iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    if not isinstance(chunk, (bytes, bytearray)):
                        raise ConnectorContractError(
                            "connector response is not valid JSON"
                        )
                    body.extend(chunk)
                    if len(body) > self.max_response_bytes:
                        raise ConnectorContractError(
                            "connector response exceeds size limit"
                        )
                raw = bytes(body)
            else:
                content = getattr(response, "content", None)
                if isinstance(content, (bytes, bytearray)):
                    raw = bytes(content)
                else:
                    try:
                        raw = json.dumps(
                            response.json(), allow_nan=False
                        ).encode("utf-8")
                    except Exception as exc:
                        raise ConnectorContractError(
                            "connector response is not valid JSON"
                        ) from exc
            if len(raw) > self.max_response_bytes:
                raise ConnectorContractError(
                    "connector response exceeds size limit"
                )
        finally:
            self._close(response)
        try:
            return json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=self._reject_duplicate_keys,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
        except Exception as exc:
            raise ConnectorContractError("connector response is not valid JSON") from exc

    @staticmethod
    def _close(response: Any) -> None:
        close = getattr(response, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                logger.debug("Solver Connector response close failed")

    def _retry_after(self, response: Any) -> float | None:
        value = (getattr(response, "headers", {}) or {}).get("Retry-After")
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(parsed) or parsed < 0:
            return None
        return min(parsed, self.max_retry_after_seconds)

    @staticmethod
    def _validated_error(payload: Any) -> Mapping[str, Any] | None:
        if not isinstance(payload, Mapping) or set(payload) != {"error"}:
            return None
        error = payload["error"]
        if not isinstance(error, Mapping) or set(error) != {
            "code", "category", "message", "retryable", "diagnostic_ref"
        }:
            return None
        if (
            error.get("category") not in {
                "validation", "authentication", "capacity", "execution", "result", "internal"
            }
            or not isinstance(error.get("retryable"), bool)
            or not isinstance(error.get("diagnostic_ref"), str)
        ):
            return None
        return error

    @classmethod
    def _raise_http(cls, status: int, payload: Any) -> None:
        error = cls._validated_error(payload)
        if error is None:
            if status == 429:
                raise ConnectorCapacityError(status_code=status)
            if status == 503:
                raise ConnectorTransportError(status_code=status)
            raise ConnectorContractError("connector error response is invalid", status_code=status)
        kwargs = {
            "status_code": status,
            "retryable": bool(error["retryable"]),
            "diagnostic_ref": str(error["diagnostic_ref"]),
            "payload": None,
        }
        category = error["category"]
        if status in {401, 403} or category == "authentication":
            raise ConnectorAuthenticationError(**kwargs)
        if status == 429 or category == "capacity":
            raise ConnectorCapacityError(**kwargs)
        if category == "execution":
            raise ConnectorExecutionError(**kwargs)
        if category == "result":
            raise ConnectorResultError(**kwargs)
        if status == 503 or category == "internal":
            raise ConnectorTransportError(**kwargs)
        raise ConnectorContractError(**kwargs)

    def request(
        self,
        *,
        method: str,
        binding: Any,
        path: str,
        json_payload: Any = None,
        idempotency_key: str | None = None,
        traceparent: str | None = None,
        allow_retry: bool = True,
    ) -> Any:
        method = method.upper() if isinstance(method, str) else ""
        if method not in {"GET", "POST", "DELETE"}:
            raise ConnectorContractError("connector request method is invalid")
        attempts = self.max_retries if allow_retry else 0
        url = self._endpoint(binding, path)
        for attempt in range(attempts + 1):
            try:
                kwargs: dict[str, Any] = {
                    "json": json_payload,
                    "headers": self._headers(
                        binding,
                        idempotency_key=idempotency_key,
                        traceparent=traceparent,
                    ),
                    "timeout": self.timeout_seconds,
                    "allow_redirects": False,
                    "stream": True,
                }
                if self.tls_ca_bundle is not None:
                    kwargs["verify"] = self.tls_ca_bundle
                response = self.session.request(method=method, url=url, **kwargs)
            except requests.RequestException as exc:
                if attempt < attempts:
                    self.sleep(self.backoff_seconds * (2**attempt))
                    continue
                raise ConnectorTransportError() from exc
            status = int(getattr(response, "status_code", 0))
            if status in {429, 503} and attempt < attempts:
                delay = self._retry_after(response)
                self._close(response)
                self.sleep(delay if delay is not None else self.backoff_seconds * (2**attempt))
                continue
            if status in {301, 302, 303, 307, 308}:
                self._close(response)
                raise ConnectorTransportError("connector redirect is not allowed", status_code=status)
            payload = self._payload(response)
            if 200 <= status < 300:
                return payload
            self._raise_http(status, payload)
        raise ConnectorTransportError()


__all__ = ["CredentialResolver", "HttpSolverTransport"]
