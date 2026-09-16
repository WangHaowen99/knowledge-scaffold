# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from __future__ import annotations

from decimal import Decimal, localcontext

import pytest
from backend_synthetic_credentials import synthetic_secret
from solver_coordination.contracts import (
    AuthoritativeResult,
    FactSnapshot,
    NormalizedInput,
    ProblemIR,
    SolverContractError,
    SolverResult,
    VerificationResult,
)
from solver_coordination.fact_snapshot import FactSnapshotBuilder, FactSnapshotInvalidError
from solver_coordination.input_binding import SolverInputBinder


def _schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "count": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "unit": "item",
                "precision": 0,
            },
            "mode": {"type": "string", "enum": ["fast", "safe"]},
        },
        "required": ["count", "mode"],
        "additionalProperties": False,
    }


def _input(values: dict[str, object] | None = None) -> NormalizedInput:
    return NormalizedInput(
        operation_key="solve_synthetic_route",
        values=values or {"count": 3, "mode": "safe"},
        source_evidence=({"kind": "user_statement", "ref": "evidence-synthetic-1"},),
        normalization={
            "unit_conversions": (),
            "precision": "declared-by-schema",
        },
    )


def _snapshot(
    *,
    facts: object | None = None,
    as_of: str = "2026-01-01T00:00:00Z",
    run_id: str = "run-synthetic",
    required_facts: tuple[str, ...] = ("capacity",),
):
    return FactSnapshotBuilder().build(
        workspace_id="workspace-synthetic",
        owner_id="owner-synthetic",
        package_id="package-synthetic",
        package_revision="package-revision-synthetic",
        normalized_input=_input(),
        facts=facts
        or [
            {
                "name": "capacity",
                "value": "12.5",
                "unit": "item",
                "precision": "0.1",
                "entity_id": "entity-synthetic-1",
                "business_key": "capacity-synthetic",
                "source_evidence": [{"kind": "fixture", "ref": "fact-synthetic-1"}],
            }
        ],
        bindings={"provider_binding_revision": "binding-revision-synthetic"},
        as_of=as_of,
        run_id=run_id,
        required_facts=required_facts,
    )


def test_input_binder_enforces_schema_and_returns_immutable_normalized_input():
    result = SolverInputBinder().bind(
        operation_key="solve_synthetic_route",
        schema=_schema(),
        values={"count": 3, "mode": "safe"},
        source_evidence=[{"kind": "user_statement", "ref": "evidence-synthetic-1"}],
    )

    assert result.status == "bound"
    assert result.normalized_input is not None
    assert result.normalized_input.values["count"] == 3
    assert result.normalized_input.input_digest.startswith("sha256:")
    with pytest.raises(TypeError):
        result.normalized_input.values["count"] = 4  # type: ignore[index]

    with pytest.raises(SolverContractError, match="schema"):
        NormalizedInput(schema_version="solver.input.v0", operation_key="x", values={})


def test_input_schema_must_explicitly_fail_closed_on_additional_properties():
    for additional_schema in (
        {key: value for key, value in _schema().items() if key != "additionalProperties"},
        {**_schema(), "additionalProperties": True},
    ):
        with pytest.raises(SolverContractError, match="additionalProperties"):
            SolverInputBinder().bind(
                operation_key="solve_synthetic_route",
                schema=additional_schema,
                values={"count": 3, "mode": "safe"},
            )


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"count": 3, "mode": "other"}, "enum"),
        ({"count": 0, "mode": "safe"}, "minimum"),
        ({"count": "3", "mode": "safe"}, "type"),
        ({"count": 3, "mode": "safe", "extra": True}, "additionalProperties"),
    ],
)
def test_input_binder_rejects_invalid_values_with_structured_diagnostics(values, message):
    result = SolverInputBinder().bind(
        operation_key="solve_synthetic_route",
        schema=_schema(),
        values=values,
    )
    assert result.status == "invalid_input"
    assert message in result.errors[0]["code"] or message in result.errors[0]["message"]


