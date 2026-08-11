"""Tests for LLM service behavior."""

import json
import logging
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from config import AgentConfig, LLMConfig
from llm import LLMService


class _FakeStreamingResponse:
    def __init__(self, lines):
        self._lines = lines
        self.encoding = "utf-8"

    def iter_lines(self, decode_unicode=True):
        return iter(self._lines)

    def close(self) -> None:
        return


class _FakeOpenAINonStreamCompletion:
    def __init__(self, payload):
        self._payload = payload

    def model_dump(self):
        return self._payload


class _FakeOpenAIClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create_completion)
        )

    def _create_completion(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class _StructuredOutput(BaseModel):
    answer: str
    score: float


def test_llm_call_clamps_output_tokens_to_ops_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_CONFIG_PATH", str(tmp_path / "missing-ops-config.json"))
    monkeypatch.setenv("MODEL_OUTPUT_TOKEN_LIMIT", "12")
    completion = _FakeOpenAINonStreamCompletion(
        {
            "model": "unit-model",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
        }
    )
    fake_client = _FakeOpenAIClient([completion])
    llm = LLMService(
        LLMConfig(
            base_url="http://unit.test/v1",
            model="unit-model",
            api_key=None,
            timeout=30,
            max_tokens=256,
            temperature=0.0,
        ),
        openai_client=fake_client,
    )

    response = llm.call(
        messages=[{"role": "user", "content": "short"}],
        stream=False,
        max_tokens=50,
        enable_reasoning=False,
    )

    assert response.content == "ok"
    assert fake_client.calls[0]["max_tokens"] == 12
    assert response.request_params["max_tokens"] == 12


def test_llm_call_uses_openai_client_stream_and_preserves_nested_tool_object_start():
    stream_chunks = [
        {
            "model": "unit-model",
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_spawn",
                                "type": "function",
                                "function": {
                                    "name": "spawn_sub_agent",
                                    "arguments": '{"tasks": [',
                                },
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "model": "unit-model",
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {
                                    "arguments": '{"id": "cycle_impact", "task": "demo"}',
                                },
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "model": "unit-model",
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": "]}"}
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        },
    ]
    fake_client = _FakeOpenAIClient([iter(stream_chunks)])
    llm = LLMService(
        LLMConfig(
            base_url="http://unit.test/v1",
            model="unit-model",
            api_key=None,
            timeout=30,
            max_tokens=256,
            temperature=0.0,
        ),
        openai_client=fake_client,
    )

    response = llm.call(
        messages=[{"role": "user", "content": "spawn"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "spawn_sub_agent",
                    "description": "spawn",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "tasks": {"type": "array", "items": {"type": "object"}},
                        },
                        "required": ["tasks"],
                    },
                },
            }
        ],
        enable_reasoning=False,
    )

    assert fake_client.calls[0]["stream"] is True
    assert fake_client.calls[0]["extra_body"]["thinking"] == {"type": "disabled"}
    tool_call = response.tool_calls[0]
    assert tool_call["function"]["name"] == "spawn_sub_agent"
    assert json.loads(tool_call["function"]["arguments"]) == {
        "tasks": [{"id": "cycle_impact", "task": "demo"}]
    }


def test_llm_call_parses_response_model_without_sending_model_class():
    completion = _FakeOpenAINonStreamCompletion(
        {
            "model": "unit-model",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": '{"answer": "ok", "score": 0.9}',
                    },
                    "finish_reason": "stop",
                }
            ],
        }
    )
    fake_client = _FakeOpenAIClient([completion])
    llm = LLMService(
        LLMConfig(
            base_url="http://unit.test",
            model="unit-model",
            api_key=None,
            timeout=30,
            max_tokens=256,
            temperature=0.0,
        ),
        openai_client=fake_client,
    )

    response = llm.call(
        [{"role": "user", "content": "return json"}],
        response_model=_StructuredOutput,
        enable_reasoning=False,
    )

    assert response.data == _StructuredOutput(answer="ok", score=0.9)
    assert fake_client.calls[0]["stream"] is False
    assert "response_model" not in fake_client.calls[0]
    assert "response_model" not in (fake_client.calls[0].get("extra_body") or {})


