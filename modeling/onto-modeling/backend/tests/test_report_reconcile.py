"""资料分析报告与提取产物的对账（report_reconcile.py）。

提取前按报告分类拆候选、与流程图比对；提取后逐项对账，让"生成的跟报告不一致"的每一处
差异都有记录和来源。覆盖"两者结合"：流程图独有的流程允许生成，但要标明来自流程图。
数据全部合成。
"""

import importlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app import config

SCRIPTS = Path(config.ASSETS_DIR) / "workspace-toolkit" / "scripts"
GIT = shutil.which("git")

HEADER = ("# 业务项清单\n| 业务项ID | 业务项名称 | 分类 | 规则ID | 引用字段 | 来源位置 |\n"
          "| --- | --- | --- | --- | --- | --- |\n")
# 对象 1 / 操作 2 / 规则 1 / 指标 1
INVENTORY = (
    ("ITEM-01", "合成订单", "对象"),
    ("ITEM-02", "创建合成订单", "操作"),
    ("ITEM-03", "合成审核", "操作"),
    ("ITEM-04", "合成金额校验规则", "规则"),
    ("ITEM-05", "合成客单价", "指标"),
)


@pytest.fixture
def reconcile(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    return importlib.import_module("report_reconcile")


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "ws"
    for sub in ("research", "ontology/diagrams-summary", "ontology/operation",
                "ontology/rule", "ontology/metric"):
        (root / sub).mkdir(parents=True)
    monkeypatch.chdir(root)
    return root


def write_report(root: Path, rows=INVENTORY, name="报告.md") -> None:
    lines = [HEADER]
    for item_id, title, category in rows:
        lines.append(f"| {item_id} | {title} | {category} | 无 | 无 | 无 |\n")
    (root / "research" / name).write_text("".join(lines), encoding="utf-8")


def write_summaries(root: Path, *names: str) -> None:
    for name in names:
        (root / "ontology" / "diagrams-summary" / f"{name}.md").write_text(
            f"# {name}\n合成流程摘要。\n", encoding="utf-8")


def write_docs(root: Path, subdir: str, *names: str) -> None:
    for name in names:
        (root / "ontology" / subdir / f"{name}.md").write_text(f"# 合成\n{name}。\n", encoding="utf-8")


def run(reconcile, capsys, *args) -> tuple[int, dict]:
    code = reconcile.main([*args, "--json"])
    return code, json.loads(capsys.readouterr().out)


def git_commit_all(root: Path) -> None:
    commands = (["init", "-q"], ["config", "user.email", "user@example.com"],
                ["config", "user.name", "operator_a"], ["add", "-A"],
                ["commit", "-q", "-m", "合成初始提交"])
    for command in commands:
        # 安全审查：argv 是 shutil.which 解析出的 git 路径，加本测试写死的子命令与合成
        # 参数；以序列传入、不经 shell 解析，只作用于 tmp_path 下的临时仓库。
        subprocess.run(  # NOSONAR(S4721)
            [GIT, *command], cwd=root, check=True, capture_output=True, shell=False, timeout=60,
        )


# ---------------------------------------------------------------------------
# plan：提取前拆候选、比对流程图
# ---------------------------------------------------------------------------

def test_plan_splits_candidates_and_classifies_flowcharts(workspace, reconcile, capsys):
    """两者结合：同名流程图合成一份，报告没收录的流程图是补充。"""
    write_report(workspace)
    write_summaries(workspace, "创建合成订单", "合成退款")

    code, plan = run(reconcile, capsys, "plan")

    assert code == reconcile.OK
    assert plan["candidates"]["operation"] == ["创建合成订单", "合成审核"]
    assert plan["candidates"]["rule"] == ["合成金额校验规则"]
    assert plan["candidates"]["metric"] == ["合成客单价"]
    assert plan["flowchart_matches"] == ["创建合成订单"]
    assert plan["flowchart_supplements"] == ["合成退款"]
    assert plan["flowchart_conflicts"] == []


@pytest.mark.parametrize("flowchart,report_type", [
    pytest.param("合成客单价", "metric", id="与指标重名"),
    pytest.param("合成金额校验规则", "rule", id="与规则重名"),
    pytest.param("合成订单", "object", id="与对象重名"),
])
def test_plan_flags_flowchart_named_like_a_non_operation(
        workspace, reconcile, capsys, flowchart, report_type):
    """流程图把它当操作、报告把它当规则/指标/对象：提取前就要由用户决定。"""
    write_report(workspace)
    write_summaries(workspace, flowchart)

    code, plan = run(reconcile, capsys, "plan")

    assert code == reconcile.DIFF
    assert plan["flowchart_conflicts"] == [{"name": flowchart, "report_type": report_type}]


def test_plan_without_flowcharts_has_no_supplements(workspace, reconcile, capsys):
    """本轮不用流程图（--summaries 不带值）时只按报告拆候选。"""
    write_report(workspace)
    write_summaries(workspace, "合成退款")

    code, plan = run(reconcile, capsys, "plan", "--summaries")

    assert code == reconcile.OK
    assert plan["flowchart_supplements"] == []


@pytest.mark.parametrize("setup,args", [
    pytest.param(lambda root: None, ["plan"], id="没有报告"),
    pytest.param(lambda root: write_report(root), ["plan", "--reports"], id="本轮不用报告"),
    pytest.param(
        lambda root: (root / "research" / "旧报告.md").write_text(
            "# 业务项清单\n| 业务项ID | 业务项名称 | 规则ID | 引用字段 | 来源位置 |\n"
            "| --- | --- | --- | --- | --- |\n| ITEM-01 | 合成订单 | 无 | 无 | 无 |\n", encoding="utf-8"),
        ["plan"], id="报告没有分类列"),
    pytest.param(
        lambda root: write_report(root, [("ITEM-01", "合成订单", "流程")]),
        ["check"], id="分类取值非法"),
])
def test_unavailable_without_a_categorized_report(workspace, reconcile, capsys, setup, args):
    """没有带分类的报告就无从对账，不能拿名字去猜分类。"""
    setup(workspace)

    code, result = run(reconcile, capsys, *args)

    assert code == reconcile.UNAVAILABLE
    assert result["status"] == "unavailable"


def test_conflicting_categories_across_reports_need_a_decision(workspace, reconcile, capsys):
    write_report(workspace)
    write_report(workspace, [("ITEM-09", "合成审核", "规则")], name="补充报告.md")

    code, plan = run(reconcile, capsys, "plan")

    assert code == reconcile.DIFF
    assert plan["category_conflicts"] == ["合成审核"]
    assert "合成审核" not in plan["candidates"]["operation"]


# ---------------------------------------------------------------------------
# check：提取后对账
# ---------------------------------------------------------------------------

def _generate_all(root: Path) -> None:
    write_docs(root, "operation", "创建合成订单", "合成审核")
    write_docs(root, "rule", "合成金额校验规则")
    write_docs(root, "metric", "合成客单价")


def test_check_passes_when_report_items_exist_and_extras_come_from_flowcharts(
        workspace, reconcile, capsys):
    """报告条目全部落在正确目录；多出来的操作来自流程图——差异有来源，不算问题。"""
    write_report(workspace)
    write_summaries(workspace, "创建合成订单", "合成退款")
    _generate_all(workspace)
    write_docs(workspace, "operation", "合成退款")

    code, result = run(reconcile, capsys, "check")

    assert code == reconcile.OK
    assert result["matched_counts"] == {"operation": 2, "rule": 1, "metric": 1}
    assert result["extra"] == [
        {"name": "合成退款", "type": "operation", "source": "flowchart", "changed": True}]


def test_check_reports_missing_report_items(workspace, reconcile, capsys):
    write_report(workspace)
    write_docs(workspace, "operation", "创建合成订单")

    code, result = run(reconcile, capsys, "check")

    assert code == reconcile.DIFF
    assert result["missing"] == {"operation": ["合成审核"], "rule": ["合成金额校验规则"],
                                 "metric": ["合成客单价"]}


def test_check_reports_items_generated_in_the_wrong_directory(workspace, reconcile, capsys):
    """报告里是规则，却生成成了操作：单独报出，也不算作规则已生成。"""
    write_report(workspace)
    _generate_all(workspace)
    (workspace / "ontology" / "rule" / "合成金额校验规则.md").unlink()
    write_docs(workspace, "operation", "合成金额校验规则")

    code, result = run(reconcile, capsys, "check")

    assert code == reconcile.DIFF
    assert result["misplaced"] == [
        {"name": "合成金额校验规则", "expected": "rule", "found": ["operation"]}]
    assert result["matched_counts"]["rule"] == 0
    assert result["missing"]["rule"] == []


def test_alias_counts_a_renamed_flowchart_operation_as_flowchart(workspace, reconcile, capsys):
    """用户决定把与指标重名的流程图改名生成：按 --alias 声明后算流程图来源。"""
    write_report(workspace)
    write_summaries(workspace, "合成客单价")
    _generate_all(workspace)
    write_docs(workspace, "operation", "合成客单价流程")

    code, result = run(reconcile, capsys, "check", "--alias", "合成客单价流程=合成客单价")

    assert code == reconcile.OK
    assert result["extra"][0]["source"] == "flowchart"


def test_flowchart_without_its_own_doc_is_listed(workspace, reconcile, capsys):
    """报告没收录、也没单独成文的流程图（应已并入报告里的某个操作）要列出来供核对。"""
    write_report(workspace)
    write_summaries(workspace, "合成退款")
    _generate_all(workspace)

    code, result = run(reconcile, capsys, "check")

    assert code == reconcile.OK
    assert result["flowcharts_without_doc"] == ["合成退款"]


@pytest.mark.skipif(GIT is None, reason="需要 git 区分本轮改动")
def test_unexplained_new_doc_needs_action_but_earlier_docs_do_not(workspace, reconcile, capsys):
    """报告外的文档：本轮新增的要说明，之前就有、本轮没动的不计入差异。

    之前就有的如从知识包导入的规则；它们每次都算差异的话，对账会被淹没。
    """
    write_report(workspace)
    _generate_all(workspace)
    write_docs(workspace, "rule", "导入的合成规则")
    git_commit_all(workspace)

    code, result = run(reconcile, capsys, "check")
    assert code == reconcile.OK
    assert result["extra"] == [
        {"name": "导入的合成规则", "type": "rule", "source": "unknown", "changed": False}]

    write_docs(workspace, "operation", "合成发货")
    code, result = run(reconcile, capsys, "check")
    assert code == reconcile.DIFF
    assert {"name": "合成发货", "type": "operation", "source": "unknown",
            "changed": True} in result["extra"]


def test_without_git_every_extra_counts_as_changed(workspace, reconcile, capsys):
    """不是 git 仓库时分不出哪些是本轮的，按"本轮改动"处理：宁可多报，不漏报。"""
    write_report(workspace)
    _generate_all(workspace)
    write_docs(workspace, "rule", "来源不明的合成规则")

    code, result = run(reconcile, capsys, "check")

    assert code == reconcile.DIFF
    assert result["extra"][0]["changed"] is True


@pytest.mark.skipif(GIT is None, reason="需要 git")
def test_workspace_below_repo_root_counts_extras_as_changed(
        tmp_path, reconcile, capsys, monkeypatch):
    """git 路径相对仓库根目录；工作区不是仓库根时对不上路径，同样按"本轮改动"处理。"""
    repo = tmp_path / "repo"
    workspace = repo / "nested" / "ws"
    for sub in ("research", "ontology/operation", "ontology/rule", "ontology/metric"):
        (workspace / sub).mkdir(parents=True)
    write_report(workspace)
    _generate_all(workspace)
    write_docs(workspace, "rule", "之前就有的合成规则")
    git_commit_all(repo)
    monkeypatch.chdir(workspace)

    code, result = run(reconcile, capsys, "check")

    assert code == reconcile.DIFF
    assert result["extra"][0]["changed"] is True


def test_text_output_lists_counts_and_sources(workspace, reconcile, capsys):
    """不带 --json 时给人看的摘要：逐类计数、缺项名称、流程图补充。"""
    write_report(workspace)
    write_summaries(workspace, "合成退款")
    write_docs(workspace, "operation", "创建合成订单", "合成退款")

    reconcile.main(["check"])
    text = capsys.readouterr().out

    assert "操作：报告 2 个，已生成 1 个；缺 1 个：合成审核" in text
    assert "流程图补充的操作 1 个：合成退款" in text
