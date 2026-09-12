"""Phase A offline regression baseline for the OpenJiuwen ContextEngine.

These tests use the real ``openjiuwen==0.1.17`` ContextEngine and ReActAgent.
A fake model captures the final ``invoke`` and ``stream`` message arguments.

Test matrix:
1. Full history is seeded into the ContextEngine via ``create_context``.
2. ``get_context_window`` truncates to the configured number of rounds.
3. Tool-call / tool-result pairing is never split across the window boundary.
4. The ask-user recovery scenario uses ``effective_rounds = max(configured, 2)``
   so the model always sees at least the preceding user turn.
5. ``max_context_message_num=None`` (as configured by the factory) does not
   truncate single rounds with >200 messages.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
from typing import List
from uuid import uuid4

import pytest
from backend_synthetic_credentials import synthetic_secret
from openjiuwen.core.context_engine import ContextEngine, ContextEngineConfig
from openjiuwen.core.foundation.llm import (
    AssistantMessage,
    AssistantMessageChunk,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.single_agent import ReActAgent, ReActAgentConfig
from openjiuwen.core.single_agent.rail.base import AgentCallbackEvent
from openjiuwen.core.single_agent.schema.agent_card import AgentCard


def test_phase_a_uses_requirements_pinned_openjiuwen_version():
    assert importlib.metadata.version("openjiuwen") == "0.1.17"


def test_default_factory_configures_openjiuwen_0_1_17_runtime_features():
    from types import SimpleNamespace

    from agent.core.jiuwen_engine import create_default_jiuwen_agent

    agent = create_default_jiuwen_agent(
        llm_config=SimpleNamespace(
            provider="openai_compatible",
            api_key=synthetic_secret("context_engine_model_window-credential-01"),
            base_url="https://llm.example.com/v1",
            model="model-a",
            timeout=30,
            max_tokens=128,
            temperature=0.2,
        ),
        max_iterations=1,
        context_max_rounds=3,
    )

    config = agent.config
    assert config.context_processors == []
    assert config.parallel_tool_calls is True
    assert config.kv_cache_affinity_config.enable_kv_cache_release is False
    assert config.kv_cache_affinity_config.enable_kv_cache_affinity is False


def _run(coro):
    """Run a coroutine to completion in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_rounds(n: int) -> List:
    """Build ``n`` complete rounds: user → assistant (no tool calls)."""
    messages = []
    for i in range(n):
        messages.append(UserMessage(content=f"user turn {i}"))
        messages.append(AssistantMessage(content=f"assistant turn {i}"))
    return messages


def _make_tool_rounds(n: int) -> List:
    """Build ``n`` rounds where each assistant turn calls a tool.

    Round structure: user → assistant(tool_calls) → tool(result) → assistant(final)
    """
    messages = []
    for i in range(n):
        messages.append(UserMessage(content=f"user turn {i}"))
        messages.append(
            AssistantMessage(
                content="",
                tool_calls=[
                    {
                        "id": f"call-{i}",
                        "type": "function",
                        "name": "search",
                        "arguments": '{"query": "test"}',
                    }
                ],
            )
        )
        messages.append(
            ToolMessage(
                content=f"tool result {i}",
                tool_call_id=f"call-{i}",
            )
        )
        messages.append(AssistantMessage(content=f"final answer {i}"))
    return messages


def _message_payloads(messages: List) -> list[dict]:
    return [message.model_dump() if hasattr(message, "model_dump") else dict(message) for message in messages]


class _CapturingFakeModel:
    def __init__(self) -> None:
        self.invoke_messages: list[list] = []
        self.stream_messages: list[list] = []

    @staticmethod
    def supports_kv_cache_release() -> bool:
        return False

    async def invoke(self, messages, **_kwargs):
        self.invoke_messages.append(list(messages))
        return AssistantMessage(content="captured invoke")

    async def stream(self, messages, **_kwargs):
        self.stream_messages.append(list(messages))
        yield AssistantMessageChunk(content="captured stream")


