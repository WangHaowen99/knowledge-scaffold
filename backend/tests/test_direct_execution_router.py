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
