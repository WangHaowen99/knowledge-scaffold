# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""User memory management endpoints.

Exposes the current user's ordinary long-term memories (preferences and
profile) to the configuration-center UI so a user can view, add, edit, and
remove their own durable personalization memory. Routing, attribution, and
storage policy stay in backend code; the model never touches storage paths.
"""

from __future__ import annotations

import hashlib
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from agent.memory.models import MEMORY_TYPE_TO_DIR
from agent.tools.memory import store_user_memory
from api.auth.auth import AuthUser, require_authenticated_user
from api.dependencies import require_workspace_id, run_in_runtime_pool
from api.request_parsing import read_json_object
from utils.logging import get_logger

logger = get_logger("routes.memories")

memories_router = APIRouter(prefix="/agentic")


def _get_orchestrator():
    from api.routes import get_orchestrator
    return get_orchestrator()


def _get_memory_manager(user: AuthUser):
    return _get_orchestrator().get_memory_manager(user.username)


def _parse_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_limit(value: Optional[str]) -> int:
    try:
        parsed = int(value or 100)
    except (TypeError, ValueError):
        parsed = 100
    return max(1, min(parsed, 200))


def _parse_memory_type(value: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    memory_type = str(value or "").strip().lower()
    if not memory_type or memory_type == "all":
        return None, None
    if memory_type not in MEMORY_TYPE_TO_DIR:
        return None, "Unsupported memory_type '{}'. Expected one of: {}.".format(
            memory_type,
            ", ".join(sorted(MEMORY_TYPE_TO_DIR)),
        )
    return memory_type, None


def _memory_id(source_uri: str) -> str:
    return hashlib.sha256(str(source_uri or "").encode("utf-8")).hexdigest()[:32]


def _memory_payload(hit) -> dict[str, Any]:
    payload = hit.to_dict()
    payload["id"] = _memory_id(hit.source_uri)
    payload.pop("source_uri", None)
    return payload


def _store_result_payload(result: dict[str, Any]) -> dict[str, Any]:
    payload = dict(result)
    source_uri = str(payload.get("source_uri") or "")
    if source_uri:
        payload["id"] = _memory_id(source_uri)
    payload.pop("source_uri", None)
    return payload


def _list_memory_hits(
    memory_manager,
    *,
    memory_type: Optional[str] = None,
    include_archived: bool = False,
    limit: int = 100,
    query: str = "",
):
    if query:
        return memory_manager.recall_memory(
            query=query,
            memory_type=memory_type,
            scope="user",
            k=limit,
            include_archived=include_archived,
        )
    if hasattr(memory_manager, "list_memories"):
        return memory_manager.list_memories(
            memory_type=memory_type,
            include_archived=include_archived,
            limit=limit,
        )
    return memory_manager.recall_memory(
        query="用户记忆 用户偏好 用户画像 preference profile",
        memory_type=memory_type,
        scope="user",
        k=limit,
        include_archived=include_archived,
    )


def _find_memory_hit(memory_manager, memory_id: str):
    target = str(memory_id or "").strip()
    if not target:
        return None
    hits = _list_memory_hits(
        memory_manager,
        include_archived=False,
        limit=1000,
    )
    for hit in hits:
        if _memory_id(hit.source_uri) == target:
            return hit
    return None


@memories_router.get("/memories")
async def list_memories(
    request: Request,
    user: AuthUser = Depends(require_authenticated_user),
    _workspace_id: str = Depends(require_workspace_id),
):
    """List the current user's ordinary long-term memories."""
    memory_type, error = _parse_memory_type(request.query_params.get("memory_type"))
    if error:
        return JSONResponse({"error": error}, status_code=400)

    memory_manager = _get_memory_manager(user)
    if not memory_manager:
        return {
            "memory_enabled": False,
            "memories": [],
            "count": 0,
        }

    limit = _parse_limit(request.query_params.get("limit"))
    include_archived = _parse_bool(request.query_params.get("include_archived"))
    query = str(request.query_params.get("query") or "").strip()
    try:
        hits = await run_in_runtime_pool(
            request,
            "short_io",
            _list_memory_hits,
            memory_manager,
            memory_type=memory_type,
            include_archived=include_archived,
            limit=limit,
            query=query,
        )
    except Exception:
        logger.warning("Failed to list user memories", exc_info=True)
        return JSONResponse(
            {"error": "Failed to list memories"},
            status_code=500,
        )

    return {
        "memory_enabled": True,
        "memories": [_memory_payload(hit) for hit in hits],
        "count": len(hits),
    }


