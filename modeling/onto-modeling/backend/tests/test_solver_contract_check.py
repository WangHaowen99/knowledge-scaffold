"""求解契约静态检查、结构化读写与发布门禁（需求 PR2）。

覆盖：五个稳定错误码、评估器的 Consumer 兼容层接口（符号、模块常量、SOLVER_CONTEXT 键与提示）、
普通包无额外门禁、未验证/非权威/过期验证阻止发布、
面板保存的修订冲突与结构拒绝、受管目录整体替换、输入 Schema 的消费平台口径、
模板化契约的生成与受管判定。fixture 全部为合成数据。
"""

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app.services import solver_contract, solver_validation
from app.services.workspace import WorkspaceError, WorkspaceManager

MODEL_YAML = """id: route_plan
profile_id: ordered_binary_route.v1
required_facts:
  - route_metrics
  - refinery_route_context
objectives:
  - {metric: primary_score, sense: maximize}
  - {metric: transition_count, sense: minimize}
  - {metric: safety_margin, sense: maximize}
  - {metric: candidate_rank, sense: minimize}
display_mapping:
  OPTION_0: 路线甲
  OPTION_1: 路线乙
fact_source:
  kind: external_snapshot
  fail_closed: true
"""
OBJECT_SCHEMA = '{"type": "object", "properties": {}}\n'
TEXT = {"type": "string"}
INPUT_SCHEMA_DOC = {
    "type": "object",
    "additionalProperties": False,
    "required": ["plan_month", "items"],
    "properties": {
        "plan_month": TEXT,
        "diesel_unit_no": TEXT,
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "item_order": {"type": "integer"},
                    "item_id": TEXT,
                    "crude_origin": TEXT,
                    "quantity_wan_tons": TEXT,
                    "process_days": TEXT,
                },
            },
        },
    },
}
INPUT_SCHEMA = json.dumps(INPUT_SCHEMA_DOC, ensure_ascii=False) + "\n"
# Consumer 兼容层要求的 SOLVER_CONTEXT 必填键（atmospheric_product_tanks 可省略），取值均为合成占位
CONTEXT_PY = '''SOLVER_CONTEXT = {
    "option_routes": {"OPTION_0": "route-a", "OPTION_1": "route-b"},
    "route_stream_products": ["stream-a"],
    "special_tanks": {"diesel": "tank-a", "vgo": "tank-b"},
    "unit_capacity_keys": {
        "atmospheric": "cap-a", "diesel_by_unit": {"U1": "cap-b"}, "vgo": "cap-c",
    },
    "correction_factor_prefix_by_unit": {"U1": "factor-a"},
    "production_condition": "condition-a",
}
'''
EVALUATOR_PY = '''"""路线评估器（合成示例：只含 Consumer 兼容层接口的桩）"""

''' + CONTEXT_PY + '''ATMOS_UNIT_ID = "unit-a"
DIESEL_UNIT_IDS = {"U1": "unit-b"}
DEFAULT_DIESEL_UNIT_ID = "unit-b"
VGO_UNIT_ID = "unit-c"
YIELD_ATMOS = "yield-a"
YIELD_DIESEL_WITH_VGO = "yield-b"
YIELD_DIESEL_WITHOUT_VGO = "yield-c"
YIELD_VGO_WITH_VGO = "yield-d"
YIELD_VGO_WITHOUT_VGO = "yield-e"
ATMOS_DIESEL_FEED = ("stream-a",)
ATMOS_HC_FEED = ("stream-b",)
VGO_INTERMEDIATE_NAMES = ("stream-c",)
CORRECTION_FACTORS = {}
RULE_DAILY_CAPACITIES = {}


class BatchPlan:
    pass


def build_report(tables, plans, plan_month):
    return {"cases": []}


def load_tables():
    return {}


def normalize_crude_name(name):
    return name
'''


@pytest.fixture()
def manager(tmp_path: Path) -> WorkspaceManager:
    assets = tmp_path / "assets"
    (assets / "workspace-toolkit" / "templates").mkdir(parents=True)
    (assets / "workspace-rules.md").write_text("# rules", encoding="utf-8")
    (assets / "resource-template" / "others").mkdir(parents=True)
    return WorkspaceManager(tmp_path / "data", assets)


def _sidecars(model: str = MODEL_YAML) -> dict[str, str]:
    return {
        "entry": model,
        "input_schema": INPUT_SCHEMA,
        "result_schema": OBJECT_SCHEMA,
        "fact_snapshot_schema": OBJECT_SCHEMA,
        "presentation": "columns: []\n",
    }


def _item(solver_key: str = "plan_route", operation: str = "规划路线", **binding) -> dict:
    return {
        "binding": {
            "solver_key": solver_key,
            "operation": operation,
            "decision_model": "route_plan",
            "declaration": {"engine_requirements": {"problem_family": "candidate_selection.v1"}},
            "operation_frontmatter": {},
            "units": [{"field": "distance", "unit": "km"}],
            **binding,
        },
        "sidecars": _sidecars(),
    }


def _project(manager: WorkspaceManager, *, evaluator: str | None = EVALUATOR_PY) -> tuple[str, Path]:
    pid = manager.create_project("求解门禁")["id"]
    root = manager.project_root(pid)
    (root / "ontology" / "operation" / "规划路线.md").write_text("# 规划路线\n", encoding="utf-8")
    (root / "ontology" / "operation" / "查询站点.md").write_text("# 查询站点\n", encoding="utf-8")
    if evaluator is not None:
        (root / "ontology" / "scripts" / "route_eval.py").write_text(evaluator, encoding="utf-8")
    return pid, root


def _with_contract(manager: WorkspaceManager, **kwargs) -> tuple[str, Path]:
    pid, root = _project(manager, **kwargs)
    manager.save_solver_contract(pid, [_item()], "")
    return pid, root


def _codes(status: dict) -> set[str]:
    return {item["code"] for item in status.get("findings", []) + status.get("gate", [])}


def _accept(root: Path, revision: str, **fields) -> dict:
    record = solver_validation.create(root, revision, ["plan_route"])
    return solver_validation.update(root, record["id"], **{
        "status": solver_validation.STATUS_PASSED, "authoritative": True, **fields,
    })


