from api.auth.auth import AuthManager


def test_create_user_ignores_legacy_ops_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_CONFIG_PATH", str(tmp_path / "missing-ops-config.json"))
    monkeypatch.setenv("USER_MAX_COUNT", "2")
    auth_manager = AuthManager(str(tmp_path / "users.json"), "test-secret")

    for username in ("operator", "auditor", "reviewer"):
        auth_manager.create_user(username, "StrongPass123!")

    assert len(auth_manager.list_users()) == 3
