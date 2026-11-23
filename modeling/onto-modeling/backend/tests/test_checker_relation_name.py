"""ontology_checker 对「# 关联对象」关系名的检查。

关系名只写谓词，不含两端对象名称，也不只写基数。发布包里对象的 key 与标题都取文件名，所以端点名称就是文件名。
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
宽表

# 对象属性清单
| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |
| --- | --- | --- | --- | --- | --- | --- |
| 编号 | ID | 主键 | 是 | 字符串 | 无 | 无 |

# 关联对象
{relations}
"""


def write(ontology: Path, name: str, relations: str = "") -> None:
    ontology.joinpath("object").mkdir(parents=True, exist_ok=True)
    (ontology / "object" / f"{name}.md").write_text(
        OBJECT_DOC.format(name=name, relations=relations),
        encoding="utf-8",
    )


def name_issues(checker, ontology: Path) -> list[str]:
    issues = [dataclasses.asdict(i) for i in checker.run_checks(ontology)]
    return [i["message"] for i in issues if i["category"] == "关系名称"]


def test_relation_name_repeating_target_is_flagged_on_both_ends(checker, tmp_path: Path):
    """两端各报自己那一行：改名要两端一起改。"""
    ontology = tmp_path / "ontology"
    write(ontology, "订单", "- 当前对象 → 所属客户 → [[客户]]")
    write(ontology, "客户", "- [[订单]] → 所属客户 → 当前对象")

    messages = name_issues(checker, ontology)
    assert len(messages) == 2
    assert all("目标对象名称「客户」" in message for message in messages)
    assert all("关系「订单 -所属客户→ 客户」" in message for message in messages)


def test_relation_name_repeating_source_is_flagged(checker, tmp_path: Path):
    ontology = tmp_path / "ontology"
    write(ontology, "订单", "- 当前对象 → 订单归属 → [[客户]]")
    write(ontology, "客户")

    messages = name_issues(checker, ontology)
    assert len(messages) == 1
    assert "源对象名称「订单」" in messages[0]


def test_predicate_only_name_passes(checker, tmp_path: Path):
    ontology = tmp_path / "ontology"
    write(ontology, "订单", "- 当前对象 → 属于 → [[客户]]")
    write(ontology, "客户", "- [[订单]] → 属于 → 当前对象")

    assert name_issues(checker, ontology) == []


@pytest.mark.parametrize("name", ["1:N", "一对多", "one-to-many", "0..1", "N 对 M"])
def test_cardinality_only_name_is_flagged(checker, tmp_path: Path, name: str):
    ontology = tmp_path / "ontology"
    write(ontology, "订单", f"- 当前对象 → {name} → [[客户]]")
    write(ontology, "客户")

    messages = name_issues(checker, ontology)
    assert len(messages) == 1
    assert "关系基数" in messages[0]


def test_comparison_ignores_case_width_and_punctuation(checker, tmp_path: Path):
    """NFKC 后忽略大小写、空白与标点：全角、换大小写、插连字符都还是同一个名称。"""
    ontology = tmp_path / "ontology"
    write(ontology, "Order", "- 当前对象 → belongs-to-ＣＵＳＴ ＯＭＥＲ → [[Customer]]")
    write(ontology, "Customer")

    messages = name_issues(checker, ontology)
    assert len(messages) == 1
    assert "目标对象名称「Customer」" in messages[0]


def test_single_character_endpoint_is_ignored(checker, tmp_path: Path):
    """不足 2 个字符的端点名称不参与比较，否则单字名称会误伤几乎所有谓词。"""
    ontology = tmp_path / "ontology"
    write(ontology, "订单", "- 当前对象 → 交给甲方 → [[甲]]")
    write(ontology, "甲")

    assert name_issues(checker, ontology) == []


def test_join_note_and_placeholder_are_not_part_of_the_name(checker, tmp_path: Path):
    """括注是关联字段声明，不算关系名；模板占位符还没填写，不报。"""
    ontology = tmp_path / "ontology"
    write(ontology, "订单",
          "- 当前对象 → 属于（客户） → [[客户]]\n"
          "- 当前对象 → {待填写：关系名} → [[客户]]")
    write(ontology, "客户")

    assert name_issues(checker, ontology) == []
