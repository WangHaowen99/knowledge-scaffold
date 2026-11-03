"""F 第一批（V 修复审查第 3 条）：台账出处写法带反引号、「」、粗体，或者紧贴汉字时识别错。

自主构建台账的出处（decide.anchors）和对话、Studio、资料地图用同一套资料路径抽取规则（material_text.find_anchors）。
"""
from __future__ import annotations

import pytest

from app.services import material_text
from app.services.autobuild import decide

EXPECTED = [{"n": 1, "anchor": "file:resource/others/合成制度.docx#p:6", "label": "合成制度.docx §6"}]


@pytest.mark.parametrize("text", [
    "依据：`resource/others/合成制度.docx#p:6`",
    "依据「resource/others/合成制度.docx#p:6」",
    "依据 **resource/others/合成制度.docx#p:6**",
    "依据：resource/others/合成制度.docx#p:6？",
    "依据：resource/others/合成制度.docx#p:6，另见",
    "出处见resource/others/合成制度.docx#p:6",
    "见【file:resource/others/合成制度.docx#p:6】",
])
def test_ledger_anchor_ignores_surrounding_markup(text):
    assert decide.anchors(text) == EXPECTED


def test_material_anchor_rule_stops_at_closing_marks():
    found = material_text.find_anchors("依据「resource/a.md#p:2」与 **resource/b.md**、`resource/c.xlsx#sheet:表!B3`")
    assert [(path, fragment) for path, fragment, _ in found] == [
        ("resource/a.md", "p:2"), ("resource/b.md", ""), ("resource/c.xlsx", "sheet:表!B3")]


def test_ledger_anchor_keeps_other_schemes_and_skips_their_resource_part():
    found = decide.anchors("见 snap:resource/a.md#L3，另见 resource/b.md#p:1 与 ev:e-12")
    assert [item["anchor"] for item in found] == ["snap:resource/a.md#L3", "file:resource/b.md#p:1", "ev:e-12"]
