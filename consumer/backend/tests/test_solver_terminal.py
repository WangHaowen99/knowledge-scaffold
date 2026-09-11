from __future__ import annotations

import json

from agent.tools.solver_terminal import SolverTerminalOutcomeProjector


def test_terminal_projector_explains_an_incompatible_knowledge_package_contract():
    outcome = SolverTerminalOutcomeProjector().project(
        {
            "status": "error",
            "code": "knowledge_package_solver_contract_incompatible",
            "phase": "snapshot",
            "retryable": False,
        }
    )

    assert json.loads(outcome.content) == {
        "code": "knowledge_package_solver_contract_incompatible",
        "phase": "snapshot",
        "retryable": False,
        "status": "error",
    }
    assert outcome.terminal is True
    assert outcome.state_updates["final_output"] == ("知识包求解器契约版本不兼容，请升级或重新导入知识包。")
