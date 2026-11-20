"""ontology_checker 对评测/发布阶段硬约束的提前检查。

每项都与评测或发布侧的同一口径对齐：同名关系的关联字段冲突（package_mapping.graph_relations）、
自关联括注、图标识符（GRAPH_IDENTIFIER）、脚本安全（script-safety.ts）、脚本绑定 operation、
知识包 key（keys.ts）、consumer 容量上限、主键必须是真实列。
"""

import dataclasses
import importlib
from pathlib import Path

import pytest

from app import config

SCRIPTS = Path(config.ASSETS_DIR) / "workspace-toolkit" / "scripts"


@pytest.fixture
def checker(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    return importlib.import_module("ontology_checker")


OBJECT_DOC = """# 对象描述
{name}。

# 对象别名
无

# 英文名称
{name}

# 数据来源
{table}

# 对象属性清单
| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |
| --- | --- | --- | --- | --- | --- | --- |
{rows}

# 关联对象
{relations}
"""

OPERATION_DOC = """# 业务描述
示例操作。

# 与operation的关联
- 调用operation：无
- 被operation调用：无

# 与object的关联
无

# 输入
无

# 输出
无

# 逻辑描述
1. 计算结果。
"""

ORDER_ROWS = (
    "| 订单号 | order_id | 订单主键 | 是 | 字符串 | 无 | 无 |\n"
    "| 买方编号 | buyer_no | 指向买方客户 | 否 | 字符串 | 无 | 无 |\n"
    "| 卖方编号 | seller_no | 指向卖方客户 | 否 | 字符串 | 无 | 无 |"
)
CUSTOMER_ROWS = (
    "| 客户编号 | customer_no | 客户主键 | 是 | 字符串 | 无 | 无 |\n"
    "| 客户名称 | customer_name | 客户名称 | 否 | 字符串 | 无 | 无 |"
)
EMPLOYEE_ROWS = (
    "| 员工编号 | employee_id | 员工主键 | 是 | 字符串 | 无 | 无 |\n"
    "| 上级编号 | manager_id | 直属上级 | 否 | 字符串 | 无 | 无 |"
)


def write_object(ontology: Path, name: str, rows: str, relations: str = "", table: str = "无") -> None:
    ontology.joinpath("object").mkdir(parents=True, exist_ok=True)
    (ontology / "object" / f"{name}.md").write_text(
        OBJECT_DOC.format(name=name, table=table, rows=rows, relations=relations), encoding="utf-8",
    )


def write_operation(ontology: Path, name: str, body: str = OPERATION_DOC) -> None:
    ontology.joinpath("operation").mkdir(parents=True, exist_ok=True)
    (ontology / "operation" / f"{name}.md").write_text(body, encoding="utf-8")


def write_script(ontology: Path, name: str, code: str) -> None:
    ontology.joinpath("scripts").mkdir(parents=True, exist_ok=True)
    (ontology / "scripts" / f"{name}.py").write_text(code, encoding="utf-8")


def issues(checker, ontology: Path, category: str, **kwargs) -> list[dict]:
    found = [dataclasses.asdict(i) for i in checker.run_checks(ontology, **kwargs)]
    return [i for i in found if i["category"] == category]


# --- 1. 同名关系字段冲突 ---

def test_same_relation_with_different_join_pairs_conflicts(checker, tmp_path: Path):
    """同一三元组两端各写一对不同字段：后端两处都不导入，两端文件各报一条。"""
    ontology = tmp_path / "ontology"
    write_object(ontology, "订单", ORDER_ROWS, "- 当前对象 → 客户（buyer_no → customer_no） → [[客户]]")
    write_object(ontology, "客户", CUSTOMER_ROWS, "- [[订单]] → 客户（seller_no → customer_no） → 当前对象")

    found = [i for i in issues(checker, ontology, "关联字段") if "不同的关联字段" in i["message"]]
    assert sorted(i["file"] for i in found) == ["object/客户.md", "object/订单.md"]
    assert all(i["severity"] == "警告" for i in found)
    message = found[0]["message"]
    assert "buyer_no → customer_no" in message and "seller_no → customer_no" in message
    assert "订单.md 第" in message and "客户.md 第" in message
    assert "不同的关系名" in message


def test_conflict_within_one_file_is_reported(checker, tmp_path: Path):
    ontology = tmp_path / "ontology"
    write_object(ontology, "订单", ORDER_ROWS,
                 "- 当前对象 → 客户（buyer_no → customer_no） → [[客户]]\n"
                 "- 当前对象 → 客户（seller_no → customer_no） → [[客户]]")
    write_object(ontology, "客户", CUSTOMER_ROWS)

    found = [i for i in issues(checker, ontology, "关联字段") if "不同的关联字段" in i["message"]]
    assert [i["file"] for i in found] == ["object/订单.md"]


def test_mirror_declaration_with_one_side_note_is_not_conflict(checker, tmp_path: Path):
    """镜像声明只有一端写括注：按该端补齐，不算冲突；不同关系名各自独立。"""
    ontology = tmp_path / "ontology"
    write_object(ontology, "订单", ORDER_ROWS,
                 "- 当前对象 → 买方客户（buyer_no → customer_no） → [[客户]]\n"
                 "- 当前对象 → 卖方客户（seller_no → customer_no） → [[客户]]")
    write_object(ontology, "客户", CUSTOMER_ROWS,
                 "- [[订单]] → 买方客户 → 当前对象\n"
                 "- [[订单]] → 卖方客户（seller_no → customer_no） → 当前对象")

    assert [i for i in issues(checker, ontology, "关联字段") if "不同的关联字段" in i["message"]] == []


def test_conflict_respects_files_filter(checker, tmp_path: Path):
    ontology = tmp_path / "ontology"
    write_object(ontology, "订单", ORDER_ROWS, "- 当前对象 → 客户（buyer_no → customer_no） → [[客户]]")
    write_object(ontology, "客户", CUSTOMER_ROWS, "- [[订单]] → 客户（seller_no → customer_no） → 当前对象")

    related = issues(checker, ontology, "关联字段", target_files=["客户"])
    found = [i for i in related if "不同的关联字段" in i["message"]]
    assert [i["file"] for i in found] == ["object/客户.md"]


# --- 2. 自关联 ---

def test_self_relation_without_join_is_flagged(checker, tmp_path: Path):
    ontology = tmp_path / "ontology"
    write_object(ontology, "员工", EMPLOYEE_ROWS, "- 当前对象 → 上级员工 → 当前对象")

    found = issues(checker, ontology, "关联字段")
    assert len(found) == 1 and found[0]["severity"] == "警告"
    assert "自关联" in found[0]["message"]


def test_self_relation_with_join_passes(checker, tmp_path: Path):
    ontology = tmp_path / "ontology"
    write_object(ontology, "员工", EMPLOYEE_ROWS,
                 "- 当前对象 → 上级员工（manager_id → employee_id） → [[员工]]")

    assert issues(checker, ontology, "关联字段") == []


# --- 3. 图标识符 ---

def test_attribute_key_must_be_graph_identifier_when_table_declared(checker, tmp_path: Path):
    ontology = tmp_path / "ontology"
    write_object(ontology, "订单", "| 订单号 | order-id | 订单主键 | 是 | 字符串 | 无 | 无 |", table="订单表")

    found = issues(checker, ontology, "图标识符")
    assert len(found) == 1 and found[0]["severity"] == "错误"
    assert "order-id" in found[0]["message"]


def test_attribute_key_not_checked_without_table(checker, tmp_path: Path):
    ontology = tmp_path / "ontology"
    write_object(ontology, "订单", "| 订单号 | order-id | 订单主键 | 是 | 字符串 | 无 | 无 |")

    assert issues(checker, ontology, "图标识符") == []


def test_relation_name_must_be_graph_identifier(checker, tmp_path: Path):
    """括注没紧跟关系名时整串成了关系名：不合规，提示括注位置。"""
    ontology = tmp_path / "ontology"
    write_object(ontology, "订单", ORDER_ROWS,
                 "- 当前对象 → 属于（buyer_no → customer_no）客户 → [[客户]]\n"
                 "- 当前对象 → 下单客户（buyer_no → customer_no） → [[客户]]")
    write_object(ontology, "客户", CUSTOMER_ROWS)

    found = issues(checker, ontology, "图标识符")
    assert len(found) == 1 and found[0]["severity"] == "警告"
    assert "紧跟" in found[0]["message"]


# --- 4. 脚本安全 & 5. 脚本绑定 ---

SCRIPT = '''"""
关联operation：{bound}

功能描述：示例

输入参数：
    amount    float    金额；必填

输出：
    return dict
"""
import argparse
import json
{extra_import}

def compute(amount):
{body}    return {{"total": amount}}


def _cli():
    parser = argparse.ArgumentParser()
    parser.add_argument("--amount", type=float, required=True)
    print(json.dumps(compute(**vars(parser.parse_args()))))


if __name__ == "__main__":
    _cli()
'''


def script(bound: str = "计算金额", extra_import: str = "", body: str = "") -> str:
    return SCRIPT.format(bound=bound, extra_import=extra_import, body=body)


@pytest.mark.parametrize("extra_import, body, label", [
    ("import subprocess", "", "subprocess"),
    ("import pickle", "", "高风险模块"),
    ("import requests", "", "网络访问"),
    ("from urllib.request import urlopen", "", "网络访问"),
    ("import os", "    os.system('true')\n", "系统命令"),
    ("", "    open('out.txt', 'w')\n", "文件写入"),
    ("", "    open('out.txt', mode='a', encoding='utf-8')\n", "文件写入"),
    ("", "    eval('1')\n", "动态执行"),
    ("import builtins", "    builtins.exec('x = 1')\n", "动态执行"),
])
def test_forbidden_script_patterns_are_errors(checker, tmp_path: Path, extra_import, body, label):
    ontology = tmp_path / "ontology"
    write_operation(ontology, "计算金额")
    write_script(ontology, "compute_amount", script(extra_import=extra_import, body=body))

    found = issues(checker, ontology, "脚本安全", include_scripts=True)
    assert [i["severity"] for i in found] == ["错误"]
    assert label in found[0]["message"]


def test_oversized_script_is_error(checker, tmp_path: Path):
    ontology = tmp_path / "ontology"
    write_operation(ontology, "计算金额")
    write_script(ontology, "compute_amount", script() + "#" * checker.SCRIPT_MAX_CHARS)

    found = issues(checker, ontology, "脚本安全", include_scripts=True)
    assert len(found) == 1 and "超长源码" in found[0]["message"]


def test_safe_script_passes(checker, tmp_path: Path):
    """re.compile、读模式 open 不是禁用写法。"""
    ontology = tmp_path / "ontology"
    write_operation(ontology, "计算金额")
    write_script(ontology, "compute_amount", script(
        extra_import="import re",
        body="    re.compile(r'\\d+')\n    open('in.txt')\n    open('in.txt', 'r', encoding='utf-8')\n",
    ))

    assert issues(checker, ontology, "脚本安全", include_scripts=True) == []


def test_ascii_encoding_is_not_write_but_warns_about_eval_regex(checker, tmp_path: Path):
    """encoding="ascii" 不是写模式：不报错误，只提示评测侧正则会误判。"""
    ontology = tmp_path / "ontology"
    write_operation(ontology, "计算金额")
    write_script(ontology, "compute_amount", script(body="    open('in.txt', encoding=\"ascii\")\n"))

    found = issues(checker, ontology, "脚本安全", include_scripts=True)
    assert [i["severity"] for i in found] == ["警告"]


def test_script_bound_to_missing_operation_is_error(checker, tmp_path: Path):
    ontology = tmp_path / "ontology"
    write_operation(ontology, "计算金额")
    write_script(ontology, "compute_amount", script(bound="不存在的操作 / 步骤1-2"))

    found = issues(checker, ontology, "脚本绑定", include_scripts=True)
    assert len(found) == 1 and found[0]["severity"] == "错误"
    assert "不存在的操作" in found[0]["message"]


def test_script_bound_to_existing_operation_passes(checker, tmp_path: Path):
    ontology = tmp_path / "ontology"
    write_operation(ontology, "计算金额")
    write_script(ontology, "compute_amount", script(bound="计算金额 / 步骤1"))

    assert issues(checker, ontology, "脚本绑定", include_scripts=True) == []


def test_unbound_script_is_suggestion(checker, tmp_path: Path):
    ontology = tmp_path / "ontology"
    write_script(ontology, "compute_amount", script(bound="无"))
    write_script(ontology, "_helpers", script(bound="无"))

    found = issues(checker, ontology, "脚本绑定", include_scripts=True)
    assert [(i["file"], i["severity"]) for i in found] == [("scripts/compute_amount.py", "建议")]


# --- 6. key 约束 ---

def test_invalid_entity_and_field_keys_are_errors(checker, tmp_path: Path):
    ontology = tmp_path / "ontology"
    write_object(ontology, "订单#明细", "| 订单号 | order..id | 订单主键 | 是 | 字符串 | 无 | 无 |")
    write_operation(ontology, "长" * 81)

    found = issues(checker, ontology, "命名约束")
    assert all(i["severity"] == "错误" for i in found)
    messages = " ".join(i["message"] for i in found)
    assert "订单#明细" in messages and "order..id" in messages and "超过 80 字符" in messages
    assert len(found) == 3


def test_valid_keys_pass(checker, tmp_path: Path):
    ontology = tmp_path / "ontology"
    write_object(ontology, "订单", ORDER_ROWS)
    write_operation(ontology, "计算金额")

    assert issues(checker, ontology, "命名约束") == []


# --- 7. consumer 容量上限 ---

def test_consumer_limits_warn(checker, tmp_path: Path):
    ontology = tmp_path / "ontology"
    row = "| 属性{0} | field_{0} | 示例 | {1} | 字符串 | 无 | 无 |"
    count = checker.CONSUMER_ATTRIBUTE_MAX + 1
    rows = "\n".join(row.format(i, "是" if i == 0 else "否") for i in range(count))
    write_object(ontology, "宽对象", rows)
    write_operation(ontology, "长操作", OPERATION_DOC + "说明。" * checker.CONSUMER_OPERATION_MAX_CHARS)
    for i in range(checker.CONSUMER_OPERATION_MAX):
        write_operation(ontology, f"操作{i}")

    found = issues(checker, ontology, "容量上限")
    assert all(i["severity"] == "警告" for i in found)
    assert sorted(i["file"] for i in found) == ["object/宽对象.md", "operation/", "operation/长操作.md"]


def test_within_consumer_limits_passes(checker, tmp_path: Path):
    ontology = tmp_path / "ontology"
    write_object(ontology, "订单", ORDER_ROWS)
    write_operation(ontology, "计算金额")

    assert issues(checker, ontology, "容量上限") == []


def test_consumer_limit_defaults_match_publish_rules(checker, monkeypatch):
    from app.services.consumer_package_rules.limits import LimitsConfig

    for variable in ('ONTOLOGY_OBJECT_MAX_COUNT', 'ONTOLOGY_OPERATION_MAX_COUNT',
                     'ENTITY_ATTRIBUTE_MAX_COUNT', 'ONTOLOGY_OPERATION_MAX_CHARS'):
        monkeypatch.delenv(variable, raising=False)
    limits = LimitsConfig.from_env()
    assert checker.CONSUMER_OBJECT_MAX == limits.ontology_object_max_count == 1000
    assert checker.CONSUMER_OPERATION_MAX == limits.ontology_operation_max_count == 500
    assert checker.CONSUMER_ATTRIBUTE_MAX == limits.entity_attribute_max_count == 300
    assert checker.CONSUMER_OPERATION_MAX_CHARS == limits.operation_max_chars == 30000


@pytest.mark.parametrize('kind,limit_name', [
    ('objects', 'CONSUMER_OBJECT_MAX'),
    ('operations', 'CONSUMER_OPERATION_MAX'),
    ('attributes', 'CONSUMER_ATTRIBUTE_MAX'),
    ('characters', 'CONSUMER_OPERATION_MAX_CHARS'),
])
@pytest.mark.parametrize('extra', [0, 1])
def test_consumer_limit_boundaries(checker, tmp_path, kind, limit_name, extra):
    count = getattr(checker, limit_name) + extra
    object_doc = checker.OntologyDoc(tmp_path / 'Item.md', 'object', '')
    operation_doc = checker.OntologyDoc(tmp_path / 'Operation.md', 'operation', '')
    if kind == 'objects':
        documents = {f'Item{index}': object_doc for index in range(count)}
    elif kind == 'operations':
        documents = {f'Operation{index}': operation_doc for index in range(count)}
    elif kind == 'attributes':
        object_doc.attribute_rows = [{} for _ in range(count)]
        documents = {'Item': object_doc}
    else:
        operation_doc.raw = '示' * count
        documents = {'Operation': operation_doc}
    found = checker.check_package_limits(documents, documents)
    assert len(found) == extra
    assert all(item.severity == '警告' for item in found)


# --- 8. 主键必须是真实列 ---

def test_primary_key_missing_from_table_columns_is_error(checker, tmp_path: Path, monkeypatch):
    ontology = tmp_path / "ontology"
    monkeypatch.setattr(checker, "load_table_headers",
                        lambda _: {"订单表": ["order_no", "buyer_no", "seller_no"]})
    write_object(ontology, "订单", ORDER_ROWS, table="订单表")

    found = [i for i in issues(checker, ontology, "列名对账") if "主键属性" in i["message"]]
    assert len(found) == 1 and found[0]["severity"] == "错误"
    assert "order_id" in found[0]["message"]


def test_primary_key_in_table_columns_passes(checker, tmp_path: Path, monkeypatch):
    ontology = tmp_path / "ontology"
    monkeypatch.setattr(checker, "load_table_headers",
                        lambda _: {"订单表": ["order_id", "buyer_no", "seller_no"]})
    write_object(ontology, "订单", ORDER_ROWS, table="订单表")

    assert issues(checker, ontology, "列名对账") == []


def test_invalid_script_file_name_is_error(checker, tmp_path: Path):
    ontology = tmp_path / "ontology"
    write_operation(ontology, "计算金额")
    write_script(ontology, "compute&amount", script())
    write_script(ontology, "compute_amount", script())

    found = issues(checker, ontology, "命名约束", include_scripts=True)
    assert [(i["file"], i["severity"]) for i in found] == [("scripts/compute&amount.py", "错误")]
