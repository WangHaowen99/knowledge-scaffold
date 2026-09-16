# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Small response helpers for MCP adapters."""

from __future__ import annotations

from typing import Any


def ok_result(result: Any, *, scope: dict | None = None, upstream: dict | None = None, message: str = "查询成功") -> dict:
    payload = {
        "ok": True,
        "result": result,
        "message": {"description": message},
    }
    if scope is not None:
        payload["scope"] = scope
    if upstream is not None:
        payload["upstream"] = upstream
    return payload


def error_result(
    *,
    error_type: str,
    error_code: str,
    detail: str,
    hint: str = "",
    retriable: bool = True,
    phase: str = "",
) -> dict:
    error = {
        "type": error_type,
        "error_code": error_code,
        "detail": detail,
        "retriable": retriable,
    }
    if hint:
        error["hint"] = hint
    if phase:
        error["phase"] = phase
    return {"ok": False, "error": error}
