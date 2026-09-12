"""Canonical compilation of capabilities, inputs, and snapshots into ProblemIR."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .canonical import sha256_digest
from .contracts import (
    FactSnapshot,
    NormalizedInput,
    ProblemIR,
    SolverContractError,
    _assert_model_payload_is_platform_neutral,
    _freeze_object,
)
from .ports import DecisionModelLoader, ProblemFamilyRegistryPort


class ProblemCompilationError(SolverContractError):
    code = "model_compile_failed"
    stage = "compile"

    def __init__(self, detail: str = "problem model could not be compiled") -> None:
        super().__init__(detail)
        self.detail = detail


ModelCompileError = ProblemCompilationError


@dataclass(frozen=True)
class ProblemIRCompiler:
    registry: ProblemFamilyRegistryPort
    compiler_version: str = "solver.compiler.v1"
    decision_model_loader: DecisionModelLoader | None = None

    def __post_init__(self) -> None:
        if not callable(getattr(self.registry, "compile_problem", None)):
            raise SolverContractError("registry must provide compile_problem")
        if not isinstance(self.compiler_version, str) or not self.compiler_version.strip():
            raise SolverContractError("compiler_version is required")
        object.__setattr__(self, "compiler_version", self.compiler_version.strip())

    def compile(
        self,
        *,
        capability: Any,
        normalized_input: NormalizedInput,
        fact_snapshot: FactSnapshot,
    ) -> ProblemIR:
        if not isinstance(normalized_input, NormalizedInput):
            raise ProblemCompilationError("normalized input is invalid")
        if not isinstance(fact_snapshot, FactSnapshot):
            raise ProblemCompilationError("fact snapshot is invalid")
        if fact_snapshot.normalized_input_digest != normalized_input.digest:
            raise ProblemCompilationError(
                "fact snapshot does not match normalized input"
            )
        operation_key = self._required_capability_text(capability, "operation_key")
        if operation_key != normalized_input.operation_key:
            raise ProblemCompilationError(
                "capability operation does not match normalized input"
            )
        package_id = self._required_capability_text(capability, "package_id")
        if package_id != fact_snapshot.package_id:
            raise ProblemCompilationError(
                "capability package does not match fact snapshot"
            )
        package_revision = self._required_capability_text(
            capability, "package_revision"
        )
        if package_revision != fact_snapshot.package_revision:
            raise ProblemCompilationError(
                "capability package revision does not match fact snapshot"
            )
        family_key = self._capability_value(capability, "problem_family")
        if not isinstance(family_key, str) or not family_key.strip():
            raise ProblemCompilationError("capability problem_family is missing")
        family_key = family_key.strip()
        try:
            model, model_digest = self._resolve_model(capability)
            required_facts = self._required_facts(model)
            self._verify_snapshot_completeness(fact_snapshot, required_facts)
            resolved_capability = self._capability_with_model(
                capability, model, model_digest
            )
            compiled = self.registry.compile_problem(
                family_key,
                capability=resolved_capability,
                normalized_input=normalized_input,
                fact_snapshot=fact_snapshot,
            )
            components = self._components(compiled)
            problem_id = sha256_digest(
                {
                    "schema_version": "solver.problem.v1",
                    "compiler_version": self.compiler_version,
                    "problem_family": family_key,
                    "model": {
                        "decision_model_digest": model_digest,
                        "document": model,
                    },
                    "normalized_input_digest": normalized_input.digest,
                    "fact_snapshot_digest": fact_snapshot.digest,
                    "compiled_problem": components,
                }
            )
            return ProblemIR(
                problem_id=problem_id,
                problem_family=family_key,
                decision_space=components["decision_space"],
                parameters=components["parameters"],
                constraints=tuple(components["constraints"]),
                objectives=tuple(components["objectives"]),
                limits=components["limits"],
            )
        except Exception as exc:
            if isinstance(exc, ProblemCompilationError):
                raise
            raise ProblemCompilationError() from exc

    def resolve_capability_model(
        self, capability: Any
    ) -> tuple[Mapping[str, Any], tuple[str, ...]]:
        """Attach the locked decision model and return its required facts."""
        model, model_digest = self._resolve_model(capability)
        return (
            self._capability_with_model(capability, model, model_digest),
            self._required_facts(model),
        )

    @staticmethod
    def _components(compiled: Any) -> dict[str, Any]:
        if isinstance(compiled, ProblemIR):
            payload = compiled.to_dict()
        elif isinstance(compiled, Mapping):
            payload = dict(compiled)
        else:
            raise ProblemCompilationError("problem-family compilation returned invalid data")
        required = {
            "decision_space",
            "parameters",
            "constraints",
            "objectives",
            "limits",
        }
        allowed = required | {"schema_version", "problem_id", "problem_family"}
        if set(payload) - allowed or not required.issubset(payload):
            raise ProblemCompilationError("compiled problem fields are invalid")
        if not isinstance(payload["decision_space"], Mapping):
            raise ProblemCompilationError("compiled decision_space is invalid")
        if not isinstance(payload["parameters"], Mapping):
            raise ProblemCompilationError("compiled parameters are invalid")
        if not isinstance(payload["constraints"], (list, tuple)):
            raise ProblemCompilationError("compiled constraints are invalid")
        if not isinstance(payload["objectives"], (list, tuple)):
            raise ProblemCompilationError("compiled objectives are invalid")
        if not isinstance(payload["limits"], Mapping):
            raise ProblemCompilationError("compiled limits are invalid")
        return {name: payload[name] for name in sorted(required)}

    def _resolve_model(
        self, capability: Any
    ) -> tuple[Mapping[str, Any], str]:
        explicit = self._capability_value(capability, "decision_model")
        declared_digest = self._capability_value(capability, "decision_model_digest")
        loaded_from_loader = explicit is None
        loaded_digest: Any = None
        if explicit is None:
            if self.decision_model_loader is None:
                raise ProblemCompilationError("locked decision model is missing")
            loaded = self.decision_model_loader.load(capability=capability)
            if not isinstance(loaded, Mapping) or set(loaded) != {"document", "digest"}:
                raise ProblemCompilationError("locked decision model result is malformed")
            explicit = loaded.get("document")
            loaded_digest = loaded.get("digest")
        if not isinstance(explicit, Mapping) or not explicit:
            raise ProblemCompilationError("decision model document is malformed")
        model = _freeze_object(explicit, "decision model")
        _assert_model_payload_is_platform_neutral(model, "decision model")
        if "required_facts" not in model:
            raise ProblemCompilationError(
                "decision model required_facts declaration is missing"
            )
        body_digest = sha256_digest(model)
        if loaded_from_loader:
            digests = (declared_digest, loaded_digest)
            if any(
                not isinstance(value, str) or not value.strip()
                for value in digests
            ) or any(value.strip() != body_digest for value in digests):
                raise ProblemCompilationError(
                    "locked decision model digest does not match content"
                )
        else:
            if declared_digest is not None:
                if not isinstance(declared_digest, str):
                    raise ProblemCompilationError(
                        "inline decision model digest does not match content"
                    )
                if not declared_digest.strip():
                    raise ProblemCompilationError(
                        "inline decision model digest does not match content"
                    )
                if declared_digest.strip() != body_digest:
                    raise ProblemCompilationError(
                        "inline decision model digest does not match content"
                    )
        return model, body_digest

    @staticmethod
    def _required_facts(model: Mapping[str, Any]) -> tuple[str, ...]:
        raw = model.get("required_facts")
        if not isinstance(raw, (list, tuple)):
            raise ProblemCompilationError("decision model required_facts is malformed")
        values = tuple(str(item or "").strip() for item in raw)
        if any(not value for value in values) or len(set(values)) != len(values):
            raise ProblemCompilationError("decision model required_facts is malformed")
        return tuple(sorted(values))

    @staticmethod
    def _verify_snapshot_completeness(
        fact_snapshot: FactSnapshot, required_facts: tuple[str, ...]
    ) -> None:
        proven = tuple(fact_snapshot.checks.get("required") or ())
        present = {str(fact.get("name") or "") for fact in fact_snapshot.facts}
        if proven != required_facts or set(required_facts) - present:
            raise ProblemCompilationError(
                "fact snapshot does not prove model-required facts"
            )

    @classmethod
    def _capability_with_model(
        cls,
        capability: Any,
        model: Mapping[str, Any],
        model_digest: str,
    ) -> Mapping[str, Any]:
        fields = (
            "package_id",
            "package_revision",
            "operation_key",
            "decision_model_id",
            "problem_family",
            "input_schema",
            "result_schema",
            "contract_digest",
            "validator_revision",
            "enabled",
            "package_digest",
            "operation_digest",
            "decision_model_digest",
            "input_schema_digest",
            "result_schema_digest",
            "engine_capabilities",
        )
        payload = {}
        for name in fields:
            value = cls._capability_value(capability, name)
            if value is not None:
                payload[name] = value
        payload["decision_model"] = model
        payload["decision_model_digest"] = model_digest
        return _freeze_object(payload, "resolved capability")

    @classmethod
    def _required_capability_text(cls, capability: Any, name: str) -> str:
        value = cls._capability_value(capability, name)
        if not isinstance(value, str) or not value.strip():
            raise ProblemCompilationError("capability {} is missing".format(name))
        return value.strip()

    @staticmethod
    def _capability_value(capability: Any, name: str) -> Any:
        if isinstance(capability, Mapping):
            return capability.get(name)
        return getattr(capability, name, None)


CanonicalProblemCompiler = ProblemIRCompiler


def compile_problem(*, registry: ProblemFamilyRegistryPort, **kwargs: Any) -> ProblemIR:
    return ProblemIRCompiler(registry=registry).compile(**kwargs)
