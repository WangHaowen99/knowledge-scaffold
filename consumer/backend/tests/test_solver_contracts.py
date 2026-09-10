from __future__ import annotations

import json
import math
import re
from decimal import Decimal
from pathlib import Path

import pytest
from solver_coordination import (
    Candidate,
    CandidateSet,
    Option,
    OrderedPlan,
    PlanItem,
    SolverContractError,
)
from solver_coordination.canonical import canonical_json_bytes, sha256_digest


def _item(item_order: int, *, quantity: str = "1200.500", duration: str = "2.50") -> PlanItem:
    return PlanItem(
        item_order=item_order,
        item_id="ITEM_SYNTHETIC_A",
        quantity=quantity,
        duration=duration,
    )


def _plan(*items: PlanItem) -> OrderedPlan:
    return OrderedPlan(
        items=items,
        option_domain=(Option.OPTION_0, Option.OPTION_1),
        max_transitions=1,
    )


def test_canonical_json_normalizes_unicode_recursively_before_hashing():
    composed = {"caf\u00e9": ["r\u00e9sum\u00e9", {"na\u00efve": "\u00e9"}]}
    decomposed = {"cafe\u0301": ["re\u0301sume\u0301", {"nai\u0308ve": "e\u0301"}]}

    assert canonical_json_bytes(composed) == canonical_json_bytes(decomposed)
    assert sha256_digest(composed) == sha256_digest(decomposed)


def test_digest_carries_an_explicit_algorithm_and_contract_version():
    digest = sha256_digest({"value": "synthetic"})

    assert re.fullmatch(r"sha256:solver-coordination\.v1:[0-9a-f]{64}", digest)


@pytest.mark.parametrize("value", [1.0, math.nan, math.inf, -math.inf])
def test_canonical_json_rejects_all_float_values(value):
    with pytest.raises(ValueError, match="float"):
        canonical_json_bytes({"value": value})


def test_canonical_json_rejects_non_string_mapping_keys():
    with pytest.raises(ValueError, match="mapping key"):
        canonical_json_bytes({1: "synthetic"})


def test_canonical_json_rejects_keys_that_collide_after_nfc_normalization():
    with pytest.raises(ValueError, match="collision"):
        canonical_json_bytes({"\u00e9": 1, "e\u0301": 2})


def test_canonicalization_contract_vectors_match_runtime_bytes():
    vectors_path = (
        Path(__file__).resolve().parents[3]
        / "contracts"
        / "solver-coordination"
        / "v1"
        / "canonicalization-vectors"
        / "basic.json"
    )
    vectors = json.loads(vectors_path.read_text(encoding="utf-8"))["vectors"]

    for vector in vectors:
        assert canonical_json_bytes(vector["input"]).decode("utf-8") == vector["canonical_utf8"]


def test_plan_item_normalizes_decimal_strings_without_float_conversion():
    item = _item(1)

    assert item.quantity == Decimal("1200.500")
    assert item.duration == Decimal("2.50")
    assert item.to_dict() == {
        "item_order": 1,
        "item_id": "ITEM_SYNTHETIC_A",
        "quantity": "1200.5",
        "duration": "2.5",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("quantity", "0"),
        ("quantity", "-1"),
        ("quantity", "not-a-number"),
        ("duration", "0"),
        ("duration", "NaN"),
    ],
)
def test_plan_item_rejects_non_positive_or_invalid_decimal_values(field, value):
    values = {
        "item_order": 1,
        "item_id": "ITEM_SYNTHETIC_A",
        "quantity": "100",
        "duration": "1",
    }
    values[field] = value

    with pytest.raises(SolverContractError, match=field):
        PlanItem(**values)


def test_ordered_plan_requires_at_least_one_item():
    with pytest.raises(SolverContractError, match="at least one item"):
        _plan()


@pytest.mark.parametrize(
    ("item_orders", "message"),
    [
        ((1, 1), "contiguous"),
        ((1, 3), "contiguous"),
        ((2, 1), "contiguous"),
        ((0, 1), "positive"),
    ],
)
def test_ordered_plan_rejects_duplicate_gapped_or_reordered_items(item_orders, message):
    with pytest.raises(SolverContractError, match=message):
        _plan(*(_item(number) for number in item_orders))


def test_candidate_contract_is_immutable_and_contains_no_optimization_claims():
    plan = _plan(_item(1))
    candidate = Candidate(
        candidate_id="ALL_0",
        assignments=(Option.OPTION_0,),
        transition_count=0,
    )
    candidate_set = CandidateSet.create(
        source_run_id="run_synthetic_001",
        problem_family="binary_ordered_route.v1",
        knowledge_package_id="knowledge_package_demo",
        knowledge_package_revision="package-revision-synthetic",
        plan=plan,
        candidates=(candidate,),
    )

    with pytest.raises(AttributeError):
        candidate.assignments += (Option.OPTION_1,)

    payload = candidate_set.to_dict()
    assert payload["authoritative"] is False
    assert payload["candidate_count"] == 1
    assert payload["problem_digest"].startswith("sha256:")
    assert payload["candidates"][0]["candidate_digest"].startswith("sha256:")
    assert not ({"profit", "recommended", "optimal", "rank"} & set(payload))
    assert not ({"profit", "recommended", "optimal", "rank"} & set(payload["candidates"][0]))


