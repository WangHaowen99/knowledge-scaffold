"""声明式求解评估器契约：工具箱 solver_evaluator_contract.py 的规则、CLI 与模板。

后端求解契约检查（test_solver_contract_check.py）与 /ontology-solver-evaluator 的自检共用这份实现；
CLI 走真实 subprocess，与命令里的调用方式一致。fixture 全部为合成数据。
"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from app import config
from app.services import package_mapping, solver_contract
from app.services.workspace import _script_header_meta
from tests.test_script_contract_checker import run_checker

TOOLKIT = Path(config.ASSETS_DIR) / "workspace-toolkit"
CLI = TOOLKIT / "scripts" / "solver_evaluator_contract.py"
PYTHON3 = shutil.which("python3")
TEMPLATE = TOOLKIT / "templates" / "solver-evaluator-template.py"
contract = solver_contract._evaluator_contract

OMIT = object()  # declarative_source / with_fields 的覆盖值：省略该项

FACTORS = {
    "factor-a_load_a": 1.0,
    "factor-a_product_b": 0.98,
    "factor-b_load_a": "1.02",
    "factor-b_product_b": 1,
    "vgo_load_a": 1.01,
    "vgo_product_b": 0.99,
}
CONTEXT = {
    "option_routes": {"OPTION_0": "route-a", "OPTION_1": "route-b"},
    "route_stream_products": ["stream-e"],
    "atmospheric_product_tanks": {"stream-d": "tank-c"},
    "special_tanks": {"diesel": "tank-a", "vgo": "tank-b"},
    "unit_capacity_keys": {
        "atmospheric": "cap-a",
        "diesel_by_unit": {"UNIT_1": "cap-b", "UNIT_2": "cap-b"},
        "vgo": "cap-c",
    },
    "correction_factor_prefix_by_unit": {"UNIT_1": "factor-a", "UNIT_2": "factor-b"},
    "production_condition": "NO_JET",
    "yield_unit": "percent",
}
TABLE_SOURCES = {
    "crude_oil_yard": {
        "object": "SourceLot", "fields": {"OilYardID": "source_id", "CrudeOilYard Name": "source_name"},
    },
    "refinery_unit": {
        "object": "ProcessUnit", "fields": {"RefineryUnitID": "unit_id", "price_per_unit": "unit_cost"},
    },
    "vgo_ht_unit": {
        "object": "UpgradeUnit", "fields": {"VGOUnitID": "upgrade_unit_id", "price_per_unit": "unit_cost"},
    },
    "hydrogenation_product_yield": {
        "object": "ProcessYield",
        "fields": {
            "OilYardID": "source_id", "RefineryUnitID": "unit_id", "ProductName": "product",
            "LogicTankID": "tank_id", "price_per_unit": "price", "yield_atmos": "rate_primary",
            "yield_diesel_with_vgo": "rate_with", "yield_diesel_without_vgo": "rate_without",
        },
    },
    "vgo_product_yield": {
        "object": "UpgradeYield",
        "fields": {
            "OilYardID": "source_id", "VGOUnitID": "upgrade_unit_id", "ProductName": "product",
            "LogicTankID": "tank_id", "price_per_unit": "price",
            "yield_vgo_with_vgo": "rate_with", "yield_vgo_without_vgo": "rate_without",
        },
    },
    "logic_tank": {
        "object": "StorageTank",
        "fields": {
            "LogicTankID": "tank_id", "AvailableCapacity_Tons": "available",
            "CurrentCapacity_Tons": "current", "LowSafetyThrd_Tons": "low", "SafetyStockThrd_Tons": "high",
        },
    },
    "energy_consume": {
        "object": "UtilityUse",
        "fields": {
            "RefineryUnitID": "unit_id", "VGOUnitID": "upgrade_unit_id",
            "UnitConsume_Value": "consume", "price_per_unit": "price",
        },
    },
}
DECLARATIONS = {
    "ATMOS_UNIT_ID": "unit-a",
    "DIESEL_UNIT_IDS": {"UNIT_1": "unit-b", "UNIT_2": "unit-c"},
    "DEFAULT_DIESEL_UNIT_ID": "unit-b",
    "VGO_UNIT_ID": "unit-d",
    "YIELD_ATMOS": "yield_atmos",
    "YIELD_DIESEL_WITH_VGO": "yield_diesel_with_vgo",
    "YIELD_DIESEL_WITHOUT_VGO": "yield_diesel_without_vgo",
    "YIELD_VGO_WITH_VGO": "yield_vgo_with_vgo",
    "YIELD_VGO_WITHOUT_VGO": "yield_vgo_without_vgo",
    "ATMOS_DIESEL_FEED": ["stream-a"],
    "ATMOS_HC_FEED": ["stream-b"],
    "VGO_INTERMEDIATE_NAMES": ["stream-c"],
    "RULE_DAILY_CAPACITIES": {"cap-a": 100, "cap-b": 50, "cap-c": "40.5"},
    "CORRECTION_FACTORS": {"source-a": FACTORS, "source-b": FACTORS},
    "SOLVER_CONTEXT": CONTEXT,
    "TABLE_SOURCES": TABLE_SOURCES,
}
TEXT, NUMBER = "字符串", "数值"
OBJECTS = {
    "SourceLot": {"source_id": TEXT, "source_name": TEXT},
    "ProcessUnit": {"unit_id": TEXT, "unit_cost": NUMBER},
    "UpgradeUnit": {"upgrade_unit_id": TEXT, "unit_cost": NUMBER},
    "ProcessYield": {
        "source_id": TEXT, "unit_id": TEXT, "product": TEXT, "tank_id": TEXT,
        "price": NUMBER, "rate_primary": NUMBER, "rate_with": NUMBER, "rate_without": NUMBER,
    },
    "UpgradeYield": {
        "source_id": TEXT, "upgrade_unit_id": TEXT, "product": TEXT, "tank_id": TEXT,
        "price": NUMBER, "rate_with": NUMBER, "rate_without": NUMBER,
    },
    "StorageTank": {"tank_id": TEXT, "available": NUMBER, "current": NUMBER, "low": NUMBER, "high": NUMBER},
    "UtilityUse": {"unit_id": TEXT, "upgrade_unit_id": TEXT, "consume": NUMBER, "price": NUMBER},
}


def declarative_source(**overrides: Any) -> str:
    """合成的声明式评估器源码：在 DECLARATIONS 上覆盖或追加名字。"""
    values = {**DECLARATIONS, **overrides}
    body = "".join(f"{name} = {value!r}\n" for name, value in values.items() if value is not OMIT)
    return f'"""路线评估器（合成示例：声明式）"""\n\nfrom __future__ import annotations\n\n{body}'


def with_fields(table: str, changes: dict[str, Any]) -> dict:
    """TABLE_SOURCES 的副本：改写一张表的逻辑列映射。"""
    fields = {**TABLE_SOURCES[table]["fields"], **changes}
    fields = {column: key for column, key in fields.items() if key is not OMIT}
    return {**TABLE_SOURCES, table: {**TABLE_SOURCES[table], "fields": fields}}


def object_doc(name: str, fields: dict[str, str]) -> str:
    rows = "".join(f"| {key} | {key} | {type_name} |\n" for key, type_name in fields.items())
    return f"# {name}\n\n# 字段\n\n| 英文名称 | 中文名称 | 类型 |\n| --- | --- | --- |\n{rows}"


def object_docs(**overrides: dict[str, str]) -> dict[str, str]:
    """对象 key → 对象文档；覆盖值为该对象的 {属性: 类型}。"""
    return {name: object_doc(name, fields) for name, fields in {**OBJECTS, **overrides}.items()}


def object_fields(**overrides: dict[str, str]) -> dict[str, list[dict]]:
    return {
        name: [{"key": key, "type": type_name} for key, type_name in fields.items()]
        for name, fields in {**OBJECTS, **overrides}.items()
    }


def problems(source: str, objects: dict | None = None) -> list:
    return contract.check_declarative(source.encode("utf-8"), object_fields() if objects is None else objects)


def messages(source: str) -> list[str]:
    return [problem.message for problem in problems(source) if problem.severity == contract.SEVERITY_ERROR]


# ---- 判定与读取 ----


def test_valid_declarative_evaluator_has_no_problems():
    source = declarative_source().encode("utf-8")
    assert contract.evaluator_kind(source) == contract.DECLARATIVE_KIND
    assert contract.check_declarative(source, object_fields()) == []
    assert contract.check_declarative(source) == []  # 不给对象时只查结构
    assert contract.declared_objects(source) == sorted(OBJECTS)


LEGACY = '''"""旧式评估器（合成示例）"""
TABLE_SOURCES = {}


class BatchPlan:
    pass


def build_report(tables, plans, plan_month):
    return {}


def load_tables():
    return {}


def normalize_crude_name(name):
    return name
'''


@pytest.mark.parametrize(
    "source, kind",
    [
        (LEGACY, contract.LEGACY_KIND),  # 两类特征同时具备时按旧式处理，与 Consumer 一致
        ('"""普通脚本"""\nVALUE = 1\n', None),
        ("", None),
        ("def broken(:\n", None),
    ],
)
def test_evaluator_kind(source: str, kind):
    assert contract.evaluator_kind(source.encode("utf-8")) == kind


def test_oversized_or_non_utf8_file_is_not_an_evaluator():
    oversized = declarative_source() + "#" * contract.MAX_EVALUATOR_BYTES
    assert contract.evaluator_kind(oversized.encode("utf-8")) is None
    assert contract.evaluator_kind(declarative_source().encode("utf-16")) is None
    assert [p.message for p in contract.check_declarative(b"")] == [contract.UNPARSEABLE_MESSAGE]


# ---- 结构规则：一次报全，逐条可定位 ----


def _context(**changes: Any) -> dict:
    return {key: value for key, value in {**CONTEXT, **changes}.items() if value is not OMIT}


@pytest.mark.parametrize(
    "source, fragment",
    [
        (declarative_source() + "import os\n", "不是受支持的声明"),
        (declarative_source() + "\n\ndef helper():\n    return 1\n", "不是受支持的声明"),
        (declarative_source().replace("VGO_UNIT_ID = ", "VGO_UNIT_ID: str = "), "不带类型标注"),
        (declarative_source(EXTRA_LIMIT=1), "未约定的名字 EXTRA_LIMIT"),
        (declarative_source() + "VGO_UNIT_ID = 'unit-e'\n", "VGO_UNIT_ID 被重复赋值"),
        (declarative_source().replace("VGO_UNIT_ID = 'unit-d'", "VGO_UNIT_ID = ATMOS_UNIT_ID"), "VGO_UNIT_ID 须为字面量"),
        (
            declarative_source().replace("RULE_DAILY_CAPACITIES = {", "RULE_DAILY_CAPACITIES = {'cap-a': 1, "),
            "RULE_DAILY_CAPACITIES 含重复的键",
        ),
        (declarative_source(VGO_UNIT_ID=OMIT), "声明式评估器缺少：VGO_UNIT_ID"),
        (declarative_source(VGO_UNIT_ID="unit-d "), "首尾不能有空白"),
        (declarative_source(VGO_UNIT_ID="u" * 129), "不能超过 128 个字符"),
        (declarative_source(ATMOS_HC_FEED="stream-b"), "ATMOS_HC_FEED 须为字符串列表"),
        (declarative_source(RULE_DAILY_CAPACITIES={"cap-a": 0, "cap-b": 50, "cap-c": 40}), "['cap-a'] 须为正数"),
        (declarative_source(RULE_DAILY_CAPACITIES={"cap-a": True, "cap-b": 50, "cap-c": 40}), "['cap-a'] 须为正数"),
        (
            declarative_source(SOLVER_CONTEXT=_context(option_routes={"OPTION_0": "route-a", "OPTION_1": "route-a"})),
            "option_routes 须恰好包含 OPTION_0、OPTION_1",
        ),
        (
            declarative_source(SOLVER_CONTEXT=_context(special_tanks={"diesel": "tank-a", "other": "tank-b"})),
            "special_tanks 须恰好包含 diesel、vgo",
        ),
        (declarative_source(SOLVER_CONTEXT=_context(route_stream_products=[])), "route_stream_products 须为非空"),
        (
            declarative_source(SOLVER_CONTEXT=_context(route_stream_products=["stream-a", "stream-a"])),
            "route_stream_products 不能有重复项",
        ),
        (declarative_source(SOLVER_CONTEXT=_context(extra_key="x")), "含不支持的键：extra_key"),
        (
            declarative_source(SOLVER_CONTEXT=_context(production_condition="condition-a")),
            "production_condition 须为 'NO_JET'",
        ),
        (declarative_source(SOLVER_CONTEXT=_context(yield_unit="ratio")), "yield_unit 须为 'percent' 或 'fraction'"),
        (declarative_source(ATMOS_HC_FEED=["stream-a"]), "ATMOS_DIESEL_FEED 与 ATMOS_HC_FEED 都列出了 'stream-a'"),
        (
            declarative_source(ATMOS_HC_FEED=["stream-b", "stream-e"]),
            "ATMOS_HC_FEED 与 SOLVER_CONTEXT.route_stream_products 都列出了 'stream-e'",
        ),
        (
            declarative_source(SOLVER_CONTEXT=_context(unit_capacity_keys={
                **CONTEXT["unit_capacity_keys"], "vgo": "cap-x",
            })),
            "能力项 'cap-x' 不在 RULE_DAILY_CAPACITIES 中",
        ),
        (
            declarative_source(DIESEL_UNIT_IDS={**DECLARATIONS["DIESEL_UNIT_IDS"], "UNIT_3": "unit-e"}),
            "DIESEL_UNIT_IDS 的键 'UNIT_3' 在 SOLVER_CONTEXT.unit_capacity_keys.diesel_by_unit 中没有对应项",
        ),
        (
            declarative_source(CORRECTION_FACTORS={
                "source-a": {key: value for key, value in FACTORS.items() if key != "vgo_product_b"},
            }),
            "缺少 vgo_product_b",
        ),
        (
            declarative_source(CORRECTION_FACTORS={"source-a": {**FACTORS, "factor-c_load_a": 1}}),
            "多出 factor-c_load_a",
        ),
        (declarative_source(CORRECTION_FACTORS={"source-a": {**FACTORS, "vgo_load_a": -1}}), "系数须为正数"),
        (
            declarative_source(CORRECTION_FACTORS={"source-a": FACTORS, "source -a": FACTORS}),
            "归一化（NFKC 并去空白）后相同",
        ),
        (
            declarative_source(TABLE_SOURCES={k: v for k, v in TABLE_SOURCES.items() if k != "energy_consume"}),
            "缺少逻辑表：energy_consume",
        ),
        (
            declarative_source(TABLE_SOURCES={**TABLE_SOURCES, "extra_table": TABLE_SOURCES["logic_tank"]}),
            "含不支持的逻辑表：extra_table",
        ),
        (
            declarative_source(TABLE_SOURCES={**TABLE_SOURCES, "logic_tank": {"object": "StorageTank"}}),
            "TABLE_SOURCES.logic_tank 须写成",
        ),
        (declarative_source(TABLE_SOURCES=with_fields("logic_tank", {"Extra": "x"})), "含不支持的逻辑列 'Extra'"),
        (
            declarative_source(TABLE_SOURCES=with_fields("crude_oil_yard", {"CrudeOilYard Name": OMIT})),
            "缺少必填逻辑列：CrudeOilYard Name",
        ),
        (
            declarative_source(TABLE_SOURCES=with_fields("vgo_product_yield", {"VGOUnitID": OMIT})),
            "缺少必填逻辑列：VGOUnitID",
        ),
        (
            declarative_source(
                TABLE_SOURCES=with_fields("energy_consume", {"RefineryUnitID": OMIT, "VGOUnitID": OMIT}),
            ),
            "须至少映射 RefineryUnitID、VGOUnitID 之一",
        ),
        (
            declarative_source(TABLE_SOURCES=with_fields("logic_tank", {"CurrentCapacity_Tons": "available"})),
            "把同一属性映射了多次：available",
        ),
        (declarative_source(YIELD_ATMOS="rate_a"), "缺少必填逻辑列：rate_a"),  # 改了收率列名却没改映射
        (declarative_source(YIELD_ATMOS="ProductName"), "固定列重名"),
        (
            declarative_source(TABLE_SOURCES={
                **TABLE_SOURCES, "logic_tank": {**TABLE_SOURCES["logic_tank"], "object": "Storage`Tank"},
            }),
            "不能含反引号",
        ),
    ],
)
def test_structural_rule_violations_are_errors(source: str, fragment: str):
    found = messages(source)
    assert any(fragment in message for message in found), found


def test_missing_solver_context_is_context_kind():
    [problem] = problems(declarative_source(SOLVER_CONTEXT=OMIT))
    assert problem.kind == contract.KIND_CONTEXT and problem.field == "SOLVER_CONTEXT"
    for key in ("production_condition", "yield_unit"):  # yield_unit 没有默认值：缺失时不能按百分数猜
        [problem] = problems(declarative_source(SOLVER_CONTEXT=_context(**{key: OMIT})))
        assert problem.kind == contract.KIND_CONTEXT and problem.message.endswith(key)


def test_placeholders_are_reported_once_per_name():
    source = declarative_source(
        VGO_UNIT_ID="<待填写：二次加工单元 ID>",
        SOLVER_CONTEXT=_context(production_condition="<待填写：生产工况>"),
    )
    found = problems(source)
    assert [p.field for p in found] == ["VGO_UNIT_ID", "SOLVER_CONTEXT"]
    assert all("未填写的模板占位符" in p.message for p in found)


# ---- 本体引用：对象 key 与属性英文名 ----


def test_unknown_object_and_attribute_are_reference_missing():
    source = declarative_source(TABLE_SOURCES={
        **with_fields("logic_tank", {"LowSafetyThrd_Tons": "missing_attr"}),
        "energy_consume": {**TABLE_SOURCES["energy_consume"], "object": "MissingObject"},
    })
    found = problems(source)
    assert {p.kind for p in found} == {contract.KIND_REFERENCE}
    assert sorted(p.field for p in found) == [
        "TABLE_SOURCES.energy_consume.object",
        "TABLE_SOURCES.logic_tank.fields['LowSafetyThrd_Tons']",
    ]
    assert problems(source, objects={}) and contract.check_declarative(source.encode("utf-8")) == []


def test_numeric_id_attribute_is_an_error():
    objects = object_fields(StorageTank={**OBJECTS["StorageTank"], "tank_id": "整数"})
    [problem] = problems(declarative_source(), objects)
    assert problem.severity == contract.SEVERITY_ERROR
    assert problem.field == "TABLE_SOURCES.logic_tank.fields['LogicTankID']" and "前导零" in problem.message


# ---- lookup：本对象没有的列，经关联对象中唯一匹配的记录取得 ----

LOOKUP = {"lookup": "StreamLink", "match": {"product": "stream"}, "field": "source_unit"}
LOOKUP_OBJECT = {"stream": TEXT, "source_unit": TEXT, "tank": TEXT}
LOOKUP_FIELD = "TABLE_SOURCES.hydrogenation_product_yield.fields['RefineryUnitID']"


def lookup_source(**changes: Any) -> str:
    """RefineryUnitID 改为经 StreamLink 关联取得；changes 改写 LOOKUP 的键，OMIT 表示省略。"""
    lookup = {key: value for key, value in {**LOOKUP, **changes}.items() if value is not OMIT}
    return declarative_source(TABLE_SOURCES=with_fields("hydrogenation_product_yield", {"RefineryUnitID": lookup}))


def lookup_problems(source: str, **link_types: str) -> list:
    """以 StreamLink（属性类型按 link_types 覆盖）加入本体后的检查结果。"""
    return problems(source, object_fields(StreamLink={**LOOKUP_OBJECT, **link_types}))


def test_lookup_column_passes_and_its_object_is_declared():
    source = lookup_source()
    assert lookup_problems(source) == []
    assert contract.check_declarative(source.encode("utf-8")) == []
    assert contract.declared_objects(source.encode("utf-8")) == sorted([*OBJECTS, "StreamLink"])
    # 同一关联对象可以按同样的匹配提供多个列
    several = declarative_source(TABLE_SOURCES=with_fields("hydrogenation_product_yield", {
        "RefineryUnitID": LOOKUP, "LogicTankID": {**LOOKUP, "field": "tank"},
    }))
    assert lookup_problems(several) == []


@pytest.mark.parametrize(
    "source, fragment",
    [
        (lookup_source(field=OMIT), "的关联须写成"),
        (lookup_source(default="unit-a"), "的关联须写成"),
        (lookup_source(match={}), "match 须为非空字典"),
        (lookup_source(match=[("product", "stream")]), "match 须为非空字典"),
        (lookup_source(match={"product": "str`eam"}), "不能含反引号"),
        (lookup_source(match={" product": "stream"}), "首尾不能有空白"),
        (lookup_source(lookup=7), "须为非空字符串"),
        (lookup_source(field=LOOKUP), "须为非空字符串"),  # 不支持多跳
        (
            declarative_source(TABLE_SOURCES=with_fields("hydrogenation_product_yield", {
                "RefineryUnitID": LOOKUP, "LogicTankID": LOOKUP,
            })),
            "把同一属性映射了多次：StreamLink.source_unit",
        ),
    ],
)
def test_lookup_structure_violations_are_errors(source: str, fragment: str):
    found = [p.message for p in lookup_problems(source) if p.severity == contract.SEVERITY_ERROR]
    assert any(fragment in message for message in found), found


def test_lookup_object_and_attributes_are_reference_missing():
    [problem] = lookup_problems(lookup_source(lookup="MissingObject"))
    assert problem.kind == contract.KIND_REFERENCE and problem.field == f"{LOOKUP_FIELD}.lookup"

    found = lookup_problems(lookup_source(match={"missing_own": "missing_related"}, field="missing_field"))
    assert {p.kind for p in found} == {contract.KIND_REFERENCE}
    assert [p.field for p in found] == [f"{LOOKUP_FIELD}.match", f"{LOOKUP_FIELD}.match", f"{LOOKUP_FIELD}.field"]
    assert "'missing_own' 不在对象 ProcessYield 中" in found[0].message
    assert "'missing_related' 不在对象 StreamLink 中" in found[1].message
    assert "'missing_field' 不在对象 StreamLink 中" in found[2].message


@pytest.mark.parametrize(
    "link_types, field, fragment",
    [
        ({"stream": NUMBER}, f"{LOOKUP_FIELD}.match", "不做类型转换"),
        ({"source_unit": "整数"}, f"{LOOKUP_FIELD}.field", "前导零"),
    ],
)
def test_lookup_type_mismatches_are_errors(link_types: dict[str, str], field: str, fragment: str):
    [problem] = lookup_problems(lookup_source(), **link_types)
    assert problem.severity == contract.SEVERITY_ERROR
    assert problem.field == field and fragment in problem.message


def test_object_field_parser_matches_package_mapping():
    doc = (
        "# 对象\n\n# 对象属性清单\n\n"
        "| 中文名称 | 字段 key | 类型 | 描述 |\n| --- | --- | --- | --- |\n"
        "| 编号 | item_id | 字符串 | 主键 |\n"
        "| 数量 | amount |  | 未写类型 |\n"
        "| 占位 | {待填写：英文名称} | 字符串 | 模板残留 |\n"
        "| 空 | 无 | 字符串 | 不声明 |\n"
        "| 容量 | Capacity_Tons | 浮点数 | 数值 |\n"
        "\n# 关联\n\n| 英文名称 | 类型 |\n| --- | --- |\n| other | 字符串 |\n"
    )
    expected = [{"key": f["key"], "type": f["type"]} for f in package_mapping.parse_object_doc(doc)["fields"]]
    assert contract.parse_object_fields(doc) == expected
    assert [f["key"] for f in expected] == ["item_id", "amount", "Capacity_Tons"]
    assert expected[1]["type"] == "字符串"


def test_table_columns_match_consumer():
    """与 Consumer integration/solver_declarative_evaluator.py 的 TABLE_COLUMNS 逐项一致。"""
    assert {table: tuple(columns) for table, columns in contract.TABLE_COLUMNS.items()} == {
        "crude_oil_yard": (("OilYardID", "CrudeOilYard Name"), (), (), ()),
        "refinery_unit": (("RefineryUnitID",), ("price_per_unit",), (), ()),
        "vgo_ht_unit": (("VGOUnitID",), ("price_per_unit",), (), ()),
        "hydrogenation_product_yield": (
            ("OilYardID", "RefineryUnitID", "ProductName"),
            ("LogicTankID", "price_per_unit"),
            (),
            ("YIELD_ATMOS", "YIELD_DIESEL_WITH_VGO", "YIELD_DIESEL_WITHOUT_VGO"),
        ),
        "vgo_product_yield": (
            ("OilYardID", "VGOUnitID", "ProductName"),
            ("LogicTankID", "price_per_unit"),
            (),
            ("YIELD_VGO_WITH_VGO", "YIELD_VGO_WITHOUT_VGO"),
        ),
        "logic_tank": (
            ("LogicTankID",),
            ("CurrentCapacity_Tons", "LowSafetyThrd_Tons"),
            ("AvailableCapacity_Tons", "SafetyStockThrd_Tons"),
            (),
        ),
        "energy_consume": (("UnitConsume_Value",), ("price_per_unit",), ("RefineryUnitID", "VGOUnitID"), ()),
    }


# ---- 模板：可直接复制使用，只差占位符 ----


def test_template_only_reports_unfilled_placeholders():
    source = TEMPLATE.read_bytes()
    assert contract.evaluator_kind(source) == contract.DECLARATIVE_KIND
    expected = {name for name in contract.DECLARATION_NAMES if not name.startswith("YIELD_")}
    for objects in (None, {}):
        found = contract.check_declarative(source, objects)
        assert {p.field for p in found} == expected
        assert all(p.severity == contract.SEVERITY_ERROR and "未填写的模板占位符" in p.message for p in found)


def test_template_passes_script_contract_checks(tmp_path: Path):
    text = TEMPLATE.read_text(encoding="utf-8")
    assert _script_header_meta(text) == ("", [])
    ontology = tmp_path / "ontology"
    (ontology / "scripts").mkdir(parents=True)
    (ontology / "scripts" / "plan_route_evaluator.py").write_text(text, encoding="utf-8")
    assert [i for i in run_checker(ontology, "--include-scripts") if i["category"] == "脚本契约"] == []


# ---- CLI ----


def run_cli(*args: str) -> subprocess.CompletedProcess:
    # 安全审查：argv 是 shutil.which 解析出的 python3 路径加工具箱自带的 CLI 与本测试的合成参数，
    # 以序列传入、不经 shell 解析；CLI 只读取传入的评估器文件与本体目录。
    return subprocess.run(  # NOSONAR(S4721)
        [PYTHON3, str(CLI), *args], capture_output=True, text=True, shell=False, timeout=60,
    )


def write_workspace(tmp_path: Path, source: str, objects: dict[str, str] | None = None) -> tuple[Path, Path]:
    ontology = tmp_path / "ontology"
    (ontology / "scripts").mkdir(parents=True)
    (ontology / "object").mkdir()
    for name, text in (object_docs() if objects is None else objects).items():
        (ontology / "object" / f"{name}.md").write_text(text, encoding="utf-8")
    path = ontology / "scripts" / "plan_route_evaluator.py"
    path.write_text(source, encoding="utf-8")
    return ontology, path


def test_cli_passes_valid_evaluator(tmp_path: Path):
    ontology, path = write_workspace(tmp_path, declarative_source())
    result = run_cli(str(path), "--ontology-dir", str(ontology), "--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "file": str(path), "kind": "declarative", "ok": True, "errors": 0, "warnings": 0,
        "problems": [], "truncated": False,
    }
    text = run_cli(str(path), "--ontology-dir", str(ontology))
    assert text.returncode == 0 and "通过（错误 0，警告 0）" in text.stdout


def test_cli_reports_errors_and_warnings(tmp_path: Path):
    path = tmp_path / "plan_route_evaluator.py"
    path.write_text(declarative_source(VGO_UNIT_ID="<待填写：二次加工单元 ID>"), encoding="utf-8")
    missing = str(tmp_path / "missing")
    result = run_cli(str(path), "--ontology-dir", missing, "--json")
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert (report["ok"], report["errors"], report["warnings"]) == (False, 1, 1)
    assert [(p["field"], p["severity"]) for p in report["problems"]] == [
        ("VGO_UNIT_ID", contract.SEVERITY_ERROR), ("", contract.SEVERITY_WARNING),
    ]
    text = run_cli(str(path), "--ontology-dir", missing)
    assert text.returncode == 1
    assert "未通过（错误 1，警告 1）" in text.stdout and "- [错误]" in text.stdout and "- [警告]" in text.stdout


def test_cli_rejects_numeric_id_attribute(tmp_path: Path):
    docs = object_docs(StorageTank={**OBJECTS["StorageTank"], "tank_id": "整数"})
    ontology, path = write_workspace(tmp_path, declarative_source(), docs)
    report = json.loads(run_cli(str(path), "--ontology-dir", str(ontology), "--json").stdout)
    assert (report["ok"], report["errors"], report["warnings"]) == (False, 1, 0)
    assert report["problems"][0]["field"] == "TABLE_SOURCES.logic_tank.fields['LogicTankID']"


def test_cli_truncates_long_reports(tmp_path: Path):
    extra = {f"EXTRA_{index}": index for index in range(contract.MAX_REPORTED_PROBLEMS + 5)}
    ontology, path = write_workspace(tmp_path, declarative_source(**extra))
    report = json.loads(run_cli(str(path), "--ontology-dir", str(ontology), "--json").stdout)
    assert report["errors"] == contract.MAX_REPORTED_PROBLEMS + 5
    assert len(report["problems"]) == contract.MAX_REPORTED_PROBLEMS and report["truncated"] is True


def test_cli_without_object_dir_warns_but_passes(tmp_path: Path):
    path = tmp_path / "plan_route_evaluator.py"
    path.write_text(declarative_source(), encoding="utf-8")
    result = run_cli(str(path), "--ontology-dir", str(tmp_path / "missing"), "--json")
    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert report["warnings"] == 1 and "未找到对象目录" in report["problems"][0]["message"]


@pytest.mark.parametrize("source, kind, fragment", [(LEGACY, "legacy", "旧式评估器"), (None, None, "读取失败")])
def test_cli_refuses_legacy_or_unreadable_file(tmp_path: Path, source: str | None, kind, fragment: str):
    path = tmp_path / "route_eval.py"
    if source is not None:
        path.write_text(source, encoding="utf-8")
    result = run_cli(str(path), "--json")
    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert report["kind"] == kind and report["ok"] is False and fragment in report["problems"][0]["message"]
