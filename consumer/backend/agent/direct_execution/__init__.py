"""Direct execution branch for ontology script operations.

When enabled, this module intercepts user requests at the orchestrator level—
before the AgentLoopEngine—to directly execute ontology script operations when
the user request clearly matches a script-type operation and all required
parameters are available.
"""

from agent.direct_execution.models import DirectExecutionResult, RouteDecision, RuntimeContext
from agent.direct_execution.router import OperationDirectRouter

__all__ = [
    "DirectExecutionResult",
    "OperationDirectRouter",
    "RouteDecision",
    "RuntimeContext",
]
