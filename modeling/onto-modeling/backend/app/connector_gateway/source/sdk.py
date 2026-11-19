"""数据源连接器的 SDK（ADR-0103；design/data-sources.md 第 1、8 节）：插件只经 `SourceContext` 往外发东西。

运行在插件子进程里。一行一个 JSON 事件写到 `out`（子进程的 stdout）；这里只做基本的形状检查和粗限量，
遮蔽、截断、渲染都在网关主进程（`runner`、`render`）里做，插件绕不过。凭据只在 `secret` 里，不进任何事件。

插件里这样写：

    from app.connector_gateway.source.sdk import SourceError

    def test(ctx):
        ...
        ctx.check("连接", True)

    def collect(ctx):
        ctx.table("orders", [{"name": "id", "type": "bigint", "key": "pk"}], rows)
        ctx.fact("foreign_key", table="orders", columns=["customer_id"], ref_table="customer", ref_columns=["id"])
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Iterable, TextIO

from . import masking, sqlguard
from .errors import SourceError

__all__ = ["SourceContext", "SourceError", "scope_match"]

# 子进程一侧的粗限量：主进程另有权威的上限（render），这里只防插件一次发出巨量数据把管道塞满
SEND_ROWS = 200
SEND_CELL_CHARS = 1000
SEND_CELLS = 20_000
SEND_TEXT_CHARS = 4000
FACT_KINDS = ("primary_key", "foreign_key", "reference", "unique", "check", "values", "view", "routine", "trigger")
COLUMN_KEYS = ("pk", "fk", "uk", "")


def _clip(value: Any, limit: int) -> Any:
    value = masking.plain(value)
    if isinstance(value, str) and len(value) > limit:
        return value[:limit]
    return value


def _names(values, what: str) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)) or not values:
        raise SourceError("invalid_event", f"{what} 要是非空的名字列表")
    return [_clip(str(item), 200) for item in values]


def scope_match(name: str, patterns: Iterable[str]) -> bool:
    """范围写法：`orders`、`customer*`、`shop.*`（`*` 只在末尾）。没写范围就是全部。"""
    patterns = [str(item).strip() for item in patterns if str(item).strip()]
    if not patterns:
        return True
    for pattern in patterns:
        if pattern.endswith("*"):
            if name.startswith(pattern[:-1]):
                return True
        elif name == pattern:
            return True
    return False


class SourceContext:
    def __init__(self, head: dict, secret: dict | None, out: TextIO, *, clock=time.monotonic, sleep=time.sleep):
        self.operation = str(head.get("operation") or "")
        self.config = dict(head.get("config") or {})
        self.secret = dict(secret or {})
        limits = dict(head.get("limits") or {})
        self._out = out
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._deadline = clock() + max(1, int(limits.get("timeout_s") or 600))
        self._qps = float(limits.get("qps") or 20)
        self._query_timeout = int(limits.get("query_timeout_s") or 30)
        self._last_query = 0.0
        scope = self.config.get("tables")
        self.scope = [part for part in str(scope or "").replace("，", ",").split(",") if part.strip()]

    # ---- 发事件 ----

    def _emit(self, event: str, /, **fields) -> None:
        line = json.dumps({"event": event, **fields}, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._out.write(line + "\n")
            self._out.flush()

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self._emit("check", name=_clip(str(name), 40), ok=bool(ok), detail=_clip(str(detail or ""), 300))

    def table(self, name: str, columns: list[dict], rows: Iterable[Iterable[Any]] = (), comment: str = "") -> None:
        if not isinstance(name, str) or not name.strip():
            raise SourceError("invalid_event", "表名不能为空")
        if not isinstance(columns, (list, tuple)) or not columns:
            raise SourceError("invalid_event", f"表 {name} 没有列")
        cols = []
        for column in columns:
            if not isinstance(column, dict) or not str(column.get("name") or "").strip():
                raise SourceError("invalid_event", f"表 {name} 的列要有 name")
            key = str(column.get("key") or "")
            if key not in COLUMN_KEYS:
                raise SourceError("invalid_event", f"列的 key 只能是 pk、fk、uk 或空：{key}")
            cols.append({"name": _clip(str(column["name"]), 200), "type": _clip(str(column.get("type") or ""), 80),
                         "nullable": column.get("nullable") if isinstance(column.get("nullable"), bool) else None,
                         "key": key, "comment": _clip(str(column.get("comment") or ""), 300)})
        width = len(cols)
        most = max(1, min(SEND_ROWS, SEND_CELLS // width))
        sent = []
        for row in rows:
            if len(sent) >= most:
                break
            values = list(row)
            values = (values + [None] * width)[:width]
            sent.append([_clip(value, SEND_CELL_CHARS) for value in values])
        self._emit("table", name=_clip(name.strip(), 200), comment=_clip(str(comment or ""), 300), columns=cols,
                   rows=sent)

    def fact(self, kind: str, /, **fields) -> None:
        if kind not in FACT_KINDS:
            raise SourceError("invalid_event", f"事实类型不对：{kind}")
        if "kind" in fields:
            # 事件里 kind 是事实类型；存储过程是过程还是函数写在 type 里
            raise SourceError("invalid_event", "事实的字段不能叫 kind，存储过程的类型写 type")
        out: dict = {}
        for key, value in fields.items():
            if key in ("columns", "ref_columns"):
                out[key] = _names(value, key)
            elif key == "values":
                out[key] = [_clip(item, 200) for item in list(value or [])[:20]]
            else:
                out[key] = _clip(value, SEND_TEXT_CHARS)
        # 事实的字段单独放一层：字段名（如触发器的 event）不会和事件类型撞
        self._emit("fact", fact={"kind": kind, **out})

    def progress(self, done: int, total: int = 0) -> None:
        self._emit("progress", done=int(done), total=int(total))

    def limitation(self, text: str) -> None:
        self._emit("limitation", text=_clip(str(text), 500))

    # ---- 限制 ----

    def remaining_s(self) -> float:
        return max(0.0, self._deadline - self._clock())

    def check_deadline(self) -> None:
        if self.remaining_s() <= 0:
            raise SourceError("timeout", "超过任务时限")

    def throttle(self) -> None:
        """每次查业务系统前调：按每秒查询数限速，顺带查截止时间。"""
        self.check_deadline()
        if self._qps > 0:
            gap = 1.0 / self._qps - (self._clock() - self._last_query)
            if gap > 0:
                self._sleep(gap)
        self._last_query = self._clock()

    def query_timeout_s(self) -> int:
        return max(1, min(self._query_timeout, int(self.remaining_s()) or 1))

    def readonly(self, sql: str, dialect: str = "mysql") -> str:
        """语句过词法白名单（单条、只读）；放行返回语句类型，否则抛 SourceError("statement_blocked")。"""
        return sqlguard.check(sql, dialect)

    def session(self, sql: str, dialect: str = "mysql") -> str:
        """会话设置（只读会话、超时）只认固定写法。"""
        return sqlguard.check_session(sql, dialect)

    def in_scope(self, name: str) -> bool:
        return scope_match(name, self.scope)

    def fail(self, code: str, message: str = ""):
        raise SourceError(code, message)
