# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Independent exhaustive verification for the ordered binary route profile.

This module intentionally duplicates the small arithmetic and candidate
enumeration rules instead of importing the production profile evaluator.  It
is a trust-boundary component: a production evaluator may be tampered with and
the verifier must still recalculate the answer from the frozen contracts.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, DecimalException, localcontext
from typing import Any

from ..canonical import sha256_digest
from ..contracts import (
    MAX_PLAN_ITEMS,
    FactSnapshot,
    NormalizedInput,
    ProblemIR,
    SolverResult,
    VerificationResult,
)
from ..errors import VerificationDivergenceError, VerificationRuntimeFailure

_PROFILE_ID = "ordered_binary_route.v1"
_FAMILY = "candidate_selection.v1"
_FACT_NAME = "route_metrics"
_OPTION_0 = "OPTION_0"
_OPTION_1 = "OPTION_1"
_OPTIONS = (_OPTION_0, _OPTION_1)
_OBJECTIVES = (
    ("primary_score", "maximize"),
    ("transition_count", "minimize"),
    ("safety_margin", "maximize"),
    ("candidate_rank", "minimize"),
)
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1
_MAX_DECIMAL_DIGITS = 28
_MIN_DECIMAL_EXPONENT = -12
_MAX_DECIMAL_EXPONENT = 18
_MAX_TEXT_LENGTH = 256
_DECIMAL_RE = re.compile(r"^[+-]?(?:(?:[0-9]+)(?:\.[0-9]*)?|\.[0-9]+)" r"(?:[eE][+-]?[0-9]+)?$")


class _MalformedVerification(Exception):
    """Internal marker converted to a divergence response."""


@dataclass(frozen=True)
class _Evaluation:
    candidate_id: str
    assignment: tuple[str, ...]
    eligible: bool
    metrics: Mapping[str, int]


@dataclass(frozen=True)
class _Facts:
    items: tuple[str, ...]
    capacity: int | None = None
    metrics: Mapping[str, Mapping[str, Mapping[str, int]]] | None = None
    candidate_metrics: Mapping[str, Mapping[str, Any]] | None = None


