"""V 阶段真实模型下自主构建的发现（栈 b，内测引擎）。"""
from __future__ import annotations


def test_ledger_anchor_without_scheme():
    """真实模型按对话里的出处写法写依据（`resource/资料.docx#p:6`，不带 `file:`），台账里原来一个出处都没有。"""
    from app.services.autobuild import decide

    text = "置信度 0.9 · 依据：resource/others/合成制度.docx#p:6，另见 resource/others/合成细则.md#L3。"
    assert decide.anchors(text) == [
        {"n": 1, "anchor": "file:resource/others/合成制度.docx#p:6", "label": "合成制度.docx §6"},
        {"n": 2, "anchor": "file:resource/others/合成细则.md#L3", "label": "合成细则.md §L3"},
    ]
    # 带 scheme 的照旧；同一处写了两种写法只记一次
    assert decide.anchors("file:resource/a.docx#p:1 与 resource/a.docx#p:1") == [
        {"n": 1, "anchor": "file:resource/a.docx#p:1", "label": "a.docx §1"}]
    # 不是资料区的路径、不是出处的文字不算
    assert decide.anchors("依据：research_checker status=passed；见 ontology/object/报修单.md") == []


def test_hint_says_how_to_write_anchor():
    """注入里说清出处怎么写、写在哪（header 只放置信度：稳定引擎的 header 是短标签）。"""
    from app.services.autobuild import hint

    assert "置信度 0.xx" in hint.INSTRUCTIONS and "resource/" in hint.INSTRUCTIONS
