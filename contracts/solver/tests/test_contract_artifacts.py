from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest
from jsonschema import (
    Draft202012Validator,
    FormatChecker,
    RefResolver,
    ValidationError,
)
from contracts.solver.validation import (
    ContractValidationError,
    canonical_json_bytes,
    parse_json_bytes,
    validate_traceparent,
)


CONTRACT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = CONTRACT_ROOT / "schemas"


def _load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def _schema(name: str) -> dict:
    return _load_json(SCHEMA_ROOT / name)


def _example(name: str) -> dict:
    return _load_json(CONTRACT_ROOT / "examples" / name)


def _validate(schema_name: str, value: dict) -> None:
    schema = _schema(schema_name)
    family_path = CONTRACT_ROOT / "problem-families" / "binary_ordered_route.v1.schema.json"
    family = _load_json(family_path)
    resolver = RefResolver.from_schema(
        schema,
        store={
            family["$id"]: family,
            family_path.as_uri(): family,
        },
    )
    Draft202012Validator(
        schema, resolver=resolver, format_checker=FormatChecker()
    ).validate(value)


def test_contract_artifact_is_standalone_and_complete():
    expected = {
        "openapi/solver-job-v1.openapi.yaml",
        "schemas/capabilities.schema.json",
        "schemas/submit-request.schema.json",
        "schemas/job.schema.json",
        "schemas/result.schema.json",
        "schemas/error.schema.json",
        "problem-families/binary_ordered_route.v1.schema.json",
        "canonicalization.md",
        "validation.py",
        "examples/capabilities.json",
        "examples/submit-request.json",
        "examples/job.json",
        "examples/result.json",
        "examples/error.json",
    }
    assert expected <= {
        path.relative_to(CONTRACT_ROOT).as_posix()
        for path in CONTRACT_ROOT.rglob("*")
        if path.is_file()
    }

    source = (CONTRACT_ROOT / "validation.py").read_text(encoding="utf-8")
    assert "solver_coordination" not in source
    assert "consumer" not in source


def test_openapi_declares_all_transient_submit_statuses():
    source = (
        CONTRACT_ROOT / "openapi" / "solver-job-v1.openapi.yaml"
    ).read_text(encoding="utf-8")

    for status in ("'429':", "'502':", "'503':"):
        assert status in source


def test_contract_validation_utility_loads_without_consumer_source():
    module_path = CONTRACT_ROOT / "validation.py"
    spec = importlib.util.spec_from_file_location("solver_contract_validation", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    payload = _example("submit-request.json")
    assert module.validate_document("submit-request", payload) == payload
    canonical = module.canonical_json_bytes({"z": [2, 1], "a": "e\u0301"})
    assert canonical == '{"a":"é","z":[2,1]}'.encode("utf-8")


@pytest.mark.parametrize(
    ("schema_name", "example_name"),
    [
        ("capabilities.schema.json", "capabilities.json"),
        ("submit-request.schema.json", "submit-request.json"),
        ("job.schema.json", "job.json"),
        ("result.schema.json", "result.json"),
        ("error.schema.json", "error.json"),
    ],
)
def test_published_examples_validate(schema_name: str, example_name: str):
    _validate(schema_name, _example(example_name))


@pytest.mark.parametrize(
    ("schema_name", "example_name"),
    [
        ("capabilities.schema.json", "capabilities.json"),
        ("submit-request.schema.json", "submit-request.json"),
        ("job.schema.json", "job.json"),
        ("result.schema.json", "result.json"),
        ("error.schema.json", "error.json"),
    ],
)
def test_wire_schemas_reject_unknown_fields(schema_name: str, example_name: str):
    payload = _example(example_name)
    payload["unexpected"] = "synthetic"
    with pytest.raises(ValidationError):
        _validate(schema_name, payload)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("workspace_id", "workspace_synthetic"),
        ("owner_id", "owner_synthetic"),
        ("user_id", "user_synthetic"),
        ("authorization", "placeholder"),
        ("access_token", "placeholder"),
        ("service_credential_ref", "credential_ref_synthetic"),
        ("endpoint", "https://service.example.net"),
    ],
)
def test_submit_problem_recursively_rejects_workspace_secrets_and_endpoints(
    field_name: str, value: str
):
    payload = _example("submit-request.json")
    payload["problem"]["payload"]["parameters"]["private"] = {
        field_name: value
    }
    with pytest.raises(ValidationError):
        _validate("submit-request.schema.json", payload)


