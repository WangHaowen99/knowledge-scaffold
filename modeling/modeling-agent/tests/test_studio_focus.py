from __future__ import annotations

import json

import pytest

from modeling_agent.tools import FocusTool, ToolContext


pytestmark = pytest.mark.asyncio


async def test_studio_focus_emits_view_and_stable_element_id() -> None:
    events: list[tuple[str, dict]] = []
    context = ToolContext(
        project_id="project-studio",
        run_id="run-studio",
        token="token",
        gateway=None,  # type: ignore[arg-type]
        emit=lambda kind, payload: events.append((kind, payload)),
    )
    tool = FocusTool(context)

    result = json.loads(await tool.invoke({
        "kind": "studio",
        "view": "entry",
        "elementId": "rule:safety-stock",
    }))

    assert result["ok"] is True
    assert events == [("focus", {"target": {
        "kind": "studio",
        "target": "知识包 Studio",
        "view": "entry",
        "elementId": "rule:safety-stock",
    }})]


async def test_non_studio_focus_still_requires_target() -> None:
    events: list[tuple[str, dict]] = []
    context = ToolContext(
        project_id="project-studio",
        run_id="run-studio",
        token="token",
        gateway=None,  # type: ignore[arg-type]
        emit=lambda kind, payload: events.append((kind, payload)),
    )

    result = json.loads(await FocusTool(context).invoke({"kind": "file"}))

    assert result["ok"] is False
    assert result["code"] == "INVALID_FOCUS_TARGET"
    assert events == []
