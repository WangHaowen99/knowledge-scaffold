"""服务令牌接口（API 契约 3.20、3.21；ADR-0020，包出与回执部分由 ADR-0098、反馈入由 ADR-0092 取代）。

外部服务持服务令牌调用，不经登录。正式版本由知识包 Hub 分发、求解验证在评测服务里做（ADR-0098），
X-01 跨项目发布列表、求解验证候选包、X-04 验证回执都已退役；反馈入 X-05 没有调用方，也已退役。
剩下 PB-05 发布记录、PB-06 包清单、PB-07 下载接受服务令牌，在 routers/publish.py；这里只留鉴权中间件用的判定。
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Request

from ..services import service_tokens

router = APIRouter(prefix="/api/v1", tags=["contracts"])

_PROJECT = r"/api/v1/projects/[^/]+"
# 所有者或服务令牌都能读：带服务令牌时不走登录校验，带登录 token 时照常校验
_OWNER_OR_SERVICE = (
    ("GET", re.compile(rf"^{_PROJECT}/releases/[^/]+(?:/manifest|/download)?$")),
)


def bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


def skips_login(method: str, path: str, token: str) -> bool:
    """鉴权中间件用：服务令牌接口由接口自己验令牌，不做登录校验（API 契约 1.2 第 6 条）。"""
    return service_tokens.looks_like_service_token(token) and any(
        method == verb and pattern.match(path) for verb, pattern in _OWNER_OR_SERVICE
    )
