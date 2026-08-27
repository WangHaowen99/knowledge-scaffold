"""Public, connector-neutral result verifiers used by TCK admission.

The verifier deliberately consumes only the public submit/result documents.  It
does not import Consumer code or an optimization engine; its job is to
recompute the small family golden semantics and reject a forged normalized
result before a Connector is admitted.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from contracts.solver.validation import ContractValidationError, validate_document


def _objective_key(
    candidate: Mapping[str, Any], objectives: list[Mapping[str, Any]]
) -> tuple[int, ...]:
    values: list[int] = []
    for objective in objectives:
        value = candidate["metrics"][objective["metric"]]
        values.append(-value if objective["sense"] == "maximize" else value)
    return tuple(values)


def verify_binary_ordered_route_result(
    *, request: Mapping[str, Any], result: Mapping[str, Any]
) -> bool:
    """Return ``True`` only when a normalized family result is independently valid.

    This is intentionally a boolean API: admission tooling must not expose
    jsonschema, engine, or local-path details from an untrusted Connector.
    """

    try:
        validate_document("submit-request", request)
        validate_document("result", result)
        problem = request["problem"]
        if (
            result.get("request_ref") != request.get("request_ref")
            or result.get("problem_family") != problem.get("problem_family")
            or result.get("family_version") != problem.get("family_version")
        ):
            return False
        payload = problem["payload"]
        selection = payload["parameters"]["candidate_selection"]
        candidates = selection["candidates"]
        objectives = payload["objectives"]
        eligible = [candidate for candidate in candidates if candidate["eligible"]]
        if not eligible:
            return (
                result.get("solve_outcome") == "infeasible"
                and result.get("assignments") == {}
                and result.get("objective_values") == {}
            )
        expected = min(eligible, key=lambda item: _objective_key(item, objectives))
        expected_assignment = {
            "candidate_id": expected["candidate_id"],
            **expected["assignment"],
        }
        expected_objectives = {
            objective["metric"]: expected["metrics"][objective["metric"]]
            for objective in objectives
        }
        assurance = result.get("assurance")
        if not isinstance(assurance, Mapping):
            return False
        # The public golden requires a proven optimal result.  A Connector may
        # include a different best bound only for non-authoritative diagnostics;
        # admission rejects that mismatch because it would make the verifier
        # acceptance non-deterministic.
        return (
            result.get("solve_outcome") == "optimal"
            and result.get("assignments") == expected_assignment
            and result.get("objective_values") == expected_objectives
            and assurance.get("gap") == "0"
            and assurance.get("best_bound") == expected_objectives[objectives[0]["metric"]]
        )
    except (ContractValidationError, KeyError, TypeError, ValueError, IndexError):
        return False


def verify_solver_result(
    *, request: Mapping[str, Any], result: Mapping[str, Any]
) -> bool:
    """Dispatch the public verifier by problem family/version."""

    try:
        family = request["problem"]["problem_family"]
        version = request["problem"]["family_version"]
    except (KeyError, TypeError):
        return False
    if family == "binary_ordered_route" and version == "1.0":
        return verify_binary_ordered_route_result(request=request, result=result)
    return False


__all__ = [
    "verify_binary_ordered_route_result",
    "verify_solver_result",
]
