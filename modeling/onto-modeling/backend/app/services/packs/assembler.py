"""装配器（详设 8.1；方案 D2）：内核守卫 + 已启用的包 → 按内容寻址的装配目录。

    <数据目录>/assembled/<哈希>/
      CLAUDE.md          core 的工作区规则 + 各包的规则片段
      README.md          core 的工作区说明
      commands/ agents/ skills/ scripts/ templates/ reference/ …   各包原样复制（不用软链：
                         有 7 个脚本按 __file__ 找同目录模块，逐文件软链会解析错）
      scripts/*_guard.sh、_guard_common.py …   内核守卫，包不能同名覆盖
      settings.json      内核权限与 hooks 基线 + 各包追加的拒绝型 PreToolUse
      flow.yaml          组装后的流程
      MANIFEST.json      包与版本、各文件哈希（最后写入）

- 原子：先在 assembled/.tmp-* 里装好、写完 MANIFEST.json，再 rename 成哈希目录；中途停机只留下
  临时目录，启动时清掉，项目的 `.claude` 仍指向旧的完整目录。
- 切换：先建临时软链再 os.replace 覆盖 `.claude`，任何时刻 `.claude` 要么指向旧目录、要么指向新目录。
- 哈希：内核守卫摘要 + 启用的包（名、版本、包摘要），锁文件相同的项目共用一个装配目录。
  另算一个只含 H1 内容的哈希：只改了纯数据包（H0）时，会话指纹不变、不用重连。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import yaml

from ... import kernel_guards
from ..flow import assemble as flow_assemble
from . import manifest as mf
from .lockfile import LockEntry, PackLock
from .manifest import Manifest
from .repository import PackRepository, file_sha256, pack_files, verify_checksums

MANIFEST_JSON = "MANIFEST.json"
ASSEMBLY_FORMAT = 1
CORE = "core"


class AssemblyError(Exception):
    """装配失败：项目保持原版本（详设 8.1 第 3 步）。"""

    code = "assembly_failed"

    def __init__(self, reason: str, problems: list[str]):
        super().__init__("；".join(problems))
        self.reason = reason
        self.problems = problems


@dataclass
class Plan:
    lock: PackLock
    packs: list[tuple[LockEntry, Manifest, Path]]
    kernel: str
    hash: str
    h1_hash: str


def _digest(kernel: str, entries: list[tuple[str, str, str]]) -> str:
    body = {"format": ASSEMBLY_FORMAT, "kernel": kernel, "packs": sorted(entries)}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _copy_file(source: Path, dest: Path) -> None:
    """测试替换点：装配中途杀进程的恢复测试在这里模拟停机。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)


def _fsync_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


def _replace_link(path: Path, target: str) -> None:
    """原子替换软链：临时软链 + os.replace。原来是真实文件或目录时先挪开（不删用户内容）。"""
    if os.path.islink(path):
        if os.readlink(path) == target:
            return
    elif path.exists():
        path.rename(path.with_name(f"{path.name}.legacy-{int(time.time())}"))
    tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex[:8]}")
    os.symlink(target, tmp)
    try:
        os.replace(tmp, path)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)


