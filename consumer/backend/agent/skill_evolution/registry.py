# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Process-level singletons for the skill-evolution subsystem."""

from __future__ import annotations

from threading import RLock
from typing import Optional

from workspace.workspaces import get_runtime_root
from agent.skill_evolution.service import SkillEvolutionService
from agent.skill_evolution.store import SkillEvolutionStore
from agent.skill_evolution.worker import SkillEvolutionWorker

# RLock: init_skill_evolution_worker holds this lock while calling the
# store/service getters, which re-acquire it — a plain Lock would self-deadlock.
_INSTANCE_LOCK = RLock()
_STORE: Optional[SkillEvolutionStore] = None
_SERVICE: Optional[SkillEvolutionService] = None
_WORKER: Optional[SkillEvolutionWorker] = None


def get_skill_evolution_store() -> SkillEvolutionStore:
    global _STORE
    if _STORE is not None:
        return _STORE
    with _INSTANCE_LOCK:
        if _STORE is None:
            _STORE = SkillEvolutionStore(runtime_root=get_runtime_root())
        return _STORE


def get_skill_evolution_service() -> SkillEvolutionService:
    global _SERVICE
    if _SERVICE is not None:
        return _SERVICE
    with _INSTANCE_LOCK:
        if _SERVICE is None:
            _SERVICE = SkillEvolutionService(get_skill_evolution_store())
        return _SERVICE


def init_skill_evolution_worker() -> Optional[SkillEvolutionWorker]:
    """Start the background worker at app startup (always, regardless of
    ``enabled``).

    The worker is config-reactive: it re-reads ``skill_evolution`` every tick
    and self-disables when ``enabled=false`` (just sleeps, no task advancement).
    So we always start the thread at boot — flipping ``enabled`` at runtime
    (PATCH / file edit / env) takes effect within one poll cycle without any
    reconcile call, and there is no stop/restart dance on config change. When
    disabled the thread is idle (one cheap config read per poll interval).
    """
    global _WORKER
    with _INSTANCE_LOCK:
        if _WORKER is not None:
            return _WORKER
        service = get_skill_evolution_service()
        worker = SkillEvolutionWorker(get_skill_evolution_store(), service)
        worker.start()
        _WORKER = worker
        return _WORKER


def reset_skill_evolution_singletons() -> None:
    """Drop cached singletons. Test-only."""
    global _STORE, _SERVICE, _WORKER
    with _INSTANCE_LOCK:
        if _WORKER is not None:
            _WORKER.stop()
        _WORKER = None
        _SERVICE = None
        _STORE = None
