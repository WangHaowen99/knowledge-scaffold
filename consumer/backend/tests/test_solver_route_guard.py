from types import SimpleNamespace

import pytest

from agent.core.shared import solver_route_guard


def _payload():
    return {
        "operations_catalog": [
            {
                "operation key": "optimize_minus_one_route",
                "title": "Minus-one route optimization",
                "标题": "合成有序路线优化",
            },
            {
                "operation key": "explain_process",
                "标题": "流程说明",
            },
        ],
        "solver_catalog": [
            {
                "operation_key": "optimize_minus_one_route",
                "enabled": True,
            },
        ],
    }


def _solver_spec():
    return SimpleNamespace(
        name="solve_optimize_minus_one_route_abcd1234",
        schema={
            "type": "function",
            "function": {
                "name": "solve_optimize_minus_one_route_abcd1234",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "plan_month": {"type": "string"},
                        "items": {"type": "array", "minItems": 1},
                    },
                    "required": ["plan_month", "items"],
                    "additionalProperties": False,
                },
            },
        },
    )


def test_route_guard_matches_named_solver_operation_without_domain_hardcoding(monkeypatch):
    monkeypatch.setattr(
        solver_route_guard,
        "_load_solver_root_payload",
        lambda **_kwargs: _payload(),
    )
    monkeypatch.setattr(
        solver_route_guard,
        "build_solver_tool_specs",
        lambda _records: (SimpleNamespace(name="solve_optimize_minus_one_route_abcd1234"),),
    )

    requirement = solver_route_guard.resolve_solver_route_requirement(
        user_request="请执行合成有序路线优化并调用求解工具",
        selected_ontology_id="package-a",
        selected_ontology_version="9.4.2",
        workspace_id="workspace-a",
    )

    assert requirement is not None
    assert requirement.operation_key == "optimize_minus_one_route"
    assert requirement.tool_name == "solve_optimize_minus_one_route_abcd1234"


def test_route_guard_reuses_preflight_root_payload_without_package_read(monkeypatch):
    monkeypatch.setattr(
        solver_route_guard,
        "_load_solver_root_payload",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("solver routing must reuse the preflight payload")
        ),
    )
    monkeypatch.setattr(
        solver_route_guard,
        "build_solver_tool_specs",
        lambda _records: (
            SimpleNamespace(name="solve_optimize_minus_one_route_abcd1234"),
        ),
    )
    payload = {
        "operations_catalog": tuple(_payload()["operations_catalog"]),
        "solver_catalog": tuple(_payload()["solver_catalog"]),
    }

    requirement = solver_route_guard.resolve_solver_route_requirement(
        user_request="execute optimize_minus_one_route",
        selected_ontology_id="package-a",
        selected_ontology_version="9.4.2",
        workspace_id="workspace-a",
        root_payload=payload,
    )

    assert requirement is not None
    assert requirement.operation_key == "optimize_minus_one_route"


def test_route_guard_matches_unique_solver_schema_from_structured_arguments(monkeypatch):
    monkeypatch.setattr(
        solver_route_guard,
        "_load_solver_root_payload",
        lambda **_kwargs: _payload(),
    )
    monkeypatch.setattr(
        solver_route_guard,
        "build_solver_tool_specs",
        lambda _records: (_solver_spec(),),
    )
    arguments = {
                "plan_month": "2099-03",
        "items": [{"item_id": "batch-1"}],
    }

    requirement = solver_route_guard.resolve_solver_route_requirement(
        user_request="请按这份生产计划返回最终结果",
        arguments=arguments,
        selected_ontology_id="package-a",
        selected_ontology_version="9.4.2",
        workspace_id="workspace-a",
    )

    assert requirement is not None
    assert requirement.operation_key == "optimize_minus_one_route"
    assert requirement.tool_name == "solve_optimize_minus_one_route_abcd1234"
    assert requirement.direct_arguments == arguments


def test_route_guard_does_not_bind_unmatched_structured_arguments(monkeypatch):
    monkeypatch.setattr(
        solver_route_guard,
        "_load_solver_root_payload",
        lambda **_kwargs: _payload(),
    )
    monkeypatch.setattr(
        solver_route_guard,
        "build_solver_tool_specs",
        lambda _records: (_solver_spec(),),
    )

    requirement = solver_route_guard.resolve_solver_route_requirement(
        user_request="请查询这份业务数据",
        arguments={"unrelated": True},
        selected_ontology_id="package-a",
        selected_ontology_version="9.4.2",
        workspace_id="workspace-a",
    )

    assert requirement is None


