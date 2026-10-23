"""求解契约（operation-centric.v1）在已发布知识包与工作区之间的无损往返（需求 PR1）。

覆盖：0.1 普通包回归、0.2 求解包导入、未知字段保留、越界路径/运行时绑定/不支持版本拒绝、
导入→导出语义等价、重复导出确定性、中文 Operation 改名为 solver_key，以及 consumer
导入边界的契约校验。fixture 全部为合成数据。
"""

import importlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
import yaml

from app.services import package_archive, solver_contract
from app.services.workspace import WorkspaceError, WorkspaceManager, _git

TOOLKIT_SCRIPTS = Path(__file__).resolve().parents[2] / "assets" / "workspace-toolkit" / "scripts"
BASH = shutil.which("bash")

MODEL_YAML = b"""id: route_plan
profile_id: ordered_binary_route.v1
fact_source:
  kind: external_snapshot
  fail_closed: true
x_extension:
  note: kept verbatim
"""
INPUT_SCHEMA = b"""{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {"window": {"$ref": "#/$defs/window"}},
  "$defs": {"window": {"type": "integer", "minimum": 1}}
}
"""
RESULT_SCHEMA = b'{"type": "object", "properties": {"candidates": {"type": "array"}}}\n'
FACT_SCHEMA = b'{"type": "object", "required": ["sites"]}\n'
PRESENTATION_YAML = b"""title: Route plan
columns: [site, score]
"""
EVALUATOR_PY = '''"""路线评估器（合成示例）"""

SOLVER_CONTEXT = {"window": 3}


def load_tables(source):
    return {}
'''.encode("utf-8")
SIDECAR_BYTES = {
    "model.yaml": MODEL_YAML,
    "input.schema.json": INPUT_SCHEMA,
    "result.schema.json": RESULT_SCHEMA,
    "fact-snapshot.schema.json": FACT_SCHEMA,
    "presentation.yaml": PRESENTATION_YAML,
}
DECLARATION = {
    "id": "route_plan",
    "operation_key": "plan_route",
    "entry": "operations/plan_route.model.yaml",
    "input_schema": "operations/plan_route.input.schema.json",
    "result_schema": "operations/plan_route.result.schema.json",
    "fact_snapshot_schema": "operations/plan_route.fact-snapshot.schema.json",
    "presentation": "operations/plan_route.presentation.yaml",
    "engine_requirements": {"problem_family": "binary_ordered_route.v1"},
    "x_declared": {"owner": "modeling"},
}
OPERATION_FRONTMATTER = {
    "resource": "operation",
    "key": "plan_route",
    "title": "规划路线",
    "x_owner_note": "kept",
    "execution": {"mode": "solver_workflow", "decision_model": "route_plan", "retries": 0},
    "uses": {"objects": ["站点"]},
}


@pytest.fixture()
def manager(tmp_path: Path) -> WorkspaceManager:
    assets = tmp_path / "assets"
    (assets / "workspace-toolkit" / "templates").mkdir(parents=True)
    (assets / "workspace-rules.md").write_text("# rules", encoding="utf-8")
    (assets / "resource-template" / "others").mkdir(parents=True)
    return WorkspaceManager(tmp_path / "data", assets)


def _front(document: dict) -> str:
    return "---\n" + yaml.safe_dump(document, allow_unicode=True, sort_keys=False) + "---\n\n"


