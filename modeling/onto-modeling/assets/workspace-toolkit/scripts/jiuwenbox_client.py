#!/usr/bin/env python3
"""jiuwenbox 沙箱客户端，以及在沙箱里运行 ontology/scripts 脚本的执行器。

协议与 consumer（consumer/backend/integration/sandbox/client.py）一致：经 jiuwenbox 管理
socket 走 HTTP + JSON，建沙箱 → exec → 删沙箱。onto-modeling 的数据卷不在 jiuwenbox 容器里，
所以脚本和它 import 的同目录模块打包后随 exec 参数送进沙箱。

管理 socket 能决定沙箱怎么建（挂哪些目录、能否出网），只交给不跑 agent 的脚本验证网关容器
（backend/app/sandbox_gateway.py）：网关用本模块的 run_bundle / probe 按写死的策略直连 jiuwenbox。
onto-modeling 主容器里跑着能执行任意命令的建模 agent，不挂 socket，run_cases 经网关的 HTTP 接口
运行（ONTO_WEB_SANDBOX_GATEWAY_URL）。网关不可达或报 jiuwenbox 不可用时，同一个 runner 改在本机
（onto-modeling 容器）执行，结果注明 runtime=local 与原因：脚本运行验证一定会执行，只是隔离程度不同。

仅依赖标准库：script_verify.py、后端 sandbox_status 与网关共用本模块。
"""
from __future__ import annotations

import ast
import base64
import hashlib
import http.client
import io
import importlib.util
import inspect
import json
import os
import secrets
import socket
import subprocess
import sys
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

# This client is also loaded directly by the standalone gateway, without toolkit imports.
_output_spec = importlib.util.spec_from_file_location("onto_script_output", Path(__file__).with_name("script_output.py"))
_output_module = importlib.util.module_from_spec(_output_spec)
_output_spec.loader.exec_module(_output_module)

DEFAULT_SOCKET_PATH = "/run/jiuwenbox/jiuwenbox.sock"
GATEWAY_URL_ENV = "ONTO_WEB_SANDBOX_GATEWAY_URL"
SANDBOX_PYTHON = "/usr/bin/python3"
# 每个 exec 参数受 Linux MAX_ARG_STRLEN（128KB）限制，留出余量
MAX_ARGUMENT_BYTES = 96 * 1024
CASE_TIMEOUT_SECONDS = 20
MAX_EXEC_SECONDS = 300
OUTPUT_LIMIT = 1500
RESULT_PREFIX = "__ONTO_SCRIPT_VERIFY__"

_SYSTEM_PATHS = ["/bin", "/sbin", "/usr", "/usr/local", "/lib", "/lib64"]
_BLOCKED_SYSCALLS = [
    "kexec_load", "kexec_file_load", "reboot", "pivot_root", "swapon", "swapoff",
    "nfsservctl", "lookup_dcookie", "ptrace", "mount", "umount2", "unshare", "setns",
    "userfaultfd", "perf_event_open", "bpf", "add_key", "request_key", "keyctl",
    "io_uring_setup", "io_uring_enter", "io_uring_register", "open_by_handle_at",
    "name_to_handle_at", "init_module", "finit_module", "delete_module", "acct",
]
# 云厂商实例元数据地址：出网已默认全拒，这里再显式屏蔽，与 consumer（integration/sandbox/
# policy_builder.py）的沙箱策略一致。是屏蔽名单，不是连接目标。
_BLOCKED_METADATA_IPS = ("169.254.169.254/32",)  # NOSONAR(S1313)
# 脚本执行环境与 consumer 在沙箱里执行脚本时一致（consumer/backend/integration/sandbox/
# env_allowlist.py、executor.py：`/usr/bin/python3 -u <脚本> <参数>`），只是没有挂 consumer 的
# /app/sandbox-runtime/python。解释器不能加 -s / -E / -I：jiuwenbox 镜像是 openEuler，
# 它的 Python 把 /usr/local 下的 site-packages 与用户目录包绑在同一开关上，加 -s 就看不到
# pip 装进 /usr/local 的包（如 consumer 的运行时依赖、wheel 工具箱装的 ortools）。
SCRIPT_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}

