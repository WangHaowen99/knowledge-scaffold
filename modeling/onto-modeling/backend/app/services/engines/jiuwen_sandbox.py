"""内测引擎文件沙箱：助手副本顶层的 `resource` 链接只读放行（F2 第 9 条）。

助手副本里的 `resource` 是指向主线资料区的链接（kernel/changesets/worktree：资料区受保护，各副本共享）。
openjiuwen 的文件沙箱按 resolve 之后的路径判定，链接目标在主线工作区、不在沙箱三根里，read_file 读资料报
「Path … outside sandbox」，助手只好绕去 bash。这里给文件工具补一条只读的放行：

- 只认沙箱第一根（项目区）是助手副本、顶层 `resource` 是链接、而且指向这个副本对应的主线资料区；放行的只是
  这个目录（resolve 之后落在它里面才放），资料区里指向别处的链接照旧越界；
- 只有读（读文件、列目录、找文件、Grep）放行；写文件、上传与其余命令照旧按沙箱三根判定；
- 别的判定原样交给框架。

和 agent_identity.install_jiuwen_shell_isolation 一样包在框架类上，幂等；建 agent 之前调一次。
"""

from __future__ import annotations

import functools
import os
from contextvars import ContextVar
from pathlib import Path

# 正在读（只读的文件操作与 Grep 工具里）：只有这时资料链接的目标才放行
_reading: ContextVar[bool] = ContextVar("onto_jiuwen_fs_reading", default=False)
_READ_METHODS = ("read_file", "list_files", "list_directories", "search_files")
_installed = False


def readable_target(run_config) -> Path | None:
    """沙箱第一根是助手副本、顶层 resource 链接指向它的主线资料区时，返回资料区（resolve 后）；否则 None。"""
    from ...kernel.changesets import worktree

    roots = getattr(run_config, "sandbox_root", None) or []
    if not roots or not getattr(run_config, "restrict_to_sandbox", False):
        return None
    copy = Path(roots[0])
    link = copy / "resource"
    if not worktree.is_copy(copy) or not link.is_symlink():
        return None
    target = link.resolve()
    expected = (worktree.main_root(copy) / "resource").resolve()
    return target if target == expected and target.is_dir() else None


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _readable(run_config, path: Path) -> bool:
    target = readable_target(run_config) if _reading.get() else None
    return target is not None and _within(path, target)


def _reading_method(method):
    @functools.wraps(method)
    async def wrapper(self, *args, **kwargs):
        token = _reading.set(True)
        try:
            return await method(self, *args, **kwargs)
        finally:
            _reading.reset(token)
    return wrapper


def install() -> bool:
    """包上框架的文件操作、命令执行的沙箱判定与 Grep 工具。幂等；没装 openjiuwen 时返回 False。"""
    global _installed
    if _installed:
        return True
    try:
        from openjiuwen.core.sys_operation.cwd import get_cwd
        from openjiuwen.core.sys_operation.local.fs_operation import FsOperation
        from openjiuwen.core.sys_operation.local.shell_operation import ShellOperation
        from openjiuwen.harness.tools.filesystem import GrepTool
    except ImportError:
        return False

    resolve_path = FsOperation._resolve_path

    def guarded_resolve(self, path, create_parent=False):
        try:
            return resolve_path(self, path, create_parent)
        except Exception:
            # 读、而且落在资料链接的目标里：放行；其余（含要建上级目录的写）照旧越界
            normalized = Path(os.path.normpath(Path(get_cwd()) / path)).resolve(strict=False)
            if create_parent or not _readable(self._run_config, normalized):
                raise
            return normalized

    check_shell = ShellOperation._check_shell_sandbox

    def guarded_check(self, command, actual_cwd):
        error = check_shell(self, command, actual_cwd)
        if error is None or not _reading.get():
            return error
        # Grep 工具拼的命令（参数都已转义）：工作目录在沙箱里，引用的绝对路径都在沙箱或资料链接的目标里才放
        roots = self._get_sandbox_roots() or []
        cwd = Path(os.path.normpath(actual_cwd)).resolve(strict=False)
        if not any(_within(cwd, root) for root in roots):
            return error
        for raw in self._extract_abs_paths(command):
            resolved = Path(os.path.normpath(raw)).resolve(strict=False)
            if not any(_within(resolved, root) for root in roots) and not _readable(self._run_config, resolved):
                return error
        return None

    guarded_resolve.__wrapped__ = resolve_path  # type: ignore[attr-defined]
    guarded_check.__wrapped__ = check_shell  # type: ignore[attr-defined]
    FsOperation._resolve_path = guarded_resolve
    ShellOperation._check_shell_sandbox = guarded_check
    for name in _READ_METHODS:
        setattr(FsOperation, name, _reading_method(getattr(FsOperation, name)))
    GrepTool.invoke = _reading_method(GrepTool.invoke)
    _installed = True
    return True
