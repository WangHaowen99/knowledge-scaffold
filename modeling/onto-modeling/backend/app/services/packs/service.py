"""插件运行时里要动工作区 git 的操作：持项目锁，只按路径提交锁文件（不带 agent 的其他改动）。

锁文件 `packs.lock` 与组装后的流程锁 `flow.lock` 经统一变更事务（ChangeTx，方案 C1）在同一个提交里合入主线，
带 `Onto-Source: plugin`。用户在项目里的切换（PL-06～PL-10）同时是一个插件变更集：校验通过即合入、
可以撤回（changes.py，ADR-0065）。
"""

from __future__ import annotations

import logging
from typing import Callable

from . import lockfile
from . import manifest as mf
from .assembler import AssemblyError
from .lockfile import PackLock

_log = logging.getLogger(__name__)


def commit_lock(manager, project_id: str, message: str, actor: str = "",
                write: Callable[[], object] | None = None, changeset: str | None = None) -> str:
    """经写入事务提交锁文件与 flow.lock，只提交这两个路径；没有改动返回空串。调用方持项目锁（可重入）。

    write 给定时在事务里写锁文件，提交失败恢复原内容；不给定时提交已经写好的锁文件（启动核对、
    跟随通道这类先写后提交的路径，中途停机由写入意图在下次启动补提交）。flow.lock 按写好的锁文件重新组装，
    装配不起来时保持原样。项目没有 git 时只写不提交。changeset：这次切换的插件变更集（trailer 带上）。
    """
    from ...kernel.writepath import ChangeTx, Origin
    from ..flow import flowlock

    root = manager.project_root(project_id)
    if not (root / ".git").is_dir():
        if write is not None:
            write()
        manager.packs.write_flow_lock(root)
        return ""
    with ChangeTx(manager, project_id, Origin("plugin", actor, changeset=changeset), summary=message) as tx:
        tx.touch(lockfile.LOCK_REL, snapshot=write is not None)
        if write is not None:
            write()
        flow = manager.packs.desired_flow(root)
        if flow is not None:
            text = flowlock.render(flow)
            try:
                current = flowlock.path(root).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                current = None
            if text != current:
                tx.write(flowlock.FLOW_LOCK_REL, text)
        return tx.commit(message).commit


def reconcile_all(manager) -> dict:
    """启动核对（方案 D4、8.1「启动恢复」）：逐个项目核对装配目录，不对就重新装配。

    在会话开始之前调用：清掉入库、装配中途停机留下的临时目录；没有锁文件的存量项目写一份
    并作为一次系统提交（方案 2.3 第 2 步）；`.claude` 指错、缺失、是旧版绝对链接，或装配目录
    内容被改过，都按锁文件重新装配并切换。单个项目失败只记日志，不挡启动。
    """
    runtime = manager.packs
    report = {"repaired": [], "locked": [], "failed": {}}
    verified: set = set()
    runtime.repository.cleanup_staging()
    runtime.assembler.cleanup_staging()
    if runtime.core_version() is None:
        return report
    for root in manager._project_dirs():
        project_id = root.name
        try:
            with manager._project_lock(project_id):
                before = runtime.assembler.active(root)
                _, wrote = runtime.ensure_lock(root)
                if wrote and commit_lock(manager, project_id, "chore: 写入能力包锁文件"):
                    report["locked"].append(project_id)
                status = runtime.reconcile(root, verified)
                if status and (before or {}).get("assembly_hash") != status.get("active_hash"):
                    report["repaired"].append(project_id)
        except (AssemblyError, OSError, ValueError, RuntimeError) as exc:
            report["failed"][project_id] = str(exc)
            _log.warning("项目 %s 启动核对装配目录失败：%s", project_id, exc)
        except Exception as exc:  # noqa: BLE001 - git 超时等，单个项目失败不能挡住服务启动
            report["failed"][project_id] = str(exc)
            _log.warning("项目 %s 启动核对装配目录失败：%s", project_id, exc)
    if report["repaired"]:
        _log.warning("启动时重新装配了项目：%s", ", ".join(report["repaired"]))
    from . import extensions, governance

    governance.record_reconcile(report)
    extensions.sync_config(manager)  # 各包声明的配置项进配置注册表（ADR-0068）
    return report


