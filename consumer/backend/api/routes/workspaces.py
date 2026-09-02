"""Workspace management endpoints."""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from config.system_config import SystemConfigValidationError
from config.workspace_llm_profiles import (
    get_public_workspace_llm_profiles,
    test_workspace_llm_profile_connection,
    update_workspace_llm_profiles,
)
from integration.observability import reset_observer
from integration.observability.workspace_config import (
    WorkspaceObservabilityMigrationRequiredError,
    get_public_workspace_observability_config,
    test_workspace_observability_connection,
    update_workspace_observability_config,
)
from api.auth.auth import (
    AuthUser,
    get_auth_manager,
    require_admin_user,
    require_authenticated_user,
)
from api.auth.service_auth import modeling_service_authorized
from api.auth.user_deletion import (
    UserDeletionPendingError,
    UserDeletionReconciliationError,
)
from api.dependencies import (
    get_runtime_container,
    require_workspace_id,
    require_workspace_path_access,
    require_workspace_path_owner_or_admin,
    run_in_runtime_pool,
)
from agent.runtime.errors import WorkspaceBusyError
from api.exceptions import APIError
from api.request_parsing import read_optional_json_object
from api.workspace_acl_service import (
    WorkspaceIdentityDirectoryUnavailableError,
    WorkspaceIdentityNotFoundError,
    create_workspace_with_validated_owner,
    ensure_user_personal_workspace,
    replace_workspace_members,
    update_workspace_authorized_with_validated_owner,
    update_workspace_owner_with_validation,
    validate_workspace_member,
)
from workspace.workspaces import (
    WorkspaceAuthorizationError,
    delete_workspace,
    get_default_workspace_id,
    get_workspace,
    get_workspace_runtime_config,
    set_default_workspace,
    update_workspace_authorized,
    update_workspace_runtime_config,
    user_can_access_workspace,
)
from api.routes.errors import not_found_response


workspaces_router = APIRouter(prefix="/agentic")

_MODELING_PROJECT_ID_PATTERN = re.compile(r"^pj_[A-Za-z0-9_-]{1,48}$")
_WORKSPACE_IDENTITY_ERRORS = (
    WorkspaceIdentityNotFoundError,
    WorkspaceIdentityDirectoryUnavailableError,
    UserDeletionPendingError,
    UserDeletionReconciliationError,
)


def _workspace_forbidden():
    return JSONResponse(
        {"error": "Forbidden", "code": "WORKSPACE_ACCESS_DENIED"},
        status_code=403,
    )


async def _request_json_object(request: Request) -> dict:
    """Preserve the workspace API's optional-object body contract."""
    content_type = str(request.headers.get("content-type") or "").lower()
    if not (
        content_type.startswith("application/json")
        or "+json" in content_type
    ):
        return {}
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise APIError(400, {"detail": "请求体必须是 JSON object"})
    return payload


def _workspace_identity_error(exc):
    if isinstance(exc, WorkspaceIdentityNotFoundError):
        payload = {"detail": exc.detail, "code": exc.code}
        if exc.field == "owner":
            payload["username"] = exc.usernames[0] if exc.usernames else ""
        else:
            payload["usernames"] = list(exc.usernames)
            if len(exc.usernames) == 1:
                payload["username"] = exc.usernames[0]
        return JSONResponse(payload, status_code=400)
    if isinstance(exc, UserDeletionPendingError):
        return JSONResponse(
            {
                "detail": (
                    "用户 {} 正在删除或等待清理，暂时不能写入工作空间权限".format(
                        exc.username,
                    )
                ),
                "code": "USER_DELETION_RECONCILIATION_PENDING",
                "username": exc.username,
                "reconciliation_id": exc.reconciliation_id,
            },
            status_code=409,
        )
    if isinstance(exc, UserDeletionReconciliationError):
        return JSONResponse(
            {
                "error": "用户删除状态暂不可用，请稍后再试",
                "code": "USER_DELETION_STATE_UNAVAILABLE",
            },
            status_code=503,
        )
    return JSONResponse(
        {
            "detail": WorkspaceIdentityDirectoryUnavailableError.detail,
            "code": WorkspaceIdentityDirectoryUnavailableError.code,
        },
        status_code=503,
    )


def _shadow_workspace_id(project_id: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9-]", "-", project_id).lower()
    return "mw-shadow-{}".format(normalized)[:60]


def _relationship_role(user, workspace: dict):
    username = getattr(user, "username", None)
    if not username:
        return None
    if workspace.get("owner") == username:
        return "admin"
    if username in (workspace.get("members") or []):
        return "member"
    return None


