"""求解契约静态检查、结构化读写与发布门禁（需求 PR2）。

覆盖：五个稳定错误码、评估器的 Consumer 兼容层接口（符号、模块常量、SOLVER_CONTEXT 键与提示）、
声明式评估器（与旧式并列发现、错误码映射、引用对象文档计入修订）、
普通包无额外门禁、未验证/非权威/过期验证阻止发布、
面板保存的修订冲突与结构拒绝、受管目录整体替换、输入 Schema 的消费平台口径、
模板化契约的生成与受管判定、导出时受管输入 Schema 的可选值投影。fixture 全部为合成数据。
"""

import asyncio
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app.services import solver_contract, solver_validation
from app.services.workspace import WorkspaceError, WorkspaceManager, _git
from tests.test_solver_evaluator_contract import (
    FACTORS,
    LOOKUP_OBJECT,
    OBJECTS,
    OMIT,
    TABLE_SOURCES,
    declarative_source,
    lookup_source,
    object_docs,
    with_fields,
)

# RFC 5737 文档保留地址（TEST-NET-1），运行时拼出，避免在源码里出现 IP 字面量
SYNTHETIC_IP = ".".join(("192", "0", "2", "10"))
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


def _runtime_binding_findings(findings: list[dict]) -> list[dict]:
    return [item for item in findings if item["code"] == solver_contract.CODE_RUNTIME_BINDING_FORBIDDEN]


@pytest.mark.parametrize(
    "extra, field, value",
    [
        ("solver_endpoint: synthetic-value-a\n", "solver_endpoint", "synthetic-value-a"),
        ("providerRevision: synthetic-value-b\n", "providerRevision", "synthetic-value-b"),
        ("db_password: synthetic-value-c\n", "db_password", "synthetic-value-c"),
        ("notes:\n  callback_url: synthetic-value-d\n", "notes.callback_url", "synthetic-value-d"),
        ("description: 详见 https://solver.example.com/docs\n", "description", "solver.example.com"),
        (f'fallback: "{SYNTHETIC_IP}:8443"\n', "fallback", SYNTHETIC_IP),
        ("auth_header: Bearer synthetic-value-e\n", "auth_header", "synthetic-value-e"),
    ],
)
def test_runtime_binding_in_contract_is_forbidden(
    manager: WorkspaceManager, extra: str, field: str, value: str
):
    """键名（含驼峰、嵌套）与取值形态（服务地址、主机端口、Bearer 凭据）都拦下，报错不回显取值。"""
    pid, root = _with_contract(manager)
    path = root / solver_contract.SOLVER_DIR / "plan_route" / "model.yaml"
    path.write_text(MODEL_YAML + extra, encoding="utf-8")
    [finding] = _runtime_binding_findings(manager.solver_release_status(pid)["findings"])
    assert finding["field"] == field
    assert finding["path"].endswith("plan_route/model.yaml")
    assert value not in finding["message"]


def test_business_terms_and_schema_dialect_are_not_runtime_binding(manager: WorkspaceManager):
    """purity、security_level 含 uri 字样但不是地址键；$schema 的方言声明不是服务地址。"""
    pid, root = _with_contract(manager)
    directory = root / solver_contract.SOLVER_DIR / "plan_route"
    (directory / "model.yaml").write_text(
        MODEL_YAML + "labels: {purity: 0.9, security_level: 高}\n", encoding="utf-8"
    )
    schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", **INPUT_SCHEMA_DOC}
    (directory / "input.schema.json").write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
    assert _runtime_binding_findings(manager.solver_release_status(pid)["findings"]) == []


@pytest.mark.parametrize(
    "line, field, value",
    [
        ('SOLVER_URL = "synthetic-value-f"\n', "SOLVER_URL", "synthetic-value-f"),
        ('REPORT_LINK = "https://solver.example.com/report"\n', "REPORT_LINK", "solver.example.com"),
        ('HEADERS = {"Authorization": "synthetic-value-g"}\n', "HEADERS", "synthetic-value-g"),
    ],
)
def test_runtime_binding_in_evaluator_is_forbidden(
    manager: WorkspaceManager, line: str, field: str, value: str
):
    """评估器随包发布：源码里的地址常量、凭据键与服务地址字面量同样拦下。"""
    [finding] = _runtime_binding_findings(_evaluator_findings(manager, EVALUATOR_PY + line))
    assert finding["path"] == "ontology/scripts/route_eval.py"
    assert finding["field"] == field
    assert value not in finding["message"]