@memories_router.post("/memories")
async def create_memory(
    request: Request,
    user: AuthUser = Depends(require_authenticated_user),
    _workspace_id: str = Depends(require_workspace_id),
):
    """Create a current-user ordinary memory item."""
    data = await read_json_object(request)
    content = str(data.get("content") or "").strip()
    if len(content) < 8:
        return JSONResponse(
            {"error": "记忆内容太短，请补充具体上下文。"},
            status_code=400,
        )

    memory_manager = _get_memory_manager(user)
    if not memory_manager:
        return JSONResponse(
            {"error": "Memory manager not available"},
            status_code=503,
        )

    session_id = str(data.get("session_id") or "").strip() or None
    try:
        result = await run_in_runtime_pool(
            request,
            "short_io",
            store_user_memory,
            content,
            memory_manager=memory_manager,
            session_id=session_id,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception:
        logger.warning("Failed to create user memory", exc_info=True)
        return JSONResponse(
            {"error": "Failed to create memory"},
            status_code=500,
        )

    status = 200 if result.get("merged") or result.get("relation") == "duplicate" else 201
    return JSONResponse(
        {"memory": _store_result_payload(result)},
        status_code=status,
    )


def _replace_memory(memory_manager, memory_id: str, content: str):
    hit = _find_memory_hit(memory_manager, memory_id)
    if not hit:
        return None
    archived = memory_manager.archive_memory(
        source_uri=hit.source_uri,
        reason="edited by user",
    )
    if not archived:
        return None
    return memory_manager.store_memory(
        content=content,
        memory_type=hit.memory_type or "preference",
        scope="user",
        source="manual_edit",
    )


@memories_router.put("/memories/{memory_id}")
async def update_memory(
    memory_id: str,
    request: Request,
    user: AuthUser = Depends(require_authenticated_user),
    _workspace_id: str = Depends(require_workspace_id),
):
    """Update an active ordinary memory item."""
    data = await read_json_object(request)
    content = str(data.get("content") or "").strip()
    if len(content) < 8:
        return JSONResponse(
            {"error": "记忆内容太短，请补充具体上下文。"},
            status_code=400,
        )

    memory_manager = _get_memory_manager(user)
    if not memory_manager:
        return JSONResponse(
            {"error": "Memory manager not available"},
            status_code=503,
        )

    try:
        result = await run_in_runtime_pool(
            request,
            "short_io",
            _replace_memory,
            memory_manager,
            memory_id,
            content,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception:
        logger.warning("Failed to update user memory", exc_info=True)
        return JSONResponse(
            {"error": "Failed to update memory"},
            status_code=500,
        )
    if result is None:
        return JSONResponse({"error": "Memory not found"}, status_code=404)

    return {"memory": _store_result_payload(result.to_dict())}


@memories_router.delete("/memories/{memory_id}")
async def delete_memory(
    memory_id: str,
    request: Request,
    user: AuthUser = Depends(require_authenticated_user),
    _workspace_id: str = Depends(require_workspace_id),
):
    """Archive an active ordinary memory item."""
    memory_manager = _get_memory_manager(user)
    if not memory_manager:
        return JSONResponse(
            {"error": "Memory manager not available"},
            status_code=503,
        )

    try:
        hit = await run_in_runtime_pool(
            request,
            "short_io",
            _find_memory_hit,
            memory_manager,
            memory_id,
        )
    except Exception:
        logger.warning("Failed to resolve user memory for delete", exc_info=True)
        return JSONResponse(
            {"error": "Failed to delete memory"},
            status_code=500,
        )
    if not hit:
        return JSONResponse({"error": "Memory not found"}, status_code=404)

    try:
        archived = await run_in_runtime_pool(
            request,
            "short_io",
            memory_manager.archive_memory,
            source_uri=hit.source_uri,
            reason="deleted by user",
        )
    except Exception:
        logger.warning("Failed to delete user memory", exc_info=True)
        return JSONResponse(
            {"error": "Failed to delete memory"},
            status_code=500,
        )
    if not archived:
        return JSONResponse({"error": "Memory not found"}, status_code=404)

    return {"success": True, "id": memory_id}
