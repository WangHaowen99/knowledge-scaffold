"""Direct execution branch for ontology script operations.

When enabled, this module intercepts user requests at the orchestrator level—
before the Jiuwen ReAct loop—to directly execute ontology script operations when
the user request clearly matches a script-type operation and all required
parameters are available.
"""

from .models import DirectExecutionResult, RouteDecision, RuntimeContext
from .router import OperationDirectRouter

__all__ = [
    "DirectExecutionResult",
    "OperationDirectRouter",
    "RouteDecision",
    "RuntimeContext",
]