def prepare_for_message(manager, project_id: str, *, follow: bool = True) -> dict:
    """会话宿主在下一条消息送达前调用（这个项目没有正在跑的一轮）：跟随通道、装配并切换。

    装配失败不挡消息：保持原装配目录，错误记进 `.onto/run/assembly.json`，PL-11 能看到。
    """
    runtime = manager.packs
    root = manager.project_root(project_id)  # 项目不存在直接抛错，不为它建锁文件
    with manager._project_lock(project_id):
        if runtime.core_version() is None:
            return {}
        _, wrote = runtime.ensure_lock(root)
        committed = _follow_channels(manager, project_id, root) if follow else ""
        if wrote and not committed:
            commit_lock(manager, project_id, "chore: 写入能力包锁文件")
        try:
            return runtime.apply(root)
        except AssemblyError as exc:
            _log.warning("项目 %s 装配失败，保持原版本：%s", project_id, exc)
            return runtime.status(root)


def _follow_channels(manager, project_id: str, root) -> str:
    """跟随通道升级（审查第 5 条）：和手动切换一样记成插件变更集；助手这一轮还没合入（合入冲突没处理）、这次升级又会
    改流程节点时推迟（不回 423：这是系统发起的，下一条消息前再试）。返回提交号，没提交为空串。"""
    from ...kernel.changesets import store
    from . import changes

    runtime = manager.packs
    lock, packs = runtime.followed(root)
    if lock is None:
        return ""
    try:
        flow_changed = _flow_changed(manager, root, runtime.assembler.plan(lock))
    except AssemblyError:
        flow_changed = False
    if flow_changed and store.current(root) is not None:
        _log.info("项目 %s 的助手修改还没合入，推迟跟随通道升级 %s", project_id, "、".join(packs))
        return ""
    names = "、".join(packs) or "能力包"
    record = changes.begin(manager, project_id, purpose=f"跟随通道升级 {names}", pack=",".join(packs), kind="follow",
                           flow_changed=flow_changed)
    try:
        commit = commit_lock(manager, project_id, "chore: 跟随通道升级能力包", write=lambda: lockfile.write(root, lock),
                             changeset=record["id"] if record else None)
    except BaseException:
        changes.abort(manager, project_id, record)
        raise
    changes.finish(manager, project_id, record, commit)
    return commit


def desired_parts(manager, project_id: str) -> dict:
    """锁文件要求的装配对会话指纹的贡献：H1 内容哈希与流程哈希（只读，不切换）。"""
    runtime = manager.packs
    try:
        root = manager.project_root(project_id)
    except Exception:  # noqa: BLE001 - 项目已删除等：不参与指纹
        return {}
    try:
        lock = lockfile.read(root)
        if lock is None:
            active = runtime.assembler.active(root) or {}
            return {"assembly": active.get("h1_hash") or "", "flow": active.get("flow_hash") or ""}
        plan = runtime.assembler.plan(lock)
    except (AssemblyError, ValueError, OSError):
        # 锁文件要求的版本装不起来：按当前生效的算，不因为一次失败的切换反复重连
        active = runtime.assembler.active(root) or {}
        return {"assembly": active.get("h1_hash") or "", "flow": active.get("flow_hash") or ""}
    built = runtime.assembler.dir_for(plan.hash) / "MANIFEST.json"
    flow = ""
    try:
        import json

        flow = json.loads(built.read_text(encoding="utf-8")).get("flow_hash") or ""
    except (OSError, ValueError):
        pass
    return {"assembly": plan.h1_hash, "flow": flow}


# ---- 按项目启用 / 升级 / 回滚 / 停用（契约 PL-05～PL-11）----