def _package_members(
    *,
    manifest_overrides: dict | None = None,
    declaration: dict | None = None,
    model: bytes = MODEL_YAML,
    frontmatter: dict | None = None,
) -> dict[str, bytes]:
    manifest = {
        "kind": "ontology-package",
        "schema_version": "0.2",
        "id": "route-demo",
        "name": "路线示例",
        "description": "合成求解包",
        "version": "0.2.1",
        "status": "released",
        "language": "zh-CN",
        "solver_layout": "operation-centric.v1",
        "capabilities": {"decision_models": [declaration or DECLARATION]},
        **(manifest_overrides or {}),
    }
    root_index = (
        _front({"resource": "root", "key": "root", "title": "路线示例"})
        + "# 路线示例\n\n<!-- solver-capabilities:v1:start -->\n"
        "| capability key | operation key | execution |\n| --- | --- | --- |\n"
        "| plan_route | plan_route | solver_workflow |\n<!-- solver-capabilities:v1:end -->\n"
    )
    operations_index = (
        _front({"resource": "operation_index", "key": "operations", "title": "操作目录"})
        + "| operation key | 标题 | execution |\n| --- | --- | --- |\n"
        "| plan_route | 规划路线 | solver_workflow |\n| 查询站点 | 查询站点 |  |\n"
    )
    members = {
        "manifest.yaml": yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False).encode(),
        "index.md": root_index.encode(),
        "objects/index.md": (
            _front({"resource": "object_index", "key": "objects", "title": "对象目录"})
            + "| object key | 标题 |\n| --- | --- |\n| 站点 | 站点 |\n"
        ).encode(),
        "objects/站点.md": (
            _front({"resource": "object", "key": "站点", "title": "站点"}) + "# 站点\n\n配送站点\n"
        ).encode(),
        "operations/index.md": operations_index.encode(),
        "operations/plan_route.md": (
            _front(frontmatter or OPERATION_FRONTMATTER) + "# 规划路线\n\n在[[站点]]间排序\n"
        ).encode(),
        "operations/查询站点.md": (
            _front({"resource": "operation", "key": "查询站点", "title": "查询站点",
                    "uses": {"objects": ["站点"]}})
            + "# 查询站点\n\n读取[[站点]]\n"
        ).encode(),
        "operations/plan_route.model.yaml": model,
        "operations/plan_route.input.schema.json": INPUT_SCHEMA,
        "operations/plan_route.result.schema.json": RESULT_SCHEMA,
        "operations/plan_route.fact-snapshot.schema.json": FACT_SCHEMA,
        "operations/plan_route.presentation.yaml": PRESENTATION_YAML,
        "scripts/route_evaluator.py": EVALUATOR_PY,
        "scripts/route_evaluator.md": (
            _front({"resource": "script", "key": "route_evaluator", "title": "路线评估器",
                    "entrypoint": "route_evaluator.py"})
            + "评估器\n"
        ).encode(),
    }
    return members


def _write_zip(path: Path, members: dict[str, bytes], prefix: str = "route-demo/") -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(prefix + name, data)
    return path


