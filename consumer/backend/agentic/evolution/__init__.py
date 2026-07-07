"""Operation evolution proposal buffer primitives.

This package is independent from `agentic.memory`. Memory keeps user/workspace
preferences; evolution accumulates structured improvement proposals against
ontology operations.
"""

from .models import Aggregate, Proposal, compute_fingerprint
from .buffer import ProposalBuffer
from .near_point_recall import build_near_point_block, parse_ontology_doc_path
from .proposal_writer import (
    ProposalWriteResult,
    append_operation_proposal_with_relations,
    judge_operation_relations,
    operation_relation_candidates,
)
from .patcher import (
    OperationPatcher,
    PatcherConfig,
    list_pending_staged,
    resolve_operation_md,
    staged_paths_for,
)
from .reflection_worker import (
    ReflectionSnapshot,
    ReflectionWorker,
    build_snapshot as build_reflection_snapshot,
    parse_execution_summary,
)
from .registry import (
    get_operation_patcher,
    get_proposal_buffer,
    init_operation_patcher,
    reset_operation_patcher,
    reset_proposal_buffer,
)

__all__ = [
    "Aggregate",
    "OperationPatcher",
    "PatcherConfig",
    "Proposal",
    "ProposalBuffer",
    "ProposalWriteResult",
    "append_operation_proposal_with_relations",
    "ReflectionSnapshot",
    "ReflectionWorker",
    "build_reflection_snapshot",
    "build_near_point_block",
    "compute_fingerprint",
    "get_operation_patcher",
    "get_proposal_buffer",
    "init_operation_patcher",
    "judge_operation_relations",
    "list_pending_staged",
    "operation_relation_candidates",
    "parse_execution_summary",
    "parse_ontology_doc_path",
    "reset_operation_patcher",
    "reset_proposal_buffer",
    "resolve_operation_md",
    "staged_paths_for",
]
