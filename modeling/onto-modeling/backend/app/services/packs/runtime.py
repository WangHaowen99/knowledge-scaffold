"""项目侧的插件运行时：锁文件 → 装配目录 → `.claude`，以及生效时机（详设 3.5、8.1）。

只做文件操作，不碰 git；要提交锁文件的操作在 service.py（持项目锁、按路径提交）。

生效时机：
- 锁文件改了，装配目录先装好（失败就不改锁文件），但 `.claude` 不马上切：
  - 只动了纯数据包（H0，会话指纹不变）：马上切，下次读取就生效；
  - 动了能力包（H1）：等下一条消息前由会话宿主切换并重连，正在跑的一轮跑完旧版本；
- 建项目、导入、启动核对时没有会话在跑，直接切。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path

from . import builtin, lockfile
from .assembler import AssemblyError, Assembler, Plan
from .lockfile import LockEntry, PackLock
from .repository import PackRepository

_log = logging.getLogger(__name__)
STATE_REL = ".onto/run/assembly.json"
CORE = builtin.CORE


class PackRuntime:
    def __init__(self, data_dir: Path, assets_dir: Path):
        self.data_dir = Path(data_dir)
        self.assets_dir = Path(assets_dir)
        self.repository = PackRepository(self.data_dir)
        self.assembler = Assembler(self.data_dir, self.repository)
        # 提交 trailer 的 Onto-Packs：有插件运行时就登记（同一个函数只登记一次）
        from ...kernel.writepath import register_trailer_provider
        from .governance import packs_trailer

        register_trailer_provider(packs_trailer)

    # ---- 官方包与默认锁 ----

    def core_version(self) -> str | None:
        """镜像自带的 core 版本（第一次调用时入库，连同 assets/packs 下的官方包）；没有工具箱返回 None。"""
        version = builtin.import_core(self.repository, self.assets_dir)
        if version is not None:
            builtin.import_packs(self.repository, self.assets_dir)
        return version

    # ---- 拆包（方案 D7 后半）----

    def _stable_manifest(self, name: str):
        version = self.repository.latest(name, "stable")
        return (version, self.repository.manifest(name, version)) if version else (None, None)

    def split_packs(self) -> list[tuple[str, str]]:
        """从 core 拆出去的官方包（清单 split_from_core）的 stable 版本。"""
        out = []
        for name in self.repository.names():
            if name == CORE:
                continue
            version, manifest = self._stable_manifest(name)
            if manifest is not None and manifest.raw.get("split_from_core") and manifest.official:
                out.append((name, version))
        return out

    def default_capabilities(self) -> list[str]:
        """新建项目缺省启用的能力包：清单 default_on 的官方能力包（行业包按项目选，不在这里）。"""
        out = []
        for name in self.repository.names():
            if name == CORE:
                continue
            version, manifest = self._stable_manifest(name)
            if manifest is not None and manifest.category == "capability" and manifest.raw.get("default_on") \
                    and manifest.hot_level != "H2":
                out.append(name)
        return out

    def _core_is_split(self, version: str) -> bool:
        from .manifest import satisfies

        return satisfies(version, ">=0.2.0")

    def _migrate_split(self, lock: PackLock) -> bool:
        """锁文件版本 1、core 已经是拆分后的版本：补上从 core 拆出去的包（存量项目的能力不变）。"""
        core = lock.get(CORE)
        if lock.lock_version >= lockfile.LOCK_VERSION or core is None or not self._core_is_split(core.version):
            return False
        for name, version in self.split_packs():
            if lock.get(name) is None:
                lock.upsert(self.entry_for(name, version))
        lock.lock_version = lockfile.LOCK_VERSION
        return True

    def entry_for(self, name: str, version: str, *, channel: str = "stable", follow: bool = True) -> LockEntry:
        return LockEntry(name=name, version=version, sha256=self.repository.digest(name, version),
                         channel=channel, follow=follow, enabled=True, enabled_at=time.time())

    def default_lock(self, selection: list[str] | None = None) -> PackLock | None:
        """新锁：core + 选中的包（缺省是 default_on 的能力包）；选中的包不在仓库里的跳过。"""
        if self.core_version() is None:
            return None
        stable = self.repository.latest(CORE, "stable")
        if not stable:
            return None
        lock = PackLock([self.entry_for(CORE, stable)])
        for name in (self.default_capabilities() if selection is None else selection):
            version = self.repository.latest(name, "stable")
            if name != CORE and version and lock.get(name) is None:
                lock.upsert(self.entry_for(name, version))
        return lock

    def normalize(self, lock: PackLock) -> tuple[PackLock, bool]:
        """锁里的版本在本仓库不存在（别的部署导出来的项目）或摘要不符：换成通道版本并跟随。"""
        lock = lock.copy()
        changed = False
        for entry in list(lock.packs):
            known = self.repository.has(entry.name, entry.version)
            digest = self.repository.digest(entry.name, entry.version) if known else ""
            if known and entry.sha256 in ("", digest):
                if not entry.sha256:
                    entry.sha256, changed = digest, True
                continue
            latest = self.repository.latest(entry.name, entry.channel or "stable")
            if latest is None:
                lock.packs.remove(entry)
            else:
                lock.upsert(self.entry_for(entry.name, latest, channel=entry.channel or "stable"))
                lock.get(entry.name).enabled = entry.enabled
            changed = True
        core = lock.get(CORE)
        if core is None or not core.enabled:
            stable = self.repository.latest(CORE, "stable")
            if stable:
                lock.upsert(self.entry_for(CORE, core.version if core and self.repository.has(CORE, core.version) else stable))
                changed = True
        changed = self._migrate_split(lock) or changed
        return lock, changed

    def ensure_lock(self, root: Path, selection: list[str] | None = None) -> tuple[PackLock | None, bool]:
        """读出并补齐项目锁文件（没有就按 stable 写一份，selection 是新建时选的包），返回（锁, 是否写过文件）。不提交。"""
        if self.core_version() is None:
            return None, False
        try:
            lock = lockfile.read(root)
        except (ValueError, OSError) as exc:
            _log.warning("项目 %s 的锁文件坏了，按 stable 重写：%s", Path(root).name, exc)
            lock = None
        if lock is None:
            lock = self.default_lock(selection)
            if lock is None:
                return None, False
            return lock, lockfile.write(root, lock)
        lock, changed = self.normalize(lock)
        return lock, (lockfile.write(root, lock) if changed else False)

    def follow(self, root: Path) -> bool:
        """跟随通道的包：通道指针动了就把锁改到通道版本（方案 2.3 第 4 步）。返回是否改了锁。"""
        try:
            lock = lockfile.read(root)
        except (ValueError, OSError):
            return False
        if lock is None:
            return False
        changed = False
        for entry in lock.packs:
            if not entry.follow or not entry.enabled:
                continue
            latest = self.repository.latest(entry.name, entry.channel or "stable")
            if latest and latest != entry.version:
                entry.version = latest
                entry.sha256 = self.repository.digest(entry.name, latest)
                changed = True
        changed = self._migrate_split(lock) or changed
        return lockfile.write(root, lock) if changed else False

    # ---- 装配与切换 ----

    def plan(self, root: Path, lock: PackLock | None = None) -> Plan:
        lock = lock if lock is not None else lockfile.read(root)
        if lock is None:
            raise AssemblyError("plan", ["项目还没有能力包锁文件"])
        return self.assembler.plan(lock)

    def build(self, lock: PackLock) -> tuple[Plan, Path]:
        plan = self.assembler.plan(lock)
        return plan, self.assembler.build(plan)

    def apply(self, root: Path, *, h0_only: bool = False) -> dict:
        """按锁文件装配并切换 `.claude`；h0_only 时只在不涉及 H1 变化时切（见模块说明）。"""
        root = Path(root)
        try:
            plan = self.plan(root)
            target = self.assembler.build(plan)
        except AssemblyError as exc:
            self._record_error(root, exc)
            raise
        active = self.assembler.active(root)
        if active and active.get("assembly_hash") == plan.hash:
            self._record_error(root, None)
            return self.status(root)
        if h0_only and active and active.get("h1_hash") != plan.h1_hash:
            return self.status(root)
        self.assembler.switch(root, target)
        self._record_error(root, None)
        return self.status(root)

    def mount(self, root: Path, selection: list[str] | None = None) -> dict | None:
        """建项目、导入、克隆：补齐锁文件并立即装配切换（没有会话在跑）。工具箱缺席时返回 None。"""
        lock, _ = self.ensure_lock(root, selection)
        if lock is None:
            return None
        return self.apply(root)

    def reconcile(self, root: Path, verified: set | None = None) -> dict | None:
        """启动核对（方案 D4）：`.claude` 指向的装配目录在不在、内容对不对、和锁文件一致不一致。

        装配目录被改过就删掉重装；`.claude` 还是旧版绝对链接、指错或缺失都按锁文件重新切。
        verified：本轮已核对过的装配哈希（锁文件相同的项目共用一个装配目录，不必逐个项目重算）。
        """
        root = Path(root)
        verified = verified if verified is not None else set()
        lock, _ = self.ensure_lock(root)
        if lock is None:
            return None
        active = self.assembler.active(root)
        if active and active.get("assembly_hash") not in verified:
            if self.assembler.verify(Path(active["dir"])):
                _log.warning("项目 %s 的装配目录内容被改过，重新装配：%s", root.name, active.get("assembly_hash"))
                self.assembler.discard(active["assembly_hash"])
            else:
                verified.add(active.get("assembly_hash"))
        plan = self.plan(root, lock)
        built = self.assembler.dir_for(plan.hash)
        if plan.hash not in verified and built.is_dir():
            if self.assembler.verify(built):
                self.assembler.discard(plan.hash)
            else:
                verified.add(plan.hash)
        return self.apply(root)

    # ---- 状态 ----

    def _state_path(self, root: Path) -> Path:
        return Path(root) / STATE_REL

    def _record_error(self, root: Path, exc: AssemblyError | None) -> None:
        path = self._state_path(root)
        if exc is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        error_id = f"err-{uuid.uuid4().hex[:6]}"
        path.write_text(json.dumps({
            "last_error": {"error_id": error_id, "reason": exc.reason, "message": str(exc), "at": time.time()},
        }, ensure_ascii=False), encoding="utf-8")

    def last_error(self, root: Path) -> dict | None:
        try:
            return json.loads(self._state_path(root).read_text(encoding="utf-8")).get("last_error")
        except (OSError, ValueError):
            return None

    def status(self, root: Path) -> dict:
        """契约 PL-11 的原始数据：生效中的装配、锁文件要求的装配、待生效的变化。"""
        root = Path(root)
        active = self.assembler.active(root)
        try:
            lock = lockfile.read(root)
        except (ValueError, OSError):
            lock = None
        plan = None
        error = self.last_error(root)
        if lock is not None:
            try:
                plan = self.assembler.plan(lock)
            except AssemblyError as exc:
                error = error or {"error_id": None, "reason": exc.reason, "message": str(exc), "at": time.time()}
        active_hash = active.get("assembly_hash") if active else None
        pending_hash = plan.hash if plan and plan.hash != active_hash else None
        if plan is None and lock is not None:
            state = "failed"
        elif pending_hash:
            state = "failed" if error else "pending"
        else:
            state = "applied"
        active_packs = {item["name"]: item["version"] for item in (active or {}).get("packs") or []}
        wanted = {entry.name: entry.version for entry in lock.enabled()} if lock else {}
        changes = []
        for name in sorted(set(active_packs) | set(wanted)):
            before, after = active_packs.get(name), wanted.get(name)
            if before == after:
                continue
            kind = "enable" if before is None else "disable" if after is None else (
                "upgrade" if _newer(after, before) else "rollback")
            changes.append({"pack": name, "from": before, "to": after, "kind": kind})
        h1_pending = bool(plan and active and plan.h1_hash != active.get("h1_hash")) or (plan is not None and not active)
        return {
            "active_hash": active_hash,
            "active_h1_hash": active.get("h1_hash") if active else None,
            "pending_hash": pending_hash,
            "lock_hash": lock.hash() if lock else None,
            "flow_hash": (active or {}).get("flow_hash"),
            "state": state,
            "pending_changes": changes,
            "effective": "next_message" if h1_pending else "immediate",
            "last_error": error if state == "failed" else None,
            "desired_hash": plan.hash if plan else None,
            "desired_h1_hash": plan.h1_hash if plan else None,
        }


def _newer(a: str, b: str) -> bool:
    from .manifest import version_key

    return version_key(a) >= version_key(b)
