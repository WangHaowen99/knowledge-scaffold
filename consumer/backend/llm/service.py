"""LLM service for making API calls to language models."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

from agent.exceptions import WorkflowCancelledError
from config import AgentConfig, LLMConfig
from integration.observability import get_observability_context
from llm.collectors import collect_non_stream_response, collect_stream_response
from llm.reasoning_summary import summarize_reasoning_intent
from llm.tool_calls import normalize_tool_calls
from llm.transport import OpenAIChatTransport
from llm.types import LLMResponse
from utils.logging import get_logger

logger = get_logger(__name__)


class LLMService:
    """
    HTTP client for making LLM API calls.

    Supports OpenAI-compatible APIs (OpenAI, Azure OpenAI, Ollama, vLLM, etc.)
    """

    def __init__(
        self,
        config: Optional[LLMConfig] = None,
        agent_config: Optional[AgentConfig] = None,
        summary_llm: Optional["LLMService"] = None,
        cancel_lock: Optional[Any] = None,
        cancel_events: Optional[Dict[str, threading.Event]] = None,
        reasoning_summary_executor: Optional[ThreadPoolExecutor] = None,
        openai_client: Optional[Any] = None,
    ) -> None:
        """
        Initialize LLM service.

        Args:
            config: LLM configuration. If None, loads from environment.
            agent_config: Agent configuration. If None, uses defaults.
        """
        self.config = config or LLMConfig.from_env()
        self.agent_config = agent_config or AgentConfig.from_env()
        self.summary_llm = summary_llm
        self.transport = OpenAIChatTransport(
            config=self.config,
            agent_config=self.agent_config,
            openai_client=openai_client,
        )
        self._cancel_lock = cancel_lock or threading.Lock()
        self._cancel_events = cancel_events if cancel_events is not None else {}
        self._reasoning_summary_executor = reasoning_summary_executor or ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="reasoning-summary",
        )

    def with_config(
        self,
        config: Optional[LLMConfig] = None,
        *,
        agent_config: Optional[AgentConfig] = None,
    ) -> "LLMService":
        """Create a sibling service that shares cancellation state."""
        resolved_config = config or self.config
        resolved_agent_config = agent_config or self.agent_config
        return LLMService(
            config=resolved_config,
            agent_config=resolved_agent_config,
            summary_llm=self.summary_llm,
            cancel_lock=self._cancel_lock,
            cancel_events=self._cancel_events,
            reasoning_summary_executor=self._reasoning_summary_executor,
            openai_client=(self.transport.openai_client if resolved_config is self.config else None),
        )

    @property
    def is_configured(self) -> bool:
        """Check if the service is properly configured."""
        return bool(self.config.base_url and self.config.model)

    def cancel_run(self, run_id: str) -> None:
        """Signal cancellation for a run to in-flight LLM calls."""
        if not run_id:
            return
        with self._cancel_lock:
            event = self._cancel_events.get(run_id)
            if not event:
                event = threading.Event()
                self._cancel_events[run_id] = event
            event.set()

    def clear_run(self, run_id: str) -> None:
        """Clear cancellation tracking for a completed run."""
        if not run_id:
            return
        with self._cancel_lock:
            self._cancel_events.pop(run_id, None)

    def set_cancel_event_for_test(self, run_id: str, event: threading.Event) -> None:
        with self._cancel_lock:
            self._cancel_events[run_id] = event

    def get_cancel_event_for_test(
        self,
        run_id: Optional[str],
    ) -> Optional[threading.Event]:
        """Resolve a cancellation event through the production service path."""
        return self._get_cancel_event(run_id)

    def collect_non_stream_response_for_test(self, response: Any, **kwargs: Any) -> Dict[str, Any]:
        return self._collect_non_stream_response(response, **kwargs)

    def collect_response_for_test(self, response: Any, **kwargs: Any) -> Dict[str, Any]:
        return self._collect_response(response, **kwargs)

    def _get_cancel_event(self, run_id: Optional[str]) -> Optional[threading.Event]:
        """Return the cancel event for ``run_id``, creating one on first touch.

        The event must be registered before the in-flight request begins so
        that a subsequent ``cancel_run`` call — which may arrive mid-request —
        mutates the same event the active request holds a reference to.
        Cleanup is the caller's responsibility via ``clear_run``.
        """
        if not run_id:
            return None
        with self._cancel_lock:
            event = self._cancel_events.get(run_id)
            if not event:
                event = threading.Event()
                self._cancel_events[run_id] = event
            return event

    @staticmethod
    def _raise_if_cancelled(
        cancel_event: Optional[threading.Event],
        run_id: Optional[str],
    ) -> None:
        if cancel_event and cancel_event.is_set():
            raise WorkflowCancelledError(
                "Workflow cancelled by user",
                details={"run_id": run_id},
            )

    def _dispatch_reasoning_summary(
        self,
        *,
        reasoning_text: str,
        reasoning_block_id: str,
        run_id: Optional[str],
        reasoning_summary_callback: Optional[Callable[[str, str], None]],
    ) -> bool:
        """Schedule UI-only reasoning summaries without blocking the main response path."""
        text = str(reasoning_text or "").strip()
        if not text or not reasoning_block_id or not reasoning_summary_callback:
            return False

        summary_llm = self.summary_llm or self
        observability_context = get_observability_context()

        def run_summary_task() -> None:
            try:
                summary = summarize_reasoning_intent(
                    summary_llm,
                    text,
                    run_id=run_id,
                    observer=observability_context.observer,
                    session_id=observability_context.session_id,
                    turn_id=observability_context.turn_id,
                    parent_key=observability_context.parent_key,
                    span_key="reasoning_summary:{}".format(reasoning_block_id),
                )
                if not summary:
                    return
                reasoning_summary_callback(reasoning_block_id, summary)
            except Exception:
                logger.warning("Failed to emit reasoning summary callback", exc_info=True)

        try:
            self._reasoning_summary_executor.submit(run_summary_task)
            return True
        except RuntimeError:
            logger.warning("Failed to submit reasoning summary task", exc_info=True)
            return False

    @staticmethod
    def _normalize_tool_calls(tool_calls: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
        return normalize_tool_calls(tool_calls)

    @staticmethod
    def _extract_structured_payload(content: str) -> Any:
        """Extract a JSON payload from a model response."""
        text = str(content or "").strip()
        if not text:
            raise ValueError("empty structured response")
        return json.loads(text)

    @staticmethod
    def _validate_response_model(response_model: Any, payload: Any) -> Any:
        if hasattr(response_model, "model_validate"):
            return response_model.model_validate(payload)
        if hasattr(response_model, "parse_obj"):
            return response_model.parse_obj(payload)
        return response_model(**payload)

    def _make_request(  # pylint: disable=too-many-arguments,huawei-too-many-arguments
        self,
        messages: List[Dict[str, Any]],
        stream: bool,
        temperature: Optional[float],
        max_tokens: Optional[int],
        tools: Optional[List[Dict[str, Any]]],
        enable_reasoning: bool,
        cancel_event: Optional[threading.Event] = None,
        **kwargs: Any,
    ) -> Any:
        """Test seam — production calls ``transport.request`` directly."""
        return self.transport.request(
            messages=messages,
            stream=stream,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            enable_reasoning=enable_reasoning,
            cancel_event=cancel_event,
            **kwargs,
        )

    def _collect_response(  # pylint: disable=too-many-arguments,huawei-too-many-arguments
        self,
        response: Any,
        emit_func: Optional[Callable[..., None]] = None,
        run_id: Optional[str] = None,
        reasoning_summary_callback: Optional[Callable[[str, str], None]] = None,
        request_label: Optional[str] = None,
        tool_calls_closed_callback: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
    ) -> Dict[str, Any]:
        """Test seam that adapts SDK stream objects or non-stream payloads."""
        if hasattr(response, "model_dump"):
            return self._collect_non_stream_response(
                response,
                emit_func=emit_func,
                run_id=run_id,
                reasoning_summary_callback=reasoning_summary_callback,
                request_label=request_label,
            )
        return self._collect_stream_response(
            response,
            emit_func=emit_func,
            run_id=run_id,
            reasoning_summary_callback=reasoning_summary_callback,
            request_label=request_label,
            tool_calls_closed_callback=tool_calls_closed_callback,
        )

    def _collect_non_stream_response(
        self,
        response: Any,
        emit_func: Optional[Callable[..., None]] = None,
        run_id: Optional[str] = None,
        reasoning_summary_callback: Optional[Callable[[str, str], None]] = None,
        request_label: Optional[str] = None,
    ) -> Dict[str, Any]:
        dispatch = self._dispatch_reasoning_summary

        def _dispatch_wrapper(*, reasoning_text, reasoning_block_id, run_id, reasoning_summary_callback):
            return dispatch(
                reasoning_text=reasoning_text,
                reasoning_block_id=reasoning_block_id,
                run_id=run_id,
                reasoning_summary_callback=reasoning_summary_callback,
            )

        return collect_non_stream_response(
            response,
            emit_func=emit_func,
            run_id=run_id,
            reasoning_summary_callback=reasoning_summary_callback,
            request_label=request_label,
            dispatch_reasoning_summary=_dispatch_wrapper,
        )

    def _collect_stream_response(  # pylint: disable=too-many-arguments,huawei-too-many-arguments
        self,
        response: Any,
        emit_func: Optional[Callable[..., None]] = None,
        run_id: Optional[str] = None,
        reasoning_summary_callback: Optional[Callable[[str, str], None]] = None,
        request_label: Optional[str] = None,
        tool_calls_closed_callback: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
    ) -> Dict[str, Any]:
        dispatch = self._dispatch_reasoning_summary

        def _dispatch_wrapper(*, reasoning_text, reasoning_block_id, run_id, reasoning_summary_callback):
            return dispatch(
                reasoning_text=reasoning_text,
                reasoning_block_id=reasoning_block_id,
                run_id=run_id,
                reasoning_summary_callback=reasoning_summary_callback,
            )

        return collect_stream_response(
            response,
            emit_func=emit_func,
            run_id=run_id,
            reasoning_summary_callback=reasoning_summary_callback,
            request_label=request_label,
            tool_calls_closed_callback=tool_calls_closed_callback,
            dispatch_reasoning_summary=_dispatch_wrapper,
        )

    def collect_stream_response_for_test(
        self,
        response: Any,
        emit_func: Optional[Callable[..., None]] = None,
        run_id: Optional[str] = None,
        reasoning_summary_callback: Optional[Callable[[str, str], None]] = None,
        request_label: Optional[str] = None,
        tool_calls_closed_callback: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
    ) -> Dict[str, Any]:
        """Collect a stream through the production path for contract tests."""
        return self._collect_stream_response(
            response,
            emit_func=emit_func,
            run_id=run_id,
            reasoning_summary_callback=reasoning_summary_callback,
            request_label=request_label,
            tool_calls_closed_callback=tool_calls_closed_callback,
        )

    def _execute_single_request(
        self,
        messages: List[Dict[str, Any]],
        *,
        tools: Optional[List[Dict[str, Any]]],
        stream: Optional[bool],
        temperature: Optional[float],
        max_tokens: Optional[int],
        enable_reasoning: bool,
        run_id: Optional[str],
        cancel_event: Optional[threading.Event],
        emit_func: Optional[Callable[..., None]] = None,
        reasoning_summary_callback: Optional[Callable[[str, str], None]] = None,
        request_label: Optional[str] = None,
        tool_calls_closed_callback: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
        **kwargs: Any,
    ) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """Execute a single LLM request and append the assistant message."""
        self._raise_if_cancelled(cancel_event, run_id)
        use_stream = True if stream is None else bool(stream)
        request_started_at = time.perf_counter()
        resolved_temperature = temperature if temperature is not None else self.config.temperature
        output_limit = max(1, int(self.agent_config.limits.model_output_token_limit))
        requested_max_tokens = max_tokens if max_tokens is not None else self.config.max_tokens
        resolved_max_tokens = min(max(1, int(requested_max_tokens)), output_limit)
        api_params = {
            "model": self.config.model,
            "stream": use_stream,
            "temperature": resolved_temperature,
            "max_tokens": resolved_max_tokens,
            "enable_reasoning": enable_reasoning,
            "request_label": request_label or "chat_completion",
            "tools_count": len(tools or []),
            "extra_body": {
                **self.transport.provider_adapter.extra_body(enable_reasoning=enable_reasoning),
                **dict(kwargs or {}),
            },
        }

        response = self._make_request(
            messages=messages,
            stream=use_stream,
            temperature=temperature,
            max_tokens=resolved_max_tokens,
            tools=tools,
            enable_reasoning=enable_reasoning,
            cancel_event=cancel_event,
            **kwargs,
        )
        response_headers_ms = int((time.perf_counter() - request_started_at) * 1000)

        collected = self._collect_response(
            response,
            emit_func=emit_func,
            run_id=run_id,
            reasoning_summary_callback=reasoning_summary_callback,
            request_label=request_label,
            tool_calls_closed_callback=tool_calls_closed_callback,
        )
        collected["usage"] = self.transport.provider_adapter.normalize_usage(
            collected.get("usage") if isinstance(collected.get("usage"), dict) else {}
        )
        total_elapsed_ms = int((time.perf_counter() - request_started_at) * 1000)
        collected["request_params"] = api_params
        collected["response_headers_ms"] = response_headers_ms
        collected["duration_ms"] = total_elapsed_ms
        self._raise_if_cancelled(cancel_event, run_id)

        messages.append(collected["message"])
        logger.info("LLM result: %s", collected["message"])
        return collected, messages

    def call(
        self,
        messages: List[Dict[str, Any]],
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        stream: Optional[bool] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        enable_reasoning: bool = True,
        run_id: Optional[str] = None,
        emit_func: Optional[Callable[..., None]] = None,
        reasoning_summary_callback: Optional[Callable[[str, str], None]] = None,
        request_label: Optional[str] = None,
        tool_calls_closed_callback: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
        response_model: Optional[Any] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """
        Unified LLM call entry point.

        All requests return a collected response object.

        Args:
            messages: List of chat messages
            tools: Optional list of tool definitions (OpenAI format)
            stream: Whether to request upstream streaming. Defaults to True.
            temperature: Sampling temperature (overrides config)
            max_tokens: Maximum tokens to generate (overrides config)
            enable_reasoning: Whether to enable reasoning mode (default: True)
            run_id: Optional run identifier for cancellation
            emit_func: Optional callback for streaming tokens.
            reasoning_summary_callback: Optional callback called when a reasoning
                block is fully collected. Signature: (reasoning_block_id, summary)
            tool_calls_closed_callback: Optional callback invoked whenever one or
                more streamed tool calls become complete enough to execute.
            response_model: Optional Pydantic-style model class for structured JSON parsing.
            **kwargs: Additional parameters to pass to the API

        Returns:
            LLMResponse
        """
        if not self.is_configured:
            raise RuntimeError(
                "LLM service not configured. Set LLM_BASE_URL and LLM_MODEL, or save an enabled model profile in system"
                " config."
            )

        cancel_event = self._get_cancel_event(run_id)
        self._raise_if_cancelled(cancel_event, run_id)

        request_stream = False if response_model is not None and stream is None else stream

        collected, result_messages = self._execute_single_request(
            messages.copy(),
            tools=tools,
            stream=request_stream,
            temperature=temperature,
            max_tokens=max_tokens,
            enable_reasoning=enable_reasoning,
            run_id=run_id,
            cancel_event=cancel_event,
            emit_func=emit_func,
            reasoning_summary_callback=reasoning_summary_callback,
            request_label=request_label,
            tool_calls_closed_callback=tool_calls_closed_callback,
            **kwargs,
        )

        content = collected["message"].get("content", "")
        structured_data = None
        if response_model is not None:
            payload = self._extract_structured_payload(content)
            structured_data = self._validate_response_model(response_model, payload)

        response_message = collected.get("message", {})
        return LLMResponse(
            content=content,
            model=collected.get("model") or self.config.model or "",
            finish_reason=collected.get("finish_reason"),
            reasoning_content=response_message.get("reasoning_content"),
            messages=result_messages,
            tool_calls=response_message.get("tool_calls"),
            data=structured_data,
            usage=collected.get("usage"),
            request_params=collected.get("request_params"),
            response_headers_ms=collected.get("response_headers_ms"),
            duration_ms=collected.get("duration_ms"),
            first_reasoning_ms=collected.get("first_reasoning_ms"),
            first_content_ms=collected.get("first_content_ms"),
            first_tool_call_ms=collected.get("first_tool_call_ms"),
        )
