from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from solver_coordination.errors import SolverResultRejectedError
from solver_coordination.result_schema import SolverAssignmentSchemaValidator


ASSIGNMENT_REJECTED_DETAIL = (
    "solver assignment does not match the locked result schema"
)
SCHEMA_REJECTED_DETAIL = "locked solver result schema is invalid"


def _assignment_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "candidate_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
            }
        },
        "required": ["candidate_id"],
        "additionalProperties": {
            "type": "string",
            "enum": ["OPTION_0", "OPTION_1"],
        },
        "minProperties": 2,
        "maxProperties": 65,
    }


def _assert_assignment_rejected(assignment: object) -> None:
    validator = SolverAssignmentSchemaValidator(schema=_assignment_schema())

    with pytest.raises(SolverResultRejectedError) as exc_info:
        validator.validate(assignment)

    assert exc_info.value.code == "solver_result_rejected"
    assert exc_info.value.detail == ASSIGNMENT_REJECTED_DETAIL


def test_validator_accepts_candidate_id_and_dynamic_batch_keys():
    assignment = {
        "candidate_id": "OPTION_0_TO_OPTION_1_K0004",
        "batch_0001": "OPTION_0",
        "batch_0002": "OPTION_1",
    }
    validator = SolverAssignmentSchemaValidator(schema=_assignment_schema())

    validated = validator.validate(assignment)

    assert validated == assignment
    assert validated is not assignment


def test_validator_rejects_an_option_outside_the_locked_domain():
    _assert_assignment_rejected(
        {
            "candidate_id": "OPTION_0_TO_OPTION_1_K0004",
            "batch_0001": "UNDECLARED_OPTION",
        }
    )


def test_validator_rejects_an_empty_candidate_id():
    _assert_assignment_rejected(
        {
            "candidate_id": "",
            "batch_0001": "OPTION_0",
        }
    )


def test_validator_rejects_more_than_65_total_properties():
    assignment = {
        "candidate_id": "OPTION_0_TO_OPTION_1_K0065",
        **{f"batch_{index:04d}": "OPTION_0" for index in range(1, 66)},
    }

    assert len(assignment) == 66
    _assert_assignment_rejected(assignment)


def test_validator_rejects_a_non_object_assignment():
    _assert_assignment_rejected(
        ["OPTION_0_TO_OPTION_1_K0004", "OPTION_0"]
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("$ref", "#"),
        ("$dynamicRef", "https://solver.example.test/assignment.schema.json"),
        ("$id", "https://solver.example.test/assignment.schema.json"),
    ],
)
def test_validator_rejects_reference_bearing_or_remote_schemas(field, value):
    schema = deepcopy(_assignment_schema())
    schema[field] = value

    with pytest.raises(SolverResultRejectedError) as exc_info:
        SolverAssignmentSchemaValidator(schema=schema)

    assert exc_info.value.code == "solver_result_rejected"
    assert exc_info.value.detail == SCHEMA_REJECTED_DETAIL