class Assembler:
    def __init__(self, data_dir: Path, repository: PackRepository):
        self.root = Path(data_dir) / "assembled"
        self.repository = repository

    # ---- 计划 ----

    def plan(self, lock: PackLock) -> Plan:
        """按锁文件定出要装配的包与哈希；包缺失、摘要不符、兼容不过时抛 AssemblyError。"""
        problems: list[str] = []
        enabled = sorted(lock.enabled(), key=lambda entry: (entry.name != CORE, entry.name))
        versions = {entry.name: entry.version for entry in enabled}
        if CORE not in versions:
            problems.append("缺少必装的基础建模能力（core）")
        packs: list[tuple[LockEntry, Manifest, Path]] = []
        full: list[tuple[str, str, str]] = []
        h1: list[tuple[str, str, str]] = []
        for entry in enabled:
            if not self.repository.has(entry.name, entry.version):
                problems.append(f"包仓库里没有 {entry.name} {entry.version}")
                continue
            manifest = self.repository.manifest(entry.name, entry.version)
            digest = self.repository.digest(entry.name, entry.version)
            if entry.sha256 and entry.sha256 != digest:
                problems.append(f"{entry.name} {entry.version} 的内容与锁文件记录的摘要不一致")
            if not mf.satisfies(mf.KERNEL_API, manifest.kernel_api):
                problems.append(f"{entry.name} {entry.version} 需要内核接口 {manifest.kernel_api}，当前是 {mf.KERNEL_API}")
            for dep, spec in manifest.depends.items():
                if dep not in versions or not mf.satisfies(versions[dep], spec):
                    problems.append(f"{entry.name} 依赖 {dep} {spec}，本项目是 {versions.get(dep, '未启用')}")
            if manifest.hot_level == "H2":
                problems.append(f"{entry.name} 是可替换实现，要由管理员安装到平台，不能按项目启用")
            packs.append((entry, manifest, self.repository.version_dir(entry.name, entry.version)))
            full.append((entry.name, entry.version, digest))
            if manifest.hot_level != "H0":
                h1.append((entry.name, entry.version, digest))
        if problems:
            raise AssemblyError("plan", problems)
        kernel = kernel_guards.digest()
        return Plan(lock=lock, packs=packs, kernel=kernel, hash=_digest(kernel, full), h1_hash=_digest(kernel, h1))

    # ---- 构建 ----

    def dir_for(self, digest: str) -> Path:
        return self.root / digest

    def build(self, plan: Plan) -> Path:
        target = self.dir_for(plan.hash)
        if (target / MANIFEST_JSON).is_file():
            return target
        self.root.mkdir(parents=True, exist_ok=True)
        staging = self.root / f".tmp-{plan.hash}-{uuid.uuid4().hex[:8]}"
        staging.mkdir()
        try:
            self._fill(plan, staging)
            try:
                os.rename(staging, target)
            except OSError:
                # 并发装配同一个哈希：别人先装好了就用别人的
                if not (target / MANIFEST_JSON).is_file():
                    raise
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        return target

    def _fill(self, plan: Plan, staging: Path) -> None:
        owners: dict[str, str] = {}
        flows: list[tuple[str, str, dict]] = []
        rules: list[str] = []
        readme = None
        extra_hooks: list[dict] = []
        for entry, manifest, source in plan.packs:
            problems = verify_checksums(source)
            if problems:
                raise AssemblyError("tampered", [f"包仓库里 {entry.name} {entry.version} 的内容被改过：{problems[0]}"])
            flow_rel = manifest.contributes.get("flow") or ""
            readme_rel = manifest.contributes.get("readme") or ""
            special = {mf.MANIFEST_FILE, flow_rel, readme_rel, *manifest.items("rules")}
            for rel in pack_files(source):
                if rel in special:
                    continue
                if rel in owners:
                    raise AssemblyError("conflict", [f"{rel} 同时来自 {owners[rel]} 和 {entry.name}"])
                owners[rel] = entry.name
                _copy_file(source / rel, staging / rel)
            for rel in manifest.items("rules"):
                if (source / rel).is_file():
                    rules.append((source / rel).read_text(encoding="utf-8").rstrip() + "\n")
            if entry.name == CORE and readme_rel and (source / readme_rel).is_file():
                readme = (source / readme_rel).read_text(encoding="utf-8")
            if flow_rel and (source / flow_rel).is_file():
                flows.append((entry.name, entry.version, yaml.safe_load((source / flow_rel).read_text(encoding="utf-8")) or {}))
            extra_hooks += manifest.hooks()
        for script in kernel_guards.scripts():
            rel = f"scripts/{script.name}"
            if rel in owners:
                raise AssemblyError("conflict", [f"{rel} 是内核守卫，包不能覆盖（来自 {owners[rel]}）"])
            owners[rel] = "kernel"
            _copy_file(script, staging / rel)
        _fsync_write(staging / "settings.json", json.dumps(self._settings(extra_hooks), ensure_ascii=False, indent=2) + "\n")
        _fsync_write(staging / "CLAUDE.md", "\n".join(rules))
        if readme is not None:
            _fsync_write(staging / "README.md", readme)
        flow = None
        if flows:
            try:
                flow = flow_assemble.assemble(flows)
            except flow_assemble.FlowAssemblyError as exc:
                raise AssemblyError("flow", exc.problems) from exc
            _fsync_write(staging / "flow.yaml", flow_assemble.render(flow))
        files = {
            path.relative_to(staging).as_posix(): file_sha256(path)
            for path in sorted(staging.rglob("*")) if path.is_file()
        }
        manifest_json = {
            "format": ASSEMBLY_FORMAT,
            "assembly_hash": plan.hash,
            "h1_hash": plan.h1_hash,
            "kernel": plan.kernel,
            "kernel_api": mf.KERNEL_API,
            "flow_hash": flow["flow_hash"] if flow else None,
            "packs": [
                {"name": entry.name, "version": entry.version, "sha256": entry.sha256 or self.repository.digest(entry.name, entry.version),
                 "kind": manifest.kind, "hot_level": manifest.hot_level}
                for entry, manifest, _ in plan.packs
            ],
            "lock": plan.lock.to_dict(),
            "files": files,
            "built_at": time.time(),
        }
        _fsync_write(staging / MANIFEST_JSON, json.dumps(manifest_json, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    @staticmethod
    def _settings(extra_hooks: list[dict]) -> dict:
        settings = kernel_guards.settings_base()
        pre = settings.setdefault("hooks", {}).setdefault("PreToolUse", [])
        for hook in extra_hooks:
            # 包追加的守卫与内核守卫一样：脚本缺失或出错一律拒绝
            pre.append({
                "matcher": str(hook.get("matcher") or "Bash|Edit|Write|MultiEdit|NotebookEdit"),
                "hooks": [{"type": "command", "command": f"{hook['command']} || exit 2"}],
            })
        return settings

    # ---- 项目侧 ----

    def active(self, project_root: Path) -> dict | None:
        """项目 `.claude` 当前指向的装配目录的 MANIFEST（带 dir）；不是装配目录返回 None。"""
        link = Path(project_root) / ".claude"
        if not os.path.lexists(link):
            return None
        real = Path(os.path.realpath(link))
        try:
            if real.parent != self.root.resolve():
                return None
            data = json.loads((real / MANIFEST_JSON).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return {**data, "dir": real} if isinstance(data, dict) else None

    def switch(self, project_root: Path, assembled: Path) -> None:
        """原子切换 `.claude`；CLAUDE.md、README.md 是指向 .claude 里的相对链接，只建一次。"""
        root = Path(project_root)
        _replace_link(root / ".claude", os.path.relpath(assembled, root))
        _replace_link(root / "CLAUDE.md", ".claude/CLAUDE.md")
        if (assembled / "README.md").is_file():
            _replace_link(root / "README.md", ".claude/README.md")

    def verify(self, directory: Path) -> list[str]:
        """按 MANIFEST.json 核对装配目录（受保护目录第 2 层：内容被改过能发现）。"""
        directory = Path(directory)
        try:
            files = json.loads((directory / MANIFEST_JSON).read_text(encoding="utf-8"))["files"]
        except (OSError, ValueError, KeyError):
            return ["装配目录缺少 MANIFEST.json"]
        problems = []
        actual = {path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()}
        for rel, digest in files.items():
            path = directory / rel
            if not path.is_file():
                problems.append(f"{rel} 缺失")
            elif file_sha256(path) != digest:
                problems.append(f"{rel} 被改过")
        problems += [f"{rel} 是多出来的文件" for rel in sorted(actual - set(files) - {MANIFEST_JSON})
                     if "__pycache__" not in rel]
        return problems

    def verified_file(self, project_root: Path, rel: str) -> Path | None:
        return verified_file(project_root, rel)

    def discard(self, digest: str) -> None:
        """删掉一个装配目录（内容被改过时重新装配用）。"""
        target = self.dir_for(digest)
        if target.is_dir():
            trash = self.root / f".tmp-discard-{uuid.uuid4().hex[:8]}"
            os.rename(target, trash)
            shutil.rmtree(trash, ignore_errors=True)

    def cleanup_staging(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        removed = []
        for entry in self.root.glob(".tmp-*"):
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry)
        return removed


def verified_file(project_root: Path, rel: str) -> Path | None:
    """项目装配目录里哈希与 MANIFEST 一致的文件；`.claude` 不是装配目录或文件被改过返回 None。"""
    link = Path(project_root) / ".claude"
    if not os.path.lexists(link):
        return None
    real = Path(os.path.realpath(link))
    if real.parent.name != "assembled":
        return None
    try:
        files = json.loads((real / MANIFEST_JSON).read_text(encoding="utf-8")).get("files") or {}
    except (OSError, ValueError):
        return None
    path = real / rel
    expected = files.get(rel)
    if not expected or not path.is_file() or file_sha256(path) != expected:
        return None
    return path


def trusted_script(project_root: Path, name: str, assets_dir: Path) -> Path:
    """后端要执行的工作区脚本（方案 D19）：本项目锁定版本的装配目录里、哈希核对过的那份；
    没有装配目录或被改过时用镜像里的那份，不跑工作区里可被改写的副本。"""
    verified = verified_file(project_root, f"scripts/{name}")
    return verified or Path(assets_dir) / "workspace-toolkit" / "scripts" / name
