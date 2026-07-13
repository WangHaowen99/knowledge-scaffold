import pytest

from api.auth.auth import AuthManager


def test_create_user_rejects_when_user_count_reaches_ops_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_CONFIG_PATH", str(tmp_path / "missing-ops-config.json"))
    monkeypatch.setenv("USER_MAX_COUNT", "2")
    auth_manager = AuthManager(str(tmp_path / "users.json"), "test-secret")

    auth_manager.create_user("operator", "StrongPass123!")

    with pytest.raises(ValueError, match="用户数量已达上限"):
        auth_manager.create_user("auditor", "OtherStrong123!")