def _read_zip(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def _frontmatter(data: bytes) -> dict:
    return dict(solver_contract.read_frontmatter(data.decode("utf-8")) or {})


def _import(manager: WorkspaceManager, tmp_path: Path, **kwargs) -> tuple[dict, Path]:
    archive = _write_zip(tmp_path / "in.zip", _package_members(**kwargs))
    meta = manager.import_package_zip("路线任务", archive)
    return meta, manager.project_root(meta["id"])


# ---- 0.1 普通包回归 ----


def test_ordinary_project_still_exports_v01_without_solver_projection(
    manager: WorkspaceManager, tmp_path: Path
):
    meta = manager.create_project("普通任务")
    root = manager.project_root(meta["id"])
    (root / "ontology" / "object" / "站点.md").write_text("# 站点\n", encoding="utf-8")
    (root / "ontology" / "operation" / "查询站点.md").write_text(
        "# 查询站点\n\n读取[[站点]]\n", encoding="utf-8"
    )
    files = _read_zip(manager.export_package_zip(meta["id"], tmp_path / "out.zip"))
    pid = meta["id"]
    manifest = files[f"{pid}/manifest.yaml"].decode()
    assert "schema_version: '0.1'\n" in manifest
    assert "solver_layout" not in manifest and "capabilities" not in manifest
    index = files[f"{pid}/operations/index.md"].decode()
    assert "| operation key | 标题 | 输入 | 输出 |\n" in index
    assert "| 查询站点 | 查询站点 | 查询站点 | 查询站点 |\n" in index
    assert solver_contract.INDEX_BLOCK_START not in files[f"{pid}/index.md"].decode()
    assert not any(name.endswith((".schema.json", ".model.yaml")) for name in files)


def test_v01_package_import_has_no_solver_contract(manager: WorkspaceManager, tmp_path: Path):
    members = _package_members(manifest_overrides={"schema_version": "0.1"})
    archive = _write_zip(tmp_path / "v01.zip", members)
    summary = package_archive.inspect_archive(archive)
    assert summary["blockers"] == [] and summary["solver_operations"] == 0
    meta = manager.import_package_zip("旧包任务", archive)
    assert not (manager.project_root(meta["id"]) / solver_contract.SOLVER_DIR).exists()


# ---- 0.2 导入 ----


def test_v02_import_materializes_solver_contract_verbatim(
    manager: WorkspaceManager, tmp_path: Path
):
    archive = _write_zip(tmp_path / "in.zip", _package_members())
    summary = package_archive.inspect_archive(archive)
    assert summary["blockers"] == []
    assert summary["solver_operations"] == 1 and summary["operations"] == 2

    meta = manager.import_package_zip("路线任务", archive)
    root = manager.project_root(meta["id"])
    solver_dir = root / solver_contract.SOLVER_DIR / "plan_route"
    for filename, data in SIDECAR_BYTES.items():
        assert (solver_dir / filename).read_bytes() == data
    binding = yaml.safe_load((solver_dir / "binding.yaml").read_text(encoding="utf-8"))
    assert binding["solver_key"] == "plan_route" and binding["operation"] == "plan_route"
    assert binding["decision_model"] == "route_plan"
    # 未知扩展字段（声明与 frontmatter 两处）原样保留；路径与身份字段由布局重新生成
    assert binding["declaration"] == {
        "engine_requirements": {"problem_family": "binary_ordered_route.v1"},
        "x_declared": {"owner": "modeling"},
    }
    assert binding["operation_frontmatter"]["x_owner_note"] == "kept"
    assert binding["operation_frontmatter"]["execution"]["retries"] == 0
    assert binding["units"] == []
    operation = (root / "ontology" / "operation" / "plan_route.md").read_text(encoding="utf-8")
    assert operation.startswith("# 规划路线")
    # 导入本身已提交，工作区干净
    assert manager.versions(meta["id"])


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        (
            {"declaration": {**DECLARATION, "entry": "../outside.model.yaml"}},
            solver_contract.CODE_REFERENCE_MISSING,
        ),
        (
            {"declaration": {**DECLARATION, "input_schema": "/abs/input.schema.json"}},
            solver_contract.CODE_REFERENCE_MISSING,
        ),
        (
            {"declaration": {**DECLARATION, "result_schema": "operations/missing.json"}},
            solver_contract.CODE_REFERENCE_MISSING,
        ),
        ({"model": MODEL_YAML + b"service_endpoint: example\n"},
         solver_contract.CODE_RUNTIME_BINDING_FORBIDDEN),
        ({"model": MODEL_YAML + b'fallback: "192.0.2.10:8443"\n'},
         solver_contract.CODE_RUNTIME_BINDING_FORBIDDEN),
        ({"manifest_overrides": {"providerRevision": "synthetic-rev"}},
         solver_contract.CODE_RUNTIME_BINDING_FORBIDDEN),
        (
            {"frontmatter": {**OPERATION_FRONTMATTER, "runtime_binding": {"x": 1}}},
            solver_contract.CODE_RUNTIME_BINDING_FORBIDDEN,
        ),
        ({"manifest_overrides": {"schema_version": "0.3"}},
         solver_contract.CODE_VERSION_INCOMPATIBLE),
        ({"manifest_overrides": {"solver_layout": "operation-centric.v9"}},
         solver_contract.CODE_VERSION_INCOMPATIBLE),
        (
            {"frontmatter": {**OPERATION_FRONTMATTER,
                             "execution": {"mode": "solver_workflow", "decision_model": "nope"}}},
            solver_contract.CODE_REFERENCE_MISSING,
        ),
    ],
)
def test_invalid_solver_package_is_blocked_before_project_creation(
    manager: WorkspaceManager, tmp_path: Path, kwargs: dict, code: str
):
    archive = _write_zip(tmp_path / "bad.zip", _package_members(**kwargs))
    summary = package_archive.inspect_archive(archive)
    assert {problem["code"] for problem in summary["solver_problems"]} == {code}
    assert any(blocker.startswith("求解契约：") for blocker in summary["blockers"])
    before = len(manager.list_projects())
    with pytest.raises(WorkspaceError):
        manager.import_package_zip("坏包任务", archive)
    assert len(manager.list_projects()) == before


