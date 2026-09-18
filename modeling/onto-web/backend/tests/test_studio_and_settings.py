"""Studio 解析、MCP 配置校验与错误脱敏的单元测试。"""

from pathlib import Path

import pytest

from app.services import studio
from app.services.agent_sessions import _looks_like_raw_error
from app.services.mcp_settings import McpConfigError, validate


@pytest.fixture()
def ontology_root(tmp_path: Path) -> Path:
    obj_dir = tmp_path / "ontology" / "object"
    logic_dir = tmp_path / "ontology" / "logic"
    obj_dir.mkdir(parents=True)
    logic_dir.mkdir(parents=True)
    (obj_dir / "订单.md").write_text(
        "# 订单\n\n## 属性\n\n编号、金额\n\n## 与logic的关联\n\n使用logic：[[创建订单]]\n",
        encoding="utf-8",
    )
    (logic_dir / "创建订单.md").write_text(
        "# 创建订单\n\n## 逻辑描述\n\n生成[[订单]]并调用[[不存在的对象]]\n",
        encoding="utf-8",
    )
    return tmp_path


def test_studio_graph_links_and_degrees(ontology_root: Path):
    result = studio.graph(ontology_root)
    names = {n["id"]: n for n in result["nodes"]}
    assert set(names) == {"订单", "创建订单"}
    assert names["订单"]["kind"] == "object"
    # 指向不存在文档的链接不产生边
    assert {(e["source"], e["target"]) for e in result["edges"]} == {
        ("订单", "创建订单"),
        ("创建订单", "订单"),
    }
    assert names["订单"]["out"] == 1 and names["订单"]["in"] == 1


def test_studio_element_sections_and_backlinks(ontology_root: Path):
    element = studio.element(ontology_root, "ontology/object/订单.md")
    assert [s["title"] for s in element["sections"]] == ["属性", "与logic的关联"]
    assert element["links"] == ["创建订单"]
    assert [b["name"] for b in element["backlinks"]] == ["创建订单"]


def test_mcp_validate():
    ok = validate({"name": "srv-1", "transport": "http", "url": "https://mcp.example.com"})
    assert ok["enabled"] is True
    with pytest.raises(McpConfigError):
        validate({"name": "bad name", "transport": "http", "url": "https://x.example.com"})
    with pytest.raises(McpConfigError):
        validate({"name": "s", "transport": "http", "url": "ftp://x"})
    with pytest.raises(McpConfigError):
        validate({"name": "s", "transport": "stdio", "command": ""})


def test_raw_error_detection():
    assert _looks_like_raw_error("API Error: 400 thinking block must contain thinking")
    assert _looks_like_raw_error("Claude Code returned an error result")
    assert _looks_like_raw_error("(request id: 2026...)")
    assert not _looks_like_raw_error("订单对象提取完成，共 12 个字段。")
