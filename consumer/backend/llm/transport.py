"""OpenAI-compatible chat completion transport."""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Dict, List, Optional

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

from config import AgentConfig, LLMConfig
from agent.exceptions import WorkflowCancelledError
from utils.logging import get_logger
from llm.provider_adapters import get_provider_adapter


logger = get_logger(__name__)


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
        extra_body: Dict[str, Any] = self.provider_adapter.extra_body(
            enable_reasoning=enable_reasoning
        )
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

        max_retries = self.agent_config.llm_connection_max_retries
        for attempt in range(max_retries):
            try:
                return self._get_openai_client().chat.completions.create(**payload)
            except APIStatusError as exc:
                logger.error(
                    "LLM error %d: %s",
                    exc.status_code,
                    getattr(exc.response, "text", str(exc)),
                )
                raise
            except (APIConnectionError, APITimeoutError) as exc:
                if attempt >= max_retries - 1:
                    logger.error("LLM connection failed after %d attempts: %s", max_retries, exc)
                    raise
                backoff = min(2 ** attempt, 8)
                logger.warning(
                    "LLM connection error on attempt %d: %s. Retrying in %ds...",
                    attempt + 1, exc, backoff,
                )
                if cancel_event is not None and cancel_event.wait(backoff):
                    raise WorkflowCancelledError("Workflow cancelled by user")
                elif cancel_event is None:
                    threading.Event().wait(backoff)

        raise RuntimeError("Request failed")

    def _get_openai_client(self) -> Any:
        if self._openai_client is None:
            self._openai_client = OpenAI(
                api_key=self.config.api_key or "",
                base_url=str(self.config.base_url or "").rstrip("/"),
                timeout=self.config.timeout,
                max_retries=0,
            )
        return self._openai_client
