"""openJiuwen callback to onto-agent observability bridge."""

from __future__ import annotations

import importlib
import asyncio
import time
from collections.abc import Mapping
from inspect import isawaitable
from typing import Any, Callable, Dict, Optional

from integration.observability import AgentRuntimeTrace
from llm.provider_adapters import get_provider_adapter
from utils.logging import get_logger
from .common import coerce_optional_int, normalize_arguments, object_to_dict, read_field
from .tools import set_jiuwen_pending_assistant_context


logger = get_logger("jiuwen.trace")


class JiuwenModelTraceBridge:
    """Bridge openJiuwen model callbacks into the repo observability facade.

    ``sliding_window_applied`` is a compatibility field.  It indicates that
    this project configured the Jiuwen-owned ContextEngine round window; it
    does not mean the project pre-trimmed the message list.
    """

    def __init__(
        self,
        *,
        runtime_trace: AgentRuntimeTrace,
        state: Mapping[str, Any],
        caller: str,
        iteration_provider: Any,
        model_provider: Optional[str],
        llm_config: Any = None,
        stream_enabled: bool,
        enable_reasoning: bool,
        event_class_provider: Any = None,
        model_timing_observer: Optional[Callable[[int, int], None]] = None,
        model_error_callback: Optional[Callable[[BaseException | None], None]] = None,
        model_completion_callback: Optional[Callable[[Mapping[str, Any]], None]] = None,
        prompt_build_ms: int = 0,
        system_prompt_chars: int = 0,
        sliding_window_applied: bool,
    ) -> None:
        self.runtime_trace = runtime_trace
        self.state = state
        self.caller = caller
        self.iteration_provider = iteration_provider
        self.model_provider = model_provider
        self.llm_config = llm_config
        self.stream_enabled = bool(stream_enabled)
        self.enable_reasoning = bool(enable_reasoning)
        self.event_class_provider = event_class_provider
        self.model_timing_observer = model_timing_observer
        self.model_error_callback = model_error_callback
        self.model_completion_callback = model_completion_callback
        self.prompt_build_ms = max(0, int(prompt_build_ms or 0))
        self.system_prompt_chars = max(0, int(system_prompt_chars or 0))
        # Keep the legacy exported name, but store only the static ownership
        # signal documented above?not a claim that messages were mutated.
        self.sliding_window_applied = bool(sliding_window_applied)
        self._manager: Any = None
        self._registered: list[tuple[Any, Any]] = []
        self._active_calls: Dict[int, Dict[str, Any]] = {}
        self._call_counts: Dict[int, int] = {}

    async def register(self, agent: Any) -> bool:
        if (
            not self.runtime_trace.enabled
            and self.model_timing_observer is None
            and self.model_completion_callback is None
        ):
            return False
        manager = getattr(agent, "agent_callback_manager", None)
        register_callback = getattr(manager, "register_callback", None)
        if not callable(register_callback):
            return False
        event_provider = self.event_class_provider or jiuwen_agent_callback_event_class
        event_cls = event_provider()
        if event_cls is None:
            return False
        callbacks = [
            (getattr(event_cls, "BEFORE_MODEL_CALL", None), self._on_before_model_call),
            (getattr(event_cls, "AFTER_MODEL_CALL", None), self._on_after_model_call),
            (getattr(event_cls, "ON_MODEL_EXCEPTION", None), self._on_model_exception),
        ]
        self._manager = manager
        for event, callback in callbacks:
            if event is None:
                continue
            result = register_callback(event, callback, priority=-1000)
            if isawaitable(result):
                await result
            self._registered.append((event, callback))
        return bool(self._registered)

    async def unregister(self) -> None:
        manager = self._manager
        unregister = getattr(manager, "unregister", None)
        if not callable(unregister):
            self._registered = []
            return
        for event, callback in reversed(self._registered):
            try:
                result = unregister(event, callback)
                if isawaitable(result):
                    await result
            except Exception as exc:
                logger.warning("Jiuwen callback unregister failed: %s", exc, exc_info=True)
        self._registered = []

    async def _on_before_model_call(self, ctx: Any) -> None:
        if self.model_error_callback is not None:
            self.model_error_callback(None)
        iteration = self._current_iteration()
        call_index = self._next_call_index(iteration)
        inputs = read_field(ctx, "inputs")
        messages = jiuwen_messages_to_dicts(read_field(inputs, "messages") or [])
        tools = jiuwen_tools_to_dicts(read_field(inputs, "tools") or [])
        model_name = self._model_name_from_context(ctx)
        started_at = time.perf_counter()
        self._active_calls[id(ctx)] = {
            "iteration": iteration,
            "call_index": call_index,
            "started_at": started_at,
            "model": model_name,
            "messages": messages,
            "tools": tools,
        }
        self.runtime_trace.llm_started(
            iteration=iteration,
            call_index=call_index,
            request_label="jiuwen_react_model_call",
            messages=messages,
            tools=tools,
            model=model_name,
            api_params=self._build_api_params(model=model_name, tools_count=len(tools)),
        )

    async def on_before_model_call_for_test(self, ctx: Any) -> None:
        """Run the registered pre-model callback through a public test boundary."""
        await self._on_before_model_call(ctx)

    async def _on_after_model_call(self, ctx: Any) -> None:
        if read_field(ctx, "exception") is not None:
            return
        record = self._active_calls.pop(id(ctx), None)
        if record is None:
            record = self._start_missing_record(ctx)
        inputs = read_field(ctx, "inputs")
        response = read_field(inputs, "response")
        messages = jiuwen_messages_to_dicts(
            read_field(inputs, "messages") or record.get("messages") or []
        )
        tool_infos = jiuwen_tools_to_dicts(
            read_field(inputs, "tools") or record.get("tools") or []
        )
        tool_calls = jiuwen_tool_calls_to_dicts(read_field(response, "tool_calls") or [])
        if tool_calls:
            set_jiuwen_pending_assistant_context(
                self.state,
                content=str(read_field(response, "content") or ""),
                reasoning_content=str(read_field(response, "reasoning_content") or ""),
            )
        elapsed_ms = elapsed_ms_since(record.get("started_at"))
        self._notify_model_timing(int(record["iteration"]), elapsed_ms)
        normalized_usage = get_provider_adapter(self.model_provider).normalize_usage(
            jiuwen_usage_to_dict(read_field(response, "usage_metadata"))
        )
        self._notify_model_completion(
            {
                "iteration": int(record["iteration"]),
                "call_index": int(record["call_index"]),
                "finish_reason": read_field(response, "finish_reason"),
                "has_tool_calls": bool(tool_calls),
                "usage": normalized_usage,
            }
        )
        model_name = resolved_jiuwen_model_name(ctx, self.llm_config, preferred=record.get("model"))
        self.runtime_trace.llm_finished(
            iteration=int(record["iteration"]),
            call_index=int(record["call_index"]),
            usage=normalized_usage,
            prompt_text=lambda: "\n".join(str(message.get("content", "")) for message in messages),
            completion_text=lambda: str(read_field(response, "content") or ""),
            reasoning_text=lambda: str(read_field(response, "reasoning_content") or ""),
            tool_calls=tool_calls,
            finish_reason=read_field(response, "finish_reason"),
            response_model=model_name,
            duration_ms=elapsed_ms,
            stream_duration_ms=elapsed_ms,
            wall_duration_ms=elapsed_ms,
            request_label="jiuwen_react_model_call",
            model=model_name,
            model_provider=self.model_provider,
            tool_names=tool_names_from_calls_or_infos(tool_calls, tool_infos),
        )

    async def on_after_model_call_for_test(self, ctx: Any) -> None:
        """Run the registered post-model callback through a public test boundary."""
        await self._on_after_model_call(ctx)

    def _notify_model_completion(self, completion: Mapping[str, Any]) -> None:
        if self.model_completion_callback is None:
            return
        try:
            self.model_completion_callback(dict(completion))
        except Exception:
            logger.warning("Jiuwen model completion observer failed", exc_info=True)

    async def _on_model_exception(self, ctx: Any) -> None:
        record = self._active_calls.pop(id(ctx), None)
        if record is None:
            record = self._start_missing_record(ctx)
        exc = read_field(ctx, "exception")
        if (
            isinstance(exc, BaseException)
            and not isinstance(exc, asyncio.CancelledError)
            and self.model_error_callback is not None
        ):
            self.model_error_callback(exc)
        self._notify_model_timing(
            int(record["iteration"]),
            elapsed_ms_since(record.get("started_at")),
        )
        self.runtime_trace.llm_failed(
            iteration=int(record["iteration"]),
            call_index=int(record["call_index"]),
            error_type=type(exc).__name__ if exc is not None else None,
            error_message=str(exc or ""),
        )

    def _notify_model_timing(self, iteration: int, elapsed_ms: int) -> None:
        if self.model_timing_observer is None:
            return
        try:
            self.model_timing_observer(
                max(1, int(iteration or 1)),
                max(0, int(elapsed_ms or 0)),
            )
        except Exception:
            logger.warning("Jiuwen model timing observer failed", exc_info=True)

    def _current_iteration(self) -> int:
        if callable(self.iteration_provider):
            return max(1, int(self.iteration_provider()))
        return 1

    def current_call_index(self, iteration: int) -> int:
        return max(1, int(self._call_counts.get(max(1, int(iteration or 1))) or 1))

    def _next_call_index(self, iteration: int) -> int:
        count = int(self._call_counts.get(iteration) or 0) + 1
        self._call_counts[iteration] = count
        return count

    def _build_api_params(self, *, model: str, tools_count: int) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "model": model,
            "stream": self.stream_enabled,
            "enable_reasoning": self.enable_reasoning,
            "tools_count": tools_count,
            "request_label": "jiuwen_react_model_call",
            "sliding_window_applied": self.sliding_window_applied,
            "system_prompt_chars": self.system_prompt_chars,
            "prompt_build_ms": self.prompt_build_ms,
            "source": "openjiuwen_agent_callback",
        }
        llm_config = self.llm_config
        if llm_config is None:
            return params
        base_url = str(getattr(llm_config, "base_url", "") or "").rstrip("/")
        if base_url:
            params["base_url"] = base_url
        for key in ("temperature", "max_tokens", "timeout"):
            value = getattr(llm_config, key, None)
            if value is not None:
                params[key] = value
        return params

    def _model_name_from_context(self, ctx: Any) -> str:
        return resolved_jiuwen_model_name(ctx, self.llm_config)

    def _start_missing_record(self, ctx: Any) -> Dict[str, Any]:
        iteration = self._current_iteration()
        call_index = self._next_call_index(iteration)
        inputs = read_field(ctx, "inputs")
        return {
            "iteration": iteration,
            "call_index": call_index,
            "started_at": time.perf_counter(),
            "model": self._model_name_from_context(ctx),
            "messages": jiuwen_messages_to_dicts(read_field(inputs, "messages") or []),
            "tools": jiuwen_tools_to_dicts(read_field(inputs, "tools") or []),
        }


