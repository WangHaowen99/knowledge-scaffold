"""资料锚点解析（契约 EV-29）：`file:` 锚点走资料文本，其余锚点回 resolved=false（not_available）。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..api import deps
from ..api.errors import ApiError
from ..services import material_text

ANCHORS_MAX = 100

router = APIRouter(tags=["anchors"])
_PATH = "/api/v1/projects/{project_id}/anchors/resolve"


def resolve_anchor(project_id: str, request: Request, anchor: str = ""):
    """EV-29（GET）：一个锚点。"""
    root = deps.require_owner(project_id, request)
    if not anchor.strip():
        raise ApiError("invalid_argument", "要指定 anchor", 400, {"fields": {"anchor": "required"}})
    return {"items": material_text.resolve_many(root, [anchor])}


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


router.add_api_route(_PATH, resolve_anchor, methods=["GET"], include_in_schema=False)
router.add_api_route(_PATH, resolve_anchors, methods=["POST"], include_in_schema=False)