def test_remote_schema_reference_is_rejected():
    members = _package_members()
    members["operations/plan_route.input.schema.json"] = json.dumps(
        {"$ref": "https://schemas.example.com/input.json"}
    ).encode()
    solver = solver_contract.extract_package_solver(members)
    assert [problem.code for problem in solver.problems] == [solver_contract.CODE_INVALID]
    assert solver.workspace_files == {}


def test_orphan_decision_model_declaration_is_reported():
    second = {**DECLARATION, "id": "unused_model", "operation_key": "unused"}
    members = _package_members()
    manifest = yaml.safe_load(members["manifest.yaml"])
    manifest["capabilities"]["decision_models"].append(second)
    members["manifest.yaml"] = yaml.safe_dump(manifest, allow_unicode=True).encode()
    solver = solver_contract.extract_package_solver(members)
    assert [(p.code, p.field) for p in solver.problems] == [
        (solver_contract.CODE_REFERENCE_MISSING, "capabilities.decision_models")
    ]
    assert "unused_model" in str(solver.problems[0])


def test_operation_file_name_differing_from_key_becomes_workspace_identity(
    manager: WorkspaceManager, tmp_path: Path
):
    members = _package_members()
    members["operations/规划路线.md"] = members.pop("operations/plan_route.md")
    meta = manager.import_package_zip("文件名任务", _write_zip(tmp_path / "in.zip", members))
    root = manager.project_root(meta["id"])
    binding = yaml.safe_load(
        (root / solver_contract.SOLVER_DIR / "plan_route" / "binding.yaml").read_text(encoding="utf-8")
    )
    assert binding["operation"] == "规划路线" and binding["solver_key"] == "plan_route"
    assert (root / "ontology" / "operation" / "规划路线.md").is_file()
    files = _read_zip(manager.export_package_zip(meta["id"], tmp_path / "out.zip"))
    assert _frontmatter(files[f"{meta['id']}/operations/plan_route.md"]) == OPERATION_FRONTMATTER


def test_legacy_v02_directories_are_accepted_and_canonicalized(
    manager: WorkspaceManager, tmp_path: Path
):
    members = _package_members(
        manifest_overrides={"solver_layout": None},
        declaration={**DECLARATION, "entry": "decision-models/route_plan.yaml",
                     "input_schema": "schemas/plan_route.input.schema.json"},
    )
    members["decision-models/route_plan.yaml"] = members.pop("operations/plan_route.model.yaml")
    members["schemas/plan_route.input.schema.json"] = members.pop(
        "operations/plan_route.input.schema.json"
    )
    meta = manager.import_package_zip("旧布局任务", _write_zip(tmp_path / "legacy.zip", members))
    files = _read_zip(manager.export_package_zip(meta["id"], tmp_path / "out.zip"))
    pid = meta["id"]
    assert files[f"{pid}/operations/plan_route.model.yaml"] == MODEL_YAML
    assert files[f"{pid}/operations/plan_route.input.schema.json"] == INPUT_SCHEMA
    assert not any("/decision-models/" in name or "/schemas/" in name for name in files)


# ---- 导入 → 导出 ----