def jiuwen_agent_callback_event_class() -> Any:
    try:
        rail_base_module = importlib.import_module("openjiuwen.core.single_agent.rail.base")
    except ImportError:
        return None
    return getattr(rail_base_module, "AgentCallbackEvent", None)


def jiuwen_messages_to_dicts(messages: Any) -> list[Dict[str, Any]]:
    normalized: list[Dict[str, Any]] = []
    for message in list(messages or []):
        payload = object_to_dict(message)
        role = str(payload.get("role") or read_field(message, "role") or "unknown")
        content = payload.get("content", read_field(message, "content", ""))
        normalized.append(
            {
                **payload,
                "role": role,
                "content": "" if content is None else str(content),
            }
        )
    return normalized


def jiuwen_tools_to_dicts(tools: Any) -> list[Dict[str, Any]]:
    normalized: list[Dict[str, Any]] = []
    for tool in list(tools or []):
        payload = object_to_dict(tool)
        if payload:
            normalized.append(payload)
            continue
        name = str(read_field(tool, "name") or "").strip()
        normalized.append(
            {
                "name": name,
                "description": str(read_field(tool, "description") or ""),
                "parameters": read_field(tool, "parameters") or read_field(tool, "input_params") or {},
            }
        )
    return normalized


def jiuwen_tool_calls_to_dicts(tool_calls: Any) -> list[Dict[str, Any]]:
    normalized: list[Dict[str, Any]] = []
    for tool_call in list(tool_calls or []):
        try:
            normalized.append(jiuwen_tool_call_to_trace_tool_call(tool_call))
            continue
        except Exception as exc:
            logger.debug("Jiuwen tool call normalization failed: %s", exc, exc_info=True)
            payload = object_to_dict(tool_call)
        if payload:
            normalized.append(payload)
    return normalized