def test_missing_and_ambiguous_values_are_structured_awaiting_input():
    binder = SolverInputBinder()
    missing = binder.bind(
        operation_key="solve_synthetic_route",
        schema=_schema(),
        values={"mode": "safe"},
    )
    assert missing.status == "awaiting_input"
    assert missing.awaiting_input["missing"] == ("count",)

    ambiguous = binder.bind(
        operation_key="solve_synthetic_route",
        schema=_schema(),
        values={"count": 3, "mode": "safe"},
        ambiguous_fields={"count": [3, 4]},
    )
    assert ambiguous.status == "awaiting_input"
    assert ambiguous.awaiting_input["ambiguous"]["count"] == (3, 4)


def test_backend_identity_cannot_enter_model_input():
    for field_name in ("workspace_id", "source_run_id", "script_path"):
        with pytest.raises(SolverContractError, match="runtime identity"):
            NormalizedInput(
                operation_key="solve_synthetic_route",
                values={"count": 3, field_name: "identity-synthetic"},
            )
    with pytest.raises(SolverContractError, match="runtime identity"):
        NormalizedInput(
            operation_key="solve_synthetic_route",
            values={"count": 3},
            source_evidence=({"workspace_id": "identity-synthetic"},),
        )
    with pytest.raises(SolverContractError, match="credential"):
        NormalizedInput(
            operation_key="solve_synthetic_route",
            values={"count": 3},
            normalization={"apiKey": synthetic_secret("solver_input_binding-credential-01")},
        )


def test_number_values_have_one_decimal_wire_form():
    schema = {
        "type": "object",
        "properties": {"ratio": {"type": "number"}},
        "required": ["ratio"],
        "additionalProperties": False,
    }
    bound = [
        SolverInputBinder().bind(operation_key="solve_synthetic_route", schema=schema, values={"ratio": value})
        for value in (3, 3.0)
    ]
    assert all(item.status == "bound" for item in bound)
    assert {item.normalized_input.values["ratio"] for item in bound} == {"3"}
    assert len({item.normalized_input.digest for item in bound}) == 1

    numeric_string = SolverInputBinder().bind(
        operation_key="solve_synthetic_route",
        schema=schema,
        values={"ratio": "3.0"},
    )
    assert numeric_string.status == "invalid_input"
    assert numeric_string.errors[0]["code"] == "type"


@pytest.mark.parametrize(
    "value",
    [
        Decimal("12345678901234567890123456789"),
        Decimal("1e19"),
        Decimal("1e-13"),
        Decimal("1e999999999999"),
    ],
)
def test_number_precision_and_exponent_limits_fail_as_structured_input_errors(value):
    schema = {
        "type": "object",
        "properties": {"ratio": {"type": "number"}},
        "required": ["ratio"],
        "additionalProperties": False,
    }
    result = SolverInputBinder().bind(operation_key="solve_synthetic_route", schema=schema, values={"ratio": value})
    assert result.status == "invalid_input"
    assert result.errors[0]["code"] == "precision"


def test_number_normalization_does_not_depend_on_the_ambient_decimal_context():
    schema = {
        "type": "object",
        "properties": {"ratio": {"type": "number"}},
        "required": ["ratio"],
        "additionalProperties": False,
    }
    with localcontext() as context:
        context.prec = 5
        result = SolverInputBinder().bind(
            operation_key="solve_synthetic_route",
            schema=schema,
            values={"ratio": Decimal("12345678901234567890.12345")},
        )
    assert result.status == "bound"
    assert result.normalized_input.values["ratio"] == "12345678901234567890.12345"


def test_string_values_are_nfc_normalized_before_enum_and_length_validation():
    schema = {
        "type": "object",
        "properties": {
            "label": {
                "type": "string",
                "enum": ["\u00e9"],
                "minLength": 1,
                "maxLength": 1,
            }
        },
        "required": ["label"],
        "additionalProperties": False,
    }

    result = SolverInputBinder().bind(
        operation_key="solve_synthetic_route",
        schema=schema,
        values={"label": "e\u0301"},
    )

    assert result.status == "bound"
    assert result.normalized_input.values["label"] == "\u00e9"