def test_import_export_roundtrip_is_semantically_lossless(
    manager: WorkspaceManager, tmp_path: Path
):
    meta, _root = _import(manager, tmp_path)
    pid = meta["id"]
    files = _read_zip(manager.export_package_zip(pid, tmp_path / "out.zip"))

    manifest = yaml.safe_load(files[f"{pid}/manifest.yaml"])
    assert manifest["schema_version"] == "0.2"
    assert manifest["solver_layout"] == "operation-centric.v1"
    assert manifest["capabilities"]["decision_models"] == [DECLARATION]
    for filename, data in SIDECAR_BYTES.items():
        sidecar = next(s for s in solver_contract.SIDECARS if s.filename == filename)
        assert files[f"{pid}/operations/plan_route{sidecar.suffix}"] == data
    assert files[f"{pid}/scripts/route_evaluator.py"] == EVALUATOR_PY
    assert _frontmatter(files[f"{pid}/operations/plan_route.md"]) == OPERATION_FRONTMATTER
    ordinary = _frontmatter(files[f"{pid}/operations/查询站点.md"])
    assert "execution" not in ordinary
    assert solver_contract.index_block(
        solver_contract.PackageSolverRender(capabilities={})
    ) == ""
    root_index = files[f"{pid}/index.md"].decode()
    assert "| plan_route | plan_route | solver_workflow |" in root_index
    operations_index = files[f"{pid}/operations/index.md"].decode()
    assert "| plan_route | plan_route | plan_route | plan_route | solver_workflow |" in operations_index
    assert "| 查询站点 | 查询站点 | 查询站点 | 查询站点 |  |" in operations_index

    # 再导入一次得到同一份求解契约（第二轮往返）
    again = manager.import_package_zip("回导任务", tmp_path / "out.zip")
    solver_dir = manager.project_root(again["id"]) / solver_contract.SOLVER_DIR / "plan_route"
    for filename, data in SIDECAR_BYTES.items():
        assert (solver_dir / filename).read_bytes() == data


def test_repeated_export_is_deterministic(manager: WorkspaceManager, tmp_path: Path):
    """REQ-BLD-010：同一工作区重复导出逐字节相同（成员顺序、时间戳、权限都固定）。"""
    meta, _root = _import(manager, tmp_path)
    first = manager.export_package_zip(meta["id"], tmp_path / "a.zip")
    second = manager.export_package_zip(meta["id"], tmp_path / "b.zip")
    assert _read_zip(first) == _read_zip(second)
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as zf:
        infos = zf.infolist()
    assert [info.filename for info in infos] == sorted(info.filename for info in infos)
    assert {info.date_time for info in infos} == {(1980, 1, 1, 0, 0, 0)}


def _workspace_solver(root: Path, *, solver_key: str, operation: str, enabled: bool = True):
    directory = root / solver_contract.SOLVER_DIR / solver_key
    directory.mkdir(parents=True)
    for filename, data in SIDECAR_BYTES.items():
        (directory / filename).write_bytes(data)
    binding = {
        "solver_key": solver_key,
        "operation": operation,
        "decision_model": "route_plan",
        "declaration": {"engine_requirements": {"problem_family": "binary_ordered_route.v1"}},
        "operation_frontmatter": {"execution": {"enabled": enabled}},
        "units": [{"field": "distance", "unit": "km"}],
    }
    (directory / "binding.yaml").write_text(
        yaml.safe_dump(binding, allow_unicode=True), encoding="utf-8"
    )


def test_chinese_operation_is_renamed_to_solver_key_on_export(
    manager: WorkspaceManager, tmp_path: Path
):
    meta = manager.create_project("改名任务")
    root = manager.project_root(meta["id"])
    (root / "ontology" / "object" / "站点.md").write_text("# 站点\n", encoding="utf-8")
    (root / "ontology" / "operation" / "规划路线.md").write_text(
        "# 规划路线\n\n排序[[站点]]\n", encoding="utf-8"
    )
    (root / "ontology" / "scripts" / "route_eval.py").write_text(
        '"""路线评估\n\n关联operation：规划路线\n"""\n\ndef run():\n    return {}\n',
        encoding="utf-8",
    )
    _workspace_solver(root, solver_key="plan_route", operation="规划路线")
    pid = meta["id"]
    files = _read_zip(manager.export_package_zip(pid, tmp_path / "out.zip"))

    assert f"{pid}/operations/规划路线.md" not in files
    front = _frontmatter(files[f"{pid}/operations/plan_route.md"])
    assert front["key"] == "plan_route" and front["title"] == "规划路线"
    assert front["execution"] == {
        "enabled": True, "mode": "solver_workflow", "decision_model": "route_plan"
    }
    assert front["uses"] == {"objects": ["站点"]}
    assert "| plan_route | 规划路线 | 规划路线 | 规划路线 | solver_workflow |" in (
        files[f"{pid}/operations/index.md"].decode()
    )
    assert 'bound_operation: "plan_route"' in files[f"{pid}/scripts/route_eval.md"].decode()
    # binding.yaml（含单位）是建模侧元数据，不随包发布
    assert not any(name.endswith("binding.yaml") for name in files)
    assert "units" not in files[f"{pid}/manifest.yaml"].decode()


