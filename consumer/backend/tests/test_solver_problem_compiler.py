from __future__ import annotations

import pytest
from backend_synthetic_credentials import synthetic_secret
from solver_coordination.canonical import sha256_digest
from solver_coordination.compiler import ProblemCompilationError, ProblemIRCompiler
from solver_coordination.contracts import NormalizedInput
from solver_coordination.fact_snapshot import FactSnapshotBuilder
from solver_coordination.problem_families.registry import ProblemFamilyRegistry


class _SyntheticAdapter:
    family_key = "synthetic_family.v1"

    def compile_problem(self, *, capability, normalized_input, fact_snapshot):
        model = capability["decision_model"]
        return {
            "decision_space": {"choice": model["choices"]},
            "parameters": dict(normalized_input.values),
            "constraints": list(model.get("constraints", [])) + [{"fact_digest": fact_snapshot.digest}],
            "objectives": list(model.get("objectives", [])),
            "limits": dict(model.get("limits", {})),
        }


class _ExplodingAdapter:
    family_key = "exploding_family.v1"

    def compile_problem(self, **kwargs):
        raise RuntimeError("synthetic adapter failure")


class _DefaultingAdapter:
    family_key = "synthetic_family.v1"

    def compile_problem(self, *, capability, normalized_input, fact_snapshot):
        model = capability.get("decision_model", {}) if isinstance(capability, dict) else {}
        return {
            "decision_space": {"choice": list(model.get("choices", []))},
            "parameters": dict(normalized_input.values),
            "constraints": list(model.get("constraints", [])),
            "objectives": list(model.get("objectives", [])),
            "limits": dict(model.get("limits", {})),
        }


class _LockedModelLoader:
    def __init__(self, document, digest):
        self.document = document
        self.digest = digest

    def load(self, *, capability):
        return {"document": self.document, "digest": self.digest}


class _RegistryDouble:
    def compile_problem(self, family_key, **kwargs):
        assert family_key == "synthetic_family.v1"
        return _SyntheticAdapter().compile_problem(**kwargs)


def _input(count: int = 3) -> NormalizedInput:
    return NormalizedInput(
        operation_key="solve_synthetic_route",
        values={"count": count},
        source_evidence=({"kind": "fixture", "ref": "input-synthetic"},),
    )


def _snapshot(
    as_of: str = "2026-01-01T00:00:00Z",
    *,
    normalized_input: NormalizedInput | None = None,
):
    bound_input = normalized_input or _input()
    return FactSnapshotBuilder().build(
        workspace_id="workspace-synthetic",
        owner_id="owner-synthetic",
        package_id="package-synthetic",
        package_revision="package-revision-synthetic",
        normalized_input=bound_input,
        facts=[{"name": "limit", "value": "10", "unit": "item", "precision": "1"}],
        bindings={"provider_binding_revision": "binding-synthetic"},
        as_of=as_of,
        run_id="run-synthetic",
        required_facts=("limit",),
    )


def _capability(*, choices: list[str] | None = None):
    return {
        "operation_key": "solve_synthetic_route",
        "package_id": "package-synthetic",
        "package_revision": "package-revision-synthetic",
        "problem_family": "synthetic_family.v1",
        "decision_model": {
            "choices": choices or ["a", "b"],
            "constraints": [{"kind": "bound", "value": 3}],
            "objectives": [{"name": "cost", "direction": "min"}],
            "limits": {"steps": 10},
            "required_facts": ["limit"],
        },
    }


def test_compiler_emits_canonical_problem_ir_and_digest_binds_all_inputs():
    registry = ProblemFamilyRegistry([_SyntheticAdapter()])
    compiler = ProblemIRCompiler(registry=registry, compiler_version="compiler-synthetic-1")

    first = compiler.compile(capability=_capability(), normalized_input=_input(), fact_snapshot=_snapshot())
    same = compiler.compile(capability=_capability(), normalized_input=_input(), fact_snapshot=_snapshot())
    changed_model = compiler.compile(
        capability=_capability(choices=["a", "b", "c"]),
        normalized_input=_input(),
        fact_snapshot=_snapshot(),
    )
    different_input = _input(4)
    changed_input = compiler.compile(
        capability=_capability(),
        normalized_input=different_input,
        fact_snapshot=_snapshot(normalized_input=different_input),
    )
    changed_snapshot = compiler.compile(
        capability=_capability(),
        normalized_input=_input(),
        fact_snapshot=_snapshot("2026-01-02T00:00:00Z"),
    )
    changed_compiler = ProblemIRCompiler(registry=registry, compiler_version="compiler-synthetic-2").compile(
        capability=_capability(), normalized_input=_input(), fact_snapshot=_snapshot()
    )

    assert first.to_dict()["schema_version"] == "solver.problem.v1"
    assert first.problem_id == same.problem_id == first.problem_digest
    assert (
        len(
            {
                changed_model.problem_id,
                changed_input.problem_id,
                changed_snapshot.problem_id,
                changed_compiler.problem_id,
            }
        )
        == 4
    )


