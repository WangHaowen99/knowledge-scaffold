"""元模型单一定义（详设 4.4、方案 C3～C5）：链接语法、目录、文件名编码只有一份，各处解析结果相同。

合成的外卖与门店名称；`1#泵站` 模拟存量文件名里带 `#` 的导入对象。
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import compat
from app.kernel.metamodel import mm
from app.main import app
from app.routers import files as files_router
from app.routers import studio as studio_router
from app.services import package_mapping, studio
from app.services.onto_mcp import documents
from app.services.publish import ontology_package

SCRIPTS = Path(config.ASSETS_DIR) / "workspace-toolkit" / "scripts"

OPERATION = """# 业务描述
合成的下单流程。

# 与operation的关联
- 调用operation > 无

# 与object的关联
- 输入数据的对象 > [[订单|下单对象]]、[[门店#营业时间]]、[[1#泵站]]、[[ontology/object/订单.md]]、[[不存在的对象]]

# 输入
| 参数名称 | 描述 | 输入源 |
| --- | --- | --- |
| order_id | 订单编号 | 用户 |

# 输出
| 参数名称 | 描述 |
| --- | --- |
| ok | 是否成功 |

# 逻辑描述
1. 读取订单。
"""

EXPECTED_RESOLVED = ["订单", "门店", "1#泵站"]


def _object(name: str) -> str:
    return f"# 对象描述\n合成对象 {name}。\n\n# 关联对象\n无\n"


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "ontology" / "object").mkdir(parents=True)
    (root / "ontology" / "operation").mkdir(parents=True)
    for name in ("订单", "门店", "1#泵站"):
        (root / "ontology" / "object" / f"{name}.md").write_text(_object(name), encoding="utf-8")
    (root / "ontology" / "operation" / "下单.md").write_text(OPERATION, encoding="utf-8")
    return root


@pytest.fixture()
def checker(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    return importlib.import_module("ontology_checker")


# ---- 链接语法 ----


@pytest.mark.parametrize(
    ("raw", "name", "anchor", "alias"),
    [
        ("订单", "订单", None, None),
        ("订单|下单对象", "订单", None, "下单对象"),
        ("订单\\|下单对象", "订单", None, "下单对象"),  # 表格单元格里转义的竖线
        ("门店#营业时间", "门店", "营业时间", None),
        ("门店#营业时间|看营业时间", "门店", "营业时间", "看营业时间"),
        ("ontology/object/订单.md", "订单", None, None),
        (" 订单 ", "订单", None, None),
    ],
)
def test_parse_link_text(raw, name, anchor, alias):
    link = mm.parse_link_text(raw)
    assert (link.name, link.anchor, link.alias) == (name, anchor, alias)


def test_links_need_closing_brackets_and_keep_spans():
    text = "前 [[订单]] 中 [[门店 没闭合 [[1#泵站]]"
    links = mm.parse_links(text)
    assert [link.full_name for link in links] == ["订单", "1#泵站"]
    assert text[slice(*links[0].span)] == "[[订单]]"


def test_resolution_prefers_full_name_then_name_then_encoded():
    names = {"订单", "1#泵站", "2%23泵站"}
    assert mm.resolve("1#泵站", names) == "1#泵站"  # 存量文件名带 #
    assert mm.resolve("订单#金额", names) == "订单"  # # 是锚点
    assert mm.resolve("2#泵站", names) == "2%23泵站"  # 新文件名按编码规则落盘
    assert mm.resolve("缺失", names) is None


def test_link_names_and_unresolved(workspace):
    names = {"订单", "门店", "1#泵站"}
    assert mm.link_names(OPERATION, names) == [*EXPECTED_RESOLVED, "不存在的对象"]
    assert [link.name for link in mm.unresolved(OPERATION, names)] == ["不存在的对象"]
    assert mm.strip_links("见 [[订单|下单对象]] 与 [[门店#营业时间]]") == "见 下单对象 与 门店#营业时间"


# ---- 各处解析结果相同 ----


def _resolved_only(values) -> list[str]:
    return [value for value in values if value in EXPECTED_RESOLVED]


def test_backend_and_toolkit_resolve_links_identically(workspace, checker):
    names = {"订单", "门店", "1#泵站"}
    operation = workspace / "ontology" / "operation" / "下单.md"

    # Studio 图谱：输入对象边指向操作
    graph = studio.graph(workspace)
    graph_targets = {edge["source"] for edge in graph["edges"] if edge["target"] == "下单"}
    # Studio 条目详情：正文链接与反链
    detail = studio.element(workspace, "ontology/operation/下单.md")
    backlinks = studio.element(workspace, "ontology/object/1#泵站.md")["backlinks"]
    # MCP 文档模型
    mcp_links = documents.links(OPERATION, names)
    # 发布包 frontmatter 的 uses.objects
    published = ontology_package._used_objects(OPERATION, names)
    # 工具箱规则检查器
    all_docs = checker.load_ontology(workspace / "ontology")
    checker_links = [checker._resolve_link(link, all_docs) for link in all_docs["下单"].wiki_links]
    dangling = checker.check_operation_links(all_docs["下单"], all_docs)

    assert graph_targets == set(EXPECTED_RESOLVED)
    assert _resolved_only(detail["links"]) == sorted(EXPECTED_RESOLVED)
    assert [item["name"] for item in backlinks] == ["下单"]
    assert _resolved_only(mcp_links) == EXPECTED_RESOLVED
    assert published == sorted(EXPECTED_RESOLVED)
    assert [link for link in checker_links if link] == EXPECTED_RESOLVED
    assert [issue.message for issue in dangling] == ["Wiki 链接 [[不存在的对象]] 无对应文件"]
    assert operation.is_file()


def test_studio_check_flags_only_unresolved_links(workspace):
    result = studio.deterministic_check(workspace, "ontology/operation/下单.md", OPERATION)
    assert [item for item in result["review"] if "指向不存在的文档" in item] == ["[[不存在的对象]] 指向不存在的文档"]


def test_relation_endpoints_agree_between_backend_and_checker(checker):
    for value, expected in (("[[1#泵站]]", "1#泵站"), ("[[订单|别名]]", "订单"), ("当前对象", "当前对象")):
        assert checker._relation_node(value) == expected
        assert package_mapping._relation_node(value) == expected
    texts = {"订单": "", "1#泵站": ""}
    assert package_mapping._endpoint("1#泵站", texts) == "1#泵站"
    assert package_mapping._endpoint("订单#金额", texts) == "订单"


# ---- 目录与 key 规则 ----


def test_entity_directories_come_from_one_definition():
    expected = {
        "ontology/object": "object", "ontology/operation": "operation", "ontology/logic": "operation",
        "ontology/rule": "rule", "ontology/metric": "metric", "ontology/scripts": "script",
    }
    assert {directory: kind for kind, directory, _glob in mm.kind_dirs()} == expected
    assert {directory: kind for kind, directory, _glob in studio.KIND_DIRS} == expected
    assert set(files_router.ENTRY_DIRS) == set(expected)
    assert {kind: path for kind, (path, _suffix) in studio_router.KIND_PATHS.items()} == {
        kind: directory for directory, kind in expected.items() if directory != "ontology/logic"
    }
    assert set(documents.KINDS) == set(mm.ENTITY_KINDS)
    assert documents.KINDS["operation"] == ("ontology/operation", "ontology/logic")
    assert compat.entry_id_for_path("ontology/logic/下单.md") == "operation:下单"
    assert compat.entry_id_for_path("ontology/object/index.md") == "ontology/object/index.md"
    assert studio.schema_for("ontology/logic/下单.md") == studio.OPERATION_SCHEMA


def test_filename_encoding_is_shared_with_report_reconcile(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    report_reconcile = importlib.import_module("report_reconcile")
    for name in ("1#泵站", "甲/乙", "A&B", "index", "末尾点.", "[括号]"):
        encoded = mm.encode_filename(name)
        assert report_reconcile.safe_document_stem(name) == encoded
        assert mm.decode_filename(encoded) == name
        assert not set("#&/[]") & set(encoded)
    assert mm.key_problem("订单") is None
    assert mm.key_problem("1#泵站") == "reserved_character"
    assert mm.key_problem(" 订单") == "empty_or_padded"


def test_metamodel_endpoint(monkeypatch):
    from app.services import auth

    monkeypatch.setattr(auth, "ENABLED", False)
    body = TestClient(app).get("/api/v1/metamodel").json()
    kinds = {item["kind"]: item for item in body["entity_types"]}
    assert set(kinds) == {"object", "operation", "rule", "metric", "script"}
    assert kinds["operation"]["readonly_aliases"] == ["ontology/logic"]
    assert [section["title"] for section in kinds["object"]["sections"]][:1] == ["对象描述"]
    assert body["link_syntax"]["anchor_separator"] == "#"
    assert body["link_syntax"]["resolution"][0] == "full_name"
