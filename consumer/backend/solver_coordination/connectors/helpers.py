# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Protocol-neutral helpers shared by Consumer solver connectors."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import hmac
import re
from typing import Any

from solver_coordination.canonical import canonical_json_bytes


_TRACEPARENT_RE = re.compile(
    r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$"
)


def validate_traceparent(value: Any) -> str:
    """Validate the canonical W3C version-00 trace context shape."""

    if not isinstance(value, str) or _TRACEPARENT_RE.fullmatch(value) is None:
        raise ValueError("traceparent is invalid")
    parts = value.split("-")
    if parts[1] == "0" * 32 or parts[2] == "0" * 16:
        raise ValueError("traceparent is invalid")
    return value


def idempotency_key(
    *,
    secret: bytes,
    binding_revision_id: str,
    run_id: str,
    attempt_id: str,
    problem_digest: str,
) -> str:
    """Build an opaque key for one platform solve attempt."""

    if not isinstance(secret, bytes) or not secret:
        raise ValueError("secret must be non-empty bytes")
    values = (binding_revision_id, run_id, attempt_id, problem_digest)
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("idempotency inputs are invalid")
    message = "\0".join(values).encode("utf-8")
    return "hmac-sha256:" + hmac.new(secret, message, hashlib.sha256).hexdigest()


def capability_fingerprint(payload: Mapping[str, Any]) -> str:
    """Digest a JSON-shaped capability descriptor without a wire DTO."""

    if not isinstance(payload, Mapping):
        raise ValueError("capabilities must be a mapping")
    return "sha256:" + hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


__all__ = ["capability_fingerprint", "idempotency_key", "validate_traceparent"]
