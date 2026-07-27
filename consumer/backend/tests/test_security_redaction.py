from __future__ import annotations

import json

from utils.security.redaction import (
    REDACTED,
    is_sensitive_key,
    redact_sensitive_text,
    redact_sensitive_value,
)


def test_sensitive_key_recognizes_common_prefixed_credentials():
    for key in (
        "access_token",
        "refresh-token",
        "client_secret",
        "db_password",
        "private_key",
        "x-api-key",
        "proxy_authorization",
    ):
        assert is_sensitive_key(key) is True

    assert is_sensitive_key("token_count") is False
    assert is_sensitive_key("public_key_id") is False


def test_redact_sensitive_text_handles_assignments_json_and_private_keys():
    private_key = (
        "-----BEGIN PRIVATE KEY-----\n"
        "prod-private-key\n"
        "-----END PRIVATE KEY-----"
    )
    samples = (
        "access_token=prod-token",
        "client_secret=prod-secret",
        "db_password=prod-pass",
        '{"password":"prod-json-pass"}',
        "private_key=prod-key",
        "Authorization: Bearer prod-bearer",
        private_key,
    )

    for sample in samples:
        redacted = redact_sensitive_text(sample)
        assert "prod-" not in redacted
        assert REDACTED in redacted


def test_redact_sensitive_value_recurses_through_json_like_data():
    value = {
        "access_token": "prod-token",
        "nested": {
            "client_secret": "prod-secret",
            "message": "db_password=prod-pass",
        },
        "warnings": ['{"password":"prod-json-pass"}'],
    }

    redacted = redact_sensitive_value(value)
    serialized = json.dumps(redacted, ensure_ascii=False)

    assert "prod-" not in serialized
    assert redacted["access_token"] == REDACTED
    assert redacted["nested"]["client_secret"] == REDACTED