class PackError(Exception):
    """插件操作的契约错误（第 2.7 节错误码）。"""

    def __init__(self, code: str, message: str, status: int = 409, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details


def _ui(manifest: mf.Manifest) -> dict | None:
    """契约 7.11 PackUi：页面只在能力包上有。"""
    if manifest.name == "core":
        return {"kind": "kernel", "note": "内核能力：工作台、Studio、资料、发布"}
    if manifest.kind != "capability":
        return None
    pages = manifest.pages()
    subpages = [sub for sub in manifest.contributes.get("subpages") or [] if isinstance(sub, dict)]
    if pages:
        return {"kind": "page", "pages": [{"id": page.get("id"), "title": (page.get("nav") or {}).get("title") or page.get("id")}
                                          for page in pages], "note": "启用后左栏出现对应页面"}
    if subpages:
        return {"kind": "subpages", "subpages": subpages, "note": "启用后在内核页面里多出子页"}
    return {"kind": "none", "note": "没有页面，在对话里使用"}


def _newer_available(repository, name: str, locked: str, channel: str) -> str | None:
    latest = repository.latest(name, channel or "stable")
    return latest if latest and mf.version_key(latest) > mf.version_key(locked) else None


def _rollback_target(runtime, lock: PackLock, name: str, locked: str) -> str | None:
    """PL-08 能回滚到的版本：比锁定版本旧、没停用，而且换上它以后本项目还能装配（依赖、内核接口对得上）；
    一个都没有就不给回滚选项（不给出必然 409 的按钮）。"""
    repository = runtime.repository
    disabled = repository.disabled(name)
    older = [version for version in repository.versions(name)
             if version not in disabled and mf.version_key(version) < mf.version_key(locked)]
    if not older:
        # 同一基础版本的内容摘要不同（镜像自带的 core）：按入库时间取上一个，后入库的是新版本、不算回滚
        locked_at = repository.meta(name, locked).get("released_at", 0)

        def released_earlier(version: str) -> bool:
            if version == locked or version in disabled:
                return False
            return repository.meta(name, version).get("released_at", 0) < locked_at

        older = [version for version in repository.versions(name) if released_earlier(version)]
    for version in older:
        trial = lock.copy()
        entry = trial.get(name)
        entry.version, entry.sha256 = version, repository.digest(name, version)
        if runtime._plannable(trial):
            return version
    return None


def project_packs(manager, project_id: str) -> dict:
    """PL-05：本项目的能力（含停用的）与待生效的切换。"""
    runtime = manager.packs
    root = manager.project_root(project_id)
    lock, _ = runtime.ensure_lock(root)
    status = runtime.status(root)
    changes = {change["pack"]: change for change in status["pending_changes"]}
    items = []
    for entry in (lock.packs if lock else []):
        if not runtime.repository.has(entry.name, entry.version):
            continue
        manifest = runtime.repository.manifest(entry.name, entry.version)
        change = changes.get(entry.name)
        latest = _newer_available(runtime.repository, entry.name, entry.version, entry.channel)
        rollback_to = _rollback_target(runtime, lock, entry.name, entry.version)
        if change:
            state = "pending"
            actions = ["cancel_pending"]
        elif not entry.enabled:
            state, actions = "disabled", ["enable"]
        else:
            state = "required" if manifest.required else "enabled"
            offered = (("upgrade", latest), ("rollback", rollback_to), ("disable", not manifest.required))
            actions = [action for action, ok in offered if ok]
        items.append({
            "name": entry.name, "label": manifest.label, "category": manifest.category, "ui": _ui(manifest),
            "locked_version": entry.version, "latest_available": latest, "channel": entry.channel or "stable",
            "status": state,
            "pending": {"from": change["from"], "to": change["to"], "kind": change["kind"],
                        "effective": status["effective"]} if change else None,
            "enabled_at": entry.enabled_at or None, "actions": actions, "rollback_to": rollback_to,
            "follow": entry.follow,
        })
    return {"items": items, "pending_switches": len(changes), "lock_hash": status["lock_hash"],
            "flow_hash": status["flow_hash"], "status": status}


def _plan_or_raise(runtime, lock: PackLock):
    try:
        plan = runtime.assembler.plan(lock)
        runtime.assembler.check(plan)
        return plan
    except AssemblyError as exc:
        if exc.reason == "flow":
            raise PackError("flow_assembly_failed", "流程组装不通过：" + "；".join(exc.problems), 422,
                            {"problems": exc.problems}) from exc
        if exc.reason in ("plan", "conflict"):
            raise PackError("pack_incompatible", "兼容检查不通过：" + "；".join(exc.problems), 409,
                            {"checks": [{"id": "names" if exc.reason == "conflict" else "depends",
                                         "label": "名字不冲突" if exc.reason == "conflict" else "依赖与内核接口",
                                         "ok": False, "detail": problem} for problem in exc.problems]}) from exc
        raise PackError("assembly_failed", "装配失败，项目保持原版本", 409,
                        {"error_id": None, "reason": str(exc)}) from exc


def _extensions_or_none(manager, project_id: str) -> dict | None:
    from . import registry

    try:
        return registry.extensions(manager, project_id)
    except Exception:  # noqa: BLE001 - 事件的比较基准取不到只影响 changed 列表
        return None


def announce_immediate_switch(project_id: str, active_before: str | None, status: dict) -> None:
    """改锁文件之后的项目流事件（ADR-0045）。

    待生效的 assembly.updated（EP-08）与 extensions.updated（EP-07）由项目流在锁文件的提交事件里发
    （`services/project_events.workspace_changed`），这里不重复。只动纯数据包（H0）时装配目录当场就切了，
    之后不会再有「消息前切换」，已生效的这一次在这里补发（与 `fingerprint.prepare` 同一个出口）。
    """
    from .. import project_events

    project_events.assembly_switched(project_id, active_before, status.get("active_hash"))


def _flow_changed(manager, root, plan) -> bool:
    """这次切换会不会改流程节点（flow.lock 的内容变不变）。"""
    from ..flow import flowlock

    flow = manager.packs.flow_of(plan)
    if flow is None:
        return False
    current = flowlock.read(root)
    if current is None:  # 存量项目还没有 flow.lock：和现在锁文件组装出的流程比
        before = manager.packs.desired_flow(root)
        current = flowlock.summarize(before) if before is not None else {}
    return current.get("nodes_hash") != flowlock.summarize(flow)["nodes_hash"]


def _switch(manager, project_id: str, mutate, message: str, actor: str, *, pack: str = "", kind: str = "") -> dict:
    """改锁文件的公共流程：先装配新版本（失败就不改锁文件），再作为插件变更集提交锁文件与 flow.lock；
    只动纯数据包时立即生效。"""
    from . import changes

    runtime = manager.packs
    root = manager.project_root(project_id)
    if manager.get_project(project_id).get("archived"):
        raise PackError("project_archived", "项目已归档，只能查看", 409)
    before = _extensions_or_none(manager, project_id)
    from . import registry

    registry.remember_baseline(project_id, before)
    with manager._project_lock(project_id):
        lock, wrote = runtime.ensure_lock(root)
        if lock is None:
            raise PackError("pack_unavailable", "部署里没有基础建模能力，不能管理插件", 409, {"reason": "not_installed"})
        new_lock = lock.copy()
        result = mutate(new_lock)
        plan = _plan_or_raise(runtime, new_lock)
        runtime.assembler.build(plan)
        record = changes.begin(manager, project_id, purpose=message.removeprefix("plugin: "), pack=pack, kind=kind,
                               flow_changed=_flow_changed(manager, root, plan))
        try:
            commit = commit_lock(manager, project_id, message, actor, write=lambda: lockfile.write(root, new_lock),
                                 changeset=record["id"] if record else None)
        except BaseException:
            changes.abort(manager, project_id, record)
            raise
        record = changes.finish(manager, project_id, record, commit)
        if wrote and not commit:
            commit = commit_lock(manager, project_id, "chore: 写入能力包锁文件", actor)
        active_before = (runtime.assembler.active(root) or {}).get("assembly_hash")
        try:
            status = runtime.apply(root, h0_only=True)
        except AssemblyError:
            status = runtime.status(root)
    from .. import mainline

    announce_immediate_switch(project_id, active_before, status)
    return {"result": result, "commit": commit or None, "version_label": mainline.state(root)["version_label"],
            "status": status, "changeset_id": record["id"] if record and record.get("state") == "merged" else None}


def _pack_view(manager, project_id: str, name: str) -> dict | None:
    return next((item for item in project_packs(manager, project_id)["items"] if item["name"] == name), None)


def _response(manager, project_id: str, name: str, outcome: dict) -> dict:
    status = outcome["status"]
    return {
        "pack": _pack_view(manager, project_id, name),
        "commit": outcome["commit"],
        "version_label": outcome["version_label"],
        "effective": "immediate" if status["state"] == "applied" else status["effective"],
        "extensions_pending_hash": status["pending_hash"],
        "changeset_id": outcome.get("changeset_id"),
    }


def _missing_providers(repository, name: str, version: str) -> list[str]:
    from ..flow import node_kinds

    try:
        return node_kinds.missing_providers(node_kinds.pack_nodes(repository, name, version))
    except Exception:  # noqa: BLE001 - 节点声明坏了由装配检查报
        return []


def _require_version(repository, name: str, version: str | None) -> str:
    if not repository.versions(name):
        raise PackError("pack_not_found", f"包 {name} 不存在", 404)
    version = version or repository.latest(name, "stable")
    if not version or not repository.has(name, version):
        raise PackError("pack_version_not_found", f"包 {name} 没有版本 {version}", 404)
    if version in repository.disabled(name):
        raise PackError("pack_unavailable", f"{name} {version} 已被禁用", 409, {"reason": "not_installed"})
    manifest = repository.manifest(name, version)
    if manifest.hot_level == "H2":
        raise PackError("pack_unavailable", "可替换实现要由管理员安装到平台，不能按项目启用", 409, {"reason": "not_installed"})
    missing = _missing_providers(repository, name, version)
    if missing:
        # 外部节点要经连接器网关调外部系统（详设 6.5）；本期证据源只留接口，没有提供方
        raise PackError("pack_unavailable", f"需要的连接器不可用：{'、'.join(missing)}", 409,
                        {"reason": "not_installed", "providers": missing})
    if manifest.page_api and not mf.satisfies(mf.PAGE_API, manifest.page_api):
        raise PackError("page_api_unsupported", f"需要平台页面接口 {manifest.page_api}", 409,
                        {"required": manifest.page_api, "platform": mf.PAGE_API})
    return version


def require_visible(manager, name: str, viewer: str, project_id: str = "") -> None:
    """别人的私有包按不存在处理（ADR-0079）；项目转给别人后，已经锁在项目里的照常能用。"""
    if manager.packs.repository.visible(name, viewer):
        return
    lock = lockfile.read(manager.project_root(project_id)) if project_id else None
    if lock is None or lock.get(name) is None:
        raise PackError("pack_not_found", f"包 {name} 不存在", 404)


def enable(manager, project_id: str, name: str, version: str | None = None, actor: str = "") -> dict:
    """PL-06：启用到本项目。停用过的包重新启用回到原锁定版本（没指定版本时）。"""
    repository = manager.packs.repository
    root = manager.project_root(project_id)
    current = (lockfile.read(root) or PackLock()).get(name)
    if current and current.enabled:
        raise PackError("pack_already_enabled", f"本项目已启用 {name}", 409)
    if current and not version and repository.has(name, current.version):
        version = current.version
    version = _require_version(repository, name, version)
    latest = repository.latest(name, "stable")

    def mutate(lock: PackLock):
        entry = manager.packs.entry_for(name, version, follow=version == latest)
        lock.upsert(entry)

    label = repository.manifest(name, version).label
    outcome = _switch(manager, project_id, mutate, f"plugin: 启用 {label} {version}", actor, pack=name, kind="enable")
    return _response(manager, project_id, name, outcome)


def change_version(manager, project_id: str, name: str, to_version: str, *, kind: str, actor: str = "") -> dict:
    """PL-07 升级、PL-08 回滚：改锁定版本。升级到通道最新版恢复跟随通道，回滚则锁定版本。"""
    repository = manager.packs.repository
    root = manager.project_root(project_id)
    current = (lockfile.read(root) or PackLock()).get(name)
    if current is None or not current.enabled:
        raise PackError("pack_not_enabled", f"本项目没启用 {name}", 409)
    to_version = _require_version(repository, name, to_version)
    latest = repository.latest(name, current.channel or "stable")
    label = repository.manifest(name, to_version).label

    def mutate(lock: PackLock):
        lock.upsert(manager.packs.entry_for(name, to_version, channel=current.channel or "stable",
                                            follow=kind == "upgrade" and to_version == latest))

    verb = "升级" if kind == "upgrade" else "回滚"
    outcome = _switch(manager, project_id, mutate, f"plugin: {verb} {label} {current.version} → {to_version}", actor,
                      pack=name, kind=kind)
    return _response(manager, project_id, name, outcome)


def disable(manager, project_id: str, name: str, actor: str = "") -> dict:
    """PL-09：停用。产物保留；锁文件留一条 enabled=false 的记录，重新启用回到原版本。"""
    repository = manager.packs.repository
    root = manager.project_root(project_id)
    current = (lockfile.read(root) or PackLock()).get(name)
    if current is None or not current.enabled:
        raise PackError("pack_not_enabled", f"本项目没启用 {name}", 409)
    if repository.has(name, current.version) and repository.manifest(name, current.version).required:
        raise PackError("pack_required", "必装的基础建模能力不能停用", 409)

    def mutate(lock: PackLock):
        lock.get(name).enabled = False

    outcome = _switch(manager, project_id, mutate, f"plugin: 停用 {name}", actor, pack=name, kind="disable")
    return _response(manager, project_id, name, outcome)


def cancel_pending(manager, project_id: str, name: str, actor: str = "") -> dict:
    """PL-10：下一条消息之前把这个包的锁定改回生效中的版本。"""
    runtime = manager.packs
    root = manager.project_root(project_id)
    active = runtime.assembler.active(root)
    if not active:
        raise PackError("no_pending_switch", "没有待生效的切换可撤销", 409)
    applied = {item["name"]: item for item in (active.get("lock") or {}).get("packs") or []}
    current = (lockfile.read(root) or PackLock()).get(name)
    before = applied.get(name)
    enabled_now = bool(current and current.enabled)
    unchanged = enabled_now and bool(before) and before["version"] == current.version
    if unchanged or (not before and not enabled_now):
        raise PackError("no_pending_switch", "没有待生效的切换可撤销", 409)

    def mutate(lock: PackLock):
        if before:
            restored = PackLock.from_dict({"packs": [before]}).packs[0]
            restored.enabled = True
            lock.upsert(restored)
        elif lock.get(name):
            lock.get(name).enabled = False

    if not _revert_pending(manager, project_id, name, actor) or not _matches_active(root, name, before):
        # 撤回的只是最近一次（同一条消息前切换了两次时，前一次还在）：再按生效中的版本改回（审查第 7 条）
        _switch(manager, project_id, mutate, f"plugin: 撤销待生效的切换 {name}", actor, pack=name, kind="cancel")
    return {"pack": _pack_view(manager, project_id, name) or _unlocked_view(manager, name, current)}


def _matches_active(root, name: str, before: dict | None) -> bool:
    """锁文件里这个包和生效中的装配一致：生效中有它就版本相同且启用，没有它就不在锁里或已停用。"""
    entry = (lockfile.read(root) or PackLock()).get(name)
    if before:
        return bool(entry and entry.enabled and entry.version == before["version"])
    return entry is None or not entry.enabled


def _unlocked_view(manager, name: str, entry) -> dict | None:
    """撤回了第一次启用之后锁文件里没有这个包了：按「没启用」给一项（和停用的一样可以再启用）。"""
    repository = manager.packs.repository
    version = entry.version if entry and repository.has(name, entry.version) else repository.latest(name, "stable")
    if not version:
        return None
    manifest = repository.manifest(name, version)
    return {"name": name, "label": manifest.label, "category": manifest.category, "ui": _ui(manifest),
            "locked_version": version, "latest_available": None, "channel": "stable", "status": "disabled",
            "pending": None, "enabled_at": None, "actions": ["enable"], "rollback_to": None, "follow": True}


def _revert_pending(manager, project_id: str, name: str, actor: str) -> bool:
    """待生效的切换正好是这个包最近一次插件变更集、而且之后锁文件没再改过：撤回这个变更集（反向提交）。
    撤不了（之后又切换过别的包、助手的修改还没确认）返回 False，由调用方按生效中的版本改回。"""
    from ...services.workspace.errors import WorkspaceError
    from . import changes

    from ...kernel.changesets import store

    record = changes.latest_for(manager, project_id, name)
    if record is None:
        return False
    runtime = manager.packs
    root = manager.project_root(project_id)
    with manager._project_lock(project_id):
        if store.current(root) is not None:
            # 助手的修改还没确认：回退类写入一律 423（B4b 口径），交给调用方按生效中的版本改回，改流程的那条路回 423
            return False
        try:
            changes.revert(manager, project_id, record, actor=actor)
        except WorkspaceError as exc:
            _log.info("项目 %s 撤销 %s 没有走变更集撤回：%s", project_id, name, exc)
            return False
        active_before = (runtime.assembler.active(root) or {}).get("assembly_hash")
        try:
            status = runtime.apply(root, h0_only=True)
        except AssemblyError:
            status = runtime.status(root)
    announce_immediate_switch(project_id, active_before, status)
    return True


def assembly_view(manager, project_id: str) -> dict:
    """PL-11：装配状态。"""
    runtime = manager.packs
    root = manager.project_root(project_id)
    runtime.ensure_lock(root)
    status = runtime.status(root)
    error = status["last_error"]
    return {
        "active_hash": status["active_hash"],
        "pending_hash": status["pending_hash"],
        "lock_hash": status["lock_hash"],
        "flow_hash": status["flow_hash"],
        "state": status["state"],
        "pending_changes": status["pending_changes"],
        "effective": "next_message" if status["effective"] == "next_message" else "immediate",
        "last_error": {"error_id": error.get("error_id"), "message": error.get("message"),
                       "at": error.get("at")} if error else None,
    }


def _review_after(root, flow: dict | None) -> list[str]:
    """换成这一版以后会标「需复核」的节点：已确认过、而完成判定签名变了的（详设 6.9 第 6 步，与节点状态同一判据）。"""
    from ..flow import state as node_state

    if not flow:
        return []
    stored = node_state.load(root)["nodes"]
    out = []
    for node in flow.get("nodes") or []:
        record = stored.get(node["id"]) or {}
        if record.get("sig") and not record.get("needs_review") and record["sig"] != node_state.done_signature(flow, node):
            out.append(node["id"])
    return out


def compatibility(manager, name: str, version: str, project_id: str) -> dict:
    """PL-03：把这个版本放进本项目试装配一遍，逐项给出兼容结论（不改锁文件、不留装配目录）。"""
    runtime = manager.packs
    repository = runtime.repository
    if not repository.versions(name):
        raise PackError("pack_not_found", f"包 {name} 不存在", 404)
    if not repository.has(name, version):
        raise PackError("pack_version_not_found", f"包 {name} 没有版本 {version}", 404)
    root = manager.project_root(project_id)
    manifest = repository.manifest(name, version)
    lock, _ = runtime.ensure_lock(root)
    trial = (lock or PackLock()).copy()
    trial.upsert(runtime.entry_for(name, version))
    versions = {entry.name: entry.version for entry in trial.enabled()}
    engine = manager.get_project(project_id).get("engine") or "claude"
    checks: list[dict] = []

    def add(check_id: str, label: str, ok: bool, detail: str = "") -> None:
        checks.append({"id": check_id, "label": label, "ok": ok, "detail": detail})

    add("kernel_api", "内核接口版本", mf.satisfies(mf.KERNEL_API, manifest.kernel_api),
        f"需要 {manifest.kernel_api}，当前 {mf.KERNEL_API}")
    add("engines", "会话引擎", engine in manifest.engines, f"本项目用 {engine}，包支持 {'、'.join(manifest.engines)}")
    bad = [f"{dep} {spec}（本项目 {versions.get(dep, '未启用')}）" for dep, spec in manifest.depends.items()
           if dep not in versions or not mf.satisfies(versions[dep], spec)]
    # 升级 core 时，已启用的可选包对 core 的依赖范围也要满足（详设 8.1「包之间的依赖」）
    for entry in trial.enabled():
        if entry.name == name or not repository.has(entry.name, entry.version):
            continue
        other = repository.manifest(entry.name, entry.version)
        spec = other.depends.get(name)
        if spec and not mf.satisfies(version, spec):
            bad.append(f"{entry.name} 需要 {name} {spec}")
    missing = _missing_providers(repository, name, version)
    bad += [f"需要的连接器不可用：{'、'.join(missing)}"] if missing else []
    add("depends", "依赖", not bad, "；".join(bad))
    names_ok, flow_ok, detail_names, detail_flow = True, True, "", ""
    trial_flow = None
    if not bad:
        try:
            plan = runtime.assembler.plan(trial)
            runtime.assembler.check(plan)
            trial_flow = runtime.flow_of(plan)
        except AssemblyError as exc:
            if exc.reason == "flow":
                flow_ok, detail_flow = False, "；".join(exc.problems)
            else:
                names_ok, detail_names = False, "；".join(exc.problems)
    add("names", "命令、子代理、脚本名字唯一", names_ok, detail_names)
    add("flow", "流程组装", flow_ok, detail_flow)
    add("page_api", "平台页面接口", not manifest.page_api or mf.satisfies(mf.PAGE_API, manifest.page_api),
        f"需要 {manifest.page_api or '不限'}，平台 {mf.PAGE_API}")
    ui = _ui(manifest)
    ui_effect = {"kind": (ui or {}).get("kind", "none"), "text": (ui or {}).get("note", "")}
    brings = {
        "summary_md": manifest.description,
        "commands": len(manifest.items("commands")), "agents": len(manifest.items("agents")),
        "skills": len(manifest.items("skills")), "nodes": [{"id": node, "title": node, "placement": ""} for node in manifest.items("nodes")],
        "pages": [{"id": page.get("id"), "title": (page.get("nav") or {}).get("title") or page.get("id")} for page in manifest.pages()],
        "subpages": [sub for sub in manifest.contributes.get("subpages") or [] if isinstance(sub, dict)],
    }
    ok = all(check["ok"] for check in checks)
    return {
        "ok": ok, "checks": checks, "brings": brings, "ui_effect": ui_effect,
        "effective": manifest.effective if manifest.effective != "restart" else "next_message",
        "impact": {"needs_review_after_upgrade": len(_review_after(root, trial_flow)), "affected_artifacts": 0,
                   "note": None},
        "blocked_reason": None if ok else next(check["detail"] or check["label"] for check in checks if not check["ok"]),
    }

