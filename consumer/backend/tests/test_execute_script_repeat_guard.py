from types import SimpleNamespace

from agent.tools.base import ToolContext
from agent.tools.execute_script import (
    _validate_structured_result_consistency,
    MAX_SUCCESSFUL_EXECUTIONS_PER_OPERATION,
    ExecuteScriptTool,
    _is_successful_script_output,
    _prior_successful_operation_calls,
)


def _tool_call(call_id, arguments):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": call_id,
            "function": {
                "name": "execute_script",
                "arguments": __import__("json").dumps(arguments, ensure_ascii=False),
            },
        }],
    }


def _context(messages):
    return ToolContext(
        memory_manager=None,
        messages=messages,
        run_id="run-repeat",
        session_id="session-repeat",
        turn_id=1,
        agent_config=SimpleNamespace(execute_script_timeout=30),
        caller="main",
    )


def test_structured_unit_load_result_rejects_unexplained_monthly_difference():
    payload = {
        "operation_key": "装置负荷硬约束炼化装置容量约束与超限分流",
        "batches": [
            {"unit_loads": {"蜡油加氢": {"input_tons": 120}}},
            {"unit_loads": {"蜡油加氢": {"input_tons": 80}}},
        ],
        "monthly_unit_load": {
            "units": {"蜡油加氢": {"monthly_input_tons": 190}}
        },
    }

    error = _validate_structured_result_consistency(__import__("json").dumps(payload, ensure_ascii=False))

    assert error == "Error: BUSINESS_SUMMARY_INCONSISTENT"


def test_structured_unit_load_result_accepts_matching_monthly_total():
    payload = {
        "operation_key": "装置负荷硬约束炼化装置容量约束与超限分流",
        "batches": [
            {"unit_loads": {"2#柴油加氢": {"input_tons": 120}}},
            {"unit_loads": {"2#柴油加氢": {"input_tons": 80}}},
        ],
        "monthly_unit_load": {
            "units": {"柴油加氢": {"monthly_input_tons": 200}}
        },
    }

    assert _validate_structured_result_consistency(__import__("json").dumps(payload, ensure_ascii=False)) is None


def test_structured_unit_load_result_allows_documented_raw_vgo_monthly_basis():
    payload = {
        "operation_key": "装置负荷硬约束炼化装置容量约束与超限分流",
        "batches": [
            {
                "input_weight_tons": 100,
                "vgo_load_raw_feed_tons": 90,
                "unit_loads": {"蜡油加氢": {"input_tons": 100}},
            },
            {
                "input_weight_tons": 100,
                "vgo_load_raw_feed_tons": 90,
                "unit_loads": {"蜡油加氢": {"input_tons": 100}},
            },
        ],
        "monthly_unit_load": {
            "units": {"蜡油加氢": {"monthly_input_tons": 180}}
        },
    }

    assert _validate_structured_result_consistency(__import__("json").dumps(payload, ensure_ascii=False)) is None


def test_identical_successful_script_call_is_reused_instead_of_executed_again():
    arguments = {
        "ontology_id": "demo",
        "operation_key": "装置负荷校验",
        "arguments": {"monthlyFeedAndDays": [{"totalProcessingDays": 31}]},
    }
    context = _context([
        _tool_call("call-1", arguments),
        {"role": "tool", "name": "execute_script", "tool_call_id": "call-1", "content": '{"monthlyLoadSummary":[]}'},
    ])

    outcome = ExecuteScriptTool().execute(
        args=arguments,
        context=context,
        tool_call_id="call-2",
    )

    assert "already succeeded with exactly the same inputs" in outcome.content


def test_distinct_successful_retries_are_capped_before_agent_loops_forever():
    messages = []
    for index in range(MAX_SUCCESSFUL_EXECUTIONS_PER_OPERATION):
        arguments = {
            "ontology_id": "demo",
            "operation_key": "装置负荷校验",
            "arguments": {"monthlyFeedAndDays": [{"totalProcessingDays": 27 + index}]},
        }
        call_id = f"call-{index}"
        messages.extend([
            _tool_call(call_id, arguments),
            {"role": "tool", "name": "execute_script", "tool_call_id": call_id, "content": '{"monthlyLoadSummary":[]}'},
        ])

    outcome = ExecuteScriptTool().execute(
        args={
            "ontology_id": "demo",
            "operation_key": "装置负荷校验",
            "arguments": {"monthlyFeedAndDays": [{"totalProcessingDays": 1000}]},
        },
        context=_context(messages),
        tool_call_id="call-next",
    )

    assert "already returned successful results multiple times" in outcome.content


def test_failed_script_call_does_not_consume_retry_budget():
    arguments = {
        "ontology_id": "demo",
        "operation_key": "装置负荷校验",
        "arguments": {},
    }
    messages = [
        _tool_call("call-error", arguments),
        {"role": "tool", "name": "execute_script", "tool_call_id": "call-error", "content": 'Error: invalid arguments'},
    ]

    assert _prior_successful_operation_calls(
        messages,
        ontology_id="demo",
        operation_key="装置负荷校验",
        current_tool_call_id="call-retry",
    ) == []


def test_operation_selection_redirect_does_not_consume_retry_budget():
    content = (
        "Operation selection blocked: load the coordinating operation first. "
        "The script did not run."
    )

    assert _is_successful_script_output(content) is False