def test_llm_call_preserves_input_history_and_appends_single_assistant(monkeypatch):
    llm = LLMService(
        LLMConfig(
            base_url="http://unit.test",
            model="unit-model",
            api_key=None,
            timeout=30,
            max_tokens=256,
            temperature=0.0,
        )
    )

    streamed_messages = iter([
        {
            "message": {
                "role": "assistant",
                "content": "final answer",
            },
            "finish_reason": "stop",
            "model": "unit-model",
        },
    ])

    request_kwargs = []

    def fake_make_request(*args, **kwargs):
        request_kwargs.append(kwargs)
        return object()

    def fake_collect_response(
        response,
        emit_func=None,
        run_id=None,
        reasoning_summary_callback=None,
        request_label=None,
        tool_calls_closed_callback=None,
    ):
        del response, emit_func, run_id, reasoning_summary_callback, request_label, tool_calls_closed_callback
        return next(streamed_messages)

    monkeypatch.setattr(llm, "_make_request", fake_make_request)
    monkeypatch.setattr(llm, "_collect_response", fake_collect_response)

    input_messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "user"},
    ]
    original_input = [m.copy() for m in input_messages]

    response = llm.call(
        messages=input_messages,
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "business_data_search",
                    "description": "search",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "entity": {"type": "string"},
                            "query": {"type": "string"},
                        },
                        "required": ["entity", "query"],
                    },
                },
            }
        ],
        stream=False,
        enable_reasoning=False,
    )

    assert response.content == "final answer"
    assert input_messages == original_input
    assert request_kwargs[0]["stream"] is False

    roles = [msg.get("role") for msg in response.messages]
    assert roles == ["system", "user", "assistant"]
    assert sum(1 for msg in response.messages if msg.get("role") == "system") == 1
    assert sum(1 for msg in response.messages if msg.get("role") == "user") == 1


def test_llm_call_collects_openai_sdk_non_stream_completion_without_close():
    completion = _FakeOpenAINonStreamCompletion(
        {
            "model": "unit-model",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "summary",
                    },
                    "finish_reason": "stop",
                }
            ],
        }
    )
    fake_client = _FakeOpenAIClient([completion])
    llm = LLMService(
        LLMConfig(
            base_url="http://unit.test/v1",
            model="unit-model",
            api_key=None,
            timeout=30,
            max_tokens=256,
            temperature=0.0,
        ),
        openai_client=fake_client,
    )

    response = llm.call(
        messages=[{"role": "user", "content": "summarize"}],
        stream=False,
        enable_reasoning=False,
    )

    assert fake_client.calls[0]["stream"] is False
    assert response.content == "summary"
    assert response.finish_reason == "stop"
    assert response.messages[-1] == {"role": "assistant", "content": "summary"}


