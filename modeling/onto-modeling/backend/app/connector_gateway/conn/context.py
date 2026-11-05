"""连接型提供方的上下文（证据采集接口 §8）：提供方只经它往外发东西。

一行一个 JSON 事件写到 `out`（子进程里是 stdout）。记录在这里补 `hash`、核对锚点前缀和类型、拒绝浮点数、计预算；
超了预算抛 `BudgetExceeded`，提供方停下，任务按不完整收尾。凭据只在 `secret` 里，不进任何事件。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any, Iterable, TextIO

from ...evidence import RECORD_TYPES
from ...evidence import spec as evidence_spec


class ConnError(Exception):
    """提供方报给网关的失败：`code` 进任务状态，`message` 不带连接串、密码。"""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code
        self.message = message or code


class BudgetExceeded(ConnError):
    def __init__(self, what: str):
        super().__init__("budget_exceeded", f"超出预算：{what}")


def _reject_floats(value: Any, path: str = "record") -> None:
    if isinstance(value, float):
        raise ConnError("invalid_record", f"{path} 是浮点数，记录里只放整数和字符串")
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_floats(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_floats(item, f"{path}[{index}]")


def scope_match(name: str, patterns: Iterable[str]) -> bool:
    """范围写法：`crm.*`、`crm.customer`、`erp.store*`（`*` 只在末尾）。没写范围就是全部。"""
    patterns = [str(item) for item in patterns if str(item).strip()]
    if not patterns:
        return True
    for pattern in patterns:
        if pattern.endswith("*"):
            if name.startswith(pattern[:-1]):
                return True
        elif name == pattern:
            return True
    return False


class Context:
    def __init__(self, head: dict, secret: dict | None, out: TextIO, *, scheme: str, clock=time.monotonic,
                 sleep=time.sleep):
        source = head.get("source") or {}
        self.provider = str(head.get("provider") or source.get("provider") or "")
        self.operation = str(head.get("operation") or "")
        self.source_id = str(source.get("id") or "")
        self.connection = dict(source.get("connection") or {})
        self.secret = dict(secret or {})
        self.scope = [str(item) for item in head.get("scope") or []]
        self.budget = dict(head.get("budget") or {})
        self.rate_limit = dict(head.get("rate_limit") or {})
        self.egress = [dict(item) for item in head.get("egress") or []]
        self.masking = dict(head.get("masking") or {"policy": "mask-v1", "salt": ""})
        self.selector = head.get("selector")
        self.limit = head.get("limit")
        self.prefix = f"{scheme}:{self.source_id}/"
        self._out = out
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._deadline = clock() + max(1, int(self.budget.get("max_duration_s") or 1800))
        self._max_records = int(self.budget.get("max_rows") or 200_000)
        self._max_bytes = int(self.budget.get("max_bytes") or 64 * 1024 * 1024)
        self._qps = float(self.rate_limit.get("qps") or 0)
        self._last_query = 0.0
        self.records = 0
        self.bytes = 0
        self._ids: set[str] = set()

    # ---- 发事件 ----

    def _emit(self, event: str, **fields) -> None:
        line = json.dumps({"event": event, **fields}, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._out.write(line + "\n")
            self._out.flush()

    def record(self, record: dict) -> None:
        record = {key: value for key, value in record.items() if key != "hash"}
        anchor = str(record.get("id") or "")
        if not anchor.startswith(self.prefix):
            raise ConnError("invalid_record", f"锚点要以 {self.prefix} 开头")
        if record.get("type") not in RECORD_TYPES:
            raise ConnError("invalid_record", f"记录类型不对：{record.get('type')}")
        if anchor in self._ids:
            raise ConnError("invalid_record", "同一快照里锚点重复")
        _reject_floats(record)
        record["hash"] = evidence_spec.record_hash(record)
        size = len(evidence_spec.canonical_json(record).encode("utf-8"))
        if self.records + 1 > self._max_records:
            raise BudgetExceeded("记录条数")
        if self.bytes + size > self._max_bytes:
            raise BudgetExceeded("记录字节数")
        self._ids.add(anchor)
        self.records += 1
        self.bytes += size
        self._emit("record", record=record)

    def progress(self, phase: str, done: int = 0, total: int = 0, unit: str = "") -> None:
        self._emit("progress", phase=phase, done=int(done), total=int(total), unit=unit)

    def inventory(self, items: list[dict], skipped: Iterable[dict] = ()) -> None:
        self._emit("inventory", items=list(items), skipped=list(skipped))

    def access(self, statement: str, *, rows: int = 0, size: int = 0, duration_ms: int = 0) -> str:
        """访问审计：只记语句摘要（类型 + sha256 前 16 位），不记语句原文和取值。"""
        kind = (statement.strip().split(None, 1) or ["?"])[0].lower()
        digest = f"{kind}:{hashlib.sha256(statement.encode('utf-8')).hexdigest()[:16]}"
        self._emit("access", statement_digest=digest, rows=int(rows), bytes=int(size), duration_ms=int(duration_ms))
        return digest

    def limitation(self, text: str) -> None:
        self._emit("limitation", text=str(text)[:500])

    def result(self, *, complete: bool = True, coverage_pct: float = 100.0, checks: list[dict] | None = None) -> None:
        fields: dict = {"complete": bool(complete), "coverage_pct": round(float(coverage_pct), 1)}
        if checks is not None:
            fields["checks"] = checks
        self._emit("result", **fields)

    def error(self, code: str, message: str = "") -> None:
        self._emit("error", code=code, message=message[:300])

    # ---- 限制 ----

    def remaining_s(self) -> float:
        return max(0.0, self._deadline - self._clock())

    def check_deadline(self) -> None:
        if self.remaining_s() <= 0:
            raise ConnError("timeout", "超过任务时限")

    def throttle(self) -> None:
        """每次查业务系统前调：按登记的每秒查询数限速，顺带查截止时间。"""
        self.check_deadline()
        if self._qps > 0:
            gap = 1.0 / self._qps - (self._clock() - self._last_query)
            if gap > 0:
                self._sleep(gap)
        self._last_query = self._clock()

    def query_timeout_s(self) -> int:
        configured = int(self.rate_limit.get("query_timeout_s") or 30)
        return max(1, min(configured, int(self.remaining_s()) or 1))

    def in_scope(self, name: str) -> bool:
        return scope_match(name, self.scope)

    def anchor(self, path: str) -> str:
        """`path` 由提供方按 §9、§10 拼好（各段已用 `encode` 编码）。"""
        return self.prefix + path

    encode = staticmethod(evidence_spec.encode_segment)
