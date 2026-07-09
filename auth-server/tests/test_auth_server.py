"""auth server v1 端到端测试（Flask test client，不起真服务）。"""

from __future__ import annotations

import json
import re
import time

import pytest
from authlib.jose import JsonWebKey, JsonWebToken

from auth_server import create_app
from auth_server.config import AuthServerConfig
from auth_server.users import UserStore

ADMIN_TOKEN = "test-admin-token"
ISSUER = "http://auth-server-test:5056"
AUDIENCE = "meta-wise"
STRONG_PASSWORD = "Str0ng!Pass"
NEW_STRONG_PASSWORD = "N3w!Str0ngPass"


def make_config(tmp_path, **overrides):
    params = dict(
        data_dir=tmp_path / "data",
        issuer=ISSUER,
        audience=AUDIENCE,
        access_ttl_seconds=900,
        refresh_ttl_seconds=259_200,
        admin_token=ADMIN_TOKEN,
        host="127.0.0.1",
        port=5056,
        user_max_count=10,
    )
    params.update(overrides)
    return AuthServerConfig(**params)


@pytest.fixture()
def client(tmp_path):
    return create_app(make_config(tmp_path)).test_client()


def login(client, username="admin", password="admin123"):
    return client.post(
        "/token",
        data={"grant_type": "password", "username": username, "password": password},
    )


def refresh(client, refresh_token):
    return client.post(
        "/token", json={"grant_type": "refresh_token", "refresh_token": refresh_token}
    )


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
    assert claims["pwd_cr"] is True  # bootstrap admin 强制改密
    assert claims["exp"] - claims["iat"] == 900

    users_file = tmp_path / "data" / "users.json"
    data = json.loads(users_file.read_text(encoding="utf-8"))
    (record,) = data["users"]
    assert record["username"] == "admin"
    assert record["must_change_password"] is True
    assert record["password_hash"].startswith("pbkdf2_sha256$600000$")


def test_wrong_password_invalid_grant(client):
    resp = login(client, password="wrong-password")
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
        json={"grant_type": "password", "username": "admin", "password": "<REDACTED_WEAK_PASSWORD>"},
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
    assert refresh(client, "not-a-jwt").status_code == 400
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
    assert client.post("/revoke", data={"token": "garbage"}).status_code == 200
    assert client.post("/revoke", json={"token": ""}).status_code == 200
    # access token 不是合法 refresh token，同样 200 且不吊销
    body = login(client).get_json()
    assert client.post("/revoke", data={"token": body["access_token"]}).status_code == 200
    assert refresh(client, body["refresh_token"]).status_code == 200


def test_admin_reset_revokes_refresh_and_sets_pwd_cr(client):
    assert create_user(client, "alice").status_code == 201
    old = login(client, "alice", STRONG_PASSWORD).get_json()

    resp = client.put(
        "/admin/users/alice", json={"password": NEW_STRONG_PASSWORD}, headers=admin_headers()
    )
    assert resp.status_code == 200
    assert resp.get_json()["user"] == {
        "username": "alice",
        "role": "user",
        "password_change_required": True,
    }

    # 重置后旧 refresh 失效（token_version+1）
    assert refresh(client, old["refresh_token"]).status_code == 400
    # 旧密码不能再登录，新密码可以，且 access token 带 pwd_cr=true
    assert login(client, "alice", STRONG_PASSWORD).status_code == 400
    body = login(client, "alice", NEW_STRONG_PASSWORD).get_json()
    claims = decode_with_jwks(client, body["access_token"])
    assert claims["pwd_cr"] is True
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
        json={"username": "admin", "old_password": "", "new_password": "x"},
        headers=admin_headers(),
    )
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "原密码和新密码不能为空"}

    resp = client.put(
        "/admin/change-password",
        json={"username": "admin", "old_password": "wrong", "new_password": STRONG_PASSWORD},
        headers=admin_headers(),
    )
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "原密码错误"}

    resp = client.put(
        "/admin/change-password",
        json={"username": "admin", "old_password": "admin123", "new_password": "admin123"},
        headers=admin_headers(),
    )
    assert resp.status_code == 400
    violations = resp.get_json()["violations"]
    assert "same_as_old" in violations