async def _capture_real_react_model_window(
    *,
    history: List,
    configured_rounds: int,
    stream: bool,
) -> tuple[list[dict], list[dict]]:
    session_id = f"context-window-{uuid4().hex}"
    card = AgentCard(id=f"context-window-agent-{uuid4().hex}", name="context-window-agent")
    agent = ReActAgent(card=card).configure(
        ReActAgentConfig(
            model_name="fake-model",
            max_iterations=1,
            prompt_template=[{"role": "system", "content": "system prompt"}],
            context_engine_config=ContextEngineConfig(
                max_context_message_num=None,
                default_window_round_num=configured_rounds,
                enable_reload=False,
            ),
        )
    )
    fake_model = _CapturingFakeModel()
    agent._llm = fake_model
    preview_messages: list[list] = []

    async def capture_preview(ctx):
        preview_messages.append(list(ctx.inputs.messages))

    await agent.agent_callback_manager.register_callback(
        AgentCallbackEvent.BEFORE_MODEL_CALL,
        capture_preview,
    )
    inputs = {"query": "current user turn", "conversation_id": session_id}
    session = create_agent_session(session_id=session_id, card=card)
    await session.pre_run(inputs=inputs)
    await agent.context_engine.create_context(session=session, history_messages=history)
    try:
        if stream:
            async for _ in agent.stream(inputs, session=session):
                pass
            captured = fake_model.stream_messages
        else:
            await agent.invoke(inputs, session=session)
            captured = fake_model.invoke_messages
    finally:
        await session.close_stream()
        await session.commit()
    assert len(captured) == 1
    assert len(preview_messages) == 1
    return _message_payloads(captured[0]), _message_payloads(preview_messages[0])


@pytest.mark.parametrize("stream", [False, True], ids=["invoke", "stream"])
def test_real_react_fake_model_receives_final_context_window(stream):
    final_messages, preview_messages = _run(
        _capture_real_react_model_window(
            history=_make_rounds(5),
            configured_rounds=2,
            stream=stream,
        )
    )

    assert [message["role"] for message in final_messages] == ["system", "user", "assistant", "user"]
    assert [message["content"] for message in final_messages[1:]] == [
        "user turn 4",
        "assistant turn 4",
        "current user turn",
    ]
    assert [message["content"] for message in final_messages].count("current user turn") == 1
    assert len(preview_messages) == 12
    assert preview_messages[1]["content"] == "user turn 0"
    assert preview_messages[-1]["content"] == "current user turn"


def test_real_react_fake_model_keeps_long_interleaved_tool_round_intact():
    final_messages, _ = _run(
        _capture_real_react_model_window(
            history=_make_tool_rounds(30),
            configured_rounds=2,
            stream=False,
        )
    )

    assert [message["role"] for message in final_messages] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
    ]
    assert final_messages[2]["tool_calls"][0]["id"] == "call-29"
    assert final_messages[3]["tool_call_id"] == "call-29"


# ---------------------------------------------------------------------------
# ContextEngine window behaviour
# ---------------------------------------------------------------------------


class TestContextEngineWindowByRounds:
    """Verify ContextEngine truncates history by ``default_window_round_num``."""

    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def test_full_history_returned_when_round_num_exceeds_history(self):
        """When default_window_round_num >= total rounds, all messages survive."""
        config = ContextEngineConfig(
            max_context_message_num=None,
            default_window_round_num=10,
        )
        engine = ContextEngine(config)
        history = _make_rounds(3)  # 6 messages, 3 rounds
        context = _run(
            engine.create_context(
                context_id="test-full",
                history_messages=history,
            )
        )
        window = _run(context.get_context_window())
        # system_messages is empty (none passed), so get_messages() == context_messages
        msgs = window.get_messages()
        assert len(msgs) == 6
        assert msgs[0].content == "user turn 0"
        assert msgs[-1].content == "assistant turn 2"

    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def test_truncates_to_last_n_rounds(self):
        """Only the last N rounds are retained."""
        config = ContextEngineConfig(
            max_context_message_num=None,
            default_window_round_num=2,
        )
        engine = ContextEngine(config)
        history = _make_rounds(5)  # 10 messages, 5 rounds
        context = _run(
            engine.create_context(
                context_id="test-trunc",
                history_messages=history,
            )
        )
        window = _run(context.get_context_window())
        msgs = window.get_messages()
        # 2 rounds = 4 messages (user + assistant per round)
        assert len(msgs) == 4
        assert msgs[0].content == "user turn 3"
        assert msgs[1].content == "assistant turn 3"
        assert msgs[2].content == "user turn 4"
        assert msgs[3].content == "assistant turn 4"

    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def test_truncates_to_one_round(self):
        config = ContextEngineConfig(
            max_context_message_num=None,
            default_window_round_num=1,
        )
        engine = ContextEngine(config)
        history = _make_rounds(5)
        context = _run(
            engine.create_context(
                context_id="test-one-round",
                history_messages=history,
            )
        )
        window = _run(context.get_context_window())
        msgs = window.get_messages()
        assert len(msgs) == 2
        assert msgs[0].content == "user turn 4"
        assert msgs[1].content == "assistant turn 4"

    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def test_system_messages_prepended(self):
        config = ContextEngineConfig(
            max_context_message_num=None,
            default_window_round_num=2,
        )
        engine = ContextEngine(config)
        history = _make_rounds(5)
        context = _run(
            engine.create_context(
                context_id="test-sys",
                history_messages=history,
            )
        )
        system_msg = SystemMessage(content="you are a helpful assistant")
        window = _run(
            context.get_context_window(
                system_messages=[system_msg],
            )
        )
        msgs = window.get_messages()
        assert msgs[0].role == "system"
        assert msgs[0].content == "you are a helpful assistant"
        # Remaining messages are the last 2 rounds
        assert len(msgs) == 5  # 1 system + 4 context