def test_contract_digests_follow_semantics_not_formatting(manager: WorkspaceManager):
    pid, root = _with_contract(manager)
    model = root / solver_contract.SOLVER_DIR / "plan_route" / "model.yaml"
    before = manager.solver_release_status(pid)["digests"]
    assert before["algorithm"] == "sha256:canonical-json"
    assert set(before["operations"]) == {"plan_route"}
    model.write_text(
        yaml.safe_dump(yaml.safe_load(MODEL_YAML), allow_unicode=True, sort_keys=True, indent=4),
        encoding="utf-8",
    )
    assert manager.solver_release_status(pid)["digests"] == before, "只改键序与缩进不改变摘要"
    model.write_text(MODEL_YAML.replace("OPTION_0: 路线甲", "OPTION_0: 路线丙"), encoding="utf-8")
    after = manager.solver_release_status(pid)["digests"]
    old, new = before["operations"]["plan_route"], after["operations"]["plan_route"]
    assert {key for key in new if new[key] != old[key]} == {"decision_model", "total"}
    assert after["total"] != before["total"] and after["package"] == before["package"]


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


# ---- 声明式评估器（/ontology-solver-evaluator 生成）----


def _declarative_contract(
    manager: WorkspaceManager, source: str | None = None, **objects: dict[str, str]
) -> tuple[str, Path]:
    """声明式评估器及其引用的对象文档；objects 按对象覆盖 {属性: 类型}。"""
    pid, root = _project(manager, evaluator=declarative_source() if source is None else source)
    for name, text in object_docs(**objects).items():
        (root / "ontology" / "object" / f"{name}.md").write_text(text, encoding="utf-8")
    manager.save_solver_contract(pid, [_item()], "")
    return pid, root


def test_declarative_evaluator_only_waits_for_validation(manager: WorkspaceManager):
    pid, root = _declarative_contract(manager)
    scripts = root / "ontology" / "scripts"
    (scripts / "helper.py").write_text('"""辅助脚本（合成示例）"""\nVALUE = 1\n', encoding="utf-8")
    status = manager.solver_release_status(pid)
    assert status["findings"] == []  # 非评估器脚本不参与发现
    assert status["operations"][0]["evaluator"] == "ontology/scripts/route_eval.py"
    assert _codes(status) == {solver_contract.CODE_VALIDATION_REQUIRED}
    assert solver_contract.evaluator_unit_options(scripts / "route_eval.py") == ["UNIT_1", "UNIT_2"]


def test_declarative_placeholder_blocks_release(manager: WorkspaceManager):
    pid, _root = _declarative_contract(manager, declarative_source(VGO_UNIT_ID="<待填写：二次加工单元 ID>"))
    [finding] = manager.solver_release_status(pid)["findings"]
    assert finding["code"] == solver_contract.CODE_INVALID
    assert finding["path"] == "ontology/scripts/route_eval.py" and finding["field"] == "VGO_UNIT_ID"
    assert "未填写的模板占位符" in finding["message"]
    assert finding["hint"] == solver_contract.EVALUATOR_DECLARATIVE_HINT


def test_declarative_problem_kinds_map_to_stable_codes(manager: WorkspaceManager):
    source = declarative_source(TABLE_SOURCES={
        **with_fields("logic_tank", {"LowSafetyThrd_Tons": "missing_attr"}),
        "energy_consume": {**TABLE_SOURCES["energy_consume"], "object": "MissingObject"},
    })
    pid, _root = _declarative_contract(manager, source)
    findings = manager.solver_release_status(pid)["findings"]
    assert [f["code"] for f in findings] == [solver_contract.CODE_REFERENCE_MISSING] * 2

    pid, _root = _declarative_contract(manager, declarative_source(SOLVER_CONTEXT=OMIT))
    [finding] = manager.solver_release_status(pid)["findings"]
    assert finding["code"] == solver_contract.CODE_CONTEXT_MISSING and finding["field"] == "SOLVER_CONTEXT"


def test_declarative_numeric_id_attribute_blocks_release(manager: WorkspaceManager):
    pid, _root = _declarative_contract(manager, StorageTank={**OBJECTS["StorageTank"], "tank_id": "整数"})
    [finding] = manager.solver_release_status(pid)["findings"]
    assert finding["code"] == solver_contract.CODE_INVALID
    assert finding["field"] == "TABLE_SOURCES.logic_tank.fields['LogicTankID']" and "前导零" in finding["message"]


def test_legacy_and_declarative_evaluators_are_ambiguous(manager: WorkspaceManager):
    pid, root = _with_contract(manager)
    (root / "ontology" / "scripts" / "plan_route_evaluator.py").write_text(declarative_source(), encoding="utf-8")
    [finding] = manager.solver_release_status(pid)["findings"]
    assert finding["code"] == solver_contract.CODE_INVALID and "不唯一" in finding["message"]


