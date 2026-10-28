from kb_service.chunking import chunk_markdown, parse_blocks, split_text
from kb_service.locators import LocatorIndex
from kb_service.parsing import SourceBlock, normalize_newlines
from kb_service.parsing.native import read_delimited

DOC = """# 设备手册

## 1 概述

油泵为压缩机供油。油泵启动前需要检查油位。

## 2 参数

| 名称 | 数值 |
|---|---|
| 压力 | 0.3MPa |
| 流量 | 12L/min |

```python
print("hello")
```

- 第一条
- 第二条
"""


def test_content_matches_ranges():
    chunks, _ = chunk_markdown(DOC, doc_id="d1", chunk_size=60, overlap=10)
    assert chunks
    for c in chunks:
        assert DOC[c.start : c.end] == c.content
        assert c.content == c.content.strip()


def test_heading_path_and_protected_blocks():
    chunks, blocks = chunk_markdown(DOC, doc_id="d1", chunk_size=60, overlap=10)
    table = [c for c in chunks if c.chunk_type == "table"]
    assert table and "| 压力 | 0.3MPa |" in table[0].content
    assert table[0].context_header == "设备手册 > 2 参数"
    fence = '```python\nprint("hello")\n```'
    assert any(fence in c.content for c in chunks), "fenced code must never be split"
    kinds = {b.kind for b in blocks}
    assert {"heading", "paragraph", "table", "code", "list"} <= kinds


def test_anchor_ids_are_stable():
    blocks = parse_blocks(DOC, "doc-1")
    anchors = [b.anchor_id for b in blocks]
    assert "doc-1:heading:0" in anchors
    assert "doc-1:table:0" in anchors
    assert len(anchors) == len(set(anchors))


def test_long_paragraph_split_with_overlap():
    text = "。".join(f"第{i}句内容比较长需要切开" for i in range(80)) + "。"
    pieces = split_text(text, 0, len(text), 100, 20)
    assert len(pieces) > 5
    assert all(e - s <= 100 for s, e in pieces)
    assert any(pieces[i + 1][0] < pieces[i][1] for i in range(len(pieces) - 1))
    assert pieces[0][0] == 0 and pieces[-1][1] == len(text)


def test_line_start():
    chunks, _ = chunk_markdown(DOC, chunk_size=60, overlap=0)
    first_table = next(c for c in chunks if c.chunk_type == "table")
    assert DOC.split("\n")[first_table.line_start - 1].startswith("| 名称")


def test_normalize_newlines_shifts_blocks():
    md = "a\r\nb\r\nccc"
    blocks = [SourceBlock(6, 9, {"type": "pdf", "page": 2})]
    out, shifted = normalize_newlines(md, blocks)
    assert out == "a\nb\nccc"
    assert out[shifted[0].start : shifted[0].end] == "ccc"


def test_locator_merge_by_page():
    blocks = [
        SourceBlock(0, 10, {"type": "pdf", "page": 1, "bbox": [0.1, 0.1, 0.5, 0.2]}),
        SourceBlock(10, 20, {"type": "pdf", "page": 1, "bbox": [0.1, 0.3, 0.6, 0.4]}),
        SourceBlock(20, 30, {"type": "pdf", "page": 2, "bbox": [0.1, 0.1, 0.5, 0.2]}),
    ]
    locs = LocatorIndex(blocks).for_range(5, 25)
    assert [l["page"] for l in locs] == [1, 2]
    assert locs[0]["bbox"] == [0.1, 0.1, 0.6, 0.4]


def test_csv_table_full_with_row_locators():
    data = "名称,数值\n" + "\n".join(f"r{i},{i}" for i in range(45))
    result = read_delimited(data.encode("utf-8"), delimiter=",")
    assert result.markdown.count("\n") == 47
    assert result.source_blocks[0].locator["row_start"] == 2
    assert result.source_blocks[-1].locator["row_end"] == 46
    for b in result.source_blocks:
        assert result.markdown[b.start : b.end].startswith("| r")