# ---- 普通包：无额外门禁 ----


def test_ordinary_package_has_no_solver_gate(manager: WorkspaceManager, tmp_path: Path):
    pid, _root = _project(manager, evaluator=None)
    check = manager.package_binding_check(pid)
    assert check["solver"] == {"enabled": False}
    view = manager.solver_contract_view(pid)
    assert view["enabled"] is False and view["revision"] == ""
    assert view["available_operations"] == ["查询站点", "规划路线"]
    manager.publish_package(pid, tmp_path / "plain.zip")


# ---- 静态检查：五个稳定错误码 ----


def test_valid_contract_only_waits_for_validation(manager: WorkspaceManager):
    pid, _root = _with_contract(manager)
    status = manager.solver_release_status(pid)
    assert status["findings"] == []
    assert status["revision"].startswith("sha256:")
    assert status["operations"] == [{
        "solver_key": "plan_route", "operation": "规划路线", "decision_model": "route_plan",
        "enabled": True, "evaluator": "ontology/scripts/route_eval.py",
    }]
    assert _codes(status) == {solver_contract.CODE_VALIDATION_REQUIRED}
    assert manager.package_binding_check(pid)["solver"]["blocking"] == 1


def test_unsupported_profile_is_version_incompatible(manager: WorkspaceManager):
    pid, root = _with_contract(manager)
    (root / solver_contract.SOLVER_DIR / "plan_route" / "model.yaml").write_text(
        MODEL_YAML.replace("ordered_binary_route.v1", "ordered_binary_route.v9"), encoding="utf-8"
    )
    [finding] = manager.solver_release_status(pid)["findings"]
    assert finding["code"] == solver_contract.CODE_VERSION_INCOMPATIBLE
    assert finding["field"] == "profile_id" and finding["solver_key"] == "plan_route"
    assert finding["hint"] == solver_contract.FINDING_HINTS[solver_contract.CODE_VERSION_INCOMPATIBLE]


def test_missing_evaluator_is_reference_missing(manager: WorkspaceManager):
    pid, _root = _with_contract(manager, evaluator=None)
    [finding] = manager.solver_release_status(pid)["findings"]
    assert finding["code"] == solver_contract.CODE_REFERENCE_MISSING
    assert finding["path"] == "ontology/solver/plan_route/model.yaml"
    assert finding["hint"] == solver_contract.EVALUATOR_INTERFACE_HINT


def test_missing_sidecar_is_reference_missing(manager: WorkspaceManager):
    pid, root = _with_contract(manager)
    (root / solver_contract.SOLVER_DIR / "plan_route" / "presentation.yaml").unlink()
    [finding] = manager.solver_release_status(pid)["findings"]
    assert finding["code"] == solver_contract.CODE_REFERENCE_MISSING
    assert finding["path"].endswith("plan_route/presentation.yaml")


def test_evaluator_without_solver_context_is_context_missing(manager: WorkspaceManager):
    pid, _root = _with_contract(manager, evaluator=EVALUATOR_PY.replace(CONTEXT_PY, ""))
    [finding] = manager.solver_release_status(pid)["findings"]
    assert finding["code"] == solver_contract.CODE_CONTEXT_MISSING
    assert finding["path"] == "ontology/scripts/route_eval.py"


# ---- 评估器须符合 Consumer 兼容层接口 ----


def _evaluator_findings(manager: WorkspaceManager, source: str) -> list[dict]:
    """用 evaluator_path 显式指定评估器，返回其静态检查结果。"""
    pid, root = _with_contract(manager, evaluator=source)
    (root / solver_contract.SOLVER_DIR / "plan_route" / "model.yaml").write_text(
        MODEL_YAML + "  evaluator_path: scripts/route_eval.py\n", encoding="utf-8"
    )
    return manager.solver_release_status(pid)["findings"]


def test_decision_script_is_not_a_compatible_evaluator(manager: WorkspaceManager):
    script = '''"""自行选路线的决策脚本（合成示例）"""

SOLVER_CONTEXT = {"solver_key": "plan_route", "options": ["OPTION_0", "OPTION_1"]}


def evaluate(payload, facts):
    return {"assignments": []}
'''
    findings = _evaluator_findings(manager, script)
    assert {entry["path"] for entry in findings} == {"ontology/scripts/route_eval.py"}
    symbols, constants, context = findings
    assert symbols["code"] == constants["code"] == solver_contract.CODE_VERSION_INCOMPATIBLE
    assert symbols["field"] == "BatchPlan、build_report、load_tables、normalize_crude_name"
    assert constants["field"] == "、".join(sorted(solver_contract.EVALUATOR_MODULE_CONSTANTS))
    for entry in (symbols, constants):  # 不再给「改用契约版本或 Profile」的通用提示
        assert entry["hint"] == solver_contract.EVALUATOR_INTERFACE_HINT
    assert context["code"] == solver_contract.CODE_CONTEXT_MISSING
    assert context["field"] == "SOLVER_CONTEXT"
    for key in solver_contract.EVALUATOR_CONTEXT_KEYS:
        assert key in context["message"]
    assert "SOLVER_CONTEXT" in context["hint"]


def test_missing_module_constant_is_reported(manager: WorkspaceManager):
    [finding] = _evaluator_findings(manager, EVALUATOR_PY.replace('VGO_UNIT_ID = "unit-c"\n', ""))
    assert finding["code"] == solver_contract.CODE_VERSION_INCOMPATIBLE
    assert finding["field"] == "VGO_UNIT_ID" and "模块常量" in finding["message"]
    assert finding["hint"] == solver_contract.EVALUATOR_INTERFACE_HINT


def test_annotated_assignments_are_checked_the_same(manager: WorkspaceManager):
    source = EVALUATOR_PY.replace("CORRECTION_FACTORS = {}", "CORRECTION_FACTORS: dict = {}").replace(
        "SOLVER_CONTEXT = {", "SOLVER_CONTEXT: dict = {"
    )
    assert _evaluator_findings(manager, source) == []
    [finding] = _evaluator_findings(manager, source.replace('    "production_condition": "condition-a",\n', ""))
    assert finding["message"].endswith("缺少兼容层要求的键：production_condition")