def test_route_guard_does_not_force_solver_for_explanation_question(monkeypatch):
    monkeypatch.setattr(
        solver_route_guard,
        "_load_solver_root_payload",
        lambda **_kwargs: _payload(),
    )

    requirement = solver_route_guard.resolve_solver_route_requirement(
        user_request="请解释什么是合成有序路线优化",
        selected_ontology_id="package-a",
        selected_ontology_version="9.4.2",
        workspace_id="workspace-a",
    )

    assert requirement is None


@pytest.mark.parametrize(
    "query",
    [
        "How does optimize_minus_one_route work?",
        "Explain why solve optimize_minus_one_route works this way.",
        "请解释求解器中的合成有序路线优化如何工作。",
    ],
)
def test_route_guard_does_not_treat_solver_words_as_execution_inside_explanations(
    monkeypatch,
    query,
):
    monkeypatch.setattr(
        solver_route_guard,
        "_load_solver_root_payload",
        lambda **_kwargs: _payload(),
    )

    requirement = solver_route_guard.resolve_solver_route_requirement(
        user_request=query,
        selected_ontology_id="package-a",
        selected_ontology_version="9.4.2",
        workspace_id="workspace-a",
    )

    assert requirement is None


def test_route_guard_requires_execution_intent_and_phrase_boundary_without_arguments(
    monkeypatch,
):
    payload = {
        "operations_catalog": [{"operation key": "route", "title": "Route"}],
        "solver_catalog": [{"operation_key": "route", "enabled": True}],
    }
    monkeypatch.setattr(
        solver_route_guard,
        "_load_solver_root_payload",
        lambda **_kwargs: payload,
    )

    requirement = solver_route_guard.resolve_solver_route_requirement(
        user_request="show router documentation",
        selected_ontology_id="package-a",
        selected_ontology_version="9.4.2",
        workspace_id="workspace-a",
    )

    assert requirement is None


def test_route_guard_does_not_hijack_fixed_route_load_and_capacity_evaluation(
    monkeypatch,
):
    monkeypatch.setattr(
        solver_route_guard,
        "_load_solver_root_payload",
        lambda **_kwargs: _payload(),
    )

    requirement = solver_route_guard.resolve_solver_route_requirement(
        user_request=(
            "【合成测试数据】月度生产规划已逐项指定处理路径。"
            "请统计资源负荷是否过载，并校验容量是否超限。"
        ),
        selected_ontology_id="package-a",
        selected_ontology_version="9.4.2",
        workspace_id="workspace-a",
    )

    assert requirement is None


def test_route_guard_keeps_implicit_solver_route_for_unassigned_production_plan(
    monkeypatch,
):
    monkeypatch.setattr(
        solver_route_guard,
        "_load_solver_root_payload",
        lambda **_kwargs: _payload(),
    )
    monkeypatch.setattr(
        solver_route_guard,
        "build_solver_tool_specs",
        lambda _records: (_solver_spec(),),
    )

    requirement = solver_route_guard.resolve_solver_route_requirement(
        user_request=(
            "【合成测试数据】月度生产规划如下："
            "第1项原料甲，处理量2单位，周期1天；"
            "第2项原料乙，处理量3单位，周期2天。"
        ),
        selected_ontology_id="package-a",
        selected_ontology_version="9.4.2",
        workspace_id="workspace-a",
    )

    assert requirement is not None
    assert requirement.operation_key == "optimize_minus_one_route"
    assert requirement.tool_name == "solve_optimize_minus_one_route_abcd1234"


def test_evaluation_request_suppresses_solver_tools_for_current_turn():
    assert solver_route_guard.should_suppress_solver_tools(
        "【合成测试数据】请统计资源负荷是否过载，并校验容量是否超限。"
    ) is True


def test_explicit_solver_request_keeps_solver_tools_available():
    assert solver_route_guard.should_suppress_solver_tools(
        "【合成测试数据】请求解候选方案并统计结果。"
    ) is False


def test_route_binding_suppresses_solver_tools_only_without_requirement():
    suppressed_context = solver_route_guard.bind_solver_route_requirement(
        {},
        None,
        suppress_solver_tools=True,
    )

    assert solver_route_guard.solver_tools_suppressed_from_state(
        {"user_context": suppressed_context}
    ) is True

    required_context = solver_route_guard.bind_solver_route_requirement(
        suppressed_context,
        solver_route_guard.SolverRouteRequirement(
            operation_key="optimize_minus_one_route",
            tool_name="solve_optimize_minus_one_route_abcd1234",
        ),
        suppress_solver_tools=True,
    )

    assert solver_route_guard.solver_tools_suppressed_from_state(
        {"user_context": required_context}
    ) is False