def test_input_binder_recursively_validates_objects_and_array_items():
    schema = {
        "type": "object",
        "properties": {
            "config": {
                "type": "object",
                "properties": {"threshold": {"type": "number", "minimum": 1}},
                "required": ["threshold"],
                "additionalProperties": False,
            },
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"code": {"type": "string"}},
                    "required": ["code"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["config", "items"],
        "additionalProperties": False,
    }
    bound = SolverInputBinder().bind(
        operation_key="solve_synthetic_route",
        schema=schema,
        values={"config": {"threshold": 2}, "items": [{"code": "a"}]},
    )
    assert bound.status == "bound"
    assert bound.normalized_input.values["config"]["threshold"] == "2"

    nested_unknown = SolverInputBinder().bind(
        operation_key="solve_synthetic_route",
        schema=schema,
        values={
            "config": {"threshold": 2, "extra": True},
            "items": [{"code": "a"}],
        },
    )
    assert nested_unknown.status == "invalid_input"
    assert nested_unknown.errors[0]["field"] == "config.extra"

    nested_missing = SolverInputBinder().bind(
        operation_key="solve_synthetic_route",
        schema=schema,
        values={"config": {}, "items": [{"code": "a"}]},
    )
    assert nested_missing.status == "awaiting_input"
    assert nested_missing.awaiting_input["missing"] == ("config.threshold",)

    bad_item = SolverInputBinder().bind(
        operation_key="solve_synthetic_route",
        schema=schema,
        values={"config": {"threshold": 2}, "items": [{"code": 7}]},
    )
    assert bad_item.status == "invalid_input"
    assert bad_item.errors[0]["field"] == "items[0].code"


def test_input_binder_rejects_unsupported_schema_keywords_recursively():
    schema = _schema()
    schema["properties"]["mode"]["pattern"] = "synthetic"
    with pytest.raises(SolverContractError, match="unsupported keyword"):
        SolverInputBinder().bind(
            operation_key="solve_synthetic_route",
            schema=schema,
            values={"count": 3, "mode": "safe"},
        )

    malformed_type = _schema()
    malformed_type["properties"]["mode"]["type"] = [{"not": "a-type"}]
    with pytest.raises(SolverContractError, match="unsupported type"):
        SolverInputBinder().bind(
            operation_key="solve_synthetic_route",
            schema=malformed_type,
            values={"count": 3, "mode": "safe"},
        )


def test_fact_snapshot_contains_provenance_and_stable_digest():
    first = _snapshot()
    second = _snapshot(run_id="run-synthetic-replay")

    assert isinstance(first, FactSnapshot)
    assert first.schema_version == "solver.fact-snapshot.v1"
    assert first.bindings["provider_binding_revision"] == "binding-revision-synthetic"
    assert first.as_of == "2026-01-01T00:00:00Z"
    assert first.facts[0]["unit"] == "item"
    assert first.facts[0]["precision"] == "0.1"
    assert first.snapshot_digest == first.digest == second.digest
    with pytest.raises(TypeError):
        first.bindings["provider_binding_revision"] = "changed"  # type: ignore[index]


def test_fact_snapshot_binds_package_id_into_serialized_content_and_digest():
    assert "package_id" in FactSnapshot.__dataclass_fields__

    def build(package_id: str) -> FactSnapshot:
        return FactSnapshotBuilder().build(
            workspace_id="workspace-synthetic",
            owner_id="owner-synthetic",
            package_id=package_id,
            package_revision="package-revision-synthetic",
            normalized_input=_input(),
            facts=[{"name": "capacity", "value": "12"}],
            bindings={"provider_binding_revision": "binding-revision-synthetic"},
            as_of="2026-01-01T00:00:00Z",
            required_facts=("capacity",),
        )

    first = build("package-synthetic-1")
    second = build("package-synthetic-2")

    assert first.package_id == "package-synthetic-1"
    assert first.to_dict()["package_id"] == "package-synthetic-1"
    assert first.digest != second.digest


@pytest.mark.parametrize(
    "identity_overrides",
    [
        {
            "package_id": "package-synthetic",
            "knowledge_package_id": "package-other",
        },
        {
            "package_revision": "package-revision-synthetic",
            "knowledge_package_revision": "revision-other",
        },
    ],
)
def test_fact_snapshot_rejects_conflicting_package_identity_aliases(identity_overrides):
    arguments = {
        "workspace_id": "workspace-synthetic",
        "owner_id": "owner-synthetic",
        "package_id": "package-synthetic",
        "package_revision": "package-revision-synthetic",
        "normalized_input": _input(),
        "facts": [{"name": "capacity", "value": "12"}],
        "bindings": {"provider_binding_revision": "binding-revision-synthetic"},
        "as_of": "2026-01-01T00:00:00Z",
        "required_facts": ("capacity",),
    }
    arguments.update(identity_overrides)

    with pytest.raises(FactSnapshotInvalidError, match="package_(id|revision).*(conflict|match)"):
        FactSnapshotBuilder().build(**arguments)


def test_fact_snapshot_does_not_coerce_values_without_an_explicit_fact_schema():
    numeric = _snapshot(facts=[{"name": "capacity", "value": 3}])
    textual = _snapshot(facts=[{"name": "capacity", "value": "3"}])

    assert numeric.facts[0]["value"] == 3
    assert textual.facts[0]["value"] == "3"
    assert numeric.digest != textual.digest


def test_duplicate_or_conflicting_facts_fail_closed():
    facts = [
        {"name": "capacity", "value": "12", "unit": "item"},
        {"name": "capacity", "value": "13", "unit": "item"},
    ]
    with pytest.raises(FactSnapshotInvalidError) as exc_info:
        _snapshot(facts=facts)
    assert exc_info.value.code == "fact_snapshot_invalid"
    assert exc_info.value.details[0]["code"] == "conflict"

    duplicate = [
        {"name": "capacity", "value": "12", "unit": "item"},
        {"name": "capacity", "value": "12", "unit": "item"},
    ]
    with pytest.raises(FactSnapshotInvalidError) as duplicate_info:
        _snapshot(facts=duplicate)
    assert duplicate_info.value.details[0]["code"] == "duplicate"

    with pytest.raises(FactSnapshotInvalidError) as name_info:
        _snapshot(facts={"capacity": {"name": "other", "value": "12"}})
    assert name_info.value.details[0]["code"] == "conflict"


@pytest.mark.parametrize(
    "binding_key",
    ["access_token", "client_secret", "authorization", "apiKey", "base_url"],
)
def test_fact_snapshot_rejects_provider_credential_and_endpoint_bindings(binding_key):
    with pytest.raises(FactSnapshotInvalidError, match="binding"):
        FactSnapshotBuilder().build(
            workspace_id="workspace-synthetic",
            owner_id="owner-synthetic",
            package_id="package-synthetic",
            package_revision="package-revision-synthetic",
            normalized_input=_input(),
            facts=[{"name": "capacity", "value": "12"}],
            bindings={binding_key: synthetic_secret("solver_input_binding-credential-01")},
            as_of="2026-01-01T00:00:00Z",
            run_id="run-synthetic",
            required_facts=("capacity",),
        )


def test_snapshot_materializes_unit_precision_and_evidence_keys_for_every_fact():
    snapshot = _snapshot(facts=[{"name": "capacity", "value": "12"}])
    assert snapshot.facts[0] == {
        "name": "capacity",
        "value": "12",
        "unit": None,
        "precision": None,
        "entity_id": None,
        "business_key": None,
        "source_evidence": (),
    }
    assert snapshot.checks == {
        "required": ("capacity",),
        "missing": (),
        "duplicates": (),
        "conflicts": (),
    }


def test_fact_snapshot_builder_requires_an_explicit_completeness_declaration():
    with pytest.raises(FactSnapshotInvalidError, match="required_facts"):
        FactSnapshotBuilder().build(
            workspace_id="workspace-synthetic",
            owner_id="owner-synthetic",
            package_id="package-synthetic",
            package_revision="package-revision-synthetic",
            normalized_input=_input(),
            facts=[{"name": "capacity", "value": "12"}],
            bindings={"provider_binding_revision": "binding-revision-synthetic"},
            as_of="2026-01-01T00:00:00Z",
        )


def test_fact_snapshot_constructor_rejects_unvalidated_facts_and_checks():
    kwargs = {
        "workspace_id": "workspace-synthetic",
        "owner_id": "owner-synthetic",
        "package_id": "package-synthetic",
        "package_revision": "package-revision-synthetic",
        "normalized_input_digest": _input().digest,
        "bindings": {"provider_binding_revision": "binding-revision-synthetic"},
        "as_of": "2026-01-01T00:00:00Z",
    }
    valid_checks = {
        "required": ["capacity"],
        "missing": [],
        "duplicates": [],
        "conflicts": [],
    }
    with pytest.raises(SolverContractError, match="value"):
        FactSnapshot(facts=({"name": "capacity"},), checks=valid_checks, **kwargs)
    with pytest.raises(SolverContractError, match="conflict"):
        FactSnapshot(
            facts=({"name": "capacity", "value": "12"},),
            checks={**valid_checks, "conflicts": ["capacity"]},
            **kwargs,
        )
    with pytest.raises(SolverContractError, match="unsupported"):
        FactSnapshot(
            facts=({"name": "capacity", "value": "12", "raw_provider": "x"},),
            checks=valid_checks,
            **kwargs,
        )
    with pytest.raises(SolverContractError, match="checks"):
        FactSnapshot(
            facts=({"name": "capacity", "value": "12"},),
            checks={**valid_checks, "unknown": []},
            **kwargs,
        )
    with pytest.raises(SolverContractError, match="checks"):
        FactSnapshot(
            facts=({"name": "capacity", "value": "12"},),
            checks={**valid_checks, "conflicts": ""},
            **kwargs,
        )


def test_fact_snapshot_constructor_requires_complete_checks_and_sorts_facts():
    base = {
        "workspace_id": "workspace-synthetic",
        "owner_id": "owner-synthetic",
        "package_id": "package-synthetic",
        "package_revision": "package-revision-synthetic",
        "normalized_input_digest": _input().digest,
        "bindings": {"provider_binding_revision": "binding-revision-synthetic"},
        "as_of": "2026-01-01T00:00:00Z",
    }
    with pytest.raises(SolverContractError, match="complete"):
        FactSnapshot(
            facts=({"name": "capacity", "value": "12"},),
            checks={"missing": [], "duplicates": [], "conflicts": []},
            **base,
        )
    snapshot = FactSnapshot(
        facts=(
            {"name": "zeta", "value": "2"},
            {"name": "alpha", "value": "1"},
        ),
        checks={
            "required": ["alpha", "zeta"],
            "missing": [],
            "duplicates": [],
            "conflicts": [],
        },
        **base,
    )
    assert tuple(fact["name"] for fact in snapshot.facts) == ("alpha", "zeta")


@pytest.mark.parametrize(
    "field_name",
    [
        "selected_ontology_id",
        "selected_ontology_version",
        "tenant_id",
        "user_id",
        "database_url",
        "auth_token",
    ],
)
def test_central_policy_rejects_runtime_identity_and_credentials(field_name):
    with pytest.raises(SolverContractError, match="runtime identity|credential"):
        NormalizedInput(
            operation_key="solve_synthetic_route",
            values={field_name: "synthetic-value"},
        )


@pytest.mark.parametrize(
    "field_name",
    [
        "secret_key",
        "secretKey",
        "session_token",
        "sessionToken",
        "organization_id",
        "organizationId",
        "package_id",
    ],
)
def test_central_policy_rejects_nested_identity_and_credential_variants(field_name):
    with pytest.raises(SolverContractError, match="runtime identity|credential"):
        NormalizedInput(
            operation_key="solve_synthetic_route",
            values={"request": {"metadata": {field_name: "synthetic-value"}}},
        )


def test_central_policy_rejects_semantic_credential_field_variants():
    with pytest.raises(SolverContractError, match="runtime identity|credential"):
        NormalizedInput(
            operation_key="solve_synthetic_route",
            values={"request": {"api_secret": synthetic_secret("solver_input_binding-credential-02")}},
        )


def test_central_policy_does_not_reject_business_fields_by_substring():
    normalized = NormalizedInput(
        operation_key="solve_synthetic_route",
        values={
            "secret_recipe": "synthetic-recipe",
            "session_token_count": 2,
            "organization_id_format": "external-code",
            "auth_token_ttl": 60,
        },
    )

    assert normalized.values["session_token_count"] == 2


@pytest.mark.parametrize(
    "facts,source_evidence",
    [
        ([{"name": "auth_token", "value": "synthetic-value"}], ()),
        (
            [
                {
                    "name": "capacity",
                    "value": "12",
                    "source_evidence": [{"database_url": "synthetic-value"}],
                }
            ],
            (),
        ),
        ([{"name": "capacity", "value": "12"}], ({"tenant_id": "synthetic"},)),
    ],
)
def test_fact_snapshot_rejects_identity_or_credentials_in_facts_and_evidence(facts, source_evidence):
    with pytest.raises(FactSnapshotInvalidError, match="runtime identity|credential"):
        FactSnapshotBuilder().build(
            workspace_id="workspace-synthetic",
            owner_id="owner-synthetic",
            package_id="package-synthetic",
            package_revision="package-revision-synthetic",
            normalized_input=_input(),
            facts=facts,
            bindings={"provider_binding_revision": "binding-revision-synthetic"},
            as_of="2026-01-01T00:00:00Z",
            source_evidence=source_evidence,
            required_facts=(str(facts[0]["name"]),),
        )


def test_new_result_contracts_keep_feasibility_optimality_and_proof_separate():
    problem = ProblemIR(
        problem_id="sha256:problem-synthetic",
        problem_family="synthetic_family.v1",
        decision_space={"x": ["a", "b"]},
        parameters={"limit": 3},
        constraints=({"kind": "bound", "value": 3},),
        objectives=({"name": "cost", "direction": "min"},),
        limits={"steps": 10},
    )
    solver = SolverResult(
        problem_id=problem.problem_id,
        status="optimal",
        assignment={"x": "a"},
        objective_values={"cost": "1"},
        diagnostics=(),
        engine={"name": "synthetic-engine", "version": "1"},
    )
    verification = VerificationResult(
        problem_id=problem.problem_id,
        status="verified",
        feasibility="feasible",
        optimality="proven_optimal",
        proof_kind="exhaustive",
    )
    authoritative = AuthoritativeResult(
        run_id="run-synthetic",
        job_id="job-synthetic",
        status="completed",
        feasibility=verification.feasibility,
        optimality=verification.optimality,
        proof_kind=verification.proof_kind,
        authoritative=True,
        summary={"assignment": solver.assignment},
        result_artifact_ref="artifact://result-synthetic",
        verification_evidence_ref="artifact://evidence-synthetic",
        knowledge_revision_id="revision-synthetic",
        fact_snapshot_digest="sha256:fact-synthetic",
        problem_digest=problem.problem_digest,
    )
    assert solver.to_dict()["status"] == "optimal"
    assert verification.to_dict()["feasibility"] == "feasible"
    assert verification.to_dict()["optimality"] == "proven_optimal"
    assert verification.to_dict()["proof_kind"] == "exhaustive"
    assert authoritative.authoritative is True


@pytest.mark.parametrize(
    "payload_field",
    ["assignment", "objective_values", "diagnostics", "engine"],
)
def test_solver_result_rejects_identity_or_credentials_in_external_payloads(
    payload_field,
):
    kwargs = {
        "problem_id": "sha256:problem-synthetic",
        "status": "optimal",
        "assignment": {"choice": "a"},
        "objective_values": {"cost": "1"},
        "diagnostics": (),
        "engine": {"name": "synthetic-engine", "version": "1"},
    }
    forbidden = {"nested": {"session_token": synthetic_secret("solver_input_binding-credential-01")}}
    kwargs[payload_field] = (forbidden,) if payload_field == "diagnostics" else forbidden

    with pytest.raises(SolverContractError, match="runtime identity|credential"):
        SolverResult(**kwargs)


@pytest.mark.parametrize("field_name", ["apiSecret", "APISecret", "oauth-token"])
@pytest.mark.parametrize("payload_field", ["diagnostics", "engine"])
def test_solver_result_fails_closed_for_semantic_credential_metadata(field_name, payload_field):
    kwargs = {
        "problem_id": "sha256:problem-synthetic",
        "status": "optimal",
        "assignment": {"choice": "a"},
        "objective_values": {"cost": "1"},
        "diagnostics": (),
        "engine": {"name": "synthetic-engine", "version": "1"},
    }
    payload = {"metadata": {field_name: synthetic_secret("solver_input_binding-credential-02")}}
    kwargs[payload_field] = (payload,) if payload_field == "diagnostics" else payload

    with pytest.raises(SolverContractError, match="runtime identity|credential"):
        SolverResult(**kwargs)


@pytest.mark.parametrize("payload_field", ["diagnostics", "evidence"])
def test_verification_result_rejects_identity_or_credentials_in_external_payloads(
    payload_field,
):
    kwargs = {
        "problem_id": "sha256:problem-synthetic",
        "status": "verified",
        "feasibility": "feasible",
        "optimality": "proven_optimal",
        "proof_kind": "exhaustive",
        "diagnostics": (),
        "evidence": (),
    }
    kwargs[payload_field] = ({"nested": {"organization_id": "identity-synthetic"}},)

    with pytest.raises(SolverContractError, match="runtime identity|credential"):
        VerificationResult(**kwargs)


@pytest.mark.parametrize("field_name", ["orgId", "passwordHash"])
def test_verification_evidence_fails_closed_for_semantic_identity_and_credentials(
    field_name,
):
    with pytest.raises(SolverContractError, match="runtime identity|credential"):
        VerificationResult(
            problem_id="sha256:problem-synthetic",
            status="verified",
            feasibility="feasible",
            optimality="not_proven",
            proof_kind="constraint_recalculation",
            evidence=({"metadata": {field_name: synthetic_secret("solver_input_binding-credential-02")}},),
        )


def test_authoritative_result_rejects_identity_or_credentials_in_summary():
    with pytest.raises(SolverContractError, match="runtime identity|credential"):
        AuthoritativeResult(
            run_id="run-synthetic",
            job_id="job-synthetic",
            status="completed",
            feasibility="feasible",
            optimality="proven_optimal",
            proof_kind="exhaustive",
            authoritative=True,
            summary={"assignment": {"secret_key": synthetic_secret("solver_input_binding-credential-01")}},
            result_artifact_ref="artifact://result-synthetic",
            verification_evidence_ref="artifact://evidence-synthetic",
            knowledge_revision_id="revision-synthetic",
            fact_snapshot_digest="sha256:fact-synthetic",
            problem_digest="sha256:problem-synthetic",
        )


@pytest.mark.parametrize("field_name", ["org_id", "password_hash"])
def test_authoritative_summary_fails_closed_for_semantic_identity_and_credentials(
    field_name,
):
    with pytest.raises(SolverContractError, match="runtime identity|credential"):
        AuthoritativeResult(
            run_id="run-synthetic",
            job_id="job-synthetic",
            status="completed",
            feasibility="feasible",
            optimality="not_proven",
            proof_kind="constraint_recalculation",
            authoritative=True,
            summary={"assignment": {field_name: synthetic_secret("solver_input_binding-credential-02")}},
            result_artifact_ref="artifact://result-synthetic",
            verification_evidence_ref="artifact://evidence-synthetic",
            knowledge_revision_id="revision-synthetic",
            fact_snapshot_digest="sha256:fact-synthetic",
            problem_digest="sha256:problem-synthetic",
        )


@pytest.mark.parametrize(
    ("status", "feasibility", "optimality", "proof_kind"),
    [
        ("running", "feasible", "proven_optimal", "exhaustive"),
        ("completed", "unknown", "not_proven", "exhaustive"),
        ("completed", "feasible", "not_applicable", "exhaustive"),
        ("completed", "infeasible", "proven_optimal", "certificate"),
        ("completed", "feasible", "proven_optimal", "none"),
        ("completed", "feasible", "proven_optimal", "constraint_recalculation"),
        ("completed", "infeasible", "not_applicable", "constraint_recalculation"),
    ],
)
def test_authoritative_result_rejects_inconsistent_status_semantics(status, feasibility, optimality, proof_kind):
    with pytest.raises(SolverContractError, match="status|feasibility|optimality|proof"):
        AuthoritativeResult(
            run_id="run-synthetic",
            job_id="job-synthetic",
            status=status,
            feasibility=feasibility,
            optimality=optimality,
            proof_kind=proof_kind,
            authoritative=True,
            summary={"assignment": {"choice": "a"}},
            result_artifact_ref="artifact://result-synthetic",
            verification_evidence_ref="artifact://evidence-synthetic",
            knowledge_revision_id="revision-synthetic",
            fact_snapshot_digest="sha256:fact-synthetic",
            problem_digest="sha256:problem-synthetic",
        )
