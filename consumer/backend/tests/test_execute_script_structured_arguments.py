# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from __future__ import annotations

import json

from agent.direct_execution.arguments import build_direct_cli_args
from agent.direct_execution.models import RuntimeContext
from agent.tools.execute_script import (
    _build_cli_args,
    _validate_structured_arguments_against_request,
)

PARAMETERS = [
    {
        "name": "batches",
        "flag": "--batches",
        "type": "array",
        "required": True,
        "multiple": False,
    },
    {
        "name": "options",
        "flag": "--options",
        "type": "object",
        "required": False,
        "multiple": False,
    },
]


def test_agent_loop_serializes_array_and_object_parameters_as_json():
    args, missing, invalid = _build_cli_args(
        script_args=[],
        script_parameters=PARAMETERS,
        arguments={
            "batches": [{"recordName": "sample-a", "quantity": 2.574}],
            "options": {"period": "period-3"},
        },
    )

    assert missing == []
    assert invalid == []
    assert args[0] == "--batches"
    assert json.loads(args[1]) == [{"recordName": "sample-a", "quantity": 2.574}]
    assert args[2] == "--options"
    assert json.loads(args[3]) == {"period": "period-3"}


def test_direct_execution_uses_the_same_structured_parameter_contract():
    args, missing, error = build_direct_cli_args(
        script_parameters=PARAMETERS,
        business_arguments={"batches": [{"batch": 1}]},
        runtime_context=RuntimeContext(
            session_id="session-1",
            run_id="run-1",
            turn_id=1,
            user_id="user-1",
            workspace_id="workspace-1",
            selected_ontology_id="demo",
        ),
    )

    assert missing == []
    assert error is None
    assert args[0] == "--batches"
    assert json.loads(args[1]) == [{"batch": 1}]


def test_array_parameter_rejects_non_array_json():
    _args, _missing, invalid = _build_cli_args(
        script_args=[],
        script_parameters=PARAMETERS,
        arguments={"batches": '{"not": "an array"}'},
    )

    assert invalid == ["batches: expected array"]


def test_agent_loop_serializes_structured_values_for_legacy_string_parameters():
    legacy_parameters = [
        {
            "name": "monthlySchedule",
            "flag": "--monthlySchedule",
            "type": "string",
            "required": True,
            "multiple": False,
        },
    ]

    args, missing, invalid = _build_cli_args(
        script_args=[],
        script_parameters=legacy_parameters,
        arguments={"monthlySchedule": [{"batchSequence": 1, "quantity": 25.74}]},
    )

    assert missing == []
    assert invalid == []
    assert args[0] == "--monthlySchedule"
    assert json.loads(args[1]) == [{"batchSequence": 1, "quantity": 25.74}]


def test_direct_execution_serializes_structured_values_for_legacy_string_parameters():
    legacy_parameters = [
        {
            "name": "monthlySchedule",
            "flag": "--monthlySchedule",
            "type": "string",
            "required": True,
            "multiple": False,
        },
    ]

    args, missing, error = build_direct_cli_args(
        script_parameters=legacy_parameters,
        business_arguments={"monthlySchedule": [{"batchSequence": 1}]},
        runtime_context=RuntimeContext(
            session_id="session-1",
            run_id="run-1",
            turn_id=1,
            user_id="user-1",
            workspace_id="workspace-1",
            selected_ontology_id="demo",
        ),
    )

    assert missing == []
    assert error is None
    assert json.loads(args[1]) == [{"batchSequence": 1}]


def test_structured_collection_matches_current_request_items_in_order():
    request = (
        "Record 1: sample-a, quantity 12.5, category red;\n"
        "Record 2: sample-b, quantity 7.25, category blue;\n"
        "Record 3: sample-c, quantity 4.75, category green."
    )
    arguments = {
        "batches": [
            {"name": "sample-a", "quantity": 12.5, "category": "red"},
            {"name": "sample-b", "quantity": 7.25, "category": "blue"},
            {"name": "sample-c", "quantity": 4.75, "category": "green"},
        ],
    }

    assert _validate_structured_arguments_against_request(arguments, request) == []


def test_structured_collection_rejects_values_paired_from_different_request_items():
    request = (
        "Record 1: sample-a, quantity 12.5, category red;\n"
        "Record 2: sample-b, quantity 7.25, category blue;\n"
        "Record 3: sample-c, quantity 4.75, category green."
    )
    arguments = {
        "batches": [
            {"name": "sample-a", "quantity": 12.5, "category": "red"},
            {"name": "sample-b", "quantity": 7.25, "category": "red"},
            {"name": "sample-c", "quantity": 4.75, "category": "green"},
        ],
    }

    errors = _validate_structured_arguments_against_request(arguments, request)
    assert len(errors) == 1
    assert "batches" in errors[0]
    assert "item 2" in errors[0]
    assert "Record 2: sample-b, quantity 7.25, category blue" in errors[0]


def test_serialized_json_object_enters_collection_consistency_validation():
    request = "Record 1: sample-a, quantity 12.5, category red;\n" "Record 2: sample-b, quantity 7.25, category blue."
    arguments = {
        "payload_json": json.dumps(
            {
                "batches": [
                    {"name": "sample-a", "quantity": 12.5, "category": "red"},
                    {"name": "sample-b", "quantity": 7.25, "category": "red"},
                ]
            }
        )
    }

    errors = _validate_structured_arguments_against_request(arguments, request)

    assert len(errors) == 1
    assert "arguments.payload_json.batches" in errors[0]
    assert "item 2" in errors[0]


