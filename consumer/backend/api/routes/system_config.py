"""System configuration routes."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from config.system_config import (
    SystemConfigReadError,
    SystemConfigValidationError,
    get_public_system_config,
    get_system_config_readiness,
    get_system_config_template,
    import_system_config,
    test_system_config_connection,
    update_system_config_section,
)
from api.auth.auth import require_admin_user, require_authenticated_user
from api.dependencies import run_in_runtime_pool
from api.request_parsing import read_optional_json_object
from utils.logging import get_logger

logger = get_logger("routes.system_config")

system_config_router = APIRouter(prefix="/api")


def _system_config_read_error_response(exc: SystemConfigReadError):
    return JSONResponse(
        {"code": exc.code, "detail": str(exc)},
        status_code=409,
    )


@system_config_router.get(
    "/system-config",
    dependencies=[Depends(require_authenticated_user)],
)
async def get_system_config_api(request: Request):
    try:
        return await run_in_runtime_pool(
            request,
            "short_io",
            get_public_system_config,
        )
    except SystemConfigReadError as exc:
        return _system_config_read_error_response(exc)


@system_config_router.get(
    "/system-config/readiness",
    dependencies=[Depends(require_authenticated_user)],
)
async def get_system_config_readiness_api(request: Request):
    try:
        return await run_in_runtime_pool(
            request,
            "short_io",
            get_system_config_readiness,
        )
    except SystemConfigReadError as exc:
        return _system_config_read_error_response(exc)


@system_config_router.get(
    "/system-config/template",
    dependencies=[Depends(require_admin_user)],
)
async def get_system_config_template_api(request: Request):
    content = await run_in_runtime_pool(
        request,
        "short_io",
        get_system_config_template,
    )
    return Response(
        content=json.dumps(content, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="system_config.template.json"',
        },
    )


def _refresh_runtime_services(request: Request) -> None:
    services = getattr(request.app.state, "services", {})
    refresh_runtime = services.get("REFRESH_RUNTIME_SERVICES")
    if callable(refresh_runtime):
        refresh_runtime(request.app)


def _update_and_refresh_system_config(request: Request, section, data):
    updated_section = update_system_config_section(section, data)
    _refresh_runtime_services(request)
    return updated_section


@system_config_router.patch(
    "/system-config",
    dependencies=[Depends(require_admin_user)],
)
async def update_system_config_api(request: Request):
    payload = await read_optional_json_object(request)
    try:
        section = payload.get("section")
        data = payload.get("data", {})
        updated_section = await run_in_runtime_pool(
            request,
            "short_io",
            _update_and_refresh_system_config,
            request,
            section,
            data,
        )
    except SystemConfigReadError as exc:
        return _system_config_read_error_response(exc)
    except SystemConfigValidationError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    except Exception:
        logger.exception("Failed to refresh runtime services after config update")
        return JSONResponse(
            {"detail": "配置已保存，但运行时刷新失败"},
            status_code=500,
        )

    return {"section": section, "config": updated_section}


def _import_and_refresh_system_config(request: Request, config):
    imported_sections, imported_config = import_system_config(config)
    _refresh_runtime_services(request)
    return imported_sections, imported_config


@system_config_router.post(
    "/system-config/import",
    dependencies=[Depends(require_admin_user)],
)
async def import_system_config_api(request: Request):
    payload = await read_optional_json_object(request)
    try:
        imported_sections, config = await run_in_runtime_pool(
            request,
            "short_io",
            _import_and_refresh_system_config,
            request,
            payload.get("config", {}),
        )
    except SystemConfigReadError as exc:
        return _system_config_read_error_response(exc)
    except SystemConfigValidationError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    except Exception:
        logger.exception("Failed to refresh runtime services after config import")
        return JSONResponse(
            {"detail": "配置已导入，但运行时刷新失败"},
            status_code=500,
        )

    return {
        "success": True,
        "message": "配置导入成功",
        "imported_sections": imported_sections,
        "config": config,
    }


@system_config_router.post(
    "/system-config/test",
    dependencies=[Depends(require_admin_user)],
)
async def test_system_config_api(request: Request):
    payload = await read_optional_json_object(request)
    try:
        result = await run_in_runtime_pool(
            request,
            "external_io",
            test_system_config_connection,
            payload.get("section"),
            payload.get("data", {}),
        )
    except SystemConfigReadError as exc:
        return _system_config_read_error_response(exc)
    except SystemConfigValidationError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return {
        "section": payload.get("section"),
        "success": bool(result.get("success")),
        "message": str(result.get("message", "")).strip(),
        "tested_at": result.get("tested_at"),
    }
