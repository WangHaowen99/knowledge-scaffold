# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Process-local synthetic credentials for OR-Tools service tests."""

from __future__ import annotations

import secrets


SERVICE_TOKEN = secrets.token_urlsafe(32)
INVALID_TOKEN = secrets.token_urlsafe(32)