@pytest.mark.parametrize(
    "old, new, missing",
    [
        ('    "production_condition": "condition-a",\n', "", "production_condition"),
        ('    "special_tanks": {"diesel": "tank-a", "vgo": "tank-b"},\n',
         '    "special_tanks": {"diesel": "tank-a"},\n', "special_tanks.vgo"),
        ('"OPTION_1": "route-b"', '"OPTION_2": "route-b"', "option_routes.OPTION_1"),
        ('"vgo": "cap-c",', "", "unit_capacity_keys.vgo"),
    ],
)
def test_solver_context_must_carry_required_keys(
    manager: WorkspaceManager, old: str, new: str, missing: str
):
    assert old in EVALUATOR_PY
    [finding] = _evaluator_findings(manager, EVALUATOR_PY.replace(old, new))
    assert finding["code"] == solver_contract.CODE_CONTEXT_MISSING
    assert finding["field"] == "SOLVER_CONTEXT"
    assert finding["message"].endswith(f"缺少兼容层要求的键：{missing}")
    assert finding["hint"] != solver_contract.FINDING_HINTS[solver_contract.CODE_CONTEXT_MISSING]


@pytest.mark.parametrize(
    "context",
    [
        'SOLVER_CONTEXT = dict(option_routes={})\n',
        'SOLVER_CONTEXT = {**BASE_CONTEXT}\n',
        'from route_context import SOLVER_CONTEXT\n',
        'SOLVER_CONTEXT = {}\nSOLVER_CONTEXT["option_routes"] = {}\n',
        'SOLVER_CONTEXT = {}\nSOLVER_CONTEXT.update(BASE_CONTEXT)\n',
    ],
)
def test_runtime_built_solver_context_is_left_to_validation(manager: WorkspaceManager, context: str):
    """只解析不执行：运行时拼装或改写的 SOLVER_CONTEXT 无法静态判定，交给端到端验证。"""
    assert _evaluator_findings(manager, EVALUATOR_PY.replace(CONTEXT_PY, context)) == []


def test_runtime_binding_in_contract_is_forbidden(manager: WorkspaceManager):
    pid, root = _with_contract(manager)
    path = root / solver_contract.SOLVER_DIR / "plan_route" / "model.yaml"
    path.write_text(MODEL_YAML + "solver_endpoint: http://solver.example.com\n", encoding="utf-8")
    codes = _codes(manager.solver_release_status(pid))
    assert solver_contract.CODE_RUNTIME_BINDING_FORBIDDEN in codes


def test_missing_problem_family_is_reported(manager: WorkspaceManager):
    pid, _root = _project(manager)
    manager.save_solver_contract(pid, [_item(declaration={})], "")
    [finding] = manager.solver_release_status(pid)["findings"]
    assert finding["code"] == solver_contract.CODE_INVALID
    assert finding["field"] == "declaration.engine_requirements.problem_family"


# ---- 问题族与能力目录、Profile 锁定的模型字段 ----


def _requirements(**requirements) -> dict:
    return {"declaration": {"engine_requirements": requirements}}


@pytest.mark.parametrize(
    "requirements",
    [
        {"problem_family": "candidate_selection.v1",
         "capabilities": ["lexicographic_objective", "domain_candidate_evaluation"]},
        {"problem_family": "binary_ordered_route.v1"},  # 早期包的内部族名，consumer 视为同一问题族
    ],
)
def test_registered_family_and_capabilities_pass(manager: WorkspaceManager, requirements: dict):
    pid, _root = _project(manager)
    saved = manager.save_solver_contract(pid, [_item(**_requirements(**requirements))], "")
    assert saved["findings"] == []


def test_unregistered_family_lists_supported_range(manager: WorkspaceManager):
    pid, _root = _project(manager)
    saved = manager.save_solver_contract(pid, [_item(**_requirements(problem_family="vehicle_routing.v1"))], "")
    [finding] = saved["findings"]
    assert finding["code"] == solver_contract.CODE_VERSION_INCOMPATIBLE
    assert finding["field"] == "declaration.engine_requirements.problem_family"
    assert "candidate_selection.v1" in finding["message"] and finding["hint"]


def test_unsupported_capabilities_are_listed(manager: WorkspaceManager):
    pid, _root = _project(manager)
    requirements = _requirements(
        problem_family="candidate_selection.v1",
        capabilities=["lexicographic_objective", "time_windows", "column_generation"],
    )
    saved = manager.save_solver_contract(pid, [_item(**requirements)], "")
    [finding] = saved["findings"]
    assert finding["code"] == solver_contract.CODE_VERSION_INCOMPATIBLE
    assert finding["field"] == "declaration.engine_requirements.capabilities"
    assert "缺少能力：column_generation、time_windows" in finding["message"]
    assert manager.solver_release_status(pid)["blocking"] == 2  # 能力缺失与待验证同时阻断发布


def test_malformed_capabilities_are_invalid(manager: WorkspaceManager):
    pid, _root = _project(manager)
    requirements = _requirements(problem_family="candidate_selection.v1", capabilities="lexicographic_objective")
    [finding] = manager.save_solver_contract(pid, [_item(**requirements)], "")["findings"]
    assert finding["code"] == solver_contract.CODE_INVALID
    assert finding["field"] == "declaration.engine_requirements.capabilities"


def _model_findings(manager: WorkspaceManager, old: str, new: str) -> list[dict]:
    pid, root = _with_contract(manager)
    path = root / solver_contract.SOLVER_DIR / "plan_route" / "model.yaml"
    assert old in MODEL_YAML
    path.write_text(MODEL_YAML.replace(old, new), encoding="utf-8")
    findings = manager.solver_release_status(pid)["findings"]
    assert all(item["path"] == "ontology/solver/plan_route/model.yaml" for item in findings)
    return findings


