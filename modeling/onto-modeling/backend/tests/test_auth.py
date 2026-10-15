"""访问令牌校验：签发方与本服务时钟有偏差时，iat/exp 在容差内仍应通过。"""

import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.services import auth

ISSUER = "http://auth.example.com"
AUDIENCE = "meta-wise"


@pytest.fixture()
def signing(monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(auth, "ENABLED", True)
    monkeypatch.setattr(auth, "ISSUER", ISSUER)
    monkeypatch.setattr(auth, "AUDIENCE", AUDIENCE)
    monkeypatch.setattr(
        auth,
        "_jwks_client",
        SimpleNamespace(
            get_signing_key_from_jwt=lambda _token: SimpleNamespace(key=private_key.public_key())
        ),
    )

    def issue(iat_offset: float = 0, exp_offset: float = 900, typ: str = "access") -> str:
        now = time.time()
        claims = {
            "sub": "alice",
            "typ": typ,
            "iss": ISSUER,
            "aud": AUDIENCE,
            "iat": int(now + iat_offset),
            "exp": int(now + exp_offset),
        }
        return jwt.encode(claims, private_key, algorithm="RS256")

    return issue


def test_token_issued_by_slightly_fast_clock_is_accepted(signing):
    """签发方时钟快 30 秒（或本机校时回拨）：iat 落在"未来"，容差内照常通过。"""
    claims = auth.verify_access_token(signing(iat_offset=30))
    assert claims is not None and claims["sub"] == "alice"


def test_token_far_in_future_is_rejected(signing):
    assert auth.verify_access_token(signing(iat_offset=auth.CLOCK_SKEW_LEEWAY_SECONDS + 60)) is None


def test_expired_token_beyond_leeway_is_rejected(signing):
    assert auth.verify_access_token(signing(iat_offset=-1000, exp_offset=-120)) is None


def test_refresh_token_is_rejected(signing):
    assert auth.verify_access_token(signing(typ="refresh")) is None
