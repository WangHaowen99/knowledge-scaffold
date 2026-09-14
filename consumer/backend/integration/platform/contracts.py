# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from __future__ import annotations

from typing import Any


PLATFORM_SUCCESS_STATUSES = {"completed", "success", "succeeded"}


def normalize_platform_status(value: Any, *, default: str = "success") -> str:
    text = str(value or default).strip().lower()
    if not text:
        return default
    if text in PLATFORM_SUCCESS_STATUSES:
        return "success"
    return text