def _visible_workspaces(auth_manager, user: AuthUser) -> dict:
    personal_workspace, available_workspaces = ensure_user_personal_workspace(
        auth_manager,
        user,
    )
    is_admin = getattr(user, "role", None) == "admin"
    items = []
    for item in available_workspaces:
        role = _relationship_role(user, item)
        if not is_admin and role is None:
            continue
        if not user_can_access_workspace(user, item, bool(item.get("is_default"))):
            continue
        item["role"] = role
        items.append(item)
    default_workspace_id = get_default_workspace_id()
    return {
        "workspaces": items,
        "total": len(items),
        "default_workspace_id": default_workspace_id,
        "preferred_workspace_id": (
            default_workspace_id if is_admin else personal_workspace["id"]
        ),
    }


@workspaces_router.post("/internal/modeling/shadow-workspaces/ensure")
async def ensure_modeling_shadow_workspace_api(request: Request):
    if not modeling_service_authorized(request):
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    payload = await _request_json_object(request)
    project_id = str(payload.get("project_id") or "").strip()
    owner = str(payload.get("owner") or "").strip()
    if not _MODELING_PROJECT_ID_PATTERN.fullmatch(project_id):
        return JSONResponse({"detail": "project_id 不合法"}, status_code=400)
    if not owner:
        return JSONResponse({"detail": "owner 不能为空"}, status_code=400)

    auth_manager = get_auth_manager(request)
    workspace_id = _shadow_workspace_id(project_id)
    try:
        current = await run_in_runtime_pool(
            request,
            "short_io",
            get_workspace,
            workspace_id,
        )
    except KeyError:
        try:
            created = await run_in_runtime_pool(
                request,
                "short_io",
                create_workspace_with_validated_owner,
                auth_manager,
                name="建模影子·{}".format(project_id),
                description="知识包建模工具评测专用，自动管理",
                workspace_id=workspace_id,
                owner=owner,
                enforce_workspace_limit=False,
            )
        except _WORKSPACE_IDENTITY_ERRORS as exc:
            return _workspace_identity_error(exc)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse(
            {"created": True, "workspace": created},
            status_code=201,
        )
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    current_owner = current.get("owner")
    if current_owner not in (None, owner):
        return JSONResponse(
            {"detail": "影子工作区已属于其他用户"},
            status_code=409,
        )
    if current_owner is None:
        try:
            current = await run_in_runtime_pool(
                request,
                "short_io",
                update_workspace_owner_with_validation,
                auth_manager,
                workspace_id,
                owner,
            )
        except _WORKSPACE_IDENTITY_ERRORS as exc:
            return _workspace_identity_error(exc)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
    return {"created": False, "workspace": current}


@workspaces_router.get("/workspaces")
async def list_workspaces_api(
    request: Request,
    user: AuthUser = Depends(require_authenticated_user),
):
    try:
        return await run_in_runtime_pool(
            request,
            "short_io",
            _visible_workspaces,
            get_auth_manager(request),
            user,
        )
    except _WORKSPACE_IDENTITY_ERRORS as exc:
        return _workspace_identity_error(exc)
    except ValueError as exc:
        return JSONResponse(
            {
                "detail": str(exc),
                "code": "PERSONAL_WORKSPACE_PROVISIONING_FAILED",
            },
            status_code=409,
        )


@workspaces_router.post("/workspaces")
async def create_workspace_api(
    request: Request,
    user: AuthUser = Depends(require_authenticated_user),
):
    payload = await _request_json_object(request)
    is_admin = getattr(user, "role", None) == "admin"
    owner = payload.get("owner") if is_admin and "owner" in payload else user.username
    set_as_default = bool(payload.get("set_as_default")) if is_admin else False
    try:
        created = await run_in_runtime_pool(
            request,
            "short_io",
            create_workspace_with_validated_owner,
            get_auth_manager(request),
            name=payload.get("name", ""),
            description=payload.get("description", ""),
            workspace_id=payload.get("id"),
            set_as_default=set_as_default,
            auto_set_default=is_admin,
            owner=owner,
        )
    except _WORKSPACE_IDENTITY_ERRORS as exc:
        return _workspace_identity_error(exc)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return JSONResponse(created, status_code=201)


@workspaces_router.get("/workspaces/{workspace_id}")
async def get_workspace_api(
    workspace_id: str,
    request: Request,
    user: AuthUser = Depends(require_authenticated_user),
):
    try:
        workspace = await run_in_runtime_pool(
            request,
            "short_io",
            get_workspace,
            workspace_id,
        )
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    if not user_can_access_workspace(user, workspace, bool(workspace.get("is_default"))):
        return _workspace_forbidden()
    workspace["role"] = _relationship_role(user, workspace)
    return workspace


