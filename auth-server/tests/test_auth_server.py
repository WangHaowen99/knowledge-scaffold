# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""auth server v1 端到端测试（Flask test client，不起真服务）。"""

from __future__ import annotations

import json

import pytest
from auth_server import create_app
from auth_server.config import AuthServerConfig
from auth_server.users import UserStore
from auth_server_synthetic_credentials import (
    ADMIN_TOKEN,
    BOOTSTRAP_ADMIN_PASSWORD,
    COMMON_PASSWORD,
    INVALID_PASSWORD,
    INVALID_TOKEN,
    LEGACY_PASSWORD,
    NEW_STRONG_PASSWORD,
    STRONG_PASSWORD,
    username_password_violation,
)
from authlib.jose import JsonWebKey, JsonWebToken

ISSUER = "https://service.example.net"
AUDIENCE = "knowledge-scaffold"


def make_config(tmp_path, **overrides):
    params = dict(
        data_dir=tmp_path / "data",
        issuer=ISSUER,
        audience=AUDIENCE,
        access_ttl_seconds=900,
        refresh_ttl_seconds=259_200,
        admin_token=ADMIN_TOKEN,
        bootstrap_admin_password=BOOTSTRAP_ADMIN_PASSWORD,
        host="127.0.0.1",
        port=5056,
        user_max_count=10,
    )
    params.update(overrides)
    return AuthServerConfig(**params)


@pytest.fixture()
def client(tmp_path):
    return create_app(make_config(tmp_path)).test_client()


@pytest.fixture()
def registration_disabled_client(tmp_path):
    return create_app(make_config(tmp_path, enable_registration=False)).test_client()


def login(client, username="admin", password=BOOTSTRAP_ADMIN_PASSWORD):
    return client.post(
        "/token",
        data={"grant_type": "password", "username": username, "password": password},
    )


def refresh(client, refresh_token):
    return client.post("/token", json={"grant_type": "refresh_token", "refresh_token": refresh_token})


def decode_with_jwks(client, token):
    key_set = JsonWebKey.import_key_set(client.get("/jwks.json").get_json())
    claims = JsonWebToken(["RS256"]).decode(token, key_set)
    claims.validate()
    return claims


def admin_headers():
    return {"X-Admin-Token": ADMIN_TOKEN}


def create_user(client, username, password=STRONG_PASSWORD, role="user"):
    return client.post(
        "/admin/users",
        json={"username": username, "password": password, "role": role},
        headers=admin_headers(),
    )


# ------------------------------------------------------------- 首启与密码模式


def test_bootstrap_and_password_grant_login(client, tmp_path):
    resp = login(client)
    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body) == {"access_token", "refresh_token", "token_type", "expires_in"}
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 900

    claims = decode_with_jwks(client, body["access_token"])
    assert claims["iss"] == ISSUER
    assert claims["aud"] == AUDIENCE
    assert claims["sub"] == "admin"
    assert claims["role"] == "admin"
    assert claims["typ"] == "access"
    assert claims["ver"] == 1
    assert claims["pwd_cr"] is False
    assert claims["exp"] - claims["iat"] == 900

    users_file = tmp_path / "data" / "users.json"
    data = json.loads(users_file.read_text(encoding="utf-8"))
    (record,) = data["users"]
    assert record["username"] == "admin"
    assert record["must_change_password"] is False
    assert record["password_changed_at"]
    assert record["password_hash"].startswith("pbkdf2_sha256$600000$")


def test_wrong_password_invalid_grant(client):
    resp = login(client, password=NEW_STRONG_PASSWORD)
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"] == "invalid_grant"
    assert "error_description" in body


def test_unsupported_grant_type(client):
    resp = client.post("/token", data={"grant_type": "client_credentials"})
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "unsupported_grant_type"}


def test_token_accepts_json_body(client):
    resp = client.post(
        "/token",
        json={
            "grant_type": "password",
            "username": "admin",
            "password": BOOTSTRAP_ADMIN_PASSWORD,
        },
    )
    assert resp.status_code == 200


# --------------------------------------------------------------------- refresh


