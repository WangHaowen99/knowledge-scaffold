"""Convert internal runtime events into sanitized public event drafts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from .events import PublicEventDraft, RuntimeEvent


_REDACTED = "[REDACTED]"
_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "access_token",
        "authorization",
        "auth_token",
        "bearer_token",
        "client_secret",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "database_url",
        "db_url",
        "headers",
        "password",
        "passwd",
        "private_key",
        "refresh_token",
        "secret",
        "set_cookie",
        "token",
        "tokens",
    }
)
_SENSITIVE_KEY_SUFFIXES = (
    "_access_token",
    "_api_key",
    "_apikey",
    "_auth_token",
    "_authorization",
    "_bearer_token",
    "_client_secret",
    "_cookie",
    "_cookies",
    "_credential",
    "_credentials",
    "_database_url",
    "_db_url",
    "_headers",
    "_password",
    "_passwd",
    "_private_key",
    "_refresh_token",
    "_secret",
    "_set_cookie",
    "_token",
    "_tokens",
)
_PUBLIC_TOKEN_COUNT_KEYS = frozenset(
    {
        "cached_tokens",
        "completion_tokens",
        "input_tokens",
        "output_tokens",
        "prompt_tokens",
        "reasoning_tokens",
        "total_tokens",
    }
)
_ALWAYS_PRIVATE_KEYS = frozenset(
    {
        "messages",
        "model_context",
        "prompt",
    }
)
_NEVER_PUBLIC_KEYS = frozenset(
    {
        "reasoning_summaries",
        "reasoning_summary",
    }
)
_TRACE_ONLY_KEYS = frozenset(
    {
        "reasoning",
        "reasoning_content",
        "trace",
    }
)
_INLINE_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(
        r"(?i)([?&](?:access[_-]?token|api[_-]?key|auth[_-]?token|"
        r"client[_-]?secret|password|private[_-]?key|refresh[_-]?token|"
        r"secret|token)=)[^&#\s]+"
    ),
    re.compile(
        r"(?i)([\"']?(?:access[_-]?token|api[_-]?key|authorization|"
        r"auth[_-]?token|bearer[_-]?token|client[_-]?secret|password|passwd|"
        r"private[_-]?key|refresh[_-]?token|secret|token)[\"']?\s*[:=]\s*)"
        r"(?:[\"'][^\"']*[\"']|[^\s,;}&]+)"
    ),
    re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"),
    re.compile(
        r"(?is)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?"
        r"-----END [A-Z ]*PRIVATE KEY-----"
    ),
)
_STATUS_ALIASES = {
    "success": "completed",
    "error": "failed",
}


@dataclass
class _SanitizeStats:
    text_fields_truncated: int = 0
    sensitive_fields_redacted: int = 0


class PublicEventAdapter:
    """Apply the stable public event policy before line B sees an event."""

    def __init__(self, *, include_trace: bool = False, max_text_length: int = 4096) -> None:
        if max_text_length < 32:
            raise ValueError("max_text_length must be at least 32")
        self.include_trace = bool(include_trace)
        self.max_text_length = int(max_text_length)

    def adapt(self, event: RuntimeEvent) -> PublicEventDraft | None:
        if event.event_type == "reasoning.delta" and not self.include_trace:
            return None

        raw_payload = self._normalize_payload(event)
        stats = _SanitizeStats()
        sanitized = self._sanitize(raw_payload, stats=stats)
        payload = dict(sanitized) if isinstance(sanitized, Mapping) else {"value": sanitized}
        if stats.text_fields_truncated or stats.sensitive_fields_redacted:
            payload["omitted"] = {
                "text_fields_truncated": stats.text_fields_truncated,
                "sensitive_fields_redacted": stats.sensitive_fields_redacted,
            }
        if stats.text_fields_truncated:
            payload["truncated"] = True

        public_status = event.status
        if event.event_type == "turn.finished":
            public_status = payload.get("status") or event.status

        return PublicEventDraft(
            run_id=event.run_id,
            session_id=event.session_id,
            turn_id=event.turn_id,
            event_type=event.event_type,
            status=self._normalize_status(public_status),
            source=self._sanitize_source(event.source),
            payload=payload,
            created_at=event.created_at,
        )

    def adapt_runtime_result_payload(self, event: RuntimeEvent) -> dict[str, Any]:
        """Sanitize terminal data without truncating the caller's final result."""
        raw_payload = self._normalize_payload(event)
        payload: dict[str, Any] = {}
        for key, value in raw_payload.items():
            stats = _SanitizeStats()
            payload[key] = self._sanitize(
                value,
                stats=stats,
                key=key,
                path=(self._normalized_key(key),),
                truncate_text=key not in {"content", "structured_content", "usage"},
            )
        return payload

    def _normalize_payload(self, event: RuntimeEvent) -> dict[str, Any]:
        payload = dict(event.payload or {})
        event_type = event.event_type

        if event_type == "turn.started":
            return {"turn_id": event.turn_id or payload.get("turn_id")}
        if event_type == "model.started":
            model_profile_id = payload.get("model_profile_id")
            return {"model_profile_id": model_profile_id} if model_profile_id else {}
        if event_type in {"token.delta", "reasoning.delta"}:
            return {"text": str(payload.get("text") or "")}
        if event_type == "tool.started":
            return self._tool_started_payload(payload)
        if event_type == "tool.finished":
            return self._tool_finished_payload(payload)
        if event_type == "interaction.requested":
            return {
                "interaction_id": payload.get("interaction_id"),
                "type": payload.get("type"),
                "options": payload.get("options") or [],
            }
        if event_type == "turn.finished":
            terminal_status = self._normalize_status(payload.get("status") or event.status)
            normalized = {"status": terminal_status}
            for key in ("result", "content", "structured_content", "usage", "error"):
                if key in payload:
                    normalized[key] = payload[key]
            if self.include_trace and "trace" in payload:
                normalized["trace"] = payload["trace"]
            return normalized
        return payload

    @staticmethod
    def _tool_started_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
        normalized = {
            "name": payload.get("name") or payload.get("tool_name"),
            "tool_call_id": payload.get("tool_call_id"),
        }
        for key in ("iteration", "batch_index"):
            if key in payload:
                normalized[key] = payload[key]
        return normalized

    def _tool_finished_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        normalized = {
            "name": payload.get("name") or payload.get("tool_name"),
            "tool_call_id": payload.get("tool_call_id"),
            "status": self._normalize_status(payload.get("status") or "failed"),
            "elapsed_ms": max(0, int(payload.get("elapsed_ms") or 0)),
        }
        for key in (
            "iteration",
            "batch_index",
            "output_preview",
            "error",
            "error_code",
            "timeout_seconds",
        ):
            if key in payload:
                normalized[key] = payload[key]
        return normalized

    def _sanitize(
        self,
        value: Any,
        *,
        stats: _SanitizeStats,
        key: str | None = None,
        path: tuple[str, ...] = (),
        truncate_text: bool = True,
    ) -> Any:
        if key and self._is_sensitive_key(key) and not self._is_public_token_count(
            key,
            value,
            path=path,
        ):
            stats.sensitive_fields_redacted += 1
            return _REDACTED
        if key and self._normalized_key(key) in _ALWAYS_PRIVATE_KEYS:
            stats.sensitive_fields_redacted += 1
            return _REDACTED
        if key and not self.include_trace and self._normalized_key(key) in _TRACE_ONLY_KEYS:
            stats.sensitive_fields_redacted += 1
            return _REDACTED
        if isinstance(value, Mapping):
            return {
                str(child_key): self._sanitize(
                    child_value,
                    stats=stats,
                    key=str(child_key),
                    path=path + (self._normalized_key(str(child_key)),),
                    truncate_text=truncate_text,
                )
                for child_key, child_value in value.items()
                if self._normalized_key(str(child_key)) not in _NEVER_PUBLIC_KEYS
            }
        if isinstance(value, (list, tuple)):
            return [
                self._sanitize(
                    item,
                    stats=stats,
                    path=path,
                    truncate_text=truncate_text,
                )
                for item in value
            ]
        if isinstance(value, str):
            sanitized = value
            sanitized = _INLINE_SECRET_PATTERNS[0].sub(_REDACTED, sanitized)
            sanitized = _INLINE_SECRET_PATTERNS[1].sub(
                lambda match: match.group(1) + _REDACTED,
                sanitized,
            )
            sanitized = _INLINE_SECRET_PATTERNS[2].sub(
                lambda match: match.group(1) + _REDACTED,
                sanitized,
            )
            sanitized = _INLINE_SECRET_PATTERNS[3].sub(
                lambda match: match.group(1) + _REDACTED + "@",
                sanitized,
            )
            sanitized = _INLINE_SECRET_PATTERNS[4].sub(_REDACTED, sanitized)
            if truncate_text and len(sanitized) > self.max_text_length:
                stats.text_fields_truncated += 1
                return sanitized[: self.max_text_length] + "..."
            return sanitized
        return value

    def _sanitize_source(self, source: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if not source:
            return None
        return {
            key: value
            for key, value in {
                "node": str(source.get("node") or "").strip() or None,
                "component": str(source.get("component") or "").strip() or None,
            }.items()
            if value is not None
        } or None

    @staticmethod
    def _normalized_key(key: str) -> str:
        raw = str(key or "").strip().replace("-", "_").replace(" ", "_")
        snake_case = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", raw)
        snake_case = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", snake_case)
        return snake_case.lower()

    @classmethod
    def _is_sensitive_key(cls, key: str) -> bool:
        normalized = cls._normalized_key(key)
        return normalized in _SENSITIVE_KEYS or normalized.endswith(_SENSITIVE_KEY_SUFFIXES)

    @classmethod
    def _is_public_token_count(
        cls,
        key: str,
        value: Any,
        *,
        path: tuple[str, ...],
    ) -> bool:
        normalized = cls._normalized_key(key)
        return (
            normalized in _PUBLIC_TOKEN_COUNT_KEYS
            and bool(path)
            and path[0] == "usage"
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
        )

    @staticmethod
    def _normalize_status(status: Any) -> str:
        normalized = str(status or "").strip().lower()
        return _STATUS_ALIASES.get(normalized, normalized)
