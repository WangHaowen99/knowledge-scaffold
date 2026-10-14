"""ontology_checker 的「目录归属」检查：规则与指标不能留在 operation 目录。

实体类型由所在目录决定（见 `load_ontology`），放错目录不只是乱放：发布包按目录
分 operations/rules/metrics，studio 图谱同样按目录建节点，规则留在
`ontology/operation/` 就会以操作的身份送到 consumer。

判据只认两样确定性信号——资料分析报告「业务项清单」的分类列，或文档自身就是
rule/metric 模板的段落集。数据全部合成。
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


OPERATION_DOC = """# 业务描述
{name}。

# 与operation的关联
- 被operation调用 > 无（用户在合成入口直接触发）
- 调用operation > 无

# 与object的关联
- 输入数据的对象：无

# 与object的语义关联
| 业务对象 | 语义关联关系 |
| --- | --- |
| 无 | 无 |

# 触发条件
无

# 输入
无

# 输出
无

# 逻辑描述
1. if 合成金额 >= 100 then 通过 else 拒绝
"""

RULE_DOC = """# 规则描述
{name}。

# 规则类型
阈值校验

# 适用对象
[[合成订单]]

# 触发条件
提交时

# 约束表达
合成金额 >= 100

# 违反处置
拒绝并提示

# 来源
research/报告.md

# 例外情况
无
"""

METRIC_DOC = """# 指标描述
{name}。

# 计算公式
合成金额合计 / 合成订单数

# 数据来源
research/报告.md

# 维度与粒度
按日

# 单位
元

