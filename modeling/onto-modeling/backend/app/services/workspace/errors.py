"""工作区错误类型（叶子模块，不依赖工作区其他部分）。"""

from __future__ import annotations


class WorkspaceError(Exception):
    """工作区错误：code 取 API 契约第 2 章的错误码（缺省按状态码归类），details 是该码约定的补充。"""

    def __init__(self, message: str, status: int = 400, code: str = "workspace_error", details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details


class NameConflictError(WorkspaceError):
    """任务名称与查重范围内的已有任务重名；conflict 带候选名，供前端改名后重试。"""

    def __init__(self, message: str, conflict: dict):
        super().__init__(message, 409, "project_name_conflict")
        self.conflict = conflict
