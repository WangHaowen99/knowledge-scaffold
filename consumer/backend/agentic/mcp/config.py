"""MCP runtime configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..config import AppConfig


MIN_MCP_TOKEN_SECRET_LENGTH = 32


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

    def effective_public_base_url(self) -> str:
        if self.public_base_url:
            return self.public_base_url.rstrip("/")
        host = self.server_host
        if host in {"", "0.0.0.0", "::"}:
            host = "127.0.0.1"
        return "http://{}:{}".format(host, self.server_port)


def load_mcp_runtime_config() -> MCPRuntimeConfig:
    _load_local_dotenv()
    app_config = AppConfig.from_env()
    runtime_root = Path(app_config.project_root).resolve()
    return MCPRuntimeConfig(
        server_host=_read_env("MCP_SERVER_HOST", "0.0.0.0"),
        server_port=_read_int_env("MCP_SERVER_PORT", 7488),
        public_base_url=_read_env("MCP_PUBLIC_BASE_URL", ""),
        agentic_base_url=_read_env("MCP_AGENTIC_BASE_URL", "http://127.0.0.1:5055").rstrip("/"),
        token_secret=_read_env("MCP_TOKEN_SECRET", ""),
        token_algorithm=_read_env("MCP_TOKEN_ALGORITHM", "HS256").upper(),
        token_issuer=_read_env("MCP_TOKEN_ISSUER", "meta-wise"),
        token_audience=_read_env("MCP_TOKEN_AUDIENCE", "meta-wise-mcp"),
        token_ttl_seconds=max(60, _read_int_env("MCP_TOKEN_TTL_SECONDS", 24 * 3600)),
        token_max_ttl_seconds=max(60, _read_int_env("MCP_TOKEN_MAX_TTL_SECONDS", 30 * 24 * 3600)),
        http_timeout_seconds=max(1, _read_int_env("MCP_HTTP_TIMEOUT_SECONDS", 600)),
        transport=_read_env("MCP_TRANSPORT", "streamable-http").lower(),
        token_store_path=Path(
            _read_env("MCP_TOKEN_STORE_PATH", str(runtime_root / "mcp-tokens.json"))
        ).resolve(),
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
