"""插件运行时接到治理平面的四个登记点（B3 合并时留下，见 _plan/gaps.md「B3 合并」）。

- 就绪检查的 `assembly` 项（契约 C-04、O-13；ADR-0042）：stable 通道的 core 能装配、上次启动核对
  没有失败的项目，才算就绪。判据：
  - fail：包仓库里没有 stable core，或 stable core 的内容和校验和对不上（新项目建不起来）；
  - warn：上次启动核对有项目装配失败（这些项目保持原版本还能用，其余项目不受影响）；
  - ok：其余情况；镜像里没有工具箱（测试里的最小部署）也算 ok。
- O-29 熔断的包查询：返回 O-25 的一项，包或版本不在包仓库里返回 None（接口回 404）。
- O-42 版本清单：各包 stable 版本、`packs.lock` 与 `flow.lock` 格式版本。
- O-25～O-28 插件运维（B5c，ADR-0067）：列表、灰度、回滚通道、禁用版本。灰度只做按项目：beta 通道带比例，
  跟随 stable 的项目按项目编号的稳定分桶落进比例里就跟 beta（`runtime.follow`）；按租户、自动熔断属于 P4，
  接口如实回未开放。
- 提交 trailer 的 `Onto-Packs`（详设 4.3、契约 7.17）：本项目生效中的装配里启用的包与版本，
  `core@0.2.0+c08f6c92, trace@0.9.1+98687971` 这样按名字排；没有装配目录时不带。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from . import lockfile
from . import manifest as mf

_log = logging.getLogger(__name__)
_LOCK = threading.Lock()
_last_reconcile: dict = {}
_installed = False
TRAILER_MAX_PACKS = 20


def record_reconcile(report: dict) -> None:
    """启动核对的结果（service.reconcile_all 调用），就绪检查据此判断有没有装配失败的项目。"""
    with _LOCK:
        _last_reconcile.clear()
        _last_reconcile.update(report or {})


def assembly_probe(manager) -> tuple[str, str | None]:
    runtime = manager.packs
    try:
        if runtime.core_version() is None:
            return "ok", "镜像里没有工具箱，插件装配未启用"
        stable = runtime.repository.latest("core", "stable")
        if not stable:
            return "fail", "包仓库里没有 stable 通道的基础建模能力"
        problems = runtime.repository.verify("core", stable)
        if problems:
            return "fail", f"stable 基础建模能力 {stable} 的内容被改过：{problems[0]}"
        runtime.assembler.plan(lockfile.PackLock([runtime.entry_for("core", stable)]))
    except Exception as exc:  # noqa: BLE001 - 探测本身出错按不可用报
        return "fail", f"装配检查出错：{type(exc).__name__}"
    with _LOCK:
        failed = dict(_last_reconcile.get("failed") or {})
    if failed:
        return "warn", f"上次启动核对有 {len(failed)} 个项目装配失败，保持原版本"
    return "ok", None


def pack_item(manager, name: str, version: str) -> dict | None:
    """O-25 的一项（O-29 熔断用）；包或版本不存在返回 None。"""
    from ...governance import ops_state

    repository = manager.packs.repository
    if not repository.has(name, version):
        return None
    manifest = repository.manifest(name, version)
    entry = repository.channel_entry(name)
    beta = entry.get("beta") if isinstance(entry.get("beta"), dict) else {}
    if repository.channel(name, "stable") == version:
        channel, rollout_pct, rollout_by = "stable", 100, "project"
    elif beta.get("version") == version:
        channel, rollout_pct, rollout_by = "beta", int(beta.get("percent") or 0), str(beta.get("by") or "project")
    else:
        channel, rollout_pct, rollout_by = "retired", 0, "project"
    using = 0
    for root in manager._project_dirs():
        try:
            lock = lockfile.read(root)
        except (ValueError, OSError):
            continue
        current = lock.get(name) if lock else None
        if current and current.enabled and current.version == version:
            using += 1
    if version in repository.disabled(name):
        channel, rollout_pct = "retired", 0
    breaker = ops_state.breaker_state(name, version)
    return {"name": name, "kind": manifest.kind, "version": version, "channel": channel,
            "rollout_pct": rollout_pct, "rollout_by": rollout_by, "error_rate_pct": None,
            "breaker": breaker or "closed", "projects_using": using,
            "rollback_to": rollback_to(repository, name, version, channel)}


def rollback_to(repository, name: str, version: str, channel: str) -> str | None:
    """O-27 能把这个通道改回去的版本：通道历史里上一个不同、还在、没禁用的版本；历史里没有时取更早的版本。"""
    if channel not in ("stable", "beta"):
        return None
    previous = repository.previous_in_channel(name, channel, exclude=version)
    if previous:
        return previous
    disabled = repository.disabled(name)
    older = [item for item in repository.versions(name)
             if item != version and item not in disabled and mf.version_key(item) < mf.version_key(version)]
    return older[0] if older else None


def _is_provider(repository, name: str) -> bool:
    """证据源提供方（本期不开放，ADR-0056）：插件运维与版本清单里不列。"""
    versions = repository.versions(name)
    try:
        return bool(versions) and repository.manifest(name, versions[0]).category == "provider"
    except Exception:  # noqa: BLE001 - 读不出清单的照常列，由 pack_item 兜底
        return False


def ops_items(manager) -> list[dict]:
    """O-25：每个包在通道上的版本、项目在用的版本与禁用的版本，各一行。"""
    runtime = manager.packs
    runtime.core_version()
    repository = runtime.repository
    used: dict[str, set[str]] = {}
    for root in manager._project_dirs():
        try:
            lock = lockfile.read(root)
        except (ValueError, OSError):
            continue
        for entry in (lock.enabled() if lock else []):
            used.setdefault(entry.name, set()).add(entry.version)
    items = []
    for name in repository.names():
        if _is_provider(repository, name):
            continue
        entry = repository.channel_entry(name)
        beta = entry.get("beta") if isinstance(entry.get("beta"), dict) else {}
        wanted = {repository.latest(name, "stable"), beta.get("version"), *used.get(name, set()),
                  *repository.disabled(name)}
        for version in sorted((item for item in wanted if item and repository.has(name, item)),
                              key=mf.version_key, reverse=True):
            item = pack_item(manager, name, version)
            if item is not None:
                items.append(item)
    return items


class OpsError(Exception):
    def __init__(self, code: str, message: str, status: int = 409, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details


def _require(manager, name: str, version: str) -> None:
    if not manager.packs.repository.has(name, version):
        raise OpsError("pack_version_not_found", "包或版本不存在", 404)


def set_rollout(manager, name: str, version: str, *, percent: int, by: str, targets: list[str], actor: str) -> dict:
    """O-26：调 beta 通道的放量比例（按项目）。stable 版本恒为全量；按租户放量本期未开放。"""
    repository = manager.packs.repository
    _require(manager, name, version)
    if by != "project":
        raise OpsError("rollout_not_available", "按租户灰度本期未开放", 501, {"by": by})
    entry = repository.channel_entry(name)
    beta = entry.get("beta") if isinstance(entry.get("beta"), dict) else {}
    if beta.get("version") != version:
        raise OpsError("invalid_argument", "只有 beta 通道上的版本能调灰度", 400, {"fields": {"version": "not_beta"}})
    rollout = {key: value for key, value in beta.items() if key not in ("version", "percent", "by", "targets")}
    repository.set_channel(name, "beta", version, by=actor,
                           rollout={**rollout, "percent": percent, "by": by, "targets": list(targets)})
    return pack_item(manager, name, version)


def rollback_channel(manager, name: str, *, channel: str, to_version: str, actor: str) -> dict:
    """O-27：把通道指针改回旧版本。锁定版本的项目不受影响，跟随通道的项目下一条消息生效；core 的 stable
    同 O-32（先备份包仓库）。"""
    from . import builtin, channels

    repository = manager.packs.repository
    _require(manager, name, to_version)
    if to_version in repository.disabled(name):
        raise OpsError("pack_unavailable", f"{name} {to_version} 已被禁用", 409, {"reason": "not_installed"})
    if channel == "stable" and name == builtin.CORE:
        channels.set_core_default(repository, to_version, by=actor, backup_first=True)
    elif channel == "beta":
        beta = repository.channel_entry(name).get("beta")
        rollout = {key: value for key, value in (beta or {}).items() if key != "version"} if isinstance(beta, dict) else None
        repository.set_channel(name, "beta", to_version, by=actor, rollout=rollout)
    else:
        repository.set_channel(name, channel, to_version, by=actor)
    return pack_item(manager, name, to_version)


def disable_version(manager, name: str, version: str, *, reason: str, actor: str) -> dict:
    """O-28：禁用一个版本：新项目不能再启用它，通道指着它的时候改取最新的可用版本（跟随的项目下一条消息换过去）。
    基础建模能力的默认版本不能禁用（新项目会建不起来），先回滚默认版本。"""
    from . import builtin

    repository = manager.packs.repository
    _require(manager, name, version)
    if name == builtin.CORE and repository.latest(name, "stable") == version:
        raise OpsError("pack_required", "基础建模能力的默认版本不能禁用，先回滚默认版本", 409)
    repository.disable_version(name, version, reason, by=actor)
    return pack_item(manager, name, version)


def rollout_bucket(project_id: str) -> int:
    """按项目灰度的稳定分桶 0～99（同一个项目每次落在同一个桶）。"""
    import hashlib

    return int(hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:8], 16) % 100


def beta_for(repository, name: str, project_id: str) -> str | None:
    """跟随 stable 的项目这次该不该拿 beta：beta 在名单里点了它，或分桶落在放量比例里。"""
    beta = repository.channel_entry(name).get("beta")
    if not isinstance(beta, dict) or not beta.get("version") or str(beta.get("by") or "project") != "project":
        return None
    version = beta["version"]
    if not repository.has(name, version) or version in repository.disabled(name):
        return None
    # beta 要比 stable 新才放量（审查第 3 条）：停在旧版本上的 beta 会把分桶里的项目降级
    stable = repository.latest(name, "stable")
    if stable and mf.version_key(version) <= mf.version_key(stable):
        return None
    if project_id in (beta.get("targets") or []) or rollout_bucket(project_id) < int(beta.get("percent") or 0):
        return version
    return None


def versions(manager) -> list[dict]:
    """O-42：各包 stable 版本与锁文件格式版本。"""
    runtime = manager.packs
    from ..flow import flowlock

    items = [{"category": "lockfile", "component": "packs.lock", "version": str(lockfile.LOCK_VERSION),
              "note": "2：core 拆出可选包之后的锁文件"},
             {"category": "lockfile", "component": "flow.lock", "version": str(flowlock.FLOW_LOCK_VERSION),
              "note": "组装后的流程节点"}]
    try:
        runtime.core_version()
        repository = runtime.repository
        for name in repository.names():
            version = repository.latest(name, "stable")
            if not version:
                continue
            manifest = repository.manifest(name, version)
            if manifest.category == "provider":
                continue  # 证据源提供方本期不开放（ADR-0056）
            items.append({"category": manifest.category, "component": name, "version": version, "note": manifest.label})
    except Exception as exc:  # noqa: BLE001 - 版本清单取不到包时只给锁文件格式
        _log.warning("版本清单读取包仓库失败：%s", exc)
    return items


def packs_trailer(root: Path) -> dict:
    """提交 trailer 的 Onto-Packs：本项目生效中的装配里的包与版本。"""
    import json
    import os

    try:
        link = Path(root) / ".claude"
        if not os.path.lexists(link):
            return {}
        real = Path(os.path.realpath(link))
        if real.parent.name != "assembled":
            return {}
        manifest = json.loads((real / "MANIFEST.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    packs = sorted(f"{item['name']}@{item['version']}" for item in manifest.get("packs") or [] if item.get("name"))
    if not packs:
        return {}
    shown = packs[:TRAILER_MAX_PACKS]
    if len(packs) > TRAILER_MAX_PACKS:
        shown.append(f"…+{len(packs) - TRAILER_MAX_PACKS}")
    return {"Onto-Packs": ", ".join(shown)}


def install(manager) -> None:
    """登记到治理平面与写入路径（进程里只做一次；manager 是业务面的工作区管理器）。"""
    global _installed
    from ...governance import health
    from ...kernel.writepath import register_trailer_provider
    from ...routers import ops_control

    with _LOCK:
        first = not _installed
        _installed = True
    health.register_check("assembly", "stable core 可装配、启动核对无失败项目",
                          lambda: assembly_probe(_current(manager)), critical=True)
    ops_control.register_pack_lookup(lambda name, version: pack_item(_current(manager), name, version))
    if first:
        ops_control.register_version_provider(lambda: versions(_current(manager)))
    # 包声明的指标与告警规则进可观测（B5c，ADR-0068）
    from ...governance import telemetry

    ops_control.register_alert_provider(_declared_alerts)
    telemetry.register_declarations(_declared_metrics)
    # 插件运行时建出来时已经登记过（PackRuntime），这里再登记一次也只算一个
    register_trailer_provider(packs_trailer)


def _declared_alerts() -> list[dict]:
    from . import extensions

    return extensions.declared_alerts(_current(None))


def _declared_metrics() -> list[dict]:
    from . import extensions

    return extensions.declared_metrics(_current(None))


def _current(fallback):
    """路由里替换过的工作区管理器（测试会换 deps.workspaces）优先。"""
    try:
        from ...api import deps

        return deps.workspaces
    except Exception:  # noqa: BLE001
        return fallback
