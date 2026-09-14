# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from api.auth.auth import AuthManager
from backend_synthetic_credentials import AUTH_SIGNING_SECRET


def test_create_user_ignores_legacy_ops_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_CONFIG_PATH", str(tmp_path / "missing-ops-config.json"))
    monkeypatch.setenv("USER_MAX_COUNT", "2")
    auth_manager = AuthManager(str(tmp_path / "users.json"), AUTH_SIGNING_SECRET)

    for username in ("operator", "auditor", "reviewer"):
        auth_manager.create_user(username, "StrongPass123!")

    usernames = {item["username"] for item in auth_manager.list_users()}
    assert {"operator", "auditor", "reviewer"} <= usernames
