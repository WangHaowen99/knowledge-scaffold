"""Shared tool interfaces and execution context."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Literal, Optional, Protocol

from config import AgentConfig

if TYPE_CHECKING:
    from agent.memory.protocol import MemoryManager

ToolCaller = Literal["main", "subagent"]


@dataclass(frozen=True)
class ToolInvocation:
    """Provider-neutral tool call passed from an adapter to the executor.

    Provider payloads must be normalized before they cross this boundary.  In
    particular, ``arguments`` is already a JSON object rather than a serialized
    OpenAI ``function.arguments`` string.
    """

    id: Optional[str]
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.id is not None and not isinstance(self.id, str):
            raise TypeError("ToolInvocation.id must be a string or None")
        if not isinstance(self.name, str):
            raise TypeError("ToolInvocation.name must be a string")
        if not isinstance(self.arguments, dict):
            raise TypeError("ToolInvocation.arguments must be a dict")
        object.__setattr__(self, "arguments", dict(self.arguments))


@dataclass
class ToolContext:
    memory_manager: Optional["MemoryManager"]
    messages: List[Dict[str, Any]]
    run_id: Optional[str]
    session_id: Optional[str]
    turn_id: int
    agent_config: AgentConfig
    caller: ToolCaller
    user_id: Optional[str] = None
    agent_mode: Optional[str] = None
    agent_iteration: Optional[int] = None
    tool_batch_index: Optional[int] = None
    workspace_id: Optional[str] = None
    project_root: Optional[str] = None
    event_emitter: Optional[Callable[[str, Dict[str, Any]], None]] = None
    parent_tool_call_id: Optional[str] = None
    subagent_manager: Any = None
    subagent_lineage: Optional[Dict[str, Any]] = None
    protocol_event_observer: Optional[Callable[[str, Dict[str, Any]], None]] = None
    read_documents: Optional[Dict[str, Any]] = None
    selected_ontology_id: Optional[str] = None
    selected_ontology_version: Optional[str] = None
    model_profile_id: Optional[str] = None
    observer: Any = None
    supports_user_interaction: bool = True
    # Appended at the end to preserve positional compatibility for existing
    # tool adapters while carrying the W3C context through solver calls.
    traceparent: Optional[str] = None


@dataclass
class ToolOutcome:
    """Unified return value for every tool execution.

    content: tool message body sent back to the LLM. None means no tool
        message is appended.
    state_updates: keys merged into AgenticState after the batch completes.
    terminal: when True, the agent loop exits after this batch.
    """
    content: Optional[str] = None
    state_updates: Dict[str, Any] = field(default_factory=dict)
    terminal: bool = False


@dataclass
class ToolBatchResult:
    messages: List[Dict[str, Any]] = field(default_factory=list)
    state_updates: Dict[str, Any] = field(default_factory=dict)
    terminal: bool = False
    tool_intervals_ms: List[Dict[str, int]] = field(default_factory=list)
    terminal_tool_call_id: Optional[str] = None
    terminal_content: Optional[str] = None


class BaseTool(Protocol):
    name: str
    schema: Dict[str, Any]
    allowed_callers: set[ToolCaller]
    concurrency_safe: bool
    max_parallelism: Optional[int]
    category: str

    def execute(
        self,
        *,
        args: Dict[str, Any],
        context: ToolContext,
        tool_call_id: Optional[str] = None,
        event_emitter: Optional[Callable[..., Any]] = None,
    ) -> ToolOutcome:
        """Run the tool with the provided execution context."""
