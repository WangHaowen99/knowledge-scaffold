"""治理平面的启动与停机步骤（由 main 的 lifespan 调用）。

启动恢复之前：结构化日志、配置校验、会话池覆盖、密钥目录迁移与告警、agent 身份的目录权限，再等独立部署的 Auth 就绪；
启动恢复之后：标记就绪、起运维监听；停机：停运维监听、记下正常退出。
"""

from __future__ import annotations

import logging

from .. import config
from . import agent_identity, config_registry, health, lifecycle, ops_listener, ops_state, secrets, telemetry

_log = logging.getLogger(__name__)


def before_recovery() -> None:
    telemetry.configure_logging(getattr(logging, str(config.LOG_LEVEL or "INFO").upper(), logging.INFO))
    problems = config_registry.validate_all()
    for key, reason in problems.items():
        _log.warning("配置项不合法，已回落到下一个来源：%s（%s）", key, reason)
    ops_state.apply_pool_config()
    secrets.prepare()
    agent_identity.prepare_filesystem()
    _register_gauges()


def _register_gauges() -> None:
    from ..services.agent_sessions import manager

    telemetry.register_gauge("onto_sessions_active", manager.active_count)
    telemetry.register_gauge("onto_sessions_queued", lambda: len(manager.pool_status()["waiting"]))
    telemetry.register_gauge("onto_maintenance_active", lambda: int(ops_state.maintenance_active()))
    # 自主构建（详设 8.5 可观测）：在跑的运行数、复核时被改掉的比例（没复核过时不出）
    from ..services.autobuild import review, store

    telemetry.register_gauge("onto_autobuild_running", lambda: len(store.all_runs(store.RUNNING)))
    telemetry.register_gauge("onto_autobuild_rejudge_ratio", review.review_ratio)


async def wait_for_dependencies() -> None:
    """独立部署的依赖就绪之后再往下走：现在只有 Auth（ADR-0107），本地模式和关掉等待时直接返回。"""
    from ..auth_startup import wait_for_auth

    await wait_for_auth()


async def after_recovery():
    health.mark_startup_done()
    return await ops_listener.start()


async def shutdown(handle) -> None:
    await ops_listener.stop(handle)


def stopped() -> None:
    lifecycle.mark_stopped()
