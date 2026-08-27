"""Phase A regression probes for the OpenJiuwen ContextEngine cutover.

These tests validate the cutover-specific behaviour introduced in Phase A
without requiring a live LLM:

1. ``_resolve_effective_context_max_rounds`` clamps to ``max(configured, 2)``
   for ask-user resume requests and returns the configured value otherwise.
2. ``_reject_factory_agent_with_history`` blocks the factory path when
   conversation history or an ``interaction_response`` is present.
3. ``sliding_window_applied`` is ``True`` only for the default agent path and
   ``False`` for the factory path, and is now a required parameter of
   ``JiuwenModelTraceBridge``.
4. The engine no longer imports or calls ``apply_sliding_window``.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest

from agent.core.jiuwen_engine import (
    JiuwenReactLoopEngine,
    _reject_factory_agent_with_history,
    _resolve_effective_context_max_rounds,
)
from agent.core.jiuwen.trace import JiuwenModelTraceBridge
from config.settings import AgentConfig


def _agent_config(**overrides):
    config = {
        "llm_connection_max_retries": 3,
        "main_agent_max_iterations": 50,
        "sub_agent_max_iterations": 5,
        "sub_agent_max_concurrency": 4,
        "sub_agent_max_depth": 2,
        "parallel_default_max_workers": 10,
        "context_max_rounds": 10,
    }
    config.update(overrides)
    return AgentConfig(**config)


# ---------------------------------------------------------------------------
# _resolve_effective_context_max_rounds
# ---------------------------------------------------------------------------

class TestResolveEffectiveContextMaxRounds:
    """Verify effective_rounds = max(configured_rounds, 2) for ask-user resume."""

    @pytest.mark.parametrize("configured", [1, 2, 5, 10])
    def test_normal_request_returns_configured_value(self, configured):
        state = {"user_context": {}}
        assert _resolve_effective_context_max_rounds(configured, state) == configured

    @pytest.mark.parametrize("configured", [1, 2, 5, 10])
    def test_resume_request_clamps_to_at_least_two(self, configured):
        state = {"user_context": {"interaction_response": {"choice": "A"}}}
        assert _resolve_effective_context_max_rounds(configured, state) == max(configured, 2)

    def test_resume_with_configured_one_returns_two(self):
        """The critical ask-user recovery case: configured=1 must become 2."""
        state = {"user_context": {"interaction_response": {"choice": "A"}}}
        assert _resolve_effective_context_max_rounds(1, state) == 2

    def test_resume_with_configured_two_returns_two(self):
        state = {"user_context": {"interaction_response": {"choice": "A"}}}
        assert _resolve_effective_context_max_rounds(2, state) == 2

    def test_resume_with_configured_five_returns_five(self):
        state = {"user_context": {"interaction_response": {"choice": "A"}}}
        assert _resolve_effective_context_max_rounds(5, state) == 5

    def test_none_state_treated_as_normal(self):
        assert _resolve_effective_context_max_rounds(5, None) == 5

    def test_empty_interaction_response_is_normal(self):
        """An empty/None interaction_response must not trigger the clamp."""
        state = {"user_context": {"interaction_response": None}}
        assert _resolve_effective_context_max_rounds(1, state) == 1

    def test_empty_dict_interaction_response_is_normal(self):
        """An empty dict interaction_response is falsy, so treated as normal.
        This matches the orchestrator's own truthiness check."""
        state = {"user_context": {"interaction_response": {}}}
        assert _resolve_effective_context_max_rounds(1, state) == 1


# ---------------------------------------------------------------------------
# _reject_factory_agent_with_history
# ---------------------------------------------------------------------------

class TestRejectFactoryAgentWithHistory:
    """Verify the factory path is blocked when history is present."""

    def test_none_state_passes(self):
        _reject_factory_agent_with_history(None)

    def test_empty_state_passes(self):
        _reject_factory_agent_with_history({})

    def test_single_user_message_passes(self):
        """A single user turn (the current request) is allowed."""
        _reject_factory_agent_with_history({
            "messages": [{"role": "user", "content": "hello"}],
        })

    def test_system_plus_user_passes(self):
        _reject_factory_agent_with_history({
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "hello"},
            ],
        })

    def test_assistant_history_rejected(self):
        with pytest.raises(ValueError, match="conversation history"):
            _reject_factory_agent_with_history({
                "messages": [
                    {"role": "user", "content": "q1"},
                    {"role": "assistant", "content": "a1"},
                    {"role": "user", "content": "q2"},
                ],
            })

    def test_tool_history_rejected(self):
        with pytest.raises(ValueError, match="conversation history"):
            _reject_factory_agent_with_history({
                "messages": [
                    {"role": "user", "content": "q1"},
                    {"role": "assistant", "content": "a1", "tool_calls": []},
                    {"role": "tool", "content": "result", "tool_call_id": "tc1"},
                    {"role": "user", "content": "q2"},
                ],
            })

    def test_consecutive_user_history_rejected(self):
        with pytest.raises(ValueError, match="conversation history"):
            _reject_factory_agent_with_history({
                "user_request": "q2",
                "messages": [
                    {"role": "user", "content": "q1"},
                    {"role": "user", "content": "q2"},
                ],
            })

    def test_interaction_response_rejected(self):
        with pytest.raises(ValueError, match="interaction_response"):
            _reject_factory_agent_with_history({
                "user_context": {"interaction_response": {"choice": "A"}},
                "messages": [{"role": "user", "content": "resume"}],
            })

    def test_interaction_response_rejected_even_without_history(self):
        with pytest.raises(ValueError, match="interaction_response"):
            _reject_factory_agent_with_history({
                "user_context": {"interaction_response": {"choice": "A"}},
            })