@pytest.mark.parametrize(
    ("old", "new", "code", "text"),
    [
        ("  - refinery_route_context\n", "", solver_contract.CODE_INVALID, "refinery_route_context"),
        ("  - refinery_route_context\n", "  - refinery_route_context\n  - weather_forecast\n",
         solver_contract.CODE_VERSION_INCOMPATIBLE, "weather_forecast"),
        ("  - refinery_route_context\n", "  - route_metrics\n", solver_contract.CODE_INVALID, "不重复"),
        ("required_facts:\n  - route_metrics\n  - refinery_route_context\n", "",
         solver_contract.CODE_INVALID, "事实名列表"),
    ],
)
def test_required_facts_follow_profile(manager: WorkspaceManager, old, new, code, text):
    [finding] = _model_findings(manager, old, new)
    assert finding["code"] == code and finding["field"] == "required_facts"
    assert text in finding["message"]


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("  - {metric: transition_count, sense: minimize}\n  - {metric: safety_margin, sense: maximize}\n",
         "  - {metric: safety_margin, sense: maximize}\n  - {metric: transition_count, sense: minimize}\n"),
        ("  - {metric: candidate_rank, sense: minimize}\n", ""),
        ("{metric: primary_score, sense: maximize}", "{metric: primary_score, sense: minimize}"),
        ("{metric: primary_score, sense: maximize}", "{metric: primary_score, sense: maximize, weight: 2}"),
    ],
)
def test_objectives_must_match_locked_baseline(manager: WorkspaceManager, old: str, new: str):
    [finding] = _model_findings(manager, old, new)
    assert finding["code"] == solver_contract.CODE_INVALID and finding["field"] == "objectives"
    assert "primary_score maximize → transition_count minimize" in finding["message"]


@pytest.mark.parametrize(
    ("old", "new", "text"),
    [
        ("  OPTION_1: 路线乙\n", "", "OPTION_0、OPTION_1"),
        ("  OPTION_1: 路线乙\n", "  OPTION_1: 路线乙\n  OPTION_2: 路线丙\n", "OPTION_0、OPTION_1"),
        ("  OPTION_1: 路线乙\n", '  OPTION_1: " "\n', "不能为空"),
        ("  OPTION_1: 路线乙\n", "  OPTION_1: 路线甲\n", "相同"),
        ("  OPTION_1: 路线乙\n", f"  OPTION_1: {'乙' * 129}\n", "128"),
        ("  OPTION_1: 路线乙\n", '  OPTION_1: "路线\\t乙"\n', "控制字符"),
    ],
)
def test_display_mapping_covers_option_domain(manager: WorkspaceManager, old: str, new: str, text: str):
    [finding] = _model_findings(manager, old, new)
    assert finding["code"] == solver_contract.CODE_INVALID and finding["field"] == "display_mapping"
    assert text in finding["message"]


def _input_findings(manager: WorkspaceManager, schema: dict) -> list[dict]:
    pid, root = _with_contract(manager)
    path = root / solver_contract.SOLVER_DIR / "plan_route" / "input.schema.json"
    path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
    findings = manager.solver_release_status(pid)["findings"]
    assert all(
        item["path"] == "ontology/solver/plan_route/input.schema.json"
        and item["code"] == solver_contract.CODE_INVALID
        for item in findings
    )
    return findings


def _item_fields(**changes) -> dict:
    """INPUT_SCHEMA_DOC 的副本，批次字段按 changes 增删（值为 None 表示删除）。"""
    schema = json.loads(INPUT_SCHEMA)
    fields = schema["properties"]["items"]["items"]["properties"]
    for name, value in changes.items():
        if value is None:
            fields.pop(name, None)
        else:
            fields[name] = value
    return schema


@pytest.mark.parametrize(
    ("schema", "text"),
    [
        (json.loads(OBJECT_SCHEMA), "additionalProperties: false"),
        ({**INPUT_SCHEMA_DOC, "properties": {**INPUT_SCHEMA_DOC["properties"], "plan_month": {
            "type": "string", "pattern": "^[0-9]{4}-[0-9]{2}$"}}}, "不支持的关键字：pattern"),
        ({**INPUT_SCHEMA_DOC, "properties": {**INPUT_SCHEMA_DOC["properties"], "diesel_unit_no": {}}},
         "$.diesel_unit_no 须声明受支持的 type"),
        ({**INPUT_SCHEMA_DOC, "required": ["plan_month", "unknown"]}, "未声明的字段：unknown"),
        ({**INPUT_SCHEMA_DOC, "properties": {**INPUT_SCHEMA_DOC["properties"], "items": TEXT}}, "对象数组"),
        ({**INPUT_SCHEMA_DOC, "properties": {"plan_month": TEXT}, "required": []},
         "缺少字段：diesel_unit_no、items"),
        (_item_fields(process_days=None, crude_origin=None), "缺少字段：items[].crude_origin、items[].process_days"),
        # 适配器会丢弃 input_weight_tons，它不能代替批次量
        (_item_fields(quantity_wan_tons=None, input_weight_tons=TEXT), "缺少字段：items[].quantity_wan_tons"),
    ],
)
def test_input_schema_must_fit_consumer_binder(manager: WorkspaceManager, schema: dict, text: str):
    [finding] = _input_findings(manager, schema)
    assert text in finding["message"]


def test_input_schema_accepts_adapter_aliases(manager: WorkspaceManager):
    schema = _item_fields(
        crude_origin=None, quantity_wan_tons=None, process_days=None,
        source=TEXT, quantity={"type": "number", "unit": "万吨"}, duration=TEXT,
    )
    assert _input_findings(manager, schema) == []


def test_ambiguous_evaluator_requires_explicit_path(manager: WorkspaceManager):
    pid, root = _with_contract(manager)
    (root / "ontology" / "scripts" / "route_eval_copy.py").write_text(EVALUATOR_PY, encoding="utf-8")
    [finding] = manager.solver_release_status(pid)["findings"]
    assert finding["code"] == solver_contract.CODE_INVALID and "不唯一" in finding["message"]

    explicit = MODEL_YAML + "  evaluator_path: scripts/route_eval.py\n"
    view = manager.solver_contract_view(pid)
    item = view["contract"]["operations"][0]
    assert item["evaluator"] == ""  # 不唯一时面板不显示评估器链接
    item["sidecars"]["entry"] = explicit
    saved = manager.save_solver_contract(pid, [item], view["revision"])  # 回传只读字段不影响保存
    assert saved["findings"] == []
    assert saved["contract"]["operations"][0]["evaluator"] == "ontology/scripts/route_eval.py"
    assert manager.solver_release_status(pid)["operations"][0]["evaluator"] == "ontology/scripts/route_eval.py"


