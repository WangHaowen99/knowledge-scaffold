"""插件报给网关的失败（ADR-0103）。单独一个模块，SDK 和语句白名单都从这里导入，避免互相导入。"""

from __future__ import annotations


class SourceError(Exception):
    """`code` 进任务结果，`message` 里的凭据取值由子进程遮掉。"""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = str(code or "provider_error")
        self.message = str(message or code)
