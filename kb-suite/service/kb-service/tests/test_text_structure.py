from __future__ import annotations

import pytest

from kb_service import text_structure as ts

MD = "# 泵房规程\n\n总则一段。\n\n## 巡检\n\n每班记录出口压力。\n\n## 联锁\n\n出口压力低于 0.3 MPa 联锁停泵。\n"


def test_window_pages_on_line_breaks_with_anchors():
    win = ts.window(MD, "d1", offset=0, limit=20)
    assert win["content"].endswith("\n") and win["has_more"] and win["next_offset"] == len(win["content"])
    assert win["anchors"] and all(a["id"].startswith("d1:") for a in win["anchors"])
    assert win["outline"] == ["泵房规程", "  巡检", "  联锁"] and win["total_chars"] == len(MD)


def test_window_heading_returns_the_section():
    win = ts.window(MD, "d1", heading="巡检")
    assert win["content"].startswith("## 巡检") and "联锁" not in win["content"] and not win["has_more"]
    assert ts.window(MD, "d1", heading="不存在") is None


def test_window_limit_zero_is_outline_only():
    win = ts.window(MD, "d1", limit=0)
    assert win["content"] == "" and win["anchors"] == [] and win["outline"]


def test_grep_lines_and_patterns():
    regex, mode = ts.compile_pattern("出口压力")
    total, lines = ts.grep_lines(MD, regex, 1)
    assert (mode, total, lines[0]["line_number"]) == ("regex", 2, 7)
    assert ts.compile_pattern("a(")[1] == "literal"
    with pytest.raises(ts.PatternError):
        ts.compile_pattern("")