# ---------------------------------------------------------------------------
# Tool-call pairing preservation
# ---------------------------------------------------------------------------


class TestToolCallPairingPreserved:
    """Verify tool-call sequences are never split across the window boundary."""

    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def test_tool_round_not_split(self):
        """A round with tool calls must stay fully within the window."""
        config = ContextEngineConfig(
            max_context_message_num=None,
            default_window_round_num=1,
        )
        engine = ContextEngine(config)
        history = _make_tool_rounds(3)  # 12 messages, 3 tool rounds
        context = _run(
            engine.create_context(
                context_id="test-tool-pairing",
                history_messages=history,
            )
        )
        window = _run(context.get_context_window())
        msgs = window.get_messages()

        # 1 tool round = 4 messages: user, assistant(tool_calls), tool, assistant(final)
        assert len(msgs) == 4
        assert msgs[0].role == "user"
        assert msgs[0].content == "user turn 2"
        assert msgs[1].role == "assistant"
        assert msgs[1].tool_calls is not None
        assert msgs[1].tool_calls[0].id == "call-2"
        assert msgs[2].role == "tool"
        assert msgs[2].tool_call_id == "call-2"
        assert msgs[3].role == "assistant"
        assert msgs[3].content == "final answer 2"

    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def test_two_tool_rounds_preserved(self):
        """Two tool rounds must both be fully present."""
        config = ContextEngineConfig(
            max_context_message_num=None,
            default_window_round_num=2,
        )
        engine = ContextEngine(config)
        history = _make_tool_rounds(4)  # 16 messages, 4 tool rounds
        context = _run(
            engine.create_context(
                context_id="test-two-tool-rounds",
                history_messages=history,
            )
        )
        window = _run(context.get_context_window())
        msgs = window.get_messages()

        # 2 tool rounds = 8 messages
        assert len(msgs) == 8
        # Verify tool_call_id pairing for the last round
        assert msgs[-3].role == "assistant"
        assert msgs[-3].tool_calls is not None
        assert msgs[-3].tool_calls[0].id == "call-3"
        assert msgs[-2].role == "tool"
        assert msgs[-2].tool_call_id == "call-3"

    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def test_tool_call_id_matching(self):
        """Every tool message must have a matching preceding assistant tool_call."""
        config = ContextEngineConfig(
            max_context_message_num=None,
            default_window_round_num=3,
        )
        engine = ContextEngine(config)
        history = _make_tool_rounds(5)
        context = _run(
            engine.create_context(
                context_id="test-tool-id-matching",
                history_messages=history,
            )
        )
        window = _run(context.get_context_window())
        msgs = window.get_messages()

        # Collect all tool_call IDs from assistant messages
        assistant_tool_call_ids = set()
        for msg in msgs:
            if msg.role == "assistant" and msg.tool_calls:
                for tc in msg.tool_calls:
                    assistant_tool_call_ids.add(tc.id)

        # Every tool message must have a matching tool_call_id
        for msg in msgs:
            if msg.role == "tool":
                assert (
                    msg.tool_call_id in assistant_tool_call_ids
                ), f"Tool message with tool_call_id={msg.tool_call_id} has no matching assistant tool_call"


# ---------------------------------------------------------------------------
# Ask-user recovery: effective_rounds = max(configured, 2)
# ---------------------------------------------------------------------------


