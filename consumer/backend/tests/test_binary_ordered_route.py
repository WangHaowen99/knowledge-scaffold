from __future__ import annotations

import pytest
from solver_coordination import Option, OrderedPlan, PlanItem, SolverContractError
from solver_coordination.problem_families.binary_ordered_route import (
    MAX_PLAN_ITEMS,
    enumerate_ordered_routes,
    expected_candidate_ids,
)


def _plan(item_count: int = 9) -> OrderedPlan:
    return OrderedPlan(
        items=tuple(
            PlanItem(
                item_order=index,
                item_id="ITEM_SYNTHETIC_{}".format(index),
                quantity=str(1000 + index * 100),
                duration=str(index),
            )
            for index in range(1, item_count + 1)
        ),
        option_domain=(Option.OPTION_0, Option.OPTION_1),
        max_transitions=1,
    )


def _transition_count(assignments: tuple[Option, ...]) -> int:
    return sum(left != right for left, right in zip(assignments, assignments[1:]))


def test_nine_items_generate_eighteen_stable_neutral_candidate_ids_and_order():
    candidate_set = enumerate_ordered_routes(
        _plan(),
        source_run_id="run_synthetic_001",
        knowledge_package_id="knowledge_package_demo",
        knowledge_package_revision="package-revision-synthetic",
    )

    assert [candidate.candidate_id for candidate in candidate_set.candidates] == list(expected_candidate_ids(9))
    assert candidate_set.candidate_count == 18
    assert all(len(candidate.assignments) == 9 for candidate in candidate_set.candidates)
    assert all(_transition_count(candidate.assignments) <= 1 for candidate in candidate_set.candidates)


def test_candidate_assignments_match_the_transition_identifier():
    candidate_set = enumerate_ordered_routes(
        _plan(4),
        source_run_id="run_synthetic_002",
        knowledge_package_id="knowledge_package_demo",
        knowledge_package_revision="package-revision-synthetic",
    )
    by_id = {candidate.candidate_id: candidate for candidate in candidate_set.candidates}

    assert by_id["ALL_0"].assignments == (Option.OPTION_0,) * 4
    assert by_id["ALL_1"].assignments == (Option.OPTION_1,) * 4
    assert by_id["OPTION_0_TO_OPTION_1_K0002"].assignments == (
        Option.OPTION_0,
        Option.OPTION_0,
        Option.OPTION_1,
        Option.OPTION_1,
    )
    assert by_id["OPTION_1_TO_OPTION_0_K0003"].assignments == (
        Option.OPTION_1,
        Option.OPTION_1,
        Option.OPTION_1,
        Option.OPTION_0,
    )


def test_one_item_generates_only_the_two_constant_candidates():
    candidate_set = enumerate_ordered_routes(
        _plan(1),
        source_run_id="run_synthetic_003",
        knowledge_package_id="knowledge_package_demo",
        knowledge_package_revision="package-revision-synthetic",
    )

    assert [candidate.candidate_id for candidate in candidate_set.candidates] == [
        "ALL_0",
        "ALL_1",
    ]
    assert candidate_set.candidate_count == 2


def test_repeated_enumeration_has_stable_problem_and_candidate_set_digests():
    plan = _plan()
    runs = [
        enumerate_ordered_routes(
            plan,
            source_run_id="run_synthetic_004_{}".format(index),
            knowledge_package_id="knowledge_package_demo",
            knowledge_package_revision="package-revision-synthetic",
        )
        for index in range(3)
    ]

    assert len({result.problem_digest for result in runs}) == 1
    assert all(result.problem_digest.startswith("sha256:") for result in runs)
    assert all(result.candidate_set_digest.startswith("sha256:") for result in runs)
    assert len({tuple(c.candidate_digest for c in result.candidates) for result in runs}) == 1
    assert len({result.candidate_set_digest for result in runs}) == 3


def test_candidate_set_container_digest_tracks_source_run_lineage():
    plan = _plan()
    first = enumerate_ordered_routes(
        plan,
        source_run_id="run_synthetic_005",
        knowledge_package_id="knowledge_package_demo",
        knowledge_package_revision="package-revision-synthetic",
    )
    second = enumerate_ordered_routes(
        plan,
        source_run_id="run_synthetic_006",
        knowledge_package_id="knowledge_package_demo",
        knowledge_package_revision="package-revision-synthetic",
    )

    assert first.problem_digest == second.problem_digest
    assert first.candidate_set_digest != second.candidate_set_digest


def test_candidate_set_digest_changes_when_the_package_revision_changes():
    plan = _plan()
    first = enumerate_ordered_routes(
        plan,
        source_run_id="run_synthetic_007",
        knowledge_package_id="knowledge_package_demo",
        knowledge_package_revision="package-revision-synthetic-1",
    )
    second = enumerate_ordered_routes(
        plan,
        source_run_id="run_synthetic_008",
        knowledge_package_id="knowledge_package_demo",
        knowledge_package_revision="package-revision-synthetic-2",
    )

    assert first.problem_digest != second.problem_digest
    assert first.candidate_set_digest != second.candidate_set_digest


def test_enumerator_rejects_an_empty_source_run_id():
    try:
        enumerate_ordered_routes(
            _plan(),
            source_run_id="",
            knowledge_package_id="knowledge_package_demo",
            knowledge_package_revision="package-revision-synthetic",
        )
    except SolverContractError as exc:
        assert "source_run_id" in str(exc)
    else:
        raise AssertionError("expected empty source_run_id to be rejected")


def test_ordered_route_rejects_item_count_before_candidate_materialization():
    items = tuple(
        PlanItem(
            item_order=index,
            item_id="ITEM_{:03d}".format(index),
            quantity="1",
            duration="1",
        )
        for index in range(1, MAX_PLAN_ITEMS + 2)
    )
    with pytest.raises(SolverContractError, match="at most"):
        OrderedPlan(
            items=items,
            option_domain=(Option.OPTION_0, Option.OPTION_1),
            max_transitions=1,
        )
