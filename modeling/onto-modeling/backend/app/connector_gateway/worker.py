"""转换任务子进程：先给自己设资源上限（软硬一起设，降了就升不回去），再导入转换器、转换、把结果写到 stdout。

标准输入：第一行是 JSON 头 `{entry, filename, limits, memory_bytes, cpu_seconds}`，后面是原文件字节。
标准输出：`{"ok": true, "result": Conversion}` 或 `{"ok": false, "error": {code, message}}`。
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys


def _limit(memory_bytes: int, cpu_seconds: int) -> None:
    try:
        import resource
    except ImportError:  # 非 POSIX
        return
    for name, value in (("RLIMIT_AS", memory_bytes), ("RLIMIT_CPU", cpu_seconds), ("RLIMIT_CORE", 0),
                        ("RLIMIT_FSIZE", 16 * 1024 * 1024)):
        kind = getattr(resource, name, None)
        if kind is None or value is None:
            continue
        try:
            resource.setrlimit(kind, (value, value))
        except (ValueError, OSError):  # macOS 不支持 RLIMIT_AS；本机开发照跑，容器里由 Linux 生效
            pass


def _reply(payload: dict) -> None:
    # 标准输出就是给网关的结果：单独的输出通道，原样写出（不加换行），不受应用日志配置影响
    handler = logging.StreamHandler(sys.stdout)
    handler.terminator = ""
    output = logging.Logger("converter.reply", logging.INFO)
    output.addHandler(handler)
    output.info(json.dumps(payload, ensure_ascii=False))


def main() -> int:
    stream = sys.stdin.buffer
    head = json.loads(stream.readline().decode("utf-8"))
    data = stream.read()
    _limit(int(head.get("memory_bytes") or 0) or None, int(head.get("cpu_seconds") or 0) or None)
    from ..evidence.convert import ConvertError, ConvertLimits

    try:
        spec = importlib.util.spec_from_file_location("onto_connector_converter", head["entry"])
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = module.convert(data, str(head.get("filename") or ""), ConvertLimits.from_dict(head.get("limits")))
        _reply({"ok": True, "result": result.to_dict()})
    except ConvertError as exc:
        _reply({"ok": False, "error": {"code": exc.code, "message": str(exc)}})
    except MemoryError:
        _reply({"ok": False, "error": {"code": "memory", "message": ""}})
    except RecursionError:
        _reply({"ok": False, "error": {"code": "crashed", "message": ""}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