# ---------------------------------------------------------------------------
# JiuwenModelTraceBridge: sliding_window_applied is required
# ---------------------------------------------------------------------------

class TestTraceBridgeSlidingWindowAppliedRequired:
    """Verify ``sliding_window_applied`` is a required parameter."""

    def test_parameter_has_no_default(self):
        sig = inspect.signature(JiuwenModelTraceBridge.__init__)
        param = sig.parameters["sliding_window_applied"]
        assert param.default is inspect.Parameter.empty, (
            "sliding_window_applied must be a required parameter (no default)"
        )

    def test_missing_parameter_raises_type_error(self):
        """Calling without sliding_window_applied must raise TypeError."""
        with pytest.raises(TypeError, match="sliding_window_applied"):
            JiuwenModelTraceBridge(
                runtime_trace=SimpleNamespace(enabled=False),
                state={},
                caller="main",
                iteration_provider=lambda: 1,
                model_provider=None,
                stream_enabled=True,
                enable_reasoning=False,
            )


# ---------------------------------------------------------------------------
# Engine no longer imports apply_sliding_window
# ---------------------------------------------------------------------------

class TestEngineDoesNotImportSlidingWindow:
    """Verify the engine module no longer imports apply_sliding_window."""

    def test_apply_sliding_window_not_in_module_namespace(self):
        import agent.core.jiuwen_engine as engine_module
        assert not hasattr(engine_module, "apply_sliding_window"), (
            "jiuwen_engine must not import apply_sliding_window; "
            "ContextEngine is solely responsible for windowing."
        )

    def test_build_history_messages_does_not_call_sliding_window(self, monkeypatch):
        """_build_jiuwen_history_messages must forward full history without
        calling apply_sliding_window."""
        calls = []

        def fail_if_called(messages, max_rounds):
            calls.append((messages, max_rounds))
            raise AssertionError("apply_sliding_window must not be called")

        monkeypatch.setattr("agent.context.apply_sliding_window", fail_if_called)

        from agent.core.jiuwen_engine import _build_jiuwen_history_messages

        messages = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
        ]
        _build_jiuwen_history_messages({"messages": messages}, _agent_config())
        assert calls == []

    def test_build_context_messages_does_not_call_sliding_window(self, monkeypatch):
        """_build_jiuwen_context_messages must forward full history without
        calling apply_sliding_window."""
        calls = []

        def fail_if_called(messages, max_rounds):
            calls.append((messages, max_rounds))
            raise AssertionError("apply_sliding_window must not be called")

        monkeypatch.setattr("agent.context.apply_sliding_window", fail_if_called)

        from agent.core.jiuwen_engine import _build_jiuwen_context_messages

        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
        ]
        result = _build_jiuwen_context_messages(
            {"messages": messages},
            inputs={"query": "q2"},
            system_prompt="sys",
            agent_config=_agent_config(context_max_rounds=1),
        )
        assert calls == []
        # Full history (minus system, which is re-added from system_prompt)
        # is forwarded as the estimated messages.
        roles = [m["role"] for m in result]
        assert roles[0] == "system"
        assert "user" in roles
        assert "assistant" in roles


# ---------------------------------------------------------------------------
# _create_agent passes effective_rounds and rejects factory with history
# ---------------------------------------------------------------------------