def test_compiler_depends_on_the_registry_port_not_a_concrete_registry_class():
    compiler = ProblemIRCompiler(
        registry=_RegistryDouble(),  # type: ignore[arg-type]
        compiler_version="compiler-synthetic-1",
    )
    problem = compiler.compile(capability=_capability(), normalized_input=_input(), fact_snapshot=_snapshot())
    assert problem.problem_family == "synthetic_family.v1"


def test_compiler_maps_family_failures_to_model_compile_failed():
    compiler = ProblemIRCompiler(registry=ProblemFamilyRegistry())
    with pytest.raises(ProblemCompilationError) as exc_info:
        compiler.compile(capability=_capability(), normalized_input=_input(), fact_snapshot=_snapshot())
    assert exc_info.value.code == "model_compile_failed"

    exploding = ProblemIRCompiler(registry=ProblemFamilyRegistry([_ExplodingAdapter()]))
    exploding_capability = _capability()
    exploding_capability["problem_family"] = "exploding_family.v1"
    with pytest.raises(ProblemCompilationError) as runtime_info:
        exploding.compile(
            capability=exploding_capability,
            normalized_input=_input(),
            fact_snapshot=_snapshot(),
        )
    assert runtime_info.value.code == "model_compile_failed"


def test_compiler_rejects_snapshot_bound_to_a_different_normalized_input():
    compiler = ProblemIRCompiler(registry=ProblemFamilyRegistry([_SyntheticAdapter()]))
    with pytest.raises(ProblemCompilationError) as exc_info:
        compiler.compile(
            capability=_capability(),
            normalized_input=_input(4),
            fact_snapshot=_snapshot(normalized_input=_input(3)),
        )
    assert exc_info.value.code == "model_compile_failed"


def test_compiler_requires_operation_and_complete_package_identity_match():
    compiler = ProblemIRCompiler(registry=ProblemFamilyRegistry([_SyntheticAdapter()]))
    wrong_operation = _capability()
    wrong_operation["operation_key"] = "other_operation"
    with pytest.raises(ProblemCompilationError) as operation_info:
        compiler.compile(
            capability=wrong_operation,
            normalized_input=_input(),
            fact_snapshot=_snapshot(),
        )
    assert operation_info.value.code == "model_compile_failed"

    wrong_revision = _capability()
    wrong_revision["package_revision"] = "other-revision"
    with pytest.raises(ProblemCompilationError) as revision_info:
        compiler.compile(
            capability=wrong_revision,
            normalized_input=_input(),
            fact_snapshot=_snapshot(),
        )
    assert revision_info.value.code == "model_compile_failed"

    wrong_package = _capability()
    wrong_package["package_id"] = "other-package"
    with pytest.raises(ProblemCompilationError) as package_info:
        compiler.compile(
            capability=wrong_package,
            normalized_input=_input(),
            fact_snapshot=_snapshot(),
        )
    assert package_info.value.code == "model_compile_failed"


def test_real_capability_requires_a_version_locked_decision_model_loader():
    from solver_coordination.capability_catalog import SolverOperationCapability

    model = _capability()["decision_model"]
    model_digest = sha256_digest(model)
    capability = SolverOperationCapability(
        package_id="package-synthetic",
        package_revision="package-revision-synthetic",
        operation_key="solve_synthetic_route",
        decision_model_id="model-synthetic",
        problem_family="synthetic_family.v1",
        input_schema={},
        result_schema={},
        contract_digest="contract-digest-synthetic",
        validator_revision="validator-synthetic",
        enabled=True,
        package_digest="package-digest-synthetic",
        operation_digest="operation-digest-synthetic",
        decision_model_digest=model_digest,
        input_schema_digest="input-digest-synthetic",
        result_schema_digest="result-digest-synthetic",
    )
    registry = ProblemFamilyRegistry([_SyntheticAdapter()])
    without_loader = ProblemIRCompiler(registry=registry)
    with pytest.raises(ProblemCompilationError) as missing_info:
        without_loader.compile(
            capability=capability,
            normalized_input=_input(),
            fact_snapshot=_snapshot(),
        )
    assert missing_info.value.code == "model_compile_failed"

    with_loader = ProblemIRCompiler(
        registry=registry,
        decision_model_loader=_LockedModelLoader(model, model_digest),
    )
    problem = with_loader.compile(
        capability=capability,
        normalized_input=_input(),
        fact_snapshot=_snapshot(),
    )
    assert problem.objectives[0]["name"] == "cost"