def jiuwen_tool_call_to_trace_tool_call(tool_call: Any) -> Dict[str, Any]:
    name = str(read_field(tool_call, "name") or "").strip()
    if not name:
        raise ValueError("Jiuwen tool call name is required")

    converted: Dict[str, Any] = {
        "id": read_field(tool_call, "id"),
        "type": str(read_field(tool_call, "type") or "function"),
        "function": {
            "name": name,
            "arguments": normalize_arguments(read_field(tool_call, "arguments")),
        },
    }
    index = read_field(tool_call, "index")
    if index is not None:
        converted["index"] = index
    return converted


def jiuwen_usage_to_dict(usage: Any) -> Dict[str, Any]:
    payload = object_to_dict(usage)
    if not payload:
        return {}
    if "input_tokens" in payload and "prompt_tokens" not in payload:
        payload["prompt_tokens"] = payload.get("input_tokens")
    if "output_tokens" in payload and "completion_tokens" not in payload:
        payload["completion_tokens"] = payload.get("output_tokens")
    cache_tokens = coerce_optional_int(payload.get("cache_tokens"))
    prompt_tokens = coerce_optional_int(payload.get("prompt_tokens"))
    if (
        cache_tokens is not None
        and cache_tokens > 0
        and "cache_hit_tokens" not in payload
    ):
        payload["cache_hit_tokens"] = cache_tokens
        if prompt_tokens is not None and prompt_tokens > 0:
            payload["cache_total_input_tokens"] = prompt_tokens
            payload.setdefault("cache_miss_tokens", max(0, prompt_tokens - cache_tokens))
            payload["cache_hit_rate"] = cache_tokens / prompt_tokens
    return payload


