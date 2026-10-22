"""Modeling MCP management adapter.

Onto Modeling does not expose a second MCP service. It delegates project and
token management to the core Modeling service, which owns the 5188 MCP
transport and its JWT/scope checks.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .. import config


class ModelingMcpError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ProvisionResult:
    meta: dict
    modeling_project_id: str | None
    created: bool


def configured() -> bool:
    return bool(config.MODELING_URL)


def _request_user_token(request) -> str:
    header = request.headers.get("authorization", "")
    return header if header.lower().startswith("bearer ") else ""


def _request(
    method: str,
    path: str,
    token: str,
    body: dict | None = None,
    extra_headers: dict[str, str] | None = None,
    base_url: str | None = None,
) -> dict:
    base_url = (base_url or config.MODELING_URL).rstrip("/")
    if not base_url:
        raise ModelingMcpError("未配置 Modeling MCP 服务（ONTO_WEB_MODELING_URL）", 503)
    payload = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = token
    if body is not None:
        headers["Content-Type"] = "application/json"
    if extra_headers:
        headers.update(extra_headers)
    request = urllib.request.Request(
        f"{base_url}{path}",
        method=method,
        data=payload,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read().decode() or "{}"
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode() or "{}").get("error")
        except (OSError, ValueError):
            detail = None
        raise ModelingMcpError(str(detail or f"Modeling MCP 管理接口返回 HTTP {exc.code}"), exc.code) from exc
    except urllib.error.URLError as exc:
        raise ModelingMcpError("Modeling MCP 服务不可达", 502) from exc
    except TimeoutError as exc:
        raise ModelingMcpError("Modeling MCP 服务不可达", 502) from exc
    except OSError as exc:
        raise ModelingMcpError("Modeling MCP 服务不可达", 502) from exc
    except ValueError as exc:
        raise ModelingMcpError("Modeling MCP 服务不可达", 502) from exc


def _modeling_project_id(meta: dict) -> str:
    return str(meta.get("modeling_project_id") or "").strip()


def provision_project(meta: dict, request) -> ProvisionResult:
    """Create the matching core Modeling project once and persist its id."""
    if _modeling_project_id(meta) or not configured():
        return ProvisionResult(meta, _modeling_project_id(meta) or None, False)
    body = {
        "name": meta["name"],
        "background": meta.get("background", ""),
        "runtime": "agent",
        "external_id": f"onto:{meta['id']}",
    }
    headers = {"X-Onto-Project-Id": body["external_id"]}
    owner = str(meta.get("owner") or "").strip()
    if owner:
        body["onto_owner"] = owner
    try:
        response = _request("POST", "/api/projects", _request_user_token(request), body, extra_headers=headers)
    except ModelingMcpError as exc:
        if exc.status != 502:
            raise
        # A lost response may hide a successful create. Query the idempotency key
        # before retrying; the core unique index also makes a retry safe.
        try:
            response = _request(
                "GET",
                f"/api/projects/by-external-id?external_id={urllib.parse.quote(body['external_id'])}",
                _request_user_token(request),
                extra_headers=headers,
            )
            response["created"] = False
        except ModelingMcpError as lookup_error:
            if lookup_error.status != 404:
                raise
            response = _request("POST", "/api/projects", _request_user_token(request), body, extra_headers=headers)
    project = response.get("project") or {}
    project_id = str(project.get("id") or "").strip()
    if not project_id:
        raise ModelingMcpError("Modeling 项目创建响应缺少 project.id")
    meta["modeling_project_id"] = project_id
    return ProvisionResult(meta, project_id, bool(response.get("created", True)))


def delete_project(
    meta: dict,
    request=None,
    *,
    cleanup: bool = False,
    operation: str = "provision_rollback",
) -> dict:
    """Delete only the linked project; cleanup mode uses the service credential."""
    project_id = _modeling_project_id(meta)
    if not project_id:
        return {"deleted": False}
    external_id = f"onto:{meta['id']}"
    body = {
        "confirmed": True,
        "external_id": external_id,
        "cleanup_only": operation == "provision_rollback",
        "cleanup_operation": operation,
    }
    if association_configured() and not configured():
        return _association_request(
            "DELETE", f"/api/projects/{project_id}", body,
            str(meta.get("owner") or "").strip(), external_id=external_id,
        )
    headers = {}
    token = _request_user_token(request) if request is not None else ""
    if cleanup:
        if not config.MODELING_CLEANUP_TOKEN:
            raise ModelingMcpError("未配置 Modeling 清理凭证", 503)
        token = ""
        headers["X-Modeling-Cleanup-Token"] = config.MODELING_CLEANUP_TOKEN
    return _request("DELETE", f"/api/projects/{project_id}", token, body, headers)


def sync_members(meta: dict, request) -> dict:
    """Mirror Onto project roles into the linked Modeling project."""
    project_id = _modeling_project_id(meta)
    if not project_id:
        return {"members": []}
    if association_configured() and not configured():
        return _association_sync_members(meta)
    role_map = {"editor": "modeler", "viewer": "viewer"}
    members = [
        {"username": username, "role": role_map.get(role)}
        for username, role in (meta.get("members") or {}).items()
        if role_map.get(role) is not None
    ]
    return _request(
        "PUT",
        f"/api/projects/{project_id}/members/sync",
        _request_user_token(request),
        {"members": members},
    )


def status(meta: dict, request) -> dict:
    project_id = _modeling_project_id(meta)
    if not project_id:
        return {"configured": False, "detail": "当前 Onto 项目尚未关联 Modeling 项目"}
    return _management_request("GET", f"/api/projects/{project_id}/mcp/runtime/status", request, meta=meta)


def tokens(meta: dict, request) -> dict:
    project_id = _modeling_project_id(meta)
    if not project_id:
        return {"tokens": []}
    return _management_request("GET", f"/api/projects/{project_id}/mcp/tokens", request, meta=meta)


def issue_token(meta: dict, request, body: dict) -> dict:
    project_id = _modeling_project_id(meta)
    if not project_id:
        raise ModelingMcpError("当前 Onto 项目尚未关联 Modeling 项目", 409)
    return _management_request(
        "POST",
        f"/api/projects/{project_id}/mcp/tokens",
        request,
        body,
        meta=meta,
    )


def revoke_token(meta: dict, request, token_id: str) -> dict:
    project_id = _modeling_project_id(meta)
    if not project_id:
        raise ModelingMcpError("当前 Onto 项目尚未关联 Modeling 项目", 409)
    return _management_request(
        "DELETE",
        f"/api/projects/{project_id}/mcp/tokens/{token_id}",
        request,
        meta=meta,
    )


def association_configured() -> bool:
    return bool(config.MODELING_ASSOCIATION_URL and config.MODELING_ASSOCIATION_TOKEN)


def _association_request(
    method: str, path: str, body: dict | None = None, username: str = "",
    *, external_id: str, is_admin: bool = False
) -> dict:
    headers = {
        "X-Modeling-Service-Token": config.MODELING_ASSOCIATION_TOKEN,
        "X-Onto-Project-Id": external_id,
    }
    if username:
        headers["X-Onto-Username"] = username
    if is_admin:
        headers["X-Onto-Role"] = "admin"
    return _request(
        method,
        path,
        "",
        body,
        extra_headers=headers,
        base_url=config.MODELING_ASSOCIATION_URL,
    )


def _management_request(
    method: str, path: str, request, body: dict | None = None, *, meta: dict
) -> dict:
    """Keep legacy user-token behavior; fall back to the import service credential."""
    if configured():
        return _request(
            method, path, _request_user_token(request), body,
            extra_headers={"X-Onto-Project-Id": f"onto:{meta['id']}"},
        )
    if association_configured():
        user = getattr(getattr(request, "state", None), "user", None)
        return _association_request(
            method, path, body, str((user or {}).get("sub") or "").strip(),
            external_id=f"onto:{meta['id']}",
            is_admin=(user or {}).get("role") == "admin",
        )
    return _request(method, path, _request_user_token(request), body)


def _association_provision(meta: dict) -> ProvisionResult:
    if _modeling_project_id(meta):
        return ProvisionResult(meta, _modeling_project_id(meta), False)
    body = {
        "name": meta["name"],
        "background": meta.get("background", ""),
        "runtime": "agent",
        "external_id": f"onto:{meta['id']}",
    }
    username = str(meta.get("owner") or "").strip()
    try:
        response = _association_request("POST", "/api/projects", body, username, external_id=body["external_id"])
    except ModelingMcpError as exc:
        if exc.status != 502:
            raise
        try:
            response = _association_request(
                "GET",
                f"/api/projects/by-external-id?external_id={urllib.parse.quote(body['external_id'])}",
                username=username, external_id=body["external_id"],
            )
            response["created"] = False
        except ModelingMcpError as lookup_error:
            if lookup_error.status != 404:
                raise
            response = _association_request("POST", "/api/projects", body, username, external_id=body["external_id"])
    project = response.get("project") or {}
    project_id = str(project.get("id") or "").strip()
    if not project_id:
        raise ModelingMcpError("Modeling 项目创建响应缺少 project.id")
    meta["modeling_project_id"] = project_id
    return ProvisionResult(meta, project_id, bool(response.get("created", True)))


def _association_sync_members(meta: dict) -> dict:
    project_id = _modeling_project_id(meta)
    if not project_id:
        return {"members": []}
    role_map = {"editor": "modeler", "viewer": "viewer"}
    members = [
        {"username": username, "role": role_map.get(role)}
        for username, role in (meta.get("members") or {}).items()
        if role_map.get(role) is not None
    ]
    return _association_request(
        "PUT", f"/api/projects/{project_id}/members/sync", {"members": members},
        str(meta.get("owner") or "").strip(), external_id=f"onto:{meta['id']}",
    )


def ensure_project_association(meta: dict, request, persist) -> dict:
    """Create or recover an imported project's Modeling association."""
    if not association_configured() and not configured():
        return {
            "linked": False,
            "configured": False,
            "detail": "未配置 Modeling 导入关联服务（ONTO_WEB_MODELING_ASSOCIATION_URL/TOKEN）",
        }

    if association_configured() and not configured():
        provision = _association_provision(meta)
    else:
        provision = provision_project(meta, request)
    if not provision.modeling_project_id:
        return {
            "linked": False,
            "configured": True,
            "detail": "\u5f53\u524d Onto \u9879\u76ee\u5c1a\u672a\u5173\u8054 Modeling \u9879\u76ee",
        }

    updated = persist(meta["id"], modeling_project_id=provision.modeling_project_id)
    if association_configured() and not configured():
        _association_sync_members(updated)
    else:
        sync_members(updated, request)
    return {
        "linked": True,
        "configured": True,
        "modeling_project_id": provision.modeling_project_id,
    }
