"""证据源（契约 3.19 EV-01～EV-30）与提供方安装（PL-04、PL-19）：本期只占住路由（ADR-0056）。

全部回 `501 evidence_not_available`，错误信封按契约 1.5；`details.api` 带接口编号，方便前端与日志认。
路由先注册好，前端与以后的实现有固定落点；实现时按编号逐条换成真的处理函数。
同一前缀下固定段（`snapshots/diff`）先于路径参数（`snapshots/{snapshot_id}`）登记。

例外：EV-29 锚点解析先实现了 `file:` 锚点（上传资料里的出处，主控 11-02 定）：对话、资料、Studio 的出处
角标悬停看原文；`db`、`code` 等业务系统锚点逐项回 `resolved=false`（`reason=not_available`），整体不再 501。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..api import deps, errors
from ..api.errors import ApiError
from ..services import material_text

router = APIRouter(tags=["evidence"])

STATUS = 501
CODE = "evidence_not_available"
MESSAGE = "这一版没有开放业务系统证据源"

_P = "/api/v1/projects/{project_id}"
# (接口编号, 方法, 路径)
ROUTES = (
    ("EV-01", "GET", "/api/v1/evidence/sources"),
    ("EV-02", "POST", "/api/v1/admin/evidence/sources"),
    ("EV-03", "POST", "/api/v1/evidence/source-requests"),
    ("EV-04", "PATCH", "/api/v1/admin/evidence/sources/{source_id}"),
    ("EV-05", "DELETE", "/api/v1/admin/evidence/sources/{source_id}"),
    ("EV-06", "PUT", "/api/v1/admin/evidence/sources/{source_id}/secret"),
    ("EV-07", "DELETE", "/api/v1/admin/evidence/sources/{source_id}/secret"),
    ("EV-08", "POST", "/api/v1/evidence/sources/{source_id}/test"),
    ("EV-09", "GET", "/api/v1/evidence/sources/{source_id}/access-log"),
    ("EV-10", "GET", "/api/v1/evidence/providers"),
    ("EV-11", "GET", f"{_P}/evidence/grants"),
    ("EV-11", "PUT", f"{_P}/evidence/grants/{{source_id}}"),
    ("EV-12", "GET", f"{_P}/evidence/summary"),
    ("EV-13", "POST", f"{_P}/evidence/jobs/preview"),
    ("EV-14", "POST", f"{_P}/evidence/jobs"),
    ("EV-15", "GET", f"{_P}/evidence/jobs"),
    ("EV-16", "GET", f"{_P}/evidence/jobs/{{job_id}}"),
    ("EV-17", "POST", f"{_P}/evidence/jobs/{{job_id}}/cancel"),
    ("EV-17", "POST", f"{_P}/evidence/jobs/{{job_id}}/resume"),
    ("EV-17", "POST", f"{_P}/evidence/jobs/{{job_id}}/retry"),
    ("EV-18", "GET", f"{_P}/evidence/snapshots"),
    ("EV-21", "GET", f"{_P}/evidence/snapshots/diff"),
    ("EV-22", "POST", f"{_P}/evidence/snapshots/diff/suggestions"),
    ("EV-19", "GET", f"{_P}/evidence/snapshots/{{snapshot_id}}"),
    ("EV-20", "DELETE", f"{_P}/evidence/snapshots/{{snapshot_id}}"),
    ("EV-23", "GET", f"{_P}/evidence/summary-tree"),
    ("EV-24", "GET", f"{_P}/evidence/search"),
    ("EV-25", "GET", f"{_P}/evidence/record"),
    ("EV-26", "GET", f"{_P}/evidence/where-used"),
    ("EV-27", "GET", f"{_P}/evidence/lineage"),
    ("EV-28", "GET", f"{_P}/evidence/er"),
    ("EV-30", "POST", "/api/v1/evidence/sources/{source_id}/notify-admin"),
    # 提供方由管理员装进网关或旁路容器（H2），随证据源一起留接口
    ("PL-04", "POST", "/api/v1/packs/{name}/install-requests"),
    ("PL-19", "POST", "/api/v1/admin/packs/{name}/install"),
)


def not_available(api: str):
    return errors.response(CODE, MESSAGE, STATUS, {"api": api})


def _handler(api: str):
    async def handler():
        return not_available(api)

    handler.__name__ = f"not_available_{api.replace('-', '_').lower()}"
    handler.__doc__ = f"{api}：本期未开放（ADR-0056）。"
    return handler


for _api, _method, _path in ROUTES:
    router.add_api_route(_path, _handler(_api), methods=[_method], include_in_schema=False)


# ---- EV-29 锚点解析（只实现 file: 锚点） ----

ANCHORS_MAX = 100
IMPLEMENTED = (("EV-29", "GET", f"{_P}/anchors/resolve"), ("EV-29", "POST", f"{_P}/anchors/resolve"))


@router.get(f"{_P}/anchors/resolve")
def resolve_anchor(project_id: str, request: Request, anchor: str = ""):
    """EV-29（GET）：一个锚点。"""
    root = deps.require_owner(project_id, request)
    if not anchor.strip():
        raise ApiError("invalid_argument", "要指定 anchor", 400, {"fields": {"anchor": "required"}})
    return {"items": material_text.resolve_many(root, [anchor])}


@router.post(f"{_P}/anchors/resolve")
async def resolve_anchors(project_id: str, request: Request):
    """EV-29（POST）：一批锚点，最多 100 个，按请求顺序返回。"""
    root = deps.require_owner(project_id, request)
    body = await deps.json_body(request)
    anchors = body.get("anchors")
    if not isinstance(anchors, list) or not all(isinstance(item, str) for item in anchors):
        raise ApiError("invalid_argument", "anchors 要是文字列表", 400, {"fields": {"anchors": "list"}})
    if len(anchors) > ANCHORS_MAX:
        raise ApiError("invalid_argument", f"一次最多 {ANCHORS_MAX} 个锚点", 400, {"fields": {"anchors": "too_many"}})
    return {"items": material_text.resolve_many(root, anchors)}