def test_solver_history_filter_removes_the_entire_solver_turn():
    messages = [
        {"role": "user", "content": "Generate a synthetic route plan."},
        {
            "role": "assistant",
            "content": "Calling the route tool.",
            "tool_calls": [
                {
                    "id": "solver-call",
                    "type": "function",
                    "function": {
                        "name": "solve_synthetic_route_aaaaaaaa",
                        "arguments": "{}",
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "solver-call",
            "name": "solve_synthetic_route_aaaaaaaa",
            "content": "synthetic authoritative result",
        },
        {"role": "assistant", "content": "The synthetic route plan is ready."},
        {"role": "user", "content": "Read ordinary synthetic data."},
        {
            "role": "assistant",
            "content": "Calling a regular tool.",
            "tool_calls": [
                {
                    "id": "script-call",
                    "type": "function",
                    "function": {
                        "name": "execute_script",
                        "arguments": "{}",
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "script-call",
            "name": "execute_script",
            "content": "synthetic script result",
        },
        {"role": "assistant", "content": "The ordinary check is complete."},
    ]

    filtered = solver_route_guard.filter_solver_history_messages(messages)

    assert [message.get("content") for message in filtered] == [
        "Read ordinary synthetic data.",
        "Calling a regular tool.",
        "synthetic script result",
        "The ordinary check is complete.",
    ]


def test_route_guard_exposes_only_required_solver_and_interaction_tools():
    requirement = solver_route_guard.SolverRouteRequirement(
        operation_key="optimize_minus_one_route",
        tool_name="solve_optimize_minus_one_route_abcd1234",
    )
    schemas = [
        {"type": "function", "function": {"name": "execute_script"}},
        {
            "type": "function",
            "function": {"name": "solve_optimize_minus_one_route_abcd1234"},
        },
        {"type": "function", "function": {"name": "request_user_interaction"}},
    ]

    restricted = solver_route_guard.restrict_tools_for_solver_route(
        schemas,
        requirement=requirement,
    )

    assert [item["function"]["name"] for item in restricted] == [
        "solve_optimize_minus_one_route_abcd1234",
        "request_user_interaction",
    ]


def test_route_guard_fails_closed_when_required_toolcard_is_missing():
    requirement = solver_route_guard.SolverRouteRequirement(
        operation_key="optimize_minus_one_route",
        tool_name="solve_optimize_minus_one_route_abcd1234",
    )

    with pytest.raises(solver_route_guard.SolverRouteGuardError) as error_info:
        solver_route_guard.restrict_tools_for_solver_route(
            [{"type": "function", "function": {"name": "execute_script"}}],
            requirement=requirement,
        )

    assert error_info.value.code == "solver_tool_unavailable"


def test_route_guard_requires_authoritative_result_before_success():
    requirement = solver_route_guard.SolverRouteRequirement(
        operation_key="optimize_minus_one_route",
        tool_name="solve_optimize_minus_one_route_abcd1234",
    )
    guard = solver_route_guard.SolverRouteExecutionGuard(requirement)

    with pytest.raises(solver_route_guard.SolverRouteGuardError) as error_info:
        guard.ensure_terminal(allow_awaiting_input=False)

    assert error_info.value.code == "solver_authoritative_result_missing"

    guard.observe(
        {
            "protocol": "agent-stream.v2",
            "type": "solver.result.authoritative",
            "payload": {
                "tool_name": requirement.tool_name,
                "authoritative": True,
            },
        }
    )
    guard.ensure_terminal(allow_awaiting_input=False)


def test_route_guard_preserves_matching_terminal_solver_error_code():
    requirement = solver_route_guard.SolverRouteRequirement(
        operation_key="optimize_minus_one_route",
        tool_name="solve_optimize_minus_one_route_abcd1234",
    )
    guard = solver_route_guard.SolverRouteExecutionGuard(requirement)
    guard.observe(
        {
            "protocol": "agent-stream.v2",
            "type": "tool.finished",
            "payload": {
                "tool_name": requirement.tool_name,
                "status": "error",
                "error": (
                    '{"code":"fact_snapshot_invalid","phase":"snapshot",'
                    '"retryable":false,"status":"error"}'
                ),
            },
        }
    )

    with pytest.raises(solver_route_guard.SolverRouteGuardError) as error_info:
        guard.ensure_terminal(allow_awaiting_input=False)

    assert error_info.value.code == "fact_snapshot_invalid"
    assert "fact_snapshot_invalid" in error_info.value.public_message
    assert error_info.value.phase == "snapshot"
    assert error_info.value.retryable is False