DECLARATIVE_EVALUATOR = '"""声明式评估器（合成示例）\n\n关联operation：无\n"""\n\nTABLE_SOURCES = {}\n'
LEGACY_EVALUATOR = (
    '"""旧式评估器（合成示例）\n\n关联operation：无\n"""\n\n'
    "class BatchPlan:\n    pass\n\n\n"
    "def build_report(plan):\n    return {}\n\n\n"
    "def load_tables(source):\n    return {}\n\n\n"
    "def normalize_crude_name(name):\n    return name\n"
)


@pytest.mark.parametrize("evaluator", [DECLARATIVE_EVALUATOR, LEGACY_EVALUATOR])
def test_solver_evaluator_ships_source_without_script_descriptor(
    manager: WorkspaceManager, tmp_path: Path, evaluator: str
):
    # modeling 把每个脚本描述符都当 operation 实现校验，评估器带描述符会让整包被拒
    meta = manager.create_project("评估器任务")
    root = manager.project_root(meta["id"])
    scripts = root / "ontology" / "scripts"
    (root / "ontology" / "object" / "站点.md").write_text("# 站点\n", encoding="utf-8")
    (root / "ontology" / "operation" / "规划路线.md").write_text(
        "# 规划路线\n\n排序[[站点]]\n", encoding="utf-8"
    )
    (scripts / "route_eval.py").write_text(
        '"""路线评估\n\n关联operation：规划路线\n"""\n\ndef run():\n    return {}\n',
        encoding="utf-8",
    )
    (scripts / "site_evaluator.py").write_text(evaluator, encoding="utf-8")
    _workspace_solver(root, solver_key="plan_route", operation="规划路线")
    pid = meta["id"]
    files = _read_zip(manager.export_package_zip(pid, tmp_path / "out.zip"))

    assert files[f"{pid}/scripts/site_evaluator.py"] == evaluator.encode("utf-8")
    assert f"{pid}/scripts/site_evaluator.md" not in files
    index = files[f"{pid}/scripts/index.md"].decode()
    assert "| route_eval | route_eval |" in index and "site_evaluator" not in index
    assert 'bound_operation: "plan_route"' in files[f"{pid}/scripts/route_eval.md"].decode()

    # 没有描述符也按包内文件名落回工作区
    again = manager.import_package_zip("回导任务", tmp_path / "out.zip")
    restored = manager.project_root(again["id"]) / "ontology" / "scripts" / "site_evaluator.py"
    assert restored.read_text(encoding="utf-8") == evaluator


def test_package_with_only_evaluator_script_has_no_script_index(
    manager: WorkspaceManager, tmp_path: Path
):
    meta = manager.create_project("仅评估器任务")
    root = manager.project_root(meta["id"])
    (root / "ontology" / "object" / "站点.md").write_text("# 站点\n", encoding="utf-8")
    (root / "ontology" / "operation" / "规划路线.md").write_text(
        "# 规划路线\n\n排序[[站点]]\n", encoding="utf-8"
    )
    (root / "ontology" / "scripts" / "site_evaluator.py").write_text(
        DECLARATIVE_EVALUATOR, encoding="utf-8"
    )
    _workspace_solver(root, solver_key="plan_route", operation="规划路线")
    pid = meta["id"]
    files = _read_zip(manager.export_package_zip(pid, tmp_path / "out.zip"))

    assert f"{pid}/scripts/site_evaluator.py" in files
    assert not any(
        name.startswith(f"{pid}/scripts/") and name.endswith(".md") for name in files
    )


