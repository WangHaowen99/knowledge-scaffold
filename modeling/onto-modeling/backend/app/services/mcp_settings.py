"""MCP 服务器配置：存于数据目录，按需装配进 Agent 会话。

变更只影响新建的会话；已运行的会话保持原配置。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .. import config

TRANSPORTS = ("http", "sse", "stdio")
NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")


class McpConfigError(Exception):
    pass


def _file() -> Path:
    return Path(config.DATA_DIR) / "mcp-servers.json"


def load() -> list[dict]:
    path = _file()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def save(servers: list[dict]) -> list[dict]:
    cleaned = [validate(entry) for entry in servers]
    names = [entry["name"] for entry in cleaned]
    if len(names) != len(set(names)):
        raise McpConfigError("服务器名称重复")
    path = _file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")
    return cleaned


def validate(entry: dict) -> dict:
    name = str(entry.get("name", "")).strip()
    if not NAME_RE.fullmatch(name):
        raise McpConfigError("名称须为 1-32 位字母/数字/下划线/连字符")
    transport = entry.get("transport", "http")
    if transport not in TRANSPORTS:
        raise McpConfigError(f"类型必须是 {TRANSPORTS} 之一")
    cleaned = {
        "name": name,
        "transport": transport,
        "enabled": bool(entry.get("enabled", True)),
    }
    if transport in ("http", "sse"):
        url = str(entry.get("url", "")).strip()
        if not url.startswith(("http://", "https://")):
            raise McpConfigError(f"{name}: url 必须以 http(s):// 开头")
        cleaned["url"] = url
    else:
        command = str(entry.get("command", "")).strip()
        if not command:
            raise McpConfigError(f"{name}: stdio 类型必须填写 command")
        cleaned["command"] = command
        args = entry.get("args") or []
        if isinstance(args, str):
            args = args.split()
        cleaned["args"] = [str(a) for a in args]
    return cleaned


def sdk_config(disabled_names: set[str] | None = None) -> dict:
    """转换为 ClaudeAgentOptions.mcp_servers 需要的 dict。

    disabled_names：项目级停用清单（全局启用的服务器可按项目关闭）。
    """
    servers = {}
    for entry in load():
        if not entry.get("enabled", True):
            continue
        if disabled_names and entry["name"] in disabled_names:
            continue
        if entry["transport"] == "http":
            servers[entry["name"]] = {"type": "http", "url": entry["url"]}
        elif entry["transport"] == "sse":
            servers[entry["name"]] = {"type": "sse", "url": entry["url"]}
        else:
            servers[entry["name"]] = {
                "type": "stdio",
                "command": entry["command"],
                "args": entry.get("args", []),
            }
    return servers


def test_server(entry: dict) -> dict:
    """连通性测试：http/sse 探测 URL 可达（任何 HTTP 响应都算可达），
    stdio 检查命令可执行。不做完整 MCP 握手（避免 stdio 任意命令被这里拉起）。"""
    cleaned = validate(entry)
    if cleaned["transport"] in ("http", "sse"):
        import urllib.error
        import urllib.request

        request = urllib.request.Request(cleaned["url"], method="GET")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return {"ok": True, "detail": f"HTTP {response.status}"}
        except urllib.error.HTTPError as exc:
            # 405/406 等也说明端点存在并在响应
            return {"ok": True, "detail": f"HTTP {exc.code}"}
        except Exception as exc:
            return {"ok": False, "detail": f"连接失败：{exc}"}
    import shutil

    found = shutil.which(cleaned["command"])
    if found:
        return {"ok": True, "detail": f"命令可执行：{found}"}
    return {"ok": False, "detail": f"命令不存在：{cleaned['command']}"}
