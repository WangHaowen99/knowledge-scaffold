from agentic.config import DirectExecutionConfig
from agentic.direct_execution.router import OperationDirectRouter
from agentic.direct_execution.models import RuntimeContext


def test_direct_router_falls_back_for_interactive_operation(monkeypatch):
    monkeypatch.setattr(
        "agentic.direct_execution.router.find_best_script_operation",
        lambda **kwargs: (
            {
                "operation_key": "流程A",
                "arguments": [],
                "parameters": [],
            },
            "流程A",
            1.0,
        ),
    )
    monkeypatch.setattr(
        "agentic.direct_execution.router._operation_requires_user_interaction",
        lambda ontology_id, operation_key: True,
    )

    router = OperationDirectRouter(
        DirectExecutionConfig(
            enabled=True,
            selected_ontology_only=True,
            confidence_threshold=0.85,
        )
    )
    decision = router.route(
        user_request="执行流程A",
        context={"selected_ontology_id": "demo"},
        runtime_context=RuntimeContext(
            session_id="sess",
            run_id="run",
            turn_id=1,
            selected_ontology_id="demo",
        ),
    )

    assert decision.route == "fallback_agent_loop"
    assert decision.reason == "operation_requires_user_interaction"
    assert decision.operation_key == "流程A"


def test_direct_router_extracts_declared_alias_argument(monkeypatch):
    monkeypatch.setattr(
        "agentic.direct_execution.router.find_best_script_operation",
        lambda **kwargs: (
            {
                "operation_key": "target_item_source_item_version_query",
                "arguments": [],
                "parameters": [
                    {
                        "name": "target_item_id",
                        "flag": "--target_item-code",
                        "required": True,
                        "type": "string",
                        "aliases": ["TargetItem编码", "TargetItem编号", "target_item_code"],
                    }
                ],
            },
            "target_item_source_item_version_query",
            1.0,
        ),
    )
    monkeypatch.setattr(
        "agentic.direct_execution.router._operation_requires_user_interaction",
        lambda ontology_id, operation_key: False,
    )

    router = OperationDirectRouter(
        DirectExecutionConfig(
            enabled=True,
            selected_ontology_only=True,
            confidence_threshold=0.85,
        )
    )
    decision = router.route(
        user_request="问题21：TargetItem对应的SourceItem版本是什么？TargetItem编码是 A001",
        context={"selected_ontology_id": "demo"},
        runtime_context=RuntimeContext(
            session_id="sess",
            run_id="run",
            turn_id=1,
            selected_ontology_id="demo",
        ),
    )

    assert decision.route == "direct_script"
    assert decision.business_arguments == {"target_item_id": "A001"}
