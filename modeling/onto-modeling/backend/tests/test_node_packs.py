"""节点包（详设 6.3～6.9；方案 D18；用户 2026-11-02 定：能力包的特例）。

- 上传静态检查：命令存在、写入范围不碰受保护目录、不新增实体类型、兼容版本、不能带 hooks / skill / 页面 /
  产物登记 / 界面代码，四类节点各自的声明；
- 插件目录里归进能力包。

测试用的节点包全部是合成内容（tests/pack_helpers.make_node_pack），不进 assets/packs。
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from app import config
from app.services.packs import checks
from app.services.packs import manifest as mf
from app.services.workspace import WorkspaceManager
from tests.pack_helpers import NODE_PACK_FLOW, make_node_pack


@pytest.fixture()
def repo(tmp_path: Path):
    manager = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    assert manager.packs.core_version()
    return manager.packs.repository


def run(directory: Path, repository) -> dict[str, dict]:
    manifest, items = checks.run(directory, repository, tenant=True)
    return {item["id"]: item for item in items}


def flow_with(**node_fields) -> dict:
    flow = copy.deepcopy(NODE_PACK_FLOW)
    flow["nodes"][0].update(node_fields)
    return flow


def failed(result: dict[str, dict]) -> set[str]:
    return {key for key, item in result.items() if item["result"] == "fail"}


def test_good_node_pack_passes_and_is_a_capability(tmp_path, repo):
    directory = make_node_pack(tmp_path / "src", tenant=True)
    result = run(directory, repo)
    assert not failed(result), result
    assert result["node_pack"]["result"] == "pass"
    manifest = mf.load(directory)
    assert manifest.kind == "node" and manifest.category == "capability"
    # 清单里自己写 category: node 也归进能力包
    assert mf.parse({**manifest.raw, "category": "node"}).category == "capability"


def test_entry_command_must_exist_in_the_pack(tmp_path, repo):
    flow = flow_with(entry={"command": "/naming-missing"})
    result = run(make_node_pack(tmp_path / "src", flow=flow, tenant=True), repo)
    assert {"node_entry", "node_pack"} <= failed(result)
    assert "/naming-missing" in result["node_pack"]["detail"]


@pytest.mark.parametrize("field,value", [
    ("produces", [{"path": ".onto/flow.lock"}]),
    ("produces", [{"path": "resource/others/*"}]),
    ("writes", [".claude/commands/*"]),
])
def test_write_scope_must_not_touch_protected_dirs(tmp_path, repo, field, value):
    result = run(make_node_pack(tmp_path / "src", flow=flow_with(**{field: value}), tenant=True), repo)
    assert "write_scope" in failed(result)


@pytest.mark.parametrize("field", ["stale_on", "requires"])
def test_no_new_entity_types(tmp_path, repo, field):
    result = run(make_node_pack(tmp_path / "src", flow=flow_with(**{field: ["contract_clause"]}), tenant=True), repo)
    assert "node_pack" in failed(result)
    assert "不能新增实体类型" in result["node_pack"]["detail"]


def test_compatible_versions(tmp_path, repo):
    flow = copy.deepcopy(NODE_PACK_FLOW)
    flow["requires_flow"] = "core>=9.0"
    result = run(make_node_pack(tmp_path / "src", flow=flow, tenant=True), repo)
    assert "requires_flow" in result["node_pack"]["detail"]
    result = run(make_node_pack(tmp_path / "src2", tenant=True, kernel_api=">=2.0"), repo)
    assert "compat" in failed(result)


@pytest.mark.parametrize("extra,files", [
    ({"contributes": {"hooks": {"PreToolUse": [{"matcher": "Bash", "command": "bash .claude/scripts/naming_guard_x.sh"}]}}},
     {"scripts/naming_guard_x.sh": "exit 0\n"}),
    ({}, {"skills/naming-skill/SKILL.md": "合成 skill\n"}),
    ({"contributes": {"pages": [{"id": "scenarios"}]}}, {}),
    ({"contributes": {"artifacts": ["artifacts/note.yaml"]}}, {"artifacts/note.yaml": "type: naming.note\n"}),
    ({"contributes": {"editors": [{"id": "note"}]}}, {"ui/note/index.html": "<html></html>\n"}),
])
def test_node_pack_cannot_carry_hooks_skills_pages_artifacts_or_ui(tmp_path, repo, extra, files):
    base = {"commands/naming-check.md": "检查 $ARGUMENTS\n"}
    directory = make_node_pack(tmp_path / "src", files={**base, **files}, tenant=True, **copy.deepcopy(extra))
    assert "node_pack" in failed(run(directory, repo))


@pytest.mark.parametrize("fields,word", [
    ({"placement": "spine"}, "挂法"),
    ({"after": None}, "锚点"),
    ({"id": "other.check"}, "编号"),
    ({"done": ["unknown_probe"]}, "探针"),
    ({"kind": "script"}, "script.name"),
    ({"kind": "human"}, "decision"),
])
def test_node_declaration_rules(tmp_path, repo, fields, word):
    result = run(make_node_pack(tmp_path / "src", flow=flow_with(**fields), tenant=True), repo)
    assert "node_pack" in failed(result)
    assert word in result["node_pack"]["detail"]


def test_three_node_kinds_with_their_declarations_pass(tmp_path, repo):
    flow = copy.deepcopy(NODE_PACK_FLOW)
    flow["probes"].update({
        "naming_lint": {"files": ["reports"], "glob": "naming-lint.md"},
        "naming_signoff": {"files": ["reports"], "glob": "naming-signoff.md"},
    })
    flow["nodes"] += [
        {"id": "naming.lint", "title": "命名格式检查", "kind": "script", "placement": "branch", "after": "core.object",
         "done": ["naming_lint"], "produces": ["reports/naming-lint.md"],
         "script": {"name": "naming_lint.py", "inputs": ["ontology/object/*.md"], "output": "reports/naming-lint.md"}},
        {"id": "naming.signoff", "title": "命名会签", "kind": "human", "placement": "gate", "after": "naming.check",
         "done": ["naming_signoff"], "produces": ["reports/naming-signoff.md"],
         "decision": {"title": "线下会签结果", "options": [{"id": "pass", "label": "已签署"}, {"id": "fail", "label": "未通过"}],
                      "record": "reports/naming-signoff.md"}},
    ]
    files = {"commands/naming-check.md": "检查 $ARGUMENTS\n", "scripts/naming_lint.py": "print('{}')\n"}
    result = run(make_node_pack(tmp_path / "src", flow=flow, files=files, tenant=True), repo)
    assert not failed(result), result