@workspaces_router.patch("/workspaces/{workspace_id}")
async def update_workspace_api(
    workspace_id: str,
    request: Request,
    user: AuthUser = Depends(require_authenticated_user),
):
    payload = await _request_json_object(request)
    fields = {"name": payload.get("name"), "description": payload.get("description")}
    if "owner" in payload:
        fields["owner"] = payload["owner"]
    try:
        if "owner" in payload:
            if getattr(user, "role", None) != "admin":
                return _workspace_forbidden()
            owner = fields.pop("owner")
            updated = await run_in_runtime_pool(
                request,
                "short_io",
                update_workspace_authorized_with_validated_owner,
                get_auth_manager(request),
                workspace_id,
                user,
                owner=owner,
                **fields,
            )
        else:
            updated = await run_in_runtime_pool(
                request,
                "short_io",
                update_workspace_authorized,
                workspace_id,
                user,
                name=fields["name"],
                description=fields["description"],
            )
    except _WORKSPACE_IDENTITY_ERRORS as exc:
        return _workspace_identity_error(exc)
    except WorkspaceAuthorizationError:
        return _workspace_forbidden()
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return updated


@workspaces_router.put("/workspaces/{workspace_id}/members")
async def update_workspace_members_api(
    workspace_id: str,
    request: Request,
    user: AuthUser = Depends(require_authenticated_user),
    _authorized_workspace_id: str = Depends(
        require_workspace_path_owner_or_admin,
    ),
):
    payload = await _request_json_object(request)
    members = payload.get("members")
    if members is None:
        return JSONResponse(
            {"detail": "members 必须是字符串数组"},
            status_code=400,
        )
    try:
        return await run_in_runtime_pool(
            request,
            "short_io",
            replace_workspace_members,
            get_auth_manager(request),
            workspace_id,
            user,
            members,
        )
    except _WORKSPACE_IDENTITY_ERRORS as exc:
        return _workspace_identity_error(exc)
    except WorkspaceAuthorizationError:
        return _workspace_forbidden()
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)


@workspaces_router.post("/workspaces/{workspace_id}/members/validate")
async def validate_workspace_member_api(
    workspace_id: str,
    request: Request,
    _user: AuthUser = Depends(require_authenticated_user),
    _authorized_workspace_id: str = Depends(
        require_workspace_path_owner_or_admin,
    ),
):
    payload = await _request_json_object(request)
    try:
        username = await run_in_runtime_pool(
            request,
            "short_io",
            validate_workspace_member,
            get_auth_manager(request),
            payload.get("username"),
        )
        return {"valid": True, "username": username}
    except _WORKSPACE_IDENTITY_ERRORS as exc:
        return _workspace_identity_error(exc)
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)


@workspaces_router.delete("/workspaces/{workspace_id}")
async def delete_workspace_api(
    workspace_id: str,
    request: Request,
    user: AuthUser = Depends(require_authenticated_user),
    _authorized_workspace_id: str = Depends(
        require_workspace_path_owner_or_admin,
    ),
):
    is_admin = getattr(user, "role", None) == "admin"
    runtime = get_runtime_container(request)
    try:
        async with runtime.manager.workspace_deletion_guard(workspace_id):
            await run_in_runtime_pool(
                request,
                "short_io",
                delete_workspace,
                workspace_id,
                allow_default=not is_admin,
            )
    except WorkspaceBusyError as exc:
        return JSONResponse(
            {
                "error": "工作空间存在运行中任务，请等待完成或显式停止后再删除",
                "error_code": "WORKSPACE_RUN_ACTIVE",
                "workspace_id": workspace_id,
                "run_id": exc.run_id,
                "session_id": exc.session_id,
                "run_status": exc.status,
            },
            status_code=409,
        )
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return {"success": True, "id": workspace_id}


@workspaces_router.post(
    "/workspaces/{workspace_id}/default",
    dependencies=[Depends(require_admin_user)],
)
async def set_default_workspace_api(workspace_id: str, request: Request):
    try:
        updated = await run_in_runtime_pool(
            request,
            "short_io",
            set_default_workspace,
            workspace_id,
        )
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return {
        "workspace": updated,
        "default_workspace_id": updated["id"],
    }


@workspaces_router.get("/workspaces/current/runtime-config")
async def get_current_runtime_config_api(
    request: Request,
    workspace_id: str = Depends(require_workspace_id),
):
    try:
        return await run_in_runtime_pool(
            request,
            "short_io",
            get_workspace_runtime_config,
            workspace_id,
        )
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)