@pytest.mark.parametrize(
    ("family", "version"),
    [
        ("binary_ordered_route.v1", "1.0"),
        ("binary_ordered_route", "2.0"),
        ("candidate_selection", "1.0"),
        ("vendor_private", "1.0"),
    ],
)
def test_submit_rejects_unknown_or_misencoded_family_version(
    family: str, version: str
):
    payload = _example("submit-request.json")
    payload["problem"]["problem_family"] = family
    payload["problem"]["family_version"] = version
    with pytest.raises(ValidationError):
        _validate("submit-request.schema.json", payload)


def test_binary_ordered_route_family_schema_rejects_noncanonical_problem():
    payload = deepcopy(_example("submit-request.json")["problem"])
    _validate("../problem-families/binary_ordered_route.v1.schema.json", payload)

    payload["payload"]["parameters"]["candidate_selection"]["candidates"][0][
        "metrics"
    ]["primary_score"] = 1.5
    with pytest.raises(ValidationError):
        _validate("../problem-families/binary_ordered_route.v1.schema.json", payload)


def test_attempt_lost_is_a_terminal_connector_job_status():
    payload = _example("job.json")
    payload["status"] = "attempt_lost"
    _validate("job.schema.json", payload)

    payload["status"] = "timed_out"
    with pytest.raises(ValidationError):
        _validate("job.schema.json", payload)


@pytest.mark.parametrize(
    "category",
    ["contract", "authentication", "capacity", "transport", "execution", "result"],
)
def test_error_contract_freezes_all_standard_categories(category: str):
    payload = _example("error.json")
    payload["error"]["category"] = category
    _validate("error.schema.json", payload)


def test_error_contract_rejects_stack_and_request_echo():
    payload = _example("error.json")
    payload["error"]["stack"] = "synthetic stack"
    with pytest.raises(ValidationError):
        _validate("error.schema.json", payload)


def test_result_requires_assurance_and_engine_metadata():
    payload = _example("result.json")
    del payload["assurance"]
    with pytest.raises(ValidationError):
        _validate("result.schema.json", payload)

    payload = _example("result.json")
    del payload["engine"]["connector_build"]
    with pytest.raises(ValidationError):
        _validate("result.schema.json", payload)


def test_capability_result_assurances_are_closed_and_nonempty():
    payload = _example("capabilities.json")
    payload["problem_families"][0]["result_assurances"] = []
    with pytest.raises(ValidationError):
        _validate("capabilities.schema.json", payload)

    payload = _example("capabilities.json")
    payload["problem_families"][0]["result_assurances"] = ["vendor_claim"]
    with pytest.raises(ValidationError):
        _validate("capabilities.schema.json", payload)


@pytest.mark.parametrize("payload", [b'"\\udead"', b'{"x":"\\ud800"}'])
def test_strict_json_parser_rejects_lone_unicode_surrogates(payload: bytes):
    with pytest.raises(ContractValidationError, match="surrogate"):
        parse_json_bytes(payload)

    # Canonicalization is independently fail-closed when called directly on
    # a value produced by a permissive JSON decoder.
    permissive = json.loads(payload)
    with pytest.raises(ContractValidationError, match="surrogate"):
        canonical_json_bytes(permissive)


@pytest.mark.parametrize(
    "value",
    [
        "00-" + "0" * 32 + "-" + "b" * 16 + "-01",
        "00-" + "a" * 32 + "-" + "0" * 16 + "-01",
        "01-" + "a" * 32 + "-" + "b" * 16 + "-01",
        "00-" + "A" * 32 + "-" + "b" * 16 + "-01",
    ],
)
def test_traceparent_validation_rejects_noncanonical_or_reserved_values(value):
    with pytest.raises(ContractValidationError, match="traceparent"):
        validate_traceparent(value)
