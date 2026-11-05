"""连接型提供方的子进程运行器（证据采集接口 §8；ADR-0084）。

`SubprocessProvider` 对网关的任务表实现和旁路容器提供方同一套方法（submit / status / cancel / result / records / raw /
delete），`jobs` 不用区分。每个任务一个子进程（`conn.worker`）：最小环境变量、新会话（超时、取消时杀整组）；凭据由这里
解密，经 stdin 交给子进程，不进命令行和环境变量。后台线程读子进程的事件：记录落到运行目录的 records.jsonl，进度、清单、
访问审计、局限、结果、错误记在内存。子进程输出里出现凭据取值的一律遮蔽（纵深防御，提供方本来就不该输出它）。
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator
from urllib.parse import urlsplit

from ...evidence import RECORD_TYPES
from ...evidence.spec import canonical_json, record_hash
from .. import bundle
from ..errors import GatewayError
from . import catalog
from .worker import host_key, scrub

_log = logging.getLogger(__name__)

APP_ROOT = Path(__file__).resolve().parents[3]
RUNS_DIR_ENV = "ONTO_CONNECTOR_CONN_DIR"
MEMORY_ENV = "ONTO_CONNECTOR_CONN_MEMORY_MB"
FILE_ENV = "ONTO_CONNECTOR_CONN_FILE_MB"
# 提供方自己的非机密配置（如 ONTO_CONNECTOR_DB_PROFILE_ROWS）原样传给子进程
PASS_ENV_PREFIXES = ("ONTO_CONNECTOR_DB_", "ONTO_CONNECTOR_CODE_")
TEST_TIMEOUT_S = 30
KILL_GRACE_S = 10
MAX_EVENT_BYTES = bundle.MAX_LINE_BYTES + 4096
STDERR_TAIL = 8 * 1024
# 记录里出现的凭据才遮蔽的最短长度：太短的取值会误伤正常数据
RECORD_SCRUB_MIN = 6


class _Run:
    """一次子进程执行：起进程、读事件、超时或取消时杀整组、定结局。"""

    def __init__(self, job_id: str, provider_id: str, directory: Path, secret: dict, *, timeout_s: float,
                 keep_records: bool):
        self.job_id, self.provider_id, self.directory = job_id, provider_id, directory
        self.timeout_s = timeout_s
        self.state, self.phase, self.done, self.total, self.unit = "queued", "连接", 0, 0, ""
        self.error_code: str | None = None
        self.error_message: str | None = None
        self.items: list[dict] = []
        self.skipped: list[dict] = []
        self.access: list[dict] = []
        self.limitations: list[str] = []
        self.outcome: dict | None = None
        self.counts: dict[str, int] = {}
        self.records_path = directory / "records.jsonl" if keep_records else None
        self.records_bytes = 0
        self.truncated = False
        self.scrubbed = 0
        self.finished = threading.Event()
        self.on_finish = None
        self._secret = secret
        self._error: tuple[str, str] | None = None
        self._invalid: str | None = None
        self._cancelled = False
        self._timed_out = False
        self._stderr = bytearray()
        self._lock = threading.Lock()
        self.process: subprocess.Popen | None = None
        self._readers: list[threading.Thread] = []

    # ---- 起进程 ----

    def start(self, head: dict, env: dict) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-B", "-E", "-s", "-u", "-m", "app.connector_gateway.conn.worker"], cwd=APP_ROOT,
            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
            close_fds=True)
        self.state = "running"
        self._readers = [threading.Thread(target=self._read_events, daemon=True),
                         threading.Thread(target=self._read_stderr, daemon=True)]
        for reader in self._readers:
            reader.start()
        try:
            self.process.stdin.write(json.dumps(head, ensure_ascii=False).encode("utf-8") + b"\n")
            self.process.stdin.write(json.dumps(self._secret, ensure_ascii=False).encode("utf-8") + b"\n")
            self.process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        threading.Thread(target=self._wait, name=f"conn-{self.job_id}", daemon=True).start()

    def _killpg(self) -> None:
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def _exited(self) -> bool:
        """不收尸地看进程退出没有：没收尸前 pid（也就是组号）不会被别的进程复用，杀整组不会误杀。"""
        if hasattr(os, "waitid"):
            try:
                return os.waitid(os.P_PID, self.process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
            except ChildProcessError:
                return True
        return self.process.poll() is not None

    def _wait(self) -> None:
        deadline = time.monotonic() + self.timeout_s
        while not self._exited():
            if time.monotonic() > deadline:
                self._timed_out = True
                self._killpg()
                break
            time.sleep(0.05)
        # 提供方留下的孙进程（比如 git）一起清掉
        if self.process.returncode is None:
            self._killpg()
        returncode = self.process.wait()
        for reader in self._readers:
            reader.join(timeout=5)
        try:
            self._settle(returncode)
        finally:
            self._secret = {}
            self.finished.set()
            if self.on_finish is not None:
                self.on_finish()

    def cancel(self) -> None:
        with self._lock:
            if self.finished.is_set():
                return
            self._cancelled = True
            self.phase = "取消中"
        if self.process is not None and self.process.returncode is None:
            self._killpg()

    # ---- 读事件 ----

    def _read_events(self) -> None:
        handle = open(self.records_path, "wb") if self.records_path else None
        try:
            stream = self.process.stdout
            while True:
                line = stream.readline(MAX_EVENT_BYTES + 1)
                if not line:
                    return
                if len(line) > MAX_EVENT_BYTES:
                    self._reject("事件超过 1 MiB")
                    return
                try:
                    self._handle(line, handle)
                except (UnicodeDecodeError, ValueError, TypeError, AttributeError):
                    self._reject("事件不是合法的 JSON 对象")
                    return
        finally:
            if handle is not None:
                handle.close()

    def _reject(self, why: str) -> None:
        self._invalid = why
        self._killpg()

    def _handle(self, line: bytes, handle: BinaryIO | None) -> None:
        text = line.decode("utf-8")
        cleaned = scrub(text, self._secret, RECORD_SCRUB_MIN, json_text=True)
        event = json.loads(cleaned)
        if not isinstance(event, dict):
            raise ValueError("not an object")
        kind = event.get("event")
        if cleaned != text:
            self.scrubbed += 1
        if kind == "record":
            record = event["record"]
            if not isinstance(record, dict):
                raise ValueError("record")
            if cleaned != text:
                record["hash"] = record_hash(record)
            record_type = record.get("type")
            self.counts[record_type if record_type in RECORD_TYPES else "?"] = \
                self.counts.get(record_type if record_type in RECORD_TYPES else "?", 0) + 1
            if handle is None:
                return
            data = (canonical_json(record) + "\n").encode("utf-8")
            if self.truncated or self.records_bytes + len(data) > bundle.RECORDS_CAP_BYTES:
                self.truncated = True
                return
            handle.write(data)
            self.records_bytes += len(data)
        elif kind == "progress":
            self.phase = str(event.get("phase") or self.phase)[:40]
            self.done, self.total = bundle.as_count(event.get("done")), bundle.as_count(event.get("total"))
            self.unit = str(event.get("unit") or "")[:16]
        elif kind == "inventory":
            room = bundle.MAX_INVENTORY + 1 - len(self.items)
            self.items.extend(item for item in (event.get("items") or [])[:max(room, 0)] if isinstance(item, dict))
            room = bundle.MAX_INVENTORY - len(self.skipped)
            self.skipped.extend(item for item in (event.get("skipped") or [])[:max(room, 0)] if isinstance(item, dict))
        elif kind == "access":
            self._add_access({"statement_digest": event.get("statement_digest"), "rows": event.get("rows"),
                              "bytes": event.get("bytes"), "duration_ms": event.get("duration_ms"),
                              "at": bundle.iso(time.time())})
        elif kind == "limitation":
            if len(self.limitations) < bundle.MAX_LIMITATIONS:
                self.limitations.append(str(event.get("text") or "")[:2000])
        elif kind == "result":
            self.outcome = event
        elif kind == "error" and self._error is None:
            self._error = (str(event.get("code") or "provider_error")[:64],
                           str(event.get("message") or "")[:300])

    def _add_access(self, entry: dict) -> None:
        """最多 MAX_ACCESS_LOG 条，超出的和最后一条合并成一条汇总（内存里也不无限增长）。"""
        entry = bundle.normalize_access_log([entry])[0]
        limit = bundle.MAX_ACCESS_LOG
        if len(self.access) < limit and not (self.access and self.access[-1]["statement_digest"] == "*"):
            self.access.append(entry)
            return
        last = self.access[-1]
        if last["statement_digest"] != "*":
            last = {"statement_digest": "*", "count": 1, "rows": last["rows"], "bytes": last["bytes"],
                    "duration_ms": last["duration_ms"]}
            self.access[-1] = last
        last["count"] += 1
        for key in ("rows", "bytes", "duration_ms"):
            last[key] += entry[key]

    def _read_stderr(self) -> None:
        stream = self.process.stderr
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            self._stderr.extend(chunk)
            del self._stderr[:-STDERR_TAIL]

    # ---- 结局 ----

    def _settle(self, returncode: int) -> None:
        if self._cancelled:
            self._finish("cancelled")
        elif self._timed_out:
            self._finish("failed", "timeout", "超过时限")
        elif self._invalid:
            self._finish("failed", "invalid_event", self._invalid)
        elif self._error is not None:
            code, message = self._error
            self._finish("failed", code, scrub(message, self._secret) or code)
        elif returncode != 0:
            tail = scrub(self._stderr.decode("utf-8", "replace"), self._secret).strip().splitlines()[-1:] or [""]
            _log.warning("连接型提供方 %s 子进程退出码 %s：%s", self.provider_id, returncode, tail[0][:200])
            if returncode == -signal.SIGXCPU:
                self._finish("failed", "timeout", "超过 CPU 时间上限")
            elif returncode in (-signal.SIGKILL, -signal.SIGSEGV) or "MemoryError" in tail[0]:
                self._finish("failed", "memory", "子进程超出内存上限或被杀")
            else:
                self._finish("failed", "crashed", "提供方子进程异常退出")
        elif self.outcome is None:
            self._finish("failed", "no_result", "提供方没有给出结果")
        else:
            complete = bool(self.outcome.get("complete")) and not self.truncated
            self._finish("succeeded" if complete else "partial")
        if self.scrubbed:
            _log.warning("连接型提供方 %s 任务 %s 的输出里有 %d 处凭据取值，已遮蔽", self.provider_id, self.job_id,
                         self.scrubbed)

    def _finish(self, state: str, code: str | None = None, message: str | None = None) -> None:
        self.error_code, self.error_message = code, message
        self.phase = {"succeeded": "完成", "partial": "完成（不完整）", "failed": "失败", "cancelled": "已取消"}[state]
        self.state = state

    def status(self) -> dict:
        return {"job_id": self.job_id, "state": self.state, "phase": self.phase, "done": self.done,
                "total": self.total, "unit": self.unit, "error_code": self.error_code,
                "error_message": self.error_message}


class SubprocessProvider:
    input = "connection"

    def __init__(self, provider_id: str, secrets, *, root: Path | str | None = None, memory_bytes: int | None = None,
                 file_bytes: int | None = None, kill_grace_s: float = KILL_GRACE_S,
                 find_spec=None, which=None):
        self.info = catalog.PROVIDERS[provider_id]
        self.id = provider_id
        self.version = self.info["version"]
        self.capabilities = tuple(self.info["capabilities"])
        self.scheme = catalog.scheme_of(provider_id)
        self.secrets = secrets
        self.root = Path(root or os.environ.get(RUNS_DIR_ENV) or Path(tempfile.gettempdir()) / "connector-conn")
        self.memory_bytes = memory_bytes or int(os.environ.get(MEMORY_ENV) or 768) * 1024 * 1024
        self.file_bytes = file_bytes or int(os.environ.get(FILE_ENV) or 256) * 1024 * 1024
        self.kill_grace_s = kill_grace_s
        self._find_spec, self._which = find_spec, which
        self._runs: dict[str, _Run] = {}
        self._lock = threading.Lock()

    # ---- 清单 ----

    def drivers_found(self) -> bool:
        """驱动包找得到（不导入）、可执行文件在 PATH 里。"""
        find_spec, which = self._find_spec or importlib.util.find_spec, self._which or shutil.which
        try:
            packages = all(find_spec(item["module"]) is not None for item in self.info.get("drivers") or [])
        except (ImportError, ValueError):
            packages = False
        return packages and all(which(name) for name in self.info.get("executables") or [])

    def installed(self) -> bool:
        return self.secrets.ready() and self.drivers_found()

    def describe(self) -> dict:
        return catalog.describe(self.id, installed=self.installed())

    def tool(self) -> dict:
        drivers = self.info.get("drivers") or []
        if not drivers:
            return {"name": self.id, "version": self.version}
        try:
            version = importlib.metadata.version(drivers[0]["dist"])
        except importlib.metadata.PackageNotFoundError:
            version = ""
        return {"name": drivers[0]["dist"], "version": version}

    def targets(self, connection: dict) -> list[tuple[str, int]]:
        """连接参数里要连的主机和端口（任务说明核对用：都要在出口白名单里）。"""
        if self.info["kind"] == "code":
            url = str(connection.get("repo_url") or "")
            parts = urlsplit(url)
            try:
                port = parts.port or 443
            except ValueError:
                port = -1
            if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
                raise GatewayError(400, "bad_request", "repo_url 要是不带用户信息的 https 地址")
            return [(host_key(parts.hostname), port)]
        host = host_key(connection.get("host"))
        port = connection.get("port", self.info.get("default_port"))
        if isinstance(port, str) and port.isdigit():
            port = int(port)
        if not host or not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
            raise GatewayError(400, "bad_request", "连接参数要有 host、port")
        return [(host, port)]

    # ---- 子进程 ----

    def _head(self, spec: dict, budget: dict, operation: str) -> dict:
        head = {key: spec.get(key) for key in ("source", "scope", "egress", "rate_limit", "masking", "selector",
                                              "limit")}
        head.update({
            "provider": self.id, "operation": operation, "budget": budget, "module": catalog.module_of(self.id),
            "scheme": self.scheme, "executables": list(self.info.get("executables") or []),
            "limits": {"memory_bytes": self.memory_bytes, "cpu_seconds": int(budget["max_duration_s"]) + 5,
                       "file_bytes": self.file_bytes},
        })
        return head

    def _env(self, work: Path) -> dict:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "HOME": str(work),
               "TMPDIR": str(work)}
        env.update({key: value for key, value in os.environ.items() if key.startswith(PASS_ENV_PREFIXES)})
        return env

    def _start(self, job_id: str, spec: dict, budget: dict, operation: str, *, keep_records: bool,
               kill_after_s: float | None = None) -> _Run:
        cred_ref = spec.get("cred_ref")
        secret = self.secrets.open(cred_ref)
        directory = self.root / job_id
        shutil.rmtree(directory, ignore_errors=True)
        work = directory / "work"
        work.mkdir(parents=True, mode=0o700)
        run = _Run(job_id, self.id, directory, secret, keep_records=keep_records,
                   timeout_s=kill_after_s or budget["max_duration_s"] + self.kill_grace_s)
        self.secrets.acquire(cred_ref)
        released = threading.Event()

        def release():
            if not released.is_set():
                released.set()
                self.secrets.release(cred_ref)
                # 克隆的代码等工作文件在子进程结束时就清掉，只留 records.jsonl
                shutil.rmtree(work, ignore_errors=True)

        run.on_finish = release
        try:
            run.start(self._head(spec, budget, operation), self._env(work))
        except OSError:
            release()
            shutil.rmtree(directory, ignore_errors=True)
            raise GatewayError(503, "provider_unavailable", "起不了提供方子进程") from None
        return run

    def _run(self, job_id: str) -> _Run:
        with self._lock:
            run = self._runs.get(job_id)
        if run is None:
            raise GatewayError(404, "provider_job_missing", "网关里没有这个提供方任务")
        return run

    # ---- 任务（与 ReaStatic 同一套方法）----

    def submit(self, spec: dict, budget: dict, body, length: int) -> str:
        job_id = spec["job_id"]
        with self._lock:
            if job_id in self._runs:
                raise GatewayError(409, "job_exists", "任务已存在")
        run = self._start(job_id, spec, budget, str(spec["operation"]), keep_records=True)
        with self._lock:
            self._runs[job_id] = run
        _log.info("连接型提供方 %s 任务 %s 子进程已启动 pid=%s", self.id, job_id, run.process.pid)
        return ""

    def status(self, job_id: str) -> dict:
        return self._run(job_id).status()

    def cancel(self, job_id: str) -> None:
        self._run(job_id).cancel()

    def result(self, job_id: str) -> dict:
        run = self._run(job_id)
        outcome = run.outcome or {}
        tool = outcome.get("tool") if isinstance(outcome.get("tool"), dict) else self.tool()
        artifact = outcome.get("artifact") if isinstance(outcome.get("artifact"), dict) else {}
        return {"inventory": {"items": run.items, "skipped": run.skipped}, "limitations": run.limitations,
                "records": {kind: count for kind, count in run.counts.items() if kind in RECORD_TYPES},
                "coverage_pct": outcome.get("coverage_pct"), "tool": tool, "artifact": artifact,
                "access_log": list(run.access)}

    @contextmanager
    def records(self, job_id: str) -> Iterator[BinaryIO]:
        run = self._run(job_id)
        if run.records_path is None or not run.records_path.is_file():
            raise GatewayError(404, "provider_job_missing", "没有记录")
        try:
            with open(run.records_path, "rb") as stream:
                yield stream
        finally:
            # 任务表已经按类型写进快照包目录了，这一份不再留在内存盘上
            run.records_path.unlink(missing_ok=True)

    @contextmanager
    def raw(self, job_id: str, name: str) -> Iterator[BinaryIO]:
        raise GatewayError(404, "provider_job_missing", "连接型提供方没有原始结果")
        yield  # pragma: no cover

    def delete(self, job_id: str) -> None:
        with self._lock:
            run = self._runs.pop(job_id, None)
        if run is not None:
            run.cancel()
            run.finished.wait(timeout=10)
        shutil.rmtree(self.root / job_id, ignore_errors=True)

    # ---- 测试连接（POST /v1/sources/{源}/test）----

    def test(self, spec: dict, *, timeout_s: int = TEST_TIMEOUT_S) -> dict:
        """同步跑子进程的 test：回 `{ok, checks}`，失败时多 `error_code`、`error_message`。"""
        job_id = f"test-{os.urandom(6).hex()}"
        budget = {"max_rows": 1, "max_bytes": 1 << 20, "max_duration_s": timeout_s}
        run = self._start(job_id, spec, budget, "test", keep_records=False, kill_after_s=timeout_s + 2)
        try:
            run.finished.wait(timeout_s + 10)
        finally:
            run.cancel()
            shutil.rmtree(run.directory, ignore_errors=True)
        checks = [item for item in (run.outcome or {}).get("checks") or [] if isinstance(item, dict)][:20]
        checks = [{"name": str(item.get("name") or "")[:40], "ok": item.get("ok") is True,
                   "detail": str(item.get("detail") or "")[:300],
                   **({"latency_ms": bundle.as_count(item["latency_ms"])} if "latency_ms" in item else {})}
                  for item in checks]
        ok = run.state == "succeeded" and bool(checks) and all(item["ok"] for item in checks)
        reply: dict = {"ok": ok, "checks": checks}
        if run.state != "succeeded":
            reply["error_code"] = run.error_code or "provider_error"
            reply["error_message"] = run.error_message or ""
        return reply
