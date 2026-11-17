"""建议条能把建模带到发布：用户一直点主推，不会在两步之间来回兜圈子（端到端实测后补的模拟）。

每条命令按节点声明的写入范围改主线上的条目、合入时确认这个节点（和变更集合入一样），再按建议条取下一步；
中途绕道跑一次旁支命令（规则与指标提取、代码生成、追加资料分析），之后照样一直点主推。走到「去发布」算通过。

测试数据全部为合成内容。
"""

from __future__ import annotations

import pytest

from app import wiring
from app.services.flow import view
from app.services.research_view import _script
from tests.test_changesets import main_write, obj
from tests.test_node_pack_lifecycle import env  # noqa: F401 - 共用 fixture（装了场景模拟、代码生成等默认包）

FILES = {
    "object": "ontology/object/门店.md",
    "operation": "ontology/operation/受理申请.md",
    "rule": "ontology/rule/取严规则.md",
    "metric": "ontology/metric/达标率.md",
    "testcase": "ontology/testcase/受理申请.md",
    "scripts": "ontology/scripts/受理申请.py",
}
# 命令 → (节点, 这条命令总会改的类别)
COMMANDS = {
    "/ontology-research": ("core.research", ["research"]),
    "/ontology-extract-object": ("core.object", ["object"]),
    "/ontology-extract-operation": ("core.operation", ["operation", "rule", "metric"]),
    "/ontology-extract-rule-metric": ("core.metric", ["rule", "metric"]),
    "/ontology-validate": ("core.validate", ["report"]),
    "/ontology-trace": ("trace.trace", ["testcase"]),
    "/ontology-codegen-scan": ("codegen.codegen", ["scripts"]),
    "/ontology-codegen": ("codegen.codegen", ["scripts"]),
}


class Project:
    def __init__(self, env, extra: dict):
        _client, manager, self.project_id, _tmp = env
        self.root = manager.project_root(self.project_id)
        self.extra = extra
        self.edits = 0

    def write(self, kind: str) -> None:
        self.edits += 1
        mark = f"\n<!-- 第 {self.edits} 次改 -->\n"
        if kind == "research":
            main_write(self.root, "research/20260101-合成.md", "# 合成资料分析\n" + mark)
            review = _script(self.root, "research_review")
            review.write_review(self.root, review.PASSED, review.file_revisions(self.root, review.report_paths(self.root)),
                                [], [], {})
        elif kind == "report":
            for suffix in ("rule", "semantic"):
                main_write(self.root, f"reports/20260101-{suffix}.md", f"# 合成检查报告\n\n- [x] 已处理{mark}")
        elif kind == "object":
            main_write(self.root, FILES[kind], obj("门店", f"合成门店{self.edits}"))
        else:
            main_write(self.root, FILES[kind], f"# 合成\n\n[[门店]]{mark}")

    def run(self, command: str) -> None:
        node, kinds = COMMANDS[command]
        for kind in kinds + self.extra.get(command, []):
            self.write(kind)
        wiring._confirm_nodes(self.project_id, [node], {})


def follow(project: Project, *, detour: tuple[int, str] | None = None, limit: int = 20) -> list[str]:
    """一直点主推，直到「去发布」；返回点过的按钮。绕道 (第几步之前, 命令)。"""
    clicked: list[str] = []
    for _ in range(limit):
        if detour and len(clicked) == detour[0]:
            project.run(detour[1])
            clicked.append(f"（绕道 {detour[1]}）")
            detour = None
            continue
        primary = view.build(project.root)["suggestions"]["primary"] or {}
        clicked.append(primary.get("label") or "")
        action = primary.get("action") or {}
        if action.get("type") == "navigate" and action.get("page") == "publish":
            return clicked
        command = action.get("command") or ""
        assert command in COMMANDS, clicked
        project.run(command)
    raise AssertionError(f"{limit} 步还没走到发布：{' → '.join(clicked)}")


# 校验的修复、规则与指标提取补关联都可能改对象和操作（节点声明的写入范围里有）
EXTRA = {"/ontology-validate": ["object", "operation"], "/ontology-extract-rule-metric": ["object", "operation"]}


def test_mainline_reaches_publish(env):
    clicked = follow(Project(env, EXTRA))
    assert clicked == ["资料分析", "提取对象", "提取操作", "运行质量检查", "批量验证", "去发布"]


@pytest.mark.parametrize("at", [1, 2, 3, 4, 5])
@pytest.mark.parametrize("command", ["/ontology-extract-rule-metric", "/ontology-codegen", "/ontology-research"])
def test_detour_still_reaches_publish(env, at, command):
    """原来规则与指标提取补了对象的关联之后，建议条在「刷新操作」「刷新指标」之间来回推，走不到发布。"""
    clicked = follow(Project(env, EXTRA), detour=(at, command))
    assert clicked[-1] == "去发布"
    assert not any(label.endswith("刷新指标") for label in clicked)
