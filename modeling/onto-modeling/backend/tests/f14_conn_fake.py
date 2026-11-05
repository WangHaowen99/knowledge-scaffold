"""F14 测试专用的假连接型提供方：跑在 `conn.worker` 子进程里，按连接参数 `mode` 做不同的事。数据全部合成。"""

import hashlib
import os
import socket
import subprocess
import sys
import time

from app.connector_gateway.conn.context import ConnError


def _password(ctx) -> str:
    return str(ctx.secret.get("password") or "")


def _table(ctx, index: int) -> dict:
    return {"id": ctx.anchor(f"shop.t{index}"), "type": "entity", "parent": None, "name": f"t{index}", "label": None,
            "attrs": {"kind": "table"}, "evidence": {"provider": "f14-fake@0.1.0", "method": "fake",
                                                      "confidence": "declared"}}


def _proc_text(name: str) -> str:
    try:
        with open(f"/proc/self/{name}", "rb") as handle:
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _run(ctx):
    mode = ctx.connection.get("mode", "ok")
    password = _password(ctx)
    if mode == "ok":
        ctx.progress("读表结构", 0, 2, "张表")
        ctx.inventory([{"object": "shop.t0", "kind": "table", "estimate_rows": 12}],
                      [{"object": "shop.audit", "reason": "不在范围里"}])
        for index in range(int(ctx.connection.get("access", 1))):
            ctx.access(f"SELECT {index} FROM shop.t0", rows=1, size=10, duration_ms=2)
        for index in range(2):
            ctx.record(_table(ctx, index))
        ctx.limitation("视图定义没有读")
        artifact = ctx.connection.get("artifact")
        ctx.progress("读表结构", 2, 2, "张表")
        ctx.result(complete=True, artifact=artifact)
    elif mode == "inspect":
        environ = dict(os.environ)
        record = _table(ctx, 0)
        record["attrs"] = {
            "env_keys": sorted(environ),
            "env_has_secret": any(password in value for value in environ.values()),
            "proc_environ_has_secret": password in _proc_text("environ"),
            "argv_has_secret": password in " ".join(sys.argv) or password in _proc_text("cmdline"),
            "secret_sha": hashlib.sha256(password.encode()).hexdigest(),
            "stdin_closed": sys.stdin.closed or sys.stdin.buffer.closed,
        }
        ctx.record(record)
        ctx.result(complete=True)
    elif mode == "many":
        for index in range(10):
            ctx.progress("读表", index, 10, "张表")
            ctx.record(_table(ctx, index))
        ctx.result(complete=True)
    elif mode == "leak_record":
        record = _table(ctx, 0)
        record["label"] = f"连接串 user:{password}@db"
        ctx.record(record)
        print(f"stdout 里的 {password}")
        ctx.result(complete=True)
    elif mode == "raise_secret":
        raise RuntimeError(f"connect failed password={password}")
    elif mode == "connerror_secret":
        raise ConnError("auth_failed", f"认证失败：{password}")
    elif mode == "stderr_crash":
        sys.stderr.write(f"Traceback: password={password}\n")
        sys.stderr.flush()
        os._exit(3)
    elif mode == "no_result":
        ctx.record(_table(ctx, 0))
    elif mode == "connect":
        target = ctx.connection["target"]
        with socket.create_connection((target["host"], target["port"]), timeout=3):
            pass
        ctx.result(complete=True, checks=[{"name": "connectivity", "ok": True, "detail": "", "latency_ms": 1}])
    elif mode == "resolve":
        socket.getaddrinfo(ctx.connection["target_host"], 443)
        ctx.result(complete=True, checks=[{"name": "connectivity", "ok": True}])
    elif mode == "swallow":
        try:
            socket.create_connection(("192.0.2.10", 3306), timeout=1)
        except OSError:
            pass
        ctx.result(complete=True, checks=[{"name": "connectivity", "ok": True}])
    elif mode == "popen":
        subprocess.run(ctx.connection["argv"], capture_output=True, check=False)
        ctx.result(complete=True, checks=[{"name": "connectivity", "ok": True}])
    elif mode == "system":
        os.system("true")
        ctx.result(complete=True, checks=[{"name": "connectivity", "ok": True}])
    elif mode == "sleep_child":
        child = subprocess.Popen(["sleep", "60"])
        ctx.progress(f"child:{child.pid}", 0, 1, "")
        time.sleep(60)
        ctx.result(complete=True)
    else:
        raise ConnError("unknown_mode", mode)


def test(ctx):
    if ctx.connection.get("mode", "ok") == "ok":
        ctx.result(complete=True, checks=[
            {"name": "connectivity", "ok": True, "detail": "", "latency_ms": 3},
            {"name": "readonly_account", "ok": True, "detail": "只有 SELECT 权限"},
        ])
        return
    _run(ctx)


discover = snapshot = sample = _run
