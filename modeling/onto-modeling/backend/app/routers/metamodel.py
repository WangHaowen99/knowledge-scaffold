"""C-05 元模型（API 契约 3.1）：前端据此生成条目表单、解析 `[[链接]]`。"""

from __future__ import annotations

from fastapi import APIRouter

from ..kernel.metamodel import mm

router = APIRouter(prefix="/api/v1", tags=["metamodel"])


@router.get("/metamodel")
def metamodel():
    return mm.as_dict()