@pytest.mark.parametrize(
    "name, stale", [("StorageTank", True), ("StreamLink", True), ("OtherObject", False)]
)
def test_referenced_object_edit_makes_declarative_validation_stale(
    manager: WorkspaceManager, name: str, stale: bool
):
    # StreamLink 只经 lookup 引用，同样计入修订
    pid, root = _declarative_contract(manager, lookup_source(), StreamLink=LOOKUP_OBJECT)
    _accept(root, manager.solver_release_status(pid)["revision"])
    assert manager.solver_release_status(pid)["blocking"] == 0
    doc = root / "ontology" / "object" / f"{name}.md"
    text = doc.read_text(encoding="utf-8") if doc.exists() else f"# {name}\n"
    doc.write_text(text + "\n", encoding="utf-8")
    expected = {solver_contract.CODE_VALIDATION_STALE} if stale else set()
    assert _codes(manager.solver_release_status(pid)) == expected


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
    message = _git(root, "log", "-1", "--format=%s").strip()
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
        (_item(endpoint="https://solver.example.com"), solver_contract.CODE_RUNTIME_BINDING_FORBIDDEN),
        (_item(providerRevision="synthetic-rev"), solver_contract.CODE_RUNTIME_BINDING_FORBIDDEN),
        (_item(operation_frontmatter={"callback_url": "synthetic-value"}),
         solver_contract.CODE_RUNTIME_BINDING_FORBIDDEN),
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


def test_text_presentation_is_rendered_only_when_named():
    base = {"display_mapping": {"OPTION_0": "路线甲", "OPTION_1": "路线乙"}, "title": "", "evaluator_path": ""}
    blank = solver_contract.render_template({**base, "process_name": "", "stream_name": ""})
    # 都留空时与加入这两个参数之前逐字节一致，已保存的契约仍受管
    assert blank == solver_contract.render_template(base)
    assert "text_presentation" not in yaml.safe_load(blank["entry"])

    params = {**base, "process_name": "工序甲", "stream_name": "物流甲"}
    rendered = solver_contract.render_template(params)
    model = yaml.safe_load(rendered["entry"])
    assert model["text_presentation"] == {
        "kind": "ordered_route_plan_zh.v1", "process_name": "工序甲", "stream_name": "物流甲",
    }
    assert solver_contract._check_ordered_route_model(model, "model.yaml") == []
    assert solver_contract.extract_template_params(rendered) == params
    assert solver_contract.render_template(solver_contract.extract_template_params(rendered)) == rendered


_TEXT = {"kind": "ordered_route_plan_zh.v1", "process_name": "工序甲", "stream_name": "物流甲"}


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ("工序甲", "不允许其他字段"),
        ({"kind": _TEXT["kind"], "process_name": "工序甲"}, "不允许其他字段"),
        ({**_TEXT, "unit": "工序乙"}, "不允许其他字段"),
        ({**_TEXT, "kind": "plan_text.v2"}, "不允许其他字段"),
        ({**_TEXT, "stream_name": ""}, "须同时填写"),
        ({**_TEXT, "process_name": " 工序甲"}, "首尾不能有空白"),
        ({**_TEXT, "process_name": 3}, "须同时填写"),
        ({**_TEXT, "process_name": "工序\n甲"}, "控制字符"),
        ({**_TEXT, "stream_name": "甲" * 129}, "128"),
    ],
)
def test_text_presentation_follows_consumer_rules(value, text: str):
    # 不合法时 consumer 拒收整个求解结果，须在发布前拦下
    rendered = solver_contract.render_template({"display_mapping": {"OPTION_0": "路线甲", "OPTION_1": "路线乙"}})
    model = {**yaml.safe_load(rendered["entry"]), "text_presentation": value}
    [problem] = solver_contract._check_ordered_route_model(model, "model.yaml")
    assert problem.code == solver_contract.CODE_INVALID and problem.field == "text_presentation"
    assert text in str(problem)


def test_saved_template_contract_is_managed(manager: WorkspaceManager):
    pid, root = _project(manager)
    item = _template_item(title="  路线方案 ")
    item["sidecars"] = {"entry": "ignored: true\n"}  # 模板条目以参数为准，忽略同时提交的原文
    saved = manager.save_solver_contract(pid, [item], "")
    assert saved["findings"] == []
    directory = root / solver_contract.SOLVER_DIR / "plan_route"
    params = {"display_mapping": {"OPTION_0": "路线甲", "OPTION_1": "路线乙"}, "title": "路线方案",
              "evaluator_path": "", "process_name": "", "stream_name": ""}
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
        "process_name": "", "stream_name": "",
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
        ({"process_name": ["工序"]}, "process_name"),
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