# ------------------------------------------------------------------ 密码策略


def test_password_policy_violation_codes_exact(client):
    # 多重违规：首尾空白 + 过短 + 含用户名 + 字符类不足
    resp = create_user(client, "alice", password=" alice ")
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
    resp = create_user(client, "alice", password="Password123")
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
    assert resp.get_json() == {"error": "角色必须是 admin 或 user"}


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
    resp = client.get(
        "/userinfo", headers={"Authorization": "Bearer " + body["access_token"]}
    )
    assert resp.status_code == 200
    assert resp.get_json() == {
        "sub": "admin",
        "role": "admin",
        "password_change_required": True,
    }

    # 缺头 / 垃圾 token / 用 refresh token 冒充 access → 401
    assert client.get("/userinfo").status_code == 401
    assert client.get("/userinfo", headers={"Authorization": "Bearer junk"}).status_code == 401
    resp = client.get(
        "/userinfo", headers={"Authorization": "Bearer " + body["refresh_token"]}
    )
    assert resp.status_code == 401

    # revoke 后 ver 不匹配 → 401
    client.post("/revoke", data={"token": body["refresh_token"]})
    resp = client.get(
        "/userinfo", headers={"Authorization": "Bearer " + body["access_token"]}
    )
    assert resp.status_code == 401


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


# ------------------------------------------------------------------ Admin 鉴权


