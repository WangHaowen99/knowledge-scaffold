# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Shared HTTP error responses for agentic routes."""

from __future__ import annotations

from fastapi.responses import JSONResponse

from workspace.workspaces import WorkspaceNotFoundError


def not_found_response(exc: KeyError):
    if isinstance(exc, WorkspaceNotFoundError):
        return JSONResponse(exc.to_dict(), status_code=404)
    return JSONResponse({"detail": str(exc)}, status_code=404)


def workspace_not_found_response(exc: WorkspaceNotFoundError):
    return JSONResponse(exc.to_dict(), status_code=404)
