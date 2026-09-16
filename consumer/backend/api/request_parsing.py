# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Small request parsers for endpoints that must authenticate before body reads."""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException, Request


async def read_optional_json_object(request: Request) -> dict[str, Any]:
    """Return an object body, or an empty object for an absent/malformed body."""
    content_type = str(request.headers.get("content-type") or "").lower()
    if not (
        content_type.startswith("application/json")
        or "+json" in content_type
    ):
        return {}
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


async def read_json_object(request: Request) -> dict[str, Any]:
    """Require an application/json object after route dependencies have run."""
    content_type = str(request.headers.get("content-type") or "").lower()
    if not (
        content_type.startswith("application/json")
        or "+json" in content_type
    ):
        raise HTTPException(
            status_code=415,
            detail="Content-Type 必须是 application/json",
        )
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="JSON body 必须是有效对象") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON body 必须是对象")
    return payload