def test_candidate_digest_is_stable_while_container_identity_tracks_source_run():
    plan = _plan(_item(1))
    first = CandidateSet.create(
        source_run_id="run_synthetic_002",
        problem_family="binary_ordered_route.v1",
        knowledge_package_id="knowledge_package_demo",
        knowledge_package_revision="package-revision-synthetic",
        plan=plan,
        candidates=(Candidate("ALL_0", (Option.OPTION_0,), 0),),
    )
    second = CandidateSet.create(
        source_run_id="run_synthetic_003",
        problem_family="binary_ordered_route.v1",
        knowledge_package_id="knowledge_package_demo",
        knowledge_package_revision="package-revision-synthetic",
        plan=plan,
        candidates=(Candidate("ALL_0", (Option.OPTION_0,), 0),),
    )

    assert first.problem_digest == second.problem_digest
    assert first.candidate_set_digest != second.candidate_set_digest
    assert first.candidates[0].candidate_digest == second.candidates[0].candidate_digest


def test_candidate_digest_is_derived_from_assignment_not_display_identifier():
    plan = _plan(_item(1))
    first = CandidateSet.create(
        source_run_id="run_synthetic_010",
        problem_family="binary_ordered_route.v1",
        knowledge_package_id="knowledge_package_demo",
        knowledge_package_revision="package-revision-synthetic",
        plan=plan,
        candidates=(Candidate("DISPLAY_A", (Option.OPTION_0,), 0),),
    )
    second = CandidateSet.create(
        source_run_id="run_synthetic_011",
        problem_family="binary_ordered_route.v1",
        knowledge_package_id="knowledge_package_demo",
        knowledge_package_revision="package-revision-synthetic",
        plan=plan,
        candidates=(Candidate("DISPLAY_B", (Option.OPTION_0,), 0),),
    )

    assert first.candidates[0].candidate_digest == second.candidates[0].candidate_digest


def test_candidate_set_can_be_rehydrated_and_rejects_tampered_content():
    candidate_set = CandidateSet.create(
        source_run_id="run_synthetic_004",
        problem_family="binary_ordered_route.v1",
        knowledge_package_id="knowledge_package_demo",
        knowledge_package_revision="package-revision-synthetic",
        plan=_plan(_item(1)),
        candidates=(Candidate("ALL_0", (Option.OPTION_0,), 0),),
    )
    restored = CandidateSet.from_dict(candidate_set.to_dict())
    assert restored.to_dict() == candidate_set.to_dict()

    tampered = candidate_set.to_dict()
    tampered["candidates"][0]["assignments"] = ["OPTION_1"]
    with pytest.raises(SolverContractError, match="digest"):
        CandidateSet.from_dict(tampered)


def test_candidate_set_rehydration_rejects_non_object_items():
    candidate_set = CandidateSet.create(
        source_run_id="run_synthetic_005",
        problem_family="binary_ordered_route.v1",
        knowledge_package_id="knowledge_package_demo",
        knowledge_package_revision="package-revision-synthetic",
        plan=_plan(_item(1)),
        candidates=(Candidate("ALL_0", (Option.OPTION_0,), 0),),
    )
    payload = candidate_set.to_dict()
    payload["plan"]["items"] = ["not-an-object"]
    with pytest.raises(SolverContractError, match="item is invalid"):
        CandidateSet.from_dict(payload)


@pytest.mark.parametrize("scope", ["top", "plan", "item", "candidate"])
def test_candidate_set_rehydration_rejects_unknown_fields_at_every_schema_level(scope):
    candidate_set = CandidateSet.create(
        source_run_id="run_synthetic_schema_001",
        problem_family="binary_ordered_route.v1",
        knowledge_package_id="knowledge_package_demo",
        knowledge_package_revision="package-revision-synthetic",
        plan=_plan(_item(1)),
        candidates=(Candidate("ALL_0", (Option.OPTION_0,), 0),),
    )
    payload = candidate_set.to_dict()
    if scope == "top":
        payload["recommended"] = False
    elif scope == "plan":
        payload["plan"]["profit"] = "0"
    elif scope == "item":
        payload["plan"]["items"][0]["source_url"] = "synthetic"
    else:
        payload["candidates"][0]["rank"] = 1

    with pytest.raises(SolverContractError, match="unsupported fields"):
        CandidateSet.from_dict(payload)


@pytest.mark.parametrize("value", ["1e-13", "1e19", "12345678901234567890123456789"])
def test_plan_item_rejects_unsupported_decimal_precision(value):
    with pytest.raises(SolverContractError, match="decimal|precision|exponent"):
        _item(1, quantity=value)


@pytest.mark.parametrize("value", ["1e3", "+1", "1.", "1_0", " 1"])
def test_plan_item_rejects_non_schema_decimal_wire_forms(value):
    with pytest.raises(SolverContractError, match="decimal"):
        _item(1, quantity=value)
