"""Skill-evolution subsystem: external skill-evolver integration for ontologies.

See docs/ontology-skill-evolution.md for the full design. Independent of the
operation-evolution module (`backend/agentic/evolution/`); reuses only shared
ontology helpers.
"""

from agent.skill_evolution.models import (
    APPROVE_DECISIONS,
    PENDING_STATUSES,
    TERMINAL_STATUSES,
    DiffFileEntry,
    DiffResult,
    DriftStatus,
    TaskRecord,
)
from agent.skill_evolution.registry import (
    get_skill_evolution_service,
    get_skill_evolution_store,
    init_skill_evolution_worker,
    reset_skill_evolution_singletons,
)
from api.routes.skill_evolution import skill_evolution_bp
from agent.skill_evolution.service import GateError, SkillEvolutionService, SkillEvolverClient
from agent.skill_evolution.store import SkillEvolutionStore
from agent.skill_evolution.worker import SkillEvolutionWorker

__all__ = [
    "APPROVE_DECISIONS",
    "PENDING_STATUSES",
    "TERMINAL_STATUSES",
    "DiffFileEntry",
    "DiffResult",
    "DriftStatus",
    "TaskRecord",
    "get_skill_evolution_service",
    "get_skill_evolution_store",
    "init_skill_evolution_worker",
    "reset_skill_evolution_singletons",
    "skill_evolution_bp",
    "GateError",
    "SkillEvolutionService",
    "SkillEvolverClient",
    "SkillEvolutionStore",
    "SkillEvolutionWorker",
]
