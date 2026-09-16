# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from __future__ import annotations

import pytest
from solver_coordination.errors import (
    ERRORS,
    CandidateDigestMismatchError,
    CandidateNotFoundError,
    KnowledgePackageSolverContractIncompatibleError,
    VerificationDivergenceError,
    VerificationRuntimeFailure,
    error_payload,
    public_error_status,
)


def test_public_error_registry_uses_stable_lower_snake_case_codes():
    assert ERRORS["candidate_not_found"].http_status == 404
    assert ERRORS["candidate_digest_mismatch"].http_status == 409
    assert ERRORS["validation_service_unavailable"].retryable is True
    assert ERRORS["validation_failed"].http_status == 200
    assert ERRORS["validation_failed"].retryable is False
    assert ERRORS["quality_requirement_not_met"].http_status == 409
    assert ERRORS["quality_requirement_not_met"].retryable is False
    assert ERRORS["knowledge_package_solver_contract_incompatible"].http_status == 409
    assert ERRORS["knowledge_package_solver_contract_incompatible"].retryable is False
    assert all(code == code.lower() and " " not in code for code in ERRORS)


@pytest.mark.parametrize(
    ("error_type", "code", "status", "retryable"),
    [
        (CandidateNotFoundError, "candidate_not_found", 404, False),
        (CandidateDigestMismatchError, "candidate_digest_mismatch", 409, False),
        (
            KnowledgePackageSolverContractIncompatibleError,
            "knowledge_package_solver_contract_incompatible",
            409,
            False,
        ),
        (VerificationDivergenceError, "verification_divergence", 409, False),
        (VerificationRuntimeFailure, "verification_runtime_failed", 503, True),
    ],
)
def test_public_error_exception_carries_contract_code(
    error_type,
    code,
    status,
    retryable,
):
    error = error_type("internal detail")
    assert error.code == code
    assert public_error_status(error.code) == status
    expected_payload = {"code": code}
    if retryable:
        expected_payload["retryable"] = True
    assert error_payload(error.code) == expected_payload


def test_verification_error_retryability_is_stable():
    assert error_payload("verification_runtime_failed") == {
        "code": "verification_runtime_failed",
        "retryable": True,
    }
    assert error_payload("verification_divergence") == {"code": "verification_divergence"}
