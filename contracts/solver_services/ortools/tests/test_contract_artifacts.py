from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest
import yaml

from contracts.solver_services.ortools.validation import (
    ContractValidationError,
    parse_json_bytes,
    validate_document,
)


CONTRACT_ROOT = Path(__file__).resolve().parents[1]


def load_example(name: str) -> dict:
    return json.loads(
        (CONTRACT_ROOT / "examples" / f"{name}.json").read_text(encoding="utf-8")
    )


def load_openapi() -> dict:
    return yaml.safe_load(
        (
            CONTRACT_ROOT
            / "openapi"
            / "ortools-solver-v1.openapi.yaml"
        ).read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(
    "document_name",
    ["capabilities", "submit-request", "job", "result", "error"],
)
def test_published_examples_validate(document_name: str) -> None:
    payload = load_example(document_name)
    assert validate_document(document_name, payload) == payload


def test_capabilities_identifies_solver_service() -> None:
    payload = load_example("capabilities")
    validate_document("capabilities", payload)
    assert payload["api_versions"] == ["ortools.solver.v1"]
    assert payload["service"]["name"] == "ortools-solver-service"
    assert payload["problem_types"][0]["limits"]["max_search_workers"] == 1
    assert "connector" not in payload


def test_submit_uses_actual_single_search_worker() -> None:
    payload = load_example("submit-request")
    validate_document("submit-request", payload)
    assert payload["options"]["search_workers"] == 1

    payload["options"]["search_workers"] = 2
    with pytest.raises(ContractValidationError):
        validate_document("submit-request", payload)


def test_openapi_exposes_only_solver_service_routes() -> None:
    document = load_openapi()
    assert set(document["paths"]) == {
        "/health",
        "/api/v1/capabilities",
        "/api/v1/jobs",
        "/api/v1/jobs/{job_id}",
        "/api/v1/jobs/{job_id}/result",
    }


def test_openapi_methods_and_authentication_boundary_are_exact() -> None:
    document = load_openapi()
    assert set(document["paths"]["/health"]) == {"get"}
    assert document["paths"]["/health"]["get"].get("security") == []
    assert set(document["paths"]["/api/v1/capabilities"]) == {"get"}
    assert set(document["paths"]["/api/v1/jobs"]) == {"post"}
    assert set(document["paths"]["/api/v1/jobs/{job_id}"]) == {
        "parameters",
        "get",
        "delete",
    }
    assert set(document["paths"]["/api/v1/jobs/{job_id}/result"]) == {
        "parameters",
        "get",
    }
    assert document["security"] == [{"serviceBearer": []}]
    assert document["components"]["securitySchemes"]["serviceBearer"] == {
        "type": "http",
        "scheme": "bearer",
    }


def test_submit_problem_data_uses_strict_platform_problem_family_contract() -> None:
    payload = load_example("submit-request")
    payload["problem"]["data"]["workspace_id"] = "workspace_forbidden"
    with pytest.raises(ContractValidationError):
        validate_document("submit-request", payload)

    payload = load_example("submit-request")
    payload["requirements"] = {"result_quality": "optimal"}
    with pytest.raises(ContractValidationError):
        validate_document("submit-request", payload)


def test_service_contract_is_self_contained_with_its_problem_family_schema() -> None:
    family_schema = (
        CONTRACT_ROOT
        / "problem-families"
        / "binary_ordered_route.v1.schema.json"
    )
    document = json.loads(family_schema.read_text(encoding="utf-8"))
    assert document["$id"].endswith(
        "/contracts/solver-services/ortools/problem-families/"
        "binary_ordered_route.v1.schema.json"
    )


@pytest.mark.parametrize("status", ["failed", "attempt_lost"])
def test_failure_statuses_require_failure(status: str) -> None:
    payload = load_example("job")
    payload["status"] = status
    with pytest.raises(ContractValidationError):
        validate_document("job", payload)

    payload["failure"] = {
        "code": "EXECUTION_FAILED",
        "category": "execution",
        "retryable": False,
        "diagnostic_ref": "diag_synthetic_0001",
    }
    validate_document("job", payload)


@pytest.mark.parametrize(
    "status", ["accepted", "running", "completed", "cancelled"]
)
def test_nonfailure_statuses_forbid_failure(status: str) -> None:
    payload = load_example("job")
    payload["status"] = status
    payload["failure"] = {
        "code": "EXECUTION_FAILED",
        "category": "execution",
        "retryable": False,
        "diagnostic_ref": "diag_synthetic_0001",
    }
    with pytest.raises(ContractValidationError):
        validate_document("job", payload)


def test_result_requires_correlations_and_service_build() -> None:
    for path in (("job_id",), ("request_id",), ("engine", "service_build")):
        payload = load_example("result")
        target = payload
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]
        with pytest.raises(ContractValidationError):
            validate_document("result", payload)


@pytest.mark.parametrize(
    "category",
    ["validation", "authentication", "capacity", "execution", "result", "internal"],
)
def test_error_categories_are_exact(category: str) -> None:
    payload = load_example("error")
    payload["error"]["category"] = category
    validate_document("error", payload)


def test_error_rejects_former_connector_categories() -> None:
    payload = load_example("error")
    payload["error"]["category"] = "transport"
    with pytest.raises(ContractValidationError):
        validate_document("error", payload)


@pytest.mark.parametrize(
    "document_name",
    ["capabilities", "submit-request", "job", "result", "error"],
)
def test_documents_reject_unknown_top_level_fields(document_name: str) -> None:
    payload = deepcopy(load_example(document_name))
    payload["unexpected"] = "synthetic"
    with pytest.raises(ContractValidationError):
        validate_document(document_name, payload)


def test_strict_json_parser_rejects_duplicate_keys_and_nonfinite_numbers() -> None:
    with pytest.raises(ContractValidationError, match="duplicate"):
        parse_json_bytes(b'{"api_version":"ortools.solver.v1","api_version":"other"}')
    with pytest.raises(ContractValidationError, match="non-finite"):
        parse_json_bytes(b'{"value":NaN}')