class TestCreateAgentEffectiveRounds:
    """Verify _create_agent passes effective_rounds to the factory."""

    def _setup_factory_spy(self, monkeypatch):
        captured = {}

        def fake_factory(*, llm_config, max_iterations, system_prompt=None, agent_id=None,
                         context_max_rounds=None, **_kwargs):
            captured["context_max_rounds"] = context_max_rounds
            del llm_config, max_iterations, system_prompt, agent_id
            return SimpleNamespace()

        monkeypatch.setattr(
            "agent.core.jiuwen_engine.create_default_jiuwen_agent", fake_factory
        )
        monkeypatch.setattr(
            "agent.core.jiuwen_engine._resolve_jiuwen_llm_config",
            lambda state, *, caller, agent_config=None: SimpleNamespace(
                provider="openai", api_key="k", base_url="u", model="m",
                timeout=30, max_tokens=2048, temperature=0.2,
            ),
        )
        return captured

    def test_normal_request_uses_configured_rounds(self, monkeypatch):
        captured = self._setup_factory_spy(monkeypatch)
        engine = JiuwenReactLoopEngine(agent_config=_agent_config(context_max_rounds=5))
        engine._create_agent(max_iterations=10, system_prompt="p", caller="main")
        assert captured["context_max_rounds"] == 5

    def test_resume_request_clamps_to_at_least_two(self, monkeypatch):
        captured = self._setup_factory_spy(monkeypatch)
        engine = JiuwenReactLoopEngine(agent_config=_agent_config(context_max_rounds=1))
        state = {"user_context": {"interaction_response": {"choice": "A"}}}
        engine._create_agent(
            max_iterations=10, system_prompt="p", caller="main", state=state
        )
        assert captured["context_max_rounds"] == 2

    def test_resume_request_keeps_configured_when_above_two(self, monkeypatch):
        captured = self._setup_factory_spy(monkeypatch)
        engine = JiuwenReactLoopEngine(agent_config=_agent_config(context_max_rounds=7))
        state = {"user_context": {"interaction_response": {"choice": "A"}}}
        engine._create_agent(
            max_iterations=10, system_prompt="p", caller="main", state=state
        )
        assert captured["context_max_rounds"] == 7


class TestFactoryAgentCapabilityValidation:
    """Verify run validates factory capability exactly once before creation."""

    def test_factory_without_history_passes(self, monkeypatch):
        created = SimpleNamespace()
        engine = JiuwenReactLoopEngine(
            agent_config=_agent_config(),
            agent_factory=lambda: created,
        )
        state = {"messages": [{"role": "user", "content": "hello"}]}
        result = engine._create_agent(
            max_iterations=10, system_prompt="p", caller="main", state=state
        )
        assert result is created

    def test_run_rejects_history_before_create_agent(self, monkeypatch):
        monkeypatch.setattr(
            "agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True
        )
        engine = JiuwenReactLoopEngine(
            agent_config=_agent_config(),
            agent_factory=lambda: SimpleNamespace(),
        )
        create_calls = []

        def fail_if_created(**kwargs):
            create_calls.append(kwargs)
            raise AssertionError("_create_agent must not be called")

        monkeypatch.setattr(engine, "_create_agent", fail_if_created)
        state = {
            "run_id": "run-stage-b-factory-history",
            "session_id": "session-stage-b-factory-history",
            "current_turn_id": 2,
            "user_request": "q2",
            "messages": [
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "q2"},
            ],
            "results": {},
        }

        with pytest.raises(ValueError, match="conversation history"):
            asyncio.run(
                engine.arun(
                    state,
                    caller="subagent",
                    system_prompt="stage-b-test",
                )
            )

        assert create_calls == []

    def test_run_rejects_interaction_response_before_create_agent(self, monkeypatch):
        monkeypatch.setattr(
            "agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True
        )
        engine = JiuwenReactLoopEngine(
            agent_config=_agent_config(),
            agent_factory=lambda: SimpleNamespace(),
        )
        create_calls = []

        def fail_if_created(**kwargs):
            create_calls.append(kwargs)
            raise AssertionError("_create_agent must not be called")

        monkeypatch.setattr(engine, "_create_agent", fail_if_created)
        state = {
            "run_id": "run-stage-b-factory-resume",
            "session_id": "session-stage-b-factory-resume",
            "current_turn_id": 2,
            "user_request": "A",
            "user_context": {"interaction_response": {"choice": "A"}},
            "messages": [{"role": "user", "content": "A"}],
            "results": {},
        }

        with pytest.raises(ValueError, match="interaction_response"):
            asyncio.run(
                engine.arun(
                    state,
                    caller="subagent",
                    system_prompt="stage-b-test",
                )
            )

        assert create_calls == []


def test_history_conversion_uses_precomputed_source(monkeypatch):
    from agent.core.jiuwen_engine import _build_jiuwen_history_messages

    monkeypatch.setattr(
        "agent.core.jiuwen_engine._build_jiuwen_history_source_messages",
        lambda _state: (_ for _ in ()).throw(
            AssertionError("history source must not be recomputed")
        ),
    )
    converted = _build_jiuwen_history_messages(
        {"messages": [{"role": "user", "content": "different"}]},
        _agent_config(),
        history_source_messages=[{"role": "user", "content": "q1"}],
    )

    assert len(converted) == 1
    assert getattr(converted[0], "content", None) == "q1"