def test_refresh_rotates_token_pair(client):
    first = login(client).get_json()
    resp = refresh(client, first["refresh_token"])
    assert resp.status_code == 200
    second = resp.get_json()
    # refresh token 必然轮换（jti 随机）；access claims 不含随机成分，
    # 同一秒内换发的 access token 与原 token 字节级相同属预期（RS256 签名确定性）
    assert second["refresh_token"] != first["refresh_token"]
    access_claims = decode_with_jwks(client, second["access_token"])
    assert access_claims["typ"] == "access"
    assert access_claims["sub"] == "admin"

    claims = decode_with_jwks(client, second["refresh_token"])
    assert claims["typ"] == "refresh"
    assert claims["sub"] == "admin"
    assert claims["ver"] == 1
    assert "jti" in claims

    # 新 refresh 依然可用（预置 v1 不做单次轮换失效）
    assert refresh(client, second["refresh_token"]).status_code == 200


def test_refresh_rejects_access_token_and_garbage(client):
    body = login(client).get_json()
    assert refresh(client, body["access_token"]).status_code == 400
    assert refresh(client, INVALID_TOKEN).status_code == 400
    assert refresh(client, "").status_code == 400


# --------------------------------------------------------------------- 吊销


def test_revoke_invalidates_refresh_but_allows_new_login(client):
    body = login(client).get_json()
    resp = client.post("/revoke", data={"token": body["refresh_token"]})
    assert resp.status_code == 200
    assert resp.get_json() == {}

    # 旧 refresh 失效
    assert refresh(client, body["refresh_token"]).status_code == 400

    # 新登录可用，且 ver 已递增
    again = login(client)
    assert again.status_code == 200
    claims = decode_with_jwks(client, again.get_json()["access_token"])
    assert claims["ver"] == 2
    assert refresh(client, again.get_json()["refresh_token"]).status_code == 200


def test_revoke_always_returns_200(client):
    assert client.post("/revoke", data={"token": INVALID_TOKEN}).status_code == 200
    assert client.post("/revoke", json={"token": ""}).status_code == 200
    # access token 不是合法 refresh token，同样 200 且不吊销
    body = login(client).get_json()
    assert client.post("/revoke", data={"token": body["access_token"]}).status_code == 200
    assert refresh(client, body["refresh_token"]).status_code == 200


def test_admin_reset_revokes_refresh_without_requiring_password_change(client):
    assert create_user(client, "alice").status_code == 201
    old = login(client, "alice", STRONG_PASSWORD).get_json()

    resp = client.put("/admin/users/alice", json={"password": NEW_STRONG_PASSWORD}, headers=admin_headers())
    assert resp.status_code == 200
    assert resp.get_json()["user"] == {
        "username": "alice",
        "role": "user",
        "password_change_required": False,
    }

    # 重置后旧 refresh 失效（token_version+1）
    assert refresh(client, old["refresh_token"]).status_code == 400
    # 旧密码不能再登录，新密码可以直接使用。
    assert login(client, "alice", STRONG_PASSWORD).status_code == 400
    body = login(client, "alice", NEW_STRONG_PASSWORD).get_json()
    claims = decode_with_jwks(client, body["access_token"])
    assert claims["pwd_cr"] is False
    assert claims["ver"] == 2


def test_change_password_clears_pwd_cr_and_revokes(client, tmp_path):
    assert create_user(client, "bob").status_code == 201
    old = login(client, "bob", STRONG_PASSWORD).get_json()

    resp = client.put(
        "/admin/change-password",
        json={
            "username": "bob",
            "old_password": STRONG_PASSWORD,
            "new_password": NEW_STRONG_PASSWORD,
        },
        headers=admin_headers(),
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"success": True}

    # 本人改密后旧 refresh 失效，新 token 不再带强制改密
    assert refresh(client, old["refresh_token"]).status_code == 400
    body = login(client, "bob", NEW_STRONG_PASSWORD).get_json()
    assert decode_with_jwks(client, body["access_token"])["pwd_cr"] is False

    data = json.loads((tmp_path / "data" / "users.json").read_text(encoding="utf-8"))
    record = next(r for r in data["users"] if r["username"] == "bob")
    assert record["must_change_password"] is False
    assert record["password_changed_at"]  # UTC ISO 时间戳已写入
    assert record["password_version"] == 2
    assert record["token_version"] == 2


