"""Deterministic enumeration for ordered binary option assignments."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from solver_coordination.contracts import (
    Candidate,
    CandidateSet,
    FactSnapshot,
    MAX_PLAN_ITEMS,
    NormalizedInput,
    Option,
    OrderedPlan,
    SolverContractError,
)

PROBLEM_FAMILY = "binary_ordered_route.v1"
_BINARY_OPTION_DOMAIN = (Option.OPTION_0.value, Option.OPTION_1.value)


def expected_candidate_ids(item_count: int) -> tuple[str, ...]:
    if isinstance(item_count, bool) or not isinstance(item_count, int) or item_count < 1:
        raise SolverContractError("item_count must be positive")
    return (
        "ALL_0",
        "ALL_1",
        *("OPTION_0_TO_OPTION_1_K{:04d}".format(index) for index in range(1, item_count)),
        *("OPTION_1_TO_OPTION_0_K{:04d}".format(index) for index in range(1, item_count)),
    )


def assert_candidate_set_shape(candidate_set: CandidateSet) -> None:
    """Check the family invariants for any positive item count."""
    if candidate_set.problem_family != PROBLEM_FAMILY:
        raise SolverContractError("candidate set problem_family is unsupported")
    item_count = len(candidate_set.plan.items)
    expected_ids = expected_candidate_ids(item_count)
    actual_ids = tuple(candidate.candidate_id for candidate in candidate_set.candidates)
    if actual_ids != expected_ids:
        raise SolverContractError("candidate set IDs do not match the ordered option contract")
    if candidate_set.candidate_count != 2 * item_count:
        raise SolverContractError("candidate set candidate_count does not match the family contract")
    if candidate_set.plan.max_transitions != 1:
        raise SolverContractError("ordered option family requires max_transitions=1")


def enumerate_ordered_routes(
    plan: OrderedPlan,
    *,
    source_run_id: str,
    knowledge_package_id: str,
    knowledge_package_revision: str,
) -> CandidateSet:
    item_count = len(plan.items)
    candidates = [
        Candidate("ALL_0", (Option.OPTION_0,) * item_count, 0),
        Candidate("ALL_1", (Option.OPTION_1,) * item_count, 0),
    ]
    candidates.extend(
        Candidate(
            "OPTION_0_TO_OPTION_1_K{:04d}".format(switch_after),
            (Option.OPTION_0,) * switch_after + (Option.OPTION_1,) * (item_count - switch_after),
            1,
        )
        for switch_after in range(1, item_count)
    )
    candidates.extend(
        Candidate(
            "OPTION_1_TO_OPTION_0_K{:04d}".format(switch_after),
            (Option.OPTION_1,) * switch_after + (Option.OPTION_0,) * (item_count - switch_after),
            1,
        )
        for switch_after in range(1, item_count)
    )
    candidate_set = CandidateSet.create(
        source_run_id=source_run_id,
        problem_family=PROBLEM_FAMILY,
        knowledge_package_id=knowledge_package_id,
        knowledge_package_revision=knowledge_package_revision,
        plan=plan,
        candidates=candidates,
    )
    assert_candidate_set_shape(candidate_set)
    return candidate_set


class BinaryOrderedRouteAdapter:
    """Registry adapter preserving the existing CandidateSet API."""

    family_key = PROBLEM_FAMILY
    problem_family = PROBLEM_FAMILY

    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def enumerate(
        self,
        *,
        plan: OrderedPlan,
        source_run_id: str,
        knowledge_package_id: str,
        knowledge_package_revision: str,
    ) -> CandidateSet:
        return enumerate_ordered_routes(
            plan,
            source_run_id=source_run_id,
            knowledge_package_id=knowledge_package_id,
            knowledge_package_revision=knowledge_package_revision,
        )

    generate_candidates = enumerate

    def compile_problem(
        self,
        *,
        capability: Any,
        normalized_input: NormalizedInput,
        fact_snapshot: FactSnapshot,
    ) -> dict[str, Any]:
        model = self._capability_value(capability, "decision_model")
        if not isinstance(model, Mapping):
            raise SolverContractError("decision_model must be an object")
        raw_decision_space = model.get("decision_space")
        if not isinstance(raw_decision_space, Mapping):
            raise SolverContractError("decision_space must be an object")
        self._validate_decision_space(raw_decision_space)
        decision_space = dict(raw_decision_space)
        materialized_facts = {
            str(fact["name"]): {key: fact.get(key) for key in ("value", "unit", "precision") if key in fact}
            for fact in fact_snapshot.facts
        }
        model_parameters = model.get("parameters")
        if model_parameters is not None and not isinstance(model_parameters, Mapping):
            raise SolverContractError("parameters must be an object")
        parameters = dict(model_parameters or {})
        parameters.update(
            {
                "input": dict(normalized_input.values),
                "facts": materialized_facts,
            }
        )
        constraints = model.get("constraints")
        objectives = model.get("objectives")
        limits = model.get("limits")
        if not isinstance(constraints, (list, tuple)):
            raise SolverContractError("constraints must be an array")
        if not isinstance(objectives, (list, tuple)):
            raise SolverContractError("objectives must be an array")
        if not isinstance(limits, Mapping):
            raise SolverContractError("limits must be an object")
        return {
            "decision_space": decision_space,
            "parameters": parameters,
            "constraints": list(constraints),
            "objectives": list(objectives),
            "limits": dict(limits),
        }

    @staticmethod
    def _capability_value(capability: Any, name: str) -> Any:
        if isinstance(capability, Mapping):
            return capability.get(name)
        return getattr(capability, name, None)

    @classmethod
    def _validate_decision_space(cls, raw: Mapping[str, Any]) -> None:
        keys = set(raw)
        if keys == {"choices"}:
            cls._validate_domain(raw["choices"], "decision_space choices")
            return
        if "variables" not in raw:
            raise SolverContractError("decision_space requires choices or non-empty variables")
        variables = raw["variables"]
        if not isinstance(variables, (list, tuple)) or not variables:
            raise SolverContractError("decision_space variables must be a non-empty array")
        if len(variables) > MAX_PLAN_ITEMS:
            raise SolverContractError("decision_space variables exceed the ordered route limit")
        if all(isinstance(item, str) for item in variables):
            names = cls._validate_variable_names(variables)
            if keys == {"variables", "option_domain"}:
                cls._validate_domain(raw["option_domain"], "decision_space option_domain")
                return
            if keys == {"variables", "domains"}:
                domains = raw["domains"]
                if not isinstance(domains, Mapping) or set(domains) != set(names):
                    raise SolverContractError("decision_space domains must match every variable exactly")
                for name in names:
                    cls._validate_domain(domains[name], "decision_space domain for {}".format(name))
                return
            raise SolverContractError("decision_space variables require option_domain or domains")
        if all(isinstance(item, Mapping) for item in variables):
            if keys != {"variables"}:
                raise SolverContractError("inline decision_space variables cannot use shared domains")
            names: list[str] = []
            for item in variables:
                if set(item) != {"name", "domain"}:
                    raise SolverContractError("inline decision_space variable fields are invalid")
                name = item.get("name")
                if not isinstance(name, str) or not name or name != name.strip():
                    raise SolverContractError("decision_space variable name is invalid")
                names.append(name)
                cls._validate_domain(item.get("domain"), "decision_space domain for {}".format(name))
            cls._validate_variable_names(names)
            return
        raise SolverContractError("decision_space variables are invalid")

    @staticmethod
    def _validate_variable_names(values: tuple[Any, ...] | list[Any]) -> tuple[str, ...]:
        names = tuple(values)
        if any(not isinstance(name, str) or not name or name != name.strip() for name in names) or len(
            set(names)
        ) != len(names):
            raise SolverContractError("decision_space variables are invalid")
        return names

    @staticmethod
    def _validate_domain(value: Any, field_name: str) -> None:
        if not isinstance(value, (list, tuple)) or tuple(value) != _BINARY_OPTION_DOMAIN:
            raise SolverContractError("{} must be OPTION_0 and OPTION_1 in that order".format(field_name))


BinaryOrderedRouteProblemFamilyAdapter = BinaryOrderedRouteAdapter
