"""ACP Server — Agent Communication Protocol REST API (Flask Blueprint).

遵循 A2A 适配器模式：路由层只做请求解析与事件映射，
内部完全复用 AgenticOrchestrator.run() 获得持久化、记忆、反射等基础设施。
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Generator, List, Optional, Tuple

from flask import Blueprint, Response, g, jsonify, request

from agentic.orchestrator import AgenticOrchestrator
from agentic.services.auth import get_current_user, require_auth
from agentic.stores.run_store import RunStatus as StoreRunStatus
from agentic.utils.logging import bind_log_context, get_logger
from agentic.workspaces import (
    enforce_workspace_access,
    get_current_workspace_runtime_config,
    get_workspace_runtime_config,
    resolve_workspace_id,
    workspace_scope,
)

from .config import get_config, ACPConfig
from .models import MessagePartType, MessageRole, RunResponse, RunStatus

logger = get_logger("acp.server")

# ---------------------------------------------------------------------------
# ACP run registry — protocol layer metadata only
# ---------------------------------------------------------------------------
# _ACP_RUN_REGISTRY 只保留协议层元数据引用（messages、context、session 映射等）。
# 实际运行状态持久化委托给 Orchestrator.run_store（RunStore），
# 服务重启后 registry 从 RunStore 按需重建。
_ACP_RUN_REGISTRY: Dict[str, Dict[str, Any]] = {}
_ACP_REGISTRY_LOCK = threading.RLock()

# ACP RunStatus → StoreRunStatus 映射表
_ACP_TO_STORE_STATUS = {
    RunStatus.PENDING: StoreRunStatus.PENDING,
    RunStatus.RUNNING: StoreRunStatus.RUNNING,
    RunStatus.AWAITING: StoreRunStatus.AWAITING,
    RunStatus.COMPLETED: StoreRunStatus.COMPLETED,
    RunStatus.FAILED: StoreRunStatus.FAILED,
    RunStatus.CANCELLED: StoreRunStatus.CANCELLED,
}

_TERMINAL_RUN_DETAILS = {
    RunStatus.RUNNING: "Run is already running",
    RunStatus.COMPLETED: "Run is already completed",
    RunStatus.FAILED: "Run has already failed",
    RunStatus.CANCELLED: "Run is already cancelled",
    RunStatus.AWAITING: "Run is awaiting input",
}


def _to_store_status(acp_status: RunStatus) -> StoreRunStatus:
    return _ACP_TO_STORE_STATUS.get(acp_status, StoreRunStatus.RUNNING)


def _entry_status(entry: Dict[str, Any]) -> RunStatus:
    status = entry.get("status", RunStatus.PENDING)
    if isinstance(status, RunStatus):
        return status
    try:
        return RunStatus(str(status))
    except ValueError:
        return RunStatus.PENDING


def _claim_acp_run(
    entry: Dict[str, Any],
    *,
    allowed_statuses: set[RunStatus],
) -> Tuple[bool, Optional[str], Optional[int]]:
    """Atomically claim a protocol run for orchestrator execution."""
    with _ACP_REGISTRY_LOCK:
        current_status = _entry_status(entry)
        if current_status not in allowed_statuses:
            return (
                False,
                _TERMINAL_RUN_DETAILS.get(current_status, "Run is not executable"),
                409,
            )
        entry["status"] = RunStatus.RUNNING
        entry["updated_at"] = datetime.now(timezone.utc)
        return True, None, None


def _ensure_registry_entry(run_id: str) -> Dict[str, Any] | None:
    """获取 registry entry，缓存未命中时从 RunStore 重建。"""
    with _ACP_REGISTRY_LOCK:
        entry = _ACP_RUN_REGISTRY.get(run_id)
        if entry is not None:
            return entry
    try:
        orch = _get_orchestrator()
        run = orch.run_store.get_run(run_id)
        if run and run.metadata.get("source") == "acp":
            entry = {
                "workspace_id": run.metadata.get("acp_workspace_id"),
                "session_id": run.metadata.get("acp_session_id"),
                "user_query": run.metadata.get("acp_user_query", ""),
                "user_context": run.metadata.get("acp_user_context", {}),
                "lang": run.metadata.get("acp_lang"),
                "user_id": run.metadata.get("acp_user_id"),
                "status": RunStatus(run.metadata.get("acp_status", "pending")),
                "created_at": run.created_at,
                "updated_at": run.updated_at,
                "messages": run.metadata.get("acp_messages", []),
                "error": run.error,
            }
            with _ACP_REGISTRY_LOCK:
                _ACP_RUN_REGISTRY[run_id] = entry
            return entry
    except Exception:
        pass
    return None


def _ensure_acp_run_owner(run_id: str) -> bool:
    current_user = get_current_user()
    if not current_user:
        return True
    entry = _ensure_registry_entry(run_id)
    if not entry:
        return False
    if entry.get("user_id") != current_user.username:
        return False
    return True


def _persist_to_runstore(run_id: str, entry: Dict[str, Any]) -> None:
    """将 ACP run 状态持久化到 Orchestrator.run_store。"""
    try:
        orch = _get_orchestrator()
        acp_status = entry.get("status")
        store_status = (
            _to_store_status(acp_status)
            if isinstance(acp_status, RunStatus)
            else StoreRunStatus.RUNNING
        )
        error = entry.get("error")

        existing = orch.run_store.get_run(run_id)
        if existing:
            if error:
                orch.run_store.fail_run(run_id, error)
            else:
                orch.run_store.update_status(run_id, store_status)
            orch.run_store.update_metadata(run_id, {
                "source": "acp",
                "acp_workspace_id": entry.get("workspace_id"),
                "acp_session_id": entry.get("session_id"),
                "acp_user_query": entry.get("user_query"),
                "acp_user_context": entry.get("user_context"),
                "acp_lang": entry.get("lang"),
                "acp_user_id": entry.get("user_id"),
                "acp_messages": entry.get("messages", []),
                "acp_status": acp_status.value if isinstance(acp_status, RunStatus) else str(acp_status),
            })
        else:
            orch.run_store.create_run(
                run_id=run_id,
                user_request=entry.get("user_query", ""),
                metadata={
                    "source": "acp",
                    "acp_workspace_id": entry.get("workspace_id"),
                    "acp_session_id": entry.get("session_id"),
                    "acp_user_query": entry.get("user_query"),
                    "acp_user_context": entry.get("user_context"),
                    "acp_lang": entry.get("lang"),
                    "acp_user_id": entry.get("user_id"),
                    "acp_messages": entry.get("messages", []),
                    "acp_status": acp_status.value if isinstance(acp_status, RunStatus) else str(acp_status),
                },
            )
            if error:
                orch.run_store.fail_run(run_id, error)
            else:
                orch.run_store.update_status(run_id, store_status)
    except Exception as e:
        logger.warning("[ACP] Failed to persist run %s to RunStore: %s", run_id, e)

# ---------------------------------------------------------------------------
# Blueprint + Auth (Adopts A2A's @require_auth() pattern)
# ---------------------------------------------------------------------------
acp_bp = Blueprint("acp", __name__, url_prefix="/acp")


def require_acp_auth():
    """ACP conditional auth decorator — mirrors A2A's @require_auth().

    - If ACP config ``authentication.type`` is ``"none"``, skips auth.
    - Otherwise delegates to the standard ``require_auth()`` decorator.
    """
    from functools import wraps

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if request.method == "OPTIONS":
                return "", 204
            auth_type = get_config().acp.authentication.type
            if auth_type == "none":
                g.current_user = None
                return fn(*args, **kwargs)
            return require_auth()(fn)(*args, **kwargs)
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Orchestrator reference
# ---------------------------------------------------------------------------
_orchestrator: Optional[AgenticOrchestrator] = None


def set_orchestrator(orchestrator: AgenticOrchestrator) -> None:
    global _orchestrator
    _orchestrator = orchestrator


def _get_orchestrator() -> AgenticOrchestrator:
    if _orchestrator is None:
        from agentic.routes import _orchestrator as routes_orch
        if routes_orch is not None:
            return routes_orch
        raise RuntimeError("Orchestrator not initialized")
    return _orchestrator


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _extract_text_from_messages(messages: List[Any]) -> str:
    text_parts = []
    for msg in messages:
        role = None
        parts = []
        if isinstance(msg, dict):
            role = msg.get("role")
            parts = msg.get("parts", []) or []
        else:
            role = getattr(msg, "role", None)
            parts = getattr(msg, "parts", []) or []
        if role not in (MessageRole.USER, "user"):
            continue
        for part in parts:
            part_type = None
            content = None
            if isinstance(part, dict):
                part_type = part.get("type")
                content = part.get("content")
            else:
                part_type = getattr(part, "type", None)
                content = getattr(part, "content", None)
            if part_type not in (MessagePartType.TEXT, "text"):
                continue
            if isinstance(content, str):
                text_parts.append(content)
            elif isinstance(content, dict):
                text_parts.append(json.dumps(content, ensure_ascii=False))
    return "\n".join(text_parts) if text_parts else ""


def _build_agent_message(text: str, metadata: Optional[Dict] = None) -> Dict:
    return {
        "role": "agent",
        "parts": [{"type": "text", "content": text, "content_type": "text/plain"}],
        "metadata": metadata or {},
    }


# ---------------------------------------------------------------------------
# Runtime config: suppress patterns
# ---------------------------------------------------------------------------

def _suppress_keywords_from_runtime_config(workspace_id: Optional[str] = None) -> List[str]:
    try:
        if workspace_id:
            runtime_config = get_workspace_runtime_config(workspace_id)
        else:
            runtime_config = get_current_workspace_runtime_config()
        patterns = runtime_config.get("acp_suppress_script_patterns", "")
        if isinstance(patterns, str) and patterns.strip():
            return [kw.strip().lower() for kw in patterns.split(",") if kw.strip()]
    except Exception:
        pass
    return []


# ---------------------------------------------------------------------------
# ACP Event mapper — converts orchestrator agent.v2 events → ACP SSE tuples
# ---------------------------------------------------------------------------

def _map_orch_event_to_acp_sse(
    orch_event: Dict[str, Any],
    run_id: str,
    tool_op_map: Dict[str, str],
    suppress_keywords: List[str],
) -> Generator[Tuple[str, Dict[str, Any]], None, None]:
    """Map a single orchestrator.run() event to ACP SSE (event_type, data) pairs.

    Yields (event_type, data_dict) for each ACP SSE message to emit.
    Nothing is yielded for internal/no-op events.
    """
    # Unwrap agent.v2 envelope
    data = orch_event.get("data", {}) if orch_event.get("event") == "agent.v2" else orch_event
    if not isinstance(data, dict):
        return

    event_type = data.get("type", "")
    payload = data.get("payload", {})

    # ── token.delta ──────────────────────────────────────────────
    if event_type == "token.delta":
        text = payload.get("text", "")
        channel = payload.get("channel", "content")
        if channel == "content" and text:
            delta = {"type": "message_delta", "text": text, "run_id": run_id}
            yield ("message_delta", delta)
        elif channel == "reasoning" and text:
            yield ("thinking", {"type": "thinking", "text": text, "run_id": run_id})

    # ── tool.started ─────────────────────────────────────────────
    elif event_type == "tool.started":
        tool_name = payload.get("tool_name", "")
        message = payload.get("message", "")
        tc_id = payload.get("tool_call_id", "")
        tool_input = payload.get("input", {})  # 保存工具输入
        if tool_name == "execute_script" and tc_id:
            op_key = (tool_input.get("operation_key") or "").strip()
            if op_key:
                tool_op_map[tc_id] = op_key
                if op_key.lower() in suppress_keywords:
                    tool_op_map[tc_id] = f"__SUPPRESSED__:{op_key}"
        if tool_name.lower() in suppress_keywords and tc_id:
            tool_op_map[tc_id] = f"__SUPPRESSED__:{tool_name}"
        op_key_or_marker = tool_op_map.get(tc_id, "")
        if isinstance(op_key_or_marker, str) and op_key_or_marker.startswith("__SUPPRESSED__:"):
            return
        # ontology_context 是内部系统工具，前端不可见
        if tool_name == "ontology_context":
            return
        # 构建thinking事件，包含工具名称和输入（用于测试捕获）
        thinking_text = message or f"调用工具: {tool_name}"
        # 如果有输入参数，添加到thinking文本末尾（便于测试捕获）
        if tool_input:
            try:
                thinking_text += f"\n输入参数: {json.dumps(tool_input, ensure_ascii=False)}"
            except (TypeError, ValueError) as exc:
                logger.debug("[ACP] Failed to serialize tool input for thinking event: %s", exc)
        yield ("thinking", {
            "type": "thinking",
            "text": thinking_text,
            "run_id": run_id,
        })

    # ── tool.progress ────────────────────────────────────────────
    elif event_type == "tool.progress":
        msg = payload.get("message", "")
        if msg:
            yield ("thinking", {"type": "thinking", "text": msg, "run_id": run_id})

    # ── tool.finished ────────────────────────────────────────────
    elif event_type == "tool.finished":
        tool_name = payload.get("tool_name", "")
        if tool_name == "ontology_context":
            return
        tc_id = payload.get("tool_call_id", "")
        op_key_or_marker = tool_op_map.pop(tc_id, "")
        if isinstance(op_key_or_marker, str) and op_key_or_marker.startswith("__SUPPRESSED__:"):
            return
        operation_key = op_key_or_marker
        status = payload.get("status", "")
        elapsed_ms = payload.get("elapsed_ms", 0)

        result_data: Dict[str, Any] = {
            "type": "result",
            "text": f"{tool_name} {status} ({elapsed_ms}ms)",
            "done": True,
            "run_id": run_id,
            "tool_name": tool_name,
            "tool_status": status,
            "elapsed_ms": elapsed_ms,
        }
        if operation_key:
            result_data["operation_key"] = operation_key
        tool_result = payload.get("result") or payload.get("output_preview")
        if tool_result is not None:
            result_data["result"] = (
                tool_result
                if isinstance(tool_result, str)
                else json.dumps(tool_result, ensure_ascii=False)
            )
        yield ("result", result_data)

    # ── turn.started → run.started ──────────────────────────────
    elif event_type == "turn.started":
        yield ("run.started", {"type": "run.started", "run_id": run_id})

    # ── turn.finished ────────────────────────────────────────────
    elif event_type == "turn.finished":
        status = payload.get("status", "")
        if status == "error":
            error_msg = payload.get("error", "Turn failed")
            yield ("run.failed", {
                "type": "run.failed",
                "run_id": run_id,
                "error": error_msg,
            })
        elif status == "awaiting_input":
            awaiting = _awaiting_data_from_final_output(payload.get("final_output"), run_id=run_id)
            if awaiting:
                yield ("run.awaiting", awaiting)
        elif status == "success":
            fo = payload.get("final_output")
            if fo and isinstance(fo, str) and fo.strip():
                yield ("answer", {"type": "answer", "text": fo, "run_id": run_id})

    # ── dag_performance → progress ───────────────────────────────
    elif event_type == "dag_performance":
        yield ("progress", {
            "type": "progress",
            "data": payload,
            "run_id": run_id,
        })

    # ── iteration.completed / reasoning.summary (no-op) ──────────
    elif event_type in ("iteration.completed", "reasoning.summary"):
        pass


def _awaiting_data_from_final_output(final_output: Any, *, run_id: str) -> Optional[Dict[str, Any]]:
    if not isinstance(final_output, dict):
        return None
    interaction = final_output.get("interaction")
    if not isinstance(interaction, dict):
        interaction = {}
    return {
        "type": "run.awaiting",
        "question": final_output.get("text") or interaction.get("prompt") or "",
        "tool_call_id": interaction.get("tool_call_id") or interaction.get("id") or "",
        "options": final_output.get("options") or interaction.get("options") or [],
        "interaction": interaction,
        "run_id": run_id,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@acp_bp.route("/agent", methods=["GET"])
def get_agent_detail():
    config: ACPConfig = get_config()
    workspace_id = resolve_workspace_id(request)
    acp_base = request.host_url.rstrip("/") + "/acp"
    auth_type = config.acp.authentication.type

    return jsonify({
        "name": config.agent.name,
        "version": config.agent.version,
        "description": config.agent.description,
        "capabilities": {
            "supported_content_types": config.acp.capabilities.supported_content_types,
            "supported_modes": config.acp.capabilities.supported_modes,
            "features": config.acp.capabilities.features,
        },
        "authentication": {
            "type": auth_type if auth_type != "none" else "none",
            "scheme": "bearer" if auth_type != "none" else None,
        },
        "endpoints": {
            "agent": f"{acp_base}/agent",
            "runs": f"{acp_base}/runs",
            "health": config.health_check.endpoint,
        },
        "metadata": {
            "workspace_id": workspace_id,
            "protocol_version": config.acp.protocol_version,
        },
    })


@acp_bp.route("/runs", methods=["POST"])
@require_acp_auth()
def create_run():
    """创建运行记录（异步模式：创建后通过 execute/stream 执行）。"""
    payload = request.get_json(silent=True) or {}
    workspace_id = resolve_workspace_id(request)
    run_id = f"run_{uuid.uuid4().hex}"

    user_query = _extract_text_from_messages(payload.get("messages", []))
    if not user_query:
        return jsonify({"detail": "No user message provided"}), 400

    session_id = (
        payload.get("context", {}).get("session_id")
        or f"sess_{uuid.uuid4().hex[:8]}"
    )
    user_lang = payload.get("lang")
    user_context = dict(payload.get("context") or {})
    current_user = get_current_user()

    with bind_log_context(workspace_id=workspace_id, run_id=run_id, session_id=session_id):
        logger.info("[ACP] Creating run: %s", run_id)

        # 在 registry 中保存 ACP 协议层元数据
        entry = {
            "workspace_id": workspace_id,
            "session_id": session_id,
            "user_query": user_query,
            "user_context": user_context,
            "lang": user_lang,
            "user_id": current_user.username if current_user else None,
            "status": RunStatus.PENDING,
            "created_at": datetime.now(timezone.utc),
            "messages": [],
            "error": None,
        }
        with _ACP_REGISTRY_LOCK:
            _ACP_RUN_REGISTRY[run_id] = entry
        _persist_to_runstore(run_id, entry)

        run_response = RunResponse(
            run_id=run_id,
            status=RunStatus.PENDING,
            messages=[],
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            metadata={
                "workspace_id": workspace_id,
                "session_id": session_id,
                "user_query": user_query[:100],
            },
        )

        return jsonify(run_response.model_dump(mode="json"))


@acp_bp.route("/runs/<run_id>", methods=["GET"])
@require_acp_auth()
def get_run_status(run_id: str):
    """查询运行状态（优先查 registry 缓存，回退到 Orchestrator.run_store）。"""
    if not _ensure_acp_run_owner(run_id):
        return jsonify({"detail": "Run not found"}), 404
    entry = _ensure_registry_entry(run_id)
    if entry:
        return jsonify({
            "run_id": run_id,
            "status": entry.get("status", RunStatus.PENDING).value,
            "messages": entry.get("messages", []),
            "error": entry.get("error"),
            "created_at": entry["created_at"].isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

    # 再查 orchestrator.run_store
    try:
        orch = _get_orchestrator()
        run = orch.run_store.get_run(run_id)
        if run:
            return jsonify({
                "run_id": run.run_id,
                "status": run.status.value,
                "created_at": run.created_at.isoformat(),
                "updated_at": run.updated_at.isoformat(),
                "completed_at": run.completed_at.isoformat() if run.completed_at else None,
                "result": run.result,
                "error": run.error,
            })
    except Exception:
        pass

    return jsonify({"detail": "Run not found"}), 404


@acp_bp.route("/runs/<run_id>/execute", methods=["POST"])
@require_acp_auth()
def execute_run(run_id: str):
    """同步执行：收集 Orchestrator 所有事件后返回最终结果 JSON。"""
    if not _ensure_acp_run_owner(run_id):
        return jsonify({"detail": "Run not found"}), 404
    entry = _ensure_registry_entry(run_id)
    if not entry:
        return jsonify({"detail": "Run not found"}), 404

    workspace_id = entry["workspace_id"]
    # 续跑用持久化 workspace_id，须重检访问权：owner 移除成员后旧 run 不能继续在其空间内驱动
    enforce_workspace_access(workspace_id)
    session_id = entry["session_id"]
    user_query = entry["user_query"]
    user_context = dict(entry.get("user_context") or {})
    user_lang = entry.get("lang")
    current_user = get_current_user()

    if user_lang:
        user_context["lang"] = user_lang

    with bind_log_context(workspace_id=workspace_id, run_id=run_id, session_id=session_id):
        logger.info("[ACP] Executing run: %s", run_id)

        claimed, detail, status_code = _claim_acp_run(
            entry,
            allowed_statuses={RunStatus.PENDING},
        )
        if not claimed:
            return jsonify({"detail": detail}), status_code
        _persist_to_runstore(run_id, entry)

        try:
            with workspace_scope(workspace_id):
                orch = _get_orchestrator()
                final_answer = ""
                final_status = RunStatus.COMPLETED
                error_msg = None

                for orch_event in orch.run(
                    user_request=user_query,
                    user_context=user_context,
                    session_id=session_id,
                    user_id=current_user.username if current_user else "anonymous",
                    workspace_id=workspace_id,
                    requested_run_id=run_id,
                    allow_existing_acp_run_id=True,
                ):
                    data = (
                        orch_event.get("data", {})
                        if orch_event.get("event") == "agent.v2"
                        else orch_event
                    )
                    if not isinstance(data, dict):
                        continue
                    evt_type = data.get("type", "")
                    evt_payload = data.get("payload", {})

                    if evt_type == "token.delta":
                        text = evt_payload.get("text", "")
                        channel = evt_payload.get("channel", "content")
                        if channel == "content" and text:
                            final_answer += text

                    elif evt_type == "turn.finished":
                        status = evt_payload.get("status", "")
                        if status == "success":
                            fo = evt_payload.get("final_output")
                            if fo and isinstance(fo, str):
                                final_answer = fo
                        elif status == "error":
                            final_status = RunStatus.FAILED
                            error_msg = evt_payload.get("error", "Turn failed")
                        elif status == "awaiting_input":
                            final_status = RunStatus.AWAITING
                            awaiting_data = _awaiting_data_from_final_output(
                                evt_payload.get("final_output"),
                                run_id=run_id,
                            )

                # 更新 registry
                agent_msg = _build_agent_message(final_answer)
                entry["messages"].append(agent_msg)
                entry["status"] = final_status
                entry["error"] = error_msg
                _persist_to_runstore(run_id, entry)

                result = RunResponse(
                    run_id=run_id,
                    status=final_status,
                    messages=[agent_msg],
                    created_at=entry["created_at"],
                    updated_at=datetime.now(timezone.utc),
                    error=error_msg,
                )

                return jsonify(result.model_dump(mode="json"))

        except Exception as e:
            logger.exception("[ACP] Run execution failed: %s", run_id)
            entry["status"] = RunStatus.FAILED
            entry["error"] = str(e)
            _persist_to_runstore(run_id, entry)
            return jsonify(RunResponse(
                run_id=run_id,
                status=RunStatus.FAILED,
                messages=[],
                created_at=entry["created_at"],
                updated_at=datetime.now(timezone.utc),
                error=str(e),
            ).model_dump(mode="json")), 500


def _stream_orchestrator_events(
    run_id: str,
    entry: Dict[str, Any],
    user_query: str,
    user_context: Dict[str, Any],
    workspace_id: str,
    session_id: str,
    user_id: str,
) -> Generator[str, None, None]:
    """核心流式生成器：调用 Orchestrator.run() 并将事件映射为 ACP SSE。"""
    suppress_keywords = _suppress_keywords_from_runtime_config(workspace_id)
    tool_op_map: Dict[str, str] = {}
    final_answer = ""
    awaiting_input = False
    awaiting_data = None
    error_msg = None

    try:
        try:
            with workspace_scope(workspace_id):
                orch = _get_orchestrator()
                for orch_event in orch.run(
                    user_request=user_query,
                    user_context=user_context,
                    session_id=session_id,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    requested_run_id=run_id,
                    allow_existing_acp_run_id=True,
                ):
                    # 逐一映射事件到 ACP SSE
                    for sse_event_type, sse_data in _map_orch_event_to_acp_sse(
                        orch_event, run_id, tool_op_map, suppress_keywords
                    ):
                        # 跟踪状态
                        if sse_event_type == "answer":
                            final_answer += sse_data.get("text", "")
                        elif sse_event_type == "run.failed":
                            error_msg = sse_data.get("error", "Unknown error")
                        elif sse_event_type == "run.awaiting":
                            awaiting_input = True
                            awaiting_data = sse_data

                        yield f"event: {sse_event_type}\ndata: {json.dumps(sse_data, ensure_ascii=False)}\n\n"

                        # 遇到 await 时停止流式输出
                        if sse_event_type == "run.awaiting":
                            break

                    # 外层 break（停止 orchestrator 事件迭代）
                    if awaiting_input:
                        break

        except Exception as exc:
            logger.exception("[ACP] Stream execution failed: run_id=%s", run_id)
            error_msg = str(exc)
            yield f"event: run.failed\ndata: {json.dumps({'type': 'run.failed', 'run_id': run_id, 'error': error_msg}, ensure_ascii=False)}\n\n"

        # ── 最终状态事件 ──────────────────────────────────────────
        if not awaiting_input and not error_msg:
            if final_answer:
                agent_msg = _build_agent_message(final_answer)
                entry.setdefault("messages", []).append(agent_msg)
            entry["status"] = RunStatus.COMPLETED
            entry["error"] = None
            yield f"event: run.completed\ndata: {json.dumps({'type': 'run.completed', 'run_id': run_id}, ensure_ascii=False)}\n\n"
        elif error_msg:
            entry["status"] = RunStatus.FAILED
            entry["error"] = error_msg
        elif awaiting_data:
            entry["status"] = RunStatus.AWAITING
            entry["awaiting"] = awaiting_data
    finally:
        entry["updated_at"] = datetime.now(timezone.utc)
        _persist_to_runstore(run_id, entry)


@acp_bp.route("/runs/<run_id>/stream", methods=["POST"])
@require_acp_auth()
def stream_run(run_id: str):
    """对已创建的 run 执行流式输出。"""
    if not _ensure_acp_run_owner(run_id):
        return jsonify({"detail": "Run not found"}), 404
    entry = _ensure_registry_entry(run_id)
    if not entry:
        return jsonify({"detail": "Run not found"}), 404

    workspace_id = entry["workspace_id"]
    # 续跑用持久化 workspace_id，须重检访问权：owner 移除成员后旧 run 不能继续在其空间内驱动
    enforce_workspace_access(workspace_id)
    session_id = entry["session_id"]
    user_query = entry["user_query"]
    user_context = dict(entry.get("user_context") or {})
    user_lang = entry.get("lang")
    current_user = get_current_user()

    if user_lang:
        user_context["lang"] = user_lang

    with bind_log_context(workspace_id=workspace_id, run_id=run_id, session_id=session_id):
        logger.info("[ACP] Stream run: %s", run_id)
        claimed, detail, status_code = _claim_acp_run(
            entry,
            allowed_statuses={RunStatus.PENDING},
        )
        if not claimed:
            return jsonify({"detail": detail}), status_code
        _persist_to_runstore(run_id, entry)

        return Response(
            _stream_orchestrator_events(
                run_id=run_id,
                entry=entry,
                user_query=user_query,
                user_context=user_context,
                workspace_id=workspace_id,
                session_id=session_id,
                user_id=current_user.username if current_user else "anonymous",
            ),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "close",
            },
        )


@acp_bp.route("/runs/execute_stream", methods=["POST"])
@require_acp_auth()
def execute_stream():
    """快捷端点：创建 + 流式执行一步完成。"""
    payload = request.get_json(silent=True) or {}
    workspace_id = resolve_workspace_id(request)
    run_id = f"run_{uuid.uuid4().hex}"

    user_query = _extract_text_from_messages(payload.get("messages", []))
    if not user_query:
        return jsonify({"detail": "No user message provided"}), 400

    session_id = (
        payload.get("context", {}).get("session_id")
        or f"sess_{uuid.uuid4().hex[:8]}"
    )
    user_lang = payload.get("lang")
    user_context = dict(payload.get("context") or {})
    current_user = get_current_user()

    if user_lang:
        user_context["lang"] = user_lang

    with bind_log_context(workspace_id=workspace_id, run_id=run_id, session_id=session_id):
        logger.info("[ACP] execute_stream: %s", run_id)

        # 注册 ACP run
        entry = {
            "workspace_id": workspace_id,
            "session_id": session_id,
            "user_query": user_query,
            "user_context": user_context,
            "lang": user_lang,
            "user_id": current_user.username if current_user else None,
            "status": RunStatus.RUNNING,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
            "messages": [],
            "error": None,
        }
        with _ACP_REGISTRY_LOCK:
            _ACP_RUN_REGISTRY[run_id] = entry
        _persist_to_runstore(run_id, entry)

        return Response(
            _stream_orchestrator_events(
                run_id=run_id,
                entry=entry,
                user_query=user_query,
                user_context=user_context,
                workspace_id=workspace_id,
                session_id=session_id,
                user_id=current_user.username if current_user else "anonymous",
            ),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "close",
            },
        )


@acp_bp.route("/runs/<run_id>/await", methods=["POST"])
@require_acp_auth()
def respond_to_await(run_id: str):
    """响应 await 请求（同步模式）。"""
    if not _ensure_acp_run_owner(run_id):
        return jsonify({"detail": "Run not found"}), 404
    entry = _ensure_registry_entry(run_id)
    if not entry:
        return jsonify({"detail": "Run not found"}), 404
    if entry.get("status") != RunStatus.AWAITING:
        return jsonify({"detail": "Run is not in awaiting state"}), 400

    payload = request.get_json(silent=True) or {}
    workspace_id = entry["workspace_id"]
    # 续跑用持久化 workspace_id，须重检访问权：owner 移除成员后旧 run 不能继续在其空间内驱动
    enforce_workspace_access(workspace_id)
    session_id = entry["session_id"]
    user_lang = entry.get("lang")
    current_user = get_current_user()

    # 提取用户响应
    response_msg = payload.get("response", {})
    user_response = _extract_text_from_messages([response_msg]) if response_msg else ""
    if not user_response:
        user_response = payload.get("response_text", "")

    user_context = dict(entry.get("user_context") or {})
    user_context["interaction_response"] = {
        "interaction_id": payload.get("await_id", ""),
        "value": user_response,
    }
    if user_lang:
        user_context["lang"] = user_lang

    with bind_log_context(workspace_id=workspace_id, run_id=run_id, session_id=session_id):
        logger.info("[ACP] Responding to await: %s", run_id)
        claimed, detail, status_code = _claim_acp_run(
            entry,
            allowed_statuses={RunStatus.AWAITING},
        )
        if not claimed:
            return jsonify({"detail": detail}), status_code
        _persist_to_runstore(run_id, entry)

        try:
            with workspace_scope(workspace_id):
                orch = _get_orchestrator()
                final_answer = ""
                final_status = RunStatus.COMPLETED
                error_msg = None

                for orch_event in orch.run(
                    user_request=user_response,
                    user_context=user_context,
                    session_id=session_id,
                    user_id=current_user.username if current_user else "anonymous",
                    workspace_id=workspace_id,
                    requested_run_id=run_id,
                    allow_existing_acp_run_id=True,
                ):
                    data = (
                        orch_event.get("data", {})
                        if orch_event.get("event") == "agent.v2"
                        else orch_event
                    )
                    if not isinstance(data, dict):
                        continue
                    evt_type = data.get("type", "")
                    evt_payload = data.get("payload", {})

                    if evt_type == "token.delta":
                        text = evt_payload.get("text", "")
                        channel = evt_payload.get("channel", "content")
                        if channel == "content" and text:
                            final_answer += text

                    elif evt_type == "turn.finished":
                        status = evt_payload.get("status", "")
                        if status == "success":
                            fo = evt_payload.get("final_output")
                            if fo and isinstance(fo, str):
                                final_answer = fo
                        elif status == "error":
                            final_status = RunStatus.FAILED
                            error_msg = evt_payload.get("error", "Turn failed")
                        elif status == "awaiting_input":
                            final_status = RunStatus.AWAITING
                            awaiting_data = _awaiting_data_from_final_output(
                                evt_payload.get("final_output"),
                                run_id=run_id,
                            )

                agent_msg = _build_agent_message(final_answer)
                entry["messages"].append(agent_msg)
                entry["status"] = final_status
                entry["error"] = error_msg
                _persist_to_runstore(run_id, entry)

                result = RunResponse(
                    run_id=run_id,
                    status=final_status,
                    messages=[agent_msg],
                    created_at=entry["created_at"],
                    updated_at=datetime.now(timezone.utc),
                    error=error_msg,
                )

                return jsonify(result.model_dump(mode="json"))

        except Exception as e:
            logger.exception("[ACP] Await execution failed: %s", run_id)
            entry["status"] = RunStatus.FAILED
            entry["error"] = str(e)
            _persist_to_runstore(run_id, entry)
            return jsonify(RunResponse(
                run_id=run_id,
                status=RunStatus.FAILED,
                messages=[],
                created_at=entry["created_at"],
                updated_at=datetime.now(timezone.utc),
                error=str(e),
            ).model_dump(mode="json")), 500


@acp_bp.route("/runs/<run_id>/interact_stream", methods=["POST"])
@require_acp_auth()
def interact_stream(run_id: str):
    """流式交互：响应 await 并以 SSE 流式返回后续过程。"""
    if not _ensure_acp_run_owner(run_id):
        return jsonify({"detail": "Run not found"}), 404
    entry = _ensure_registry_entry(run_id)
    if not entry:
        return jsonify({"detail": "Run not found"}), 404
    if entry.get("status") != RunStatus.AWAITING:
        return jsonify({"detail": "Run is not in awaiting state"}), 400

    payload = request.get_json(silent=True) or {}
    workspace_id = entry["workspace_id"]
    # 续跑用持久化 workspace_id，须重检访问权：owner 移除成员后旧 run 不能继续在其空间内驱动
    enforce_workspace_access(workspace_id)
    session_id = entry["session_id"]
    user_lang = entry.get("lang")
    current_user = get_current_user()

    # 提取用户响应
    response_msg = payload.get("response", {})
    user_response = _extract_text_from_messages([response_msg]) if response_msg else ""
    if not user_response:
        user_response = payload.get("response_text", "")

    user_context = dict(entry.get("user_context") or {})
    user_context["interaction_response"] = {
        "interaction_id": payload.get("await_id", ""),
        "value": user_response,
    }
    if user_lang:
        user_context["lang"] = user_lang

    with bind_log_context(workspace_id=workspace_id, run_id=run_id, session_id=session_id):
        logger.info("[ACP] interact_stream: %s", run_id)
        claimed, detail, status_code = _claim_acp_run(
            entry,
            allowed_statuses={RunStatus.AWAITING},
        )
        if not claimed:
            return jsonify({"detail": detail}), status_code
        _persist_to_runstore(run_id, entry)

        return Response(
            _stream_orchestrator_events(
                run_id=run_id,
                entry=entry,
                user_query=user_response,
                user_context=user_context,
                workspace_id=workspace_id,
                session_id=session_id,
                user_id=current_user.username if current_user else "anonymous",
            ),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "close",
            },
        )


@acp_bp.route("/runs/<run_id>", methods=["DELETE"])
@require_acp_auth()
def cancel_run(run_id: str):
    """取消运行。"""
    if not _ensure_acp_run_owner(run_id):
        return jsonify({"detail": "Run not found"}), 404
    entry = _ensure_registry_entry(run_id)
    if not entry:
        return jsonify({"detail": "Run not found"}), 404

    try:
        orch = _get_orchestrator()
        orch.cancel_run(run_id)
    except Exception as e:
        logger.warning("[ACP] Cancel via orchestrator failed: %s", e)

    entry["status"] = RunStatus.CANCELLED
    entry["updated_at"] = datetime.now(timezone.utc)
    _persist_to_runstore(run_id, entry)
    return jsonify({"run_id": run_id, "status": "cancelled"})


@acp_bp.route("/runs", methods=["GET"])
@require_acp_auth()
def list_runs():
    """列出当前用户的 ACP 运行记录（优先从 RunStore 读取）。"""
    current_user = get_current_user()
    current_username = current_user.username if current_user else None
    runs = []

    # 从 RunStore 读取持久化的 ACP runs
    try:
        orch = _get_orchestrator()
        store_runs = orch.run_store.list_runs(limit=200)
        for r in store_runs:
            if r.metadata.get("source") == "acp":
                if current_username and r.metadata.get("acp_user_id") != current_username:
                    continue
                runs.append({
                    "run_id": r.run_id,
                    "status": r.status.value,
                    "created_at": r.created_at.isoformat(),
                    "updated_at": r.updated_at.isoformat(),
                })
    except Exception:
        pass

    # 补充 registry 中尚未持久化的运行
    seen = {r["run_id"] for r in runs}
    with _ACP_REGISTRY_LOCK:
        registry_snapshot = list(_ACP_RUN_REGISTRY.items())
    for run_id, entry in registry_snapshot:
        if run_id not in seen:
            if current_username and entry.get("user_id") != current_username:
                continue
            runs.append({
                "run_id": run_id,
                "status": entry.get("status", RunStatus.PENDING).value,
                "created_at": entry.get("created_at", datetime.now(timezone.utc)).isoformat(),
                "updated_at": entry.get("updated_at", datetime.now(timezone.utc)).isoformat(),
            })

    return jsonify({"runs": runs})