def test_admin_api_rejects_missing_or_wrong_token(client):
    assert client.get("/admin/users").status_code == 401
    assert client.get("/admin/users", headers={"X-Admin-Token": "nope"}).status_code == 401

    resp = client.get("/admin/users", headers=admin_headers())
    assert resp.status_code == 200
    assert resp.get_json() == {
        "users": [{"username": "admin", "role": "admin", "password_change_required": True}]
    }


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
                "password_hash": UserStore.hash_password("Ev3!Legacy#Pass", iterations=100_000),
                "role": "user",
                "must_change_password": False,
                "password_changed_at": "2025-01-01T00:00:00+00:00",
                "password_version": 3,
            },
            {
                # 遗留明文 password 字段：首登迁移为哈希并强制改密
                "username": "frank",
                "password": "Fr4nk!Plain#Pass",
                "role": "admin",
            },
        ]
    }
    (data_dir / "users.json").write_text(
        json.dumps(legacy, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    client = create_app(make_config(tmp_path)).test_client()

    resp = login(client, "eve", "Ev3!Legacy#Pass")
    assert resp.status_code == 200
    body = resp.get_json()
    claims = decode_with_jwks(client, body["access_token"])
    assert claims["ver"] == 1  # 缺省视为 1
    assert claims["pwd_cr"] is False
    assert refresh(client, body["refresh_token"]).status_code == 200

    resp = login(client, "frank", "Fr4nk!Plain#Pass")
    assert resp.status_code == 200
    assert decode_with_jwks(client, resp.get_json()["access_token"])["pwd_cr"] is True

    saved = json.loads((data_dir / "users.json").read_text(encoding="utf-8"))
    eve = next(r for r in saved["users"] if r["username"] == "eve")
    assert eve["password_hash"].startswith("pbkdf2_sha256$600000$")  # 自动 rehash
    assert eve["password_version"] == 3  # 已有版本号保留
    assert eve["token_version"] == 1
    frank = next(r for r in saved["users"] if r["username"] == "frank")
    assert "password" not in frank
    assert frank["password_hash"].startswith("pbkdf2_sha256$600000$")
    assert frank["must_change_password"] is True


# ------------------------------------------------------------------ 开放注册


@pytest.fixture()
def reg_client(tmp_path):
    return create_app(make_config(tmp_path, enable_registration=True)).test_client()


def last_mail(client):
    return client.application.config["MAILER"].sent[-1]


def last_code(client):
    return re.search(r"\d{6}", last_mail(client)["body"]).group(0)


def register(client, username, email, password=STRONG_PASSWORD, ip=None):
    kwargs = {"json": {"username": username, "email": email, "password": password}}
    if ip is not None:
        kwargs["environ_base"] = {"REMOTE_ADDR": ip}
    return client.post("/register", **kwargs)


def verify(client, email, code):
    return client.post("/register/verify", json={"email": email, "code": code})


def pending_file(tmp_path):
    return tmp_path / "data" / "pending_registrations.json"


def edit_pending(tmp_path, email, **updates):
    path = pending_file(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    for entry in data["pending"]:
        if entry["email"] == email:
            entry.update(updates)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def test_register_verify_login_full_flow(reg_client, tmp_path):
    resp = register(reg_client, "walter", "walter@example.com")
    assert resp.status_code == 200
    assert resp.get_json() == {"message": "验证码已发送"}
    assert last_mail(reg_client)["to"] == "walter@example.com"

    # pending 落盘且验证码只存哈希
    pending = json.loads(pending_file(tmp_path).read_text(encoding="utf-8"))["pending"]
    (entry,) = pending
    assert entry["username"] == "walter"
    assert entry["attempts"] == 0
    assert last_code(reg_client) not in entry["code_hash"]
    assert entry["password_hash"].startswith("pbkdf2_sha256$600000$")

    resp = verify(reg_client, "walter@example.com", last_code(reg_client))
    assert resp.status_code == 200
    assert resp.get_json() == {
        "success": True,
        "user": {
            "username": "walter",
            "role": "user",
            "password_change_required": False,
        },
    }
    # pending 已清
    assert json.loads(pending_file(tmp_path).read_text(encoding="utf-8"))["pending"] == []

    # /token 登录闭环：默认 role=user、不强制改密
    body = login(reg_client, "walter", STRONG_PASSWORD).get_json()
    claims = decode_with_jwks(reg_client, body["access_token"])
    assert claims["role"] == "user"
    assert claims["pwd_cr"] is False
    resp = reg_client.get(
        "/userinfo", headers={"Authorization": "Bearer " + body["access_token"]}
    )
    assert resp.get_json() == {
        "sub": "walter",
        "role": "user",
        "password_change_required": False,
    }


def test_registration_disabled_503(client):
    for path, payload in [
        ("/register", {"username": "x", "email": "x@example.com", "password": STRONG_PASSWORD}),
        ("/register/verify", {"email": "x@example.com", "code": "000000"}),
        ("/register/resend", {"email": "x@example.com"}),
    ]:
        resp = client.post(path, json=payload)
        assert resp.status_code == 503
        assert resp.get_json() == {"error": "注册功能未开启"}


def test_registration_smtp_unconfigured_503(tmp_path):
    client = create_app(
        make_config(tmp_path, enable_registration=True, mail_mode="smtp")
    ).test_client()
    assert client.application.config["MAILER"] is None
    resp = register(client, "walter", "walter@example.com")
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

    resp = register(reg_client, "gina", "other@example.com")  # pending 用户名占用
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "用户 gina 已存在"}

    resp = register(reg_client, "gina2", "gina@example.com")  # pending 邮箱占用
    assert resp.status_code == 400
    assert "该邮箱已提交注册" in resp.get_json()["error"]

    assert verify(reg_client, "gina@example.com", last_code(reg_client)).status_code == 200

    resp = register(reg_client, "gina3", "GINA@Example.COM")  # 正式用户邮箱占用（大小写不敏感）
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "邮箱已被注册"}

    resp = register(reg_client, "admin", "admin2@example.com")  # 正式用户名占用
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "用户 admin 已存在"}


def test_register_password_policy_violations_passthrough(reg_client):
    resp = register(reg_client, "gina", "gina@example.com", password=" gina ")
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


def test_verify_wrong_code_locks_after_five_attempts(reg_client):
    assert register(reg_client, "carol", "carol@example.com").status_code == 200
    code = last_code(reg_client)
    wrong = "000000" if code != "000000" else "111111"

    for _ in range(5):
        resp = verify(reg_client, "carol@example.com", wrong)
        assert resp.status_code == 400
        assert resp.get_json() == {"error": "验证码错误"}

    # 第 6 次即使验证码正确也已锁死
    resp = verify(reg_client, "carol@example.com", code)
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "验证码错误次数过多，请重新注册"}