def test_disabled_solver_operation_is_left_out_of_solver_indexes(
    manager: WorkspaceManager, tmp_path: Path
):
    meta = manager.create_project("停用任务")
    root = manager.project_root(meta["id"])
    (root / "ontology" / "object" / "站点.md").write_text("# 站点\n", encoding="utf-8")
    (root / "ontology" / "operation" / "plan_route.md").write_text("# 规划路线\n", encoding="utf-8")
    _workspace_solver(root, solver_key="plan_route", operation="plan_route", enabled=False)
    pid = meta["id"]
    files = _read_zip(manager.export_package_zip(pid, tmp_path / "out.zip"))
    assert solver_contract.INDEX_BLOCK_START not in files[f"{pid}/index.md"].decode()
    assert "| plan_route | plan_route | plan_route | plan_route |  |" in (
        files[f"{pid}/operations/index.md"].decode()
    )


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda d: (d / "presentation.yaml").unlink(), solver_contract.CODE_REFERENCE_MISSING),
        (
            lambda d: (d / "binding.yaml").write_text(
                (d / "binding.yaml").read_text(encoding="utf-8").replace("规划路线", "不存在"),
                encoding="utf-8",
            ),
            solver_contract.CODE_REFERENCE_MISSING,
        ),
        (
            lambda d: (d / "binding.yaml").write_text(
                (d / "binding.yaml").read_text(encoding="utf-8").replace(
                    "solver_key: plan_route", "solver_key: Plan-Route"
                ),
                encoding="utf-8",
            ),
            solver_contract.CODE_INVALID,
        ),
        (
            lambda d: (d / "model.yaml").write_bytes(MODEL_YAML + b"access_token: x\n"),
            solver_contract.CODE_RUNTIME_BINDING_FORBIDDEN,
        ),
    ],
)
def test_broken_workspace_contract_blocks_export(
    manager: WorkspaceManager, tmp_path: Path, mutate, code: str
):
    meta = manager.create_project("坏契约任务")
    root = manager.project_root(meta["id"])
    (root / "ontology" / "object" / "站点.md").write_text("# 站点\n", encoding="utf-8")
    (root / "ontology" / "operation" / "规划路线.md").write_text("# 规划路线\n", encoding="utf-8")
    _workspace_solver(root, solver_key="plan_route", operation="规划路线")
    mutate(root / solver_contract.SOLVER_DIR / "plan_route")
    with pytest.raises(WorkspaceError) as exc:
        manager.export_package_zip(meta["id"], tmp_path / "out.zip")
    assert exc.value.code == code


def test_solver_key_colliding_with_another_operation_blocks_export(
    manager: WorkspaceManager, tmp_path: Path
):
    meta = manager.create_project("冲突任务")
    root = manager.project_root(meta["id"])
    (root / "ontology" / "object" / "站点.md").write_text("# 站点\n", encoding="utf-8")
    for stem in ("规划路线", "plan_route"):
        (root / "ontology" / "operation" / f"{stem}.md").write_text(f"# {stem}\n", encoding="utf-8")
    _workspace_solver(root, solver_key="plan_route", operation="规划路线")
    with pytest.raises(WorkspaceError, match="同名"):
        manager.export_package_zip(meta["id"], tmp_path / "out.zip")


# ---- 受管目录 ----


def test_solver_directory_is_not_writable_through_file_editor(
    manager: WorkspaceManager, tmp_path: Path
):
    meta, root = _import(manager, tmp_path)
    with pytest.raises(WorkspaceError) as exc:
        manager.write_file(meta["id"], "ontology/solver/plan_route/model.yaml", "id: other\n")
    assert exc.value.status == 403
    assert (root / "ontology/solver/plan_route/model.yaml").read_bytes() == MODEL_YAML


