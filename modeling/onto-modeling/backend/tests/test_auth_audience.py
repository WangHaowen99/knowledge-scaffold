"""access token 的 audience 校验：产品新旧名互认，自定义取值严格匹配。"""

import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.services import auth

ISSUER = "https://auth-server.example"


@pytest.fixture(scope="module")
def private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def verifier(monkeypatch, private_key):
    """模拟已启用鉴权、JWKS 返回测试公钥；返回按给定配置 audience 验签的函数。"""
    jwks = SimpleNamespace(
        get_signing_key_from_jwt=lambda _token: SimpleNamespace(key=private_key.public_key())
    )
    monkeypatch.setattr(auth, "ENABLED", True)
    monkeypatch.setattr(auth, "ISSUER", ISSUER)
    monkeypatch.setattr(auth, "_jwks_client", jwks)

    def verify(token: str, configured: str):
        monkeypatch.setattr(auth, "ACCEPTED_AUDIENCES", auth.accepted_audiences(configured))
        return auth.verify_access_token(token)

    return verify


def _token(private_key, audience: str) -> str:
    now = int(time.time())
    claims = {
        "sub": "alice",
        "typ": "access",
        "iss": ISSUER,
        "aud": audience,
        "iat": now,
        "exp": now + 600,
    }
    return jwt.encode(claims, private_key, algorithm="RS256")


def test_legacy_configured_audience_accepts_renamed_product_token(verifier, private_key):
    claims = verifier(_token(private_key, "knowledge-scaffold"), "meta-wise")
    assert claims["sub"] == "alice"


def test_renamed_configured_audience_accepts_legacy_token(verifier, private_key):
    claims = verifier(_token(private_key, "meta-wise"), "knowledge-scaffold")
    assert claims["sub"] == "alice"


def test_custom_audience_stays_strict(verifier, private_key):
    assert verifier(_token(private_key, "knowledge-scaffold"), "custom-app") is None
    assert verifier(_token(private_key, "custom-app"), "custom-app")["sub"] == "alice"


def test_unrelated_audience_rejected(verifier, private_key):
    assert verifier(_token(private_key, "other-app"), "meta-wise") is None
