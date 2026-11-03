"""资料的文本版（ADR-0073）：经连接器网关把 PDF、Word、Excel 转成 Markdown，后台排队，结果走写入路径落盘。

- `service`：分类、状态、队列与后台线程、写入事件、重新转换、启动补转；
- `gateway`：调连接器网关（健康、已加载的转换器、转换），每次签一张一次性票据；
- `store`：转换状态记录与管理员指定的默认转换器（内核数据目录）。
"""

from .service import (  # noqa: F401
    EVENT,
    candidates,
    drain,
    enqueue,
    install,
    kind_of,
    known_converters,
    migrate,
    pick,
    process,
    reconvert,
    reset,
    retry_waiting,
    start,
    stop,
    text_file,
    text_markdown,
    view,
)