def jiuwen_model_name_from_context(ctx: Any) -> str:
    agent = read_field(ctx, "agent")
    config = read_field(agent, "_config")
    return str(
        read_field(config, "model_name")
        or read_field(config, "model")
        or "unknown"
    )


def resolved_jiuwen_model_name(ctx: Any, llm_config: Any = None, *, preferred: Any = None) -> str:
    for candidate in (
        preferred,
        jiuwen_model_name_from_context(ctx),
        read_field(llm_config, "model_name"),
        read_field(llm_config, "model"),
        read_field(llm_config, "deployment"),
        read_field(llm_config, "model_id"),
    ):
        value = str(candidate or "").strip()
        if value and value.lower() != "unknown":
            return value
    return "unknown"


def tool_names_from_calls_or_infos(
    tool_calls: list[Mapping[str, Any]],
    tool_infos: list[Mapping[str, Any]],
) -> list[str]:
    names: list[str] = []
    for call in tool_calls:
        function = call.get("function") if isinstance(call, Mapping) else None
        name = ""
        if isinstance(function, Mapping):
            name = str(function.get("name") or "").strip()
        if not name and isinstance(call, Mapping):
            name = str(call.get("name") or "").strip()
        if name:
            names.append(name)
    if names:
        return names
    for tool in tool_infos:
        name = str(tool.get("name") if isinstance(tool, Mapping) else "").strip()
        if name:
            names.append(name)
    return names


def elapsed_ms_since(started_at: Any) -> int:
    try:
        return max(0, int((time.perf_counter() - float(started_at)) * 1000))
    except (TypeError, ValueError):
        return 0
