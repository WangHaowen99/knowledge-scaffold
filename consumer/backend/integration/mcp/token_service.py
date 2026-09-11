"""MCP token creation, listing, revocation and verification."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import MCPRuntimeConfig, load_mcp_runtime_config, validate_mcp_runtime_config
from .token_crypto import decode_jwt, encode_jwt, hash_token, mask_token
from .token_store import MCPTokenStore, utc_now_iso

DEFAULT_SCOPES = ("mcp:search", "mcp:chat")
ALLOWED_SCOPES = set(DEFAULT_SCOPES)


class MCPTokenServiceError(ValueError):
    """Raised when MCP token service input is invalid."""


class MCPTokenAuthError(ValueError):
    """Raised when an MCP token cannot be authenticated."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


def normalize_scopes(raw: Any) -> list[str]:
    if raw in (None, ""):
        return list(DEFAULT_SCOPES)
    if isinstance(raw, str):
        values = raw.split()
    elif isinstance(raw, list):
        values = [str(item or "").strip() for item in raw]
    else:
        raise MCPTokenServiceError("scopes 必须是数组或空格分隔字符串")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        scope = str(value or "").strip()
        if not scope:
            continue
        if scope not in ALLOWED_SCOPES:
            raise MCPTokenServiceError("不支持的 scope: {}".format(scope))
        if scope not in seen:
            seen.add(scope)
            result.append(scope)
    if not result:
        raise MCPTokenServiceError("scopes 不能为空")
    return result


def create_mcp_token(
    *,
    username: str,
    workspace_id: str,
    name: str = "",
    scopes: Any = None,
    expires_in: Any = None,
    ontology_id: str = "",
    config: MCPRuntimeConfig | None = None,
    store: MCPTokenStore | None = None,
) -> dict[str, Any]:
    cfg = config or load_mcp_runtime_config()
    _validate_config(cfg)
    normalized_username = str(username or "").strip()
    normalized_workspace_id = str(workspace_id or "").strip()
    if not normalized_username:
        raise MCPTokenServiceError("username 不能为空")
    if not normalized_workspace_id:
        raise MCPTokenServiceError("workspace_id 不能为空")
    resolved_scopes = normalize_scopes(scopes)
    normalized_ontology_id = str(ontology_id or "").strip()
    if normalized_ontology_id:
        from .ontology_binding import validate_token_ontology_binding

        validate_token_ontology_binding(
            workspace_id=normalized_workspace_id,
            ontology_id=normalized_ontology_id,
            scopes=resolved_scopes,
        )
    ttl = _normalize_expires_in(expires_in, cfg)
    now_ts = int(time.time())
    jti = uuid.uuid4().hex
    payload: dict[str, Any] = {
        "iss": cfg.token_issuer,
        "aud": cfg.token_audience,
        "sub": normalized_username,
        "workspace_id": normalized_workspace_id,
        "scope": " ".join(resolved_scopes),
        "jti": jti,
        "iat": now_ts,
    }
    if normalized_ontology_id:
        payload["ontology_id"] = normalized_ontology_id
    expires_at = None
    if ttl is not None:
        payload["exp"] = now_ts + ttl
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl)).replace(microsecond=0).isoformat()
    jwt_token = encode_jwt(payload, secret=cfg.token_secret, algorithm=cfg.token_algorithm)
    token = "mcp_{}".format(jwt_token)
    item = {
        "token_id": "mcp_{}".format(uuid.uuid4().hex),
        "jti": jti,
        "token_hash": hash_token(token),
        "token_masked": mask_token(token),
        "username": payload["sub"],
        "workspace_id": payload["workspace_id"],
        "ontology_id": normalized_ontology_id,
        "scopes": resolved_scopes,
        "status": "active",
        "created_at": utc_now_iso(),
        "expires_at": expires_at,
        "revoked_at": None,
        "last_used_at": None,
        "name": str(name or "").strip(),
    }
    public_item = (store or MCPTokenStore(cfg.token_store_path)).create_token(item)
    return {"token": token, "item": public_item, "algorithm": cfg.token_algorithm}


def list_mcp_tokens(*, username: str, workspace_id: str, store: MCPTokenStore | None = None) -> list[dict[str, Any]]:
    return (store or MCPTokenStore()).list_tokens(username=username, workspace_id=workspace_id)


