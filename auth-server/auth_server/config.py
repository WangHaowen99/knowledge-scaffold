# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""全部来自环境变量的 auth server 配置。"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

logger = logging.getLogger(__name__)

_DOTENV_PATH = Path(__file__).resolve().parent.parent / ".env"

# 架构决策 D2 硬约束：access token TTL 锁死短值，沿用 12h 即安全回归。
ACCESS_TTL_MAX_SECONDS = 3600
TTL_MIN_SECONDS = 60

DEFAULT_ACCESS_TTL_SECONDS = 900
DEFAULT_REFRESH_TTL_SECONDS = 259_200  # 72h
DEFAULT_ISSUER = "http://auth-server:5056"
DEFAULT_AUDIENCE = "meta-wise"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5056
# 0 disables the user-count limit. Deployments may set a positive value.
DEFAULT_USER_MAX_COUNT = 0
DEFAULT_ENABLE_REGISTRATION = True


def _load_local_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError:
        return

    load_dotenv(dotenv_path=_DOTENV_PATH, override=False)


def _int_env(env: Mapping[str, str], name: str, default: int) -> int:
    raw = str(env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r 不是合法整数，使用默认值 %s", name, raw, default)
        return default


def _bool_env(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = str(env.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class AuthServerConfig:
    data_dir: Path = Path("./data")
    issuer: str = DEFAULT_ISSUER
    audience: str = DEFAULT_AUDIENCE
    access_ttl_seconds: int = DEFAULT_ACCESS_TTL_SECONDS
    refresh_ttl_seconds: int = DEFAULT_REFRESH_TTL_SECONDS
    admin_token: Optional[str] = None
    bootstrap_admin_password: Optional[str] = None
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    user_max_count: int = DEFAULT_USER_MAX_COUNT
    enable_registration: bool = DEFAULT_ENABLE_REGISTRATION

    def __post_init__(self) -> None:
        object.__setattr__(self, "data_dir", Path(self.data_dir))

        access = int(self.access_ttl_seconds)
        if access > ACCESS_TTL_MAX_SECONDS:
            logger.warning(
                "AUTH_ACCESS_TTL_SECONDS=%s 超过硬上限 %s（架构决策 D2），已钳制为 %s",
                access,
                ACCESS_TTL_MAX_SECONDS,
                ACCESS_TTL_MAX_SECONDS,
            )
            access = ACCESS_TTL_MAX_SECONDS
        object.__setattr__(self, "access_ttl_seconds", max(TTL_MIN_SECONDS, access))

        object.__setattr__(
            self, "refresh_ttl_seconds", max(TTL_MIN_SECONDS, int(self.refresh_ttl_seconds))
        )

        admin_token = str(self.admin_token or "").strip() or None
        object.__setattr__(self, "admin_token", admin_token)

        bootstrap_admin_password = str(self.bootstrap_admin_password or "") or None
        object.__setattr__(self, "bootstrap_admin_password", bootstrap_admin_password)

        # User-count limits are retired; retain the field for old callers.
        object.__setattr__(self, "user_max_count", 0)

        object.__setattr__(self, "enable_registration", bool(self.enable_registration))

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "AuthServerConfig":
        if env is None:
            _load_local_dotenv()
            env = os.environ
        return cls(
            data_dir=Path(str(env.get("AUTH_SERVER_DATA_DIR") or "./data")),
            issuer=str(env.get("AUTH_SERVER_ISSUER") or DEFAULT_ISSUER).strip(),
            audience=str(env.get("AUTH_SERVER_AUDIENCE") or DEFAULT_AUDIENCE).strip(),
            access_ttl_seconds=_int_env(env, "AUTH_ACCESS_TTL_SECONDS", DEFAULT_ACCESS_TTL_SECONDS),
            refresh_ttl_seconds=_int_env(env, "AUTH_REFRESH_TTL_SECONDS", DEFAULT_REFRESH_TTL_SECONDS),
            admin_token=env.get("AUTH_SERVER_ADMIN_TOKEN"),
            bootstrap_admin_password=env.get("AUTH_BOOTSTRAP_ADMIN_PASSWORD"),
            host=str(env.get("AUTH_SERVER_HOST") or DEFAULT_HOST).strip(),
            port=_int_env(env, "AUTH_SERVER_PORT", DEFAULT_PORT),
            user_max_count=DEFAULT_USER_MAX_COUNT,
            enable_registration=_bool_env(
                env, "AUTH_ENABLE_REGISTRATION", DEFAULT_ENABLE_REGISTRATION
            ),
        )
