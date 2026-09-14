# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from solver_coordination.contracts import NormalizedInput, SolverResult, VerificationResult
from solver_coordination.errors import SolverResultRejectedError
from solver_coordination.presentation import (
    OrderedBinaryRoutePresenter,
    ProfilePresentationService,
)
from solver_coordination.profiles import (
    ProfilePresentationPort,
    SolverProfileRegistration,
    SolverProfileRegistry,
)

DISPLAY_MAPPING = {
    "OPTION_0": "SYNTHETIC_ROUTE_A",
    "OPTION_1": "SYNTHETIC_ROUTE_B",
}


def _normalized_input() -> NormalizedInput:
    return NormalizedInput(
        operation_key="solve_synthetic_route",
        values={"items": [{"item_order": order, "item_id": f"ITEM_SYNTHETIC_{order:04d}"} for order in range(1, 5)]},
    )


_UNSET = object()


def _capability(*, mapping: Any = None, text_presentation: Any = _UNSET) -> dict[str, Any]:
    model = {
        "profile_id": "ordered_binary_route.v1",
        "display_mapping": DISPLAY_MAPPING if mapping is None else mapping,
    }
    if text_presentation is not _UNSET:
        model["text_presentation"] = text_presentation
    return {"decision_model": model}


def _solver_result(*, assignment: dict[str, Any] | None = None) -> SolverResult:
    return SolverResult(
        problem_id="sha256:" + "a" * 64,
        status="optimal",
        assignment=assignment
        or {
            "candidate_id": "OPTION_0_TO_OPTION_1_K0002",
            "ITEM_SYNTHETIC_0001": "OPTION_0",
            "ITEM_SYNTHETIC_0002": "OPTION_0",
            "ITEM_SYNTHETIC_0003": "OPTION_1",
            "ITEM_SYNTHETIC_0004": "OPTION_1",
        },
        objective_values={
            "primary_score": 1400,
            "transition_count": 1,
            "safety_margin": 500,
            "candidate_rank": 2,
        },
        diagnostics=(),
        engine={"name": "ortools-cp-sat", "version": "synthetic"},
    )


def _verification(*, status: str = "verified") -> VerificationResult:
    return VerificationResult(
        problem_id="sha256:" + "a" * 64,
        status=status,
        feasibility="feasible",
        optimality="proven_optimal",
        proof_kind="verifier_exhaustive_check",
        evidence=({"kind": "synthetic"},),
    )


def test_ordered_route_presenter_returns_a_stable_synthetic_summary():
    summary = OrderedBinaryRoutePresenter().present(
        capability=_capability(),
        normalized_input=_normalized_input(),
        solver_result=_solver_result(),
        verification=_verification(),
    )

    assert summary == {
        "candidate_id": "OPTION_0_TO_OPTION_1_K0002",
        "combination_no": 6,
        "assignment": {
            "ITEM_SYNTHETIC_0001": "OPTION_0",
            "ITEM_SYNTHETIC_0002": "OPTION_0",
            "ITEM_SYNTHETIC_0003": "OPTION_1",
            "ITEM_SYNTHETIC_0004": "OPTION_1",
        },
        "items": [
            {"item_id": "ITEM_SYNTHETIC_0001", "route": "SYNTHETIC_ROUTE_A"},
            {"item_id": "ITEM_SYNTHETIC_0002", "route": "SYNTHETIC_ROUTE_A"},
            {"item_id": "ITEM_SYNTHETIC_0003", "route": "SYNTHETIC_ROUTE_B"},
            {"item_id": "ITEM_SYNTHETIC_0004", "route": "SYNTHETIC_ROUTE_B"},
        ],
        "objective_values": dict(_solver_result().objective_values),
    }


def test_ordered_route_presenter_renders_declared_chinese_plan_text_verbatim():
    normalized_input = NormalizedInput(
        operation_key="solve_synthetic_route",
        values={
            "diesel_unit_no": "2#",
            "plan_month": "2099-03",
            "items": [
                {
                    "item_order": 1,
                    "item_id": "ITEM_SYNTHETIC_0001",
                    "crude_origin": "来源甲",
                    "quantity_wan_tons": "3.125",
                    "process_days": "1.25",
                },
                {
                    "item_order": 2,
                    "item_id": "ITEM_SYNTHETIC_0002",
                    "crude_origin": "来源乙",
                    "quantity_wan_tons": "8.250",
                    "process_days": "2.500",
                },
            ],
        },
    )
    solver_result = _solver_result(
        assignment={
            "candidate_id": "OPTION_0_TO_OPTION_1_K0001",
            "ITEM_SYNTHETIC_0001": "OPTION_0",
            "ITEM_SYNTHETIC_0002": "OPTION_1",
        }
    )

    summary = OrderedBinaryRoutePresenter().present(
        capability=_capability(
            text_presentation={
                "kind": "ordered_route_plan_zh.v1",
                "process_name": "合成加工装置",
                "stream_name": "合成中间物流",
            }
        ),
        normalized_input=normalized_input,
        solver_result=solver_result,
        verification=_verification(),
    )

    assert summary["title"] == "2#合成加工装置的3月度生产规划如下："
    assert summary["lines"] == [
        "第 1 批：来源甲，3.125 万吨，周期为 1.25 天，合成中间物流→SYNTHETIC_ROUTE_A；",
        "第 2 批：来源乙，8.250 万吨，周期为 2.5 天，合成中间物流→SYNTHETIC_ROUTE_B；",
    ]
    assert summary["text"] == "  \n".join([summary["title"], *summary["lines"]])


