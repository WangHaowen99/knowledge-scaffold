import pytest

from solver_coordination.connectors.helpers import (
    capability_fingerprint,
    idempotency_key,
    validate_traceparent,
)


def test_idempotency_key_is_opaque_and_stable():
    arguments = {
        "secret": b"synthetic-idempotency-secret",
        "binding_revision_id": "binding-synthetic",
        "run_id": "run-synthetic",
        "attempt_id": "attempt-synthetic",
        "problem_digest": "sha256:problem-synthetic",
    }

    first = idempotency_key(**arguments)

    assert first == idempotency_key(**arguments)
    assert first.startswith("hmac-sha256:")
    assert "run-synthetic" not in first


def test_capability_fingerprint_is_canonical_without_a_legacy_dto():
    first = capability_fingerprint(
        {"service": {"build": "sha256:" + "1" * 64}, "api_versions": ["ortools.solver.v1"]}
    )
    reordered = capability_fingerprint(
        {"api_versions": ["ortools.solver.v1"], "service": {"build": "sha256:" + "1" * 64}}
    )

    assert first == reordered
    assert first.startswith("sha256:")


@pytest.mark.parametrize(
    "value",
    (
        "00-" + "0" * 32 + "-" + "1" * 16 + "-01",
        "00-" + "1" * 32 + "-" + "0" * 16 + "-01",
        "not-a-traceparent",
    ),
)
def test_validate_traceparent_rejects_invalid_or_reserved_identifiers(value):
    with pytest.raises(ValueError, match="traceparent"):
        validate_traceparent(value)


def test_validate_traceparent_accepts_canonical_version_zero_value():
    value = "00-" + "1" * 32 + "-" + "2" * 16 + "-01"

    assert validate_traceparent(value) == value