# ---- 验证门禁与发布 ----


def test_publish_is_blocked_until_authoritative_validation(manager: WorkspaceManager, tmp_path: Path):
    pid, root = _with_contract(manager)
    with pytest.raises(WorkspaceError) as exc:
        manager.publish_package(pid, tmp_path / "blocked.zip", allow_unbound=True)
    assert exc.value.code == solver_contract.CODE_VALIDATION_REQUIRED
    assert not (tmp_path / "blocked.zip").exists()

    revision = manager.solver_release_status(pid)["revision"]
    _accept(root, revision, authoritative=False)
    assert _codes(manager.solver_release_status(pid)) == {solver_contract.CODE_VALIDATION_FAILED}

    _accept(root, revision, candidate_count=0)  # 全不可行也是有效的权威结果
    status = manager.solver_release_status(pid)
    assert status["blocking"] == 0 and status["validation"]["status"] == "passed"
    manager.publish_package(pid, tmp_path / "ok.zip")
    assert (tmp_path / "ok.zip").is_file()


def test_failed_validation_blocks_with_reason(manager: WorkspaceManager):
    pid, root = _with_contract(manager)
    revision = manager.solver_release_status(pid)["revision"]
    record = solver_validation.create(root, revision, ["plan_route"])
    assert _codes(manager.solver_release_status(pid)) == {solver_contract.CODE_VALIDATION_REQUIRED}
    solver_validation.update(
        root, record["id"], status=solver_validation.STATUS_FAILED, message="Consumer 不可用"
    )
    [gate] = manager.solver_release_status(pid)["gate"]
    assert gate["code"] == solver_contract.CODE_VALIDATION_FAILED and "Consumer 不可用" in gate["message"]