# 在沙箱（或本机回退）里执行：解包脚本，逐个用例以子进程运行，只回传判定所需的事实。
# 执行器自身用 -I（只用标准库），用例子进程继承 SCRIPT_ENV、不加隔离参数，与 consumer 相同。
RUNNER = inspect.getsource(_output_module.output_error) + r'''
import base64, io, json, os, shutil, subprocess, sys, tempfile, time, zipfile

bundle, entry, encoded_cases, limit, timeout = sys.argv[1:6]
limit, timeout = int(limit), int(timeout)
cases = json.loads(base64.b64decode(encoded_cases).decode("utf-8"))
outputs = ()
if isinstance(cases, dict):
    outputs, cases = cases["outputs"], cases["argv_cases"]
workdir = tempfile.mkdtemp(prefix="onto-script-verify-")
zipfile.ZipFile(io.BytesIO(base64.b64decode(bundle))).extractall(workdir)


def as_text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value or ""


def single_json(text):
    text = text.strip()
    if not text:
        return False
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


results = []
try:
    for argv in cases:
        started = time.monotonic()
        command = [sys.executable, "-u", os.path.join(workdir, entry), *argv]
        try:
            done = subprocess.run(command, cwd=workdir, capture_output=True, timeout=timeout)
            stdout, stderr = as_text(done.stdout), as_text(done.stderr)
            exit_code, timed_out = done.returncode, False
        except subprocess.TimeoutExpired as exc:
            stdout, stderr, exit_code, timed_out = as_text(exc.stdout), as_text(exc.stderr), None, True
        contract_error = ""
        if outputs and single_json(stdout):
            contract_error = output_error(json.loads(stdout), outputs)
        results.append({
            "exit_code": exit_code,
            "timed_out": timed_out,
            "stdout_is_json": single_json(stdout),
            "stdout": stdout[:limit],
            "stdout_truncated": len(stdout) > limit,
            "stderr": stderr[-limit:],
            "duration_ms": int((time.monotonic() - started) * 1000),
            "output_contract_error": contract_error,
        })
finally:
    shutil.rmtree(workdir, ignore_errors=True)
print("__ONTO_SCRIPT_VERIFY__" + json.dumps(results))
'''


class SandboxUnavailable(Exception):
    """jiuwenbox 不可达：socket 不存在、连不上或健康检查失败。"""


class SandboxError(Exception):
    """jiuwenbox 可达，但沙箱接口出错或没有返回执行结果。"""


class BundleError(ValueError):
    """脚本或用例无法送进沙箱（文件缺失、超过参数上限）。"""


def default_url() -> str:
    """jiuwenbox 管理 socket：只有网关容器挂载它。"""
    return "unix://" + (os.environ.get("JIUWENBOX_SOCKET_PATH") or DEFAULT_SOCKET_PATH)


