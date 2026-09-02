from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from modeling_agent.rails import ActivityRail, _confirmation_requires_schema_mapping


def test_rule_only_confirmation_does_not_require_schema_mapping() -> None:
    assert _confirmation_requires_schema_mapping([{
        "impact": ["rules/data-integrity-prompt.md"],
    }]) is False
    assert _confirmation_requires_schema_mapping([{
        "impact": ["mappings/object-data-mappings.json"],
    }]) is True
    assert _confirmation_requires_schema_mapping([{
        "impact": ["objects/tank.md"],
    }]) is True


@pytest.mark.asyncio
async def test_material_impact_result_focuses_server_view_without_model_payload() -> None:
    events: list[tuple[str, dict]] = []
    rail = ActivityRail(lambda kind, payload: events.append((kind, payload)))
    ctx = SimpleNamespace(
        extra={},
        inputs=SimpleNamespace(
            tool_name="analyze_material_impact",
            tool_args={},
            tool_call=SimpleNamespace(id="call-impact"),
            tool_result=json.dumps({
                "source": "increment-acceptance-material.md",
                "view": {"scopeResolved": True, "todo": {"n": 1, "items": []}},
                "display": {"text": "影响分析完成"},
            }, ensure_ascii=False),
        ),
    )

    await rail.after_tool_call(ctx)

    focus = [payload for kind, payload in events if kind == "focus"]
    assert focus == [{"target": {
        "kind": "impact",
        "target": "increment-acceptance-material.md",
        "data": {"scopeResolved": True, "todo": {"n": 1, "items": []}},
    }}]
