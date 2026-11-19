"""数据源连接器的子进程（ADR-0104；design/data-sources.md 第 8 节）：`python -m app.connector_gateway.source.worker`。

标准输入：第一行 JSON 头 `{entry, vendor, operation, config, egress, limits}`，第二行凭据 JSON，然后关闭。
标准输出：一行一个事件，只经 `SourceContext` 写；插件直接 print 的东西进标准错误，不会混进事件流。最后一个事件是 `done` 或 `error`。

启动顺序：读完输入 → 设资源上限 → 解析出口白名单里的主机 → 装审计钩子（不许起子进程）→ 把 vendor 目录放进 sys.path
→ 导入入口 → 调 `test(ctx)` 或 `collect(ctx)`。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from .errors import SourceError
from .guard import Guard, scrub
from .sdk import SourceContext

OPERATIONS = ("test", "collect")


def _limit(limits: dict) -> None:
    try:
        import resource
    except ImportError:  # 非 POSIX
        return
    for name, value in (("RLIMIT_AS", limits.get("memory_bytes")), ("RLIMIT_CPU", limits.get("cpu_seconds")),
                        ("RLIMIT_FSIZE", 16 * 1024 * 1024), ("RLIMIT_CORE", 0)):
        kind = getattr(resource, name, None)
        if kind is None or value is None:
            continue
        try:
            resource.setrlimit(kind, (int(value), int(value)))
        except (ValueError, OSError):  # macOS 不支持 RLIMIT_AS；容器里由 Linux 生效
            pass


def _load(entry: str, vendor: str):
    if vendor:
        sys.path.insert(0, vendor)
    path = Path(entry)
    spec = importlib.util.spec_from_file_location("onto_source_connector", path)
    if spec is None or spec.loader is None:
        raise SourceError("crashed", "入口文件读不了")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    stream = sys.stdin.buffer
    head = json.loads(stream.readline().decode("utf-8"))
    secret_line = stream.readline()
    stream.close()
    secret = json.loads(secret_line.decode("utf-8")) if secret_line.strip() else {}
    del secret_line
    _limit(head.get("limits") or {})
    out = sys.stdout
    out.reconfigure(encoding="utf-8")
    # 插件或驱动直接 print 的东西不能混进事件流
    sys.stdout = sys.stderr
    ctx = SourceContext(head, secret, out)
    operation = str(head.get("operation") or "")
    guard = Guard([dict(item) for item in head.get("egress") or []], [])
    sys.addaudithook(guard)

    def denied() -> bool:
        if not guard.denied:
            return False
        code, what = guard.denied[0]
        ctx._emit("error", code=code, message=f"不在白名单里：{what}")
        return True

    try:
        if operation not in OPERATIONS:
            raise SourceError("invalid_operation", operation)
        module = _load(str(head["entry"]), str(head.get("vendor") or ""))
        handler = getattr(module, operation, None)
        if not callable(handler):
            raise SourceError("invalid_operation", f"插件没有 {operation}()")
        handler(ctx)
        # 插件吞掉了被拦的异常也算失败：它试图连白名单外的地方或起子进程
        if not denied():
            ctx._emit("done")
    except SourceError as exc:
        if not denied():
            ctx._emit("error", code=exc.code, message=scrub(exc.message, secret)[:300])
    except MemoryError:
        ctx._emit("error", code="memory", message="超出内存上限")
    except Exception as exc:  # noqa: BLE001 异常原文可能带连接串、密码：只报类型
        if not denied():
            ctx._emit("error", code="provider_error", message=type(exc).__name__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