def test_llm_request_payload_logged_at_debug(caplog):
    completion = _FakeOpenAINonStreamCompletion(
        {
            "model": "unit-model",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
        }
    )
    fake_client = _FakeOpenAIClient([completion])
    llm = LLMService(
        LLMConfig(
            base_url="http://unit.test/v1",
            model="unit-model",
            api_key=None,
            timeout=30,
            max_tokens=256,
            temperature=0.0,
        ),
        openai_client=fake_client,
    )

    with caplog.at_level(logging.DEBUG, logger="llm.transport"):
        llm.call(
            messages=[{"role": "user", "content": "log full payload"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "read",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            stream=False,
            enable_reasoning=False,
        )

    assert "LLM request payload:" in caplog.text
    assert '"messages": [{"role": "user", "content": "log full payload"}]' in caplog.text
    assert '"tools": [{"type": "function", "function": {"name": "read_file"' in caplog.text
    assert '"extra_body": {' in caplog.text
    assert '"thinking": {"type": "disabled"}' in caplog.text


def test_llm_request_payload_not_logged_at_info(caplog):
    completion = _FakeOpenAINonStreamCompletion(
        {
            "model": "unit-model",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
        }
    )
    fake_client = _FakeOpenAIClient([completion])
    llm = LLMService(
        LLMConfig(
            base_url="http://unit.test/v1",
            model="unit-model",
            api_key=None,
            timeout=30,
            max_tokens=256,
            temperature=0.0,
        ),
        openai_client=fake_client,
    )

    with caplog.at_level(logging.INFO, logger="llm.transport"):
        llm.call(
            messages=[{"role": "user", "content": "do not log full payload"}],
            stream=False,
            enable_reasoning=False,
        )

    assert "Making LLM request via OpenAI client" in caplog.text
    assert "LLM request payload:" not in caplog.text


def test_provider_adapter_keeps_glm_reasoning_params_and_openai_compatible_full():
    messages = [{"role": "user", "content": "hi"}]
    glm = LLMService(
        LLMConfig(
            base_url="http://unit.test",
            model="glm-model",
            api_key=None,
            timeout=30,
            max_tokens=256,
            temperature=0.0,
            provider="glm",
        )
    )
    openai = LLMService(
        LLMConfig(
            base_url="http://unit.test",
            model="openai-model",
            api_key=None,
            timeout=30,
            max_tokens=256,
            temperature=0.0,
            provider="openai_compatible",
        )
    )

    glm_payload = glm.transport.build_payload(
        messages=messages,
        enable_reasoning=True,
        stream=True,
        temperature=None,
        max_tokens=None,
        tools=None,
    )
    openai_payload = openai.transport.build_payload(
        messages=messages,
        enable_reasoning=True,
        stream=True,
        temperature=None,
        max_tokens=None,
        tools=None,
    )

    assert glm_payload["extra_body"]["chat_template_kwargs"]["thinking"] is True
    assert glm_payload["extra_body"]["thinking"]["type"] == "enabled"
    assert glm_payload["extra_body"]["reasoning"]["enabled"] is True
    assert openai_payload["extra_body"]["chat_template_kwargs"]["thinking"] is True
    assert openai_payload["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True
    assert openai_payload["extra_body"]["thinking"]["type"] == "enabled"
    assert openai_payload["extra_body"]["reasoning"]["enabled"] is True
    assert openai_payload["extra_body"]["provider"]["order"] == ["z-ai", "siliconflow/fp8", "deepinfra/fp8"]


def test_deepseek_provider_adapter_uses_v4_reasoning_extra_body():
    llm = LLMService(
        LLMConfig(
            base_url="http://unit.test",
            model="deepseek-v4-flash",
            api_key=None,
            timeout=30,
            max_tokens=256,
            temperature=0.0,
            provider="deepseek",
        )
    )

    payload = llm.transport.build_payload(
        messages=[{"role": "user", "content": "hi"}],
        enable_reasoning=True,
        stream=True,
        temperature=None,
        max_tokens=None,
        tools=None,
    )

    assert payload["extra_body"] == {
        "thinking": {"type": "enabled"},
    }
    assert payload["stream_options"] == {"include_usage": True}


def test_glm_and_deepseek_stream_usage_option_is_provider_adapter_controlled():
    glm = LLMService(
        LLMConfig(
            base_url="http://unit.test",
            model="glm-model",
            api_key=None,
            timeout=30,
            max_tokens=256,
            temperature=0.0,
            provider="glm",
        )
    )
    deepseek = LLMService(
        LLMConfig(
            base_url="http://unit.test",
            model="deepseek-model",
            api_key=None,
            timeout=30,
            max_tokens=256,
            temperature=0.0,
            provider="deepseek",
        )
    )
    unknown = LLMService(
        LLMConfig(
            base_url="http://unit.test",
            model="unknown-model",
            api_key=None,
            timeout=30,
            max_tokens=256,
            temperature=0.0,
            provider="unknown",
        )
    )

    glm_stream_payload = glm.transport.build_payload(
        messages=[{"role": "user", "content": "hi"}],
        enable_reasoning=False,
        stream=True,
        temperature=None,
        max_tokens=None,
        tools=None,
    )
    glm_non_stream_payload = glm.transport.build_payload(
        messages=[{"role": "user", "content": "hi"}],
        enable_reasoning=False,
        stream=False,
        temperature=None,
        max_tokens=None,
        tools=None,
    )
    deepseek_stream_payload = deepseek.transport.build_payload(
        messages=[{"role": "user", "content": "hi"}],
        enable_reasoning=False,
        stream=True,
        temperature=None,
        max_tokens=None,
        tools=None,
    )
    unknown_stream_payload = unknown.transport.build_payload(
        messages=[{"role": "user", "content": "hi"}],
        enable_reasoning=False,
        stream=True,
        temperature=None,
        max_tokens=None,
        tools=None,
    )

    assert glm_stream_payload["stream_options"] == {"include_usage": True}
    assert deepseek_stream_payload["stream_options"] == {"include_usage": True}
    assert "stream_options" not in glm_non_stream_payload
    assert "stream_options" not in unknown_stream_payload


def test_provider_adapters_normalize_cache_usage_fields():
    glm = LLMService(
        LLMConfig(
            base_url="http://unit.test",
            model="glm-model",
            api_key=None,
            timeout=30,
            max_tokens=256,
            temperature=0.0,
            provider="glm",
        )
    )
    deepseek = LLMService(
        LLMConfig(
            base_url="http://unit.test",
            model="deepseek-model",
            api_key=None,
            timeout=30,
            max_tokens=256,
            temperature=0.0,
            provider="deepseek",
        )
    )
    openai_compatible = LLMService(
        LLMConfig(
            base_url="http://unit.test",
            model="compat-model",
            api_key=None,
            timeout=30,
            max_tokens=256,
            temperature=0.0,
            provider="openai_compatible",
        )
    )

    glm_usage = glm.transport.provider_adapter.normalize_usage(
        {
            "prompt_tokens": 5992,
            "completion_tokens": 8,
            "total_tokens": 6000,
            "prompt_tokens_details": {"cached_tokens": 5952},
        }
    )
    deepseek_usage = deepseek.transport.provider_adapter.normalize_usage(
        {
            "prompt_tokens": 6250,
            "completion_tokens": 8,
            "total_tokens": 6258,
            "prompt_cache_hit_tokens": 6144,
            "prompt_cache_miss_tokens": 106,
        }
    )

    assert glm_usage["cache_hit_tokens"] == 5952
    assert glm_usage["cache_total_input_tokens"] == 5992
    assert glm_usage["cache_hit_rate"] == 5952 / 5992
    assert deepseek_usage["cache_hit_tokens"] == 6144
    assert deepseek_usage["cache_miss_tokens"] == 106
    assert deepseek_usage["cache_total_input_tokens"] == 6250
    assert deepseek_usage["cache_hit_rate"] == 6144 / 6250

    compat_deepseek_usage = openai_compatible.transport.provider_adapter.normalize_usage(
        {
            "prompt_tokens": 6250,
            "completion_tokens": 8,
            "total_tokens": 6258,
            "prompt_cache_hit_tokens": 6144,
            "prompt_cache_miss_tokens": 106,
        }
    )
    compat_glm_usage = openai_compatible.transport.provider_adapter.normalize_usage(
        {
            "prompt_tokens": 5992,
            "completion_tokens": 8,
            "total_tokens": 6000,
            "prompt_tokens_details": {"cached_tokens": 5952},
        }
    )

    assert compat_deepseek_usage["cache_hit_tokens"] == 6144
    assert compat_deepseek_usage["cache_miss_tokens"] == 106
    assert compat_deepseek_usage["cache_total_input_tokens"] == 6250
    assert compat_deepseek_usage["cache_hit_rate"] == 6144 / 6250
    assert compat_glm_usage["cache_hit_tokens"] == 5952
    assert compat_glm_usage["cache_total_input_tokens"] == 5992
    assert compat_glm_usage["cache_hit_rate"] == 5952 / 5992


def test_unknown_provider_adapter_does_not_send_extra_body():
    llm = LLMService(
        LLMConfig(
            base_url="http://unit.test",
            model="unknown-model",
            api_key=None,
            timeout=30,
            max_tokens=256,
            temperature=0.0,
            provider="unknown",
        )
    )

    payload = llm.transport.build_payload(
        messages=[{"role": "user", "content": "hi"}],
        enable_reasoning=True,
        stream=True,
        temperature=None,
        max_tokens=None,
        tools=None,
    )

    assert "extra_body" not in payload


def test_llm_call_passes_stream_override_to_transport(monkeypatch):
    llm = LLMService(
        LLMConfig(
            base_url="http://unit.test",
            model="unit-model",
            api_key=None,
            timeout=30,
            max_tokens=256,
            temperature=0.0,
        )
    )

    request_kwargs = []

    def fake_make_request(*args, **kwargs):
        del args
        request_kwargs.append(kwargs)
        return object()

    def fake_collect_response(
        response,
        emit_func=None,
        run_id=None,
        reasoning_summary_callback=None,
        request_label=None,
        tool_calls_closed_callback=None,
    ):
        del response, emit_func, run_id, reasoning_summary_callback, request_label, tool_calls_closed_callback
        return {
            "message": {
                "role": "assistant",
                "content": "sync answer",
            },
            "finish_reason": "stop",
            "model": "unit-model",
        }

    monkeypatch.setattr(llm, "_make_request", fake_make_request)
    monkeypatch.setattr(llm, "_collect_response", fake_collect_response)

    response = llm.call(
        messages=[{"role": "user", "content": "hello"}],
        stream=False,
        enable_reasoning=False,
    )

    assert response.content == "sync answer"
    assert request_kwargs[0]["stream"] is False


def test_llm_with_config_refreshes_transport_agent_config():
    old_agent_config = AgentConfig(
        llm_connection_max_retries=1,
        main_agent_max_iterations=50,
        sub_agent_max_iterations=20,
        sub_agent_max_concurrency=6,
        sub_agent_max_depth=10,
        parallel_default_max_workers=10,
        context_max_rounds=10,
    )
    new_agent_config = AgentConfig(
        llm_connection_max_retries=9,
        main_agent_max_iterations=50,
        sub_agent_max_iterations=20,
        sub_agent_max_concurrency=6,
        sub_agent_max_depth=10,
        parallel_default_max_workers=10,
        context_max_rounds=10,
    )
    old_config = LLMConfig(
        base_url="http://old.test",
        model="old-model",
        api_key=None,
        timeout=30,
        max_tokens=256,
        temperature=0.0,
    )
    new_config = LLMConfig(
        base_url="http://new.test",
        model="new-model",
        api_key=None,
        timeout=30,
        max_tokens=256,
        temperature=0.0,
    )

    llm = LLMService(old_config, agent_config=old_agent_config)
    refreshed = llm.with_config(new_config, agent_config=new_agent_config)

    assert refreshed.agent_config is new_agent_config
    assert refreshed.transport.agent_config is new_agent_config


def test_llm_call_exposes_collected_reasoning_content(monkeypatch):
    llm = LLMService(
        LLMConfig(
            base_url="http://unit.test",
            model="unit-model",
            api_key=None,
            timeout=30,
            max_tokens=256,
            temperature=0.0,
        )
    )

    def fake_make_request(*args, **kwargs):
        del args, kwargs
        return object()

    def fake_collect_response(
        response,
        emit_func=None,
        run_id=None,
        reasoning_summary_callback=None,
        request_label=None,
        tool_calls_closed_callback=None,
    ):
        del response, emit_func, run_id, reasoning_summary_callback, request_label, tool_calls_closed_callback
        return {
            "message": {
                "role": "assistant",
                "content": "",
                "reasoning_content": "先判断是否需要工具",
                "tool_calls": [
                    {
                        "id": "call_read",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            },
            "finish_reason": "tool_calls",
            "model": "unit-model",
        }

    monkeypatch.setattr(llm, "_make_request", fake_make_request)
    monkeypatch.setattr(llm, "_collect_response", fake_collect_response)

    response = llm.call(
        messages=[{"role": "user", "content": "read"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "read",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        enable_reasoning=True,
    )

    assert response.reasoning_content == "先判断是否需要工具"
    assert response.messages[-1]["reasoning_content"] == "先判断是否需要工具"

def test_collect_stream_response_collects_empty_choices_usage_chunks():
    llm = LLMService(
        LLMConfig(
            base_url="http://unit.test",
            model="unit-model",
            api_key=None,
            timeout=30,
            max_tokens=256,
            temperature=0.0,
        )
    )

    response = _FakeStreamingResponse(
        [
            'data: {"id":"chunk-usage","model":"unit-model","choices":[],"usage":{"total_tokens":10}}',
            'data: {"id":"chunk-content","model":"unit-model","choices":[{"delta":{"content":"Hello"},"finish_reason":null}]}',
            'data: {"id":"chunk-stop","model":"unit-model","choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
    )

    collected = llm._collect_stream_response(response)

    assert collected["message"]["content"] == "Hello"
    assert collected["finish_reason"] == "stop"
    assert collected["model"] == "unit-model"
    assert collected["usage"] == {"total_tokens": 10}


def test_collect_non_stream_response_preserves_provider_usage_fields():
    llm = LLMService(
        LLMConfig(
            base_url="http://unit.test",
            model="unit-model",
            api_key=None,
            timeout=30,
            max_tokens=256,
            temperature=0.0,
        )
    )
    response = _FakeOpenAINonStreamCompletion(
        {
            "model": "unit-model",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "ok",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 6250,
                "completion_tokens": 8,
                "total_tokens": 6258,
                "prompt_cache_hit_tokens": 6144,
                "prompt_cache_miss_tokens": 106,
                "prompt_tokens_details": {"cached_tokens": 6144},
            },
        }
    )

    collected = llm._collect_non_stream_response(response)

    assert collected["usage"] == {
        "prompt_tokens": 6250,
        "completion_tokens": 8,
        "total_tokens": 6258,
        "prompt_cache_hit_tokens": 6144,
        "prompt_cache_miss_tokens": 106,
        "prompt_tokens_details": {"cached_tokens": 6144},
    }


def test_collect_stream_response_repairs_cumulative_tool_argument_chunks():
    llm = LLMService(
        LLMConfig(
            base_url="http://unit.test",
            model="unit-model",
            api_key=None,
            timeout=30,
            max_tokens=256,
            temperature=0.0,
        )
    )

    response = _FakeStreamingResponse(
        [
            (
                'data: {"id":"chunk-1","model":"unit-model","choices":[{"delta":{"tool_calls":'
                '[{"index":0,"id":"call_1","type":"function","function":{"name":"read_file","arguments":"{\\"file_path\\":\\"ontologies/example-domain-operations-manual/manifest.yaml\\""}}]},'
                '"finish_reason":null}]}'
            ),
            (
                'data: {"id":"chunk-2","model":"unit-model","choices":[{"delta":{"tool_calls":'
                '[{"index":0,"function":{"arguments":"{\\"file_path\\": \\"ontologies/example-domain-operations-manual/manifest.yaml\\"}"}}]},'
                '"finish_reason":"tool_calls"}]}'
            ),
            "data: [DONE]",
        ]
    )

    collected = llm._collect_stream_response(response)
    tool_calls = collected["message"]["tool_calls"]

    assert collected["finish_reason"] == "tool_calls"
    assert tool_calls[0]["function"]["name"] == "read_file"
    assert json.loads(tool_calls[0]["function"]["arguments"]) == {
        "file_path": "ontologies/example-domain-operations-manual/manifest.yaml"
    }


def test_collect_stream_response_keeps_final_tool_call_fragment_when_finish_reason_arrives_same_chunk():
    llm = LLMService(
        LLMConfig(
            base_url="http://unit.test",
            model="unit-model",
            api_key=None,
            timeout=30,
            max_tokens=256,
            temperature=0.0,
        )
    )

    response = _FakeStreamingResponse(
        [
            (
                'data: {"id":"chunk-1","model":"unit-model","choices":[{"delta":{"tool_calls":'
                '[{"index":0,"id":"call_1","type":"function","function":{"name":"read_file","arguments":"{\\"file_path\\":\\"ontologies/example-domain-operations-manual/objects/CHANGE_"}}]},'
                '"finish_reason":null}]}'
            ),
            (
                'data: {"id":"chunk-2","model":"unit-model","choices":[{"delta":{"tool_calls":'
                '[{"index":0,"function":{"arguments":"ORDER受影响对象在制品处理结论.md\\"}"}}]},'
                '"finish_reason":"stop"}]}'
            ),
            "data: [DONE]",
        ]
    )

    collected = llm._collect_stream_response(response)
    tool_calls = collected["message"]["tool_calls"]

    assert collected["finish_reason"] == "stop"
    assert tool_calls[0]["function"]["name"] == "read_file"
    assert json.loads(tool_calls[0]["function"]["arguments"]) == {
        "file_path": "ontologies/example-domain-operations-manual/objects/CHANGE_ORDER受影响对象在制品处理结论.md"
    }


def test_collect_stream_response_emits_closed_tool_calls_as_one_batch_on_finish_reason():
    llm = LLMService(
        LLMConfig(
            base_url="http://unit.test",
            model="unit-model",
            api_key=None,
            timeout=30,
            max_tokens=256,
            temperature=0.0,
        )
    )

    response = _FakeStreamingResponse(
        [
            (
                'data: {"id":"chunk-1","model":"unit-model","choices":[{"delta":{"tool_calls":'
                '[{"index":0,"id":"call_read","type":"function","function":{"name":"read_file","arguments":"{\\"file_path\\":\\"a.md\\"}"}},'
                '{"index":1,"id":"call_search","type":"function","function":{"name":"business_data_search","arguments":"{\\"query\\":\\"TargetItem\\"}"}}]},'
                '"finish_reason":null}]}'
            ),
            'data: {"id":"chunk-2","model":"unit-model","choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
        ]
    )

    closed_batches = []
    collected = llm._collect_stream_response(
        response,
        tool_calls_closed_callback=lambda tool_calls: closed_batches.append(tool_calls),
    )

    assert len(closed_batches) == 1
    assert [tool_call["function"]["name"] for tool_call in closed_batches[0]] == [
        "read_file",
        "business_data_search",
    ]
    assert collected["message"]["tool_calls"][0]["function"]["name"] == "read_file"
    assert collected["message"]["tool_calls"][1]["function"]["name"] == "business_data_search"


def test_collect_stream_response_emits_remaining_closed_tool_calls_on_finish_reason():
    llm = LLMService(
        LLMConfig(
            base_url="http://unit.test",
            model="unit-model",
            api_key=None,
            timeout=30,
            max_tokens=256,
            temperature=0.0,
        )
    )

    response = _FakeStreamingResponse(
        [
            (
                'data: {"id":"chunk-1","model":"unit-model","choices":[{"delta":{"tool_calls":'
                '[{"index":0,"id":"call_read","type":"function","function":{"name":"read_file","arguments":"{\\"file_path\\":\\"a.md\\"}"}}]},'
                '"finish_reason":"tool_calls"}]}'
            ),
            "data: [DONE]",
        ]
    )

    closed_batches = []
    llm._collect_stream_response(
        response,
        tool_calls_closed_callback=lambda tool_calls: closed_batches.append(tool_calls),
    )

    assert len(closed_batches) == 1
    assert closed_batches[0][0]["id"] == "call_read"
    assert json.loads(closed_batches[0][0]["function"]["arguments"]) == {"file_path": "a.md"}


def test_collect_stream_response_defers_closed_tool_calls_until_id_and_arguments_are_complete():
    llm = LLMService(
        LLMConfig(
            base_url="http://unit.test",
            model="unit-model",
            api_key=None,
            timeout=30,
            max_tokens=256,
            temperature=0.0,
        )
    )

    chunk_1 = {
        "id": "chunk-1",
        "model": "unit-model",
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_read",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"file_path":"a',
                            },
                        },
                        {
                            "index": 1,
                            "id": "call_search",
                            "type": "function",
                            "function": {
                                "name": "business_data_search",
                                "arguments": '{"query":"TargetItem"}',
                            },
                        },
                    ]
                },
                "finish_reason": None,
            }
        ],
    }
    chunk_2 = {
        "id": "chunk-2",
        "model": "unit-model",
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "function": {
                                "arguments": '.md"}',
                            },
                        }
                    ]
                },
                "finish_reason": "tool_calls",
            }
        ],
    }

    response = _FakeStreamingResponse(
        [
            "data: {}".format(json.dumps(chunk_1, ensure_ascii=False)),
            "data: {}".format(json.dumps(chunk_2, ensure_ascii=False)),
            "data: [DONE]",
        ]
    )

    closed_batches = []
    collected = llm._collect_stream_response(
        response,
        tool_calls_closed_callback=lambda tool_calls: closed_batches.append(tool_calls),
    )

    assert len(closed_batches) == 1
    assert [tool_call["function"]["name"] for tool_call in closed_batches[0]] == [
        "read_file",
        "business_data_search",
    ]
    assert json.loads(closed_batches[0][0]["function"]["arguments"]) == {"file_path": "a.md"}
    assert [tool_call["index"] for tool_call in collected["message"]["tool_calls"]] == [0, 1]