class TestAskUserRecoveryEffectiveRounds:
    """Verify the ask-user recovery scenario uses max(configured, 2) rounds."""

    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def test_configured_one_becomes_two_for_resume(self):
        """When configured_rounds=1 and resume is active, the ContextEngine
        must receive default_window_round_num=2 (via _resolve_effective_context_max_rounds).
        Here we verify that ContextEngineConfig(2) retains 2 rounds."""
        from agent.core.jiuwen_engine import _resolve_effective_context_max_rounds

        state = {"user_context": {"interaction_response": {"choice": "A"}}}
        effective = _resolve_effective_context_max_rounds(1, state)
        assert effective == 2

        config = ContextEngineConfig(
            max_context_message_num=None,
            default_window_round_num=effective,
        )
        engine = ContextEngine(config)
        # 3 rounds; with effective=2, only last 2 should survive
        history = _make_rounds(3)
        context = _run(
            engine.create_context(
                context_id="test-resume",
                history_messages=history,
            )
        )
        window = _run(context.get_context_window())
        msgs = window.get_messages()
        assert len(msgs) == 4  # 2 rounds
        assert msgs[0].content == "user turn 1"
        assert msgs[-1].content == "assistant turn 2"

    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def test_configured_two_stays_two_for_resume(self):
        from agent.core.jiuwen_engine import _resolve_effective_context_max_rounds

        state = {"user_context": {"interaction_response": {"choice": "A"}}}
        effective = _resolve_effective_context_max_rounds(2, state)
        assert effective == 2

        config = ContextEngineConfig(
            max_context_message_num=None,
            default_window_round_num=effective,
        )
        engine = ContextEngine(config)
        history = _make_rounds(5)
        context = _run(
            engine.create_context(
                context_id="test-resume-two",
                history_messages=history,
            )
        )
        window = _run(context.get_context_window())
        msgs = window.get_messages()
        assert len(msgs) == 4  # 2 rounds

    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def test_configured_five_stays_five_for_resume(self):
        from agent.core.jiuwen_engine import _resolve_effective_context_max_rounds

        state = {"user_context": {"interaction_response": {"choice": "A"}}}
        effective = _resolve_effective_context_max_rounds(5, state)
        assert effective == 5

        config = ContextEngineConfig(
            max_context_message_num=None,
            default_window_round_num=effective,
        )
        engine = ContextEngine(config)
        history = _make_rounds(7)
        context = _run(
            engine.create_context(
                context_id="test-resume-five",
                history_messages=history,
            )
        )
        window = _run(context.get_context_window())
        msgs = window.get_messages()
        assert len(msgs) == 10  # 5 rounds


# ---------------------------------------------------------------------------
# max_context_message_num=None does not truncate large single rounds
# ---------------------------------------------------------------------------


class TestNoHardMessageLimit:
    """Verify max_context_message_num=None (factory setting) does not truncate."""

    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def test_large_history_within_single_round_not_truncated(self):
        """A single round with >200 messages must not be truncated when
        max_context_message_num=None."""
        config = ContextEngineConfig(
            max_context_message_num=None,
            default_window_round_num=1,
        )
        engine = ContextEngine(config)

        # Build a single round with many tool calls (>200 messages total)
        history: List = [UserMessage(content="start")]
        for i in range(250):
            history.append(
                AssistantMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": f"call-{i}",
                            "type": "function",
                            "name": "noop",
                            "arguments": "{}",
                        }
                    ],
                )
            )
            history.append(
                ToolMessage(
                    content=f"result {i}",
                    tool_call_id=f"call-{i}",
                )
            )
        # Close the round with a final assistant message (no tool calls)
        history.append(AssistantMessage(content="done"))

        context = _run(
            engine.create_context(
                context_id="test-large",
                history_messages=history,
            )
        )
        window = _run(context.get_context_window())
        msgs = window.get_messages()

        # All 502 messages should survive: 1 user + 250*(assistant+tool) + 1 final
        assert len(msgs) == 502

    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def test_hard_limit_truncates_when_set(self):
        """When max_context_message_num is set, it enforces a hard cap."""
        config = ContextEngineConfig(
            max_context_message_num=10,
            default_window_round_num=10,
        )
        engine = ContextEngine(config)
        history = _make_rounds(10)  # 20 messages
        context = _run(
            engine.create_context(
                context_id="test-hard-limit",
                history_messages=history,
            )
        )
        # The message buffer should cap at 10 messages
        assert len(context) <= 10


def test_real_react_resume_uses_effective_two_round_window():
    from agent.core.jiuwen_engine import _resolve_effective_context_max_rounds

    effective_rounds = _resolve_effective_context_max_rounds(
        1,
        {"user_context": {"interaction_response": {"choice": "A"}}},
    )
    ask_user_history = [
        UserMessage(content="need a deployment choice"),
        AssistantMessage(
            content="",
            tool_calls=[
                {
                    "id": "ask-1",
                    "type": "function",
                    "name": "request_user_interaction",
                    "arguments": '{"prompt":"choose"}',
                }
            ],
        ),
        ToolMessage(content='{"status":"awaiting_input"}', tool_call_id="ask-1"),
    ]
    final_messages, _ = _run(
        _capture_real_react_model_window(
            history=ask_user_history,
            configured_rounds=effective_rounds,
            stream=False,
        )
    )

    assert effective_rounds == 2
    assert [message["role"] for message in final_messages] == [
        "system",
        "user",
        "assistant",
        "tool",
        "user",
    ]
    assert final_messages[2]["tool_calls"][0]["id"] == "ask-1"
    assert final_messages[3]["tool_call_id"] == "ask-1"
    assert final_messages[-1]["content"] == "current user turn"