def test_structured_identity_value_must_be_grounded_in_current_request():
    arguments = {
        "payload_json": json.dumps(
            {
                "unit_no": "unit-02",
                "batches": [{"batch_no": "generated-1", "quantity": 12.5}],
            }
        )
    }

    errors = _validate_structured_arguments_against_request(
        arguments,
        "Run the plan with unit-01 and quantity 12.5.",
    )

    assert errors == [
        "arguments.payload_json.unit_no identity value is not grounded in the current request or current tool evidence"
    ]


def test_structured_identity_value_can_come_from_current_business_tool_evidence():
    arguments = {"payload_json": json.dumps({"unit_id": "derived-unit-02", "batches": [{"batch_no": "generated-1"}]})}

    assert (
        _validate_structured_arguments_against_request(
            arguments,
            "Run the selected plan.",
            trusted_tool_evidence='{"unit_id":"derived-unit-02"}',
        )
        == []
    )


def test_structured_collection_rejects_when_most_items_use_the_wrong_field_value():
    request = (
        "Record 1: sample-a, quantity 12.5, category red;\n"
        "Record 2: sample-b, quantity 7.25, category blue;\n"
        "Record 3: sample-c, quantity 4.75, category blue;\n"
        "Record 4: sample-d, quantity 6.5, category blue."
    )
    arguments = {
        "batches": [
            {"name": "sample-a", "quantity": 12.5, "category": "blue"},
            {"name": "sample-b", "quantity": 7.25, "category": "blue"},
            {"name": "sample-c", "quantity": 4.75, "category": "red"},
            {"name": "sample-d", "quantity": 6.5, "category": "red"},
        ],
    }

    errors = _validate_structured_arguments_against_request(arguments, request)
    assert len(errors) == 1
    assert "item 1, 3, 4" in errors[0]
    assert "category" in errors[0]
    assert "Record 1: sample-a, quantity 12.5, category red" in errors[0]


def test_structured_collection_rejects_a_fully_permuted_field():
    request = (
        "Record 1: sample-a, quantity 12.5, category red;\n"
        "Record 2: sample-b, quantity 7.25, category blue;\n"
        "Record 3: sample-c, quantity 4.75, category green;\n"
        "Record 4: sample-d, quantity 6.5, category yellow."
    )
    arguments = {
        "batches": [
            {"name": "sample-a", "quantity": 12.5, "category": "blue"},
            {"name": "sample-b", "quantity": 7.25, "category": "green"},
            {"name": "sample-c", "quantity": 4.75, "category": "yellow"},
            {"name": "sample-d", "quantity": 6.5, "category": "red"},
        ],
    }

    errors = _validate_structured_arguments_against_request(arguments, request)
    assert len(errors) == 1
    assert "item 1, 2, 3, 4" in errors[0]
    assert "category" in errors[0]
    assert "Record 4: sample-d, quantity 6.5, category yellow" in errors[0]


def test_structured_collection_rejects_out_of_request_values_for_grounded_field():
    request = (
        "Record 1: sample-a, quantity 12.5, category blue;\n"
        "Record 2: sample-b, quantity 7.25, category blue;\n"
        "Record 3: sample-c, quantity 4.75, category blue;\n"
        "Record 4: sample-d, quantity 6.5, category blue."
    )
    arguments = {
        "batches": [
            {"name": "sample-a", "quantity": 12.5, "category": "blue"},
            {"name": "sample-b", "quantity": 7.25, "category": "blue"},
            {"name": "sample-c", "quantity": 4.75, "category": "red"},
            {"name": "sample-d", "quantity": 6.5, "category": "red"},
        ],
    }

    errors = _validate_structured_arguments_against_request(arguments, request)
    assert len(errors) == 1
    assert "item 3, 4" in errors[0]
    assert "category" in errors[0]
    assert "Record 3: sample-c, quantity 4.75, category blue" in errors[0]


def test_structured_collection_rejects_request_item_reordering():
    request = (
        "Record 1: sample-a, quantity 12.5, category red;\n"
        "Record 2: sample-b, quantity 7.25, category blue;\n"
        "Record 3: sample-c, quantity 4.75, category green."
    )
    arguments = {
        "batches": [
            {"name": "sample-b", "quantity": 7.25, "category": "blue"},
            {"name": "sample-a", "quantity": 12.5, "category": "red"},
            {"name": "sample-c", "quantity": 4.75, "category": "green"},
        ],
    }

    errors = _validate_structured_arguments_against_request(arguments, request)
    assert len(errors) == 1
    assert "order" in errors[0]


def test_structured_collection_does_not_reject_derived_items_without_request_evidence():
    arguments = {
        "batches": [
            {"internalId": "derived-001", "score": 0.125},
            {"internalId": "derived-002", "score": 0.375},
        ],
    }

    assert (
        _validate_structured_arguments_against_request(
            arguments,
            "Summarize the selected records.",
        )
        == []
    )