def test_collect_non_stream_response_preserves_tool_calls():
    llm = LLMService(
        LLMConfig(
            base_url="http://unit.test",
            model="unit-model",
            api_key=None,
            timeout=30,
            max_tokens=256,
            temperature=0.0,
        )
    )

    response = _FakeOpenAINonStreamCompletion(
        {
            "model": "unit-model",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"file_path":"ontologies/example-domain-operations-manual/objects/CHANGE_ORDER受影响对象在制品处理结论.md"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
    )

    collected = llm._collect_response(response)
    tool_calls = collected["message"]["tool_calls"]

    assert collected["finish_reason"] == "tool_calls"
    assert collected["model"] == "unit-model"
    assert json.loads(tool_calls[0]["function"]["arguments"]) == {
        "file_path": "ontologies/example-domain-operations-manual/objects/CHANGE_ORDER受影响对象在制品处理结论.md"
    }


def test_collect_non_stream_response_dispatches_reasoning_summary(monkeypatch):
    llm = LLMService(
        LLMConfig(
            base_url="http://unit.test",
            model="unit-model",
            api_key=None,
            timeout=30,
            max_tokens=256,
            temperature=0.0,
        )
    )
    dispatches = []
    monkeypatch.setattr(
        llm,
        "_dispatch_reasoning_summary",
        lambda **kwargs: dispatches.append(kwargs) or True,
    )

    response = _FakeOpenAINonStreamCompletion(
        {
            "model": "unit-model",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "final answer",
                        "reasoning_content": "reasoning trace",
                    },
                    "finish_reason": "stop",
                }
            ],
        }
    )

    collected = llm._collect_non_stream_response(
        response,
        run_id="run-1",
        reasoning_summary_callback=lambda block_id, summary: None,
        request_label="main_agent_iteration",
    )

    assert collected["message"]["reasoning_content"] == "reasoning trace"
    assert len(dispatches) == 1


