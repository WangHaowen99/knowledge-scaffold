"""主流程单一定义（方案 D12、D14；详设 6.1、P1 验收「阶段定义只剩一处」）。

原来 5 处各写一份阶段：后端 STAGES、state_helper、session_start_hook、/ontology 的下一步、前端
LeftPane/ChatPane。现在都读 core 包的 flow.yaml：这里逐处核对它们给出的结果与 flow.yaml 一致，
并核对统一后的「质量检查完成」口径。前端两处改读 GET /flow（见 _plan/frontend-progress.md）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.main import app
from app.services import auth
from app.services.flow import assemble, model as flow_model
from app.services.packs import builtin
from app.services.session_host import injection
from app.services.workspace import WorkspaceManager

TOOLKIT = Path(config.ASSETS_DIR) / "workspace-toolkit"
FLOW = yaml.safe_load((TOOLKIT / "flow.yaml").read_text(encoding="utf-8"))
sys.path.insert(0, str(TOOLKIT / "scripts"))
import flow_state  # noqa: E402

PACKS = Path(config.ASSETS_DIR) / "packs"
# 拆包后（方案 D7 后半）命令分在 core 与各可选包里
COMMANDS = {path.stem for path in (TOOLKIT / "commands").glob("*.md")} | {
    path.stem for path in PACKS.glob("*/commands/*.md")}
# 新建项目缺省启用的能力包里带流程节点的（知识沉淀包没有节点）
DEFAULT_FLOW_PACKS = ("codegen", "diagram", "trace")


def _default_flow() -> dict:
    contributions = [("core", "test", FLOW)]
    available = {path.stem for path in (TOOLKIT / "commands").glob("*.md")}
    for name in ("codegen", "diagram", "knowledge", "trace"):
        available |= {path.stem for path in (PACKS / name / "commands").glob("*.md")}
        if name in DEFAULT_FLOW_PACKS:
            contributions.append((name, "test", yaml.safe_load((PACKS / name / "flow.yaml").read_text(encoding="utf-8"))))
    return assemble.assemble(contributions, available_commands=available)


DEFAULT_FLOW = _default_flow()

# 场景：要建的文件 → 期望的旧版阶段、计入进度的完成数（0.3.0 起对象与操作合成「建模」一个节点，详设 6.8）
SCENARIOS = {
    "empty": ({}, "未开始", 0),
    "research_pending": ({"research/20260101-订单.md": "# 报告\n"}, "资料分析待核验", 0),
    "diagram": ({"research/r.md": "# 报告\n", "ontology/diagrams/下单.drawio": "<mxfile/>"}, "流程图就绪", 1),
    # 只有对象：建模还没做完（全量模式那一段停在「操作」）
    "objects": ({"ontology/object/订单.md": "# 订单\n"}, "对象已提取", 0),
    "operations": ({"ontology/object/订单.md": "# 订单\n", "ontology/operation/下单.md": "# 下单\n"}, "逻辑已提取", 1),
    # 只有场景验证报告：不算质量检查完成（统一口径前后端会算完成）
    "trace_report_only": ({"ontology/object/a.md": "x", "ontology/operation/b.md": "x",
                           "reports/20260101-000000-trace.md": "- [x] 通过\n"}, "逻辑已提取", 1),
    "validated": ({"ontology/object/a.md": "x", "ontology/operation/b.md": "x",
                   "reports/20260101-000000-all-rule.md": "- [x] 已修\n"}, "完成", 2),
    "validate_pending": ({"ontology/object/a.md": "x", "ontology/operation/b.md": "x",
                          "reports/20260101-000000-all-rule.md": "- [ ] 待修\n"}, "校验待审阅", 1),
    # 建模、流程图、校验、全量回归、代码生成；全局一致性、发布还没做
    "full": ({"ontology/object/a.md": "x", "ontology/operation/b.md": "x", "ontology/rule/r.md": "x",
              "reports/20260101-000000-all-semantic.md": "ok\n", "ontology/data-mock/m.md": "x",
              "ontology/testcase/t.md": "x", "ontology/scripts/s.py": "x", "ontology/diagrams/d.drawio": "x"},
             "完成", 5),
}


@pytest.fixture(autouse=True)
def _fresh_builtin():
    builtin.clear_cache()
    yield
    builtin.clear_cache()


@pytest.fixture()
def manager(tmp_path: Path) -> WorkspaceManager:
    return WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))


def make_project(manager: WorkspaceManager, files: dict[str, str]) -> Path:
    root = manager.project_root(manager.create_project("流程")["id"])
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def state_helper(root: Path, *args: str) -> str:
    proc = subprocess.run([sys.executable, str(root / ".claude" / "scripts" / "state_helper.py"), *args],
                          cwd=root, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


# ---- 定义本身 ----


def test_flow_definition_assembles_and_references_real_commands():
    flow = assemble.assemble([("core", "test", FLOW)])
    ids = [node["id"] for node in flow["nodes"]]
    assert ids[0] == "core.research" and len(ids) == len(set(ids))
    referenced = set()
    for node in FLOW["nodes"]:
        for action in [*(node.get("actions") or []), *(node.get("steps") or [])]:
            if str(action.get("command") or "").startswith("/"):
                referenced.add(action["command"].split()[0][1:])
        if node.get("entry"):
            referenced.add(node["entry"]["command"][1:])
    for items in FLOW["next_steps"].values():
        referenced |= {item["command"].split()[0][1:] for item in items if item.get("command")}
    referenced |= {f"ontology-{name}" for name in FLOW["commands"]}
    assert referenced - COMMANDS == set(), "flow.yaml 引用了不存在的命令"
    # 8 个旧版阶段名是命令模板按字面量分支用的，不能改
    assert flow_state.stage_names(FLOW) == [
        "未开始", "资料分析待核验", "资料分析完成", "流程图就绪", "对象已提取", "逻辑已提取", "校验待审阅", "完成",
    ]


def test_assembled_flow_is_json_readable_by_stdlib(manager: WorkspaceManager):
    root = make_project(manager, {})
    text = (root / ".claude" / "flow.yaml").read_text(encoding="utf-8")
    assembled = json.loads(text)
    assert yaml.safe_load(text) == assembled  # 也是合法 YAML
    assert [node["id"] for node in assembled["nodes"]] == [node["id"] for node in DEFAULT_FLOW["nodes"]]
    assert assembled["flow_hash"] == json.loads((root / ".claude" / "MANIFEST.json").read_text())["flow_hash"]


# ---- 5 处取同一份 ----


@pytest.mark.parametrize("scenario", SCENARIOS, ids=list(SCENARIOS))
def test_backend_state_helper_and_api_agree(manager: WorkspaceManager, scenario: str, monkeypatch):
    files, stage, done = SCENARIOS[scenario]
    root = make_project(manager, files)
    # ① 后端项目进度
    progress = manager._project_progress(root)
    assert (progress["stages_done"], progress["stages_total"]) == (done, len(flow_state.progress_nodes(DEFAULT_FLOW)))
    # ② state_helper（工作区里实际跑的那份）
    assert state_helper(root, "detect-stage") == stage
    # ③ 会话开始的阶段提示（宿主注入与 session_start_hook 都经 detect-stage）
    assert injection.stage_hint(root).startswith(f"本体项目当前阶段：{stage}。")
    # ④ /ontology 的下一步建议条目
    assert state_helper(root, "next-step").splitlines() == flow_state.next_step_lines(DEFAULT_FLOW, stage)
    # ⑤ GET /flow（前端进度条与建议条改读它）
    monkeypatch.setattr(deps, "workspaces", manager)
    monkeypatch.setattr(auth, "ENABLED", False)
    body = TestClient(app).get(f"/api/v1/projects/{root.name}/flow").json()
    assert body["stage"] == stage
    assert body["progress"] == {"done": done, "total": progress["stages_total"]}
    assert [node["id"] for node in body["nodes"]] == [node["id"] for node in DEFAULT_FLOW["nodes"]]
    confirmed = {node["id"] for node in body["nodes"] if node["state"] == "confirmed"}
    hits = flow_state.node_hits(DEFAULT_FLOW, root)
    assert confirmed == {node_id for node_id, hit in hits.items() if hit}


@pytest.mark.parametrize("command, expected", [
    ("extract-object", "missing:资料分析完成"),
    ("validate", "missing:逻辑已提取,对象已提取"),
    ("research", "ok"),
    ("unknown-command", "ok"),
])
def test_check_prereq_reads_flow(manager: WorkspaceManager, command, expected):
    root = make_project(manager, {})
    assert state_helper(root, "check-prereq", command) == expected


def test_state_helper_has_no_stage_tables():
    source = (TOOLKIT / "scripts" / "state_helper.py").read_text(encoding="utf-8")
    for name in ("STAGE_ORDER", "COMMAND_PREREQS", "STAGE_SORT", "STAGE_NEXT_COMMAND"):
        assert name not in source


def test_backend_has_no_stage_probes():
    source = (Path(__file__).resolve().parents[1] / "app" / "services" / "workspace" / "core.py").read_text(encoding="utf-8")
    assert "STAGES = (" not in source and "STAGE_TOTAL" not in source


def test_ontology_command_reads_next_steps_from_flow():
    doc = (TOOLKIT / "commands" / "ontology.md").read_text(encoding="utf-8")
    assert "state_helper.py next-step" in doc
    assert doc.count("{next-step}") >= len(FLOW["next_steps"])
    # 各阶段的条目不再写死在命令文档里（示例模式的引导不属于阶段定义，不在此列）
    for stage in FLOW["next_steps"]:
        for line in flow_state.next_step_lines(FLOW, stage):
            if line.startswith("- 回复"):
                assert line not in doc, line


def test_flow_api_suggests_mainline_next_step(manager: WorkspaceManager, monkeypatch):
    root = make_project(manager, {"ontology/object/订单.md": "# 订单\n"})
    monkeypatch.setattr(deps, "workspaces", manager)
    monkeypatch.setattr(auth, "ENABLED", False)
    client = TestClient(app)
    body = client.get(f"/api/v1/projects/{root.name}/flow").json()
    assert body["suggestions"]["primary"]["action"]["command"] == "/ontology-research"
    assert len(body["suggestions"]["secondary"]) <= 2 and body["suggestions"]["all_commands"] is True
    assert client.get(f"/api/v1/projects/{root.name}/flow/suggestions").json() == body["suggestions"]
    sections = {section["id"]: section["nodes"] for section in body["sections"]}
    assert "core.update" in sections["on_demand"] and "diagram.flowchart" in sections["optional"]
    assert body["commands"]["extract-object"] == "core.model"


def test_running_command_marks_node_running(manager: WorkspaceManager, monkeypatch):
    root = make_project(manager, {"research/r.md": "# 报告\n"})
    (root / ".ontology-state").mkdir(exist_ok=True)
    (root / ".ontology-state" / "lock-extract-object.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(deps, "workspaces", manager)
    monkeypatch.setattr(auth, "ENABLED", False)
    body = TestClient(app).get(f"/api/v1/projects/{root.name}/flow").json()
    assert next(node for node in body["nodes"] if node["id"] == "core.model")["state"] == "running"


def test_flow_follows_project_locked_core_version(manager: WorkspaceManager):
    root = make_project(manager, {})
    assert flow_model.load_for(root)["flow_hash"] == json.loads((root / ".claude" / "flow.yaml").read_text())["flow_hash"]
    (root / ".claude").unlink()
    assert flow_model.load_for(root) == flow_model.image_flow()

