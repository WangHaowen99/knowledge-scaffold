"""Data models for the direct execution branch."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

RouteLabel = Literal["direct_script", "missing_args", "fallback_agent_loop"]


@dataclass
class RuntimeContext:
    """Runtime context that only the backend can construct.

    All fields originate from the current request lifecycle (run, session, turn)
    and must never be overridable by user-supplied natural language or request
    body fields.
    """

    session_id: str
    run_id: str
    turn_id: int
    user_id: Optional[str] = None
    workspace_id: Optional[str] = None
    selected_ontology_id: Optional[str] = None

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


@dataclass
class RouteDecision:
    """Result of the routing decision for the current user request."""

    route: RouteLabel
    reason: str
    confidence: float

    # Operation metadata (populated for direct_script and missing_args)
    ontology_id: Optional[str] = None
    operation_key: Optional[str] = None

    # Parameter information
    business_arguments: Dict[str, Any] = field(default_factory=dict)
    missing_business_arguments: List[str] = field(default_factory=list)
    runtime_requirements: List[str] = field(default_factory=list)

    # Full script entry from parsed catalog (for runner)
    script_entry: Optional[Dict[str, Any]] = None


@dataclass
class DirectExecutionResult:
    """Result of executing a script in the direct branch."""

    status: str  # "success" | "error"
    output: str
    elapsed_ms: int
    error: Optional[str] = None
