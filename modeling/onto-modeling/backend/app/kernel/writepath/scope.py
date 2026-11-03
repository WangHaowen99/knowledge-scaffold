"""写入范围检查（详设 4.3「助手的修改确认之前，写入范围内的条目只读」；方案 C10；契约 2.4 write_scope_locked）。

所有写入方（网页、Studio、MCP、插件产物、回退）都经写入事务，检查就放在事务里一处：事务登记要写的路径时
问一次写入范围锁，落在范围里就 423 `write_scope_locked`（details 是 ScopeLock），什么都还没写。

- 锁由变更集一侧算（`kernel/changesets/service.paths_in_scope`），启动时登记；没登记时不检查；
- 合入变更集本身（Origin.changeset 就是锁住范围的那个变更集）不受限；
- 回退（V-06）是整树恢复：有没确认的助手变更集、或助手正在跑时一律 423，要先确认或撤回；
- 派生文件（index.md、脚本描述符）不经这里：它们跟着被允许的改动走。
"""

from __future__ import annotations

from collections.abc import Callable

from ...services.workspace.errors import WorkspaceError

_checker: Callable | None = None


def register_checker(checker: Callable | None) -> None:
    """checker(manager, project_id, rels, lock=None) -> (ScopeLock, 命中的路径)。"""
    global _checker
    _checker = checker


class ScopeGuard:
    """一个写入事务的范围检查：第一次登记路径时算一次锁，之后复用。"""

    def __init__(self, manager, project_id: str, origin):
        self.manager = manager
        self.project_id = project_id
        self.origin = origin
        self.lock: dict | None = None

    def check(self, rels: list[str]) -> None:
        if _checker is None or not rels:
            return
        lock, hits = _checker(self.manager, self.project_id, rels, self.lock)
        self.lock = lock
        if not lock or lock.get("state") in (None, "none"):
            return
        if self.origin.changeset and self.origin.changeset == lock.get("changeset_id"):
            return
        if self.origin.source == "revert":
            raise WorkspaceError("助手这一轮还没结束或有冲突没处理，先处理完再回退版本", 423, "write_scope_locked", lock)
        if hits:
            raise WorkspaceError("这些内容在助手的写入范围里，这一轮结束前只读", 423, "write_scope_locked", lock)
