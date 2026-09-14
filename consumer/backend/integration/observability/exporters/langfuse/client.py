"""Low-level Langfuse SDK adapter."""

from __future__ import annotations

import re
import threading
from typing import Any, Dict, Mapping, Optional

from utils.logging import get_logger

logger = get_logger("observability.exporters.langfuse.client")

_SENSITIVE_PATTERNS = {
    "phone": re.compile(r"1[3-9]\d{9}"),
    "id_card": re.compile(r"\d{17}[\dXx]|\d{15}"),
    "email": re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
    "url": re.compile(r"https?://[^\s]+"),
    "ip": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    "mac": re.compile(r"([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})"),
}

_SENSITIVE_FIELD_KEYWORDS = [
    "password",
    "secret",
    "token",
    "key",
    "api_key",
    "access_token",
    "secret_key",
    "private_key",
    "credential",
    "certificate",
    "phone",
    "mobile",
    "tel",
    "email",
    "id_card",
    "identity",
    "ssn",
    "address",
    "location",
    "ip",
    "mac",
]


def _mask_text(text: str) -> str:
    if not isinstance(text, str):
        return str(text)

    result = text
    for pattern_name, pattern in _SENSITIVE_PATTERNS.items():
        if pattern_name == "phone":
            result = pattern.sub(r"1*** **** ****", result)
        elif pattern_name == "id_card":
            result = pattern.sub(r"*****************", result)
        elif pattern_name == "email":
            result = pattern.sub(r"***@***.***", result)
        elif pattern_name == "url":
            result = pattern.sub(r"[URL]", result)
        elif pattern_name == "ip":
            result = pattern.sub(r"***.***.***.***", result)
        elif pattern_name == "mac":
            result = pattern.sub(r"**:**:**:**:**:**", result)

    return result


def _is_sensitive_key(key: str) -> bool:
    if not isinstance(key, str):
        return False
    lower_key = key.lower()
    return any(keyword in lower_key for keyword in _SENSITIVE_FIELD_KEYWORDS)


def _sanitize_value(key: str, value: Any) -> Any:
    if _is_sensitive_key(key):
        return "[REDACTED]"
    if isinstance(value, str):
        return _mask_text(value)
    if isinstance(value, dict):
        return _sanitize_dict(value)
    if isinstance(value, list):
        return [_sanitize_value(str(index), item) for index, item in enumerate(value)]
    return value


def _sanitize_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    return {key: _sanitize_value(key, value) for key, value in data.items()}


def _coerce_text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def create_langfuse_client(config: Mapping[str, Any]) -> Optional[Any]:
    """Create a Langfuse SDK client from an explicit config mapping."""
    if not config.get("enabled", False):
        return None

    public_key = _coerce_text(config.get("public_key"))
    secret_key = _coerce_text(config.get("secret_key"))
    base_url = _coerce_text(config.get("base_url"))
    if not public_key or not secret_key or not base_url:
        logger.warning("Langfuse enabled but missing public_key, secret_key, or base_url")
        return None
    langfuse_class = _load_langfuse_class()
    if langfuse_class is None:
        logger.warning("Langfuse SDK is not installed")
        return None

    try:
        return langfuse_class(
            public_key=public_key,
            secret_key=secret_key,
            host=base_url,
            debug=bool(config.get("debug", False)),
        )
    except Exception as exc:
        logger.error("Failed to initialize Langfuse client: %s", exc)
        return None


def _load_langfuse_class() -> Optional[Any]:
    try:
        from langfuse import Langfuse

        return Langfuse
    except ImportError:
        return None