# 更新频率
每日
"""

HEADER = ("| 业务项ID | 业务项名称 | 分类 | 规则ID | 引用字段 | 来源位置 |\n"
          "| --- | --- | --- | --- | --- | --- |\n")


def write_doc(ontology: Path, subdir: str, name: str, template: str) -> None:
    (ontology / subdir).mkdir(parents=True, exist_ok=True)
    (ontology / subdir / f"{name}.md").write_text(template.format(name=name), encoding="utf-8")


def write_report(ontology: Path, rows: str, name: str = "报告.md", header: str = HEADER) -> None:
    research = ontology.parent / "research"
    research.mkdir(parents=True, exist_ok=True)
    research.joinpath(name).write_text("# 业务项清单\n" + header + rows, encoding="utf-8")


def placement_issues(checker, ontology: Path) -> list[dict]:
    issues = [dataclasses.asdict(i) for i in checker.run_checks(ontology)]
    return [i for i in issues if i["category"] == "目录归属"]


# ---------------------------------------------------------------------------
# 判据一：报告声明的分类
# ---------------------------------------------------------------------------

def test_rule_in_operation_dir_is_error(checker, tmp_path: Path):
    """现场形态：报告把它分类为规则，提取却按 operation 模板写进了 operation 目录。"""
    ontology = tmp_path / "ontology"
    write_doc(ontology, "operation", "合成金额校验规则", OPERATION_DOC)
    write_report(ontology, "| ITEM-01 | 合成金额校验规则 | 规则 | RULE-01 | 无 | 无 |\n")

    issues = placement_issues(checker, ontology)

    assert len(issues) == 1
    assert issues[0]["severity"] == "错误"  # pre_commit_check.sh 按错误级阻止提交
    assert "ontology/rule/" in issues[0]["message"]
    assert "rule-template.md" in issues[0]["message"]


def test_metric_in_operation_dir_points_to_metric_dir(checker, tmp_path: Path):
    ontology = tmp_path / "ontology"
    write_doc(ontology, "operation", "合成客单价", OPERATION_DOC)
    write_report(ontology, "| ITEM-01 | 合成客单价 | 指标 | 无 | 无 | 无 |\n")

    issues = placement_issues(checker, ontology)

    assert len(issues) == 1
    assert "ontology/metric/" in issues[0]["message"]


def test_operation_category_is_not_flagged(checker, tmp_path: Path):
    ontology = tmp_path / "ontology"
    write_doc(ontology, "operation", "创建合成订单", OPERATION_DOC)
    write_report(ontology, "| ITEM-01 | 创建合成订单 | 操作 | 无 | 无 | 无 |\n")

    assert placement_issues(checker, ontology) == []


def test_rule_in_rule_dir_is_not_flagged(checker, tmp_path: Path):
    """分流到位的规则不该再被报出来。"""
    ontology = tmp_path / "ontology"
    write_doc(ontology, "rule", "合成金额校验规则", RULE_DOC)
    write_report(ontology, "| ITEM-01 | 合成金额校验规则 | 规则 | RULE-01 | 无 | 无 |\n")

    assert placement_issues(checker, ontology) == []


def test_name_missing_only_the_suffix_is_a_warning_not_an_error(checker, tmp_path: Path):
    """报告里没有「XX」、只有规则「XX规则」，而 operation 目录下有「XX」。

    可能是提取时丢了后缀的规则，也可能是流程图来源的同名操作——检查器分不出来，
    所以只提示、不拦提交：判成错误会把后一种正常的操作文档挡在提交之外。
    """
    ontology = tmp_path / "ontology"
    write_doc(ontology, "operation", "合成金额校验", OPERATION_DOC)
    write_report(ontology, "| ITEM-01 | 合成金额校验规则 | 规则 | RULE-01 | 无 | 无 |\n")

    issues = placement_issues(checker, ontology)

    assert [issue["severity"] for issue in issues] == ["警告"]
    assert "疑似" in issues[0]["message"]
    assert "合成金额校验规则" in issues[0]["message"]
    assert "ontology/rule/" in issues[0]["message"]


def test_missing_suffix_is_not_inferred_when_report_names_the_operation(checker, tmp_path: Path):
    """报告同时列了操作「XX」和规则「XX规则」：以报告对这个名字的分类为准，不提示。"""
    ontology = tmp_path / "ontology"
    write_doc(ontology, "operation", "合成金额校验", OPERATION_DOC)
    write_report(ontology,
                 "| ITEM-01 | 合成金额校验规则 | 规则 | RULE-01 | 无 | 无 |\n"
                 "| ITEM-02 | 合成金额校验 | 操作 | 无 | 无 | 无 |\n")

    assert placement_issues(checker, ontology) == []


# ---------------------------------------------------------------------------
# 判据二：文档自身的模板段落集（没有报告的项目，如纯流程图来源）
# ---------------------------------------------------------------------------

def test_rule_template_doc_in_operation_dir_without_report(checker, tmp_path: Path):
    ontology = tmp_path / "ontology"
    write_doc(ontology, "operation", "合成退款时限规则", RULE_DOC)

    issues = placement_issues(checker, ontology)

    assert len(issues) == 1
    assert "ontology/rule/" in issues[0]["message"]


def test_metric_template_doc_in_operation_dir_without_report(checker, tmp_path: Path):
    ontology = tmp_path / "ontology"
    write_doc(ontology, "operation", "合成转化率", METRIC_DOC)

    issues = placement_issues(checker, ontology)

    assert len(issues) == 1
    assert "ontology/metric/" in issues[0]["message"]


def test_plain_operation_without_report_is_not_flagged(checker, tmp_path: Path):
    """没有报告、文档也不是规则/指标模板时不猜：命名约束由提取侧的 agent 守。"""
    ontology = tmp_path / "ontology"
    write_doc(ontology, "operation", "创建合成订单", OPERATION_DOC)

    assert placement_issues(checker, ontology) == []


# ---------------------------------------------------------------------------
# load_report_categories 的解析边界
# ---------------------------------------------------------------------------

def test_report_without_category_column_yields_no_categories(checker, tmp_path: Path):
    """旧契约报告没有分类列：这条判据失效，退回模板段落集判断，不乱报。"""
    ontology = tmp_path / "ontology"
    write_doc(ontology, "operation", "合成金额校验规则", OPERATION_DOC)
    write_report(ontology,
                 "| ITEM-01 | 合成金额校验规则 | RULE-01 | 无 | 无 |\n",
                 header=("| 业务项ID | 业务项名称 | 规则ID | 引用字段 | 来源位置 |\n"
                         "| --- | --- | --- | --- | --- |\n"))

    assert checker.load_report_categories(ontology) == {}
    assert placement_issues(checker, ontology) == []


def test_conflicting_categories_across_reports_are_dropped(checker, tmp_path: Path):
    """两份报告给同一名称不同分类：互相矛盾的依据不足以判错，整条丢弃。"""
    ontology = tmp_path / "ontology"
    write_doc(ontology, "operation", "合成金额校验规则", OPERATION_DOC)
    write_report(ontology, "| ITEM-01 | 合成金额校验规则 | 规则 | RULE-01 | 无 | 无 |\n")
    write_report(ontology, "| ITEM-01 | 合成金额校验规则 | 操作 | 无 | 无 | 无 |\n",
                 name="报告二.md")

    assert checker.load_report_categories(ontology) == {}
    assert placement_issues(checker, ontology) == []


def test_report_categories_map_names_to_doc_types(checker, tmp_path: Path):
    ontology = tmp_path / "ontology"
    ontology.mkdir()
    write_report(ontology,
                 "| ITEM-01 | 合成订单 | 对象 | 无 | 无 | 无 |\n"
                 "| ITEM-02 | 创建合成订单 | 操作 | 无 | 无 | 无 |\n"
                 "| ITEM-03 | 合成金额校验规则 | 规则 | RULE-03 | 无 | 无 |\n"
                 "| ITEM-04 | 合成客单价 | 指标 | 无 | 无 | 无 |\n")

    assert checker.load_report_categories(ontology) == {
        "合成订单": "object",
        "创建合成订单": "operation",
        "合成金额校验规则": "rule",
        "合成客单价": "metric",
    }
