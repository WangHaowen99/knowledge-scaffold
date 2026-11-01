"""C-05 元模型（API 契约 3.1）：前端据此生成条目表单、解析 `[[链接]]`。"""

from __future__ import annotations

from fastapi import APIRouter

from ..kernel import metamodel as _metamodel

router = APIRouter(prefix="/api/v1", tags=["metamodel"])


@router.get("/metamodel")
def metamodel():
    """全局接口没有项目上下文：给 stable 通道当前的 core 版本里的元模型。"""
    return _metamodel.stable().as_dict()
