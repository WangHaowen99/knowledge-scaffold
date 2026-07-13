"""Provider-specific OpenAI-compatible payload adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


@dataclass(frozen=True)
class ProviderPayloadAdapter:
    """Base adapter for provider-specific request fields."""

    def extra_body(self, *, enable_reasoning: bool) -> Dict[str, Any]:
        del enable_reasoning
        return {}

    def top_level_params(self, *, stream: bool) -> Dict[str, Any]:
        del stream
        return {}

    def normalize_usage(self, usage: Dict[str, Any]) -> Dict[str, Any]:
        return dict(usage or {})


@dataclass(frozen=True)
class GLMPayloadAdapter(ProviderPayloadAdapter):
    """GLM-compatible reasoning parameters."""

    def extra_body(self, *, enable_reasoning: bool) -> Dict[str, Any]:
        return {
            "chat_template_kwargs": {
                "thinking": enable_reasoning,
                "enable_thinking": enable_reasoning,
            },
            "thinking": {"type": ("enabled" if enable_reasoning else "disabled")},
            "reasoning": {"enabled": bool(enable_reasoning)},
            "provider": {
                "order": ["z-ai", "siliconflow/fp8", "deepinfra/fp8"]
            },
        }

    def top_level_params(self, *, stream: bool) -> Dict[str, Any]:
        return {"stream_options": {"include_usage": True}} if stream else {}

    def normalize_usage(self, usage: Dict[str, Any]) -> Dict[str, Any]:
        normalized = super().normalize_usage(usage)
        prompt_details = normalized.get("prompt_tokens_details")
        if not isinstance(prompt_details, dict) or "cached_tokens" not in prompt_details:
            return normalized
        hit_tokens = _optional_int(prompt_details.get("cached_tokens"))
        total_input_tokens = _optional_int(normalized.get("prompt_tokens"))
        if hit_tokens is None or total_input_tokens is None:
            return normalized
        _set_cache_metrics(
            normalized,
            hit_tokens=hit_tokens,
            total_input_tokens=total_input_tokens,
        )
        return normalized


@dataclass(frozen=True)
class DeepSeekPayloadAdapter(ProviderPayloadAdapter):
    """DeepSeek OpenAI-compatible reasoning parameters."""

    def extra_body(self, *, enable_reasoning: bool) -> Dict[str, Any]:
        # DeepSeek documents reasoning_effort values such as "high" and "max",
        # but we avoid setting effort by default so users do not get a hidden
        # reasoning-cost policy from provider selection alone.
        return {
            "thinking": {"type": ("enabled" if enable_reasoning else "disabled")},
        }

    def top_level_params(self, *, stream: bool) -> Dict[str, Any]:
        return {"stream_options": {"include_usage": True}} if stream else {}

    def normalize_usage(self, usage: Dict[str, Any]) -> Dict[str, Any]:
        normalized = super().normalize_usage(usage)
        if "prompt_cache_hit_tokens" not in normalized and "prompt_cache_miss_tokens" not in normalized:
            return normalized
        hit_tokens = _optional_int(normalized.get("prompt_cache_hit_tokens"))
        miss_tokens = _optional_int(normalized.get("prompt_cache_miss_tokens"))
        if hit_tokens is None and miss_tokens is None:
            return normalized
        hit_tokens = hit_tokens or 0
        miss_tokens = miss_tokens or 0
        _set_cache_metrics(
            normalized,
            hit_tokens=hit_tokens,
            total_input_tokens=hit_tokens + miss_tokens,
            miss_tokens=miss_tokens,
        )
        return normalized


@dataclass(frozen=True)
class OpenAICompatiblePayloadAdapter(ProviderPayloadAdapter):
    """Broad compatibility adapter that sends all known reasoning switches."""

    def extra_body(self, *, enable_reasoning: bool) -> Dict[str, Any]:
        payload = GLMPayloadAdapter().extra_body(enable_reasoning=enable_reasoning)
        payload.update(DeepSeekPayloadAdapter().extra_body(enable_reasoning=enable_reasoning))
        return payload


_DEFAULT_ADAPTER = ProviderPayloadAdapter()
_PROVIDER_ADAPTERS: Dict[str, ProviderPayloadAdapter] = {
    "deepseek": DeepSeekPayloadAdapter(),
    "glm": GLMPayloadAdapter(),
    "openai_compatible": OpenAICompatiblePayloadAdapter(),
}


def get_provider_adapter(provider: str | None) -> ProviderPayloadAdapter:
    normalized = str(provider or "openai_compatible").strip().lower()
    return _PROVIDER_ADAPTERS.get(normalized, _DEFAULT_ADAPTER)


def _set_cache_metrics(
    usage: Dict[str, Any],
    *,
    hit_tokens: int,
    total_input_tokens: int,
    miss_tokens: int | None = None,
) -> None:
    usage["cache_hit_tokens"] = hit_tokens
    usage["cache_total_input_tokens"] = total_input_tokens
    if miss_tokens is not None:
        usage["cache_miss_tokens"] = miss_tokens
    if total_input_tokens > 0:
        usage["cache_hit_rate"] = hit_tokens / total_input_tokens


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