def test_change_password_error_messages(client):
    resp = client.put(
        "/admin/change-password",
        json={"username": "admin", "old_password": "", "new_password": INVALID_PASSWORD},
        headers=admin_headers(),
    )
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "原密码和新密码不能为空"}

    resp = client.put(
        "/admin/change-password",
        json={"username": "admin", "old_password": NEW_STRONG_PASSWORD, "new_password": STRONG_PASSWORD},
        headers=admin_headers(),
    )
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "原密码错误"}

    resp = client.put(
        "/admin/change-password",
        json={
            "username": "admin",
            "old_password": BOOTSTRAP_ADMIN_PASSWORD,
            "new_password": BOOTSTRAP_ADMIN_PASSWORD,
        },
        headers=admin_headers(),
    )
    assert resp.status_code == 400
    violations = resp.get_json()["violations"]
    assert "same_as_old" in violations


def test_bootstrap_requires_explicit_strong_password(tmp_path):
    with pytest.raises(RuntimeError, match="AUTH_BOOTSTRAP_ADMIN_PASSWORD"):
        create_app(make_config(tmp_path, bootstrap_admin_password=None))

    with pytest.raises(ValueError, match="密码复杂度"):
        create_app(make_config(tmp_path, bootstrap_admin_password=INVALID_PASSWORD))


# ------------------------------------------------------------------ 密码策略


def test_password_policy_violation_codes_exact(client):
    # 多重违规：首尾空白 + 过短 + 含用户名 + 字符类不足
    resp = create_user(client, "alice", password=username_password_violation("alice"))
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"] == "密码复杂度不满足要求"
    assert body["code"] == "PASSWORD_POLICY_FAILED"
    assert body["violations"] == [
        "surrounding_whitespace",
        "min_length",
        "contains_username",
        "character_classes",
    ]

    # 常见弱口令
    resp = create_user(client, "alice", password=COMMON_PASSWORD)
    assert resp.get_json()["violations"] == ["common_password"]

    # 超长
    resp = create_user(client, "alice", password="A1!" + "a" * 126)
    assert resp.get_json()["violations"] == ["max_length"]


def test_create_user_value_errors_chinese(client):
    resp = client.post("/admin/users", json={"username": "", "password": ""}, headers=admin_headers())
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "用户名和密码不能为空"}

    resp = create_user(client, "admin")
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "用户 admin 已存在"}

    resp = create_user(client, "carol", role="superuser")
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "角色必须是 admin、user 或 ops"}


def test_ops_role_is_issued_in_claims_and_can_be_assigned(client):
    """ops 角色（onto-modeling 运维管理面）：可创建、可改，access token 的 role claim 照实签发。"""
    resp = create_user(client, "operator_ops", role="ops")
    assert resp.status_code == 201 and resp.get_json()["user"]["role"] == "ops"
    claims = decode_with_jwks(client, login(client, "operator_ops", STRONG_PASSWORD).get_json()["access_token"])
    assert claims["role"] == "ops"
    assert client.get("/userinfo", headers={"Authorization": "Bearer " + login(
        client, "operator_ops", STRONG_PASSWORD).get_json()["access_token"]}).get_json()["role"] == "ops"

    assert create_user(client, "operator_user").status_code == 201
    changed = client.put("/admin/users/operator_user", json={"role": "ops"}, headers=admin_headers())
    assert changed.status_code == 200 and changed.get_json()["user"]["role"] == "ops"
    back = client.put("/admin/users/operator_user", json={"role": "user"}, headers=admin_headers())
    assert back.get_json()["user"]["role"] == "user"
    rejected = client.put("/admin/users/operator_user", json={"role": "root"}, headers=admin_headers())
    assert rejected.status_code == 400


# -------------------------------------------------------------------- JWKS


def test_jwks_roundtrip_verifies_signature(client):
    body = login(client).get_json()
    jwks = client.get("/jwks.json").get_json()
    assert len(jwks["keys"]) >= 1
    key = jwks["keys"][0]
    assert key["kty"] == "RSA"
    assert key["alg"] == "RS256"
    assert key["use"] == "sig"
    assert len(key["kid"]) == 12

    claims = decode_with_jwks(client, body["access_token"])
    assert claims["sub"] == "admin"

    # 篡改的 token 验签必须失败
    tampered = body["access_token"][:-4] + "AAAA"
    key_set = JsonWebKey.import_key_set(jwks)
    with pytest.raises(Exception):
        claims = JsonWebToken(["RS256"]).decode(tampered, key_set)
        claims.validate()