@pytest.mark.parametrize(
    ("document", "digest"),
    [([], "model-digest-synthetic"), ({"choices": ["a"]}, "wrong-digest")],
)
def test_locked_model_loader_rejects_malformed_or_drifted_models(document, digest):
    capability = {key: value for key, value in _capability().items() if key != "decision_model"}
    capability["decision_model_digest"] = "model-digest-synthetic"
    compiler = ProblemIRCompiler(
        registry=ProblemFamilyRegistry([_DefaultingAdapter()]),
        decision_model_loader=_LockedModelLoader(document, digest),
    )
    with pytest.raises(ProblemCompilationError) as exc_info:
        compiler.compile(
            capability=capability,
            normalized_input=_input(),
            fact_snapshot=_snapshot(),
        )
    assert exc_info.value.code == "model_compile_failed"


def test_locked_model_loader_cannot_self_attest_a_different_body():
    trusted_model = _capability()["decision_model"]
    declared_digest = sha256_digest(trusted_model)
    capability = {key: value for key, value in _capability().items() if key != "decision_model"}
    capability["decision_model_digest"] = declared_digest
    lying_model = {**trusted_model, "choices": ["tampered-choice"]}
    compiler = ProblemIRCompiler(
        registry=ProblemFamilyRegistry([_DefaultingAdapter()]),
        decision_model_loader=_LockedModelLoader(lying_model, declared_digest),
    )

    with pytest.raises(ProblemCompilationError) as exc_info:
        compiler.compile(
            capability=capability,
            normalized_input=_input(),
            fact_snapshot=_snapshot(),
        )

    assert exc_info.value.code == "model_compile_failed"


@pytest.mark.parametrize("declared_digest", ["wrong-digest", "", 123])
def test_inline_decision_model_digest_must_match_the_frozen_body(declared_digest):
    capability = _capability()
    capability["decision_model_digest"] = declared_digest
    compiler = ProblemIRCompiler(registry=ProblemFamilyRegistry([_SyntheticAdapter()]))
    with pytest.raises(ProblemCompilationError) as exc_info:
        compiler.compile(
            capability=capability,
            normalized_input=_input(),
            fact_snapshot=_snapshot(),
        )
    assert exc_info.value.code == "model_compile_failed"


def test_compiler_rejects_snapshot_without_model_required_fact_proof():
    unchecked = FactSnapshotBuilder().build(
        workspace_id="workspace-synthetic",
        owner_id="owner-synthetic",
        package_id="package-synthetic",
        package_revision="package-revision-synthetic",
        normalized_input=_input(),
        facts=[{"name": "limit", "value": "10"}],
        bindings={"provider_binding_revision": "binding-synthetic"},
        as_of="2026-01-01T00:00:00Z",
        required_facts=(),
    )
    compiler = ProblemIRCompiler(registry=ProblemFamilyRegistry([_SyntheticAdapter()]))
    with pytest.raises(ProblemCompilationError) as exc_info:
        compiler.compile(capability=_capability(), normalized_input=_input(), fact_snapshot=unchecked)
    assert exc_info.value.code == "model_compile_failed"


def test_compiler_rejects_runtime_binding_data_in_decision_model():
    capability = _capability()
    capability["decision_model"]["runtime_binding"] = {
        "endpoint": "endpoint-synthetic",
        "token": synthetic_secret("solver_problem_compiler-credential-01"),
    }
    compiler = ProblemIRCompiler(registry=ProblemFamilyRegistry([_SyntheticAdapter()]))
    with pytest.raises(ProblemCompilationError) as exc_info:
        compiler.compile(
            capability=capability,
            normalized_input=_input(),
            fact_snapshot=_snapshot(),
        )
    assert exc_info.value.code == "model_compile_failed"
