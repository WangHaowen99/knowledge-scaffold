"""MCP runtime configuration."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from config import AppConfig


class MCPConfigurationError(RuntimeError):
    """Raised when the standalone MCP runtime is configured unsafely."""


MIN_MCP_TOKEN_SECRET_LENGTH = 32


def _is_loopback_host(host: str) -> bool:
    normalized = str(host or "").strip().lower().rstrip(".")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _validate_public_base_url(value: str) -> str:
    normalized = str(value or "").strip().rstrip("/")
    if not normalized:
        return ""
    try:
        parsed = urlsplit(normalized)
        parsed.port
    except ValueError as exc:
        raise ValueError("MCP_PUBLIC_BASE_URL 不是有效 URL") from exc
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("MCP_PUBLIC_BASE_URL 必须是无用户凭据的完整 URL")
    if parsed.query or parsed.fragment:
        raise ValueError("MCP_PUBLIC_BASE_URL 不能包含查询参数或片段")
    if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
        raise ValueError("MCP_PUBLIC_BASE_URL 对外地址必须使用 HTTPS")
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("MCP_PUBLIC_BASE_URL 仅支持 HTTPS 或本机回环 HTTP")
    return normalized


def _read_env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip() or default


def _read_int_env(name: str, default: int) -> int:
    raw = _read_env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _load_local_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return

    backend_env = Path(__file__).resolve().parents[2] / ".env"
    app_env = backend_env.parent.parent / ".env"
    if backend_env.exists():
        load_dotenv(backend_env, override=False)
    elif app_env.exists():
        load_dotenv(app_env, override=False)


@dataclass(frozen=True)
class MCPRuntimeConfig:
    server_host: str
    server_port: int
    public_base_url: str
    agentic_base_url: str
    token_secret: str
    token_algorithm: str
    token_issuer: str
    token_audience: str
    token_ttl_seconds: int
    token_max_ttl_seconds: int
    http_timeout_seconds: int
    transport: str
    token_store_path: Path
    chat_trace_max_events: int = 30
    chat_trace_max_bytes: int = 32768
    chat_trace_max_text_chars: int = 500
    chat_progress_max_events: int = 100
    chat_progress_max_message_bytes: int = 4096
    chat_progress_queue_size: int = 128
    chat_progress_heartbeat_seconds: int = 15

    def effective_public_base_url(self) -> str:
        if self.public_base_url:
            return _validate_public_base_url(self.public_base_url)
        host = str(self.server_host or "").strip()
        if host in {"", "0.0.0.0", "::"}:
            host = "127.0.0.1"
        if not _is_loopback_host(host):
            return ""
        rendered_host = "[{}]".format(host) if ":" in host else host
        return urlunsplit(("http", "{}:{}".format(rendered_host, self.server_port), "", "", ""))


def load_mcp_runtime_config() -> MCPRuntimeConfig:
    _load_local_dotenv()
    app_config = AppConfig.from_env()
    runtime_root = Path(app_config.project_root).resolve()
    token_secret = _read_env("MCP_TOKEN_SECRET", "")
    if not token_secret:
        raise MCPConfigurationError("MCP_TOKEN_SECRET 必须独立配置")
    token_algorithm = _read_env("MCP_TOKEN_ALGORITHM", "HS256").upper()
    if token_algorithm != "HS256":
        raise MCPConfigurationError("MCP_TOKEN_ALGORITHM 仅支持 HS256")
    transport = _read_env("MCP_TRANSPORT", "both").lower()
    if transport == "http":
        transport = "streamable-http"
    if transport not in {"both", "streamable-http", "sse", "stdio"}:
        raise MCPConfigurationError("MCP_TRANSPORT 必须是 both、streamable-http、sse 或 stdio")
    return MCPRuntimeConfig(
        server_host=_read_env("MCP_SERVER_HOST", "0.0.0.0"),
        server_port=_read_int_env("MCP_SERVER_PORT", 7488),
        public_base_url=_read_env("MCP_PUBLIC_BASE_URL", ""),
        agentic_base_url=_read_env("MCP_AGENTIC_BASE_URL", "http://127.0.0.1:5055").rstrip("/"),
        token_secret=token_secret,
        token_algorithm=token_algorithm,
        token_issuer=_read_env("MCP_TOKEN_ISSUER", "meta-wise"),
        token_audience=_read_env("MCP_TOKEN_AUDIENCE", "meta-wise-mcp"),
        token_ttl_seconds=max(60, _read_int_env("MCP_TOKEN_TTL_SECONDS", 24 * 3600)),
        token_max_ttl_seconds=max(60, _read_int_env("MCP_TOKEN_MAX_TTL_SECONDS", 30 * 24 * 3600)),
        http_timeout_seconds=max(1, _read_int_env("MCP_HTTP_TIMEOUT_SECONDS", 600)),
        transport=transport,
        token_store_path=Path(
            _read_env("MCP_TOKEN_STORE_PATH", str(runtime_root / "mcp-tokens.json"))
        ).resolve(),
        chat_trace_max_events=min(
            500,
            max(1, _read_int_env("MCP_CHAT_TRACE_MAX_EVENTS", 30)),
        ),
        chat_trace_max_bytes=min(
            1024 * 1024,
            max(1024, _read_int_env("MCP_CHAT_TRACE_MAX_BYTES", 32768)),
        ),
        chat_trace_max_text_chars=min(
            4000,
            max(32, _read_int_env("MCP_CHAT_TRACE_MAX_TEXT_CHARS", 500)),
        ),
        chat_progress_max_events=min(
            500,
            max(1, _read_int_env("MCP_CHAT_PROGRESS_MAX_EVENTS", 100)),
        ),
        chat_progress_max_message_bytes=min(
            16384,
            max(1024, _read_int_env("MCP_CHAT_PROGRESS_MAX_MESSAGE_BYTES", 4096)),
        ),
        chat_progress_queue_size=min(
            512,
            max(16, _read_int_env("MCP_CHAT_PROGRESS_QUEUE_SIZE", 128)),
        ),
        chat_progress_heartbeat_seconds=min(
            60,
            max(5, _read_int_env("MCP_CHAT_PROGRESS_HEARTBEAT_SECONDS", 15)),
        ),
    )


def validate_mcp_runtime_config(config: MCPRuntimeConfig) -> None:
    """Reject unsafe token signing configuration before issuing or accepting tokens."""
    if len(config.token_secret) < MIN_MCP_TOKEN_SECRET_LENGTH:
        raise ValueError(
            "MCP_TOKEN_SECRET 必须显式配置，且长度不能少于 {} 个字符".format(
                MIN_MCP_TOKEN_SECRET_LENGTH
            )
        )
    if config.token_algorithm != "HS256":
        raise ValueError("MCP_TOKEN_ALGORITHM 目前仅支持 HS256")
    _validate_public_base_url(config.public_base_url)
