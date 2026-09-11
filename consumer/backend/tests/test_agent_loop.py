"""Contract tests for the stable Jiuwen-only engine facade."""

import pytest
from agent.core.engine import AgentLoopEngine
from agent.core.jiuwen.common import JiuwenBackendUnavailable
from agent.core.jiuwen_engine import JiuwenReactLoopEngine
from agent.core.shared.modes import VALID_AGENT_MODES


def test_agent_loop_engine_is_jiuwen_only_facade():
    assert issubclass(AgentLoopEngine, JiuwenReactLoopEngine)
    assert AgentLoopEngine.__init__ is JiuwenReactLoopEngine.__init__
    assert AgentLoopEngine.arun is JiuwenReactLoopEngine.arun
    assert AgentLoopEngine._VALID_AGENT_MODES is VALID_AGENT_MODES


@pytest.mark.parametrize(
    ("user_context", "expected"),
    [
        ({"agent_mode": "quick"}, "quick"),
        ({"agent_mode": " THINKING "}, "thinking"),
        ({"agent_mode": "multi_agent"}, "multi_agent"),
        ({"agent_mode": "unsupported"}, "quick"),
        (None, "quick"),
    ],
)
def test_agent_loop_engine_delegates_mode_resolution(user_context, expected):
    assert AgentLoopEngine._resolve_agent_mode(user_context) == expected


def test_agent_loop_engine_propagates_jiuwen_backend_unavailable(monkeypatch):
    def fail_dependency_check():
        raise JiuwenBackendUnavailable("openJiuwen is unavailable")

    monkeypatch.setattr(
        "agent.core.jiuwen_engine.require_openjiuwen",
        fail_dependency_check,
    )

    with pytest.raises(JiuwenBackendUnavailable, match="openJiuwen is unavailable"):
        AgentLoopEngine(agent_config=object())