def test_verify_expired_code(reg_client, tmp_path):
    assert register(reg_client, "dave1", "dave@example.com").status_code == 200
    code = last_code(reg_client)
    edit_pending(tmp_path, "dave@example.com", expires_at=int(time.time()) - 1)

    resp = verify(reg_client, "dave@example.com", code)
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "验证码无效或已过期"}
    # 读时清理过期项
    assert json.loads(pending_file(tmp_path).read_text(encoding="utf-8"))["pending"] == []


def test_verify_unknown_email(reg_client):
    resp = verify(reg_client, "nobody@example.com", "123456")
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "验证码无效或已过期"}

    resp = reg_client.post("/register/resend", json={"email": "nobody@example.com"})
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "未找到待验证的注册，请重新注册"}


def test_resend_cooldown_and_code_rotation(reg_client, tmp_path):
    assert register(reg_client, "erin1", "erin@example.com").status_code == 200

    # 60 秒冷却内重发 → 429
    resp = reg_client.post("/register/resend", json={"email": "erin@example.com"})
    assert resp.status_code == 429
    assert resp.get_json() == {"error": "请求过于频繁，请稍后再试"}

    edit_pending(tmp_path, "erin@example.com", last_sent_at=int(time.time()) - 61)
    resp = reg_client.post("/register/resend", json={"email": "erin@example.com"})
    assert resp.status_code == 200
    assert resp.get_json() == {"message": "验证码已发送"}
    assert len(reg_client.application.config["MAILER"].sent) == 2

    # 新验证码可完成注册
    assert verify(reg_client, "erin@example.com", last_code(reg_client)).status_code == 200


def test_register_rate_limited_by_ip(reg_client):
    for i in range(5):
        resp = register(reg_client, f"user{i}x", f"user{i}@example.com")
        assert resp.status_code == 200
    resp = register(reg_client, "user5x", "user5@example.com")
    assert resp.status_code == 429
    assert resp.get_json() == {"error": "请求过于频繁，请稍后再试"}


def test_register_rate_limited_by_email_across_ips(reg_client):
    email = "flood@example.com"
    assert register(reg_client, "flood0", email, ip="10.0.0.10").status_code == 200
    for i in range(1, 5):
        # 重复邮箱 → 400，但每次请求都计入该 email 的窗口额度
        assert register(reg_client, f"flood{i}", email, ip=f"10.0.0.{10 + i}").status_code == 400
    resp = register(reg_client, "flood5", email, ip="10.0.0.99")
    assert resp.status_code == 429


def test_registered_email_persisted_and_admin_list_visible(reg_client, tmp_path):
    assert register(reg_client, "hank1", "Hank@Example.com").status_code == 200
    assert verify(reg_client, "hank@example.com", last_code(reg_client)).status_code == 200

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
        "password_change_required": True,
    }


def test_registration_config_from_env():
    cfg = AuthServerConfig.from_env(
        {
            "AUTH_ENABLE_REGISTRATION": "true",
            "AUTH_MAIL_MODE": "SMTP",
            "AUTH_SMTP_HOST": "smtp.example.com",
            "AUTH_SMTP_PORT": "465",
            "AUTH_SMTP_USERNAME": "mailer",
            "AUTH_SMTP_PASSWORD": "secret",
            "AUTH_SMTP_USE_TLS": "false",
            "AUTH_MAIL_FROM": "auth@example.com",
        }
    )
    assert cfg.enable_registration is True
    assert cfg.mail_mode == "smtp"
    assert cfg.smtp_host == "smtp.example.com"
    assert cfg.smtp_port == 465
    assert cfg.smtp_use_tls is False
    assert cfg.mail_from == "auth@example.com"

    default_cfg = AuthServerConfig.from_env({})
    assert default_cfg.enable_registration is False
    assert default_cfg.mail_mode == "console"