def test_collect_stream_response_keeps_reasoning_when_summary_dispatch_fails(monkeypatch):
    llm = LLMService(
        LLMConfig(
            base_url="http://unit.test",
            model="unit-model",
            api_key=None,
            timeout=30,
            max_tokens=256,
            temperature=0.0,
        )
    )
    dispatches = []
    monkeypatch.setattr(
        llm,
        "_dispatch_reasoning_summary",
        lambda **kwargs: dispatches.append(kwargs) and False,
    )

    response = _FakeStreamingResponse(
        [
            'data: {"id":"chunk-1","model":"unit-model","choices":[{"delta":{"reasoning_content":"think"},"finish_reason":null}]}',
            'data: {"id":"chunk-2","model":"unit-model","choices":[{"delta":{"content":"done"},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
    )

    collected = llm._collect_stream_response(
        response,
        run_id="run-2",
        reasoning_summary_callback=lambda block_id, summary: None,
        request_label="main_agent_iteration",
    )

    assert collected["message"]["reasoning_content"] == "think"
    assert len(dispatches) == 1


def test_collect_stream_response_returns_stream_milestones():
    llm = LLMService(
        LLMConfig(
            base_url="http://unit.test",
            model="unit-model",
            api_key=None,
            timeout=30,
            max_tokens=256,
            temperature=0.0,
        )
    )
    response = _FakeStreamingResponse(
        [
            'data: {"id":"chunk-1","model":"unit-model","choices":[{"delta":{"reasoning_content":"think"},"finish_reason":null}]}',
            'data: {"id":"chunk-2","model":"unit-model","choices":[{"delta":{"content":"done"},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
    )

    collected = llm._collect_stream_response(response)

    assert collected["first_reasoning_ms"] is not None
    assert collected["first_content_ms"] is not None
    assert collected["first_tool_call_ms"] is None