def test_openid_configuration(client):
    body = client.get("/.well-known/openid-configuration").get_json()
    assert body == {
        "issuer": ISSUER,
        "token_endpoint": ISSUER + "/token",
        "revocation_endpoint": ISSUER + "/revoke",
        "jwks_uri": ISSUER + "/jwks.json",
        "userinfo_endpoint": ISSUER + "/userinfo",
        "grant_types_supported": ["password", "refresh_token"],
    }


# ------------------------------------------------------------------ userinfo


def test_userinfo(client):
    body = login(client).get_json()
    resp = client.get("/userinfo", headers={"Authorization": "Bearer " + body["access_token"]})
    assert resp.status_code == 200
    assert resp.get_json() == {
        "sub": "admin",
        "role": "admin",
        "password_change_required": False,
    }

    # 缺头 / 垃圾 token / 用 refresh token 冒充 access → 401
    assert client.get("/userinfo").status_code == 401
    assert client.get("/userinfo", headers={"Authorization": f"Bearer {INVALID_TOKEN}"}).status_code == 401
    resp = client.get("/userinfo", headers={"Authorization": "Bearer " + body["refresh_token"]})
    assert resp.status_code == 401

    # revoke 后 ver 不匹配 → 401
    client.post("/revoke", data={"token": body["refresh_token"]})
    resp = client.get("/userinfo", headers={"Authorization": "Bearer " + body["access_token"]})
    assert resp.status_code == 401


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_capabilities_reflect_registration_setting(client, registration_disabled_client):
    assert client.get("/capabilities").get_json() == {"registration_enabled": True}
    assert registration_disabled_client.get("/capabilities").get_json() == {"registration_enabled": False}


# ------------------------------------------------------------------ Admin 鉴权


def test_admin_api_rejects_missing_or_wrong_token(client):
    assert client.get("/admin/users").status_code == 401
    assert client.get("/admin/users", headers={"X-Admin-Token": "nope"}).status_code == 401

    resp = client.get("/admin/users", headers=admin_headers())
    assert resp.status_code == 200
    assert resp.get_json() == {"users": [{"username": "admin", "role": "admin", "password_change_required": False}]}


def test_admin_api_disabled_without_env(tmp_path):
    client = create_app(make_config(tmp_path, admin_token=None)).test_client()
    for method, path in [
        ("get", "/admin/users"),
        ("post", "/admin/users"),
        ("put", "/admin/users/alice"),
        ("delete", "/admin/users/alice"),
        ("put", "/admin/change-password"),
    ]:
        resp = getattr(client, method)(path, headers=admin_headers())
        assert resp.status_code == 503
        assert resp.get_json() == {"error": "Admin API disabled"}


def test_delete_user(client):
    assert create_user(client, "dave").status_code == 201
    resp = client.delete("/admin/users/dave", headers=admin_headers())
    assert resp.get_json() == {"success": True}

    resp = client.delete("/admin/users/dave", headers=admin_headers())
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "用户 dave 不存在"}

    # 删除后无法登录
    assert login(client, "dave", STRONG_PASSWORD).status_code == 400


# ------------------------------------------------------------------ TTL 钳制


def test_access_ttl_clamped_to_hard_cap(tmp_path):
    cfg = make_config(tmp_path, access_ttl_seconds=7200)
    assert cfg.access_ttl_seconds == 3600

    client = create_app(cfg).test_client()
    body = login(client).get_json()
    assert body["expires_in"] == 3600
    claims = decode_with_jwks(client, body["access_token"])
    assert claims["exp"] - claims["iat"] == 3600


def test_access_ttl_clamped_from_env(tmp_path):
    cfg = AuthServerConfig.from_env(
        {
            "AUTH_SERVER_DATA_DIR": str(tmp_path / "data"),
            "AUTH_ACCESS_TTL_SECONDS": "43200",  # 沿用旧 12h 即安全回归，必须被钳制
        }
    )
    assert cfg.access_ttl_seconds == 3600
    assert cfg.refresh_ttl_seconds == 259_200
    assert cfg.admin_token is None


def test_user_max_count_defaults_to_unlimited():
    cfg = AuthServerConfig.from_env({})
    assert cfg.user_max_count == 0