@pytest.mark.parametrize(
    "configuration",
    [
        {},
        {
            "kind": "ordered_route_plan_zh.v1",
            "process_name": "合成加工装置",
        },
        {
            "kind": "ordered_route_plan_zh.v1",
            "process_name": "合成加工装置",
            "stream_name": "合成中间物流",
            "template": "unsafe",
        },
        {
            "kind": "other.v1",
            "process_name": "合成加工装置",
            "stream_name": "合成中间物流",
        },
    ],
)
def test_ordered_route_presenter_rejects_invalid_text_presentation(configuration):
    with pytest.raises(SolverResultRejectedError, match="text presentation"):
        OrderedBinaryRoutePresenter().present(
            capability=_capability(text_presentation=configuration),
            normalized_input=_normalized_input(),
            solver_result=_solver_result(),
            verification=_verification(),
        )


@pytest.mark.parametrize(
    "mapping",
    [
        {"OPTION_0": "SYNTHETIC_ROUTE_A"},
        {
            "OPTION_0": "SYNTHETIC_ROUTE_A",
            "OPTION_1": "SYNTHETIC_ROUTE_B",
            "OPTION_2": "SYNTHETIC_ROUTE_C",
        },
    ],
)
def test_ordered_route_presenter_rejects_missing_or_extra_mapping_keys(mapping):
    with pytest.raises(SolverResultRejectedError, match="display mapping"):
        OrderedBinaryRoutePresenter().present(
            capability=_capability(mapping=mapping),
            normalized_input=_normalized_input(),
            solver_result=_solver_result(),
            verification=_verification(),
        )


def test_ordered_route_presenter_rejects_an_assignment_item_not_in_input():
    assignment = dict(_solver_result().assignment)
    del assignment["ITEM_SYNTHETIC_0004"]
    assignment["UNKNOWN_SYNTHETIC_ITEM"] = "OPTION_1"

    with pytest.raises(SolverResultRejectedError, match="item"):
        OrderedBinaryRoutePresenter().present(
            capability=_capability(),
            normalized_input=_normalized_input(),
            solver_result=_solver_result(assignment=assignment),
            verification=_verification(),
        )


@pytest.mark.parametrize("status", ["unproven", "rejected"])
def test_ordered_route_presenter_requires_verified_result(status: str):
    with pytest.raises(SolverResultRejectedError, match="verified"):
        OrderedBinaryRoutePresenter().present(
            capability=_capability(),
            normalized_input=_normalized_input(),
            solver_result=_solver_result(),
            verification=_verification(status=status),
        )


@dataclass
class _Registration:
    presenter: ProfilePresentationPort
    fact_provider: Any = None
    compiler: Any = None
    verifier: Any = None


def test_profile_presentation_service_dispatches_by_locked_profile():
    presenter = OrderedBinaryRoutePresenter()
    registration = SolverProfileRegistration(
        profile_id="ordered_binary_route.v1",
        fact_provider=type("Facts", (), {"resolve_facts": lambda self, **kwargs: {}})(),
        compiler=type("Compiler", (), {"compile_problem": lambda self, **kwargs: {}})(),
        verifier=type("Verifier", (), {"verify": lambda self, **kwargs: _verification()})(),
        presenter=presenter,
    )
    service = ProfilePresentationService(SolverProfileRegistry([registration]))

    summary = service.present(
        capability=_capability(),
        normalized_input=_normalized_input(),
        solver_result=_solver_result(),
        verification=_verification(),
    )

    assert summary["candidate_id"] == "OPTION_0_TO_OPTION_1_K0002"
    assert summary["combination_no"] == 6


def test_profile_presentation_service_fails_closed_for_unknown_profile():
    service = ProfilePresentationService(SolverProfileRegistry())

    with pytest.raises(SolverResultRejectedError, match="profile"):
        service.present(
            capability=_capability(),
            normalized_input=_normalized_input(),
            solver_result=_solver_result(),
            verification=_verification(),
        )
