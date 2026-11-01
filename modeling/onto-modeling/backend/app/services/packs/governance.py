"""插件运行时接到治理平面的四个登记点（B3 合并时留下，见 _plan/gaps.md「B3 合并」）。

- 就绪检查的 `assembly` 项（契约 C-04、O-13；ADR-0042）：stable 通道的 core 能装配、上次启动核对
  没有失败的项目，才算就绪。判据：
  - fail：包仓库里没有 stable core，或 stable core 的内容和校验和对不上（新项目建不起来）；
  - warn：上次启动核对有项目装配失败（这些项目保持原版本还能用，其余项目不受影响）；
  - ok：其余情况；镜像里没有工具箱（测试里的最小部署）也算 ok。
- O-29 熔断的包查询：返回 O-25 的一项，包或版本不在包仓库里返回 None（接口回 404）。
- O-42 版本清单：各包 stable 版本、`packs.lock` 格式版本。
- 提交 trailer 的 `Onto-Packs`（详设 4.3、契约 7.17）：本项目生效中的装配里启用的包与版本，
  `core@0.2.0+c08f6c92, trace@0.9.1+98687971` 这样按名字排；没有装配目录时不带。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from . import lockfile

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
    breaker = ops_state.breaker_state(name, version)
    return {"name": name, "kind": manifest.kind, "version": version, "channel": channel,
            "rollout_pct": rollout_pct, "rollout_by": rollout_by, "error_rate_pct": None,
            "breaker": breaker or "closed", "projects_using": using}


def versions(manager) -> list[dict]:
    """O-42：各包 stable 版本与锁文件格式版本。"""
    runtime = manager.packs
    items = [{"category": "lockfile", "component": "packs.lock", "version": str(lockfile.LOCK_VERSION),
              "note": "2：core 拆出可选包之后的锁文件"}]
    try:
        runtime.core_version()
        repository = runtime.repository
        for name in repository.names():
            version = repository.latest(name, "stable")
            if not version:
                continue
            manifest = repository.manifest(name, version)
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
    # 插件运行时建出来时已经登记过（PackRuntime），这里再登记一次也只算一个
    register_trailer_provider(packs_trailer)


def _current(fallback):
    """路由里替换过的工作区管理器（测试会换 deps.workspaces）优先。"""
    try:
        from ...api import deps

        return deps.workspaces
    except Exception:  # noqa: BLE001
        return fallback