# ---- 导出时按评估器给受管输入 Schema 补可选值 ----


def _crude_description(schema: dict) -> str:
    return schema["properties"]["items"]["items"]["properties"]["crude_origin"]["description"]


def test_export_projects_evaluator_options_into_managed_input_schema(
    manager: WorkspaceManager, tmp_path: Path
):
    pid, root = _project(manager, evaluator=declarative_source())
    for name, text in object_docs().items():
        (root / "ontology" / "object" / f"{name}.md").write_text(text, encoding="utf-8")
    manager.save_solver_contract(pid, [_template_item()], "")
    workspace_schema = root / solver_contract.SOLVER_DIR / "plan_route" / "input.schema.json"
    template = workspace_schema.read_bytes()
    member = f"{pid}/operations/plan_route.input.schema.json"

    package = manager.export_package_zip(pid, tmp_path / "out.zip")
    with zipfile.ZipFile(package) as archive:
        exported = archive.read(member)
    schema = json.loads(exported)
    assert schema["properties"]["diesel_unit_no"]["enum"] == ["UNIT_1", "UNIT_2"]
    # enum 挡不住 Agent 把可选值之外的装置就近换成可选值，描述要求它先问用户
    assert schema["properties"]["diesel_unit_no"]["description"].endswith("先向用户确认")
    assert _crude_description(schema) == (
        "原油品种，须是评估器有修正系数的品种之一：source-a、source-b"
        "；用户给出的品种与这些品种对应不上时，不要改用其他品种，先向用户确认"
    )
    # 原油品种由 consumer 按规范化名称匹配，逐字比较的 enum 会拒掉等价写法
    assert "enum" not in schema["properties"]["items"]["items"]["properties"]["crude_origin"]
    assert solver_contract._check_ordered_route_input(schema, "input.schema.json") == []
    assert workspace_schema.read_bytes() == template  # 受管判定与修订摘要都按工作区字节

    # 回导还原成模板字节，契约仍受管；再导出得到同一份 Schema
    again = manager.import_package_zip("回导任务", package)
    restored = manager.project_root(again["id"]) / solver_contract.SOLVER_DIR / "plan_route"
    assert (restored / "input.schema.json").read_bytes() == template
    [operation] = manager.solver_contract_view(again["id"])["contract"]["operations"]
    assert operation["managed"] is True
    with zipfile.ZipFile(manager.export_package_zip(again["id"], tmp_path / "again.zip")) as archive:
        assert archive.read(f"{again['id']}/operations/plan_route.input.schema.json") == exported


def test_input_schema_projection_only_touches_the_template(tmp_path: Path):
    template = solver_contract.render_template({})["input_schema"]
    evaluator = tmp_path / "route_eval.py"
    evaluator.write_text(declarative_source(), encoding="utf-8")
    projected = solver_contract.project_input_schema(template, evaluator)
    assert projected != template
    assert solver_contract.restore_template_input_schema(projected) == template

    # 手写 Schema、取不到评估器时原样保留
    custom = INPUT_SCHEMA.encode("utf-8")
    assert solver_contract.project_input_schema(custom, evaluator) == custom
    assert solver_contract.restore_template_input_schema(custom) == custom
    assert solver_contract.project_input_schema(template, None) == template

    # 改过的投影不再是模板产物，回导时原样保留
    edited = projected.replace(b"UNIT_2", b"UNIT_9")
    assert solver_contract.restore_template_input_schema(edited) == edited
    assert solver_contract.restore_template_input_schema(b"\xff") == b"\xff"


def test_oversized_source_list_is_not_projected(tmp_path: Path):
    factors = {f"source-{index}": FACTORS for index in range(solver_contract.MAX_PROJECTED_OPTIONS + 1)}
    evaluator = tmp_path / "route_eval.py"
    evaluator.write_text(declarative_source(CORRECTION_FACTORS=factors), encoding="utf-8")
    template = solver_contract.render_template({})["input_schema"]
    schema = json.loads(solver_contract.project_input_schema(template, evaluator))
    assert schema["properties"]["diesel_unit_no"]["enum"] == ["UNIT_1", "UNIT_2"]
    assert _crude_description(schema) == "原油品种"


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
    message = _git(root, "log", "-1", "--format=%s").strip()
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