class OrderedBinaryRouteIndependentVerifier:
    """Rebuild and exhaustively verify every canonical candidate."""

    def verify(
        self,
        *,
        problem: ProblemIR,
        capability: Any,
        normalized_input: NormalizedInput,
        fact_snapshot: FactSnapshot,
        solver_result: SolverResult,
    ) -> VerificationResult:
        try:
            if not isinstance(problem, ProblemIR):
                raise _MalformedVerification("problem is invalid")
            if not isinstance(normalized_input, NormalizedInput):
                raise _MalformedVerification("normalized input is invalid")
            if not isinstance(fact_snapshot, FactSnapshot):
                raise _MalformedVerification("fact snapshot is invalid")
            if not isinstance(solver_result, SolverResult):
                raise _MalformedVerification("solver result is invalid")
            self._validate_profile(capability)
            self._validate_problem_identity(
                problem=problem,
                capability=capability,
                normalized_input=normalized_input,
                fact_snapshot=fact_snapshot,
            )
            facts = self._parse_facts(normalized_input, fact_snapshot)
            candidates = self._evaluate_all(facts)
            self._validate_candidate_contract(problem, facts, candidates)
            service_candidate_digest = self._validate_domain_certificate(problem, solver_result)
            limit = self._verification_limit(problem)
            if len(candidates) > limit:
                raise VerificationRuntimeFailure("verification candidate count exceeds the locked limit")

            eligible = tuple(candidate for candidate in candidates if candidate.eligible)
            if not eligible:
                if solver_result.status != "infeasible":
                    raise VerificationDivergenceError("solver result does not claim infeasibility")
                if solver_result.assignment or solver_result.objective_values:
                    raise VerificationDivergenceError("infeasible solver result contains an assignment")
                return self._verification_result(
                    problem=problem,
                    fact_snapshot=fact_snapshot,
                    candidates=candidates,
                    eligible=eligible,
                    selected=None,
                    service_candidate_digest=service_candidate_digest,
                )

            if solver_result.status not in {"optimal", "feasible"}:
                raise VerificationDivergenceError("solver result status is unsupported for a feasible family")
            selected = min(eligible, key=self._objective_key)
            self._compare_solver_result(solver_result, selected, facts.items)
            return self._verification_result(
                problem=problem,
                fact_snapshot=fact_snapshot,
                candidates=candidates,
                eligible=eligible,
                selected=selected,
                service_candidate_digest=service_candidate_digest,
            )
        except Exception as exc:
            if isinstance(
                exc,
                (VerificationDivergenceError, VerificationRuntimeFailure),
            ):
                raise
            if isinstance(exc, _MalformedVerification):
                raise VerificationDivergenceError(str(exc)) from exc
            raise VerificationDivergenceError("independent ordered route verification diverged") from exc

    @staticmethod
    def _validate_profile(capability: Any) -> None:
        model = _value(capability, "decision_model")
        if not isinstance(model, Mapping):
            raise _MalformedVerification("locked decision model is missing")
        profile_id = model.get("profile_id")
        if not isinstance(profile_id, str):
            raise _MalformedVerification("ordered route profile is unsupported")
        if not profile_id or profile_id != profile_id.strip():
            raise _MalformedVerification("ordered route profile is unsupported")
        if profile_id != _PROFILE_ID:
            raise _MalformedVerification("ordered route profile is unsupported")
        if _value(capability, "problem_family") not in {_FAMILY, "binary_ordered_route.v1"}:
            # The compiler's candidate-selection envelope is the production
            # family; accepting the legacy family here only keeps old callers
            # fail-closed rather than silently selecting another profile.
            raise _MalformedVerification("problem family is unsupported")
        objectives = model.get("objectives")
        if (
            not isinstance(objectives, (list, tuple))
            or tuple((item.get("metric"), item.get("sense")) for item in objectives if isinstance(item, Mapping))
            != _OBJECTIVES
        ):
            raise _MalformedVerification("locked objective order is invalid")

    @classmethod
    def _validate_problem_identity(
        cls,
        *,
        problem: ProblemIR,
        capability: Any,
        normalized_input: NormalizedInput,
        fact_snapshot: FactSnapshot,
    ) -> None:
        if problem.problem_family != _FAMILY:
            raise _MalformedVerification("problem family is unsupported")
        if fact_snapshot.normalized_input_digest != normalized_input.digest:
            raise _MalformedVerification("fact snapshot does not match input")
        model = _value(capability, "decision_model")
        if not isinstance(model, Mapping):
            raise _MalformedVerification("locked decision model is missing")
        compiled = {
            "decision_space": problem.decision_space,
            "parameters": problem.parameters,
            "constraints": problem.constraints,
            "objectives": problem.objectives,
            "limits": problem.limits,
        }
        model_digest = sha256_digest(model)
        expected_problem_id = sha256_digest(
            {
                "schema_version": "solver.problem.v1",
                "compiler_version": "solver.compiler.v1",
                "problem_family": problem.problem_family,
                "model": {
                    "decision_model_digest": model_digest,
                    "document": model,
                },
                "normalized_input_digest": normalized_input.digest,
                "fact_snapshot_digest": fact_snapshot.digest,
                "compiled_problem": compiled,
            }
        )
        if expected_problem_id != problem.problem_id:
            raise _MalformedVerification("problem or fact snapshot identity diverged")

    @classmethod
    def _parse_facts(
        cls,
        normalized_input: NormalizedInput,
        fact_snapshot: FactSnapshot,
    ) -> _Facts:
        raw_items = normalized_input.values.get("items")
        if not isinstance(raw_items, (list, tuple)) or not raw_items:
            raise _MalformedVerification("ordered input items are invalid")
        if len(raw_items) > MAX_PLAN_ITEMS:
            raise _MalformedVerification("ordered input exceeds item limit")
        item_ids: list[str] = []
        for expected_order, item in enumerate(raw_items, start=1):
            if not isinstance(item, Mapping):
                raise _MalformedVerification("ordered item is invalid")
            if item.get("item_order") != expected_order:
                raise _MalformedVerification("ordered item sequence diverged")
            item_id = item.get("item_id")
            if not isinstance(item_id, str) or not item_id.strip():
                raise _MalformedVerification("ordered item identifier is invalid")
            item_id = item_id.strip()
            if item_id in item_ids:
                raise _MalformedVerification("ordered item identifiers are duplicated")
            item_ids.append(item_id)

        route_facts = [fact for fact in fact_snapshot.facts if fact.get("name") == _FACT_NAME]
        if len(route_facts) != 1:
            raise _MalformedVerification("route facts are missing")
        raw = route_facts[0].get("value")
        if not isinstance(raw, Mapping) or frozenset(raw) not in {
            frozenset({"metric_scale", "rounding", "capacity_limit", "items"}),
            frozenset({"metric_scale", "rounding", "candidates"}),
        }:
            raise _MalformedVerification("route facts are malformed")
        scale = raw.get("metric_scale")
        if isinstance(scale, bool) or not isinstance(scale, int):
            raise _MalformedVerification("route metric scale is invalid")
        if scale < 1 or scale > _INT64_MAX:
            raise _MalformedVerification("route metric scale is invalid")
        if raw.get("rounding") != "ROUND_HALF_EVEN":
            raise _MalformedVerification("route rounding mode is unsupported")
        if set(raw) == {"metric_scale", "rounding", "candidates"}:
            raw_candidates = raw.get("candidates")
            assignments = cls._candidate_assignments(len(item_ids))
            expected_ids = tuple(candidate_id for candidate_id, _ in assignments)
            if not isinstance(raw_candidates, Mapping) or set(raw_candidates) != set(expected_ids):
                raise _MalformedVerification("route candidate facts do not match input")
            candidate_metrics: dict[str, Mapping[str, Any]] = {}
            for candidate_id in expected_ids:
                values = raw_candidates[candidate_id]
                if (
                    not isinstance(values, Mapping)
                    or set(values) != {"eligible", "primary_score", "safety_margin"}
                    or not isinstance(values.get("eligible"), bool)
                ):
                    raise _MalformedVerification("route candidate metrics are malformed")
                candidate_metrics[candidate_id] = {
                    "eligible": values["eligible"],
                    "primary_score": cls._scaled_int(
                        values["primary_score"],
                        scale,
                        "{}.primary_score".format(candidate_id),
                    ),
                    "safety_margin": cls._scaled_int(
                        values["safety_margin"],
                        scale,
                        "{}.safety_margin".format(candidate_id),
                    ),
                }
            return _Facts(
                items=tuple(item_ids),
                candidate_metrics=candidate_metrics,
            )

        capacity = cls._scaled_int(raw.get("capacity_limit"), scale, "capacity_limit")
        raw_item_facts = raw.get("items")
        if not isinstance(raw_item_facts, Mapping) or set(raw_item_facts) != set(item_ids):
            raise _MalformedVerification("route item facts do not match input")
        metrics: dict[str, Mapping[str, Mapping[str, int]]] = {}
        for item_id in item_ids:
            options = raw_item_facts[item_id]
            if not isinstance(options, Mapping) or set(options) != set(_OPTIONS):
                raise _MalformedVerification("route option facts are malformed")
            normalized_options: dict[str, Mapping[str, int]] = {}
            for option in _OPTIONS:
                values = options[option]
                if not isinstance(values, Mapping) or set(values) != {
                    "primary_score",
                    "load",
                    "safety",
                }:
                    raise _MalformedVerification("route metric fields are malformed")
                normalized_options[option] = {
                    name: cls._scaled_int(values[name], scale, f"{item_id}.{option}.{name}")
                    for name in ("primary_score", "load", "safety")
                }
            metrics[item_id] = normalized_options
        return _Facts(capacity=capacity, items=tuple(item_ids), metrics=metrics)

    @staticmethod
    def _candidate_assignments(
        item_count: int,
    ) -> tuple[tuple[str, tuple[str, ...]], ...]:
        assignments: list[tuple[str, tuple[str, ...]]] = [
            ("ALL_0", (_OPTION_0,) * item_count),
            ("ALL_1", (_OPTION_1,) * item_count),
        ]
        assignments.extend(
            (
                "OPTION_0_TO_OPTION_1_K{:04d}".format(switch),
                (_OPTION_0,) * switch + (_OPTION_1,) * (item_count - switch),
            )
            for switch in range(1, item_count)
        )
        assignments.extend(
            (
                "OPTION_1_TO_OPTION_0_K{:04d}".format(switch),
                (_OPTION_1,) * switch + (_OPTION_0,) * (item_count - switch),
            )
            for switch in range(1, item_count)
        )
        return tuple(assignments)

    @classmethod
    def _evaluate_all(cls, facts: _Facts) -> tuple[_Evaluation, ...]:
        assignments = cls._candidate_assignments(len(facts.items))

        evaluations: list[_Evaluation] = []
        for rank, (candidate_id, assignment) in enumerate(assignments):
            transitions = sum(left != right for left, right in zip(assignment, assignment[1:]))
            if facts.candidate_metrics is not None:
                values = facts.candidate_metrics[candidate_id]
                evaluations.append(
                    _Evaluation(
                        candidate_id=candidate_id,
                        assignment=assignment,
                        eligible=values["eligible"],
                        metrics={
                            "primary_score": values["primary_score"],
                            "transition_count": transitions,
                            "safety_margin": values["safety_margin"],
                            "candidate_rank": rank,
                            "total_load": 0,
                        },
                    )
                )
                continue

            if facts.metrics is None or facts.capacity is None:
                raise _MalformedVerification("route facts are incomplete")
            primary = 0
            load = 0
            safety: list[int] = []
            for item_id, option in zip(facts.items, assignment):
                values = facts.metrics[item_id][option]
                primary = cls._add(primary, values["primary_score"], "primary_score")
                load = cls._add(load, values["load"], "total_load")
                safety.append(values["safety"])
            margin = cls._subtract(facts.capacity, load, "safety_margin")
            margin = min(margin, *safety)
            evaluations.append(
                _Evaluation(
                    candidate_id=candidate_id,
                    assignment=assignment,
                    eligible=load <= facts.capacity,
                    metrics={
                        "primary_score": primary,
                        "transition_count": transitions,
                        "safety_margin": margin,
                        "candidate_rank": rank,
                        "total_load": load,
                    },
                )
            )
        return tuple(evaluations)

    @classmethod
    def _validate_candidate_contract(
        cls,
        problem: ProblemIR,
        facts: _Facts,
        evaluations: tuple[_Evaluation, ...],
    ) -> None:
        try:
            selection = problem.parameters["candidate_selection"]
            candidates = selection["candidates"]
            declared_count = selection["candidate_count"]
            declared_digest = selection["candidate_evaluation_digest"]
        except (KeyError, TypeError) as exc:
            raise _MalformedVerification("candidate selection payload is missing") from exc
        if not isinstance(candidates, (list, tuple)) or len(candidates) != len(evaluations):
            raise _MalformedVerification("candidate count diverged")
        if declared_count != len(candidates):
            raise _MalformedVerification("candidate count declaration diverged")
        expected_payload: list[dict[str, Any]] = []
        for rank, (evaluation, candidate) in enumerate(zip(evaluations, candidates)):
            if not isinstance(candidate, Mapping) or set(candidate) != {
                "candidate_id",
                "eligible",
                "assignment",
                "metrics",
            }:
                raise _MalformedVerification("candidate fields diverged")
            raw_assignment = candidate["assignment"]
            if not isinstance(raw_assignment, Mapping):
                raise _MalformedVerification("candidate assignment diverged")
            if tuple(raw_assignment) != facts.items:
                raise _MalformedVerification("candidate assignment keys diverged")
            if tuple(raw_assignment.values()) != evaluation.assignment:
                raise _MalformedVerification("candidate assignment diverged")
            expected_assignment = {item_id: option for item_id, option in zip(facts.items, evaluation.assignment)}
            expected = {
                "candidate_id": evaluation.candidate_id,
                "eligible": evaluation.eligible,
                "assignment": expected_assignment,
                "metrics": dict(evaluation.metrics),
            }
            if candidate != expected:
                raise _MalformedVerification("candidate evaluation diverged")
            if candidate["metrics"].get("candidate_rank") != rank:
                raise _MalformedVerification("candidate rank diverged")
            expected_payload.append(expected)
        if declared_digest != sha256_digest(expected_payload):
            raise _MalformedVerification("candidate evaluation digest diverged")

    @staticmethod
    def _verification_limit(problem: ProblemIR) -> int:
        value = problem.limits.get("max_verification_candidates")
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise _MalformedVerification("verification candidate limit is invalid")
        return value

    @staticmethod
    def _objective_key(evaluation: _Evaluation) -> tuple[int, int, int, int]:
        return (
            -evaluation.metrics["primary_score"],
            evaluation.metrics["transition_count"],
            -evaluation.metrics["safety_margin"],
            evaluation.metrics["candidate_rank"],
        )

    @staticmethod
    def _compare_solver_result(
        result: SolverResult,
        selected: _Evaluation,
        item_ids: tuple[str, ...],
    ) -> None:
        assignment = dict(result.assignment)
        expected_assignment = {item_id: option for item_id, option in zip(item_ids, selected.assignment)}
        item_keys = tuple(key for key in assignment if key != "candidate_id")
        if item_keys != item_ids:
            raise VerificationDivergenceError("selected assignment keys diverged")
        if assignment.get("candidate_id") != selected.candidate_id:
            raise VerificationDivergenceError("selected candidate diverged")
        if {item_id: assignment[item_id] for item_id in item_ids} != expected_assignment:
            raise VerificationDivergenceError("selected assignment diverged")
        expected_objectives = {name: selected.metrics[name] for name, _sense in _OBJECTIVES}
        if dict(result.objective_values) != expected_objectives:
            raise VerificationDivergenceError("objective values diverged")

    @staticmethod
    def _validate_domain_certificate(problem: ProblemIR, result: SolverResult) -> str | None:
        if "domain_evaluation" not in problem.parameters:
            return None
        assurances: list[Mapping[str, Any]] = []
        for diagnostic in result.diagnostics:
            if not isinstance(diagnostic, Mapping):
                continue
            if diagnostic.get("kind") != "connector_assurance":
                continue
            assurances.append(diagnostic)
        if len(assurances) != 1:
            raise VerificationDivergenceError("domain candidate evaluation certificate is missing")
        certificate = assurances[0].get("certificate")
        selection = problem.parameters.get("candidate_selection")
        if not isinstance(certificate, Mapping) or not isinstance(selection, Mapping):
            raise VerificationDivergenceError("domain candidate evaluation certificate is invalid")
        expected_count = selection.get("candidate_count")
        raw_candidates = selection.get("candidates")
        if not isinstance(raw_candidates, (list, tuple)):
            raise VerificationDivergenceError("domain candidate evaluation certificate is invalid")
        service_projection: list[dict[str, Any]] = []
        for candidate in raw_candidates:
            metrics: dict[str, Any] = {}
            for name, _sense in _OBJECTIVES:
                metrics[name] = candidate["metrics"][name]
            service_projection.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "eligible": candidate["eligible"],
                    "assignment": dict(candidate["assignment"]),
                    "metrics": metrics,
                }
            )
        expected_digest = sha256_digest(service_projection)
        if certificate.get("schema_version") != "refinery_route_evaluation.v1":
            raise VerificationDivergenceError("domain candidate evaluation certificate diverged")
        if certificate.get("evaluation_source") != "refinery_route.v1":
            raise VerificationDivergenceError("domain candidate evaluation certificate diverged")
        if certificate.get("candidate_count") != expected_count:
            raise VerificationDivergenceError("domain candidate evaluation certificate diverged")
        if certificate.get("candidate_digest") != expected_digest:
            raise VerificationDivergenceError("domain candidate evaluation certificate diverged")
        return str(expected_digest)

    @staticmethod
    def _verification_result(
        *,
        problem: ProblemIR,
        fact_snapshot: FactSnapshot,
        candidates: tuple[_Evaluation, ...],
        eligible: tuple[_Evaluation, ...],
        selected: _Evaluation | None,
        service_candidate_digest: str | None,
    ) -> VerificationResult:
        evidence: dict[str, Any] = {
            "kind": "verifier_exhaustive_check",
            "candidate_count": len(candidates),
            "eligible_count": len(eligible),
            "candidate_digest": problem.parameters["candidate_selection"]["candidate_evaluation_digest"],
            "facts_digest": fact_snapshot.digest,
        }
        if service_candidate_digest is not None:
            evidence["service_candidate_digest"] = service_candidate_digest
        if selected is not None:
            evidence["selected_candidate_id"] = selected.candidate_id
            feasibility = "feasible"
            optimality = "proven_optimal"
        else:
            feasibility = "infeasible"
            optimality = "not_applicable"
        return VerificationResult(
            problem_id=problem.problem_id,
            status="verified",
            feasibility=feasibility,
            optimality=optimality,
            proof_kind="verifier_exhaustive_check",
            evidence=(evidence,),
        )

    @staticmethod
    def _scaled_int(value: Any, scale: int, field_name: str) -> int:
        if isinstance(value, bool):
            raise _MalformedVerification(f"{field_name} is not decimal")
        if isinstance(value, Decimal):
            decimal_value = value
        elif (
            isinstance(value, (str, int)) and len(str(value)) <= _MAX_TEXT_LENGTH and _DECIMAL_RE.fullmatch(str(value))
        ):
            try:
                decimal_value = Decimal(str(value))
            except (DecimalException, ValueError) as exc:
                raise _MalformedVerification(f"{field_name} is not decimal") from exc
        else:
            raise _MalformedVerification(f"{field_name} is not decimal")
        if not decimal_value.is_finite():
            raise _MalformedVerification(f"{field_name} is not finite")
        sign, digits, exponent = decimal_value.as_tuple()
        del sign
        if len(digits) > _MAX_DECIMAL_DIGITS or exponent < _MIN_DECIMAL_EXPONENT or exponent > _MAX_DECIMAL_EXPONENT:
            raise _MalformedVerification(f"{field_name} exceeds decimal bounds")
        try:
            with localcontext() as context:
                context.prec = max(64, len(digits) + len(str(scale)) + 24)
                value_int = int((decimal_value * Decimal(scale)).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))
        except (DecimalException, OverflowError, ValueError) as exc:
            raise _MalformedVerification(f"{field_name} exceeds int64") from exc
        if not _INT64_MIN <= value_int <= _INT64_MAX:
            raise _MalformedVerification(f"{field_name} exceeds int64")
        return value_int

    @staticmethod
    def _add(left: int, right: int, field_name: str) -> int:
        result = left + right
        if not _INT64_MIN <= result <= _INT64_MAX:
            raise _MalformedVerification(f"{field_name} aggregate exceeds int64")
        return result

    @staticmethod
    def _subtract(left: int, right: int, field_name: str) -> int:
        result = left - right
        if not _INT64_MIN <= result <= _INT64_MAX:
            raise _MalformedVerification(f"{field_name} subtraction exceeds int64")
        return result


def _value(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


__all__ = ["OrderedBinaryRouteIndependentVerifier"]
