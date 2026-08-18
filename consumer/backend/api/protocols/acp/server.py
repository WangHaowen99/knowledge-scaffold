"""ACP REST adapter over the canonical asynchronous run platform."""

from __future__ import annotations

import json
import queue
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Generator, List, Optional, Tuple

from flask import Blueprint, Response, abort, g, jsonify, make_response, request

from agent.core.orchestrator import AgenticOrchestrator
from agent.core.shared.interaction_policy import set_user_interaction_capability
from api.auth.auth import get_current_user, require_auth
from api.final_output import coerce_final_output_to_text, extract_final_output_text
from persistence.run_store import RunStatus as StoreRunStatus
from utils.identifiers import validate_session_id
from utils.logging import bind_log_context, get_logger
from workspace.workspaces import (
    enforce_workspace_access,
    get_default_workspace_id,
    get_current_workspace_runtime_config,
    get_workspace,
    get_workspace_runtime_config,
    resolve_workspace_id,
    user_can_access_workspace,
)

from api.protocols.acp.config import get_config, ACPConfig
from api.protocols.acp.models import MessagePartType, MessageRole, RunResponse, RunStatus
from agent.runtime.bridge import public_event_envelope, wait_for_run
from agent.runtime.contracts import RunScope, RuntimeInvocationContext
from agent.runtime.event_bus import STREAM_CLOSED, STREAM_RECONNECT
from agent.core.shared.modes import resolve_agent_mode
from llm.llm_request_mode import normalize_llm_request_mode

logger = get_logger("acp.server")

# ---------------------------------------------------------------------------
# ACP run registry — protocol layer metadata only
# ---------------------------------------------------------------------------
# _ACP_RUN_REGISTRY 只保留协议层元数据引用（messages、context、session 映射等）。
# AsyncRunManager 是执行启动后的唯一状态来源；RunStore 仅保留 ACP 协议投影。
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


class _ACPAnonymousPrincipal:
    username = None
    role = None


