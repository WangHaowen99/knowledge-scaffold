"""agent 提交收口（方案 C11；详设 4.3「agent 提交收口」、P2 验收「写入都带来源 trailer」）。

agent 仍在工作区里自己 `git commit`（变更集副本在 B4b），内核在提交接口上收口：各项目的 git
`core.hooksPath` 指向数据目录里内核发布的钩子（`<数据目录>/githooks`，随镜像更新、agent 只读）：

- `pre-commit`：跑内核守卫里的提交检查（`.claude/scripts/pre_commit_check.sh`），看到的是提交那一刻
  的真实暂存区，`git add -A && git commit` 写在一行也查得到；检查出错一律拒绝；
- `commit-msg`：写来源 trailer（Onto-Source: agent、Onto-Author、Onto-Packs、Onto-Node），agent
  自己写的 Onto-* 行去掉。

钩子只管 agent 的提交（命令里带会话身份 ONTO_RUN_ENGINE，方案 B7）。后端自己的 git 命令一律带
`-c core.hooksPath=/dev/null`（gitops._run_git），写入事务用 commit-tree，都不经钩子。
绕过写法（`--no-verify`、改 hooksPath）由 PreToolUse 的提交检查拦；防不住有意绕过，真正改不了靠
进程隔离（详设 4.8 第 4 层）。

agent 以独立用户运行时（方案 E10），它的提交要往主线仓库的对象库里写：仓库设成按组共享
（`core.sharedRepository=group`），之后不论谁、以什么 umask 跑 git，新建的目录都对 agent 组可写；
之前已经写不进的目录在装钩子时一并修好。
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from pathlib import Path

from ... import config
from ...services.workspace.gitops import _run_git

_log = logging.getLogger(__name__)

SOURCE_DIR = Path(__file__).resolve().parents[2] / "kernel_guards" / "githooks"
HOOK_NAMES = ("pre-commit", "commit-msg")


def hooks_dir() -> Path:
    return Path(config.DATA_DIR) / "githooks"


def prepare() -> Path:
    """把镜像里的钩子发布到数据目录（内容变了才换，逐个文件原子替换），agent 只读可执行。"""
    target = hooks_dir()
    target.mkdir(parents=True, exist_ok=True)
    for name in HOOK_NAMES:
        source = SOURCE_DIR / name
        dest = target / name
        data = source.read_bytes()
        if dest.is_file() and dest.read_bytes() == data and os.access(dest, os.X_OK):
            continue
        tmp = target / f".{name}.{uuid.uuid4().hex[:8]}.tmp"
        tmp.write_bytes(data)
        os.chmod(tmp, 0o755)
        os.replace(tmp, dest)
    os.chmod(target, 0o755)
    try:
        from ...governance import agent_identity

        agent_identity.share_read_only(target)
    except Exception:  # noqa: BLE001 - 权限整理失败只影响 agent 用户能否执行，记日志
        _log.warning("钩子目录权限整理失败：%s", target)
    return target


def installed(root: Path) -> bool:
    # --local：后端的 git 命令都带 -c core.hooksPath=/dev/null，不加它读到的是那个覆盖值
    result = _run_git(Path(root), ("config", "--local", "--get", "core.hooksPath"))
    return result.returncode == 0 and result.stdout.strip() == str(hooks_dir())


def share_with_agent(root: Path) -> bool:
    """agent 以独立用户运行时：仓库按组共享，写不进的目录修好。返回是否改了什么。"""
    from ...governance import agent_identity

    if not agent_identity.switching():
        return False
    changed = False
    current = _run_git(root, ("config", "--local", "--get", "core.sharedRepository"))
    if current.returncode != 0 or current.stdout.strip() != "group":
        result = _run_git(root, ("config", "--local", "core.sharedRepository", "group"))
        if result.returncode != 0:
            _log.warning("项目 %s 设置按组共享失败：%s", root.name, result.stderr.strip())
        changed = result.returncode == 0
    fixed = agent_identity.share_git_dir(root / ".git")
    if fixed:
        _log.info("项目 %s 的版本库有 %d 处 agent 写不进，已修好", root.name, fixed)
    return changed or fixed > 0


def install(root: Path) -> bool:
    """项目的 git 指向内核钩子、对 agent 组可写；已经这样就不动。返回是否改了什么。"""
    root = Path(root)
    if not (root / ".git").is_dir():
        return False
    shared = share_with_agent(root)
    if not (hooks_dir() / HOOK_NAMES[0]).is_file():
        prepare()
    if installed(root):
        return shared
    result = _run_git(root, ("config", "--local", "core.hooksPath", str(hooks_dir())))
    if result.returncode != 0:
        _log.warning("项目 %s 设置提交钩子失败：%s", root.name, result.stderr.strip())
        return False
    return True


def install_all(manager) -> dict | None:
    """启动恢复步骤：发布钩子并装到每个项目（只做事，不进报告；失败的项目记日志）。"""
    prepare()
    failed = []
    for root in manager._project_dirs():
        try:
            install(root)
        except (OSError, ValueError) as exc:
            failed.append(root.name)
            _log.warning("项目 %s 设置提交钩子失败：%s", root.name, exc)
    if failed:
        return {"id": "agent_commit_hooks", "title": "agent 提交钩子", "result": "failed", "tone": "warn",
                "detail": f"{len(failed)} 个项目没装上提交钩子，agent 的提交检查退回 PreToolUse",
                "projects": [{"project_id": name, "count": 1, "paths": []} for name in failed]}
    return None


def remove_installed_copy() -> None:
    """测试用：删掉数据目录里的钩子。"""
    shutil.rmtree(hooks_dir(), ignore_errors=True)
