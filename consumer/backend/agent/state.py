"""State definitions for the agentic workflow."""

from typing import Any, Dict, List, NotRequired, Optional, TypedDict


class TurnExecutionRecord(TypedDict):
    """
    Complete execution record for a single conversation turn.

    A turn represents one user request and its final workflow result.
    """
    turn_id: int
    session_id: str
    run_id: str
    user_request: str
    start_time: str
    end_time: Optional[str]
    status: str                 # "success" | "failed" | "cancelled" | "awaiting_input"
    final_output: Optional[Dict[str, Any]]
    summary: Optional[str]
    ontology_version: Optional[str]  # active_version when the turn ran (skill-evolution version gate)
    interaction_response: NotRequired[Dict[str, Any]]
    interaction_consumed_at: NotRequired[str]
    interaction_cancelled_at: NotRequired[str]
    interaction_context_consumed_at: NotRequired[str]
    interaction_context_consumed_by_turn_id: NotRequired[int]
    prior_interaction_cancellation: NotRequired[Dict[str, Any]]


class SessionExecutionHistory(TypedDict):
    """
    Complete execution history for a chat session.

    Contains all turns in a session, stored as a list for easy
    appending of new turns and retrieval of history.
    """
    session_id: str
    created_at: str
    updated_at: str
    turns: List[TurnExecutionRecord]
    session_history: List[Dict[str, Any]]  # LLM message history (OpenAI-format messages list)
    read_documents: Dict[str, Any]  # Structured read-document ledger for cross-turn tool guards


# =============================================================================
# Cross-turn Context Types (for multi-turn conversation management)
# =============================================================================

class TurnSummary(TypedDict):
    """
    Compressed summary of a single conversation turn.

    Used to maintain cross-turn context without context explosion.
    Each turn is summarized by LLM and stored for injection into future turns.
    """
    turn_id: int
    user_request: str           # Original user request (complete)
    result: str                 # Final output (complete)
    execution_summary: str      # LLM-generated summary of the execution process
    timestamp: str              # ISO format timestamp


class ReadDocumentRecord(TypedDict, total=False):
    """Structured record for a document read or preloaded into runtime context."""

    path: str
    normalized_path: str
    doc_type: str
    ontology_id: Optional[str]
    entity_name: Optional[str]
    operation_name: Optional[str]
    source: str
    turn_id: int
    iteration: Optional[int]
    content_hash: str
    size_bytes: int
    mtime_ns: Optional[int]
    read_count: int


class ReadDocumentsRegistry(TypedDict):
    """Aggregated read-document ledger kept alongside the mutable agent state."""

    by_path: Dict[str, ReadDocumentRecord]
    recent_paths: List[str]
    active_object_docs: List[str]


class AgenticState(TypedDict):
    """
    Mutable run state used by the single agent loop.
    """

    # Workflow identification
    run_id: Optional[str]
    session_id: Optional[str]
    user_id: Optional[str]
    workspace_id: Optional[str]
    user_request: str
    user_context: Dict[str, Any]
    memory_context: Optional[str]

    # Execution results
    results: Dict[int, Any]  # task_id -> result
    completed_ids: List[int]
    failed_ids: List[int]
    errors: List[str]

    # Message history for LLM calls (OpenAI format)
    messages: List[Dict[str, Any]]

    # Structured read-document ledger (stable beyond message compaction)
    read_documents: ReadDocumentsRegistry

    # Operation-evolution fingerprints injected during this turn for outcome tracking
    evolution_recall_records: List[Dict[str, Any]]

    # Debug mode
    debug: bool

    # Post-processed final output
    final_output: Optional[Dict[str, Any]]

    # Current turn identifier
    current_turn_id: int
