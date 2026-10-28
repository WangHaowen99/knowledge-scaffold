"""Provider-owned configuration (frozen; unknown keys are rejected).

Two kinds of hosts create this harness:

* a host that knows the provider (the da-analyst plugin) passes the DA keys
  directly;
* an openJiuwen team spawning a third-party provider member passes only the
  keys every provider shares (``cwd``, ``env``, ``skills`` ...). Those are
  accepted, and the engine settings fall back to ``<prefix>*`` variables
  (``DA_*`` for DeepAnalyze) read from that ``env`` mapping, then from the
  process environment.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit
from dataclasses import dataclass, field, fields
from typing import Any, Mapping

# Keys an openJiuwen host hands every provider package; DA has no use for them.
HOST_KEYS = frozenset({
    "cwd", "inherit_process_env", "skills", "skill_conflict", "cli_path", "system_prompt_mode", "model", "effort",
})

# Config field -> environment variable (after the engine prefix) consulted when the field is absent.
ENV_FALLBACKS = {
    "base_url": "BASE_URL",
    "tenant_id": "TENANT_ID",
    "kb_ids": "KB_IDS",
    "kb_catalog_url": "KB_CATALOG_URL",
    "kb_catalog_key_env": "KB_CATALOG_KEY_ENV",
    "mcp_bridge_host": "MCP_BRIDGE_HOST",
}


@dataclass(frozen=True)
class DeepAnalyzeConfig:
    """How to reach one DA engine.

    Secrets never live in config values: ``secret_env`` / ``kb_catalog_key_env``
    name the environment variables holding them.
    """

    base_url: str
    secret_env: str = "DA_ENGINE_SECRET"
    tenant_id: str = ""
    session_prefix: str = ""
    kb_ids: tuple[str, ...] = ()
    # Knowledge-base catalog (kb-service ``GET /v1/kbs``) used when no kb_ids are
    # given: DA hides its knowledge tools without a scope, so a team member
    # searches every knowledge base of the tenant, re-listed before each turn.
    kb_catalog_url: str = ""
    kb_catalog_key_env: str = ""
    # Host name DA uses to reach this process for bridged MCP servers; empty
    # disables the bridge (and the MCP_TOOLS capability is then unused).
    mcp_bridge_host: str = ""
    agent_type: str = ""
    max_turns: int | None = None
    connect_timeout_s: float = 10.0
    read_idle_timeout_s: float = 90.0
    reconnect_max_attempts: int = 5
    reconnect_backoff_s: float = 1.0
    abort_wait_s: float = 15.0
    # DA's cancel does not stop the engine run, so the conversation reports
    # ENGINE_BUSY until it drains; a turn that fails that way before any
    # output is retried for up to busy_retry_s.
    busy_retry_s: float = 180.0
    busy_poll_s: float = 3.0
    unanswered_reply: str = "（当前无法向用户提问，请基于已有信息继续完成任务）"
    # Environment handed over by the host; consulted for secrets.
    env: Mapping[str, str] = field(default_factory=dict, repr=False, compare=False)

    def getenv(self, name: str) -> str:
        return self.env.get(name) or os.environ.get(name, "")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None, *, env_prefix: str = "DA_") -> "DeepAnalyzeConfig":
        """Validate a host mapping; ``env_prefix`` names the engine's variables (``DA_``, ``LF_`` ...)."""
        data = {k: v for k, v in dict(raw or {}).items() if k not in HOST_KEYS}
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ValueError(f"unknown deepanalyze config keys: {', '.join(unknown)}")
        env = {str(k): str(v) for k, v in dict(data.get("env") or {}).items()}
        data["env"] = env
        for key, suffix in ENV_FALLBACKS.items():
            if not data.get(key):
                var = env_prefix + suffix
                value = env.get(var) or os.environ.get(var, "")
                if value:
                    data[key] = value
        data.setdefault("secret_env", f"{env_prefix}ENGINE_SECRET")
        base_url = str(data.get("base_url") or "").strip()
        parts = urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError(f"base_url (or {env_prefix}BASE_URL) must be an http(s) URL")
        data["base_url"] = base_url.rstrip("/")
        kb_ids = data.get("kb_ids") or ()
        if isinstance(kb_ids, str):
            kb_ids = [part for part in kb_ids.split(",") if part.strip()]
        data["kb_ids"] = tuple(str(kb).strip() for kb in kb_ids if str(kb).strip())
        for key in ("connect_timeout_s", "read_idle_timeout_s", "reconnect_backoff_s", "abort_wait_s",
                    "busy_retry_s", "busy_poll_s"):
            if key in data:
                data[key] = float(data[key])
                if data[key] <= 0:
                    raise ValueError(f"deepanalyze config {key} must be positive")
        if "reconnect_max_attempts" in data:
            data["reconnect_max_attempts"] = max(0, int(data["reconnect_max_attempts"]))
        if data.get("max_turns") is not None:
            data["max_turns"] = int(data["max_turns"])
        return cls(**data)