_ACP_ANONYMOUS_PRINCIPAL = _ACPAnonymousPrincipal()


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
                "workspace_id": run.metadata.get("acp_workspace_id") or run.metadata.get("workspace_id"),
                "session_id": run.metadata.get("acp_session_id") or run.metadata.get("session_id"),
                "user_query": run.metadata.get("acp_user_query", ""),
                "user_context": run.metadata.get("acp_user_context", {}),
                "lang": run.metadata.get("acp_lang"),
                "user_id": run.metadata.get("acp_user_id") or run.metadata.get("user_id"),
                "status": RunStatus(run.metadata.get("acp_status", "pending")),
                "runtime_started": bool(run.metadata.get("acp_runtime_started", False)),
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
    request_workspace_id = _resolve_acp_request_workspace_id()
    entry = _ensure_registry_entry(run_id)
    if not entry:
        return False
    entry_user_id = str(entry.get("user_id") or "").strip()
    if current_user and entry_user_id != current_user.username:
        return False
    if current_user is None and entry_user_id:
        return False
    workspace_id = str(entry.get("workspace_id") or "").strip()
    if not workspace_id:
        return False
    if request_workspace_id != workspace_id:
        return False
    _enforce_acp_workspace_access(workspace_id)
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
                "workspace_id": entry.get("workspace_id"),
                "session_id": entry.get("session_id"),
                "user_id": entry.get("user_id"),
                "acp_workspace_id": entry.get("workspace_id"),
                "acp_session_id": entry.get("session_id"),
                "acp_user_query": entry.get("user_query"),
                "acp_user_context": entry.get("user_context"),
                "acp_lang": entry.get("lang"),
                "acp_user_id": entry.get("user_id"),
                "acp_messages": entry.get("messages", []),
                "acp_status": acp_status.value if isinstance(acp_status, RunStatus) else str(acp_status),
                "acp_runtime_started": bool(entry.get("runtime_started", False)),
            })
        else:
            orch.run_store.create_run(
                run_id=run_id,
                user_request=entry.get("user_query", ""),
                metadata={
                    "source": "acp",
                    "workspace_id": entry.get("workspace_id"),
                    "session_id": entry.get("session_id"),
                    "user_id": entry.get("user_id"),
                    "acp_workspace_id": entry.get("workspace_id"),
                    "acp_session_id": entry.get("session_id"),
                    "acp_user_query": entry.get("user_query"),
                    "acp_user_context": entry.get("user_context"),
                    "acp_lang": entry.get("lang"),
                    "acp_user_id": entry.get("user_id"),
                    "acp_messages": entry.get("messages", []),
                    "acp_status": acp_status.value if isinstance(acp_status, RunStatus) else str(acp_status),
                    "acp_runtime_started": bool(entry.get("runtime_started", False)),
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
        from api.routes import _orchestrator as routes_orch
        if routes_orch is not None:
            return routes_orch
        raise RuntimeError("Orchestrator not initialized")
    return _orchestrator


def _abort_acp_workspace_access_denied() -> None:
    abort(make_response(
        jsonify({"error": "Forbidden", "code": "WORKSPACE_ACCESS_DENIED"}),
        403,
    ))


def _acp_workspace_access_allowed(workspace_id: str) -> bool:
    """Check current membership, including explicit auth=none semantics."""
    try:
        workspace = get_workspace(workspace_id)
        default_workspace_id = get_default_workspace_id()
    except (KeyError, ValueError):
        return False
    current_user = get_current_user()
    principal = current_user or _ACP_ANONYMOUS_PRINCIPAL
    return user_can_access_workspace(
        principal,
        workspace,
        workspace_id == default_workspace_id,
    )


def _enforce_acp_workspace_access(workspace_id: str) -> str:
    """Recheck a request or persisted ACP workspace before any run operation."""
    current_user = get_current_user()
    if current_user is not None:
        return enforce_workspace_access(workspace_id)
    if not _acp_workspace_access_allowed(workspace_id):
        _abort_acp_workspace_access_denied()
    return workspace_id


def _resolve_acp_request_workspace_id() -> str:
    return _enforce_acp_workspace_access(resolve_workspace_id(request))


def _get_async_runtime_bridge():
    """Return the manager bound to the current production orchestrator."""
    from agent.runtime.bootstrap import (
        async_manager_matches,
        call_async_run,
        get_async_run_manager,
    )

    orchestrator = _get_orchestrator()
    if not async_manager_matches(orchestrator):
        raise RuntimeError("ACP async runtime is not bound to the current orchestrator")
    return get_async_run_manager(), call_async_run


def _async_context_for_acp(run_id: str, entry: Dict[str, Any]) -> RuntimeInvocationContext:
    context = dict(entry.get("user_context") or {})
    return RuntimeInvocationContext(
        run_id=run_id,
        session_id=str(entry.get("session_id") or ""),
        turn_id=1,
        workspace_id=str(entry.get("workspace_id") or ""),
        username=str(entry.get("user_id") or "anonymous"),
        query=str(entry.get("user_query") or ""),
        agent_mode=resolve_agent_mode(context),
        llm_request_mode=normalize_llm_request_mode(
            context.get("llm_request_mode")
        ),
        model_profile_id=str(context.get("model_profile_id") or "").strip() or None,
        arguments=(
            dict(context.get("arguments"))
            if isinstance(context.get("arguments"), dict)
            else None
        ),
        include_trace=False,
        supports_user_interaction=True,
        allow_existing_run_id=True,
    )


def _ensure_async_acp_run(run_id: str, entry: Dict[str, Any]):
    manager, call_async = _get_async_runtime_bridge()
    scope = RunScope(
        username=str(entry.get("user_id") or "anonymous"),
        workspace_id=str(entry.get("workspace_id") or ""),
        scopes=("agent:run", "agent:read", "agent:cancel", "agent:interact"),
    )
    snapshot = call_async(manager.get_run(run_id, scope))
    if snapshot is None:
        snapshot, _created = call_async(
            manager.create_run(_async_context_for_acp(run_id, entry), scope)
        )
    with _ACP_REGISTRY_LOCK:
        entry["runtime_started"] = True
        entry["updated_at"] = datetime.now(timezone.utc)
    _persist_to_runstore(run_id, entry)
    return manager, call_async


def _acp_response_from_async_snapshot(snapshot, entry: Dict[str, Any]) -> RunResponse:
    status = {
        "queued": RunStatus.PENDING,
        "running": RunStatus.RUNNING,
        "awaiting_input": RunStatus.AWAITING,
        "completed": RunStatus.COMPLETED,
        "failed": RunStatus.FAILED,
        "cancelled": RunStatus.CANCELLED,
    }.get(snapshot.status.value, RunStatus.FAILED)
    content = None
    if snapshot.result is not None:
        content = snapshot.result.content or coerce_final_output_to_text(
            snapshot.result.structured_content
        )
    messages = [_build_agent_message(content)] if content else []
    awaiting = _build_await_response(snapshot.pending_interaction)
    return RunResponse(
        run_id=snapshot.run_id,
        status=status,
        messages=messages,
        created_at=datetime.fromisoformat(snapshot.created_at.replace("Z", "+00:00")),
        updated_at=datetime.fromisoformat(snapshot.updated_at.replace("Z", "+00:00")),
        awaiting=awaiting,
        error=snapshot.error.message if snapshot.error else None,
    )


def _acp_sse_from_public_event(
    event: Any,
    *,
    run_id: str,
    entry: Dict[str, Any],
    workspace_id: str,
    tool_op_map: Dict[str, str],
) -> Generator[str, None, None]:
    envelope = public_event_envelope(event)
    for event_type, sse_data in _map_orch_event_to_acp_sse(
        envelope,
        run_id,
        tool_op_map,
        _suppress_keywords_from_runtime_config(workspace_id),
    ):
        yield "event: {}\ndata: {}\n\n".format(
            event_type,
            json.dumps(sse_data, ensure_ascii=False),
        )
    if event.event_type != "turn.finished":
        return

    payload = event.payload if isinstance(event.payload, dict) else {}
    final_text = coerce_final_output_to_text(_terminal_output(payload))
    raw_error = payload.get("error")
    if isinstance(raw_error, dict):
        public_error = str(raw_error.get("message") or "Run failed")
    else:
        public_error = str(raw_error or "") or None
    with _ACP_REGISTRY_LOCK:
        entry["status"] = {
            "completed": RunStatus.COMPLETED,
            "failed": RunStatus.FAILED,
            "cancelled": RunStatus.CANCELLED,
            "awaiting_input": RunStatus.AWAITING,
        }.get(event.status, RunStatus.FAILED)
        entry["error"] = public_error
        entry["updated_at"] = datetime.now(timezone.utc)
        if final_text:
            entry.setdefault("messages", []).append(
                _build_agent_message(final_text)
            )
        awaiting_data = _awaiting_data_from_final_output(
            _terminal_output(payload),
            run_id=run_id,
        )
        if awaiting_data is not None:
            entry["awaiting"] = awaiting_data
        else:
            entry.pop("awaiting", None)
    _persist_to_runstore(run_id, entry)


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


def _build_await_response(awaiting_data: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(awaiting_data, dict):
        return None
    interaction = awaiting_data.get("interaction")
    if not isinstance(interaction, dict):
        interaction = awaiting_data
    await_id = str(
        awaiting_data.get("tool_call_id")
        or interaction.get("tool_call_id")
        or interaction.get("id")
        or ""
    ).strip()
    question = str(
        awaiting_data.get("question")
        or awaiting_data.get("prompt")
        or awaiting_data.get("text")
        or interaction.get("prompt")
        or ""
    ).strip()
    if not await_id and not question:
        return None
    return {
        "await_id": await_id,
        "message": _build_agent_message(question),
        "options": awaiting_data.get("options") or interaction.get("options") or None,
        "timeout": None,
    }


def _terminal_output(payload: Dict[str, Any]) -> Any:
    if payload.get("structured_content") is not None:
        return payload.get("structured_content")
    if payload.get("content") is not None:
        return payload.get("content")
    return payload.get("final_output")


def _interaction_response_values(
    payload: Dict[str, Any],
    pending: Dict[str, Any],
    user_response: str,
) -> tuple[str, Any, Optional[str]]:
    response = payload.get("response")
    response = response if isinstance(response, dict) else {}
    metadata = response.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    interaction_id = str(
        payload.get("await_id")
        or payload.get("interaction_id")
        or pending.get("id")
        or pending.get("interaction_id")
        or ""
    ).strip()
    option_id = str(
        payload.get("option_id")
        or payload.get("selected_option_id")
        or response.get("option_id")
        or response.get("selected_option_id")
        or metadata.get("option_id")
        or metadata.get("selected_option_id")
        or ""
    ).strip() or None
    options = pending.get("options")
    options = options if isinstance(options, list) else []
    selected = next(
        (
            option
            for option in options
            if isinstance(option, dict)
            and (
                (option_id and str(option.get("id") or option.get("option_id") or "") == option_id)
                or (
                    not option_id
                    and user_response
                    in {
                        str(option.get("id") or option.get("option_id") or ""),
                        str(option.get("value") or ""),
                        str(option.get("label") or ""),
                    }
                )
            )
        ),
        None,
    )
    if selected is not None:
        option_id = str(
            selected.get("id") or selected.get("option_id") or ""
        ).strip() or None
        value = selected.get("value")
        if value is None:
            value = user_response or option_id
    else:
        value = user_response
    return interaction_id, value, option_id


def _merge_terminal_answer(existing_answer: str, final_output: Any) -> str:
    final_text = extract_final_output_text(final_output)
    return final_text or str(existing_answer or "")


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
    """Map one public runtime envelope to ACP SSE event/data pairs.

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
    if event_type in {"token.delta", "reasoning.delta"}:
        text = payload.get("text", "")
        channel = (
            "reasoning"
            if event_type == "reasoning.delta"
            else payload.get("channel", "content")
        )
        if channel == "content" and text:
            delta = {"type": "message_delta", "text": text, "run_id": run_id}
            yield ("message_delta", delta)
        elif channel == "reasoning" and text:
            yield ("thinking", {"type": "thinking", "text": text, "run_id": run_id})

    # ── tool.started ─────────────────────────────────────────────
    elif event_type == "tool.started":
        tool_name = payload.get("name") or payload.get("tool_name", "")
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
        tool_name = payload.get("name") or payload.get("tool_name", "")
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
        if status in {"error", "failed"}:
            error_msg = payload.get("error", "Turn failed")
            yield ("run.failed", {
                "type": "run.failed",
                "run_id": run_id,
                "error": error_msg,
            })
        elif status == "awaiting_input":
            awaiting = _awaiting_data_from_final_output(
                _terminal_output(payload),
                run_id=run_id,
            )
            if awaiting:
                yield ("run.awaiting", awaiting)
        elif status in {"success", "completed"}:
            final_text = coerce_final_output_to_text(_terminal_output(payload))
            if final_text:
                yield ("answer", {"type": "answer", "text": final_text, "run_id": run_id})
            yield ("run.completed", {"type": "run.completed", "run_id": run_id})
        elif status in {"cancelled", "canceled"}:
            yield ("run.cancelled", {"type": "run.cancelled", "run_id": run_id})

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
@require_acp_auth()
def get_agent_detail():
    config: ACPConfig = get_config()
    workspace_id = _resolve_acp_request_workspace_id()
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
    workspace_id = _resolve_acp_request_workspace_id()
    run_id = f"run_{uuid.uuid4().hex}"

    user_query = _extract_text_from_messages(payload.get("messages", []))
    if not user_query:
        return jsonify({"detail": "No user message provided"}), 400

    try:
        session_id = validate_session_id(
            payload.get("context", {}).get("session_id")
            or f"sess_{uuid.uuid4().hex[:8]}"
        )
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400
    user_lang = payload.get("lang")
    user_context = set_user_interaction_capability(
        dict(payload.get("context") or {}),
        supported=True,
    )
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
            "runtime_started": False,
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
    """Return the ACP projection of the canonical async run."""
    if not _ensure_acp_run_owner(run_id):
        return jsonify({"detail": "Run not found"}), 404
    entry = _ensure_registry_entry(run_id)
    if entry:
        manager, call_async = _get_async_runtime_bridge()
        try:
            scope = RunScope(
                username=str(entry.get("user_id") or "anonymous"),
                workspace_id=str(entry.get("workspace_id") or ""),
                scopes=("agent:read",),
            )
            snapshot = call_async(manager.get_run(run_id, scope))
            if snapshot is not None:
                return jsonify(
                    _acp_response_from_async_snapshot(snapshot, entry).model_dump(
                        mode="json"
                    )
                )
            if entry.get("runtime_started"):
                return jsonify({"detail": "Run state is unavailable"}), 503
        except Exception:
            logger.exception("[ACP] Async status lookup failed: %s", run_id)
            return jsonify({"detail": "Run status lookup failed"}), 503
        return jsonify({
            "run_id": run_id,
            "status": entry.get("status", RunStatus.PENDING).value,
            "messages": entry.get("messages", []),
            "error": entry.get("error"),
            "created_at": entry["created_at"].isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

    return jsonify({"detail": "Run not found"}), 404


@acp_bp.route("/runs/<run_id>/execute", methods=["POST"])
@require_acp_auth()
def execute_run(run_id: str):
    """Synchronously wait for one canonical async run and return ACP JSON."""
    if not _ensure_acp_run_owner(run_id):
        return jsonify({"detail": "Run not found"}), 404
    entry = _ensure_registry_entry(run_id)
    if not entry:
        return jsonify({"detail": "Run not found"}), 404

    claimed, detail, status_code = _claim_acp_run(
        entry,
        allowed_statuses={RunStatus.PENDING},
    )
    if not claimed:
        return jsonify({"detail": detail}), status_code
    _persist_to_runstore(run_id, entry)

    try:
        manager, call_async = _ensure_async_acp_run(run_id, entry)
        scope = RunScope(
            username=str(entry.get("user_id") or "anonymous"),
            workspace_id=str(entry.get("workspace_id") or ""),
            scopes=("agent:run", "agent:read", "agent:cancel", "agent:interact"),
        )
        snapshot = wait_for_run(
            manager,
            call_async,
            run_id,
            scope,
            timeout_seconds=600,
        )
        response = _acp_response_from_async_snapshot(snapshot, entry)
        with _ACP_REGISTRY_LOCK:
            entry["status"] = response.status
            entry["error"] = response.error
        _persist_to_runstore(run_id, entry)
        return jsonify(response.model_dump(mode="json"))
    except TimeoutError:
        logger.warning("[ACP] Async execution timed out: %s", run_id)
        return jsonify({"detail": "Run execution timed out"}), 504
    except Exception:
        logger.exception("[ACP] Async execution failed: %s", run_id)
        with _ACP_REGISTRY_LOCK:
            entry["status"] = RunStatus.FAILED
            entry["error"] = "Run execution failed"
        _persist_to_runstore(run_id, entry)
        return jsonify({"detail": "Run execution failed"}), 500


def _stream_orchestrator_events(
    run_id: str,
    entry: Dict[str, Any],
    user_query: str,
    user_context: Dict[str, Any],
    workspace_id: str,
    session_id: str,
    user_id: str,
    after_seq: int = 0,
) -> Generator[str, None, None]:
    """Subscribe to PublicEvents and project them to ACP SSE."""
    manager, call_async = _get_async_runtime_bridge()
    try:
        scope = RunScope(
            username=str(user_id or "anonymous"),
            workspace_id=str(workspace_id or ""),
            scopes=("agent:run", "agent:read"),
        )
        snapshot = call_async(manager.get_run(run_id, scope))
        if snapshot is None:
            entry["workspace_id"] = workspace_id
            entry["session_id"] = session_id
            entry["user_id"] = user_id
            context = _async_context_for_acp(run_id, entry)
            call_async(manager.create_run(context, scope))
        with _ACP_REGISTRY_LOCK:
            entry["runtime_started"] = True
            entry["updated_at"] = datetime.now(timezone.utc)
        _persist_to_runstore(run_id, entry)
        subscription = call_async(
            manager.subscribe(run_id, scope, after_seq=after_seq)
        )
    except Exception:
        logger.exception("[ACP] Async subscription failed: %s", run_id)
        yield "event: run.failed\ndata: {}\n\n".format(
            json.dumps(
                {
                    "type": "run.failed",
                    "run_id": run_id,
                    "error": "Run subscription failed",
                }
            )
        )
        return

    tool_op_map: Dict[str, str] = {}
    try:
        for event in subscription.replay:
            yield from _acp_sse_from_public_event(
                event,
                run_id=run_id,
                entry=entry,
                workspace_id=workspace_id,
                tool_op_map=tool_op_map,
            )
            if event.event_type == "turn.finished":
                return
        if subscription.closed:
            return
        while True:
            try:
                item = subscription.queue.get(timeout=15)
            except queue.Empty:
                yield ": heartbeat\n\n"
                continue
            if item is STREAM_CLOSED or item is STREAM_RECONNECT:
                return
            if not hasattr(item, "event_type"):
                return
            yield from _acp_sse_from_public_event(
                item,
                run_id=run_id,
                entry=entry,
                workspace_id=workspace_id,
                tool_op_map=tool_op_map,
            )
            if item.event_type == "turn.finished":
                return
    finally:
        call_async(manager.unsubscribe(subscription))


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
    workspace_id = _resolve_acp_request_workspace_id()
    run_id = f"run_{uuid.uuid4().hex}"

    user_query = _extract_text_from_messages(payload.get("messages", []))
    if not user_query:
        return jsonify({"detail": "No user message provided"}), 400

    try:
        session_id = validate_session_id(
            payload.get("context", {}).get("session_id")
            or f"sess_{uuid.uuid4().hex[:8]}"
        )
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400
    user_lang = payload.get("lang")
    user_context = set_user_interaction_capability(
        dict(payload.get("context") or {}),
        supported=True,
    )
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
            "runtime_started": False,
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
    """Resume a HITL checkpoint and synchronously return the next phase."""
    if not _ensure_acp_run_owner(run_id):
        return jsonify({"detail": "Run not found"}), 404
    entry = _ensure_registry_entry(run_id)
    if not entry:
        return jsonify({"detail": "Run not found"}), 404
    if entry.get("status") != RunStatus.AWAITING:
        return jsonify({"detail": "Run is not in awaiting state"}), 400

    payload = request.get_json(silent=True) or {}
    response_msg = payload.get("response", {})
    user_response = _extract_text_from_messages([response_msg]) if response_msg else ""
    if not user_response:
        user_response = str(payload.get("response_text") or "")

    manager, call_async = _get_async_runtime_bridge()
    try:
        scope = RunScope(
            username=str(entry.get("user_id") or "anonymous"),
            workspace_id=str(entry.get("workspace_id") or ""),
            scopes=("agent:interact", "agent:read"),
        )
        snapshot = call_async(manager.get_run(run_id, scope))
        if snapshot is None or snapshot.pending_interaction is None:
            return jsonify({"detail": "Run is not awaiting an interaction"}), 409
        interaction_id, value, option_id = _interaction_response_values(
            payload,
            snapshot.pending_interaction,
            user_response,
        )
        previous_last_seq = snapshot.last_seq
        call_async(
            manager.interact(
                run_id,
                scope,
                interaction_id=interaction_id,
                value=value,
                option_id=option_id,
            )
        )
        final_snapshot = wait_for_run(
            manager,
            call_async,
            run_id,
            scope,
            timeout_seconds=600,
            after_seq=previous_last_seq,
        )
        response = _acp_response_from_async_snapshot(final_snapshot, entry)
        with _ACP_REGISTRY_LOCK:
            entry["status"] = response.status
            entry["error"] = response.error
        _persist_to_runstore(run_id, entry)
        return jsonify(response.model_dump(mode="json"))
    except TimeoutError:
        return jsonify({"detail": "Run interaction timed out"}), 504
    except Exception:
        logger.exception("[ACP] Async await failed: %s", run_id)
        return jsonify({"detail": "Run interaction failed"}), 409


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
    session_id = entry["session_id"]
    current_user = get_current_user()

    # 提取用户响应
    response_msg = payload.get("response", {})
    user_response = _extract_text_from_messages([response_msg]) if response_msg else ""
    if not user_response:
        user_response = payload.get("response_text", "")

    with bind_log_context(workspace_id=workspace_id, run_id=run_id, session_id=session_id):
        logger.info("[ACP] interact_stream: %s", run_id)
        manager, call_async = _get_async_runtime_bridge()
        try:
            scope = RunScope(
                username=str(entry.get("user_id") or "anonymous"),
                workspace_id=str(workspace_id or ""),
                scopes=("agent:interact", "agent:read"),
            )
            before = call_async(manager.get_run(run_id, scope))
            if before is None or before.pending_interaction is None:
                return jsonify({"detail": "Run is not awaiting an interaction"}), 409
            interaction_id, value, option_id = _interaction_response_values(
                payload,
                before.pending_interaction,
                user_response,
            )
            after_seq = before.last_seq
            call_async(
                manager.interact(
                    run_id,
                    scope,
                    interaction_id=interaction_id,
                    value=value,
                    option_id=option_id,
                )
            )
            with _ACP_REGISTRY_LOCK:
                entry["status"] = RunStatus.RUNNING
                entry["updated_at"] = datetime.now(timezone.utc)
            _persist_to_runstore(run_id, entry)
        except Exception:
            logger.exception("[ACP] Async interact stream failed: %s", run_id)
            return jsonify({"detail": "Run interaction failed"}), 409

        return Response(
            _stream_orchestrator_events(
                run_id=run_id,
                entry=entry,
                user_query=user_response,
                user_context=dict(entry.get("user_context") or {}),
                workspace_id=workspace_id,
                session_id=session_id,
                user_id=current_user.username if current_user else "anonymous",
                after_seq=after_seq,
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

    manager, call_async = _get_async_runtime_bridge()
    try:
        scope = RunScope(
            username=str(entry.get("user_id") or "anonymous"),
            workspace_id=str(entry.get("workspace_id") or ""),
            scopes=("agent:read", "agent:cancel"),
        )
        snapshot = call_async(manager.get_run(run_id, scope))
        if snapshot is not None:
            snapshot = call_async(
                manager.cancel_run(run_id, scope, reason="acp_cancel")
            )
        elif entry.get("runtime_started"):
            return jsonify({"detail": "Run state is unavailable"}), 503
        with _ACP_REGISTRY_LOCK:
            entry["status"] = RunStatus.CANCELLED
            entry["updated_at"] = datetime.now(timezone.utc)
        _persist_to_runstore(run_id, entry)
        return jsonify({
            "run_id": run_id,
            "status": snapshot.status.value if snapshot is not None else "cancelled",
        })
    except Exception:
        logger.exception("[ACP] Async cancel failed: %s", run_id)
        return jsonify({"detail": "Run cancellation failed"}), 409


@acp_bp.route("/runs", methods=["GET"])
@require_acp_auth()
def list_runs():
    """列出当前用户的 ACP 运行记录。"""
    current_user = get_current_user()
    current_username = current_user.username if current_user else None
    request_workspace_id = _resolve_acp_request_workspace_id()
    username = str(current_username or "anonymous")
    try:
        manager, call_async = _get_async_runtime_bridge()
        snapshots = call_async(manager.list_runs(RunScope(
            username=username,
            workspace_id=request_workspace_id,
            scopes=("agent:read",),
        )))
    except Exception:
        logger.exception("[ACP] Async run listing failed")
        return jsonify({"detail": "Run listing failed"}), 503
    snapshots_by_id = {snapshot.run_id: snapshot for snapshot in snapshots}
    runs = []

    with _ACP_REGISTRY_LOCK:
        registry_snapshot = list(_ACP_RUN_REGISTRY.items())
    for run_id, entry in registry_snapshot:
        entry_user_id = str(entry.get("user_id") or "").strip()
        if current_username and entry_user_id != current_username:
            continue
        if current_username is None and entry_user_id:
            continue
        workspace_id = str(entry.get("workspace_id") or "").strip()
        if workspace_id != request_workspace_id or not _acp_workspace_access_allowed(workspace_id):
            continue
        snapshot = snapshots_by_id.get(run_id)
        if snapshot is not None:
            runs.append({
                "run_id": run_id,
                "status": snapshot.status.value,
                "created_at": snapshot.created_at,
                "updated_at": snapshot.updated_at,
            })
        elif not entry.get("runtime_started"):
            runs.append({
                "run_id": run_id,
                "status": _entry_status(entry).value,
                "created_at": entry.get("created_at", datetime.now(timezone.utc)).isoformat(),
                "updated_at": entry.get("updated_at", datetime.now(timezone.utc)).isoformat(),
            })

    return jsonify({"runs": runs})
