"""OR-Tools CP-SAT translator for binary_ordered_route 1.0.

This module is intentionally standalone: it imports neither Consumer code nor
the platform's internal solver coordination package.  The only input is the
public ProblemIR wire document.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Mapping

try:
    from ortools.sat.python import cp_model
except (ImportError, OSError) as exc:  # pragma: no cover - host dependency probe
    cp_model = None
    _CP_SAT_IMPORT_ERROR = exc


INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1


class ProblemValidationError(ValueError):
    """ProblemIR is malformed or violates family semantics."""


class EngineExecutionError(RuntimeError):
    """CP-SAT could not execute a valid model."""


class EngineDeadlineExceeded(TimeoutError):
    """The one absolute deadline was exhausted across lexicographic phases."""


@dataclass(frozen=True)
class EngineResult:
    solve_outcome: str
    assignments: Mapping[str, Any]
    objective_values: Mapping[str, int]
    best_bound: int | str | None
    gap: str | None
    certificate: Mapping[str, Any] | None = None
    engine_name: str = "ortools-cp-sat"
    engine_version: str = ""


def engine_version() -> str:
    try:
        import importlib.metadata

        return importlib.metadata.version("ortools")
    except Exception:  # pragma: no cover - defensive packaging fallback
        return "unknown"


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProblemValidationError(f"{field} must be an integer")
    if value < INT64_MIN or value > INT64_MAX:
        raise ProblemValidationError(f"{field} exceeds int64")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ProblemValidationError(f"{field} must be non-empty text")
    if value != value.strip():
        raise ProblemValidationError(f"{field} must be canonical text")
    return value


def _parse_problem(problem: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    if not isinstance(problem, Mapping):
        raise ProblemValidationError("problem must be an object")
    if problem.get("schema_version") != "solver.problem.v1":
        raise ProblemValidationError("unsupported problem schema")
    if problem.get("problem_family") != "binary_ordered_route":
        raise ProblemValidationError("unsupported problem family")
    if problem.get("family_version") != "1.0":
        raise ProblemValidationError("unsupported problem family version")
    payload = problem.get("payload")
    if not isinstance(payload, Mapping):
        raise ProblemValidationError("problem payload must be an object")
    parameters = payload.get("parameters")
    if not isinstance(parameters, Mapping):
        raise ProblemValidationError("problem parameters must be an object")
    selection = parameters.get("candidate_selection")
    if not isinstance(selection, Mapping):
        raise ProblemValidationError("candidate_selection parameters are required")
    candidates = selection.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ProblemValidationError("candidates must be a non-empty array")
    if len(candidates) > 128:
        raise ProblemValidationError("candidate count exceeds family limit")
    declared_count = selection.get("candidate_count")
    if _integer(declared_count, "candidate_count") != len(candidates):
        raise ProblemValidationError("candidate_count does not match candidates")

    parsed: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    assignment_keys: set[str] | None = None
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, Mapping):
            raise ProblemValidationError(f"candidate[{index}] must be an object")
        expected = {"candidate_id", "eligible", "assignment", "metrics"}
        if set(candidate) != expected:
            raise ProblemValidationError(f"candidate[{index}] fields are invalid")
        candidate_id = _text(candidate.get("candidate_id"), f"candidate[{index}].candidate_id")
        if candidate_id in seen_ids:
            raise ProblemValidationError("candidate_id values must be unique")
        seen_ids.add(candidate_id)
        eligible = candidate.get("eligible")
        if not isinstance(eligible, bool):
            raise ProblemValidationError(f"candidate[{index}].eligible must be boolean")
        assignment = candidate.get("assignment")
        if not isinstance(assignment, Mapping) or not assignment:
            raise ProblemValidationError(f"candidate[{index}].assignment must be an object")
        if "candidate_id" in assignment:
            raise ProblemValidationError("assignment cannot contain candidate_id")
        keys = set(assignment)
        if assignment_keys is None:
            assignment_keys = keys
        elif assignment_keys != keys:
            raise ProblemValidationError("candidate assignment variables must match")
        metrics = candidate.get("metrics")
        if not isinstance(metrics, Mapping) or not metrics:
            raise ProblemValidationError(f"candidate[{index}].metrics must be an object")
        parsed_metrics = {
            _text(name, f"candidate[{index}].metric"): _integer(value, f"candidate[{index}].metrics.{name}")
            for name, value in metrics.items()
        }
        if parsed_metrics.get("candidate_rank") != index:
            raise ProblemValidationError("candidate_rank must equal canonical candidate position")
        parsed.append(
            {
                "candidate_id": candidate_id,
                "eligible": eligible,
                "assignment": dict(assignment),
                "metrics": parsed_metrics,
            }
        )

    objectives = payload.get("objectives")
    if not isinstance(objectives, list) or not objectives:
        raise ProblemValidationError("objectives must be a non-empty array")
    parsed_objectives: list[dict[str, str]] = []
    seen_metrics: set[str] = set()
    for index, objective in enumerate(objectives):
        if not isinstance(objective, Mapping) or set(objective) != {"metric", "sense"}:
            raise ProblemValidationError(f"objective[{index}] fields are invalid")
        metric = _text(objective.get("metric"), f"objective[{index}].metric")
        sense = objective.get("sense")
        if sense not in {"maximize", "minimize"}:
            raise ProblemValidationError(f"objective[{index}].sense is invalid")
        if metric in seen_metrics:
            raise ProblemValidationError("objective metrics must be unique")
        seen_metrics.add(metric)
        parsed_objectives.append({"metric": metric, "sense": sense})
    if parsed_objectives[-1] != {"metric": "candidate_rank", "sense": "minimize"}:
        raise ProblemValidationError("final objective must minimize candidate_rank")
    for candidate in parsed:
        for objective in parsed_objectives:
            if objective["metric"] not in candidate["metrics"]:
                raise ProblemValidationError(
                    f"candidate metric is missing: {objective['metric']}"
                )
    limits = payload.get("limits")
    if not isinstance(limits, Mapping):
        raise ProblemValidationError("problem limits must be an object")
    max_candidates = _integer(limits.get("max_candidates"), "max_candidates")
    if len(parsed) > max_candidates:
        raise ProblemValidationError("candidate count exceeds max_candidates")
    return parsed, parsed_objectives


def _new_model(candidates: list[dict[str, Any]], fixed: list[tuple[str, int]]) -> tuple[cp_model.CpModel, list[cp_model.IntVar]]:
    model = cp_model.CpModel()
    selected = [model.NewBoolVar(f"selected_{index}") for index in range(len(candidates))]
    model.Add(sum(selected) == 1)
    for index, candidate in enumerate(candidates):
        if not candidate["eligible"]:
            model.Add(selected[index] == 0)
    for metric, value in fixed:
        model.Add(
            sum(candidate["metrics"][metric] * selected[index] for index, candidate in enumerate(candidates))
            == value
        )
    return model, selected


def _validate_model(model: cp_model.CpModel) -> None:
    error = model.validate()
    if error:
        if "overflow" in error.casefold():
            raise ProblemValidationError("objective expression exceeds int64")
        raise EngineExecutionError("candidate model is invalid")


def solve_candidate_selection(
    problem: Mapping[str, Any], *, solve_time_ms: int, workers: int
) -> EngineResult:
    """Solve one canonical candidate-selection ProblemIR with CP-SAT."""

    if cp_model is None:
        raise EngineExecutionError("OR-Tools CP-SAT engine is unavailable") from _CP_SAT_IMPORT_ERROR
    solve_time_ms = _integer(solve_time_ms, "solve_time_ms")
    workers = _integer(workers, "workers")
    if solve_time_ms < 1 or workers < 1:
        raise ProblemValidationError("solver limits must be positive")
    deadline = time.monotonic() + solve_time_ms / 1000.0
    candidates, objectives = _parse_problem(problem)
    fixed: list[tuple[str, int]] = []
    final_solver: cp_model.CpSolver | None = None
    final_selected: list[cp_model.IntVar] | None = None
    for objective in objectives:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise EngineDeadlineExceeded("solver deadline exhausted")
        model, selected = _new_model(candidates, fixed)
        expression = sum(
            candidate["metrics"][objective["metric"]] * selected[index]
            for index, candidate in enumerate(candidates)
        )
        if objective["sense"] == "maximize":
            model.Maximize(expression)
        else:
            model.Minimize(expression)
        _validate_model(model)
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = remaining
        # A deterministic single worker is deliberate; the service-level workers
        # limit still protects capacity and can be increased by a later revision.
        solver.parameters.num_search_workers = 1
        solver.parameters.random_seed = 0
        solver.parameters.log_search_progress = False
        status = solver.Solve(model)
        if status == cp_model.INFEASIBLE:
            return EngineResult(
                "infeasible", {}, {}, None, None, engine_version=engine_version()
            )
        if status not in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
            if time.monotonic() >= deadline:
                raise EngineDeadlineExceeded("solver deadline exhausted")
            return EngineResult(
                "unknown", {}, {}, None, None, engine_version=engine_version()
            )
        if status != cp_model.OPTIMAL:
            # A lexicographic phase cannot be frozen from an unproven value.
            return EngineResult(
                "unknown", {}, {}, None, None, engine_version=engine_version()
            )
        value = int(solver.Value(expression))
        fixed.append((objective["metric"], value))
        final_solver, final_selected = solver, selected

    if final_solver is None or final_selected is None:
        raise EngineExecutionError("solver produced no result")
    if time.monotonic() >= deadline:
        raise EngineDeadlineExceeded("solver deadline exhausted")
    selected_indices = [
        index for index, variable in enumerate(final_selected) if final_solver.Value(variable) == 1
    ]
    if len(selected_indices) != 1:
        raise EngineExecutionError("solver selected an invalid candidate count")
    candidate = candidates[selected_indices[0]]
    objective_values = {
        objective["metric"]: int(candidate["metrics"][objective["metric"]])
        for objective in objectives
    }
    assignments = {"candidate_id": candidate["candidate_id"], **candidate["assignment"]}
    return EngineResult(
        solve_outcome="optimal",
        assignments=assignments,
        objective_values=objective_values,
        best_bound=objective_values[objectives[0]["metric"]],
        gap="0",
        engine_version=engine_version(),
    )


__all__ = ["EngineResult", "ProblemValidationError", "engine_version", "solve_candidate_selection"]
