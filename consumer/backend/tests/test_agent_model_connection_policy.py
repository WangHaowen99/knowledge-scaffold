from __future__ import annotations

import pytest
from agent.core.model_connection_policy import (
    ModelConnectionPolicyError,
    resolve_model_connection_policy,
)


def test_default_policy_raises_short_timeout_to_180_seconds(monkeypatch):
    monkeypatch.delenv("AGENT_LLM_READ_TIMEOUT_SECONDS", raising=False)

    policy = resolve_model_connection_policy(configured_timeout=60)

    assert policy.timeout == 180.0
    assert policy.max_retries == 1


def test_policy_preserves_a_longer_model_profile_timeout(monkeypatch):
    monkeypatch.delenv("AGENT_LLM_READ_TIMEOUT_SECONDS", raising=False)

    assert resolve_model_connection_policy(configured_timeout=300).timeout == 300.0


def test_environment_can_raise_the_model_read_timeout_floor(monkeypatch):
    monkeypatch.setenv("AGENT_LLM_READ_TIMEOUT_SECONDS", "240")

    assert resolve_model_connection_policy(configured_timeout=180).timeout == 240.0


@pytest.mark.parametrize("value", ["0", "-1", "not-a-number", "nan", "inf"])
def test_invalid_environment_timeout_fails_closed(monkeypatch, value):
    monkeypatch.setenv("AGENT_LLM_READ_TIMEOUT_SECONDS", value)

    with pytest.raises(ModelConnectionPolicyError):
        resolve_model_connection_policy(configured_timeout=180)
