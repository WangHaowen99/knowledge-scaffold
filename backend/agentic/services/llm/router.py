"""Process-wide routing for LLM calls."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from ...config import AgentConfig, AppConfig, LLMConfig
from ...config.system_config import resolve_llm_profile_config
from .service import LLMService
from .types import LLMResponse


class LLMPurpose(str, Enum):
    MAIN = "main"
    SUBAGENT = "subagent"
    TASK = "task"
    SUMMARY = "summary"
    MEMORY = "memory"


def _normalize_purpose(purpose: str | LLMPurpose) -> LLMPurpose:
    if isinstance(purpose, LLMPurpose):
        return purpose
    return LLMPurpose(str(purpose or "").strip().lower())


def _config_fingerprint(config: LLMConfig) -> Tuple[Any, ...]:
    return (
        config.base_url,
        config.model,
        config.api_key,
        config.timeout,
        config.max_tokens,
        config.temperature,
        config.provider,
        config.profile_id,
        config.profile_name,
    )


class LLMRouter:
    """Route model calls by purpose while keeping concrete clients cached."""

    def __init__(
        self,
        config: Optional[AppConfig] = None,
        *,
        agent_config: Optional[AgentConfig] = None,
        previous_router: Optional["LLMRouter"] = None,
    ) -> None:
        self.config = config or AppConfig.from_env()
        self.agent_config = agent_config or AgentConfig.from_env()
        self._main_config = self.config.llm
        self._task_config = self.config.task_llm
        self._services: Dict[Tuple[Any, ...], LLMService] = {}
        self._services_lock = threading.RLock()

        if previous_router is not None:
            self._cancel_lock = previous_router._cancel_lock
            self._cancel_events = previous_router._cancel_events
            self._reasoning_summary_executor = previous_router._reasoning_summary_executor
        else:
            self._cancel_lock = threading.Lock()
            self._cancel_events: Dict[str, threading.Event] = {}
            self._reasoning_summary_executor = ThreadPoolExecutor(
                max_workers=2,
                thread_name_prefix="reasoning-summary",
            )

    def call(
        self,
        *,
        purpose: str | LLMPurpose,
        messages: list[dict],
        profile_id: Optional[str] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        resolved_purpose = _normalize_purpose(purpose)
        if resolved_purpose == LLMPurpose.MEMORY:
            kwargs.setdefault("stream", False)
            kwargs.setdefault("enable_reasoning", False)
        service = self.get_service(resolved_purpose, profile_id=profile_id)
        return service.call(messages=messages, **kwargs)

    def get_service(
        self,
        purpose: str | LLMPurpose,
        profile_id: Optional[str] = None,
    ) -> LLMService:
        config = self.get_config(purpose, profile_id=profile_id)
        fingerprint = _config_fingerprint(config)
        with self._services_lock:
            service = self._services.get(fingerprint)
            if service is None:
                service = LLMService(
                    config,
                    agent_config=self.agent_config,
                    cancel_lock=self._cancel_lock,
                    cancel_events=self._cancel_events,
                    reasoning_summary_executor=self._reasoning_summary_executor,
                )
                self._services[fingerprint] = service
            summary_service = service if _normalize_purpose(purpose) == LLMPurpose.SUMMARY else self.get_summary_service()
            service.summary_llm = summary_service
            service.agent_config = self.agent_config
            return service

    def get_config(
        self,
        purpose: str | LLMPurpose,
        profile_id: Optional[str] = None,
    ) -> LLMConfig:
        resolved_purpose = _normalize_purpose(purpose)
        if resolved_purpose in {LLMPurpose.TASK, LLMPurpose.SUMMARY}:
            return self._task_config
        if resolved_purpose == LLMPurpose.MEMORY:
            return self._main_config
        normalized_profile_id = str(profile_id or "").strip()
        if not normalized_profile_id:
            return self._main_config
        return LLMConfig.from_mapping(resolve_llm_profile_config(normalized_profile_id))

    def get_summary_service(self) -> LLMService:
        return self.get_service(LLMPurpose.SUMMARY)

    def cancel_run(self, run_id: str) -> None:
        if not run_id:
            return
        with self._cancel_lock:
            event = self._cancel_events.get(run_id)
            if not event:
                event = threading.Event()
                self._cancel_events[run_id] = event
            event.set()

    def get_cancel_event(self, run_id: str) -> Optional[threading.Event]:
        if not run_id:
            return None
        with self._cancel_lock:
            event = self._cancel_events.get(run_id)
            if not event:
                event = threading.Event()
                self._cancel_events[run_id] = event
            return event

    def clear_run(self, run_id: str) -> None:
        if not run_id:
            return
        with self._cancel_lock:
            self._cancel_events.pop(run_id, None)


_ROUTER_LOCK = threading.Lock()
_LLM_ROUTER: Optional[LLMRouter] = None


def configure_llm_router(router: LLMRouter) -> None:
    global _LLM_ROUTER
    with _ROUTER_LOCK:
        _LLM_ROUTER = router


def get_llm_router() -> LLMRouter:
    with _ROUTER_LOCK:
        if _LLM_ROUTER is None:
            raise RuntimeError("LLM router not configured")
        return _LLM_ROUTER


def reset_llm_router_for_tests() -> None:
    global _LLM_ROUTER
    with _ROUTER_LOCK:
        _LLM_ROUTER = None