@pytest.mark.parametrize("edit", ["model", "operation", "evaluator"])
def test_contract_edit_makes_validation_stale(manager: WorkspaceManager, edit: str):
    pid, root = _with_contract(manager)
    _accept(root, manager.solver_release_status(pid)["revision"])
    assert manager.solver_release_status(pid)["blocking"] == 0
    target = {
        "model": root / solver_contract.SOLVER_DIR / "plan_route" / "input.schema.json",
        "operation": root / "ontology" / "operation" / "规划路线.md",
        "evaluator": root / "ontology" / "scripts" / "route_eval.py",
    }[edit]
    target.write_text(target.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert _codes(manager.solver_release_status(pid)) == {solver_contract.CODE_VALIDATION_STALE}


def test_unrelated_edit_keeps_validation(manager: WorkspaceManager):
    pid, root = _with_contract(manager)
    _accept(root, manager.solver_release_status(pid)["revision"])
    (root / "ontology" / "operation" / "查询站点.md").write_text("# 查询站点\n改了\n", encoding="utf-8")
    (root / "ontology" / "object" / "站点.md").write_text("# 站点\n", encoding="utf-8")
    assert manager.solver_release_status(pid)["blocking"] == 0


def test_disabled_contract_needs_no_validation(manager: WorkspaceManager):
    pid, _root = _project(manager)
    manager.save_solver_contract(
        pid, [_item(operation_frontmatter={"execution": {"enabled": False}})], ""
    )
    status = manager.solver_release_status(pid)
    assert status["enabled"] is True and status["gate"] == [] and status["blocking"] == 0


# ---- 面板结构化保存 ----


def test_save_writes_managed_directory_and_commits(manager: WorkspaceManager):
    pid, root = _project(manager)
    saved = manager.save_solver_contract(pid, [_item()], "", author="alice")
    directory = root / solver_contract.SOLVER_DIR / "plan_route"
    assert sorted(path.name for path in directory.iterdir()) == sorted(
        ["binding.yaml", *(sidecar.filename for sidecar in solver_contract.SIDECARS)]
    )
    assert (directory / "model.yaml").read_text(encoding="utf-8") == MODEL_YAML
    binding = yaml.safe_load((directory / "binding.yaml").read_text(encoding="utf-8"))
    assert binding["units"] == [{"field": "distance", "unit": "km"}]
    assert saved["commit"] and saved["enabled"] is True
    assert saved["contract"]["operations"][0]["sidecars"]["entry"] == MODEL_YAML
    message = subprocess.run(
        ["git", "log", "-1", "--format=%s"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert message == "web: 保存求解契约 by alice"


def test_unchanged_save_keeps_bytes_and_revision(manager: WorkspaceManager):
    pid, root = _with_contract(manager)
    binding_path = root / solver_contract.SOLVER_DIR / "plan_route" / "binding.yaml"
    original = "# 手写注释保留\n" + binding_path.read_text(encoding="utf-8")
    binding_path.write_text(original, encoding="utf-8")
    view = manager.solver_contract_view(pid)
    saved = manager.save_solver_contract(pid, view["contract"]["operations"], view["revision"])
    assert binding_path.read_text(encoding="utf-8") == original
    assert saved["revision"] == view["revision"]
    again = manager.save_solver_contract(pid, saved["contract"]["operations"], saved["revision"])
    assert again["commit"] == ""  # 无改动不产生空版本点


def test_stale_base_revision_is_rejected_with_409(manager: WorkspaceManager):
    pid, root = _with_contract(manager)
    view = manager.solver_contract_view(pid)
    (root / solver_contract.SOLVER_DIR / "plan_route" / "presentation.yaml").write_text(
        "columns: [site]\n", encoding="utf-8"
    )
    with pytest.raises(WorkspaceError) as exc:
        manager.save_solver_contract(pid, view["contract"]["operations"], view["revision"])
    assert exc.value.status == 409
    assert (root / solver_contract.SOLVER_DIR / "plan_route" / "presentation.yaml").read_text(
        encoding="utf-8"
    ) == "columns: [site]\n"


@pytest.mark.parametrize(
    ("item", "code"),
    [
        (_item(solver_key="Plan-Route"), solver_contract.CODE_INVALID),
        (_item(operation="不存在"), solver_contract.CODE_REFERENCE_MISSING),
        (_item(endpoint="http://solver.example.com"), solver_contract.CODE_RUNTIME_BINDING_FORBIDDEN),
        (_item(solver_key="查询站点"), solver_contract.CODE_INVALID),
        ({"binding": {"solver_key": "plan_route"}}, solver_contract.CODE_INVALID),
    ],
)
def test_structurally_invalid_save_is_rejected(manager: WorkspaceManager, item: dict, code: str):
    pid, root = _project(manager)
    with pytest.raises(solver_contract.SolverContractRejected) as exc:
        manager.save_solver_contract(pid, [item], "")
    assert exc.value.problems[0].code == code
    assert not (root / solver_contract.SOLVER_DIR).exists()


def test_save_rejects_remote_schema_reference(manager: WorkspaceManager):
    pid, _root = _project(manager)
    item = _item()
    item["sidecars"]["input_schema"] = '{"$ref": "https://schemas.example.com/input.json"}'
    with pytest.raises(solver_contract.SolverContractRejected) as exc:
        manager.save_solver_contract(pid, [item], "")
    assert exc.value.problems[0].path.endswith("plan_route/input.schema.json")


def test_save_rejects_duplicate_operation_binding(manager: WorkspaceManager):
    pid, _root = _project(manager)
    with pytest.raises(solver_contract.SolverContractRejected):
        manager.save_solver_contract(pid, [_item(), _item(solver_key="plan_route_b")], "")


def test_save_replaces_managed_directory(manager: WorkspaceManager):
    pid, root = _with_contract(manager)
    base = root / solver_contract.SOLVER_DIR
    (base / "plan_route" / "notes.txt").write_text("stray", encoding="utf-8")
    view = manager.solver_contract_view(pid)
    renamed = _item(solver_key="route_plan_v2")
    saved = manager.save_solver_contract(pid, [renamed], view["revision"])
    assert [path.name for path in base.iterdir()] == ["route_plan_v2"]
    assert not (base / "route_plan_v2" / "notes.txt").exists()

    cleared = manager.save_solver_contract(pid, [], saved["revision"])
    assert cleared["enabled"] is False and not base.exists()
    assert cleared["commit"]
    assert manager.package_binding_check(pid)["solver"] == {"enabled": False}


def test_view_exposes_template_and_profiles(manager: WorkspaceManager):
    pid, _root = _project(manager)
    view = manager.solver_contract_view(pid)
    assert view["supported_profiles"] == ["ordered_binary_route.v1"]
    assert view["problem_families"] == [{
        "id": "candidate_selection.v1",
        "capabilities": ["domain_candidate_evaluation", "lexicographic_objective"],
        "aliases": ["binary_ordered_route.v1"],
    }]
    assert view["evaluators"] == ["ontology/scripts/route_eval.py"]
    assert view["evaluator_options"] == [{"path": "scripts/route_eval.py", "file": "ontology/scripts/route_eval.py"}]
    template = view["template"]
    assert template["declaration"]["engine_requirements"]["problem_family"] == "candidate_selection.v1"
    item = {
        "binding": {
            "solver_key": "plan_route", "operation": "规划路线", "decision_model": "route_plan",
            "declaration": template["declaration"],
        },
        "template": template["params"],
    }
    saved = manager.save_solver_contract(pid, [item], "")
    # 模板只缺业务填写的选项显示名称
    assert [(f["field"], f["message"]) for f in saved["findings"]] == [
        ("display_mapping", "display_mapping 的显示名称不能为空"),
    ]
    item["template"] = {**template["params"], "display_mapping": {"OPTION_0": "路线甲", "OPTION_1": "路线乙"}}
    assert manager.save_solver_contract(pid, [item], saved["revision"])["findings"] == []


# ---- 模板化契约：业务用户只填参数，服务端生成全部 sidecar ----


def _template_item(solver_key: str = "plan_route", operation: str = "规划路线", **params) -> dict:
    item = _item(solver_key=solver_key, operation=operation)
    del item["sidecars"]
    item["template"] = {"display_mapping": {"OPTION_0": "路线甲", "OPTION_1": "路线乙"}, **params}
    return item


def test_template_render_is_deterministic_and_profile_complete():
    params = {"display_mapping": {"OPTION_0": "路线甲", "OPTION_1": "路线乙"}, "title": "路线方案",
              "evaluator_path": "scripts/route_eval.py"}
    rendered = solver_contract.render_template(params)
    assert rendered == solver_contract.render_template(dict(params))
    assert set(rendered) == {sidecar.role for sidecar in solver_contract.SIDECARS}
    model = yaml.safe_load(rendered["entry"])
    assert model == {
        "profile_id": "ordered_binary_route.v1",
        "required_facts": list(solver_contract.ORDERED_ROUTE_REQUIRED_FACTS),
        "objectives": list(solver_contract.ORDERED_ROUTE_OBJECTIVES),
        "display_mapping": params["display_mapping"],
        "fact_source": {"kind": "external_snapshot", "fail_closed": True,
                        "evaluator_path": "scripts/route_eval.py"},
    }
    assert "id" not in model  # 模型 ID 由 binding 决定，文档里不写才不会与改名后的 ID 冲突
    input_schema = json.loads(rendered["input_schema"])
    assert input_schema["required"] == ["plan_month", "diesel_unit_no", "items"]
    assert "production_condition" not in input_schema["properties"]  # 跟随评估器的 SOLVER_CONTEXT
    assert input_schema["properties"]["items"]["items"]["required"] == [
        "item_order", "item_id", "crude_origin", "quantity_wan_tons", "process_days",
    ]
    assert solver_contract._check_ordered_route_input(input_schema, "input.schema.json") == []
    result_schema = json.loads(rendered["result_schema"])
    assert result_schema["additionalProperties"]["enum"] == ["OPTION_0", "OPTION_1"]
    assert yaml.safe_load(rendered["presentation"]) == {"title": "路线方案"}
    assert yaml.safe_load(solver_contract.render_template({})["presentation"]) == {}
    for role, data in rendered.items():
        solver_contract.parse_sidecar(data, solver_contract.SIDECAR_BY_ROLE[role].filename, role)


def test_saved_template_contract_is_managed(manager: WorkspaceManager):
    pid, root = _project(manager)
    item = _template_item(title="  路线方案 ")
    item["sidecars"] = {"entry": "ignored: true\n"}  # 模板条目以参数为准，忽略同时提交的原文
    saved = manager.save_solver_contract(pid, [item], "")
    assert saved["findings"] == []
    directory = root / solver_contract.SOLVER_DIR / "plan_route"
    params = {"display_mapping": {"OPTION_0": "路线甲", "OPTION_1": "路线乙"}, "title": "路线方案",
              "evaluator_path": ""}
    for role, data in solver_contract.render_template(params).items():
        assert (directory / solver_contract.SIDECAR_BY_ROLE[role].filename).read_bytes() == data
    [operation] = saved["contract"]["operations"]
    assert operation["managed"] is True and operation["template_params"] == params

    # 原样回传视图（含 template_params）按原文保存，不触发重新生成
    again = manager.save_solver_contract(pid, saved["contract"]["operations"], saved["revision"])
    assert again["commit"] == "" and again["revision"] == saved["revision"]


def test_manual_edit_falls_back_to_raw_mode_and_regenerates(manager: WorkspaceManager):
    pid, root = _project(manager)
    saved = manager.save_solver_contract(pid, [_template_item()], "")
    model = root / solver_contract.SOLVER_DIR / "plan_route" / "model.yaml"
    model.write_text("# 手写注释\n" + model.read_text(encoding="utf-8"), encoding="utf-8")
    view = manager.solver_contract_view(pid)
    [operation] = view["contract"]["operations"]
    assert operation["managed"] is False
    # 尽量还原的参数供「按模板重新生成」沿用
    assert operation["template_params"]["display_mapping"] == {"OPTION_0": "路线甲", "OPTION_1": "路线乙"}
    regenerated = {"binding": operation["binding"], "template": operation["template_params"]}
    again = manager.save_solver_contract(pid, [regenerated], view["revision"])
    assert again["contract"]["operations"][0]["managed"] is True
    assert again["revision"] == saved["revision"]


def test_hand_written_contract_is_not_managed(manager: WorkspaceManager):
    pid, _root = _with_contract(manager)
    [operation] = manager.solver_contract_view(pid)["contract"]["operations"]
    assert operation["managed"] is False
    assert operation["template_params"] == {
        "display_mapping": {"OPTION_0": "路线甲", "OPTION_1": "路线乙"}, "title": "", "evaluator_path": "",
    }


@pytest.mark.parametrize(
    ("template", "field"),
    [
        ("路线甲", "template"),
        ({"display_mapping": {}, "colour": "red"}, "template"),
        ({"display_mapping": {"OPTION_2": "路线丙"}}, "display_mapping"),
        ({"display_mapping": {"OPTION_0": 1}}, "display_mapping.OPTION_0"),
        ({"title": ["路线"]}, "title"),
        ({"title": "路线\n方案"}, "title"),
        ({"evaluator_path": "/srv/route_eval.py"}, "fact_source.evaluator_path"),
        ({"evaluator_path": "scripts/../route_eval.py"}, "fact_source.evaluator_path"),
    ],
)
def test_invalid_template_params_are_rejected(manager: WorkspaceManager, template, field: str):
    pid, root = _project(manager)
    item = _template_item()
    item["template"] = template
    with pytest.raises(solver_contract.SolverContractRejected) as exc:
        manager.save_solver_contract(pid, [item], "")
    [problem] = exc.value.problems
    assert problem.code == solver_contract.CODE_INVALID and problem.field == field
    assert problem.path.startswith("ontology/solver/plan_route/")
    assert not (root / solver_contract.SOLVER_DIR).exists()


def test_view_lists_evaluator_unit_options(manager: WorkspaceManager):
    pid, root = _project(manager)
    (root / "ontology" / "scripts" / "route_eval.py").write_text(
        EVALUATOR_PY + '\nDIESEL_UNIT_IDS = {"U1": "unit-a", "U2": "unit-b", 3: "skipped"}\n', encoding="utf-8"
    )
    saved = manager.save_solver_contract(pid, [_template_item()], "")
    assert saved["contract"]["operations"][0]["unit_options"] == ["U1", "U2"]


@pytest.mark.parametrize(
    "source",
    [
        EVALUATOR_PY.replace('DIESEL_UNIT_IDS = {"U1": "unit-b"}\n', ""),
        EVALUATOR_PY + "\nDIESEL_UNIT_IDS = dict(U1='unit-a')\n",  # 非字面量不执行求值
        "def broken(:\n",
    ],
)
def test_unit_options_need_a_literal_mapping(tmp_path: Path, source: str):
    path = tmp_path / "route_eval.py"
    path.write_text(source, encoding="utf-8")
    assert solver_contract.evaluator_unit_options(path) == []
    assert solver_contract.evaluator_unit_options(None) == []


# ---- 面板即时检查（不落盘）----


def _draft_entry(*replacements: tuple[str, str]) -> str:
    text = MODEL_YAML
    for old, new in replacements:
        assert old in text
        text = text.replace(old, new)
    return text


def test_draft_check_reports_model_findings_without_writing(manager: WorkspaceManager):
    pid, root = _project(manager)
    item = _item()
    item["sidecars"]["entry"] = _draft_entry(
        ("  - refinery_route_context\n", ""),
        ("  - {metric: candidate_rank, sense: minimize}\n", ""),
        ("  OPTION_1: 路线乙\n", "  OPTION_1: 路线甲\n"),
    )
    checked = manager.check_solver_contract_draft(pid, [item])
    assert checked["rejected"] == []
    assert sorted(entry["field"] for entry in checked["findings"]) == [
        "display_mapping", "objectives", "required_facts",
    ]
    assert {(entry["solver_key"], entry["path"]) for entry in checked["findings"]} == {
        ("plan_route", "ontology/solver/plan_route/model.yaml"),
    }
    assert not (root / solver_contract.SOLVER_DIR).exists()


def test_draft_check_matches_saved_release_findings(manager: WorkspaceManager):
    pid, _root = _project(manager)
    item = _item()
    item["sidecars"]["entry"] = _draft_entry(("  OPTION_1: 路线乙\n", ""))
    checked = manager.check_solver_contract_draft(pid, [item])
    saved = manager.save_solver_contract(pid, [item], "")
    assert checked == {"rejected": [], "findings": saved["findings"], "rendered": [None]}


def test_draft_check_previews_template_sidecars(manager: WorkspaceManager):
    pid, root = _project(manager)
    unnamed = _template_item(solver_key="route_b", operation="查询站点")
    unnamed["template"]["display_mapping"] = {"OPTION_0": "路线甲", "OPTION_1": " "}
    broken = _template_item(solver_key="route_c", title=3)
    checked = manager.check_solver_contract_draft(pid, [_template_item(), unnamed, broken])
    params = {"display_mapping": {"OPTION_0": "路线甲", "OPTION_1": "路线乙"}, "title": "", "evaluator_path": ""}
    assert checked["rendered"][0] == {
        role: data.decode("utf-8") for role, data in solver_contract.render_template(params).items()
    }
    assert yaml.safe_load(checked["rendered"][1]["entry"])["display_mapping"]["OPTION_1"] == ""
    assert checked["rendered"][2] is None
    assert [(entry["field"], entry["path"]) for entry in checked["rejected"]] == [
        ("title", "ontology/solver/route_c/presentation.yaml"),
    ]
    # 显示名称为空不拦保存，作为发布问题给出，与手动编辑口径一致
    assert [(entry["solver_key"], entry["field"]) for entry in checked["findings"]] == [
        ("route_b", "display_mapping"),
    ]
    assert not (root / solver_contract.SOLVER_DIR).exists()


def test_draft_check_separates_save_rejections(manager: WorkspaceManager):
    pid, root = _with_contract(manager)
    revision = manager.solver_contract_view(pid)["revision"]
    valid = _item(solver_key="route_b")
    valid["sidecars"]["entry"] = _draft_entry(("  OPTION_1: 路线乙\n", ""))
    checked = manager.check_solver_contract_draft(
        pid, [_item(solver_key="Plan-Route"), _item(solver_key="route_a", operation="不存在"), valid]
    )
    assert [(entry["code"], entry["field"]) for entry in checked["rejected"]] == [
        (solver_contract.CODE_INVALID, "solver_key"),
        (solver_contract.CODE_REFERENCE_MISSING, "operation"),
    ]
    # 结构错误不掩盖其他契约的发布问题
    assert [(entry["solver_key"], entry["field"]) for entry in checked["findings"]] == [
        ("route_b", "display_mapping"),
    ]
    assert [path.name for path in (root / solver_contract.SOLVER_DIR).iterdir()] == ["plan_route"]
    assert manager.solver_contract_view(pid)["revision"] == revision


# ---- 路由 ----


def test_routes_return_findings_and_conflict(manager: WorkspaceManager, monkeypatch):
    from app.main import workspace_error_handler
    from app.routers import projects

    monkeypatch.setattr(projects, "workspaces", manager)
    monkeypatch.setattr(projects, "ensure_access", lambda *args, **kwargs: None)
    pid, root = _project(manager)
    request = SimpleNamespace(state=SimpleNamespace(user=None), headers={})

    assert projects.get_solver_contract(pid, request)["enabled"] is False
    body = projects.SolverContractBody.model_validate(
        {"baseRevision": "", "contract": {"operations": [_item(operation="不存在")]}}
    )
    response = projects.save_solver_contract(pid, body, request)
    payload = json.loads(response.body)
    assert response.status_code == 400
    assert payload["code"] == solver_contract.CODE_REFERENCE_MISSING
    assert payload["findings"][0]["field"] == "operation"

    body = projects.SolverContractBody.model_validate(
        {"contract": {"operations": [_item()]}, "summary": "启用路线求解"}
    )
    saved = projects.save_solver_contract(pid, body, request)
    assert saved["revision"].startswith("sha256:")
    message = subprocess.run(
        ["git", "log", "-1", "--format=%s"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert message == "web: 保存求解契约：启用路线求解"
    with pytest.raises(WorkspaceError) as exc:
        projects.save_solver_contract(
            pid,
            projects.SolverContractBody(base_revision="sha256:old", contract={"operations": [_item()]}),
            request,
        )
    conflict = asyncio.run(workspace_error_handler(request, exc.value))
    assert conflict.status_code == 409
    assert json.loads(conflict.body)["code"] == "solver_contract_revision_conflict"
    assert projects.package_check(pid, request)["solver"]["enabled"] is True


def test_check_route_reports_draft_without_saving(manager: WorkspaceManager, monkeypatch):
    from app.routers import projects

    monkeypatch.setattr(projects, "workspaces", manager)
    monkeypatch.setattr(projects, "ensure_access", lambda *args, **kwargs: None)
    pid, root = _project(manager)
    request = SimpleNamespace(state=SimpleNamespace(user=None), headers={})
    item = _item()
    item["sidecars"]["entry"] = _draft_entry(("  - refinery_route_context\n", ""))

    body = projects.SolverContractBody.model_validate({"contract": {"operations": [item]}})
    checked = projects.check_solver_contract(pid, body, request)
    assert checked["rejected"] == []
    assert [entry["field"] for entry in checked["findings"]] == ["required_facts"]
    assert not (root / solver_contract.SOLVER_DIR).exists()

    with pytest.raises(WorkspaceError) as exc:
        projects.check_solver_contract(
            pid, projects.SolverContractBody.model_validate({"contract": {"operations": {}}}), request
        )
    assert exc.value.code == solver_contract.CODE_INVALID
