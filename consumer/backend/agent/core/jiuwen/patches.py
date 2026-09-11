"""Runtime compatibility patches for openJiuwen clients."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any, Optional

from .common import coerce_optional_int, read_field

logger = logging.getLogger("jiuwen.patches")


def patch_jiuwen_openai_reasoning_alias(openai_client_module: Any) -> None:
    """Keep openJiuwen OpenAI-compatible reasoning/cache parsing aligned with runtime config."""
    client_cls = getattr(openai_client_module, "OpenAIModelClient", None)
    if client_cls is None or getattr(client_cls, "_onto_reasoning_alias_patch", False):
        return

    original_parse_stream_chunk = getattr(client_cls, "_parse_stream_chunk", None)
    if callable(original_parse_stream_chunk):
        def patched_parse_stream_chunk(self: Any, chunk: Any) -> Any:
            parsed = original_parse_stream_chunk(self, chunk)
            parsed = _with_jiuwen_usage_cache_tokens(
                parsed,
                read_field(chunk, "usage"),
            )
            if parsed is None or read_field(parsed, "reasoning_content"):
                return parsed
            reasoning = _extract_jiuwen_reasoning_alias_from_stream_chunk(chunk)
            return _with_jiuwen_reasoning_content(parsed, reasoning)

        setattr(client_cls, "_parse_stream_chunk", patched_parse_stream_chunk)

    original_parse_response = getattr(client_cls, "_parse_response", None)
    if callable(original_parse_response):
        async def patched_parse_response(self: Any, response: Any, parser: Optional[Any] = None) -> Any:
            parsed = await original_parse_response(self, response, parser)
            parsed = _with_jiuwen_finish_reason(parsed, response)
            parsed = _with_jiuwen_usage_cache_tokens(
                parsed,
                read_field(response, "usage"),
            )
            if parsed is None or read_field(parsed, "reasoning_content"):
                return parsed
            reasoning = _extract_jiuwen_reasoning_alias_from_response(response)
            return _with_jiuwen_reasoning_content(parsed, reasoning)

        setattr(client_cls, "_parse_response", patched_parse_response)

    setattr(client_cls, "_onto_reasoning_alias_patch", True)


def _extract_jiuwen_reasoning_alias_from_stream_chunk(chunk: Any) -> str:
    choice = _first_jiuwen_choice(chunk)
    delta = read_field(choice, "delta")
    return str(read_field(delta, "reasoning") or "")


def _extract_jiuwen_reasoning_alias_from_response(response: Any) -> str:
    choice = _first_jiuwen_choice(response)
    message = read_field(choice, "message")
    return str(read_field(message, "reasoning") or "")


def _with_jiuwen_finish_reason(message: Any, response: Any) -> Any:
    choice = _first_jiuwen_choice(response)
    finish_reason = read_field(choice, "finish_reason")
    if message is None or finish_reason is None:
        return message
    return _copy_or_set_jiuwen_field(message, "finish_reason", finish_reason)


def _first_jiuwen_choice(payload: Any) -> Any:
    choices = read_field(payload, "choices") or []
    if not choices:
        return None
    try:
        return choices[0]
    except (TypeError, IndexError):
        return None


def _with_jiuwen_reasoning_content(message: Any, reasoning: str) -> Any:
    reasoning_text = str(reasoning or "")
    if not reasoning_text:
        return message
    model_copy = getattr(message, "model_copy", None)
    if callable(model_copy):
        return model_copy(update={"reasoning_content": reasoning_text})
    try:
        setattr(message, "reasoning_content", reasoning_text)
    except Exception:
        return message
    return message


def _with_jiuwen_usage_cache_tokens(message: Any, raw_usage: Any) -> Any:
    if message is None:
        return None
    cache_tokens = _extract_jiuwen_cache_tokens(raw_usage)
    if cache_tokens is None:
        return message
    usage_metadata = read_field(message, "usage_metadata")
    if usage_metadata is None:
        return message
    current_cache_tokens = coerce_optional_int(read_field(usage_metadata, "cache_tokens"))
    if current_cache_tokens is not None and current_cache_tokens > 0:
        return message
    updated_usage = _copy_or_set_jiuwen_field(
        usage_metadata,
        "cache_tokens",
        cache_tokens,
    )
    return _copy_or_set_jiuwen_field(message, "usage_metadata", updated_usage)


def _extract_jiuwen_cache_tokens(raw_usage: Any) -> Optional[int]:
    if raw_usage is None:
        return None
    for field_name in ("prompt_cache_hit_tokens", "cache_tokens", "cached_tokens"):
        value = coerce_optional_int(_read_jiuwen_usage_field(raw_usage, field_name))
        if value is not None:
            return max(0, value)
    prompt_details = _read_jiuwen_usage_field(raw_usage, "prompt_tokens_details")
    if prompt_details:
        value = coerce_optional_int(_read_jiuwen_usage_field(prompt_details, "cached_tokens"))
        if value is not None:
            return max(0, value)
    return None


def _copy_or_set_jiuwen_field(target: Any, field_name: str, value: Any) -> Any:
    if isinstance(target, Mapping):
        next_target = dict(target)
        next_target[field_name] = value
        return next_target
    model_copy = getattr(target, "model_copy", None)
    if callable(model_copy):
        try:
            return model_copy(update={field_name: value})
        except Exception as exc:
            logger.debug("Jiuwen model copy failed for field %s: %s", field_name, exc, exc_info=True)
    try:
        setattr(target, field_name, value)
    except Exception as exc:
        logger.debug("Jiuwen field assignment failed for %s: %s", field_name, exc, exc_info=True)
        return target
    return target


def _read_jiuwen_usage_field(payload: Any, name: str, default: Optional[Any] = None) -> Any:
    if isinstance(payload, Mapping):
        return payload.get(name, default)
    value = getattr(payload, name, default)
    if value is not default:
        return value
    model_extra = getattr(payload, "model_extra", None)
    if isinstance(model_extra, Mapping) and name in model_extra:
        return model_extra.get(name)
    pydantic_extra = getattr(payload, "__pydantic_extra__", None)
    if isinstance(pydantic_extra, Mapping) and name in pydantic_extra:
        return pydantic_extra.get(name)
    model_dump = getattr(payload, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump()
        except Exception as exc:
            logger.debug("Jiuwen model dump failed while reading %s: %s", name, exc, exc_info=True)
            dumped = None
        if isinstance(dumped, Mapping) and name in dumped:
            return dumped.get(name)
    return default
