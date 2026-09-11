"""Verifier-backed candidate results prepared for deterministic presentation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Any

from .canonical import sha256_digest
from .contracts import (
    ProblemIR,
    SolverContractError,
    SolverResult,
    VerificationResult,
    _freeze_array,
    _freeze_object,
    _required_text,
    _thaw_json,
)


CANDIDATE_RESULTS_SCHEMA_VERSION = "solver.candidate-results.v1"
_SOURCE_KIND = "problem_ir.candidate_selection"
_PRESENTATION_REVISION = "ordered_binary_route.presentation.v1"
_OPTIONS = ("OPTION_0", "OPTION_1")
_SWITCH_ID_RE = re.compile(
    r"^(OPTION_[01])_TO_(OPTION_[01])_K(?P<switch>[0-9]{4})$"
)
_DOMAIN_PROFILE_ID = "refinery_route.v1"
_METRIC_DISPLAY_FIELDS = {"key", "label", "unit", "scale", "sense"}
_METRIC_SENSES = {"maximize", "minimize"}
# The refinery route fact worker reports profit in CNY and the tightest capacity
# headroom in percentage points; the ordered binary route profile then stores both
# as integers multiplied by domain_evaluation.metric_scale. Presentation needs the
# scale and the unit to turn those integers back into business numbers, so the
# artifact carries them instead of letting each reader rediscover the convention.
_REFINERY_ROUTE_METRIC_DISPLAY = (
    ("primary_score", "利润", "元", "maximize"),
    ("safety_margin", "最小余量", "%", "maximize"),
)
_FIELDS = {
    "schema_version",
    "problem_digest",
    "candidate_source_kind",
    "candidate_source_digest",
    "presentation_revision",
    "item_ids",
    "option_labels",
    "candidate_count",
    "selected_candidate_id",
    "candidates",
    "metric_display",
}
# Artifacts persisted before metric display existed omit the field entirely; they
# must keep loading, and to_dict must keep reproducing their stored digest.
_REQUIRED_FIELDS = _FIELDS - {"metric_display"}
_CANDIDATE_FIELDS = {
    "combination_no",
    "candidate_id",
    "assignment",
    "eligible",
    "validation_status",
    "metrics",
    "rejection_reasons",
    "selected",
}


@dataclass(frozen=True)
class CandidateResultsArtifact:
    problem_digest: str
    candidate_source_kind: str
    candidate_source_digest: str
    presentation_revision: str
    item_ids: tuple[str, ...]
    option_labels: Mapping[str, str]
    candidate_count: int
    selected_candidate_id: str | None
    candidates: tuple[Mapping[str, Any], ...]
    schema_version: str = CANDIDATE_RESULTS_SCHEMA_VERSION
    metric_display: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != CANDIDATE_RESULTS_SCHEMA_VERSION:
            raise SolverContractError("candidate results schema version is unsupported")
        for field_name in (
            "problem_digest",
            "candidate_source_kind",
            "candidate_source_digest",
            "presentation_revision",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_text(getattr(self, field_name), field_name),
            )
        if self.candidate_source_kind != _SOURCE_KIND:
            raise SolverContractError("candidate results source kind is unsupported")
        if self.presentation_revision != _PRESENTATION_REVISION:
            raise SolverContractError("candidate results presentation revision is unsupported")
        item_ids = tuple(_required_text(item_id, "item_id") for item_id in self.item_ids)
        if not item_ids or len(set(item_ids)) != len(item_ids):
            raise SolverContractError("candidate results item IDs are invalid")
        object.__setattr__(self, "item_ids", item_ids)
        labels = _freeze_object(self.option_labels, "option_labels")
        if set(labels) != set(_OPTIONS) or any(
            not isinstance(labels[option], str) or not labels[option].strip()
            for option in _OPTIONS
        ):
            raise SolverContractError("candidate results option labels are invalid")
        object.__setattr__(self, "option_labels", labels)
        if (
            isinstance(self.candidate_count, bool)
            or not isinstance(self.candidate_count, int)
            or self.candidate_count < 1
        ):
            raise SolverContractError("candidate results count is invalid")
        candidates = _freeze_array(self.candidates, "candidates")
        if len(candidates) != self.candidate_count:
            raise SolverContractError("candidate results count does not match content")
        if any(not isinstance(candidate, Mapping) for candidate in candidates):
            raise SolverContractError("candidate result is invalid")
        object.__setattr__(self, "candidates", candidates)
        selected_id = self.selected_candidate_id
        if selected_id is not None:
            selected_id = _required_text(selected_id, "selected_candidate_id")
        object.__setattr__(self, "selected_candidate_id", selected_id)
        object.__setattr__(
            self, "metric_display", _freeze_array(self.metric_display, "metric_display")
        )
        self._validate_candidates()
        self._validate_metric_display()

    def _validate_metric_display(self) -> None:
        keys: list[str] = []
        for descriptor in self.metric_display:
            if (
                not isinstance(descriptor, Mapping)
                or set(descriptor) != _METRIC_DISPLAY_FIELDS
            ):
                raise SolverContractError("candidate metric display fields are invalid")
            key = _required_text(descriptor.get("key"), "metric_display.key")
            _required_text(descriptor.get("label"), "metric_display.label")
            _required_text(descriptor.get("unit"), "metric_display.unit")
            scale = descriptor.get("scale")
            if isinstance(scale, bool) or not isinstance(scale, int) or scale < 1:
                raise SolverContractError("candidate metric display scale is invalid")
            if descriptor.get("sense") not in _METRIC_SENSES:
                raise SolverContractError("candidate metric display sense is invalid")
            keys.append(key)
        if len(set(keys)) != len(keys):
            raise SolverContractError("candidate metric display keys are duplicated")
        # A described metric that some candidate lacks would render as a blank cell,
        # so require every row to carry an integer for it.
        for candidate in self.candidates:
            metrics = candidate["metrics"]
            for key in keys:
                value = metrics.get(key)
                if isinstance(value, bool) or not isinstance(value, int):
                    raise SolverContractError(
                        "candidate metric display value is missing"
                    )

    def _validate_candidates(self) -> None:
        numbers: list[int] = []
        candidate_ids: list[str] = []
        ranks: list[int] = []
        selected: list[Mapping[str, Any]] = []
        source_candidates: list[Mapping[str, Any]] = []
        for candidate in self.candidates:
            if set(candidate) != _CANDIDATE_FIELDS:
                raise SolverContractError("candidate result fields are invalid")
            number = candidate.get("combination_no")
            if isinstance(number, bool) or not isinstance(number, int):
                raise SolverContractError("candidate combination number is invalid")
            numbers.append(number)
            candidate_id = _required_text(candidate.get("candidate_id"), "candidate_id")
            candidate_ids.append(candidate_id)
            assignment = tuple(candidate.get("assignment") or ())
            if len(assignment) != len(self.item_ids) or any(
                option not in _OPTIONS for option in assignment
            ):
                raise SolverContractError("candidate result assignment is invalid")
            eligible = candidate.get("eligible")
            if not isinstance(eligible, bool):
                raise SolverContractError("candidate result eligibility is invalid")
            expected_status = "feasible" if eligible else "infeasible"
            if candidate.get("validation_status") != expected_status:
                raise SolverContractError("candidate validation status is inconsistent")
            metrics = candidate.get("metrics")
            if not isinstance(metrics, Mapping):
                raise SolverContractError("candidate metrics are invalid")
            rank = metrics.get("candidate_rank")
            if isinstance(rank, bool) or not isinstance(rank, int) or rank < 0:
                raise SolverContractError("candidate rank is invalid")
            ranks.append(rank)
            reasons = tuple(candidate.get("rejection_reasons") or ())
            expected_reasons = () if eligible else ("constraint_violation",)
            if reasons != expected_reasons:
                raise SolverContractError("candidate rejection reasons are inconsistent")
            if not isinstance(candidate.get("selected"), bool):
                raise SolverContractError("candidate selected flag is invalid")
            if candidate["selected"]:
                selected.append(candidate)
            source_candidates.append(
                {
                    "candidate_id": candidate_id,
                    "eligible": eligible,
                    "assignment": dict(zip(self.item_ids, assignment)),
                    "metrics": _thaw_json(metrics),
                }
            )
        if numbers != list(range(1, self.candidate_count + 1)):
            raise SolverContractError("candidate combination numbers are invalid")
        if len(set(candidate_ids)) != self.candidate_count:
            raise SolverContractError("candidate IDs are duplicated")
        if sorted(ranks) != list(range(self.candidate_count)):
            raise SolverContractError("candidate ranks are invalid")
        if self.selected_candidate_id is None:
            if selected:
                raise SolverContractError("infeasible candidate results select a candidate")
        elif (
            len(selected) != 1
            or selected[0]["candidate_id"] != self.selected_candidate_id
        ):
            raise SolverContractError("selected candidate does not match candidate results")
        source_candidates.sort(key=lambda candidate: candidate["metrics"]["candidate_rank"])
        if sha256_digest(source_candidates) != self.candidate_source_digest:
            raise SolverContractError("candidate source digest does not match content")

    @property
    def digest(self) -> str:
        return sha256_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "problem_digest": self.problem_digest,
            "candidate_source_kind": self.candidate_source_kind,
            "candidate_source_digest": self.candidate_source_digest,
            "presentation_revision": self.presentation_revision,
            "item_ids": list(self.item_ids),
            "option_labels": _thaw_json(self.option_labels),
            "candidate_count": self.candidate_count,
            "selected_candidate_id": self.selected_candidate_id,
            "candidates": _thaw_json(self.candidates),
        }
        if self.metric_display:
            payload["metric_display"] = _thaw_json(self.metric_display)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CandidateResultsArtifact":
        if not isinstance(payload, Mapping) or not _REQUIRED_FIELDS <= set(payload) <= _FIELDS:
            raise SolverContractError("candidate results artifact fields are invalid")
        raw_candidates = payload.get("candidates")
        if not isinstance(raw_candidates, (list, tuple)):
            raise SolverContractError("candidate results candidates are invalid")
        raw_display = payload.get("metric_display", ())
        if not isinstance(raw_display, (list, tuple)):
            raise SolverContractError("candidate metric display is invalid")
        return cls(
            metric_display=tuple(raw_display),
            schema_version=payload.get("schema_version"),
            problem_digest=payload.get("problem_digest"),
            candidate_source_kind=payload.get("candidate_source_kind"),
            candidate_source_digest=payload.get("candidate_source_digest"),
            presentation_revision=payload.get("presentation_revision"),
            item_ids=tuple(payload.get("item_ids") or ()),
            option_labels=payload.get("option_labels"),
            candidate_count=payload.get("candidate_count"),
            selected_candidate_id=payload.get("selected_candidate_id"),
            candidates=tuple(raw_candidates),
        )


def build_candidate_results(
    *,
    problem: ProblemIR,
    capability: Any,
    solver_result: SolverResult,
    verification: VerificationResult,
) -> CandidateResultsArtifact:
    if not isinstance(problem, ProblemIR):
        raise SolverContractError("candidate results require ProblemIR")
    if not isinstance(solver_result, SolverResult):
        raise SolverContractError("candidate results require SolverResult")
    if not isinstance(verification, VerificationResult):
        raise SolverContractError("candidate results require VerificationResult")
    if verification.status != "verified":
        raise SolverContractError("candidate results require a verified result")
    if (
        solver_result.problem_id != problem.problem_id
        or verification.problem_id != problem.problem_id
    ):
        raise SolverContractError("candidate results problem identity diverged")
    selection = problem.parameters.get("candidate_selection")
    if not isinstance(selection, Mapping):
        raise SolverContractError("candidate selection is missing")
    if selection.get("profile_id") != "ordered_binary_route.v1":
        raise SolverContractError("candidate results profile is unsupported")
    raw_candidates = selection.get("candidates")
    count = selection.get("candidate_count")
    source_digest = selection.get("candidate_evaluation_digest")
    if not isinstance(raw_candidates, (list, tuple)):
        raise SolverContractError("candidate selection digest is invalid")
    if isinstance(count, bool) or not isinstance(count, int):
        raise SolverContractError("candidate selection digest is invalid")
    if count != len(raw_candidates):
        raise SolverContractError("candidate selection digest is invalid")
    if not isinstance(source_digest, str):
        raise SolverContractError("candidate selection digest is invalid")
    if sha256_digest(raw_candidates) != source_digest:
        raise SolverContractError("candidate selection digest is invalid")
    evidence = None
    for item in verification.evidence:
        if not isinstance(item, Mapping):
            continue
        if item.get("kind") == "verifier_exhaustive_check":
            evidence = item
            break
    if (
        not isinstance(evidence, Mapping)
        or evidence.get("candidate_digest") != source_digest
        or evidence.get("candidate_count") != count
    ):
        raise SolverContractError("verification candidate digest is invalid")
    item_ids = _item_ids(raw_candidates)
    model = (
        capability.get("decision_model")
        if isinstance(capability, Mapping)
        else getattr(capability, "decision_model", None)
    )
    mapping = model.get("display_mapping") if isinstance(model, Mapping) else None
    if not isinstance(mapping, Mapping) or set(mapping) != set(_OPTIONS):
        raise SolverContractError("candidate display mapping is invalid")
    labels = {}
    for option in _OPTIONS:
        labels[option] = _required_text(mapping[option], option)
    selected_id = None
    if verification.feasibility == "feasible":
        selected_id = _required_text(
            solver_result.assignment.get("candidate_id"), "selected_candidate_id"
        )
        if evidence.get("selected_candidate_id") != selected_id:
            raise SolverContractError("verification selected candidate diverged")
    elif verification.feasibility != "infeasible":
        raise SolverContractError("verification feasibility is unsupported")

    rows = []
    for candidate in raw_candidates:
        if not isinstance(candidate, Mapping) or set(candidate) != {
            "candidate_id",
            "eligible",
            "assignment",
            "metrics",
        }:
            raise SolverContractError("candidate selection item is invalid")
        candidate_id = _required_text(candidate.get("candidate_id"), "candidate_id")
        assignment = candidate.get("assignment")
        if not isinstance(assignment, Mapping) or tuple(assignment) != item_ids:
            raise SolverContractError("candidate assignment is invalid")
        eligible = candidate.get("eligible")
        if not isinstance(eligible, bool):
            raise SolverContractError("candidate eligibility is invalid")
        rows.append(
            {
                "combination_no": ordered_binary_combination_no(
                    candidate_id, len(item_ids)
                ),
                "candidate_id": candidate_id,
                "assignment": list(assignment.values()),
                "eligible": eligible,
                "validation_status": "feasible" if eligible else "infeasible",
                "metrics": dict(candidate.get("metrics") or {}),
                "rejection_reasons": [] if eligible else ["constraint_violation"],
                "selected": candidate_id == selected_id,
            }
        )
    rows.sort(key=lambda row: row["combination_no"])
    return CandidateResultsArtifact(
        problem_digest=problem.problem_id,
        candidate_source_kind=_SOURCE_KIND,
        candidate_source_digest=source_digest,
        presentation_revision=_PRESENTATION_REVISION,
        item_ids=item_ids,
        option_labels=labels,
        candidate_count=count,
        selected_candidate_id=selected_id,
        candidates=tuple(rows),
        metric_display=_metric_display(problem),
    )


def _metric_display(problem: ProblemIR) -> tuple[Mapping[str, Any], ...]:
    context = problem.parameters.get("domain_evaluation")
    if not isinstance(context, Mapping) or context.get("profile_id") != _DOMAIN_PROFILE_ID:
        return ()
    scale = context.get("metric_scale")
    if isinstance(scale, bool) or not isinstance(scale, int) or scale < 1:
        raise SolverContractError("candidate metric display scale is invalid")
    return tuple(
        {"key": key, "label": label, "unit": unit, "scale": scale, "sense": sense}
        for key, label, unit, sense in _REFINERY_ROUTE_METRIC_DISPLAY
    )


def ordered_binary_combination_no(candidate_id: str, item_count: int) -> int:
    if candidate_id == "ALL_1":
        return 1
    if candidate_id == "ALL_0":
        return 2
    matched = _SWITCH_ID_RE.fullmatch(candidate_id)
    if matched is None:
        raise SolverContractError("candidate ID is unsupported")
    left, right = matched.group(1), matched.group(2)
    switch = int(matched.group("switch"))
    if left == right or not 1 <= switch < item_count:
        raise SolverContractError("candidate switch point is invalid")
    return 2 * switch + (1 if (left, right) == ("OPTION_1", "OPTION_0") else 2)


def _item_ids(candidates: tuple[Any, ...] | list[Any]) -> tuple[str, ...]:
    if not candidates or not isinstance(candidates[0], Mapping):
        raise SolverContractError("candidate selection is empty")
    assignment = candidates[0].get("assignment")
    if not isinstance(assignment, Mapping) or not assignment:
        raise SolverContractError("candidate assignment is invalid")
    item_ids = tuple(_required_text(item_id, "item_id") for item_id in assignment)
    if len(set(item_ids)) != len(item_ids):
        raise SolverContractError("candidate assignment item IDs are duplicated")
    return item_ids


__all__ = [
    "CANDIDATE_RESULTS_SCHEMA_VERSION",
    "CandidateResultsArtifact",
    "build_candidate_results",
    "ordered_binary_combination_no",
]
