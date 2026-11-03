"""统一错误体（API 契约 1.5、第 2 章）：所有非 2xx 都是 {code, message, details, trace_id}。

业务代码沿用各自的异常类型（WorkspaceError、SessionError……），在这里统一翻成契约错误码；
新代码直接抛 ApiError。旧路径兼容期额外带 error 字段，由兼容中间件补（compat.py）。
"""

from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..governance import context as _context

_log = logging.getLogger(__name__)

# 本次请求的链路 ID：trace 中间件写入，错误体、审计与日志读取（公共标签之一，见 governance.context）
trace_id_var = _context.trace_id_var


class ApiError(Exception):
    """契约错误：code 取第 2 章的码，details 是该码约定的结构化补充。"""

    def __init__(self, code: str, message: str, status: int = 400, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details


def new_trace_id() -> str:
    return _context.new_trace_id()


def body(code: str, message: str, details: dict | None = None) -> dict:
    return {"code": code, "message": message, "details": details, "trace_id": trace_id_var.get() or None}


def response(code: str, message: str, status: int, details: dict | None = None, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content=body(code, message, details), headers=headers)


# ---- 旧异常 → 契约错误码 ----

# 各模块历史上用的错误码 → 契约码（第 2 章旧码说明）
_RENAMED_CODES = {
    "project_metadata_unavailable": ("metadata_unavailable", {"which": "project"}),
    "resource_metadata_unavailable": ("metadata_unavailable", {"which": "resource"}),
    "solver_contract_revision_conflict": ("revision_conflict", {"kind": "solver_contract"}),
}

# 没带具体码（workspace_error）的工作区错误按状态码归类
_WORKSPACE_STATUS_CODES = {
    400: "invalid_argument",
    403: "path_forbidden",
    404: "not_found",
    409: "revision_conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    500: "internal_error",
    503: "service_unavailable",
}
_SESSION_STATUS_CODES = {
    400: "message_empty",
    403: "forbidden",
    404: "conversation_not_found",
    409: "session_busy",
    410: "permission_request_gone",
    503: "engine_not_installed",
}
_CONVERSATION_STATUS_CODES = {400: "title_invalid", 404: "conversation_not_found", 409: "session_busy"}
_AUTH_STATUS_CODES = {
    400: "invalid_argument",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    409: "username_taken",
    502: "upstream_unavailable",
    503: "user_admin_unavailable",
}
_GENERIC_STATUS_CODES = {
    400: "invalid_argument",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "not_found",
    409: "revision_conflict",
    410: "endpoint_removed",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "invalid_argument",
    429: "rate_limited",
    502: "upstream_unavailable",
    503: "service_unavailable",
}


def _status_code(status: int, table: dict) -> str:
    return table.get(status) or _GENERIC_STATUS_CODES.get(status) or ("internal_error" if status >= 500 else "invalid_argument")


def translate(exc: Exception) -> tuple[int, str, str, dict | None]:
    """已知异常 → (HTTP 状态, 契约码, 消息, details)；未知异常抛回给调用方。"""
    from ..services.agent_sessions import SessionError
    from ..services.auth import AuthError
    from ..services.conversation_store import ConversationError
    from ..services.onto_mcp.errors import McpError
    from ..services.personal_model_settings import PersonalConfigError
    from ..services.studio import StudioError
    from ..services.workspace import WorkspaceError

    message = str(exc)
    status = int(getattr(exc, "status", 400) or 400)
    if isinstance(exc, ApiError):
        return exc.status, exc.code, message, exc.details
    if isinstance(exc, WorkspaceError):
        details = dict(getattr(exc, "details", None) or {})
        conflict = getattr(exc, "conflict", None)
        if conflict:
            details["conflict"] = conflict  # 同名冲突：候选名（2.3 project_name_conflict）
        code = exc.code
        if code in _RENAMED_CODES:
            code, extra = _RENAMED_CODES[code]
            details = {**extra, **details}
        elif code in ("workspace_error", "", None):
            code = _status_code(status, _WORKSPACE_STATUS_CODES)
        return status, code, message, details or None
    if isinstance(exc, SessionError):
        code = getattr(exc, "code", "") or _status_code(status, _SESSION_STATUS_CODES)
        return status, code, message, getattr(exc, "details", None)
    if isinstance(exc, ConversationError):
        return status, _status_code(status, _CONVERSATION_STATUS_CODES), message, None
    if isinstance(exc, AuthError):
        code = getattr(exc, "code", "") or _status_code(status, _AUTH_STATUS_CODES)
        return status, code, message, getattr(exc, "details", None)
    if isinstance(exc, McpError):
        # 项目级 MCP 管理接口（M-01～M-04）走契约错误体；传输层自己回 JSON-RPC，不经这里
        return status, _status_code(status, {}), message, exc.details or None
    if isinstance(exc, (StudioError, PersonalConfigError)):
        return status, _status_code(status, {}), message, None
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        # 契约接口的异常（服务令牌、回执、反馈）已经用契约码
        return status, code, message, getattr(exc, "details", None)
    raise exc


def _known_exception_types() -> tuple[type, ...]:
    from ..services.agent_sessions import SessionError
    from ..services.auth import AuthError
    from ..services.conversation_store import ConversationError
    from ..services.feedback_inbox import FeedbackRejected
    from ..services.onto_mcp.errors import McpError
    from ..services.personal_model_settings import PersonalConfigError
    from ..services.service_tokens import ServiceTokenError
    from ..services.solver_validation import ReceiptRejected
    from ..services.studio import StudioError
    from ..services.workspace import WorkspaceError

    return (
        ApiError, WorkspaceError, SessionError, ConversationError, AuthError, McpError,
        StudioError, PersonalConfigError, ServiceTokenError, ReceiptRejected, FeedbackRejected,
    )


def _project_of(request: Request) -> str:
    return str((request.path_params or {}).get("project_id") or "")


async def _known_handler(request: Request, exc: Exception) -> JSONResponse:
    status, code, message, details = translate(exc)
    if status >= 500 and code == "internal_error":
        return _internal(request, exc, message)
    return response(code, message, status, details)


def _internal(request: Request, exc: Exception, raw: str = "") -> JSONResponse:
    """未预期错误：原始信息只进错误留档，用户只看到错误编号（2.1 internal_error）。"""
    from ..services import error_log

    error_id = f"err-{uuid.uuid4().hex[:6]}"
    error_log.log(_project_of(request), "kernel", f"[{error_id}] {type(exc).__name__}: {raw or exc}")
    _log.exception("未预期错误 %s %s %s", error_id, request.method, request.url.path, exc_info=exc)
    return response("internal_error", "服务内部错误，请稍后重试", 500, {"error_id": error_id})


async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    fields = {}
    for item in exc.errors():
        loc = [str(part) for part in item.get("loc", ()) if part not in ("body", "query", "path")]
        fields[".".join(loc) or "body"] = item.get("msg", "invalid")
    return response("invalid_argument", "请求参数不合法", 400, {"fields": fields})


async def _http_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = _status_code(exc.status_code, {})
    message = exc.detail if isinstance(exc.detail, str) and exc.status_code != 404 else (
        "接口不存在" if exc.status_code in (404, 405) else str(exc.detail)
    )
    return response(code, message, exc.status_code, None, getattr(exc, "headers", None))


async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    return _internal(request, exc)


def install(app: FastAPI) -> None:
    for exc_type in _known_exception_types():
        app.add_exception_handler(exc_type, _known_handler)
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_exception_handler(StarletteHTTPException, _http_handler)
    app.add_exception_handler(Exception, _unhandled_handler)