def test_legacy_user_max_count_is_ignored_for_admin_created_users(tmp_path):
    cfg = AuthServerConfig.from_env(
        {
            "AUTH_SERVER_DATA_DIR": str(tmp_path / "data"),
            "AUTH_SERVER_ADMIN_TOKEN": ADMIN_TOKEN,
            "AUTH_BOOTSTRAP_ADMIN_PASSWORD": BOOTSTRAP_ADMIN_PASSWORD,
            "AUTH_USER_MAX_COUNT": "1",
        }
    )
    client = create_app(cfg).test_client()

    assert create_user(client, "unlimited-admin-created").status_code == 201


def test_legacy_user_max_count_is_ignored_for_registered_users(tmp_path):
    client = create_app(make_config(tmp_path, user_max_count=1)).test_client()

    response = register(client, "unlimited-registered", "unlimited@example.com")
    assert response.status_code == 200


# ------------------------------------------------------------------ 兼容性


def test_legacy_users_json_without_token_version(tmp_path):
    """带 pbkdf2 哈希、无 token_version 的旧格式 users.json 直接可登录。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    legacy = {
        "users": [
            {
                # 低迭代旧哈希：登录成功后应自动 rehash 到 600000
                "username": "eve",
                "password_hash": UserStore.hash_password(LEGACY_PASSWORD, iterations=100_000),
                "role": "user",
                "must_change_password": False,
                "password_changed_at": "2025-01-01T00:00:00+00:00",
                "password_version": 3,
            },
            {
                # 遗留明文 password 字段：登录时迁移为哈希。
                "username": "frank",
                "password": NEW_STRONG_PASSWORD,
                "role": "admin",
            },
        ]
    }
    (data_dir / "users.json").write_text(json.dumps(legacy, ensure_ascii=False, indent=2), encoding="utf-8")

    client = create_app(make_config(tmp_path)).test_client()

    resp = login(client, "eve", LEGACY_PASSWORD)
    assert resp.status_code == 200
    body = resp.get_json()
    claims = decode_with_jwks(client, body["access_token"])
    assert claims["ver"] == 1  # 缺省视为 1
    assert claims["pwd_cr"] is False
    assert refresh(client, body["refresh_token"]).status_code == 200

    resp = login(client, "frank", NEW_STRONG_PASSWORD)
    assert resp.status_code == 200
    assert decode_with_jwks(client, resp.get_json()["access_token"])["pwd_cr"] is False

    saved = json.loads((data_dir / "users.json").read_text(encoding="utf-8"))
    eve = next(r for r in saved["users"] if r["username"] == "eve")
    assert eve["password_hash"].startswith("pbkdf2_sha256$600000$")  # 自动 rehash
    assert eve["password_version"] == 3  # 已有版本号保留
    assert eve["token_version"] == 1
    frank = next(r for r in saved["users"] if r["username"] == "frank")
    assert "password" not in frank
    assert frank["password_hash"].startswith("pbkdf2_sha256$600000$")
    assert frank["must_change_password"] is False
    assert frank["password_changed_at"]


# ------------------------------------------------------------------ 开放注册


@pytest.fixture()
def reg_client(tmp_path):
    return create_app(make_config(tmp_path)).test_client()


def register(client, username, email, password=STRONG_PASSWORD, ip=None):
    kwargs = {"json": {"username": username, "email": email, "password": password}}
    if ip is not None:
        kwargs["environ_base"] = {"REMOTE_ADDR": ip}
    return client.post("/register", **kwargs)


def test_register_creates_user_and_login_full_flow(reg_client, tmp_path):
    resp = register(reg_client, "walter", "walter@example.com")
    assert resp.status_code == 200
    assert resp.get_json() == {
        "success": True,
        "user": {
            "username": "walter",
            "role": "user",
            "password_change_required": False,
        },
    }

    # 一步建号：users.json 落盘（email 归一化、不强制改密），不再写 pending 文件
    data = json.loads((tmp_path / "data" / "users.json").read_text(encoding="utf-8"))
    record = next(r for r in data["users"] if r["username"] == "walter")
    assert record["email"] == "walter@example.com"
    assert record["must_change_password"] is False
    assert record["password_hash"].startswith("pbkdf2_sha256$600000$")
    assert not (tmp_path / "data" / "pending_registrations.json").exists()

    # /token 登录闭环：默认 role=user、不强制改密
    body = login(reg_client, "walter", STRONG_PASSWORD).get_json()
    claims = decode_with_jwks(reg_client, body["access_token"])
    assert claims["role"] == "user"
    assert claims["pwd_cr"] is False
    resp = reg_client.get("/userinfo", headers={"Authorization": "Bearer " + body["access_token"]})
    assert resp.get_json() == {
        "sub": "walter",
        "role": "user",
        "password_change_required": False,
    }


def test_registration_disabled_503(registration_disabled_client):
    resp = registration_disabled_client.post(
        "/register",
        json={"username": "x", "email": "x@example.com", "password": STRONG_PASSWORD},
    )
    assert resp.status_code == 503
    assert resp.get_json() == {"error": "注册功能未开启"}


def test_register_format_validation(reg_client):
    resp = register(reg_client, "ab", "ab@example.com")  # 太短
    assert resp.status_code == 400
    assert "用户名格式不合法" in resp.get_json()["error"]

    resp = register(reg_client, "bad name", "bad@example.com")  # 含空格
    assert resp.status_code == 400
    assert "用户名格式不合法" in resp.get_json()["error"]

    resp = register(reg_client, "gooduser", "not-an-email")
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "邮箱格式不合法"}


def test_register_duplicate_username_and_email(reg_client):
    assert register(reg_client, "gina", "gina@example.com").status_code == 200

    resp = register(reg_client, "gina", "other@example.com")  # 用户名占用
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "用户 gina 已存在"}

    resp = register(reg_client, "gina2", "GINA@Example.COM")  # 邮箱占用（大小写不敏感）
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "邮箱已被注册"}

    resp = register(reg_client, "admin", "admin2@example.com")  # bootstrap 用户名占用
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "用户 admin 已存在"}


def test_register_password_policy_violations_passthrough(reg_client):
    resp = register(reg_client, "gina", "gina@example.com", password=username_password_violation("gina"))
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"] == "密码复杂度不满足要求"
    assert body["code"] == "PASSWORD_POLICY_FAILED"
    assert body["violations"] == [
        "surrounding_whitespace",
        "min_length",
        "contains_username",
        "character_classes",
    ]


def test_register_rate_limited_by_ip(reg_client):
    for i in range(5):
        resp = register(reg_client, f"user{i}x", f"user{i}@example.com")
        assert resp.status_code == 200
    resp = register(reg_client, "user5x", "user5@example.com")
    assert resp.status_code == 429
    assert resp.get_json() == {"error": "请求过于频繁，请稍后再试"}


def test_register_rate_limited_by_email_across_ips(reg_client):
    email = "flood@example.com"
    assert register(reg_client, "flood0", email, ip="192.0.2.10").status_code == 200
    for i in range(1, 5):
        # 重复邮箱 → 400，但每次请求都计入该 email 的窗口额度
        assert register(reg_client, f"flood{i}", email, ip=f"192.0.2.{10 + i}").status_code == 400
    resp = register(reg_client, "flood5", email, ip="192.0.2.99")
    assert resp.status_code == 429


def test_registered_email_persisted_and_admin_list_visible(reg_client, tmp_path):
    assert register(reg_client, "hank1", "Hank@Example.com").status_code == 200

    data = json.loads((tmp_path / "data" / "users.json").read_text(encoding="utf-8"))
    record = next(r for r in data["users"] if r["username"] == "hank1")
    assert record["email"] == "hank@example.com"  # 已归一化小写
    assert record["must_change_password"] is False
    assert record["password_changed_at"]

    users = reg_client.get("/admin/users", headers=admin_headers()).get_json()["users"]
    hank = next(u for u in users if u["username"] == "hank1")
    assert hank["email"] == "hank@example.com"
    # 存量无 email 用户形状不变
    admin_entry = next(u for u in users if u["username"] == "admin")
    assert admin_entry == {
        "username": "admin",
        "role": "admin",
        "password_change_required": False,
    }


def test_registration_config_from_env():
    default_cfg = AuthServerConfig.from_env({})
    assert default_cfg.enable_registration is True

    disabled_cfg = AuthServerConfig.from_env({"AUTH_ENABLE_REGISTRATION": "false"})
    assert disabled_cfg.enable_registration is False
