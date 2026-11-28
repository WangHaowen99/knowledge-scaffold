"""Keep interleaved tool argument fragments attached to their original calls."""

from functools import wraps


def _merge_tool_calls(left, right):
    calls = [call.model_copy(deep=True) for call in left or ()]
    for incoming in right or ():
        matches = [
            index for index, call in enumerate(calls)
            # Responses emits complete output items with a stable item ID.
            # SDK 0.1.19 assigns index=0 to every such item, so that index
            # cannot identify distinct Responses calls.
            if (not incoming.response_item_id and not call.response_item_id
                and incoming.index is not None and call.index == incoming.index)
            or (incoming.id and call.id == incoming.id)
            or (incoming.response_item_id and call.response_item_id == incoming.response_item_id)
        ]
        if len(matches) > 1:
            raise ValueError("Conflicting streamed tool call identities")
        if not matches:
            if incoming.index is None and not incoming.id and not incoming.response_item_id:
                if len(calls) != 1:
                    raise ValueError("Streamed tool argument fragment has no unique call identity")
                matches = [0]
            else:
                calls.append(incoming.model_copy(deep=True))
                continue
        index = matches[0]
        current = calls[index]
        for field in ("id", "response_item_id", "name"):
            before, after = getattr(current, field), getattr(incoming, field)
            if before and after and before != after:
                raise ValueError("Conflicting streamed tool call metadata")
        calls[index] = current.model_copy(update={
            "id": current.id or incoming.id,
            "name": current.name or incoming.name,
            "index": current.index if current.index is not None else incoming.index,
            "response_item_id": current.response_item_id or incoming.response_item_id,
            "arguments": (current.arguments or "") + (incoming.arguments or ""),
        })
    return calls or None


def preserve_tool_call_fragments(model):
    """Adapt this model instance only; preserve SDK text/usage/finish merging.

    The installed SDK merges argument deltas into the last tool call, ignoring
    the provider's index. Parallel calls therefore lose or concatenate JSON.
    Main and delegated agents share this model, so both use the same adapter.
    """
    from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk

    class IndexedToolChunk(AssistantMessageChunk):
        def __add__(self, other):
            if not isinstance(other, AssistantMessageChunk):
                return super().__add__(other)
            merged = AssistantMessageChunk.__add__(
                self.model_copy(update={"tool_calls": None}),
                other.model_copy(update={"tool_calls": None}),
            )
            values = merged.model_dump()
            values["tool_calls"] = _merge_tool_calls(self.tool_calls, other.tool_calls)
            return IndexedToolChunk(**values)

    original_stream = model.stream

    @wraps(original_stream)
    async def stream(*args, **kwargs):
        async for chunk in original_stream(*args, **kwargs):
            if isinstance(chunk, AssistantMessageChunk):
                values = chunk.model_dump()
                # ToolCall.index is excluded from model_dump by the SDK.
                values["tool_calls"] = chunk.tool_calls
                yield IndexedToolChunk(**values)
            else:
                yield chunk

    model.stream = stream
    return model
