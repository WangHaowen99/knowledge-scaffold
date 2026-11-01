"""求解契约端到端验证编排（需求 PR5）。

用本地 stub 代替 modeling：覆盖用例存储、2N 候选断言（N=1/7/9/64）、全不可行、
非权威结果、能力漂移、Consumer/modeling 不可用、修订冲突与变更、用例覆盖不全、
重启中断收敛，以及记录与证据不含令牌和地址。fixture 全部为合成数据。
"""

import asyncio
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import config
from app.services import eval_data_sources, solver_contract, solver_validation, solver_validation_run
from app.services.workspace import WorkspaceError, WorkspaceManager, _git

TOKEN = "synthetic-access-token"
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
INPUT_SCHEMA = json.dumps(solver_contract.ORDERED_ROUTE_INPUT_SCHEMA) + "\n"
EVALUATOR_PY = '''"""路线评估器（合成示例：只含 Consumer 兼容层接口的桩）"""

SOLVER_CONTEXT = {
    "option_routes": {"OPTION_0": "route-a", "OPTION_1": "route-b"},
    "route_stream_products": ["stream-a"],
    "special_tanks": {"diesel": "tank-a", "vgo": "tank-b"},
    "unit_capacity_keys": {
        "atmospheric": "cap-a", "diesel_by_unit": {"U1": "cap-b"}, "vgo": "cap-c",
    },
    "correction_factor_prefix_by_unit": {"U1": "factor-a"},
    "production_condition": "condition-a",
}
ATMOS_UNIT_ID = "unit-a"
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
def manager(tmp_path: Path, monkeypatch) -> WorkspaceManager:
    assets = tmp_path / "assets"
    (assets / "workspace-toolkit" / "templates").mkdir(parents=True)
    (assets / "workspace-rules.md").write_text("# rules", encoding="utf-8")
    (assets / "resource-template" / "others").mkdir(parents=True)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "MODELING_TOKEN", "")
    return WorkspaceManager(tmp_path / "data", assets)


def _item(solver_key: str = "plan_route", operation: str = "规划路线", units: str = "km") -> dict:
    return {
        "binding": {
            "solver_key": solver_key,
            "operation": operation,
            "decision_model": "route_plan",
            "declaration": {"engine_requirements": {"problem_family": "candidate_selection.v1"}},
            "operation_frontmatter": {},
            "units": [{"field": "distance", "unit": units}],
        },
        "sidecars": {
            "entry": MODEL_YAML,
            "input_schema": INPUT_SCHEMA,
            "result_schema": OBJECT_SCHEMA,
            "fact_snapshot_schema": OBJECT_SCHEMA,
            "presentation": "columns: []\n",
        },
    }


def _project(manager: WorkspaceManager, items: list[dict] | None = None) -> tuple[str, Path, str]:
    pid = manager.create_project("求解验证")["id"]
    root = manager.project_root(pid)
    (root / "ontology" / "operation" / "规划路线.md").write_text("# 规划路线\n", encoding="utf-8")
    (root / "ontology" / "operation" / "查询站点.md").write_text("# 查询站点\n", encoding="utf-8")
    (root / "ontology" / "scripts" / "route_eval.py").write_text(EVALUATOR_PY, encoding="utf-8")
    # 发布基于 HEAD 提交：Operation 文档与评估器先提交（相当于 agent 的阶段提交）
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "test: synthetic operations")
    saved = manager.save_solver_contract(pid, items or [_item()], "")
    return pid, root, saved["revision"]


def _batches(count: int) -> dict:
    return {"month": "2026-01", "items": [{"batch": f"B{index + 1}"} for index in range(count)]}


def _case(case_id: str, count: int, solver_key: str = "plan_route", **extra) -> dict:
    return {"id": case_id, "name": f"{count} 批", "solver_key": solver_key, "arguments": _batches(count), **extra}


def _authoritative(case: dict, **extra) -> dict:
    count = len(case["arguments"].get("items") or [])
    return {
        "id": case["id"],
        "operation_key": case["operation_key"],
        "status": "completed",
        "authoritative": True,
        "solver_tool_called": True,
        "solver_tool": "solve_plan_route_0a1b2c3d",
        "arguments_match": True,
        "run_id": f"run-{case['id']}",
        "job_id": f"job-{case['id']}",
        "feasibility": "feasible",
        "optimality": "proven_optimal",
        "proof_kind": "exact_search",
        "result_artifact_ref": f"artifact://result/{case['id']}",
        "verification_evidence_ref": f"artifact://evidence/{case['id']}",
        "knowledge_revision_id": "0.1.2",
        "fact_snapshot_digest": "sha256:synthetic-facts",
        "problem_digest": f"sha256:synthetic-problem-{case['id']}",
        "candidate_results_artifact_ref": f"artifact://candidates/{case['id']}",
        "candidate_results_artifact_digest": "sha256:candidates",
        "candidate_count": 2 * count,
        **extra,
    }


def _multipart_fields(body: bytes, content_type: str) -> list[tuple[str, bytes]]:
    boundary = content_type.split("boundary=", 1)[1].encode()
    fields = []
    for part in body.split(b"--" + boundary):
        if b"\r\n\r\n" not in part:
            continue
        head, value = part.split(b"\r\n\r\n", 1)
        name = head.split(b'name="', 1)[1].split(b'"', 1)[0].decode()
        fields.append((name, value[:-2] if value.endswith(b"\r\n") else value))
    return fields


class StubModeling:
    """合成 modeling：记录请求，并按 respond(cases) 给出验证结果。"""

    def __init__(self, respond=None):
        self.requests: list[dict] = []
        self.graph = {"nodeCount": 0, "relationshipCount": 0}  # 图隔离范围内已有的数据量
        self.respond = respond or (lambda cases: (200, {"validation": {
            "package": {"id": "pkg", "version": "0.1.2"},
            "cases": [_authoritative(case) for case in cases],
        }}))
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status: int, payload: dict):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._send(200, {"ok": True})

            def do_PUT(self):
                self._handle()

            def do_POST(self):
                self._handle()

            def _handle(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                entry = {
                    "method": self.command,
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "content_type": self.headers.get("Content-Type", ""),
                    "body": body,
                }
                stub.requests.append(entry)
                if self.path == "/api/package-workspaces/import":
                    self._send(200, {"project": {"id": "mp_synthetic"}})
                elif self.path == "/api/projects/mp_synthetic/data-source":
                    self._send(200, {"dataSource": {"mode": "none"}})
                elif self.path == "/api/projects/mp_synthetic/graph-import/isolation":
                    self._send(200, {"existing": stub.graph})
                elif self.path == "/api/projects/mp_synthetic/solver-contract-validations":
                    fields = dict(_multipart_fields(body, entry["content_type"]))
                    entry["fields"] = [name for name, _ in _multipart_fields(body, entry["content_type"])]
                    entry["run_id"] = fields["run_id"].decode()
                    entry["cases"] = json.loads(fields["cases"])
                    status, payload = stub.respond(entry["cases"])
                    self._send(status, payload)
                else:
                    self._send(404, {"error": "not found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def find(self, suffix: str) -> dict:
        return next(item for item in self.requests if item["path"].endswith(suffix))


@pytest.fixture()
def stub(monkeypatch):
    servers: list[StubModeling] = []

    def make(respond=None) -> StubModeling:
        server = StubModeling(respond)
        servers.append(server)
        monkeypatch.setattr(config, "MODELING_URL", server.url)
        return server

    yield make
    for server in servers:
        server.close()


def _start(manager: WorkspaceManager, pid: str, revision: str, **kwargs):
    return solver_validation_run.start(
        manager, pid, revision=revision, case_ids=kwargs.pop("case_ids", []), token=TOKEN, **kwargs
    )


def _run(manager: WorkspaceManager, pid: str, revision: str, **kwargs) -> dict:
    record, plan = _start(manager, pid, revision, **kwargs)
    assert record["status"] == solver_validation.STATUS_QUEUED
    try:
        return solver_validation_run.execute(plan, manager)
    finally:
        solver_validation_run._running.discard(pid)


# ---- 用例存储与候选数推算 ----


def test_cases_are_normalized_and_validated(manager: WorkspaceManager):
    _pid, root, _revision = _project(manager)
    saved = solver_validation_run.save_cases(root, [
        {"id": "n7", "solver_key": "plan_route", "arguments": _batches(7)},
        _case("n9", 9, expected_candidate_count=18),
    ])
    assert saved[0]["name"] == "n7"
    assert solver_validation_run.list_cases(root) == saved
    assert not (root / solver_validation_run.CASES_FILE).is_relative_to(root / "ontology")

    invalid = [
        [{"id": "a b", "solver_key": "plan_route", "arguments": {}}],
        [_case("n1", 1), _case("n1", 1)],
        [{"id": "n1", "solver_key": "规划路线", "arguments": {}}],
        [{"id": "n1", "solver_key": "plan_route", "arguments": []}],
        [_case("n1", 1, expected_candidate_count=-1)],
        [_case(f"c{index}", 1) for index in range(21)],
        [{"id": "big", "solver_key": "plan_route", "arguments": {"blob": "x" * (64 * 1024)}}],
    ]
    for cases in invalid:
        with pytest.raises(WorkspaceError) as exc:
            solver_validation_run.save_cases(root, cases)
        assert exc.value.code == solver_validation_run.CODE_CASES_INVALID
    assert solver_validation_run.list_cases(root) == saved


@pytest.mark.parametrize(("count", "expected"), [(1, 2), (7, 14), (9, 18), (64, 128), (65, None), (0, None)])
def test_route_profile_expects_two_candidates_per_item(count: int, expected: int | None):
    contract = SimpleNamespace(sidecars={"entry": MODEL_YAML.encode()})
    assert solver_validation_run.expected_candidate_count(contract, _case("c", count)) == expected


def test_explicit_expected_count_and_other_profiles():
    route = SimpleNamespace(sidecars={"entry": MODEL_YAML.encode()})
    other = SimpleNamespace(sidecars={"entry": b"id: x\nprofile_id: other.v1\n"})
    assert solver_validation_run.expected_candidate_count(
        route, {"arguments": {"month": "2026-02"}, "expected_candidate_count": 14}
    ) == 14
    assert solver_validation_run.expected_candidate_count(other, _case("c", 7)) is None


# ---- 启动前校验 ----


def test_start_requires_current_revision_and_complete_cases(manager: WorkspaceManager, stub):
    stub()
    pid, root, revision = _project(
        manager, [_item(), _item(solver_key="find_station", operation="查询站点")]
    )
    with pytest.raises(WorkspaceError) as exc:
        _start(manager, pid, revision)
    assert exc.value.code == solver_validation_run.CODE_CASES_INCOMPLETE

    solver_validation_run.save_cases(root, [_case("n7", 7)])
    with pytest.raises(WorkspaceError) as exc:
        _start(manager, pid, "sha256:old")
    assert (exc.value.status, exc.value.code) == (409, "solver_contract_revision_conflict")

    with pytest.raises(WorkspaceError) as exc:
        _start(manager, pid, revision)
    assert exc.value.code == solver_validation_run.CODE_CASES_INCOMPLETE
    assert "find_station" in str(exc.value)

    solver_validation_run.save_cases(root, [_case("n7", 7), _case("ghost", 1, solver_key="ghost_key")])
    with pytest.raises(WorkspaceError) as exc:
        _start(manager, pid, revision, case_ids=["n7", "ghost"])
    assert exc.value.code == solver_validation_run.CODE_CASES_INVALID
    with pytest.raises(WorkspaceError) as exc:
        _start(manager, pid, revision, case_ids=["missing"])
    assert exc.value.code == solver_validation_run.CODE_CASES_INVALID
    assert solver_validation.list_records(root) == []


def test_start_rejects_dataset_sources_and_unavailable_modeling(
    manager: WorkspaceManager, stub, monkeypatch
):
    pid, root, revision = _project(manager)
    solver_validation_run.save_cases(root, [_case("n7", 7)])
    source = eval_data_sources.create_source(pid, {"mode": "dataset", "include_mock": True}, root)
    stub()
    with pytest.raises(WorkspaceError) as exc:
        _start(manager, pid, revision, data_source_id=source["id"])
    assert exc.value.code == solver_validation_run.CODE_DATA_SOURCE_UNSUPPORTED
    with pytest.raises(WorkspaceError) as exc:
        _start(manager, pid, revision, data_source_id="missing")
    assert exc.value.status == 404

    monkeypatch.setattr(config, "MODELING_URL", "")
    with pytest.raises(WorkspaceError) as exc:
        _start(manager, pid, revision)
    assert (exc.value.status, exc.value.code) == (400, solver_validation_run.CODE_UNAVAILABLE)
    monkeypatch.setattr(config, "MODELING_URL", "http://127.0.0.1:9")
    with pytest.raises(WorkspaceError) as exc:
        _start(manager, pid, revision)
    assert (exc.value.status, exc.value.code) == (503, solver_validation_run.CODE_UNAVAILABLE)
    assert solver_validation.list_records(root) == []


def test_start_tolerates_mark_used_failure_and_releases_slot_when_record_fails(
    manager: WorkspaceManager, stub, monkeypatch
):
    stub()
    pid, root, revision = _project(manager)
    solver_validation_run.save_cases(root, [_case("n7", 7)])
    source = {"id": "ds_synthetic", "mode": "connection", "name": "合成外部连接"}
    monkeypatch.setattr(eval_data_sources, "get_source", lambda *args: source)
    monkeypatch.setattr(solver_validation_run.modeling_eval, "needs_import", lambda found: False)

    def broken(*args, **kwargs):
        raise OSError("synthetic storage failure")

    # 最近使用时间写不进去不影响验证，也不能在记录登记后抛出而留下并发占位
    monkeypatch.setattr(eval_data_sources, "mark_used", broken)
    record, plan = _start(manager, pid, revision, data_source_id="ds_synthetic")
    try:
        assert record["status"] == solver_validation.STATUS_QUEUED
        assert plan.data_source == source
    finally:
        solver_validation_run._running.discard(pid)

    solver_validation.update(root, record["id"], status=solver_validation.STATUS_FAILED)
    monkeypatch.setattr(solver_validation, "create", broken)
    with pytest.raises(OSError):
        _start(manager, pid, revision)
    assert pid not in solver_validation_run._running


def test_concurrent_start_is_rejected(manager: WorkspaceManager, stub):
    stub()
    pid, root, revision = _project(manager)
    solver_validation_run.save_cases(root, [_case("n7", 7)])
    record, _plan = _start(manager, pid, revision)
    try:
        with pytest.raises(WorkspaceError) as exc:
            _start(manager, pid, revision)
        assert (exc.value.status, exc.value.code) == (409, solver_validation_run.CODE_BUSY)
    finally:
        solver_validation_run._running.discard(pid)
    # 进程重启后没有编排在跑：遗留的 queued 记录收敛为失败，门禁给出原因
    assert solver_validation_run.sweep_interrupted(config.DATA_DIR) == [pid]
    current = solver_validation.latest(root, revision)
    assert current["id"] == record["id"]
    assert current["error_code"] == solver_validation_run.CODE_INTERRUPTED
    assert manager.solver_release_status(pid)["gate"][0]["code"] == solver_contract.CODE_VALIDATION_FAILED


# ---- 执行与判定 ----


def test_two_n_candidates_pass_and_unlock_publish(manager: WorkspaceManager, stub, tmp_path: Path):
    server = stub()
    pid, root, revision = _project(manager)
    cases = [_case(f"n{count}", count) for count in (1, 7, 9, 64)]
    solver_validation_run.save_cases(root, cases)

    record = _run(manager, pid, revision)

    assert record["status"] == solver_validation.STATUS_PASSED
    assert record["authoritative"] is True
    assert record["candidate_count"] == 2 + 14 + 18 + 128
    assert record["error_code"] == ""
    evidence = record["evidence"]
    assert [item["expected_candidate_count"] for item in evidence["cases"]] == [2, 14, 18, 128]
    assert all(item["passed"] for item in evidence["cases"])
    assert evidence["decision_models"] == {"plan_route": "route_plan"}
    assert record["request_digest"].startswith("sha256:")
    assert record["result_digest"].startswith("sha256:")
    assert "request_digest" not in evidence and "result_digest" not in evidence
    assert evidence["modeling_project"] == "mp_synthetic"
    # 规范化契约摘要、导出包字节摘要与逐用例输入摘要都进证据（REQ-PKG-006 / REQ-BLD-005）
    assert record["contract_digest"].startswith("sha256:")
    assert evidence["contract_digests"]["total"] == record["contract_digest"]
    assert set(evidence["contract_digests"]["operations"]) == {"plan_route"}
    assert manager.solver_release_status(pid)["digests"]["total"] == record["contract_digest"]
    assert evidence["package_digest"].startswith("sha256:")
    assert evidence["cases"][1]["input_digest"] == solver_validation_run._digest(_batches(7))
    assert evidence["cases"][1]["problem_digest"] == "sha256:synthetic-problem-n7"
    assert manager.solver_release_status(pid)["gate"] == []

    # 同一修订再验证一次：导出包逐字节相同，契约摘要不变
    again = _run(manager, pid, revision)
    assert again["evidence"]["package_digest"] == evidence["package_digest"]
    assert again["contract_digest"] == record["contract_digest"]
    manager.publish_package(pid, tmp_path / "solver.zip")

    validation = server.find("/solver-contract-validations")
    assert validation["fields"] == ["run_id", "cases", "file"]
    assert validation["run_id"] == record["id"]
    assert [item["operation_key"] for item in validation["cases"]] == ["plan_route"] * 4
    assert validation["cases"][1]["arguments"] == _batches(7)
    assert {item["authorization"] for item in server.requests} == {f"Bearer {TOKEN}"}
    assert json.loads(server.find("/data-source")["body"]) == {"mode": "none"}
    stored = (root / solver_validation.VALIDATIONS_FILE).read_text(encoding="utf-8")
    assert TOKEN not in stored and "127.0.0.1" not in stored


GRAPH_PASSWORD = secrets.token_hex(8)


@pytest.mark.parametrize(("nodes", "status"), [
    (5, solver_validation.STATUS_PASSED),
    (0, solver_validation.STATUS_FAILED),
])
def test_graph_source_with_import_selection_reads_existing_graph_data(
    manager: WorkspaceManager, stub, nodes: int, status: str,
):
    server = stub()
    server.graph = {"nodeCount": nodes, "relationshipCount": 2 if nodes else 0}
    pid, root, revision = _project(manager)
    solver_validation_run.save_cases(root, [_case("n7", 7)])
    source = eval_data_sources.create_source(pid, {
        "mode": "connection", "provider": "neo4j", "include_mock": True,
        "config": {
            "uri": "bolt://graph.example.com:7687", "username": "neo4j", "password": GRAPH_PASSWORD,
            "workspace_value": "solver-eval-ws", "ontology_property": "d2o_ontology_id",
        },
    }, root)

    record = _run(manager, pid, revision, data_source_id=source["id"])

    assert record["status"] == status
    assert record["evidence"]["graph_data"] == {"nodes": nodes, "relationships": 2 if nodes else 0}
    # 只配置连接、统计已有数据，不做图导入
    paths = [item["path"] for item in server.requests]
    assert not any("/graph-import/uploads" in path for path in paths)
    configured = json.loads(server.find("/data-source")["body"])
    assert "files" not in configured and "include_mock" not in configured
    assert configured["config"]["ontology_value"] == pid
    assert json.loads(server.find("/graph-import/isolation")["body"]) == {"password": GRAPH_PASSWORD}
    assert paths.index("/api/projects/mp_synthetic/data-source") < paths.index(
        "/api/projects/mp_synthetic/graph-import/isolation"
    )
    validated = any(path.endswith("/solver-contract-validations") for path in paths)
    if nodes:
        assert validated
    else:
        # 范围为空时在提问前失败，不产生 Agent 开销
        assert not validated
        assert record["error_code"] == solver_validation_run.CODE_GRAPH_DATA_EMPTY
        assert "solver-eval-ws" in record["message"] and "评测" in record["message"]
    stored = (root / solver_validation.VALIDATIONS_FILE).read_text(encoding="utf-8")
    assert GRAPH_PASSWORD not in stored and "graph.example.com" not in stored


def test_candidate_count_mismatch_fails(manager: WorkspaceManager, stub):
    stub(lambda cases: (200, {"validation": {"package": {}, "cases": [
        _authoritative(case, candidate_count=16 if case["id"] == "n9" else None)
        if case["id"] == "n9" else _authoritative(case)
        for case in cases
    ]}}))
    pid, root, revision = _project(manager)
    solver_validation_run.save_cases(root, [_case("n7", 7), _case("n9", 9)])

    record = _run(manager, pid, revision)

    assert record["status"] == solver_validation.STATUS_FAILED
    assert record["error_code"] == solver_validation_run.CODE_CANDIDATE_MISMATCH
    assert "候选数 16，预期 18" in record["message"]
    assertion = record["evidence"]["cases"][1]["assertions"][-1]
    assert assertion == {"name": "candidate_count", "expected": 18, "actual": 16, "passed": False}
    gate = manager.solver_release_status(pid)["gate"]
    assert gate[0]["code"] == solver_contract.CODE_VALIDATION_FAILED


def test_all_infeasible_is_a_valid_authoritative_result(manager: WorkspaceManager, stub):
    stub(lambda cases: (200, {"validation": {"package": {}, "cases": [
        _authoritative(
            case, feasibility="infeasible", optimality="not_applicable",
            candidate_count=None, candidate_results_artifact_ref=None,
            candidate_results_artifact_digest=None,
        )
        for case in cases
    ]}}))
    pid, root, revision = _project(manager)
    solver_validation_run.save_cases(root, [_case("n7", 7)])

    record = _run(manager, pid, revision)

    assert record["status"] == solver_validation.STATUS_PASSED
    assert record["candidate_count"] is None
    assert solver_validation.is_accepted(record)


@pytest.mark.parametrize("extra", [
    {"authoritative": False},
    # modeling 没收到同一调用的 solver.result.authoritative 事件时把结果降为非权威并带上原因
    {
        "authoritative": False, "code": "solver_authoritative_event_missing",
        "detail": "没有收到该求解调用的权威结果事件",
    },
])
def test_non_authoritative_result_cannot_pass(
    manager: WorkspaceManager, stub, tmp_path: Path, extra: dict,
):
    stub(lambda cases: (200, {"validation": {"package": {}, "cases": [
        _authoritative(case, **extra) for case in cases
    ]}}))
    pid, root, revision = _project(manager)
    solver_validation_run.save_cases(root, [_case("n7", 7)])

    record = _run(manager, pid, revision)

    assert record["status"] == solver_validation.STATUS_FAILED
    assert record["authoritative"] is False
    assert record["error_code"] == solver_validation_run.CODE_NOT_AUTHORITATIVE
    assert extra.get("detail", "") in record["message"]
    assert manager.solver_release_status(pid)["gate"][0]["code"] == solver_contract.CODE_VALIDATION_FAILED
    with pytest.raises(WorkspaceError):
        manager.publish_package(pid, tmp_path / "blocked.zip")


def test_authoritative_result_without_evidence_refs_cannot_pass(
    manager: WorkspaceManager, stub, tmp_path: Path,
):
    stub(lambda cases: (200, {"validation": {"package": {}, "cases": [
        _authoritative(case, verification_evidence_ref=None) for case in cases
    ]}}))
    pid, root, revision = _project(manager)
    solver_validation_run.save_cases(root, [_case("n7", 7)])

    record = _run(manager, pid, revision)

    assert record["status"] == solver_validation.STATUS_FAILED
    assert record["error_code"] == solver_validation_run.CODE_EVIDENCE_INCOMPLETE
    assert "verification_evidence_ref" in record["message"]
    assertion = record["evidence"]["cases"][0]["assertions"][-1]
    assert assertion == {"name": "evidence", "missing": ["verification_evidence_ref"], "passed": False}
    assert not solver_validation.is_accepted(record)
    with pytest.raises(WorkspaceError):
        manager.publish_package(pid, tmp_path / "blocked.zip")


def _completed(**extra) -> dict:
    return _authoritative({"id": "n7", "operation_key": "plan_route", "arguments": _batches(7)}, **extra)


@pytest.mark.parametrize(("missing", "code"), [
    ("solver_tool_called", solver_validation_run.CODE_TOOL_NOT_CALLED),
    ("arguments_match", solver_validation_run.CODE_ARGUMENTS_MISMATCH),
])
def test_completed_result_without_tool_conclusions_cannot_pass(missing: str, code: str):
    result = _completed()
    del result[missing]
    assertions, actual, reason = solver_validation_run._assess(result, 14)
    assert actual == code
    assert assertions[-1] == {"name": missing, "passed": False}
    assert "缺少" in reason


@pytest.mark.parametrize("key", [
    *solver_validation_run._REQUIRED_EVIDENCE, *solver_validation_run._CANDIDATE_EVIDENCE,
])
def test_each_authoritative_evidence_field_is_required(key: str):
    assertions, code, reason = solver_validation_run._assess(_completed(**{key: " "}), 14)
    assert code == solver_validation_run.CODE_EVIDENCE_INCOMPLETE
    assert assertions[-1] == {"name": "evidence", "missing": [key], "passed": False}
    assert key in reason


def test_candidate_evidence_is_paired_and_optional_only_without_candidates():
    assess = solver_validation_run._assess
    no_candidates = {
        "candidate_count": None,
        "candidate_results_artifact_ref": None,
        "candidate_results_artifact_digest": None,
    }
    # 全不可行可以没有候选；有候选时仍按 2N 核对
    assert assess(_completed(feasibility="infeasible", **no_candidates), 14)[1] == ""
    assert assess(_completed(feasibility="infeasible"), 14)[1] == ""
    assert assess(_completed(feasibility="infeasible", candidate_count=4), 14)[1] == (
        solver_validation_run.CODE_CANDIDATE_MISMATCH
    )
    # 候选引用与摘要成对出现，只给一半不算完整
    half = _completed(feasibility="infeasible", candidate_count=None, candidate_results_artifact_ref=None)
    assertions, code, _reason = assess(half, 14)
    assert code == solver_validation_run.CODE_EVIDENCE_INCOMPLETE
    assert assertions[-1]["missing"] == ["candidate_results_artifact_ref"]
    # 可行且有候选预期时必须带候选；没有候选预期的求解操作不要求
    assertions, code, _reason = assess(_completed(**no_candidates), 14)
    assert code == solver_validation_run.CODE_EVIDENCE_INCOMPLETE
    assert assertions[-1]["missing"] == list(solver_validation_run._CANDIDATE_EVIDENCE)
    assert assess(_completed(**no_candidates), None)[1] == ""


def test_capability_drift_and_missing_results_fail_with_case_codes(manager: WorkspaceManager, stub):
    stub(lambda cases: (200, {"validation": {"package": {}, "cases": [{
        "id": cases[0]["id"], "operation_key": "plan_route", "status": "error",
        "code": "solver_capability_drift", "phase": "request", "retryable": False,
        "authoritative": False,
    }]}}))
    pid, root, revision = _project(manager)
    solver_validation_run.save_cases(root, [_case("n7", 7), _case("n9", 9)])

    record = _run(manager, pid, revision)

    assert record["error_code"] == "solver_capability_drift"
    assert [item.get("code") for item in record["evidence"]["cases"]] == [
        "solver_capability_drift", "solver_result_missing",
    ]
    assert "0/2 条用例通过" in record["message"]


def test_consumer_unavailable_is_recorded_without_addresses(manager: WorkspaceManager, stub):
    stub(lambda cases: (502, {
        "code": "CONSUMER_UNAVAILABLE",
        "error": f"Consumer 请求失败：connect {SYNTHETIC_IP}:8000，见 https://consumer.example.com/status",
        "stage": "workspace",
    }))
    pid, root, revision = _project(manager)
    solver_validation_run.save_cases(root, [_case("n7", 7)])

    record = _run(manager, pid, revision)

    assert record["status"] == solver_validation.STATUS_FAILED
    assert record["error_code"] == "consumer_unavailable"
    assert SYNTHETIC_IP not in record["message"]
    assert "example.com" not in record["message"]
    assert "[地址已隐藏]" in record["message"]


def test_consumer_reason_for_a_rejected_case_reaches_the_message(manager: WorkspaceManager, stub):
    stub(lambda cases: (200, {"validation": {"package": {}, "cases": [{
        "id": cases[0]["id"], "operation_key": "plan_route", "status": "error",
        "code": "fact_snapshot_invalid", "phase": "snapshot", "retryable": False, "authoritative": False,
        "detail": "business data table synthetic_table is empty, see https://graph.example.net",
    }]}}))
    pid, root, revision = _project(manager)
    solver_validation_run.save_cases(root, [_case("n7", 7)])

    record = _run(manager, pid, revision)

    assert record["error_code"] == "fact_snapshot_invalid"
    assert "（fact_snapshot_invalid，阶段 snapshot）：business data table synthetic_table is empty" in record["message"]
    assert "example.net" not in record["message"]


def test_modeling_going_away_mid_run_is_recorded(manager: WorkspaceManager, stub, monkeypatch):
    stub()
    pid, root, revision = _project(manager)
    solver_validation_run.save_cases(root, [_case("n7", 7)])
    record, plan = _start(manager, pid, revision)
    monkeypatch.setattr(config, "MODELING_URL", "http://127.0.0.1:9")
    try:
        result = solver_validation_run.execute(plan, manager)
    finally:
        solver_validation_run._running.discard(pid)
    assert result["id"] == record["id"]
    assert result["error_code"] == solver_validation_run.CODE_MODELING_UNAVAILABLE
    assert "127.0.0.1" not in json.dumps(result, ensure_ascii=False)


def test_contract_edit_while_queued_fails_the_run(manager: WorkspaceManager, stub):
    server = stub()
    pid, root, revision = _project(manager)
    solver_validation_run.save_cases(root, [_case("n7", 7)])
    _record, plan = _start(manager, pid, revision)
    manager.save_solver_contract(pid, [_item(units="m")], revision)
    try:
        result = solver_validation_run.execute(plan, manager)
    finally:
        solver_validation_run._running.discard(pid)
    assert result["error_code"] == solver_validation_run.CODE_REVISION_CHANGED
    assert result["revision"] == revision
    assert server.requests == []


# ---- 路由 ----


def test_routes_store_cases_start_and_report_latest(manager: WorkspaceManager, stub, monkeypatch):
    from app.main import workspace_error_handler
    from app.routers import projects

    stub()
    monkeypatch.setattr(projects, "workspaces", manager)
    monkeypatch.setattr(projects, "require_owner", lambda pid, *args, **kwargs: manager.project_root(pid))
    launched = []
    monkeypatch.setattr(solver_validation_run, "launch", launched.append)
    pid, _root, revision = _project(manager)
    request = SimpleNamespace(
        state=SimpleNamespace(user={"sub": "alice"}), headers={"authorization": f"Bearer {TOKEN}"}
    )

    saved = projects.save_solver_validation_cases(
        pid, projects.SolverValidationCasesBody(cases=[_case("n9", 9)]), request
    )
    assert saved["cases"][0]["id"] == "n9"
    assert projects.get_solver_validation_cases(pid, request) == saved
    assert projects.latest_solver_validation(pid, request) == {"revision": revision, "validation": None}

    body = projects.SolverValidationStartBody(revision="sha256:old")
    with pytest.raises(WorkspaceError) as exc:
        asyncio.run(projects.start_solver_validation(pid, body, request))
    conflict = asyncio.run(workspace_error_handler(request, exc.value))
    assert conflict.status_code == 409

    started = asyncio.run(projects.start_solver_validation(
        pid, projects.SolverValidationStartBody(revision=revision), request
    ))
    assert started["validation"]["status"] == solver_validation.STATUS_QUEUED
    assert started["validation"]["by"] == "alice"
    (plan,) = launched
    assert plan.token == TOKEN
    try:
        solver_validation_run.execute(plan, manager)
    finally:
        solver_validation_run._running.discard(pid)
    latest = projects.latest_solver_validation(pid, request)
    assert latest["validation"]["status"] == solver_validation.STATUS_PASSED
    assert latest["validation"]["evidence"]["cases"][0]["candidate_count"] == 18
