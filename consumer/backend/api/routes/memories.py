"""User memory management endpoints.

Exposes the current user's ordinary long-term memories (preferences and
profile) to the configuration-center UI so a user can view, add, edit, and
remove their own durable personalization memory. Routing, attribution, and
storage policy stay in backend code; the model never touches storage paths.
"""

from __future__ import annotations

import hashlib
from typing import Any, Optional

from flask import Blueprint, jsonify, request

from agent.memory.models import MEMORY_TYPE_TO_DIR
from api.auth.auth import get_current_user, require_auth
from agent.tools.memory import store_user_memory
from utils.logging import get_logger

logger = get_logger("routes.memories")

memories_bp = Blueprint("memories", __name__, url_prefix="/agentic")


def _get_orchestrator():
    from api.routes import get_orchestrator
    return get_orchestrator()


def _get_memory_manager():
    user = get_current_user()
    if not user:
        return None
    return _get_orchestrator()._get_memory_manager(user.username)


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


@memories_bp.route("/memories", methods=["GET"])
@require_auth()
def list_memories():
    """List the current user's ordinary long-term memories."""
    memory_type, error = _parse_memory_type(request.args.get("memory_type"))
    if error:
        return jsonify({"error": error}), 400

    memory_manager = _get_memory_manager()
    if not memory_manager:
        return jsonify({
            "memory_enabled": False,
            "memories": [],
            "count": 0,
        })

    limit = _parse_limit(request.args.get("limit"))
    include_archived = _parse_bool(request.args.get("include_archived"))
    query = str(request.args.get("query") or "").strip()
    try:
        hits = _list_memory_hits(
            memory_manager,
            memory_type=memory_type,
            include_archived=include_archived,
            limit=limit,
            query=query,
        )
    except Exception:
        logger.warning("Failed to list user memories", exc_info=True)
        return jsonify({"error": "Failed to list memories"}), 500

    return jsonify({
        "memory_enabled": True,
        "memories": [_memory_payload(hit) for hit in hits],
        "count": len(hits),
    })


@memories_bp.route("/memories", methods=["POST"])
@require_auth()
def create_memory():
    """Create a current-user ordinary memory item."""
    data = request.get_json() or {}
    content = str(data.get("content") or "").strip()
    if len(content) < 8:
        return jsonify({"error": "记忆内容太短，请补充具体上下文。"}), 400

    memory_manager = _get_memory_manager()
    if not memory_manager:
        return jsonify({"error": "Memory manager not available"}), 503

    session_id = str(data.get("session_id") or "").strip() or None
    try:
        result = store_user_memory(
            content,
            memory_manager=memory_manager,
            session_id=session_id,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        logger.warning("Failed to create user memory", exc_info=True)
        return jsonify({"error": "Failed to create memory"}), 500

    status = 200 if result.get("merged") or result.get("relation") == "duplicate" else 201
    return jsonify({"memory": _store_result_payload(result)}), status


@memories_bp.route("/memories/<memory_id>", methods=["PUT"])
@require_auth()
def update_memory(memory_id: str):
    """Update an active ordinary memory item."""
    data = request.get_json() or {}
    content = str(data.get("content") or "").strip()
    if len(content) < 8:
        return jsonify({"error": "记忆内容太短，请补充具体上下文。"}), 400

    memory_manager = _get_memory_manager()
    if not memory_manager:
        return jsonify({"error": "Memory manager not available"}), 503

    try:
        hit = _find_memory_hit(memory_manager, memory_id)
    except Exception:
        logger.warning("Failed to resolve user memory for update", exc_info=True)
        return jsonify({"error": "Failed to update memory"}), 500
    if not hit:
        return jsonify({"error": "Memory not found"}), 404

    try:
        archived = memory_manager.archive_memory(
            source_uri=hit.source_uri,
            reason="edited by user",
        )
        if not archived:
            return jsonify({"error": "Memory not found"}), 404
        result = memory_manager.store_memory(
            content=content,
            memory_type=hit.memory_type or "preference",
            scope="user",
            source="manual_edit",
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        logger.warning("Failed to update user memory", exc_info=True)
        return jsonify({"error": "Failed to update memory"}), 500

    return jsonify({"memory": _store_result_payload(result.to_dict())})


@memories_bp.route("/memories/<memory_id>", methods=["DELETE"])
@require_auth()
def delete_memory(memory_id: str):
    """Archive an active ordinary memory item."""
    memory_manager = _get_memory_manager()
    if not memory_manager:
        return jsonify({"error": "Memory manager not available"}), 503

    try:
        hit = _find_memory_hit(memory_manager, memory_id)
    except Exception:
        logger.warning("Failed to resolve user memory for delete", exc_info=True)
        return jsonify({"error": "Failed to delete memory"}), 500
    if not hit:
        return jsonify({"error": "Memory not found"}), 404

    try:
        archived = memory_manager.archive_memory(
            source_uri=hit.source_uri,
            reason="deleted by user",
        )
    except Exception:
        logger.warning("Failed to delete user memory", exc_info=True)
        return jsonify({"error": "Failed to delete memory"}), 500
    if not archived:
        return jsonify({"error": "Memory not found"}), 404

    return jsonify({"success": True, "id": memory_id})