class LangfuseTraceAdapter:
    """Stateful adapter around one Langfuse client instance.

    This layer owns SDK calls, best-effort sanitization, active observation
    storage, and flush/close behavior. It does not know agent-loop semantics.
    """

    def __init__(self, client: Optional[Any]) -> None:
        self._client = client
        self._active_traces: Dict[str, Dict[str, Any]] = {}
        self._active_traces_lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self._client is not None

    def create_trace(  # pylint: disable=too-many-arguments,huawei-too-many-arguments
        self,
        name: str,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        tags: Optional[list] = None,
        input: Optional[Dict[str, Any]] = None,  # pylint: disable=redefined-builtin
        trace_id: Optional[str] = None,
    ) -> Optional[str]:
        if not self._client:
            return None

        try:
            trace = self._client.trace(
                id=trace_id,
                name=name,
                user_id=user_id,
                session_id=session_id,
                metadata=_sanitize_dict(metadata or {}),
                tags=tags or [],
                input=_sanitize_dict(input or {}),
            )
            resolved_trace_id = trace.id
            with self._active_traces_lock:
                self._active_traces[resolved_trace_id] = {
                    "trace": trace,
                    "spans": {},
                }
            return resolved_trace_id
        except Exception as exc:
            logger.error("Failed to create trace: %s", exc)
            return None

    def get_trace(self, trace_id: str) -> Optional[Any]:
        with self._active_traces_lock:
            entry = self._active_traces.get(trace_id)
            return entry["trace"] if entry else None

    def seed_trace_for_test(self, trace_id: str, trace: Any) -> None:
        with self._active_traces_lock:
            self._active_traces[trace_id] = {"trace": trace, "spans": {}}

    def trace_span_ids_for_test(self, trace_id: str) -> set[str]:
        with self._active_traces_lock:
            entry = self._active_traces.get(trace_id)
            return set((entry or {}).get("spans", {}))

    def has_trace_for_test(self, trace_id: str) -> bool:
        with self._active_traces_lock:
            return trace_id in self._active_traces

    def update_trace_output(self, trace_id: str, output: Dict[str, Any]) -> None:
        try:
            with self._active_traces_lock:
                entry = self._active_traces.get(trace_id)
                if not entry:
                    return
                trace = entry["trace"]

            trace.update(output=_sanitize_dict(output))
            if self._is_terminal_status(output):
                self._mark_trace_terminal(trace_id)
                self._release_trace_if_terminal_and_idle(trace_id)
        except Exception as exc:
            logger.error("Failed to update trace output: %s", exc)

    def create_span(  # pylint: disable=too-many-arguments,huawei-too-many-arguments
        self,
        trace_id: str,
        name: str,
        metadata: Optional[Dict[str, Any]] = None,
        tags: Optional[list] = None,
        parent_observation_id: Optional[str] = None,
        input: Optional[Dict[str, Any]] = None,  # pylint: disable=redefined-builtin
        output: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        trace = self.get_trace(trace_id)
        if not trace:
            return None

        try:
            span = trace.span(
                name=name,
                metadata=_sanitize_dict(metadata or {}),
                tags=tags or [],
                parent_observation_id=parent_observation_id,
                input=_sanitize_dict(input or {}),
                output=_sanitize_dict(output or {}),
            )
            self._store_span(trace_id, span.id, span)
            return span.id
        except Exception as exc:
            logger.error("Failed to create span: %s", exc, exc_info=True)
            return None

    def create_llm_span(  # pylint: disable=too-many-arguments,huawei-too-many-arguments
        self,
        trace_id: str,
        name: str,
        metadata: Optional[Dict[str, Any]] = None,
        tags: Optional[list] = None,
        parent_observation_id: Optional[str] = None,
        input: Optional[Dict[str, Any]] = None,  # pylint: disable=redefined-builtin
        output: Optional[Dict[str, Any]] = None,
        model: Optional[str] = None,
        usage: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        trace = self.get_trace(trace_id)
        if not trace:
            return None

        try:
            span = trace.generation(
                name=name,
                metadata=_sanitize_dict(metadata or {}),
                tags=tags or [],
                parent_observation_id=parent_observation_id,
                input=_sanitize_dict(input or {}),
                output=_sanitize_dict(output or {}),
                model=model,
                usage=usage,
            )
            self._store_span(trace_id, span.id, span)
            return span.id
        except Exception as exc:
            logger.error("Failed to create LLM span: %s", exc)
            return None

    def update_llm_span_tokens(  # pylint: disable=too-many-arguments,huawei-too-many-arguments
        self,
        trace_id: str,
        span_id: str,
        usage: Dict[str, Any],
        prompt_text: str = "",
        completion_text: str = "",
        output: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        total_tokens = usage.get("total_tokens", 0)

        if prompt_tokens == 0 and prompt_text:
            prompt_tokens = self._estimate_tokens(prompt_text)
        if completion_tokens == 0 and completion_text:
            completion_tokens = self._estimate_tokens(completion_text)
        if total_tokens == 0:
            total_tokens = prompt_tokens + completion_tokens

        final_usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }

        try:
            span = self._get_span(trace_id, span_id)
            if not span:
                return
            end_kwargs: Dict[str, Any] = {"usage": final_usage}
            if output:
                end_kwargs["output"] = _sanitize_dict(output)
            if metadata:
                end_kwargs["metadata"] = _sanitize_dict(metadata)
            if hasattr(span, "end"):
                span.end(**end_kwargs)
            elif hasattr(span, "usage"):
                span.usage = final_usage
            self._remove_span(trace_id, span_id)
            self._release_trace_if_terminal_and_idle(trace_id)
        except Exception as exc:
            logger.error("Failed to update LLM span tokens: %s", exc)

    def end_observation(
        self,
        trace_id: str,
        observation_id: str,
        status: str = "success",
        output: Optional[Dict[str, Any]] = None,
    ) -> None:
        span = self._get_span(trace_id, observation_id)
        if not span:
            return

        try:
            end_kwargs: Dict[str, Any] = {}
            if output:
                end_kwargs["output"] = _sanitize_dict(output)
            if status == "error":
                end_kwargs["level"] = "ERROR"
            elif status == "cancelled":
                end_kwargs["level"] = "WARNING"
            if hasattr(span, "end"):
                span.end(**end_kwargs)
            self._remove_span(trace_id, observation_id)
            self._release_trace_if_terminal_and_idle(trace_id)
        except Exception as exc:
            logger.error("Failed to end observation: %s", exc)

    def flush(self) -> None:
        if not self._client:
            return
        try:
            self._client.flush()
        except Exception as exc:
            logger.error("Failed to flush traces: %s", exc)

    def close(self) -> None:
        if not self._client:
            return
        shutdown = getattr(self._client, "shutdown", None)
        try:
            if callable(shutdown):
                shutdown()
            else:
                self._client.flush()
        except Exception as exc:
            logger.error("Failed to close Langfuse client: %s", exc)

    def _store_span(self, trace_id: str, span_id: str, span: Any) -> None:
        with self._active_traces_lock:
            entry = self._active_traces.get(trace_id)
            if entry:
                entry["spans"][span_id] = span

    def _get_span(self, trace_id: str, span_id: str) -> Optional[Any]:
        with self._active_traces_lock:
            entry = self._active_traces.get(trace_id)
            if entry:
                return entry["spans"].get(span_id)
        return None

    def _remove_span(self, trace_id: str, span_id: str) -> None:
        with self._active_traces_lock:
            entry = self._active_traces.get(trace_id)
            if entry and span_id in entry["spans"]:
                del entry["spans"][span_id]

    def _mark_trace_terminal(self, trace_id: str) -> None:
        with self._active_traces_lock:
            entry = self._active_traces.get(trace_id)
            if entry:
                entry["terminal"] = True

    def _release_trace_if_terminal_and_idle(self, trace_id: str) -> None:
        with self._active_traces_lock:
            entry = self._active_traces.get(trace_id)
            if entry and entry.get("terminal") and not entry.get("spans"):
                del self._active_traces[trace_id]

    @staticmethod
    def _is_terminal_status(output: Dict[str, Any]) -> bool:
        return str(output.get("status") or "").lower() in {"success", "error", "cancelled"}

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        if not text:
            return 0
        chinese_chars = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
        other_chars = len(text) - chinese_chars
        return (chinese_chars // 2) + (other_chars // 4) + 1
