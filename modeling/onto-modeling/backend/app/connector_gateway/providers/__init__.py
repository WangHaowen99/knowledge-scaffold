"""网关里的证据源提供方（证据采集接口 §4；ADR-0082）。

提供方只负责跟自己的执行端打交道（旁路容器、网关里的子进程）：提交任务、查进度、取消、取结果和记录流、取原始结果、清理。
票据、预算、任务表、记录校验、快照包是网关统一做的（`jobs`、`bundle`），对所有提供方一样。
"""

from __future__ import annotations

import os

from ..conn import catalog
from ..conn.runner import SubprocessProvider
from .rea_static import REA_URL_ENV, ReaStatic


def load(secrets=None) -> dict:
    """{提供方 ID: 提供方}。没配旁路容器地址的 rea-static 不加载，也就不出现在 `/v1/providers` 里。

    连接型提供方（数据库、代码仓库）在配了主密钥文件（ONTO_CONNECTOR_MASTER_KEY_FILE）时加载；`installed` 看主密钥
    读不读得出来、驱动装没装（网关进程不导入驱动）。
    """
    found: dict = {}
    url = os.environ.get(REA_URL_ENV, "").strip()
    if url:
        provider = ReaStatic(url)
        found[provider.id] = provider
    if secrets is not None and secrets.key_file:
        for provider_id in catalog.PROVIDERS:
            found[provider_id] = SubprocessProvider(provider_id, secrets)
    return found
