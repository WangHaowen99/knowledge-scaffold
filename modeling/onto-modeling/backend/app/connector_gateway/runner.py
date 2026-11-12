"""在子进程里跑一次转换：墙钟超时就杀整组进程，输出超过上限就杀，子进程崩了只算这一个任务失败。"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from ..evidence.convert import FAILURES, ConvertLimits
from .registry import LoadedConverter

_log = logging.getLogger(__name__)

APP_ROOT = Path(__file__).resolve().parents[2]
CHUNK = 64 * 1024


@dataclass(frozen=True)
class JobLimits:
    timeout_s: float = 120.0
    memory_bytes: int = 768 * 1024 * 1024
    max_output_bytes: int = 16 * 1024 * 1024


def failed(code: str) -> dict:
    return {"state": "failed", "error_code": code, "error_message": FAILURES.get(code, FAILURES["crashed"])}


def _kill(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:  # 含 ProcessLookupError、PermissionError
        process.kill()


def run(converter: LoadedConverter, data: bytes, filename: str, convert_limits: ConvertLimits,
        job: JobLimits) -> dict:
    """返回网关回给内核的结果：converted / unsupported（带 markdown 等）或 failed（带 error_code）。"""
    head = {"entry": str(converter.entry_path), "filename": filename, "limits": convert_limits.to_dict(),
            "memory_bytes": job.memory_bytes, "cpu_seconds": int(job.timeout_s) + 5}
    # 子进程只拿到最少的环境变量：不继承网关的任何配置
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"}
    process = subprocess.Popen(  # NOSONAR(S4721) 固定命令：本解释器跑网关 worker 模块，参数列表不经 shell
        [sys.executable, "-m", "app.connector_gateway.worker"], cwd=APP_ROOT, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    output = bytearray()
    errors = bytearray()
    overflow = threading.Event()

    def read_stdout():
        while True:
            chunk = process.stdout.read(CHUNK)
            if not chunk:
                return
            if len(output) + len(chunk) > job.max_output_bytes:
                overflow.set()
                _kill(process)
                return
            output.extend(chunk)

    def read_stderr():
        while True:
            chunk = process.stderr.read(CHUNK)
            if not chunk:
                return
            if len(errors) < 8 * 1024:
                errors.extend(chunk[: 8 * 1024 - len(errors)])

    readers = [threading.Thread(target=read_stdout, daemon=True), threading.Thread(target=read_stderr, daemon=True)]
    for reader in readers:
        reader.start()
    try:
        process.stdin.write(json.dumps(head).encode("utf-8") + b"\n")
        process.stdin.write(data)
        process.stdin.close()
    except OSError:  # 含 BrokenPipeError：子进程先退出了，结果按退出码判
        pass
    try:
        process.wait(timeout=job.timeout_s)
        timed_out = False
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill(process)
        process.wait()
    for reader in readers:
        reader.join(timeout=5)
    if timed_out:
        return failed("timeout")
    if overflow.is_set():
        return failed("output_too_large")
    try:
        reply = json.loads(output.decode("utf-8"))
    except ValueError:  # 含 UnicodeDecodeError、JSONDecodeError
        # 被内核信号杀掉（-9 多半是内存）或异常退出；只记最后一行错误，不记内容
        tail = errors.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
        _log.warning("转换器 %s 子进程退出码 %s：%s", converter.info.id, process.returncode, tail[0][:200])
        memory = process.returncode in (-signal.SIGKILL, -signal.SIGSEGV) or "MemoryError" in tail[0]
        return failed("memory" if memory else "crashed")
    if not reply.get("ok"):
        error = reply.get("error") or {}
        return failed(str(error.get("code") or "crashed"))
    result = dict(reply.get("result") or {})
    if result.get("state") not in ("converted", "unsupported"):
        return failed("crashed")
    return result