@workspaces_router.get("/workspaces/{workspace_id}/runtime-config")
async def get_runtime_config_api(
    workspace_id: str,
    request: Request,
    _authorized_workspace_id: str = Depends(require_workspace_path_access),
):
    try:
        return await run_in_runtime_pool(
            request,
            "short_io",
            get_workspace_runtime_config,
            workspace_id,
        )
    except KeyError as exc:
        return not_found_response(exc)


@workspaces_router.patch(
    "/workspaces/{workspace_id}/runtime-config",
    dependencies=[Depends(require_admin_user)],
)
async def update_runtime_config_api(workspace_id: str, request: Request):
    payload = await read_optional_json_object(request)
    if not payload:
        return JSONResponse({"detail": "请求体不能为空"}, status_code=400)
    try:
        updated = await run_in_runtime_pool(
            request,
            "short_io",
            update_workspace_runtime_config,
            workspace_id,
            payload,
        )
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return updated


@workspaces_router.get("/workspaces/{workspace_id}/observability-config")
async def get_observability_config_api(
    workspace_id: str,
    request: Request,
    _authorized_workspace_id: str = Depends(require_workspace_path_access),
):
    try:
        return await run_in_runtime_pool(
            request,
            "short_io",
            get_public_workspace_observability_config,
            workspace_id,
        )
    except KeyError as exc:
        return not_found_response(exc)
    except WorkspaceObservabilityMigrationRequiredError as exc:
        return JSONResponse(exc.to_dict(), status_code=409)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)


def _update_observability_config(workspace_id: str, payload: dict):
    update_workspace_observability_config(workspace_id, payload)
    reset_observer(timeout=1.0)
    return get_public_workspace_observability_config(workspace_id)


@workspaces_router.patch(
    "/workspaces/{workspace_id}/observability-config",
    dependencies=[Depends(require_admin_user)],
)
async def update_observability_config_api(workspace_id: str, request: Request):
    payload = await read_optional_json_object(request)
    if not payload:
        return JSONResponse({"detail": "请求体不能为空"}, status_code=400)
    try:
        return await run_in_runtime_pool(
            request,
            "short_io",
            _update_observability_config,
            workspace_id,
            payload,
        )
    except KeyError as exc:
        return not_found_response(exc)
    except WorkspaceObservabilityMigrationRequiredError as exc:
        return JSONResponse(exc.to_dict(), status_code=409)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)


@workspaces_router.post(
    "/workspaces/{workspace_id}/observability-config/test",
    dependencies=[Depends(require_admin_user)],
)
async def test_observability_config_api(workspace_id: str, request: Request):
    payload = await read_optional_json_object(request)
    try:
        return await run_in_runtime_pool(
            request,
            "external_io",
            test_workspace_observability_connection,
            workspace_id,
            payload,
        )
    except KeyError as exc:
        return not_found_response(exc)
    except WorkspaceObservabilityMigrationRequiredError as exc:
        return JSONResponse(exc.to_dict(), status_code=409)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)


@workspaces_router.get("/workspaces/{workspace_id}/llm-profiles")
async def get_workspace_llm_profiles_api(
    workspace_id: str,
    request: Request,
    _authorized_workspace_id: str = Depends(require_workspace_path_access),
):
    try:
        profiles = await run_in_runtime_pool(
            request,
            "short_io",
            get_public_workspace_llm_profiles,
            workspace_id,
        )
        return {"profiles": profiles}
    except KeyError as exc:
        return not_found_response(exc)


@workspaces_router.patch(
    "/workspaces/{workspace_id}/llm-profiles",
    dependencies=[Depends(require_admin_user)],
)
async def update_workspace_llm_profiles_api(workspace_id: str, request: Request):
    payload = await read_optional_json_object(request)
    try:
        return await run_in_runtime_pool(
            request,
            "short_io",
            update_workspace_llm_profiles,
            workspace_id,
            payload,
        )
    except KeyError as exc:
        return not_found_response(exc)
    except SystemConfigValidationError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)


@workspaces_router.post(
    "/workspaces/{workspace_id}/llm-profiles/test",
    dependencies=[Depends(require_admin_user)],
)
async def test_workspace_llm_profiles_api(workspace_id: str, request: Request):
    payload = await read_optional_json_object(request)
    try:
        return await run_in_runtime_pool(
            request,
            "external_io",
            test_workspace_llm_profile_connection,
            workspace_id,
            payload,
        )
    except KeyError as exc:
        return not_found_response(exc)
    except SystemConfigValidationError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
