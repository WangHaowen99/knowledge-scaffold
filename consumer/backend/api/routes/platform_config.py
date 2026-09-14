# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""onto-platform connector configuration routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from integration.platform.config import (
    PlatformConfigValidationError,
    get_public_platform_config,
    save_platform_config,
    test_platform_config_connection,
)
from api.auth.auth import require_admin_user, require_authenticated_user
from api.dependencies import run_in_runtime_pool
from api.request_parsing import read_optional_json_object
from utils.logging import get_logger

logger = get_logger("routes.platform_config")

platform_config_router = APIRouter(prefix="/api")


@platform_config_router.get(
    "/platform-config",
    dependencies=[Depends(require_authenticated_user)],
)
async def get_platform_config_api(request: Request):
    return await run_in_runtime_pool(
        request,
        "short_io",
        get_public_platform_config,
    )


def _save_and_refresh_platform_config(request: Request, payload: dict):
    updated_config = save_platform_config(payload)
    services = getattr(request.app.state, "services", {})
    refresh_platform_connector = services.get("REFRESH_PLATFORM_CONNECTOR")
    if callable(refresh_platform_connector):
        refresh_platform_connector()
    return updated_config


@platform_config_router.patch(
    "/platform-config",
    dependencies=[Depends(require_admin_user)],
)
async def update_platform_config_api(request: Request):
    payload = await read_optional_json_object(request)
    try:
        updated_config = await run_in_runtime_pool(
            request,
            "short_io",
            _save_and_refresh_platform_config,
            request,
            payload,
        )
    except PlatformConfigValidationError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    except Exception:
        logger.exception("Failed to refresh onto-platform connector after config update")
        return JSONResponse(
            {"detail": "配置已保存，但 Platform 接入刷新失败"},
            status_code=500,
        )
    return {"config": updated_config}


@platform_config_router.post(
    "/platform-config/test",
    dependencies=[Depends(require_admin_user)],
)
async def test_platform_config_api(request: Request):
    payload = await read_optional_json_object(request)
    result = await run_in_runtime_pool(
        request,
        "external_io",
        test_platform_config_connection,
        payload,
    )
    return {
        "success": bool(result.get("success")),
        "message": str(result.get("message", "")).strip(),
        "tested_at": result.get("tested_at"),
    }
