# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Process-local synthetic credentials for solver TCK tests."""

from __future__ import annotations

import secrets


CONTROL_TOKEN = secrets.token_urlsafe(32)
SERVICE_TOKEN = secrets.token_urlsafe(32)
