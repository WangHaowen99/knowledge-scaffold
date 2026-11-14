"""Real SDK chunk reduction with synthetic, interleaved provider deltas."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.services.engines.jiuwen_stream import preserve_tool_call_fragments


@pytest.mark.parametrize("mode", [None, "chat_completions", "responses"])
def test_model_protocol_is_client_setting_not_request_extra(mode):
    from app.services.engines.jiuwen import _create_model

    cfg = {
        "model": "synthetic", "api_key": "synthetic-key",
        "base_url": "https://example.com/v1",
        "context": {"max_output_tokens": 1024, "max_context_tokens": 32000},
    }
    if mode is not None:
        cfg["api_mode"] = mode
    model = _create_model(cfg)
    assert model.model_client_config.api_mode.value == (mode or "chat_completions")
    assert "api_mode" not in model.model_config.model_dump()
    assert model.model_config.max_tokens == 1024


def _call(index=None, identifier="", name="", arguments="", item=None):
    from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
    return ToolCall(index=index, id=identifier, type="function", name=name,
                    arguments=arguments, response_item_id=item)


def _reduce(chunks):
    async def source():
        for chunk in chunks:
            yield chunk

    async def run():
        model = preserve_tool_call_fragments(SimpleNamespace(stream=source))
        result = None
        async for chunk in model.stream():
            result = chunk if result is None else result + chunk
        return result

    return asyncio.run(run())


def test_interleaved_arguments_keep_each_read_path_and_stream_metadata():
    from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk as Chunk
    chunks = [
        Chunk(content="开始", reasoning_content="思", tool_calls=[
            _call(0, "synthetic-a", "Read"), _call(1, "synthetic-b", "Read"),
        ]),
        Chunk(tool_calls=[_call(1, arguments='{"file_path":"b'),
                          _call(0, arguments='{"file_path":"a')]),
        Chunk(content="完成", reasoning_content="考", tool_calls=[
            _call(0, arguments='.md"}'), _call(1, arguments='.md"}'),
        ], finish_reason="tool_calls", usage_metadata={"input_tokens":5,"output_tokens":3}),
    ]
    result = _reduce(chunks)
    assert [(c.id, json.loads(c.arguments)) for c in result.tool_calls] == [
        ("synthetic-a", {"file_path":"a.md"}), ("synthetic-b", {"file_path":"b.md"}),
    ]
    assert result.content == "开始完成"
    assert result.reasoning_content == "思考"
    assert result.finish_reason == "tool_calls"
    assert result.usage_metadata.input_tokens == 5
    assert chunks[0].tool_calls[0].arguments == ""


def test_responses_complete_items_with_reused_sdk_index_stay_separate():
    from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk as Chunk

    # Responses completed items may reuse index zero; their identities must stay distinct.
    chunks = [Chunk(tool_calls=[_call(
        index=0, identifier=f"synthetic-call-{i}", name="read_file",
        arguments=json.dumps({"file_path": path}), item=f"synthetic-item-{i}",
    )]) for i, path in enumerate(("a.md", "b.md"))]
    result = _reduce(chunks)
    assert [json.loads(c.arguments) for c in result.tool_calls] == [
        {"file_path": "a.md"}, {"file_path": "b.md"},
    ]
    assert [c.response_item_id for c in result.tool_calls] == [
        "synthetic-item-0", "synthetic-item-1",
    ]
    assert [c.id for c in result.tool_calls] == [
        "synthetic-call-0", "synthetic-call-1",
    ]


@pytest.mark.parametrize("identity", ["id", "item", "single"])
def test_indexless_provider_identity(identity):
    from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk as Chunk
    first = _call(identifier="synthetic-a", name="Read", arguments="{", item="synthetic-item")
    delta = _call(arguments='"file_path":"a.md"}',
                  identifier="synthetic-a" if identity == "id" else "",
                  item="synthetic-item" if identity == "item" else None)
    result = _reduce([Chunk(tool_calls=[first]), Chunk(tool_calls=[delta])])
    assert json.loads(result.tool_calls[0].arguments) == {"file_path":"a.md"}


def test_ambiguous_unindexed_fragment_fails_without_executing_corrupt_call():
    from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk as Chunk
    with pytest.raises(ValueError, match="unique call identity"):
        _reduce([Chunk(tool_calls=[_call(0,"synthetic-a","Read"),_call(1,"synthetic-b","Read")]),
                 Chunk(tool_calls=[_call(arguments="{}")])])


def test_conflicting_index_and_id_fails_closed():
    from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk as Chunk
    with pytest.raises(ValueError, match="identities"):
        _reduce([Chunk(tool_calls=[_call(0,"synthetic-a","Read"),_call(1,"synthetic-b","Read")]),
                 Chunk(tool_calls=[_call(0,"synthetic-b",arguments="{}")])])


def test_tool_free_stream_keeps_text():
    from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk as Chunk
    result = _reduce([Chunk(content="一"),Chunk(content="二",finish_reason="stop")])
    assert result.content == "一二"
    assert result.tool_calls is None