def gateway_url() -> str:
    """onto-modeling 主容器经网关运行验证；没配置时视为网关不可用（单独部署 onto-modeling 的情形）。"""
    return (os.environ.get(GATEWAY_URL_ENV) or "").strip().rstrip("/")


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float):
        super().__init__("jiuwenbox", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        family = getattr(socket, "AF_UNIX", None)
        if family is None:
            raise SandboxUnavailable("当前平台不支持 unix socket")
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect(self._socket_path)
        except OSError:
            sock.close()
            raise
        # 连上之后交给 HTTPConnection 持有，由 _request 里的 connection.close() 关闭
        self.sock = sock


def _request(url: str, method: str, path: str, payload=None, timeout: float = 10.0, *, peer: str = "jiuwenbox"):
    parts = urlsplit(url)
    if parts.scheme == "unix":
        if not os.path.exists(parts.path):
            raise SandboxUnavailable(f"未找到 jiuwenbox socket：{parts.path}")
        connection = _UnixHTTPConnection(parts.path, timeout)
        prefix = ""
    elif parts.scheme in ("http", "https") and parts.hostname:
        connection_class = (
            http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        )
        connection = connection_class(parts.hostname, parts.port, timeout=timeout)
        prefix = parts.path.rstrip("/")
    else:
        raise SandboxUnavailable(f"{peer} 地址必须是 unix:// 或 http(s)://：{url}")
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    try:
        connection.request(method, prefix + path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
    except (OSError, http.client.HTTPException) as exc:
        raise SandboxUnavailable(f"连接 {peer} 失败：{exc}") from exc
    finally:
        connection.close()
    text = raw.decode("utf-8", "replace")
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError:
        data = text
    return response.status, data


def _call(url: str, method: str, path: str, payload=None, timeout: float = 10.0):
    status, data = _request(url, method, path, payload, timeout)
    if status >= 400:
        detail = data.get("error") or data.get("detail") if isinstance(data, dict) else data
        raise SandboxError(f"jiuwenbox {method} {path} 返回 {status}：{str(detail or '')[:300]}")
    return data


def health(url: str | None = None, timeout: float = 3.0) -> None:
    status, _data = _request(url or default_url(), "GET", "/health", timeout=timeout)
    if status != 200:
        raise SandboxUnavailable(f"jiuwenbox 健康检查返回 {status}")


def sandbox_policy() -> dict:
    """只挂系统目录、仅 /tmp 可写、出网全拒。"""
    return {
        "version": 1,
        "name": "onto-modeling-script-verify",
        "filesystem_policy": {
            "directories": [{"path": "/tmp", "permissions": "1777"}],
            "read_only": [*_SYSTEM_PATHS, "/etc"],
            "read_write": ["/tmp"],
            "bind_mounts": [
                {"host_path": path, "sandbox_path": path, "mode": "ro"} for path in _SYSTEM_PATHS
            ],
            "device": [
                {"host_path": "/dev/null", "sandbox_path": "/dev/null"},
                {"host_path": "/dev/urandom", "sandbox_path": "/dev/urandom"},
            ],
        },
        "process": {"run_as_user": "sandbox", "run_as_group": "sandbox"},
        "namespace": {"user": True, "pid": True, "ipc": True, "cgroup": True, "uts": True},
        "landlock": {"compatibility": "best_effort"},
        "network": {
            "mode": "isolated",
            "uplink": {"nat": False, "interface": ""},
            "egress": {
                "default": "deny",
                "allowed_ports": [],
                "blocked_ips": list(_BLOCKED_METADATA_IPS),
                "blocked_ports": [53],
                "allowed_domains": [],
                "allowed_ips": [],
            },
        },
        "cgroup": {"memory_max": "256M", "cpu_max": 1, "pids_max": 64},
        "syscall": {
            "x86_64": {"blocked": _BLOCKED_SYSCALLS},
            "arm64": {"blocked": _BLOCKED_SYSCALLS},
        },
    }


def exec_in_sandbox(command: list[str], *, timeout_seconds: int, url: str | None = None) -> dict:
    """建一个一次性沙箱执行命令，无论成败都删除；返回 {exit_code, stdout, stderr}。"""
    url = url or default_url()
    sandbox_id = "omd-" + secrets.token_hex(6)[:11]
    _call(url, "POST", "/api/v1/sandboxes", {
        "sandbox_id": sandbox_id,
        "policy_mode": "override",
        "policy": sandbox_policy(),
    })
    try:
        result = _call(url, "POST", f"/api/v1/sandboxes/{sandbox_id}/exec", {
            "command": command,
            "workdir": "/tmp",
            "env": SCRIPT_ENV,
            "timeout_seconds": timeout_seconds,
        }, timeout=timeout_seconds + 15)
    finally:
        try:
            _request(url, "DELETE", f"/api/v1/sandboxes/{sandbox_id}")
        except SandboxUnavailable:
            pass
    if not isinstance(result, dict):
        raise SandboxError("jiuwenbox exec 返回的不是 JSON 对象")
    return result


MAX_PROBE_PACKAGES = 500
PROBE_SECONDS = 90
# 网关排队等空闲执行位的上限；调用方的 HTTP 超时再加上排队、健康检查、建删沙箱的耗时
GATEWAY_WAIT_SECONDS = 60
GATEWAY_OVERHEAD_SECONDS = GATEWAY_WAIT_SECONDS + 60
# RUNNER 每个用例结果都有的字段，script_verify 判定要用
RESULT_FIELDS = frozenset({
    "exit_code", "timed_out", "stdout_is_json", "stdout", "stderr", "duration_ms",
})
# 列出脚本运行验证所用 Python 环境的包：与 RUNNER 跑脚本同一个解释器、同一套环境变量、
# 同样不加隔离参数，执行 pip list --format=freeze（没有 pip 时读包元数据）；不联网
_PROBE_SOURCE = r'''
import importlib.metadata, json, subprocess, sys


def pip(*args):
    done = subprocess.run(
        [sys.executable, "-m", "pip", "--no-cache-dir", "--disable-pip-version-check", *args],
        capture_output=True, text=True, timeout=60,
    )
    if done.returncode != 0:
        raise RuntimeError((done.stderr or done.stdout).strip()[-200:])
    return done.stdout


result = {"path": sys.executable, "python": sys.version.split()[0], "pip": None}
try:
    result["pip"] = pip("--version").split()[1]
    packages = [line.split("==", 1) for line in pip("list", "--format=freeze").splitlines() if "==" in line]
except (RuntimeError, IndexError, OSError, subprocess.TimeoutExpired):
    packages = [
        [dist.metadata["Name"], dist.version]
        for dist in importlib.metadata.distributions() if dist.metadata["Name"]
    ]
result["packages"] = sorted(packages, key=lambda item: item[0].lower())
print(json.dumps(result))
'''


def probe(url: str | None = None) -> dict:
    """配置中心用的连接检测：健康检查 + 真建一个沙箱，列出脚本运行验证所用 Python 环境的包。

    返回 {connected, socket, detail, path, python, pip, packages: [{name, version}]}，包最多 500 个。
    """
    url = url or default_url()
    socket_path = urlsplit(url).path if url.startswith("unix://") else url
    unavailable = {
        "connected": False, "socket": socket_path, "path": SANDBOX_PYTHON,
        "python": None, "pip": None, "packages": [],
    }
    try:
        health(url)
        result = exec_in_sandbox(
            [SANDBOX_PYTHON, "-c", _PROBE_SOURCE], timeout_seconds=PROBE_SECONDS, url=url,
        )
    except (SandboxUnavailable, SandboxError) as exc:
        return {**unavailable, "detail": str(exc)}
    try:
        if result.get("exit_code") != 0:
            raise ValueError(str(result.get("stderr") or result.get("stdout") or "")[-300:])
        found = json.loads(str(result.get("stdout") or "").strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        return {**unavailable, "detail": f"沙箱内 Python 无法运行：{exc}"}
    return {
        "connected": True,
        "socket": socket_path,
        "detail": "jiuwenbox 可用，脚本运行验证在沙箱中执行",
        "path": found.get("path") or SANDBOX_PYTHON,
        "python": found.get("python"),
        "pip": found.get("pip"),
        "packages": [
            {"name": name, "version": version}
            for name, version in found.get("packages", [])[:MAX_PROBE_PACKAGES]
        ],
    }


def script_dependencies(script: Path) -> list[Path]:
    """脚本及其 import 的同目录模块（传递闭包），按文件名排序。"""
    directory = script.parent
    found: dict[str, Path] = {}
    pending = [script]
    while pending:
        current = pending.pop()
        if current.name in found:
            continue
        found[current.name] = current
        try:
            tree = ast.parse(current.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            else:
                continue
            for name in names:
                module = directory / f"{name}.py"
                if module.is_file() and module.name not in found:
                    pending.append(module)
    return sorted(found.values(), key=lambda path: path.name)


def source_hash(script: Path) -> str:
    """脚本连同依赖模块的内容哈希：任何一个文件变了，验证记录就失效。"""
    digest = hashlib.sha256()
    for path in script_dependencies(script):
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        digest.update(path.name.encode("utf-8") + b"\0" + text.encode("utf-8") + b"\0")
    return "sha256:" + digest.hexdigest()


def _bundle(script: Path) -> str:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in script_dependencies(script):
            archive.writestr(path.name, path.read_bytes())
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    if len(encoded) > MAX_ARGUMENT_BYTES:
        raise BundleError(f"脚本及其依赖模块打包后超过沙箱参数上限 {MAX_ARGUMENT_BYTES // 1024}KB")
    return encoded


def _encode_cases(argv_cases: list[list[str]], outputs=()) -> str:
    value = {"argv_cases": argv_cases, "outputs": outputs} if outputs else argv_cases
    encoded = base64.b64encode(json.dumps(value, ensure_ascii=False).encode("utf-8")).decode("ascii")
    if len(encoded) > MAX_ARGUMENT_BYTES:
        raise BundleError(f"用例参数超过沙箱参数上限 {MAX_ARGUMENT_BYTES // 1024}KB，请缩小验证用的输入数据")
    return encoded


def _parse_results(stdout: str, expected: int) -> list[dict]:
    line = next(
        (item for item in reversed(str(stdout or "").splitlines()) if item.startswith(RESULT_PREFIX)),
        None,
    )
    if line is None:
        raise SandboxError("没有返回脚本运行结果")
    try:
        results = json.loads(line[len(RESULT_PREFIX):])
    except ValueError as exc:
        raise SandboxError("返回的脚本运行结果不完整") from exc
    if not isinstance(results, list) or len(results) != expected:
        raise SandboxError("返回的用例结果数量不对")
    return results


def run_bundle(bundle: str, entry: str, encoded_cases: str, case_count: int, *, url: str | None = None) -> list[dict]:
    """在 jiuwenbox 沙箱里用 RUNNER 跑一个已打包的脚本：只在持有 socket 的网关里调用。

    策略、解释器、执行器、超时都在这里写死，调用方只能决定跑哪个脚本、带哪些用例。
    """
    runner_args = [bundle, entry, encoded_cases, str(OUTPUT_LIMIT), str(CASE_TIMEOUT_SECONDS)]
    health(url)
    response = exec_in_sandbox(
        [SANDBOX_PYTHON, "-I", "-c", RUNNER, *runner_args],
        timeout_seconds=exec_timeout(case_count), url=url,
    )
    if response.get("exit_code") != 0:
        raise SandboxError(
            f"沙箱内执行器异常退出：{str(response.get('stderr') or response.get('stdout') or '')[-300:]}"
        )
    return _parse_results(response.get("stdout"), case_count)


def exec_timeout(case_count: int) -> int:
    return min(CASE_TIMEOUT_SECONDS * case_count + 10, MAX_EXEC_SECONDS)


def _gateway_call(url: str, method: str, path: str, payload=None, timeout: float = 10.0) -> dict:
    """调用网关；网关不可达、或报 jiuwenbox 不可用（503）时抛 SandboxUnavailable，由调用方回退。"""
    status, data = _request(url, method, path, payload, timeout, peer="脚本验证网关")
    detail = data.get("error") if isinstance(data, dict) else data
    if status == 503:
        raise SandboxUnavailable(str(detail or "脚本验证网关不可用")[:300])
    if status >= 400:
        raise SandboxError(f"脚本验证网关 {method} {path} 返回 {status}：{str(detail or '')[:300]}")
    if not isinstance(data, dict):
        raise SandboxError("脚本验证网关返回的不是 JSON 对象")
    return data


def run_cases(script: Path, argv_cases: list[list[str]], *, gateway: str | None = None, outputs=()) -> dict:
    """经脚本验证网关在 jiuwenbox 沙箱里逐个用例运行脚本；网关或沙箱不可用时改在本机运行同一个 runner。

    返回 {runtime: "jiuwenbox" | "local", fallback_reason, results: [...]}，
    results 与 argv_cases 一一对应。
    """
    bundle = _bundle(script)
    encoded_cases = _encode_cases(argv_cases, outputs)
    timeout = exec_timeout(len(argv_cases))
    gateway = gateway if gateway is not None else gateway_url()
    try:
        if not gateway:
            raise SandboxUnavailable(f"未配置脚本验证网关（{GATEWAY_URL_ENV}）")
        response = _gateway_call(gateway, "POST", "/v1/verify", {
            "bundle": bundle, "entry": script.name, "cases": encoded_cases, "case_count": len(argv_cases),
        }, timeout=timeout + GATEWAY_OVERHEAD_SECONDS)
        results = response.get("results")
        if not isinstance(results, list) or len(results) != len(argv_cases) or not all(
            isinstance(item, dict) and RESULT_FIELDS <= set(item) for item in results
        ):
            raise SandboxError("脚本验证网关返回的用例结果不完整")
        if outputs and any("output_contract_error" not in item for item in results):
            raise SandboxError("脚本验证网关尚不支持输出契约检查，请升级网关")
        return {"runtime": "jiuwenbox", "fallback_reason": "", "results": results}
    except (SandboxUnavailable, SandboxError) as exc:
        reason = str(exc)
    runner_args = [bundle, script.name, encoded_cases, str(OUTPUT_LIMIT), str(CASE_TIMEOUT_SECONDS)]
    try:
        # 安全审查：argv 是当前解释器的绝对路径、本模块写死的执行器源码，以及 base64 编码的脚本包与用例、
        # 入口文件名和两个整数；以序列传入、不经 shell 解析，环境变量只给 SCRIPT_ENV。
        done = subprocess.run(  # NOSONAR(S4721)
            [sys.executable, "-I", "-c", RUNNER, *runner_args],
            env=_local_script_env(),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout + 10,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SandboxError("本机执行器超时") from exc
    if done.returncode != 0:
        raise SandboxError(f"本机执行器异常退出：{(done.stderr or done.stdout)[-300:]}")
    return {
        "runtime": "local",
        "fallback_reason": reason,
        "results": _parse_results(done.stdout, len(argv_cases)),
    }


def run_in_gateway(bundle: str, entry: str, argv_cases: list[list[str]], *, gateway: str | None = None) -> list[dict]:
    """经网关在 jiuwenbox 沙箱里跑一个已打包的脚本，不在本机回退：内核跑包里的脚本节点用（详设 6.5）。

    脚本来自包作者，不能在主容器里执行；网关没配、不可达或 jiuwenbox 不可用时抛 SandboxUnavailable。
    """
    if len(bundle) > MAX_ARGUMENT_BYTES:
        raise BundleError(f"脚本及其输入打包后超过沙箱参数上限 {MAX_ARGUMENT_BYTES // 1024}KB")
    encoded_cases = _encode_cases(argv_cases)
    gateway = gateway if gateway is not None else gateway_url()
    if not gateway:
        raise SandboxUnavailable(f"未配置脚本验证网关（{GATEWAY_URL_ENV}）")
    response = _gateway_call(gateway, "POST", "/v1/verify", {
        "bundle": bundle, "entry": entry, "cases": encoded_cases, "case_count": len(argv_cases),
    }, timeout=exec_timeout(len(argv_cases)) + GATEWAY_OVERHEAD_SECONDS)
    results = response.get("results")
    if not isinstance(results, list) or len(results) != len(argv_cases) or not all(
        isinstance(item, dict) and RESULT_FIELDS <= set(item) for item in results
    ):
        raise SandboxError("脚本验证网关返回的用例结果不完整")
    return results


def gateway_status(gateway: str | None = None) -> dict:
    """配置中心用：经网关查 jiuwenbox 连接状态与沙箱 Python 环境（字段同 probe，另带 gateway）。"""
    gateway = gateway if gateway is not None else gateway_url()
    unavailable = {
        "connected": False, "gateway": gateway, "socket": "", "path": SANDBOX_PYTHON,
        "python": None, "pip": None, "packages": [],
    }
    if not gateway:
        return {**unavailable, "detail": f"未配置脚本验证网关（{GATEWAY_URL_ENV}）"}
    try:
        status = _gateway_call(gateway, "GET", "/v1/status", timeout=PROBE_SECONDS + GATEWAY_OVERHEAD_SECONDS)
    except (SandboxUnavailable, SandboxError) as exc:
        return {**unavailable, "detail": str(exc)}
    return {**unavailable, **status, "gateway": gateway}


def _local_script_env() -> dict:
    """本机回退时同样只给 SCRIPT_ENV，不带 onto-modeling 进程自己的环境变量。

    SYSTEMROOT/TEMP/TMP 只在 Windows 开发机上存在，缺了它们 Python 子进程起不来。
    """
    return {
        **SCRIPT_ENV,
        **{key: os.environ[key] for key in ("SYSTEMROOT", "TEMP", "TMP") if key in os.environ},
    }
