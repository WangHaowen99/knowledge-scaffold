"""在子进程里跑一次数据源连接器（ADR-0103；design/data-sources.md 第 8 节），收齐事件。

墙钟超时、事件流超过上限就杀整组进程；子进程崩了只算这一个任务失败。凭据只经 stdin 交给子进程一次。
事件只认白名单里的几种、字段只取认识的，其余丢掉；最后一个事件是 `done` 才算成功。
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .render import Collected

_log = logging.getLogger(__name__)

APP_ROOT = Path(__file__).resolve().parents[3]
CHUNK = 64 * 1024
MAX_LINE_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class SourceLimits:
    timeout_s: float = 600.0
    memory_bytes: int = 768 * 1024 * 1024
    max_event_bytes: int = 96 * 1024 * 1024
    qps: int = 20
    query_timeout_s: int = 30


@dataclass
class Outcome:
    ok: bool
    error_code: str = ""
    error_message: str = ""
    checks: list[dict] = field(default_factory=list)
    collected: Collected = field(default_factory=Collected)
    progress: tuple[int, int] = (0, 0)


def _kill(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:  # 含 ProcessLookupError、PermissionError
        process.kill()


def _absorb(outcome: Outcome, event: dict) -> bool:
    """把一个事件并进结果；返回是否是结束事件。"""
    kind = event.get("event")
    if kind == "check":
        outcome.checks.append({"name": str(event.get("name") or ""), "ok": bool(event.get("ok")),
                               "detail": str(event.get("detail") or "")})
    elif kind == "table":
        columns = [column for column in event.get("columns") or [] if isinstance(column, dict) and column.get("name")]
        rows = [list(row) for row in event.get("rows") or [] if isinstance(row, list)]
        if columns:
            outcome.collected.tables.append({"name": str(event.get("name") or ""), "comment": str(event.get("comment") or ""),
                                             "columns": columns, "rows": rows})
    elif kind == "fact":
        fact = event.get("fact")
        if isinstance(fact, dict) and fact.get("kind"):
            outcome.collected.facts.append(dict(fact))
    elif kind == "limitation":
        outcome.collected.limitations.append(str(event.get("text") or "")[:500])
    elif kind == "progress":
        outcome.progress = (int(event.get("done") or 0), int(event.get("total") or 0))
    elif kind == "done":
        outcome.ok = True
        return True
    elif kind == "error":
        outcome.ok = False
        outcome.error_code = str(event.get("code") or "provider_error")
        outcome.error_message = str(event.get("message") or "")[:300]
        return True
    return False


def run(entry: Path, vendor: Path | None, operation: str, config: dict, secret: dict, egress: list[dict],
        limits: SourceLimits) -> Outcome:
    head = {"entry": str(entry), "vendor": str(vendor) if vendor else "", "operation": operation, "config": config,
            "egress": egress,
            "limits": {"timeout_s": int(limits.timeout_s), "memory_bytes": limits.memory_bytes,
                       "cpu_seconds": int(limits.timeout_s) + 5, "qps": limits.qps,
                       "query_timeout_s": limits.query_timeout_s}}
    # 子进程只拿到最少的环境变量：不继承网关的任何配置（主密钥路径、内核地址都不给）
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8",
           "HOME": "/tmp", "TMPDIR": "/tmp"}
    process = subprocess.Popen(  # NOSONAR(S4721) 固定命令：本解释器跑网关 worker 模块，参数列表不经 shell
        [sys.executable, "-m", "app.connector_gateway.source.worker"], cwd=APP_ROOT, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    outcome = Outcome(ok=False)
    finished = threading.Event()
    overflow = threading.Event()
    malformed = threading.Event()
    errors = bytearray()

    def read_stdout():
        size = 0
        while True:
            raw = process.stdout.readline(MAX_LINE_BYTES + 1)
            if not raw:
                return
            size += len(raw)
            if len(raw) > MAX_LINE_BYTES:
                size = limits.max_event_bytes + 1
            if size > limits.max_event_bytes:
                overflow.set()
                _kill(process)
                return
            try:
                event = json.loads(raw.decode("utf-8"))
            except ValueError:  # 含 UnicodeDecodeError
                malformed.set()
                _kill(process)
                return
            if isinstance(event, dict) and not finished.is_set() and _absorb(outcome, event):
                finished.set()

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
        process.stdin.write(json.dumps(head, ensure_ascii=False).encode("utf-8") + b"\n")
        process.stdin.write(json.dumps(secret or {}, ensure_ascii=False).encode("utf-8") + b"\n")
        process.stdin.close()
    except OSError:  # 含 BrokenPipeError：子进程先退出了，结果按事件判
        pass
    try:
        process.wait(timeout=limits.timeout_s + 5)
        timed_out = False
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill(process)
        process.wait()
    for reader in readers:
        reader.join(timeout=5)
    if timed_out:
        return Outcome(ok=False, error_code="timeout", error_message="超过任务时限", checks=outcome.checks)
    if overflow.is_set():
        return Outcome(ok=False, error_code="output_too_large", error_message="采集结果超过上限")
    if malformed.is_set():
        return Outcome(ok=False, error_code="invalid_event", error_message="插件输出了不认识的内容")
    if not finished.is_set():
        tail = errors.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
        _log.warning("数据源连接器子进程退出码 %s：%s", process.returncode, tail[0][:200])
        memory = process.returncode in (-signal.SIGKILL, -signal.SIGSEGV) or "MemoryError" in tail[0]
        return Outcome(ok=False, error_code="memory" if memory else "crashed", error_message="",
                       checks=outcome.checks)
    return outcome