def revoke_mcp_token(*, username: str, workspace_id: str, token_id: str, store: MCPTokenStore | None = None) -> bool:
    return (store or MCPTokenStore()).revoke_token(username=username, workspace_id=workspace_id, token_id=token_id)


def revoke_all_mcp_tokens_for_user(
    *,
    username: str,
    store: MCPTokenStore | None = None,
) -> int:
    """Revoke all MCP credentials for a deleted identity."""
    return (store or MCPTokenStore()).revoke_user_tokens(username=username)


def verify_mcp_token(
    token: str, *, required_scope: str | None = None, config: MCPRuntimeConfig | None = None
) -> dict[str, Any]:
    cfg = config or load_mcp_runtime_config()
    try:
        validate_mcp_runtime_config(cfg)
    except ValueError as exc:
        raise MCPTokenAuthError("MCP_CONFIG_INVALID", str(exc)) from exc
    try:
        payload = decode_jwt(
            token,
            secret=cfg.token_secret,
            algorithm=cfg.token_algorithm,
            issuer=cfg.token_issuer,
            audience=cfg.token_audience,
        )
    except Exception as exc:
        raise MCPTokenAuthError("INVALID_MCP_TOKEN", str(exc)) from exc
    username = str(payload.get("sub") or "").strip()
    workspace_id = str(payload.get("workspace_id") or "").strip()
    jti = str(payload.get("jti") or "").strip()
    if not username:
        raise MCPTokenAuthError("INVALID_MCP_TOKEN", "Token 缺少 sub")
    if not workspace_id:
        raise MCPTokenAuthError("INVALID_MCP_TOKEN", "Token 缺少 workspace_id")
    if not jti:
        raise MCPTokenAuthError("INVALID_MCP_TOKEN", "Token 缺少 jti")
    ontology_id = str(payload.get("ontology_id") or "").strip()
    scopes = normalize_scopes(payload.get("scope"))
    if required_scope and required_scope not in scopes:
        raise MCPTokenAuthError("MCP_SCOPE_FORBIDDEN", "Token 缺少 scope: {}".format(required_scope))
    ok, reason, item = MCPTokenStore(cfg.token_store_path).verify_active_token(
        jti=jti,
        token_hash=hash_token(token),
        update_last_used=True,
    )
    if not ok:
        raise MCPTokenAuthError("INVALID_MCP_TOKEN", _reason_detail(reason))
    registered_ontology_id = str((item or {}).get("ontology_id") or "").strip()
    if ontology_id != registered_ontology_id:
        raise MCPTokenAuthError(
            "MCP_TOKEN_BINDING_MISMATCH",
            "Token claim 与注册记录的知识包绑定不一致",
        )
    return {
        "username": username,
        "workspace_id": workspace_id,
        "ontology_id": ontology_id,
        "scopes": scopes,
        "jti": jti,
        "token_item": item,
    }


def _normalize_expires_in(value: Any, cfg: MCPRuntimeConfig) -> int | None:
    if value is None:
        return None
    if value == "":
        return cfg.token_ttl_seconds
    try:
        ttl = int(value)
    except Exception as exc:
        raise MCPTokenServiceError("expires_in 必须是整数或 null") from exc
    if ttl <= 0:
        raise MCPTokenServiceError("expires_in 必须大于 0，或传 null 使用默认值")
    if ttl > cfg.token_max_ttl_seconds:
        raise MCPTokenServiceError("expires_in 不能超过 {} 秒".format(cfg.token_max_ttl_seconds))
    return ttl


def _validate_config(cfg: MCPRuntimeConfig) -> None:
    try:
        validate_mcp_runtime_config(cfg)
    except ValueError as exc:
        raise MCPTokenServiceError(str(exc)) from exc


def _reason_detail(reason: str) -> str:
    return {
        "token_not_registered": "Token 未注册或已失效",
        "token_hash_mismatch": "Token 校验失败",
        "token_revoked": "Token 已吊销",
        "token_expired": "Token 已过期",
        "token_active": "Token 状态异常",
    }.get(str(reason or ""), "Token 无效: {}".format(reason))
