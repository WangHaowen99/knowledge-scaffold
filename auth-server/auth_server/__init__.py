# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""预置 auth server v1：标准 OAuth2/OIDC 子集（密码模式 + refresh + revoke + JWKS + discovery）。

与 consumer 之间禁止任何 Python import，唯一接口是 HTTP（见
consumer/docs/decisions/2026-07-09-decoupled-auth-architecture.md）。
"""

from __future__ import annotations

from typing import Optional

from flask import Flask

from .config import AuthServerConfig
from .keys import KeyManager
from .registration import RegistrationService
from .routes import bp
from .tokens import TokenService
from .users import UserStore

__all__ = ["create_app"]


def create_app(config: Optional[AuthServerConfig] = None) -> Flask:
    cfg = config or AuthServerConfig.from_env()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)

    app = Flask(__name__)
    user_store = UserStore(
        cfg.data_dir / "users.json",
        user_max_count=cfg.user_max_count,
        bootstrap_admin_password=cfg.bootstrap_admin_password,
    )
    key_manager = KeyManager(cfg.data_dir / "keys")
    token_service = TokenService(cfg, key_manager)

    registration_service = None
    if cfg.enable_registration:
        registration_service = RegistrationService(user_store, cfg.data_dir)

    app.config["AUTH_SERVER_CONFIG"] = cfg
    app.config["USER_STORE"] = user_store
    app.config["KEY_MANAGER"] = key_manager
    app.config["TOKEN_SERVICE"] = token_service
    app.config["REGISTRATION_SERVICE"] = registration_service

    app.register_blueprint(bp)
    return app
