# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""ACP 配置管理模块."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class AgentConfig(BaseModel):
    name: str = "Onto-Agent"
    version: str = "1.0.0"
    description: str = "基于 AgentLoopEngine 的本体查询智能体"


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8200
    base_url: str = "http://localhost:8200"


class AuthenticationConfig(BaseModel):
    type: str = "jwt"
    api_key_header: str = "X-API-Key"
    api_keys: list = Field(default_factory=list)


class CapabilitiesConfig(BaseModel):
    supported_content_types: list = Field(default_factory=lambda: ["text/plain", "application/json"])
    supported_modes: list = Field(default_factory=lambda: ["sync", "async", "stream"])
    features: list = Field(default_factory=lambda: [
        "ontology_query",
        "entity_search",
        "relation_search",
        "multi_turn_conversation",
    ])


class RunConfig(BaseModel):
    timeout: int = 300
    max_concurrent_runs: int = 100
    storage_type: str = "memory"
    cleanup_interval: int = 3600


class StreamConfig(BaseModel):
    heartbeat_interval: int = 30
    max_event_size: int = 1048576


class ACPProtocolConfig(BaseModel):
    protocol_version: str = "1.0.0"
    prefix: str = "/acp"
    authentication: AuthenticationConfig = Field(default_factory=AuthenticationConfig)
    capabilities: CapabilitiesConfig = Field(default_factory=CapabilitiesConfig)
    run: RunConfig = Field(default_factory=RunConfig)
    stream: StreamConfig = Field(default_factory=StreamConfig)


class LoggingConfig(BaseModel):
    level: str = "INFO"
    format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


class HealthCheckConfig(BaseModel):
    enabled: bool = True
    endpoint: str = "/health"


class ACPConfig(BaseModel):
    agent: AgentConfig = Field(default_factory=AgentConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    acp: ACPProtocolConfig = Field(default_factory=ACPProtocolConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    health_check: HealthCheckConfig = Field(default_factory=HealthCheckConfig)


_config_instance: ACPConfig | None = None


def _get_default_config_path() -> Path:
    return Path(__file__).parent.parent.parent.parent / ".onto-runtime" / "acp_config.json"


def load_config(config_path: Path | None = None) -> ACPConfig:
    global _config_instance
    if config_path is None:
        config_path = _get_default_config_path()
    if not config_path.exists():
        return ACPConfig()
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config_data = json.load(f) or {}
    except Exception as e:
        logger.warning("Failed to load ACP config from %s: %s", config_path, e)
        return ACPConfig()
    _config_instance = ACPConfig(**config_data)
    return _config_instance


def get_config(reload: bool = False) -> ACPConfig:
    global _config_instance
    if _config_instance is None or reload:
        return load_config()
    return _config_instance
