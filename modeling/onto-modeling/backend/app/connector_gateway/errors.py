"""网关接口的错误：HTTP 状态 + `{"error": 代码, "message": 说明}`。"""

from __future__ import annotations


class GatewayError(Exception):
    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message or code

    def payload(self) -> dict:
        return {"error": self.code, "message": self.message}
