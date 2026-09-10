"""OpenAI-compatible chat completion transport."""

from __future__ import annotations

import json
import logging
import math
import threading
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional

from agent.exceptions import WorkflowCancelledError
from config import AgentConfig, LLMConfig
from llm.provider_adapters import get_provider_adapter
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI
from utils.logging import get_logger

logger = get_logger(__name__)

_RETRYABLE_STATUS_CODES = frozenset({408, 409, 429})
_MAX_RETRY_AFTER_SECONDS = 60.0


def _is_retryable_status_code(status_code: Any) -> bool:
    try:
        normalized = int(status_code)
    except (TypeError, ValueError):
        return False
    return normalized in _RETRYABLE_STATUS_CODES or 500 <= normalized < 600


def _retry_delay_seconds(
    attempt: int,
    response: Any = None,
    *,
    now_utc: Optional[datetime] = None,
) -> float:
    """Use a valid Retry-After value or bounded exponential backoff."""
    fallback = float(min(2 ** max(0, attempt), 8))
    headers = getattr(response, "headers", None)
    if headers is None:
        return fallback

    raw_retry_after = str(headers.get("retry-after") or headers.get("Retry-After") or "").strip()
    if not raw_retry_after:
        return fallback

    try:
        delay = float(raw_retry_after)
        if not math.isfinite(delay) or delay < 0:
            return fallback
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(raw_retry_after)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            current_time = now_utc or datetime.now(timezone.utc)
            if current_time.tzinfo is None:
                current_time = current_time.replace(tzinfo=timezone.utc)
            delay = max(0.0, (retry_at - current_time).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return fallback

    if delay > _MAX_RETRY_AFTER_SECONDS:
        return fallback
    return delay


class OpenAIChatTransport:
    """Small wrapper around the OpenAI client with repo-specific payload defaults."""

    def __init__(
        self,
        *,
        config: LLMConfig,
        agent_config: AgentConfig,
        openai_client: Optional[Any] = None,
    ) -> None:
        self.config = config
        self.agent_config = agent_config
        self._openai_client = openai_client
        self.provider_adapter = get_provider_adapter(getattr(config, "provider", None))

    @property
    def openai_client(self) -> Optional[Any]:
        return self._openai_client

    def build_payload(
        self,
        *,
        messages: List[Dict[str, Any]],
        enable_reasoning: bool,
        stream: bool,
        temperature: Optional[float],
        max_tokens: Optional[int],
        tools: Optional[List[Dict[str, Any]]],
        **kwargs: Any,
    ) -> Dict[str, Any]:
        extra_body: Dict[str, Any] = self.provider_adapter.extra_body(enable_reasoning=enable_reasoning)
        extra_body.update(kwargs)

        payload: Dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "stream": stream,
            "temperature": temperature if temperature is not None else self.config.temperature,
            "max_tokens": max_tokens if max_tokens is not None else self.config.max_tokens,
        }
        payload.update(self.provider_adapter.top_level_params(stream=stream))
        if extra_body:
            payload["extra_body"] = extra_body
        if tools:
            payload["tools"] = tools
        return payload

    def request(
        self,
        *,
        messages: List[Dict[str, Any]],
        stream: bool,
        temperature: Optional[float],
        max_tokens: Optional[int],
        tools: Optional[List[Dict[str, Any]]],
        enable_reasoning: bool,
        cancel_event: Optional[threading.Event] = None,
        **kwargs: Any,
    ) -> Any:
        payload = self.build_payload(
            messages=messages,
            stream=stream,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            enable_reasoning=enable_reasoning,
            **kwargs,
        )
        logger.info(
            "Making LLM request via OpenAI client to %s/chat/completions, messages=%d tools=%d stream=%s temperature=%s reasoning=%s",
            str(self.config.base_url or "").rstrip("/"),
            len(messages),
            len(tools or []),
            stream,
            temperature,
            enable_reasoning,
        )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "LLM request payload: %s",
                json.dumps(payload, ensure_ascii=False, default=str),
            )

        max_retries = max(0, int(self.agent_config.llm_connection_max_retries))
        max_attempts = max_retries + 1
        for attempt in range(max_attempts):
            try:
                return self._get_openai_client().chat.completions.create(**payload)
            except APIStatusError as exc:
                if not _is_retryable_status_code(exc.status_code) or attempt >= max_attempts - 1:
                    logger.error(
                        "LLM request failed with status %d after %d attempt(s)",
                        exc.status_code,
                        attempt + 1,
                    )
                    raise
                backoff = _retry_delay_seconds(attempt, exc.response)
                logger.warning(
                    "Retryable LLM status %d on attempt %d/%d; retrying in %.1fs",
                    exc.status_code,
                    attempt + 1,
                    max_attempts,
                    backoff,
                )
                self._wait_before_retry(backoff, cancel_event)
            except (APIConnectionError, APITimeoutError) as exc:
                if attempt >= max_attempts - 1:
                    logger.error(
                        "LLM connection failed after %d attempt(s): %s",
                        max_attempts,
                        type(exc).__name__,
                    )
                    raise
                backoff = _retry_delay_seconds(attempt)
                logger.warning(
                    "LLM connection error %s on attempt %d/%d; retrying in %.1fs",
                    type(exc).__name__,
                    attempt + 1,
                    max_attempts,
                    backoff,
                )
                self._wait_before_retry(backoff, cancel_event)

        raise RuntimeError("Request failed")

    @staticmethod
    def _wait_before_retry(
        delay_seconds: float,
        cancel_event: Optional[threading.Event],
    ) -> None:
        if cancel_event is not None and cancel_event.wait(delay_seconds):
            raise WorkflowCancelledError("Workflow cancelled by user")
        if cancel_event is None:
            threading.Event().wait(delay_seconds)

    def _get_openai_client(self) -> Any:
        if self._openai_client is None:
            self._openai_client = OpenAI(
                api_key=self.config.api_key or "",
                base_url=str(self.config.base_url or "").rstrip("/"),
                timeout=self.config.timeout,
                max_retries=0,
            )
        return self._openai_client
