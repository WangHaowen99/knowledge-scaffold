"""个人（API 契约 3.2 A-06、A-07、A-09～A-11）：修改密码、我的资料、我的模型配置。

用户一律取令牌里的用户名，请求体里的用户名不认。偏好、通知、用户事件流（A-08、A-12～A-14）
属于以后阶段。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from ..api import deps
from ..api.errors import ApiError
from ..services import auth, personal_model_settings

router = APIRouter(prefix="/api/v1/me", tags=["me"])


def _username(request: Request) -> str:
    return deps.username(request)


# ---- A-06 修改密码 ----


class PasswordBody(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=1, max_length=128)


@router.put("/password", status_code=204)
def change_password(body: PasswordBody, request: Request):
    """A-06：先用旧密码认证，再按密码规则改密（含不能与旧密码相同）。

    auth-server 改密后该用户的全部令牌作废（含当前设备），前端要重新登录。
    """
    if not auth.ENABLED:
        raise ApiError("forbidden", "本地模式没有账号密码", 403, {"reason": "local_mode"})
    from .auth_routes import change_password_upstream

    change_password_upstream(_username(request), body.old_password, body.new_password)
    return Response(status_code=204)


# ---- A-07 我的资料 ----


@router.get("")
def me(request: Request):
    """A-07：UserProfile 加最近登录、名下项目数与本月用量。"""
    from .auth_routes import last_login, user_profile
    from .ops import month_usage

    claims = deps.user(request)
    profile = user_profile(claims)
    username = profile["username"]
    owned = {
        meta["id"]
        for meta in deps.workspaces.list_projects()
        if claims is None or str(meta.get("owner") or "") == username
    }
    return {
        **profile,
        "last_login": last_login(username) if claims is not None else None,
        "project_count": len(owned),
        "usage_this_month": month_usage(owned),
    }


# ---- A-09～A-11 我的模型配置 ----


def personal_engine_view(username: str, engine: str, masked: dict | None = None) -> dict:
    """PersonalEngineConfig（契约 A-09）：当前生效的来源、管理员配置摘要、个人配置（Key 不回显）。"""
    from .settings import admin_summary, effective_context

    masked = masked or personal_model_settings.masked(username)
    mine = masked[engine]
    summary = admin_summary(engine)
    configured = summary.pop("configured")
    source = "personal" if mine.get("active") else "admin" if configured else "deployment"
    return {
        "source": source,
        "admin_summary": summary,
        "base_url": mine.get("base_url", ""),
        "model": mine.get("model", ""),
        "api_key_set": bool(mine.get("api_key_masked")),
        # 个人 Key 只换网关、Key 与模型，上下文参数仍按管理员配置生效
        "context": effective_context(engine),
    }


@router.get("/model-settings")
def get_my_model_settings(request: Request):
    """A-09：我的模型配置。管理员关闭个人 Key 或本地模式时 personal_allowed=false，页面锁定。"""
    username = _username(request)
    masked = personal_model_settings.masked(username)
    return {
        "personal_allowed": bool(masked.get("enabled")),
        "engines": {
            engine: personal_engine_view(username, engine, masked)
            for engine in personal_model_settings.ENGINES
        },
    }


class MyModelBody(BaseModel):
    source: Literal["deployment", "admin", "personal"]
    api_key: str | None = None  # 留空表示不改
    base_url: str | None = None  # 不传表示沿用已存的个人网关地址
    model: str | None = None


def _check_engine(engine: str) -> None:
    if engine not in personal_model_settings.ENGINES:
        raise ApiError("invalid_argument", "未知引擎", 400, {"fields": {"engine": "enum"}})


@router.put("/model-settings/{engine}")
def put_my_model_settings(engine: str, body: MyModelBody, request: Request):
    """A-10：切换来源。personal 保存个人 Key（以前没设过时必填）；admin、deployment 回到管理员配置。"""
    _check_engine(engine)
    username = _username(request)
    if body.source == "personal":
        mine = personal_model_settings.masked(username)[engine]
        personal_model_settings.save(
            username,
            engine,
            mine.get("base_url", "") if body.base_url is None else body.base_url,
            body.api_key or "",
            mine.get("model", "") if body.model is None else body.model,
        )
    else:
        personal_model_settings.clear(username, engine)
    return personal_engine_view(username, engine)


@router.delete("/model-settings/{engine}")
def delete_my_model_settings(engine: str, request: Request):
    """A-11：清除个人配置，回到管理员配置。"""
    _check_engine(engine)
    username = _username(request)
    personal_model_settings.clear(username, engine)
    return personal_engine_view(username, engine)
