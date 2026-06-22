"""Chat session endpoints: CRUD, messages, title generation."""

from flask import Blueprint, request, jsonify
from flask_cors import cross_origin

from ..services.auth import require_auth, get_current_user
from ..services.llm import LLMPurpose, get_llm_router
from ..config import LimitsConfig
from ..config.system_config import SystemConfigValidationError, resolve_llm_profile_config
from ..utils.logging import get_logger
from ..utils.llm_request_mode import normalize_llm_request_mode

logger = get_logger("routes.sessions")

sessions_bp = Blueprint("sessions", __name__, url_prefix="/agentic")


def _get_orchestrator():
    from . import get_orchestrator
    return get_orchestrator()


def _get_user_stores():
    from . import get_request_stores
    return get_request_stores()


def _get_request_workspace_id():
    from . import get_request_workspace_id
    return get_request_workspace_id()


@sessions_bp.route("/sessions", methods=["GET"])
@require_auth()
def list_sessions():
    """List all chat sessions."""
    chat_store, _execution_store = _get_user_stores()
    sessions = chat_store.list_sessions()

    return jsonify({
        "sessions": [s.to_dict() for s in sessions]
    })


@sessions_bp.route("/sessions", methods=["POST"])
@require_auth()
def create_session():
    """
    Create a new chat session.

    Request body:
        {
            "title": "Optional session title",
            "llm_request_mode": "stream|sync"
        }
    """
    data = request.get_json() or {}
    title = data.get("title")
    llm_request_mode = normalize_llm_request_mode(data.get("llm_request_mode"))
    model_profile_id = str(data.get("model_profile_id") or "").strip()
    try:
        selected_profile = resolve_llm_profile_config(
            model_profile_id or None,
            allow_default_fallback=not bool(model_profile_id),
        )
    except SystemConfigValidationError as exc:
        return jsonify({"error": str(exc)}), 400

    chat_store, execution_store = _get_user_stores()
    session = chat_store.create_session(
        title=title,
        metadata={
            "llm_request_mode": llm_request_mode,
            "model_profile_id": selected_profile["id"],
        },
    )
    for deleted_session_id in chat_store.prune_old_sessions(
        LimitsConfig.from_env().session_max_per_user_workspace
    ):
        execution_store.delete_session(deleted_session_id)

    return jsonify(session.to_dict()), 201


@sessions_bp.route("/sessions/<session_id>", methods=["GET"])
@require_auth()
def get_session(session_id: str):
    """Get a chat session with message history."""
    chat_store, _execution_store = _get_user_stores()
    session = chat_store.get_session(session_id)

    if not session:
        return jsonify({"error": "Session not found"}), 404

    return jsonify(session.to_dict(include_messages=True))


@sessions_bp.route("/sessions/<session_id>", methods=["DELETE"])
@require_auth()
def delete_session(session_id: str):
    """Delete a chat session."""
    current_user = get_current_user()
    chat_store, execution_store = _get_user_stores()
    _get_orchestrator().delete_memory_session(
        session_id=session_id,
        user_id=current_user.username if current_user else None,
        workspace_id=_get_request_workspace_id(),
    )
    chat_deleted = chat_store.delete_session(session_id)
    execution_deleted = execution_store.delete_session(session_id)

    if not chat_deleted and not execution_deleted:
        return jsonify({"error": "Session not found"}), 404

    return jsonify({"success": True})


@sessions_bp.route("/sessions/<session_id>/messages", methods=["GET"])
@require_auth()
def get_session_messages(session_id: str):
    """Get messages for a session."""
    limit = request.args.get("limit", type=int)

    chat_store, _execution_store = _get_user_stores()
    messages = chat_store.get_history(session_id, limit=limit)

    if not messages and not chat_store.get_session(session_id):
        return jsonify({"error": "Session not found"}), 404

    return jsonify({
        "messages": [m.to_dict() for m in messages]
    })


@sessions_bp.route("/sessions/<session_id>/generate-title", methods=["POST", "OPTIONS"])
@cross_origin()
@require_auth()
def generate_session_title(session_id: str):
    """Generate a title for a chat session based on user message."""
    # Handle CORS preflight
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})

    data = request.get_json() or {}
    message = data.get("message", "").strip()

    if not message:
        return jsonify({"error": "Message is required"}), 400

    chat_store, _execution_store = _get_user_stores()
    # Check if session exists
    session = chat_store.get_session(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404

    # Generate title using LLM
    try:
        prompt = "请用简短的中文（不超过15个字）总结以下用户问题的主题，直接输出标题，不要任何解释或标点符号：\n\n{}"

        response = get_llm_router().call(
            purpose=LLMPurpose.TASK,
            messages=[{"role": "user", "content": prompt.format(message)}],
            temperature=0.5,
            max_tokens=50,
            enable_reasoning=False,
        )

        title = response.content.strip()
        # Clean up title - remove quotes, punctuation at the end
        title = title.strip('"\'""''')
        if len(title) > 30:
            title = title[:30] + "..."

        # Update session title
        chat_store.update_session_title(session_id, title)

        logger.info("Generated title for session {}: {}".format(session_id, title))

        return jsonify({
            "title": title,
            "session_id": session_id
        })

    except Exception as e:
        logger.exception("Failed to generate title")
        # Fallback: use truncated message as title
        fallback_title = message[:20] + ("..." if len(message) > 20 else "")
        chat_store.update_session_title(session_id, fallback_title)

        return jsonify({
            "title": fallback_title,
            "session_id": session_id,
            "fallback": True
        })
