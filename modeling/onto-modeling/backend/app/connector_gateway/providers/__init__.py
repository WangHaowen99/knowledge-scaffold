"""网关里的证据源提供方（证据采集接口 §4；ADR-0082）。

提供方只负责跟自己的执行端打交道（旁路容器、以后的子进程）：提交任务、查进度、取消、取结果和记录流、取原始结果、清理。
票据、预算、任务表、记录校验、快照包是网关统一做的（`jobs`、`bundle`），对所有提供方一样。
"""

from __future__ import annotations

import os

from .rea_static import REA_URL_ENV, ReaStatic


def load() -> dict:
    """{提供方 ID: 提供方}。没配旁路容器地址的提供方不加载，也就不出现在 `/v1/providers` 里。"""
    found: dict = {}
    url = os.environ.get(REA_URL_ENV, "").strip()
    if url:
        provider = ReaStatic(url)
        found[provider.id] = provider
    return found