def test_scope_guard_denies_agent_writes_to_solver_directory(tmp_path: Path):
    guard = TOOLKIT_SCRIPTS / "ontology_scope_guard.sh"
    _git(tmp_path, "init", "-q")

    def decide(rel: str) -> str:
        # 安全审查：argv 是 shutil.which 解析出的 bash 路径加工具箱自带的守卫脚本，以序列传入、
        # 不经 shell 解析；stdin 是合成的工具调用，只作用于 tmp_path 下的临时仓库。
        result = subprocess.run(  # NOSONAR(S4721)
            [BASH, str(guard)],
            input=json.dumps({"tool_input": {"file_path": str(tmp_path / rel)}}),
            capture_output=True, text=True, cwd=tmp_path, check=True, shell=False, timeout=60,
        )
        return result.stdout

    denied = decide("ontology/solver/plan_route/model.yaml")
    assert '"permissionDecision": "deny"' in denied and "求解模型面板" in denied
    assert decide("ontology/operation/规划路线.md") == ""


def test_checker_warns_when_bound_operation_disappears(tmp_path: Path, monkeypatch):
    monkeypatch.syspath_prepend(str(TOOLKIT_SCRIPTS))
    checker = importlib.import_module("ontology_checker")
    ontology = tmp_path / "ontology"
    (ontology / "operation").mkdir(parents=True)
    (ontology / "operation" / "规划路线.md").write_text("# 规划路线\n", encoding="utf-8")
    _workspace_solver(tmp_path, solver_key="plan_route", operation="规划路线")
    assert checker.check_solver_bindings(ontology) == []
    assert checker.check_file_locations(ontology) == []

    (ontology / "operation" / "规划路线.md").rename(ontology / "operation" / "路线规划.md")
    [issue] = checker.check_solver_bindings(ontology)
    assert issue.severity == "警告" and issue.file == "solver/plan_route/binding.yaml"
    assert "规划路线" in issue.message

    (ontology / "solver" / "plan_route" / "说明.md").write_text("# 说明\n", encoding="utf-8")
    [location] = checker.check_file_locations(ontology)
    assert location.severity == "错误" and "受管" in location.message


def test_workspace_rules_tell_agent_solver_directory_is_managed():
    rules = (TOOLKIT_SCRIPTS.parents[1] / "workspace-rules.md").read_text(encoding="utf-8")
    assert "`ontology/solver/` 是平台受管的求解契约目录" in rules
    assert "SOLVER_CONTEXT" in rules


# ---- consumer 导入边界 ----


def _consumer_validators(monkeypatch):
    consumer = Path(__file__).resolve().parents[4] / "consumer" / "backend"
    monkeypatch.syspath_prepend(str(consumer))
    try:
        from ontology.package_import import validate_ontology_revision
        from ontology.solver_package_layout import validate_solver_package_layout
        from solver_coordination.capability_catalog import load_solver_capability_catalog
    except Exception as exc:  # consumer 依赖未装在建模后端 venv 时跳过
        pytest.skip(f"consumer 校验器不可用：{exc}")
    return validate_ontology_revision, validate_solver_package_layout, load_solver_capability_catalog


def test_exported_package_passes_consumer_import_contract(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch
):
    validate_revision, validate_layout, load_catalog = _consumer_validators(monkeypatch)
    meta = manager.create_project("契约任务")
    root = manager.project_root(meta["id"])
    (root / "ontology" / "object" / "站点.md").write_text("# 站点\n", encoding="utf-8")
    (root / "ontology" / "operation" / "规划路线.md").write_text(
        "# 规划路线\n\n排序[[站点]]\n", encoding="utf-8"
    )
    (root / "ontology" / "operation" / "查询站点.md").write_text("# 查询站点\n", encoding="utf-8")
    _workspace_solver(root, solver_key="plan_route", operation="规划路线")
    archive = manager.export_package_zip(meta["id"], tmp_path / "out.zip")
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(extracted)
    package_root = extracted / meta["id"]

    manifest = validate_revision(package_root)
    validate_layout(package_root, manifest)
    records = load_catalog(package_root, manifest)
    assert [record.operation_key for record in records] == ["plan_route"]
