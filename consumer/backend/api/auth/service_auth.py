# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Authentication helpers for narrowly scoped service-to-service APIs."""

from __future__ import annotations

import os
import secrets

from fastapi import Request


def modeling_service_authorized(request: Request) -> bool:
    expected = str(os.getenv("MODELING_SERVICE_TOKEN") or "").strip()
    if len(expected) < 32:
        return False
    header = str(request.headers.get("Authorization") or "")
    supplied = header[7:].strip() if header.startswith("Bearer ") else ""
    return bool(supplied) and secrets.compare_digest(supplied, expected)


def evaluation_service_authorized(request: Request) -> bool:
    """The evaluation credential is additional to, never a replacement for, user auth."""
    expected = str(os.getenv("EVALUATION_SERVICE_TOKEN") or "").strip()
    modeling = str(os.getenv("MODELING_SERVICE_TOKEN") or "").strip()
    supplied = str(request.headers.get("X-Evaluation-Service-Token") or "")
    return (len(expected) >= 32 and expected != modeling and bool(supplied)
            and secrets.compare_digest(supplied.encode(), expected.encode()))
