"""知识包版本对比的 ZIP 解析子进程：限地址空间与 CPU 后建索引，结果写到参数给的 JSON 文件。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    import resource
except ImportError:  # pragma: no cover - Windows 开发环境
    resource = None

from . import package_compare
from .workspace import WorkspaceError, WorkspaceManager


def _virtual_memory_bytes() -> int:
    """导入完依赖后本进程已占的地址空间。"""
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmSize:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _apply_limits(raw: str) -> None:
    limits = json.loads(raw)
    memory_bytes = int(limits["worker_memory_bytes"])
    cpu_seconds = int(limits["worker_cpu_seconds"])
    if resource is not None:
        # RLIMIT_AS 是虚拟地址空间的绝对上限，到这里应用依赖已经导入，固定 512 MiB 可能低于基线；
        # 配置值按「在基线之上额外给解析用的预算」算
        baseline = _virtual_memory_bytes()
        address_space_limit = baseline + memory_bytes if baseline else memory_bytes
        _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        if hard != resource.RLIM_INFINITY:
            address_space_limit = min(address_space_limit, hard)
        resource.setrlimit(
            resource.RLIMIT_AS,
            (address_space_limit, address_space_limit),
        )
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    package_compare.MAX_UNCOMPRESSED_BYTES = int(limits["max_uncompressed_bytes"])
    package_compare.MAX_FILE_BYTES = int(limits["max_file_bytes"])
    package_compare.MAX_MANIFEST_BYTES = int(limits["max_manifest_bytes"])
    package_compare.MAX_FILES = int(limits["max_files"])
    package_compare.MAX_ENTRIES = int(limits["max_entries"])
    package_compare.MAX_PATH_BYTES = int(limits["max_path_bytes"])


def main() -> int:
    if len(sys.argv) != 4:
        return 2
    archive_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2])
    try:
        _apply_limits(sys.argv[3])
        response = {"ok": True, "index": package_compare._archive_index_core(archive_path)}
    except WorkspaceError as error:
        response = {
            "ok": False,
            "error": {
                "message": str(error),
                "status": error.status,
                "code": error.code,
            },
        }
    except MemoryError:
        response = {
            "ok": False,
            "error": {
                "message": "知识包解析所需内存超过限制，请缩小知识包后重试",
                "status": 413,
                "code": "compare_archive_memory_limit",
            },
        }
    except Exception:
        response = {
            "ok": False,
            "error": {
                "message": "知识包解析失败，请检查 ZIP 文件",
                "status": 400,
                "code": "compare_archive_invalid",
            },
        }
    WorkspaceManager._atomic_write(
        output_path,
        json.dumps(response, ensure_ascii=False).encode("utf-8"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
