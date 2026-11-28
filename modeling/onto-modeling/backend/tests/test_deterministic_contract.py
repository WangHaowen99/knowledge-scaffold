"""确定性 Operation 建模契约与发布转换；样例均为合成数据。"""

import ast
import asyncio
import hashlib
import json
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import deterministic_contract
from app.services import deterministic_verification
from app.services import publish as publish_service
from app.services.packs import builtin, checks
from app.services.packs.repository import PackRepository
from app.services.workspace import WorkspaceError, WorkspaceManager, _git


HANDLER = '''"""合成汇总脚本

关联operation：计算汇总
功能描述：根据结构化参数计算合成汇总。
输入参数：
amount    number    合成金额，必填
输出：
value    number    合成结果
"""
import argparse
import json

parser = argparse.ArgumentParser()
parser.add_argument("--amount", type=float, required=True)
args = parser.parse_args()
print(json.dumps({"value": args.amount}, ensure_ascii=False))
'''

PARSER = '''import argparse
import json

parser = argparse.ArgumentParser()
parser.add_argument("--request-json", required=True)
payload = json.loads(parser.parse_args().request_json)
known = dict(payload.get("known_arguments") or {})
print(json.dumps({"matched": True, "arguments": known, "issues": []}, ensure_ascii=False))
'''

INPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["amount"],
    "properties": {"amount": {"type": "number"}},
}
OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["value"],
    "properties": {"value": {"type": "number"}},
}
TEST_VECTORS = [{
    "name": "合成金额汇总",
    "text": "请计算合成金额 12.5 的汇总",
    "known_arguments": {"amount": 12.5},
    "expected_matched": True,
    "expected_arguments": {"amount": 12.5},
    "expected_issues": [],
    "expected_output": {"value": 12.5},
    "provider_fixtures": [],
}]


@pytest.fixture()
def manager(tmp_path: Path) -> WorkspaceManager:
    assets = tmp_path / "assets"
    (assets / "workspace-toolkit" / "templates").mkdir(parents=True)
    (assets / "workspace-rules.md").write_text("# rules", encoding="utf-8")
    (assets / "resource-template" / "others").mkdir(parents=True)
    return WorkspaceManager(tmp_path / "data", assets)


def _project(manager: WorkspaceManager) -> tuple[str, Path]:
    pid = manager.create_project("合成确定性任务")["id"]
    root = manager.project_root(pid)
    (root / "ontology" / "operation" / "计算汇总.md").write_text(
        "# 计算汇总\n\n# 输入\n\n| 参数名称 | 说明 |\n| --- | --- |\n| amount | 合成金额 |\n\n"
        "# 输出\n\n| 参数名称 | 说明 |\n| --- | --- |\n| value | 合成结果 |\n",
        encoding="utf-8",
    )
    (root / "ontology" / "scripts" / "calculate_summary.py").write_text(HANDLER, encoding="utf-8")
    manager.ensure_package_layout(pid)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "test: synthetic deterministic inputs")
    return pid, root


def _item(data_access="none") -> dict:
    return {
        "key": "calculate_summary",
        "operation": "计算汇总",
        "handler": "calculate_summary",
        "data_access": data_access,
        "aliases": ["汇总计算"],
        "routing": {"all_terms": ["汇总"], "any_terms": ["计算", "统计"]},
        "enabled": True,
        "parser_source": PARSER,
        "input_schema": json.dumps(INPUT_SCHEMA, ensure_ascii=False),
        "output_schema": json.dumps(OUTPUT_SCHEMA, ensure_ascii=False),
        "test_vectors": TEST_VECTORS,
    }


def _mark_verified(manager: WorkspaceManager, pid: str, root: Path, key: str = "calculate_summary") -> dict:
    check = deterministic_contract.check_at(root)
    item = next(value for value in check.contracts if value.key == key)
    return deterministic_contract.write_verification_report(
        manager,
        pid,
        key,
        deterministic_contract.source_revision(root, item),
        {
            "status": "passed",
            "runtime": "jiuwenbox",
            "passed": 1,
            "failed": 0,
            "cases": [{"name": "合成金额汇总", "ok": True, "stage": "handler", "reason": ""}],
        },
    )


def _sandbox_result(value, *, exit_code=0, stderr="") -> dict:
    stdout = json.dumps(value, ensure_ascii=False) if value is not None else ""
    return {
        "exit_code": exit_code,
        "timed_out": False,
        "stdout_is_json": value is not None,
        "stdout": stdout,
        "stderr": stderr,
        "duration_ms": 1,
    }


def test_payload_rejects_excessive_depth_before_persistence(manager: WorkspaceManager):
    _pid, root = _project(manager)
    item = _item()
    nested = []
    for _index in range(deterministic_contract.MAX_JSON_DEPTH + 1):
        nested = [nested]
    item["test_vectors"] = [{**TEST_VECTORS[0], "known_arguments": {"nested": nested}}]

    check = deterministic_contract.check_payload(root, [item])

    assert any("嵌套深度" in finding["message"] for finding in check.findings)


def test_strict_json_reports_depth_before_decoder_recursion_limit():
    source = "[" * 1_200 + "0" + "]" * 1_200

    with pytest.raises(ValueError, match="嵌套深度"):
        deterministic_contract._strict_json(source, "合成 JSON")  # pylint: disable=protected-access


@pytest.mark.parametrize("field", ["aliases", "parser_source", "test_vectors"])
def test_payload_rejects_invalid_unicode_with_field_error(
    manager: WorkspaceManager, field: str,
):
    _pid, root = _project(manager)
    item = _item()
    if field == "aliases":
        item[field] = ["synthetic\ud800"]
    elif field == "parser_source":
        item[field] = "# synthetic\ud800\n"
    else:
        item[field] = json.dumps(TEST_VECTORS, ensure_ascii=False).replace(
            "合成金额汇总", "synthetic\\ud800",
        )

    check = deterministic_contract.check_payload(root, [item])

    assert any("非法 Unicode" in finding["message"] for finding in check.findings)


def test_strict_test_vector_text_preserves_large_integer_and_rejects_duplicate_key(
    manager: WorkspaceManager,
):
    _pid, root = _project(manager)
    item = _item()
    item["test_vectors"] = json.dumps(TEST_VECTORS, ensure_ascii=False).replace("12.5", "9007199254740993")
    check = deterministic_contract.check_payload(root, [item])
    vectors, findings = deterministic_contract._test_vectors(check.contracts[0])  # pylint: disable=protected-access
    assert not findings
    assert vectors[0]["known_arguments"]["amount"] == 9007199254740993

    duplicate = _item()
    duplicate["test_vectors"] = '[{"name":"synthetic","name":"duplicate"}]'
    duplicate_check = deterministic_contract.check_payload(root, [duplicate])
    assert any("重复字段 name" in finding["message"] for finding in duplicate_check.findings)


def _rewrite_package(source: Path, dest: Path, mutate) -> Path:
    with zipfile.ZipFile(source) as package:
        members = {name: package.read(name) for name in package.namelist()}
    mutate(members)
    with zipfile.ZipFile(dest, "w") as package:
        for name, data in members.items():
            package.writestr(name, data)
    return dest


def _run_generated_parser(tmp_path: Path, source: str, payload: dict) -> dict:
    path = tmp_path / "generated_parser.py"
    path.write_text(source, encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(path), "--request-json", json.dumps(payload, ensure_ascii=False)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_candidate_scan_recommends_unique_structured_handler(manager: WorkspaceManager):
    _pid, root = _project(manager)

    result = deterministic_contract.candidate_scan(root)

    assert result["counts"] == {
        "recommended": 1, "needs_info": 0, "not_recommended": 0, "configured": 0,
    }
    assert result["candidates"] == [{
        "operation": "计算汇总",
        "recommendation": "recommended",
        "handler": "calculate_summary",
        "data_access": "none",
        "reasons": [
            "Operation 已唯一绑定有效 Handler",
            "输入参数可从 Handler 契约生成 Schema",
            "Handler 已声明结构化输出，可生成输出 Schema",
        ],
        "blockers": [],
        "confirmations": ["确认自然语言示例、字段表达方式和测试向量符合业务语义"],
        "can_create": True,
    }]


def test_candidate_scan_marks_missing_and_multiple_handlers(manager: WorkspaceManager):
    _pid, root = _project(manager)
    handler = root / "ontology" / "scripts" / "calculate_summary.py"
    handler.unlink()

    missing = deterministic_contract.candidate_scan(root)["candidates"][0]
    assert missing["recommendation"] == "needs_info"
    assert "缺少唯一" in missing["blockers"][0]

    handler.write_text(HANDLER, encoding="utf-8")
    (handler.parent / "calculate_summary_again.py").write_text(HANDLER, encoding="utf-8")
    multiple = deterministic_contract.candidate_scan(root)["candidates"][0]
    assert multiple["recommendation"] == "needs_info"
    assert "多个绑定" in multiple["blockers"][0]
    assert multiple["can_create"] is False


def test_candidate_scan_detects_provider_in_handler_dependency(manager: WorkspaceManager):
    _pid, root = _project(manager)
    scripts = root / "ontology" / "scripts"
    (scripts / "provider_helper.py").write_text(
        "from shared_capabilities.ontology_script_sdk import DeterministicProviderClient\n",
        encoding="utf-8",
    )
    handler = scripts / "calculate_summary.py"
    handler.write_text(HANDLER.replace("import json", "import json\nimport provider_helper"), encoding="utf-8")

    candidate = deterministic_contract.candidate_scan(root)["candidates"][0]

    assert candidate["data_access"] == "workspace_provider"
    assert any("工作空间 Provider" in reason for reason in candidate["reasons"])


def test_provider_capability_table_matches_consumer_broker():
    """Onto 的版本化白名单必须与当前 Consumer Broker 保持一致。"""
    source = Path(__file__).parents[4] / "consumer" / "backend" / "agent" / "direct_execution" / "provider_broker.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    assignment = next(
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "_OPERATION_ARGUMENTS" for target in node.targets)
    )
    assert isinstance(assignment.value, ast.Dict)
    consumer = {}
    for key, value in zip(assignment.value.keys, assignment.value.values):
        assert isinstance(value, ast.Call) and len(value.args) == 1
        consumer[ast.literal_eval(key)] = frozenset(ast.literal_eval(value.args[0]))

    assert deterministic_contract.PROVIDER_OPERATION_ARGUMENTS == consumer


def test_candidate_draft_is_editable_and_does_not_write_workspace(
    manager: WorkspaceManager, tmp_path: Path,
):
    _pid, root = _project(manager)
    before = sorted(path.relative_to(root) for path in root.rglob("*"))

    result = deterministic_contract.candidate_draft(root, "计算汇总")

    assert sorted(path.relative_to(root) for path in root.rglob("*")) == before
    draft = result["draft"]
    assert draft["key"] == "calculate_summary"
    assert json.loads(draft["input_schema"])["required"] == ["amount"]
    assert json.loads(draft["output_schema"])["required"] == ["value"]
    assert len(draft["test_vectors"]) == 3
    assert draft["test_vectors"][0]["expected_output"] == {"value": 1.5}
    assert draft["test_vectors"][1]["expected_issues"] == [
        {"field": "amount", "message": "请提供合成金额，必填"},
    ]
    assert draft["test_vectors"][2]["known_arguments"] == {"amount": 1.5}
    assert any("占位值" in notice for notice in result["notices"])
    payload = {key: value for key, value in draft.items() if key != "verification"}
    assert not deterministic_contract.check_payload(root, [payload]).findings

    parser = draft["parser_source"]
    parsed = _run_generated_parser(tmp_path, parser, {
        "text": "计算汇总，amount为99.5", "known_arguments": {"amount": 12.5},
    })
    assert parsed == {"matched": True, "arguments": {"amount": 12.5}, "issues": []}

    missing = _run_generated_parser(tmp_path, parser, {
        "text": "计算汇总", "known_arguments": {},
    })
    assert missing == {
        "matched": True, "arguments": {},
        "issues": [{"field": "amount", "message": "请提供合成金额，必填"}],
    }
    assert not deterministic_verification._parser_result(missing)  # pylint: disable=protected-access

    routed_by_alias = _run_generated_parser(tmp_path, parser, {
        "text": "汇总计算，amount为3.5", "known_arguments": {},
    })
    assert routed_by_alias == {"matched": True, "arguments": {"amount": 3.5}, "issues": []}

    described_field = _run_generated_parser(tmp_path, parser, {
        "text": "请计算，合成金额为6.5", "known_arguments": {},
    })
    assert described_field == {"matched": True, "arguments": {"amount": 6.5}, "issues": []}


def test_candidate_draft_rejects_changed_scan_decisions(manager: WorkspaceManager):
    _pid, root = _project(manager)

    with pytest.raises(WorkspaceError) as exc:
        deterministic_contract.candidate_draft(root, "计算汇总", data_access="workspace_provider")

    assert exc.value.code == "deterministic_candidate_changed"


def _agent_proposal(root: Path, item: dict | None = None) -> Path:
    item = _item() if item is None else item
    sources = [
        deterministic_contract._operation_path(root, item["operation"]),
        *deterministic_contract._handler_dependencies(root, item["handler"]),
        *deterministic_contract._parser_dependencies(root, item["parser_source"].encode("utf-8")),
    ]
    proposal = {
        "schema_version": "deterministic-draft.v1", "draft": item,
        "sources": {
            path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sources
        },
    }
    path = root / f"ext/deterministic-execution/drafts/{item['handler']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(proposal, ensure_ascii=False), encoding="utf-8")
    return path


def test_agent_proposal_is_read_only_and_never_trusts_verification(manager: WorkspaceManager):
    _pid, root = _project(manager)
    path = _agent_proposal(root)
    proposal = json.loads(path.read_text(encoding="utf-8"))
    proposal["draft"]["verification"] = {"status": "passed", "current": True}
    path.write_text(json.dumps(proposal), encoding="utf-8")
    result = deterministic_contract.candidate_draft(root, "计算汇总")
    assert result["draft_origin"] == "agent"
    assert result["draft"]["test_vectors"] == TEST_VECTORS
    assert result["draft"]["verification"] == {"status": "missing", "current": False}
    assert not (root / deterministic_contract.CONTRACT_DIR).exists()


@pytest.mark.parametrize("structured_role", ["input", "output"])
def test_agent_proposal_accepts_explicit_structured_schema(manager: WorkspaceManager, structured_role):
    _pid, root = _project(manager)
    handler = root / "ontology/scripts/calculate_summary.py"
    field = "amount" if structured_role == "input" else "value"
    source = HANDLER.replace(f"{field}    number", f"{field}    object")
    if structured_role == "input":
        source = source.replace("type=float", "type=json.loads")
        source = source.replace('"value": args.amount', '"value": args.amount["total"]')
    else:
        source = source.replace('"value": args.amount', '"value": {"total": args.amount}')
    handler.write_text(source, encoding="utf-8")
    path = _agent_proposal(root)
    proposal = json.loads(path.read_text(encoding="utf-8"))
    schema = json.loads(proposal["draft"][f"{structured_role}_schema"])
    schema["properties"][field] = {
        "type": "object", "required": ["total"], "additionalProperties": False,
        "properties": {"total": {"type": "number"}},
    }
    proposal["draft"][f"{structured_role}_schema"] = schema
    vector = dict(TEST_VECTORS[0])
    if structured_role == "input":
        vector["known_arguments"] = {field: {"total": 12.5}}
        vector["expected_arguments"] = {field: {"total": 12.5}}
    else:
        vector["expected_output"] = {field: {"total": 12.5}}
    proposal["draft"]["test_vectors"] = [vector]
    path.write_text(json.dumps(proposal, ensure_ascii=False), encoding="utf-8")

    assert deterministic_contract.candidate_scan(root)["candidates"][0]["can_create"] is False
    result = deterministic_contract.candidate_draft(root, "计算汇总")
    assert result["draft_origin"] == "agent"
    assert result["draft"]["verification"]["status"] == "missing"
    assert not (root / deterministic_contract.CONTRACT_DIR).exists()

    path.unlink()
    with pytest.raises(WorkspaceError) as exc:
        deterministic_contract.candidate_draft(root, "计算汇总")
    assert exc.value.code == "deterministic_candidate_incomplete"


def test_agent_proposal_does_not_bypass_handler_sandbox_check(manager: WorkspaceManager):
    _pid, root = _project(manager)
    (root / "ontology/scripts/calculate_summary.py").write_text(
        HANDLER.replace("import json", "import json\nimport subprocess"), encoding="utf-8",
    )
    _agent_proposal(root)
    with pytest.raises(WorkspaceError) as exc:
        deterministic_contract.candidate_draft(root, "计算汇总")
    assert exc.value.code == "deterministic_draft_invalid"
    assert any("subprocess" in item["message"] for item in exc.value.details["findings"])


def test_agent_proposal_returns_all_findings(manager: WorkspaceManager):
    _pid, root = _project(manager)
    path = _agent_proposal(root)
    proposal = json.loads(path.read_text(encoding="utf-8"))
    proposal["draft"]["test_vectors"] = [
        {**TEST_VECTORS[0], "name": f"case-{index}", "expected_issues": ["invalid"]}
        for index in range(6)
    ]
    path.write_text(json.dumps(proposal), encoding="utf-8")
    with pytest.raises(WorkspaceError) as exc:
        deterministic_contract.candidate_draft(root, "计算汇总")
    messages = [finding["message"] for finding in exc.value.details["findings"]]
    assert all(any(f"test_vectors[{index}].expected_issues" in message for message in messages) for index in range(6))


@pytest.mark.parametrize("issues,known,conflict", [
    ([{"field": "intent", "message": "请分开提问，本次不执行"}], {}, False),
    ([], {}, True),
    ([{"field": "quantity", "message": "请提供数量"}], {"quantity": 2}, True),
    ([{"field": " intent ", "message": "请分开提问"}], {}, False),
    ([{}], {}, True),
    (["invalid"], {}, True),
    ([{"field": None}], {}, True),
    ([{"field": " "}], {}, True),
    (None, {}, True),
    ([{"field": "intent"}], None, True),
])
def test_agent_preflight_preserves_compound_rejection_examples(issues, known, conflict):
    from app.services.deterministic_drafts import _routing_findings

    vector = {
        "name": "复合原句", "text": "请报价并查询库存", "expected_matched": True,
        "known_arguments": known, "expected_issues": issues,
    }
    candidates = [
        SimpleNamespace(
            operation=name, enabled=True, routing_all_terms=[], routing_any_terms=[term],
            tests=json.dumps([vector]),
        )
        for name, term in [("费用核算", "报价"), ("数量查询", "库存")]
    ]

    findings = _routing_findings(candidates)

    assert bool(findings) is conflict
    if conflict:
        assert all(item["code"] == "deterministic_draft_routing_ambiguous" for item in findings)


def test_agent_preflight_detects_cross_operation_routes_without_executing_code():
    from app.services.deterministic_drafts import _routing_findings

    def candidate(operation, all_terms, any_terms, text, enabled=True):
        return SimpleNamespace(
            operation=operation, enabled=enabled,
            routing_all_terms=all_terms, routing_any_terms=any_terms,
            tests=json.dumps([{"name": "用户原句", "text": text, "expected_matched": True}]),
        )

    fee = candidate("费用核算", [], ["租"], "租5小时，单价120分，押金1000分")
    query = candidate("数量查询", ["可租"], ["多少", "数量"], "现在有多少电钻可租？")
    findings = _routing_findings([fee, query])
    assert len(findings) == 1
    assert findings[0]["code"] == "deterministic_draft_routing_ambiguous"
    assert "费用核算、数量查询" in findings[0]["message"]
    fee.routing_any_terms = ["小时", "押金"]
    assert _routing_findings([fee, query]) == []
    fee.routing_any_terms = ["租"]
    query.enabled = False
    assert _routing_findings([fee, query]) == []
    query.enabled = True
    query.tests = json.dumps([{"name": "否定", "text": "不要执行可租数量查询", "expected_matched": True}])
    assert _routing_findings([fee, query]) == []


@pytest.mark.parametrize("known,issues,valid", [
    ({"amount": "invalid"}, [{"field": "amount", "message": "需要数值"}], False),
    ({"amount": 12.5}, [{"field": "amount", "message": "需要数值"}], True),
    ({}, [{"field": "amount", "message": "需要数值"}], True),
])
def test_agent_preflight_checks_override_semantics(manager: WorkspaceManager, known, issues, valid):
    _pid, root = _project(manager)
    path = _agent_proposal(root)
    proposal = json.loads(path.read_text(encoding="utf-8"))
    proposal["draft"]["test_vectors"] = [TEST_VECTORS[0], {
        **TEST_VECTORS[0], "name": "synthetic override", "known_arguments": known,
        "expected_arguments": {}, "expected_issues": issues,
    }]
    path.write_text(json.dumps(proposal), encoding="utf-8")
    if valid:
        assert deterministic_contract.candidate_draft(root, "计算汇总")["draft_origin"] == "agent"
    else:
        with pytest.raises(WorkspaceError) as exc:
            deterministic_contract.candidate_draft(root, "计算汇总")
        assert exc.value.details["findings"][0]["code"] == "deterministic_draft_expectation_invalid"
    assert not (root / deterministic_contract.CONTRACT_DIR).exists()


def test_agent_preflight_cli_uses_platform_checker_without_writes(manager: WorkspaceManager):
    _pid, root = _project(manager)
    path = _agent_proposal(root)
    before = path.read_bytes()
    script = Path(__file__).parents[1] / "scripts/onto-deterministic-check.py"
    result = subprocess.run(
        [sys.executable, str(script), "--operation", "计算汇总"],
        cwd=root, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "ready": True, "findings": [], "sandbox_verified": False,
        "message": "平台预检查通过；仍需用户确认保存并运行真实沙箱验证",
    }
    assert path.read_bytes() == before
    assert not (root / deterministic_contract.CONTRACT_DIR).exists()


def test_agent_proposal_can_repair_existing_contract_without_saving(manager: WorkspaceManager):
    pid, root = _project(manager)
    deterministic_contract.save_contracts(manager, pid, [_item()], "")
    _mark_verified(manager, pid, root)
    before = deterministic_contract.contract_view(root)
    path = _agent_proposal(root)
    proposal = json.loads(path.read_text(encoding="utf-8"))
    proposal["draft"]["aliases"] = ["业务汇总"]
    path.write_text(json.dumps(proposal), encoding="utf-8")
    result = deterministic_contract.candidate_draft(root, "计算汇总")
    assert result["draft"]["aliases"] == ["业务汇总"]
    assert result["draft"]["verification"] == {"status": "missing", "current": False}
    assert deterministic_contract.contract_view(root) == before
    payload = {key: value for key, value in result["draft"].items() if key != "verification"}
    updated = deterministic_contract.save_contracts(manager, pid, [payload], before["revision"])
    assert updated["operations"][0]["aliases"] == ["业务汇总"]
    assert not updated["operations"][0]["verification"]["current"]


@pytest.fixture()
def reimported_contracts(manager: WorkspaceManager, tmp_path: Path):
    """Published synthetic packages omit tests; both operations need Agent repair."""
    pid, root = _project(manager)
    (root / "ontology/object/合成记录.md").write_text("# 合成记录\n", encoding="utf-8")
    operation = (root / "ontology/operation/计算汇总.md").read_text(encoding="utf-8")
    (root / "ontology/operation/计算次项.md").write_text(
        operation.replace("计算汇总", "计算次项"), encoding="utf-8",
    )
    (root / "ontology/scripts/calculate_secondary.py").write_text(
        HANDLER.replace("计算汇总", "计算次项"), encoding="utf-8",
    )
    second = {
        **_item(), "key": "calculate_secondary", "operation": "计算次项",
        "handler": "calculate_secondary", "aliases": ["次项计算"],
        "routing": {"all_terms": [], "any_terms": ["次项"]},
        "test_vectors": [{**TEST_VECTORS[0], "text": "请计算次项 12.5"}],
    }
    items = [_item(), second]
    deterministic_contract.save_contracts(manager, pid, items, "")
    for item in items:
        _mark_verified(manager, pid, root, item["key"])
    archive = tmp_path / "synthetic-multiple-operations.zip"
    publish_service.export_package_zip(manager, pid, archive)
    imported = manager.import_package_zip("合成多操作回导", archive)
    imported_root = manager.project_root(imported["id"])
    for item in items:
        _agent_proposal(imported_root, item)
    return imported["id"], imported_root, items


@pytest.mark.parametrize("reverse", [False, True])
def test_reimported_operations_can_accumulate_repairs_before_strict_save(
    manager: WorkspaceManager, reimported_contracts, reverse: bool,
):
    pid, root, items = reimported_contracts
    before = deterministic_contract.contract_view(root)
    assert len(before["operations"]) == 2
    assert all(item["test_vectors"] == [] for item in before["operations"])
    managed = root / deterministic_contract.CONTRACT_DIR
    original = {path: path.read_bytes() for path in managed.rglob("*") if path.is_file()}
    repairs = []
    for item in reversed(items) if reverse else items:
        result = deterministic_contract.candidate_draft(root, item["operation"])
        assert result["draft_origin"] == "agent"
        assert result["draft"]["verification"] == {"status": "missing", "current": False}
        repairs.append({key: value for key, value in result["draft"].items() if key != "verification"})
        if len(repairs) == 1:
            remaining = [
                {key: value for key, value in other.items() if key != "verification"}
                for other in before["operations"] if other["operation"] != item["operation"]
            ]
            with pytest.raises(deterministic_contract.DeterministicContractRejected):
                deterministic_contract.save_contracts(manager, pid, [*repairs, *remaining], before["revision"])
    assert {path: path.read_bytes() for path in managed.rglob("*") if path.is_file()} == original
    assert deterministic_contract.contract_view(root) == before
    saved = deterministic_contract.save_contracts(manager, pid, repairs, before["revision"])
    assert all(item["test_vectors"] for item in saved["operations"])
    assert all(not item["verification"]["current"] for item in saved["operations"])
    assert not deterministic_contract.check_at(root).findings
    # Reading/saving repairs must not manufacture sandbox evidence or permit release.
    with pytest.raises(WorkspaceError):
        deterministic_contract.ensure_publishable(root)


@pytest.mark.parametrize("filename,content", [
    ("tests.json", "not json"), ("tests.json", '{}'), ("tests.json", '[null, {}]'),
    ("tests.json", '[{"name":"invalid","text":42}]'),
    ("parser.py", "import os\n"), ("input.schema.json", "not json"),
])
def test_other_incomplete_contract_does_not_block_reading_a_repair(
    manager: WorkspaceManager, reimported_contracts, filename: str, content: str,
):
    pid, root, _items = reimported_contracts
    target = root / deterministic_contract.CONTRACT_DIR / "calculate_secondary" / filename
    assert target.is_file()
    target.write_text(content, encoding="utf-8")
    result = deterministic_contract.candidate_draft(root, "计算汇总")
    assert result["draft_origin"] == "agent"
    assert target.read_text(encoding="utf-8") == content
    before = deterministic_contract.contract_view(root)
    payload = [
        {key: value for key, value in item.items() if key != "verification"}
        for item in before["operations"] if item["operation"] != "计算汇总"
    ]
    payload.append({key: value for key, value in result["draft"].items() if key != "verification"})
    with pytest.raises(deterministic_contract.DeterministicContractRejected):
        deterministic_contract.save_contracts(manager, pid, payload, before["revision"])


@pytest.mark.parametrize("conflict", ["alias", "operation", "route", "reverse_route"])
def test_repair_read_still_rejects_cross_operation_conflicts(reimported_contracts, conflict: str):
    _pid, root, items = reimported_contracts
    first, second = items
    if conflict == "alias":
        first["aliases"] = second["aliases"]
    elif conflict == "operation":
        first["aliases"] = [second["operation"]]
    elif conflict == "route":
        first["test_vectors"] = [{**TEST_VECTORS[0], "text": "请计算汇总与次项"}]
    else:
        # Only the existing contract's example reveals this overlap.
        first["routing"] = {"all_terms": [], "any_terms": ["计算"]}
        target = root / deterministic_contract.CONTRACT_DIR / second["key"] / "tests.json"
        target.write_text(json.dumps(second["test_vectors"]), encoding="utf-8")
    _agent_proposal(root, first)
    with pytest.raises(WorkspaceError) as exc:
        deterministic_contract.candidate_draft(root, first["operation"])
    assert exc.value.code == "deterministic_draft_invalid"
    findings = exc.value.details["findings"]
    if conflict in {"route", "reverse_route"}:
        assert any(item["code"] == "deterministic_draft_routing_ambiguous" for item in findings)
    else:
        assert any(item.get("field") == "aliases" for item in findings)


def test_agent_repair_rejects_key_change_and_missing_proposal(manager: WorkspaceManager):
    pid, root = _project(manager)
    deterministic_contract.save_contracts(manager, pid, [_item()], "")
    with pytest.raises(WorkspaceError, match="尚无助手新版草稿"):
        deterministic_contract.candidate_draft(root, "计算汇总")
    path = _agent_proposal(root)
    proposal = json.loads(path.read_text(encoding="utf-8"))
    proposal["draft"]["key"] = "another_key"
    path.write_text(json.dumps(proposal), encoding="utf-8")
    with pytest.raises(WorkspaceError, match="保留已有契约标识"):
        deterministic_contract.candidate_draft(root, "计算汇总")


@pytest.mark.parametrize("field,value", [
    ("operation", "其他业务"), ("handler", "other_handler"),
    ("data_access", "workspace_provider"), ("parser_source", "import os\n"),
    ("test_vectors", []), ("key", "../escape"),
])
def test_agent_proposal_rejects_invalid_contract(manager: WorkspaceManager, field, value):
    _pid, root = _project(manager)
    path = _agent_proposal(root)
    proposal = json.loads(path.read_text(encoding="utf-8"))
    proposal["draft"][field] = value
    path.write_text(json.dumps(proposal), encoding="utf-8")
    with pytest.raises(WorkspaceError) as exc:
        deterministic_contract.candidate_draft(root, "计算汇总")
    assert exc.value.code == "deterministic_draft_invalid"


@pytest.mark.parametrize("relative", [
    "ontology/operation/计算汇总.md", "ontology/scripts/calculate_summary.py",
])
def test_agent_proposal_rejects_stale_sources(manager: WorkspaceManager, relative):
    _pid, root = _project(manager)
    _agent_proposal(root)
    path = root / relative
    path.write_bytes(path.read_bytes() + b"\n# changed\n")
    with pytest.raises(WorkspaceError, match="已变化"):
        deterministic_contract.candidate_draft(root, "计算汇总")


@pytest.mark.parametrize("mode", ["symlink", "oversize", "invalid_json", "missing_sources"])
def test_agent_proposal_rejects_unsafe_or_incomplete_file(manager: WorkspaceManager, mode):
    from app.services.deterministic_drafts import MAX_DRAFT_BYTES

    _pid, root = _project(manager)
    path = _agent_proposal(root)
    if mode == "symlink":
        target = root / "proposal.json"
        path.rename(target)
        path.symlink_to(target)
    elif mode == "oversize":
        path.write_bytes(b" " * (MAX_DRAFT_BYTES + 1))
    elif mode == "invalid_json":
        path.write_text("{", encoding="utf-8")
    else:
        value = json.loads(path.read_text(encoding="utf-8"))
        value["sources"] = {}
        path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(WorkspaceError) as exc:
        deterministic_contract.candidate_draft(root, "计算汇总")
    assert exc.value.code == "deterministic_draft_invalid"


def test_provider_candidate_draft_requires_explicit_synthetic_fixtures(manager: WorkspaceManager):
    _pid, root = _project(manager)
    handler = root / "ontology" / "scripts" / "calculate_summary.py"
    handler.write_text(
        HANDLER.replace(
            "import json", "import json\nfrom shared_capabilities.ontology_script_sdk import DeterministicProviderClient",
        ),
        encoding="utf-8",
    )

    result = deterministic_contract.candidate_draft(root, "计算汇总")

    assert result["draft"]["data_access"] == "workspace_provider"
    assert any("Provider fixture" in notice for notice in result["notices"])


@pytest.mark.parametrize(
    ("operation", "arguments", "message"),
    [
        ("unsupported_query", {}, "不在 Consumer Provider"),
        ("count_search", {"unsupported": True}, "不支持的参数"),
    ],
)
def test_provider_fixtures_follow_consumer_allowlist(
    manager: WorkspaceManager, operation: str, arguments: dict, message: str,
):
    _pid, root = _project(manager)
    item = _item("workspace_provider")
    item["test_vectors"] = [{
        **TEST_VECTORS[0],
        "provider_fixtures": [{"operation": operation, "arguments": arguments, "result": {}}],
    }]

    findings = deterministic_contract.check_payload(root, [item]).findings

    assert any(message in finding["message"] for finding in findings)


def test_provider_fixture_does_not_change_later_vector_fields(manager: WorkspaceManager):
    _pid, root = _project(manager)
    item = _item("workspace_provider")
    item["test_vectors"] = [
        {
            **TEST_VECTORS[0],
            "name": f"synthetic provider case {index}",
            "provider_fixtures": ([{
                "operation": "count_search", "arguments": {}, "result": {"count": 1},
            }] if index == 0 else []),
        }
        for index in range(3)
    ]

    check = deterministic_contract.check_payload(root, [item])

    assert check.findings == []


def test_invalid_provider_fixture_in_first_vector_returns_finding(manager: WorkspaceManager):
    _pid, root = _project(manager)
    item = _item("workspace_provider")
    item["test_vectors"] = [
        {
            **TEST_VECTORS[0],
            "name": f"synthetic provider case {index}",
            "provider_fixtures": ([{
                "operation": "unsupported_query", "arguments": {}, "result": {},
            }] if index == 0 else []),
        }
        for index in range(3)
    ]

    findings = deterministic_contract.check_payload(root, [item]).findings

    assert any("Consumer Provider" in finding["message"] for finding in findings)


def test_none_data_access_rejects_provider_fixtures(manager: WorkspaceManager):
    _pid, root = _project(manager)
    item = _item()
    item["test_vectors"] = [{
        **TEST_VECTORS[0],
        "provider_fixtures": [{"operation": "count_search", "arguments": {}, "result": {}}],
    }]

    findings = deterministic_contract.check_payload(root, [item]).findings

    assert any("data_access:none" in finding["message"] for finding in findings)


@pytest.mark.parametrize(
    ("fixture", "message"),
    [
        ({"operation": "get_nodes_schema", "result": {}}, "arguments"),
        (
            {"operation": " get_nodes_schema ", "arguments": {}, "result": {}},
            "operation 不能包含首尾空格",
        ),
    ],
)
def test_provider_fixture_requires_canonical_execution_shape(
    manager: WorkspaceManager, fixture: dict, message: str,
):
    pid, root = _project(manager)
    item = _item("workspace_provider")
    item["test_vectors"] = [{**TEST_VECTORS[0], "provider_fixtures": [fixture]}]

    findings = deterministic_contract.check_payload(root, [item]).findings

    assert any(message in finding["message"] for finding in findings)
    with pytest.raises(deterministic_contract.DeterministicContractRejected, match=message):
        deterministic_contract.save_contracts(manager, pid, [item], "")


def test_provider_fixture_accepts_explicit_empty_arguments(manager: WorkspaceManager):
    _pid, root = _project(manager)
    item = _item("workspace_provider")
    item["test_vectors"] = [{
        **TEST_VECTORS[0],
        "provider_fixtures": [{"operation": "get_nodes_schema", "arguments": {}, "result": {}}],
    }]

    assert deterministic_contract.check_payload(root, [item]).findings == []


def test_provider_stub_rejects_unknown_calls_and_missing_fixture(tmp_path: Path):
    stub = tmp_path / "provider_stub.py"
    stub.write_bytes(deterministic_verification._provider_stub([  # pylint: disable=protected-access
        {"operation": "count_search", "arguments": {"element_type": "synthetic"}, "result": {"count": 1}},
    ]))
    runner = tmp_path / "run_provider_stub.py"
    runner.write_text(
        """import json
import sys
from provider_stub import DeterministicProviderCallError, DeterministicProviderClient

operation, arguments = sys.argv[1], json.loads(sys.argv[2])
try:
    print(json.dumps({"result": DeterministicProviderClient().execute(operation, arguments)}))
except DeterministicProviderCallError as exc:
    print(json.dumps({"code": exc.code}))
""",
        encoding="utf-8",
    )

    def call(operation: str, arguments: dict) -> dict:
        completed = subprocess.run(
            [sys.executable, str(runner), operation, json.dumps(arguments)],
            check=True, capture_output=True, text=True,
        )
        return json.loads(completed.stdout)

    assert call("count_search", {"element_type": "synthetic"}) == {"result": {"count": 1}}
    assert call("unsupported_query", {}) == {"code": "provider_arguments_invalid"}
    assert call("count_search", {}) == {"code": "provider_query_failed"}


@pytest.mark.parametrize(
    ("parameters", "arguments", "expected", "reason"),
    [
        ([{"name": "value", "flag": "--value", "type": "string", "required": "是"}],
         {"value": None}, [], "Handler 必填参数 value 缺失"),
        ([{"name": "value", "flag": "--value", "type": "string", "required": "否"}],
         {"value": None}, [], ""),
        ([{"name": "value", "flag": "--value", "type": "string", "required": "是"}],
         {"value": {"x": 1}}, ["--value", '{"x":1}'], ""),
        ([{"name": "value", "flag": "--value", "type": "json", "required": "是"}],
         {"value": '{"x":1}'}, ["--value", '{"x":1}'], ""),
        ([{"name": "enabled", "flag": "--enabled", "type": "boolean", "required": "否",
           "action": "store_true"}], {"enabled": "true"}, ["--enabled"], ""),
        ([{"name": "disabled", "flag": "--disabled", "type": "boolean", "required": "否",
           "action": "store_false"}], {"disabled": "false"}, ["--disabled"], ""),
        ([{"name": "tag", "flag": "--tag", "type": "string", "required": "否",
           "multiple": "是"}], {"tag": ["a", None]}, ["--tag", "a"], ""),
        ([{"name": "amount", "flag": "--amount", "type": "integer", "required": "是"}],
         {"amount": 12.0}, ["--amount", "12"], ""),
        ([{"name": "ratio", "flag": "--ratio", "type": "number", "required": "是"}],
         {"ratio": 12.5}, ["--ratio", "12.5"], ""),
        ([{"name": "enabled", "flag": "--enabled", "type": "boolean", "required": "是"}],
         {"enabled": True}, ["--enabled", "true"], ""),
        ([{"name": "tag", "flag": "--tag", "type": "string", "required": "是",
           "multiple": "是"}], {"tag": ["a", "b"]}, ["--tag", "a", "--tag", "b"], ""),
    ],
)
def test_handler_argv_matches_consumer_parameter_normalization(
    monkeypatch: pytest.MonkeyPatch, parameters, arguments, expected, reason,
):
    class Contract:
        params = parameters

    monkeypatch.setattr(deterministic_verification._script_contract, "parse_script", lambda _source: Contract())
    argv, actual_reason = deterministic_verification._handler_argv(  # pylint: disable=protected-access
        "synthetic", arguments,
    )

    assert argv == expected
    assert actual_reason == reason


@pytest.mark.parametrize("other_name", ["calculate", "Ｃａｌｃｕｌａｔｅ", "Calculate  "])
def test_contract_rejects_normalized_operation_name_collision(
    manager: WorkspaceManager, other_name: str,
):
    _pid, root = _project(manager)
    original = root / "ontology" / "operation" / "计算汇总.md"
    original.rename(root / "ontology" / "operation" / "Calculate.md")
    (root / "ontology" / "operation" / f"{other_name}.md").write_text(
        f"# {other_name}\n", encoding="utf-8",
    )
    item = {**_item(), "operation": "Calculate", "aliases": []}

    findings = deterministic_contract.check_payload(root, [item]).findings

    assert any("与另一个 Operation" in finding["message"] for finding in findings)


def test_contract_allows_distinct_operation_name(manager: WorkspaceManager):
    _pid, root = _project(manager)
    original = root / "ontology" / "operation" / "计算汇总.md"
    original.rename(root / "ontology" / "operation" / "Calculate.md")
    (root / "ontology" / "operation" / "different.md").write_text("# different\n", encoding="utf-8")
    item = {**_item(), "operation": "Calculate", "aliases": []}

    findings = deterministic_contract.check_payload(root, [item]).findings

    assert not any("与另一个 Operation" in finding["message"] for finding in findings)


def test_basic_parser_extracts_supported_scalar_types_and_preserves_known_values(tmp_path: Path):
    source = deterministic_contract._basic_parser_source(  # pylint: disable=protected-access
        "生成摘要",
        [
            {"name": "region", "type": "string", "required": "是", "desc": "区域"},
            {"name": "year", "type": "integer", "required": "是", "desc": "年份"},
            {"name": "ratio", "type": "number", "required": "是", "desc": "比率"},
            {"name": "enabled", "type": "boolean", "required": "否", "desc": "是否启用"},
        ],
    )

    result = _run_generated_parser(tmp_path, source, {
        "text": "生成摘要 region为华东 year=2026 ratio:1.25e2 enabled为关闭",
        "known_arguments": {"year": 2025},
    })

    assert result == {
        "matched": True,
        "arguments": {"year": 2025, "region": "华东", "ratio": 125.0, "enabled": False},
        "issues": [],
    }

    fullwidth = _run_generated_parser(tmp_path, source, {
        "text": "生成摘要，region为华东，year=２０２６，ratio=1", "known_arguments": {},
    })
    assert fullwidth["matched"] is True


@pytest.mark.parametrize(
    ("text", "field", "message"),
    [
        ("生成摘要，year为1e3", "year", "年份格式不正确"),
        ("生成摘要，ratio为12.5.6", "ratio", "比率格式不正确"),
        ("生成摘要，enabled为falsehood", "enabled", "是否启用格式不正确"),
        ("生成摘要，year为1,000", "year", "年份格式不正确"),
        ("生成摘要，ratio为1,234,567", "ratio", "比率格式不正确"),
        ("生成摘要，ratio为1,000.25", "ratio", "比率格式不正确"),
        ("生成摘要，ratio为1e309", "ratio", "比率格式不正确"),
    ],
)
def test_basic_parser_rejects_malformed_scalar_values_without_truncating(
    tmp_path: Path, text: str, field: str, message: str,
):
    source = deterministic_contract._basic_parser_source(  # pylint: disable=protected-access
        "生成摘要",
        [
            {"name": "year", "type": "integer", "required": "否", "desc": "年份"},
            {"name": "ratio", "type": "number", "required": "否", "desc": "比率"},
            {"name": "enabled", "type": "boolean", "required": "否", "desc": "是否启用"},
        ],
    )

    result = _run_generated_parser(tmp_path, source, {"text": text, "known_arguments": {}})

    assert field not in result["arguments"]
    assert result["issues"] == [{"field": field, "message": message}]


def test_basic_parser_allows_trailing_whitespace_around_scalar_values(tmp_path: Path):
    source = deterministic_contract._basic_parser_source(  # pylint: disable=protected-access
        "生成摘要",
        [
            {"name": "year", "type": "integer", "required": "是", "desc": "年份"},
            {"name": "ratio", "type": "number", "required": "是", "desc": "比率"},
        ],
    )

    result = _run_generated_parser(tmp_path, source, {
        "text": "生成摘要，year为10   ，ratio为12.5   ", "known_arguments": {},
    })

    assert result == {
        "matched": True, "arguments": {"year": 10, "ratio": 12.5}, "issues": [],
    }


def test_candidate_scan_requires_custom_schema_for_structured_output(manager: WorkspaceManager):
    _pid, root = _project(manager)
    handler = root / "ontology" / "scripts" / "calculate_summary.py"
    handler.write_text(HANDLER.replace(
        "value    number    合成结果",
        'value    object    合成结果，例：{"total": 1}',
    ), encoding="utf-8")

    candidate = deterministic_contract.candidate_scan(root)["candidates"][0]

    assert candidate["recommendation"] == "needs_info"
    assert any("结构化输出" in blocker for blocker in candidate["blockers"])
    assert candidate["can_create"] is False


def test_candidate_scan_does_not_recommend_handler_outside_sandbox_contract(manager: WorkspaceManager):
    _pid, root = _project(manager)
    handler = root / "ontology" / "scripts" / "calculate_summary.py"
    handler.write_text(HANDLER.replace("import json", "import json\nimport subprocess"), encoding="utf-8")

    candidate = deterministic_contract.candidate_scan(root)["candidates"][0]

    assert candidate["recommendation"] == "needs_info"
    assert any("不能导入 subprocess" in blocker for blocker in candidate["blockers"])
    assert candidate["can_create"] is False


@pytest.mark.parametrize("data_access", ["none", "workspace_provider"])
def test_save_and_render_deterministic_contract(manager: WorkspaceManager, tmp_path: Path, data_access: str):
    pid, root = _project(manager)
    saved = deterministic_contract.save_contracts(manager, pid, [_item(data_access)], "", author="operator_a")
    _mark_verified(manager, pid, root)

    assert saved["revision"].startswith("sha256:")
    assert saved["operations"][0]["data_access"] == data_access
    contract_text = (root / deterministic_contract.CONTRACT_DIR / "calculate_summary" / "contract.json").read_text(
        encoding="utf-8"
    )
    assert "neo4j" not in contract_text.casefold()

    archive = tmp_path / f"{data_access}.zip"
    publish_service.export_package_zip(manager, pid, archive)
    with zipfile.ZipFile(archive) as package:
        operation = package.read(f"{pid}/operations/计算汇总.md").decode("utf-8")
        descriptor = package.read(f"{pid}/scripts/calculate_summary.md").decode("utf-8")
        manifest = package.read(f"{pid}/manifest.yaml").decode("utf-8")
        assert "schema_version: '0.1'" in manifest
        assert 'type: "deterministic"' in operation
        assert 'parser: "scripts/calculate_summary_parser.py"' in operation
        assert f"data_access: {data_access}" in descriptor
        assert package.read(f"{pid}/scripts/calculate_summary_parser.py").decode("utf-8") == PARSER
        assert json.loads(package.read(f"{pid}/schemas/calculate_summary.input.json")) == INPUT_SCHEMA


@pytest.mark.parametrize("data_access", ["none", "workspace_provider"])
def test_exported_package_passes_consumer_install_validation(
    manager: WorkspaceManager, tmp_path: Path, data_access: str,
):
    """发布产物须通过 Consumer 正式安装校验并生成安装态 lock。"""
    pid, root = _project(manager)
    deterministic_contract.save_contracts(manager, pid, [_item(data_access)], "")
    _mark_verified(manager, pid, root)
    archive = tmp_path / f"consumer-{data_access}.zip"
    publish_service.export_package_zip(manager, pid, archive)
    extracted = tmp_path / f"installed-{data_access}"
    with zipfile.ZipFile(archive) as package:
        package.extractall(extracted)

    consumer_backend = Path(__file__).parents[4] / "consumer" / "backend"
    sys.path.insert(0, str(consumer_backend))
    try:
        from ontology.operation_execution import (  # pylint: disable=import-outside-toplevel
            DETERMINISTIC_LOCK_NAME,
            load_deterministic_operation_specs,
            validate_and_lock_deterministic_operations,
        )

        installed = extracted / pid
        validate_and_lock_deterministic_operations(installed)
        specs = load_deterministic_operation_specs(installed, require_lock=True)
    finally:
        sys.path.remove(str(consumer_backend))

    assert len(specs) == 1
    assert specs[0].operation_key == "计算汇总"
    assert specs[0].handler.data_access == data_access
    assert specs[0].parser_path == "scripts/calculate_summary_parser.py"
    assert (installed / DETERMINISTIC_LOCK_NAME).is_file()


def test_generated_parser_runs_through_consumer_execution_service(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch,
):
    """Consumer 无需改代码即可路由、调用 Onto Parser 并执行 Handler。"""
    pid, root = _project(manager)
    draft = deterministic_contract.candidate_draft(root, "计算汇总")["draft"]
    draft["aliases"] = ["汇总计算"]
    draft["routing"] = {"all_terms": ["合成"], "any_terms": ["汇总请求"]}
    payload = {key: value for key, value in draft.items() if key != "verification"}
    deterministic_contract.save_contracts(manager, pid, [payload], "")
    _mark_verified(manager, pid, root)
    archive = publish_service.export_package_zip(manager, pid, tmp_path / "consumer-execution.zip")
    extracted = tmp_path / "consumer-execution"
    with zipfile.ZipFile(archive) as package:
        package.extractall(extracted)
    installed = extracted / pid

    consumer_backend = Path(__file__).parents[4] / "consumer" / "backend"
    sys.path.insert(0, str(consumer_backend))
    try:
        from agent.direct_execution.contracts import (  # pylint: disable=import-outside-toplevel
            AwaitingInput,
            Completed,
            DeterministicRequest,
        )
        from agent.direct_execution.models import RuntimeContext  # pylint: disable=import-outside-toplevel
        from agent.direct_execution.service import (  # pylint: disable=import-outside-toplevel
            DeterministicExecutionService,
        )
        from agent.tools.execute_script import (  # pylint: disable=import-outside-toplevel
            handle_execute_script_path as local_runner,
        )
        from ontology.operation_execution import (  # pylint: disable=import-outside-toplevel
            validate_and_lock_deterministic_operations,
        )
        import agent.direct_execution.service as service_module  # pylint: disable=import-outside-toplevel

        validate_and_lock_deterministic_operations(installed)
        monkeypatch.setattr(service_module, "runtime_revision", lambda: "synthetic-runtime-revision")
        monkeypatch.setattr(service_module, "register_run_sandbox_companion", lambda *_args: None)
        monkeypatch.setattr(service_module, "release_run_sandbox", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            service_module,
            "handle_execute_script_path",
            lambda **kwargs: local_runner(**{**kwargs, "sandbox_required": False}),
        )
        request = DeterministicRequest(
            user_request="请执行合成汇总请求，amount为12.5",
            context={},
            arguments={},
            runtime_context=RuntimeContext(
                session_id="session-synthetic", run_id="run-synthetic", turn_id=1,
                user_id="operator_a", workspace_id="workspace_demo",
                selected_ontology_id=pid, selected_ontology_version="0.1.0",
            ),
        )
        outcome = DeterministicExecutionService().execute(
            request,
            enabled=True,
            project_root=str(extracted),
            timeout_seconds=30,
            ontology_manifest=SimpleNamespace(id=pid, version="0.1.0", ontology_dir=str(installed)),
        )
        alias_outcome = DeterministicExecutionService().execute(
            DeterministicRequest(
                user_request="汇总计算",
                context={},
                arguments={"amount": 8.5},
                runtime_context=RuntimeContext(
                    session_id="session-synthetic", run_id="run-alias", turn_id=2,
                    user_id="operator_a", workspace_id="workspace_demo",
                    selected_ontology_id=pid, selected_ontology_version="0.1.0",
                ),
            ),
            enabled=True,
            project_root=str(extracted),
            timeout_seconds=30,
            ontology_manifest=SimpleNamespace(id=pid, version="0.1.0", ontology_dir=str(installed)),
        )
        missing_outcome = DeterministicExecutionService().execute(
            DeterministicRequest(
                user_request="请执行合成汇总请求",
                context={},
                arguments={},
                runtime_context=RuntimeContext(
                    session_id="session-synthetic", run_id="run-missing", turn_id=3,
                    user_id="operator_a", workspace_id="workspace_demo",
                    selected_ontology_id=pid, selected_ontology_version="0.1.0",
                ),
            ),
            enabled=True,
            project_root=str(extracted),
            timeout_seconds=30,
            ontology_manifest=SimpleNamespace(id=pid, version="0.1.0", ontology_dir=str(installed)),
        )
    finally:
        sys.path.remove(str(consumer_backend))

    assert isinstance(outcome, Completed)
    assert outcome.operation_key == "计算汇总"
    assert outcome.envelope["structured_result"] == {"value": 12.5}
    assert isinstance(alias_outcome, Completed)
    assert alias_outcome.envelope["structured_result"] == {"value": 8.5}
    assert isinstance(missing_outcome, AwaitingInput)
    assert missing_outcome.interaction["metadata"]["field"] == "amount"


def test_contract_rejects_concrete_provider_field(manager: WorkspaceManager):
    _pid, root = _project(manager)
    item = {**_item("workspace_provider"), "provider": {"kind": "neo4j"}}
    check = deterministic_contract.check_payload(root, [item])

    assert any("具体数据源和连接配置不能进入建模契约" in finding["message"] for finding in check.findings)


def test_contract_rejects_external_schema_reference(manager: WorkspaceManager):
    _pid, root = _project(manager)
    item = _item()
    item["input_schema"] = json.dumps({"type": "object", "properties": {"x": {"$ref": "common.json"}}})

    check = deterministic_contract.check_payload(root, [item])

    assert any("必须自包含" in finding["message"] for finding in check.findings)


@pytest.mark.parametrize("field", ["workspace_id", "selected_ontology_version", "workspace-id"])
def test_contract_rejects_consumer_runtime_fields_in_input_schema(
    manager: WorkspaceManager, field: str,
):
    _pid, root = _project(manager)
    item = _item()
    item["input_schema"] = json.dumps({
        "type": "object",
        "allOf": [{"properties": {field: {"type": "string"}}}],
    })

    check = deterministic_contract.check_payload(root, [item])

    assert any(
        "Consumer 保留的运行时参数" in finding["message"] and field in finding["message"]
        for finding in check.findings
    )


def test_contract_does_not_treat_one_schema_branch_as_always_required(manager: WorkspaceManager):
    _pid, root = _project(manager)
    item = _item()
    item["input_schema"] = json.dumps({
        "type": "object",
        "properties": {"amount": {"type": "number"}},
        "oneOf": [
            {"required": ["amount"]},
            {"properties": {"amount": {"maximum": 10}}},
        ],
    })

    check = deterministic_contract.check_payload(root, [item])

    assert any("Handler 必填参数必须在输入 Schema required 中声明" in finding["message"]
               for finding in check.findings)


def test_contract_accepts_handler_requirement_shared_by_all_schema_branches(manager: WorkspaceManager):
    _pid, root = _project(manager)
    item = _item()
    item["input_schema"] = json.dumps({
        "type": "object",
        "properties": {"amount": {"type": "number"}},
        "oneOf": [
            {"required": ["amount"], "properties": {"amount": {"maximum": 10}}},
            {"required": ["amount"], "properties": {"amount": {"minimum": 11}}},
        ],
    })

    check = deterministic_contract.check_payload(root, [item])

    assert not any("Handler 必填参数必须在输入 Schema required 中声明" in finding["message"]
                   for finding in check.findings)


@pytest.mark.parametrize(
    ("issues", "expected"),
    [
        ([{"message": "缺少参数"}], "issue.field"),
        ([{"field": "workspace_id"}], "Consumer 保留的运行时参数"),
    ],
)
def test_parser_issue_contract_matches_consumer(issues: list[dict], expected: str):
    result = deterministic_verification._parser_result({  # pylint: disable=protected-access
        "matched": True,
        "arguments": {},
        "issues": issues,
    })

    assert expected in result


def test_contract_rejects_test_issue_without_consumer_required_field(manager: WorkspaceManager):
    _pid, root = _project(manager)
    item = _item()
    item["test_vectors"] = [{
        **TEST_VECTORS[0],
        "expected_issues": [{"message": "缺少合成金额"}],
    }]

    check = deterministic_contract.check_payload(root, [item])

    assert any("expected_issues 每项都必须包含非空字符串 field" in finding["message"]
               for finding in check.findings)


@pytest.mark.parametrize("issues", [True, 1, -1, 1.5, None, "invalid", {"field": "amount"}])
def test_contract_reports_invalid_expected_issues_type(manager: WorkspaceManager, issues):
    _pid, root = _project(manager)
    item = _item()
    item["test_vectors"] = [{**TEST_VECTORS[0], "expected_issues": issues}]

    check = deterministic_contract.check_payload(root, [item])

    assert any("expected_issues 必须是 object 数组" in finding["message"] for finding in check.findings)
    with pytest.raises(deterministic_contract.DeterministicContractRejected, match="expected_issues"):
        deterministic_contract.save_contracts(manager, _pid, [item], "")


@pytest.mark.parametrize(
    "field",
    ["known_arguments", "expected_arguments", "expected_output", "provider_fixtures"],
)
def test_contract_rejects_numeric_overflow_before_save(manager: WorkspaceManager, field: str):
    _pid, root = _project(manager)
    item = _item("workspace_provider" if field == "provider_fixtures" else "none")
    vector = json.dumps(TEST_VECTORS, ensure_ascii=False)
    if field == "provider_fixtures":
        replacement = (
            '"provider_fixtures": [{"operation": "count_search", '
            '"arguments": {}, "result": {"count": 1e309}}]'
        )
        vector = vector.replace('"provider_fixtures": []', replacement)
    else:
        original = json.dumps(TEST_VECTORS[0][field], ensure_ascii=False)
        replacement = '{"amount": 1e309}' if field != "expected_output" else '{"value": 1e309}'
        vector = vector.replace(f'"{field}": {original}', f'"{field}": {replacement}')
    item["test_vectors"] = vector

    check = deterministic_contract.check_payload(root, [item])

    assert any("超出有限范围" in finding["message"] for finding in check.findings)
    with pytest.raises(deterministic_contract.DeterministicContractRejected, match="超出有限范围"):
        deterministic_contract.save_contracts(manager, _pid, [item], "")


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        ({"type": "object", "properties": {"x": {"$ref": "#/$defs/missing"}}}, "无法解析"),
        ({"type": "object", "properties": {"x": {"$dynamicRef": "common.json"}}}, "必须自包含"),
        ({"type": "object", "properties": {"x": {"$id": "nested", "type": "string"}}}, "嵌套 $id"),
    ],
)
def test_contract_schema_matches_consumer_local_reference_rules(
    manager: WorkspaceManager, schema: dict, expected: str,
):
    _pid, root = _project(manager)
    item = _item()
    item["input_schema"] = json.dumps(schema)

    check = deterministic_contract.check_payload(root, [item])

    assert any(expected in finding["message"] for finding in check.findings)


def test_contract_rejects_local_reference_to_non_schema_value(manager: WorkspaceManager):
    _pid, root = _project(manager)
    item = _item()
    item["input_schema"] = json.dumps({
        "type": "object",
        "properties": {"amount": {"$ref": "#/$defs/scalar/type"}},
        "required": ["amount"],
        "$defs": {"scalar": {"type": "number"}},
    })

    findings = deterministic_contract.check_payload(root, [item]).findings

    assert any("object 或 boolean Schema" in finding["message"] for finding in findings)


def test_contract_rejects_non_consuming_local_reference_cycle(manager: WorkspaceManager):
    _pid, root = _project(manager)
    item = _item()
    item["input_schema"] = json.dumps({
        "type": "object",
        "properties": {"amount": {"$ref": "#/$defs/loop"}},
        "required": ["amount"],
        "$defs": {"loop": {"$ref": "#/$defs/loop"}},
    })

    findings = deterministic_contract.check_payload(root, [item]).findings

    assert any("本地引用循环" in finding["message"] for finding in findings)


def test_contract_accepts_recursive_schema_that_consumes_instance_structure(manager: WorkspaceManager):
    _pid, root = _project(manager)
    item = _item()
    item["input_schema"] = json.dumps({
        "type": "object",
        "properties": {
            "amount": {"type": "number"},
            "child": {"$ref": "#/$defs/node"},
        },
        "required": ["amount"],
        "$defs": {
            "node": {
                "type": "object",
                "properties": {"child": {"$ref": "#/$defs/node"}},
            },
        },
    })

    findings = deterministic_contract.check_payload(root, [item]).findings

    assert not any("本地引用" in finding["message"] for finding in findings)


@pytest.mark.parametrize(
    ("field", "source", "expected"),
    [
        (
            "parser_source",
            PARSER.replace('parser.add_argument("--request-json", required=True)\n', "")
            + "\n# --request-json 不能代替 argparse 声明\n",
            "必须声明 --request-json",
        ),
        ("handler", HANDLER.replace("import json", "import json\nvalue = getattr(json, 'loads')"), "不能调用 getattr"),
        (
            "handler",
            HANDLER.replace("import json", "import json\nvalue = object.__subclasses__"),
            "不能访问 __subclasses__",
        ),
    ],
)
def test_contract_rejects_python_that_consumer_would_reject(
    manager: WorkspaceManager, field: str, source: str, expected: str,
):
    _pid, root = _project(manager)
    item = _item()
    if field == "parser_source":
        item[field] = source.replace('parser.add_argument("--request-json", required=True)\n', "")
    else:
        (root / "ontology" / "scripts" / "calculate_summary.py").write_text(source, encoding="utf-8")

    check = deterministic_contract.check_payload(root, [item])

    assert any(expected in finding["message"] for finding in check.findings)


def test_contract_rejects_handler_dependency_outside_consumer_runtime(manager: WorkspaceManager):
    _pid, root = _project(manager)
    handler = (root / "ontology" / "scripts" / "calculate_summary.py")
    handler.write_text(HANDLER.replace("import json", "import json\nimport yaml"), encoding="utf-8")

    check = deterministic_contract.check_payload(root, [_item()])

    assert any("不在 Consumer 沙箱依赖契约中" in finding["message"] for finding in check.findings)


def test_contract_rejects_schema_field_without_handler_parameter(manager: WorkspaceManager):
    _pid, root = _project(manager)
    item = _item()
    schema = dict(INPUT_SCHEMA)
    schema["properties"] = {**INPUT_SCHEMA["properties"], "region": {"type": "string"}}
    item["input_schema"] = json.dumps(schema)

    check = deterministic_contract.check_payload(root, [item])

    assert any("没有对应的 Handler 参数：region" in finding["message"] for finding in check.findings)


def test_contract_requires_handler_required_parameter_in_schema(manager: WorkspaceManager):
    _pid, root = _project(manager)
    item = _item()
    schema = dict(INPUT_SCHEMA)
    schema["required"] = []
    item["input_schema"] = json.dumps(schema)

    check = deterministic_contract.check_payload(root, [item])

    assert any("Handler 必填参数必须在输入 Schema required 中声明：amount" in finding["message"]
               for finding in check.findings)


@pytest.mark.parametrize("data_access", ["none", "workspace_provider"])
def test_parser_cannot_import_consumer_runtime_capabilities(manager: WorkspaceManager, data_access: str):
    _pid, root = _project(manager)
    item = _item(data_access)
    item["parser_source"] = PARSER.replace(
        "import json",
        "import json\nfrom shared_capabilities.ontology_script_sdk import DeterministicProviderClient",
    )

    check = deterministic_contract.check_payload(root, [item])

    assert any("不能使用运行时能力" in finding["message"] for finding in check.findings)


def test_workspace_provider_handler_can_import_restricted_client(manager: WorkspaceManager):
    _pid, root = _project(manager)
    handler = (root / "ontology" / "scripts" / "calculate_summary.py")
    handler.write_text(HANDLER.replace(
        "import json",
        "import json\nfrom shared_capabilities.ontology_script_sdk import DeterministicProviderClient",
    ), encoding="utf-8")

    check = deterministic_contract.check_payload(root, [_item("workspace_provider")])

    assert not check.findings


@pytest.mark.parametrize("data_access", ["none", "workspace_provider"])
def test_parser_local_dependency_cannot_use_provider_client(manager: WorkspaceManager, data_access: str):
    _pid, root = _project(manager)
    (root / "ontology" / "scripts" / "parser_helper.py").write_text(
        "from shared_capabilities.ontology_script_sdk import DeterministicProviderClient\n",
        encoding="utf-8",
    )
    item = _item(data_access)
    item["parser_source"] = PARSER.replace("import json", "import json\nimport parser_helper")

    check = deterministic_contract.check_payload(root, [item])

    assert any("不能使用运行时能力" in finding["message"] for finding in check.findings)


def test_handler_local_dependency_is_checked_for_nondeterminism(manager: WorkspaceManager):
    _pid, root = _project(manager)
    (root / "ontology" / "scripts" / "helper.py").write_text(
        "def dynamic(value):\n    return getattr(value, 'real', value)\n", encoding="utf-8",
    )
    handler = root / "ontology" / "scripts" / "calculate_summary.py"
    handler.write_text(HANDLER.replace("import json", "import json\nimport helper"), encoding="utf-8")

    check = deterministic_contract.check_payload(root, [_item()])

    assert any("不能调用 getattr" in finding["message"] for finding in check.findings)


def test_contract_rejects_operation_alias_collision(manager: WorkspaceManager):
    _pid, root = _project(manager)
    (root / "ontology" / "operation" / "统计汇总.md").write_text("# 统计汇总\n", encoding="utf-8")
    (root / "ontology" / "scripts" / "statistics.py").write_text(
        HANDLER.replace("关联operation：计算汇总", "关联operation：统计汇总"), encoding="utf-8",
    )
    first = _item()
    second = {
        **_item(), "key": "statistics", "operation": "统计汇总", "handler": "statistics",
        "aliases": ["汇总计算"],
    }

    check = deterministic_contract.check_payload(root, [first, second])

    assert any("同时指向" in finding["message"] for finding in check.findings)


def test_contract_rejects_alias_colliding_with_plain_operation(manager: WorkspaceManager):
    _pid, root = _project(manager)
    (root / "ontology" / "operation" / "普通统计.md").write_text("# 普通统计\n", encoding="utf-8")

    check = deterministic_contract.check_payload(root, [{**_item(), "aliases": ["普通统计"]}])

    assert any("与另一个 Operation" in finding["message"] for finding in check.findings)


def test_contract_normalizes_unicode_before_duplicate_term_check(manager: WorkspaceManager):
    _pid, root = _project(manager)
    item = _item()
    item["routing"] = {"all_terms": [], "any_terms": ["ＡＢＣ", "abc"]}

    check = deterministic_contract.check_payload(root, [item])

    assert any("空值或重复值" in finding["message"] for finding in check.findings)


@pytest.mark.parametrize("handler", ["../outside", "nested/handler", "handler.py", "a" * 129])
def test_contract_rejects_handler_path_instead_of_script_name(manager: WorkspaceManager, handler: str):
    _pid, root = _project(manager)

    check = deterministic_contract.check_payload(root, [{**_item(), "handler": handler}])

    assert any("Python 标识符" in finding["message"] for finding in check.findings)


def test_contract_rejects_parser_publish_path_collision(manager: WorkspaceManager):
    _pid, root = _project(manager)
    (root / "ontology" / "scripts" / "calculate_summary_parser.py").write_text(
        "value = 1\n", encoding="utf-8",
    )

    check = deterministic_contract.check_payload(root, [_item()])

    assert any("发布态 Parser 会与脚本" in finding["message"] for finding in check.findings)


@pytest.mark.parametrize(
    ("patch", "field"),
    [
        ({"aliases": "汇总"}, "aliases"),
        ({"routing": []}, "routing"),
        ({"routing": {"all_terms": "汇总", "any_terms": []}}, "routing.all_terms"),
        ({"enabled": "false"}, "enabled"),
        ({"parser_source": {"source": PARSER}}, "parser_source"),
    ],
)
def test_contract_rejects_payload_shape_instead_of_silently_coercing(
    manager: WorkspaceManager, patch: dict, field: str,
):
    _pid, root = _project(manager)

    check = deterministic_contract.check_payload(root, [{**_item(), **patch}])

    assert any(finding.get("field") == field for finding in check.findings)


def test_contract_and_solver_are_mutually_exclusive(manager: WorkspaceManager):
    pid, root = _project(manager)
    deterministic_contract.save_contracts(manager, pid, [_item()], "")

    with pytest.raises(Exception) as exc:
        deterministic_contract.ensure_publishable(root, solver_enabled=True)

    assert getattr(exc.value, "code", "") == deterministic_contract.CODE_CONFLICT


def test_flow_completion_requires_current_verified_contract(manager: WorkspaceManager):
    pid, root = _project(manager)

    assert deterministic_contract.flow_complete(root) is False
    deterministic_contract.save_contracts(manager, pid, [_item()], "")
    assert deterministic_contract.flow_complete(root) is False
    _mark_verified(manager, pid, root)
    assert deterministic_contract.flow_complete(root) is True

    handler = root / "ontology" / "scripts" / "calculate_summary.py"
    handler.write_text(HANDLER.replace("合成汇总。", "合成汇总结果。"), encoding="utf-8")
    assert deterministic_contract.flow_complete(root) is False


def test_deterministic_save_rejects_enabled_solver_and_allows_disabled_draft(
    manager: WorkspaceManager,
):
    from app.services import solver as solver_service  # pylint: disable=import-outside-toplevel

    pid, root = _project(manager)
    solver_item = {
        "binding": {
            "solver_key": "summary_solver", "operation": "计算汇总", "decision_model": "summary_model",
            "declaration": {}, "operation_frontmatter": {"execution": {"enabled": True}}, "units": [],
        },
        "sidecars": {
            "entry": "id: summary_model\n",
            "input_schema": '{"type":"object","properties":{}}\n',
            "result_schema": '{"type":"object","properties":{}}\n',
            "fact_snapshot_schema": '{"type":"object","properties":{}}\n',
            "presentation": "columns: []\n",
        },
    }
    solver_service.save_solver_contract(manager, pid, [solver_item], "")

    _agent_proposal(root)
    with pytest.raises(WorkspaceError) as draft_error:
        deterministic_contract.candidate_draft(root, "计算汇总")
    assert draft_error.value.code == "deterministic_candidate_incomplete"

    checked = deterministic_contract.check_payload_with_conflicts(root, [_item()])
    assert checked.findings[0]["code"] == deterministic_contract.CODE_CONFLICT

    with pytest.raises(deterministic_contract.DeterministicContractRejected) as exc:
        deterministic_contract.save_contracts(manager, pid, [_item()], "")
    assert exc.value.findings[0]["code"] == deterministic_contract.CODE_CONFLICT

    disabled = {**_item(), "enabled": False}
    saved = deterministic_contract.save_contracts(manager, pid, [disabled], "")
    assert saved["enabled"] is False


def test_enabled_contract_requires_current_passed_verification_before_publish(
    manager: WorkspaceManager, tmp_path: Path,
):
    pid, root = _project(manager)
    deterministic_contract.save_contracts(manager, pid, [_item()], "")

    with pytest.raises(Exception) as exc:
        publish_service.export_package_zip(manager, pid, tmp_path / "unverified.zip")

    assert getattr(exc.value, "code", "") == deterministic_contract.CODE_UNVERIFIED
    _mark_verified(manager, pid, root)
    publish_service.export_package_zip(manager, pid, tmp_path / "verified.zip")


def test_publish_check_reports_unverified_contract_only_in_deterministic_gate(
    manager: WorkspaceManager,
):
    pid, _root = _project(manager)
    deterministic_contract.save_contracts(manager, pid, [_item()], "")

    check = publish_service.package_binding_check(manager, pid)
    deterministic_gate = next(gate for gate in check["gates"] if gate["id"] == "deterministic_contract")
    structure_gate = next(gate for gate in check["gates"] if gate["id"] == "package_structure")

    assert deterministic_gate["blocking"] is True
    assert deterministic_gate["findings"][0]["code"] == deterministic_contract.CODE_UNVERIFIED
    assert structure_gate["blocking"] is False
    assert not any(
        finding.get("code") == "package_render_failed"
        for gate in check["gates"] for finding in gate["findings"]
    )


@pytest.mark.parametrize(
    "changed", ["operation", "handler", "parser", "parser_dependency", "schema", "tests"],
)
def test_verification_report_becomes_stale_when_execution_closure_changes(
    manager: WorkspaceManager, changed: str,
):
    pid, root = _project(manager)
    saved = deterministic_contract.save_contracts(manager, pid, [_item()], "")
    _mark_verified(manager, pid, root)
    assert deterministic_contract.contract_view(root)["operations"][0]["verification"]["current"] is True

    if changed == "operation":
        path = root / "ontology" / "operation" / "计算汇总.md"
        path.write_text(path.read_text(encoding="utf-8") + "\n补充说明。\n", encoding="utf-8")
    elif changed == "handler":
        path = root / "ontology" / "scripts" / "calculate_summary.py"
        path.write_text(path.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    elif changed == "parser_dependency":
        helper = root / "ontology" / "scripts" / "parser_helper.py"
        helper.write_text("VALUE = 1\n", encoding="utf-8")
        item = _item()
        item["parser_source"] = PARSER.replace("import json", "import json\nimport parser_helper")
        deterministic_contract.save_contracts(manager, pid, [item], saved["revision"])
        _mark_verified(manager, pid, root)
        helper.write_text("VALUE = 2\n", encoding="utf-8")
    else:
        item = _item()
        if changed == "parser":
            item["parser_source"] += "\n# changed\n"
        elif changed == "schema":
            item["input_schema"] = json.dumps({**INPUT_SCHEMA, "title": "changed"}, ensure_ascii=False)
        else:
            item["test_vectors"] = [{**TEST_VECTORS[0], "text": "另一个合成请求"}]
        deterministic_contract.save_contracts(manager, pid, [item], saved["revision"])

    status = deterministic_contract.contract_view(root)["operations"][0]["verification"]
    assert status["current"] is False
    finding = deterministic_contract.release_status_at(root)["findings"][0]
    assert finding["code"] == deterministic_contract.CODE_UNVERIFIED


def test_verification_runs_parser_and_handler_only_through_gateway(
    manager: WorkspaceManager, monkeypatch: pytest.MonkeyPatch,
):
    pid, root = _project(manager)
    saved = deterministic_contract.save_contracts(manager, pid, [_item()], "")
    entries = []

    def run_in_gateway(_bundle, entry, cases):
        entries.append((entry, cases))
        if entry.endswith("_parser.py"):
            return [_sandbox_result({
                "matched": True, "arguments": {"amount": 12.5}, "issues": [],
            })]
        return [_sandbox_result({"value": 12.5}), _sandbox_result({"value": 12.5})]

    monkeypatch.setattr(deterministic_verification._sandbox, "run_in_gateway", run_in_gateway)
    source_revision, report = deterministic_verification.verify(root, "calculate_summary", saved["revision"])

    assert source_revision.startswith("sha256:")
    assert report["status"] == "passed"
    assert report["cases"] == [{"name": "合成金额汇总", "ok": True, "stage": "handler", "reason": ""}]
    assert [entry for entry, _cases in entries] == ["calculate_summary_parser.py", "calculate_summary.py"]
    assert len(entries[1][1]) == 2


@pytest.mark.parametrize("vector_count", [8, 9, 16])
def test_verification_batches_repeated_handlers_within_gateway_limit(
    manager: WorkspaceManager, monkeypatch: pytest.MonkeyPatch, vector_count: int,
):
    pid, root = _project(manager)
    item = _item()
    item["test_vectors"] = [
        {**TEST_VECTORS[0], "name": f"synthetic-{index}",
         "known_arguments": {"amount": index + 0.5},
         "expected_arguments": {"amount": index + 0.5},
         "expected_output": {"value": index + 0.5}}
        for index in range(vector_count)
    ]
    saved = deterministic_contract.save_contracts(manager, pid, [item], "")
    handler_batches = []

    def run_in_gateway(_bundle, entry, cases):
        assert 1 <= len(cases) <= 16
        if entry.endswith("_parser.py"):
            return [_sandbox_result({
                "matched": True, "arguments": json.loads(case[1])["known_arguments"], "issues": [],
            }) for case in cases]
        handler_batches.append(cases)
        return [_sandbox_result({"value": float(case[1])}) for case in cases]

    monkeypatch.setattr(deterministic_verification._sandbox, "run_in_gateway", run_in_gateway)
    _revision, report = deterministic_verification.verify(root, "calculate_summary", saved["revision"])

    assert report["status"] == "passed"
    assert report["passed"] == vector_count
    assert [case["name"] for case in report["cases"]] == [f"synthetic-{i}" for i in range(vector_count)]
    assert [len(batch) for batch in handler_batches] == ([16] if vector_count == 8 else [16, 2 * vector_count - 16])
    for batch in handler_batches:
        assert all(batch[index] == batch[index + 1] for index in range(0, len(batch), 2))


def test_verification_rejects_invalid_handler_output(
    manager: WorkspaceManager, monkeypatch: pytest.MonkeyPatch,
):
    pid, root = _project(manager)
    saved = deterministic_contract.save_contracts(manager, pid, [_item()], "")

    def run_in_gateway(_bundle, entry, _cases):
        if entry.endswith("_parser.py"):
            return [_sandbox_result({
                "matched": True, "arguments": {"amount": 12.5}, "issues": [],
            })]
        return [_sandbox_result({"unexpected": 12.5}), _sandbox_result({"unexpected": 12.5})]

    monkeypatch.setattr(deterministic_verification._sandbox, "run_in_gateway", run_in_gateway)
    _source_revision, report = deterministic_verification.verify(root, "calculate_summary", saved["revision"])

    assert report["status"] == "failed"
    assert report["cases"][0]["stage"] == "handler"
    assert "输出不符合 Schema" in report["cases"][0]["reason"]


def test_verification_rejects_nondeterministic_handler_result(
    manager: WorkspaceManager, monkeypatch: pytest.MonkeyPatch,
):
    pid, root = _project(manager)
    saved = deterministic_contract.save_contracts(manager, pid, [_item()], "")

    def run_in_gateway(_bundle, entry, _cases):
        if entry.endswith("_parser.py"):
            return [_sandbox_result({
                "matched": True, "arguments": {"amount": 12.5}, "issues": [],
            })]
        return [_sandbox_result({"value": 12.5}), _sandbox_result({"value": 13})]

    monkeypatch.setattr(deterministic_verification._sandbox, "run_in_gateway", run_in_gateway)
    _source_revision, report = deterministic_verification.verify(root, "calculate_summary", saved["revision"])

    assert report["status"] == "failed"
    assert "重复执行结果不一致" in report["cases"][0]["reason"]


def test_json_equality_distinguishes_boolean_from_number_recursively():
    equal = deterministic_contract._json_equal  # pylint: disable=protected-access

    assert equal({"value": 0}, {"value": 0.0})
    assert equal({"nested": [None, "0", {"value": True}]}, {"nested": [None, "0", {"value": True}]})
    assert not equal({"value": 0}, {"value": False})
    assert not equal({"nested": [{"value": 1}]}, {"nested": [{"value": True}]})
    assert not equal([1, 0], [0, 1])


@pytest.mark.parametrize("branch", ["repeated", "expected"])
def test_verification_rejects_boolean_number_confusion(
    manager: WorkspaceManager, monkeypatch: pytest.MonkeyPatch, branch: str,
):
    pid, root = _project(manager)
    item = _item()
    item["output_schema"] = json.dumps({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["value"],
        "properties": {"value": {}},
        "additionalProperties": False,
    })
    item["test_vectors"] = [{**TEST_VECTORS[0], "expected_output": {"value": False}}]
    saved = deterministic_contract.save_contracts(manager, pid, [item], "")

    def run_in_gateway(_bundle, entry, _cases):
        if entry.endswith("_parser.py"):
            return [_sandbox_result({
                "matched": True, "arguments": {"amount": 12.5}, "issues": [],
            })]
        second = {"value": False} if branch == "repeated" else {"value": 0}
        return [_sandbox_result({"value": 0}), _sandbox_result(second)]

    monkeypatch.setattr(deterministic_verification._sandbox, "run_in_gateway", run_in_gateway)
    _source_revision, report = deterministic_verification.verify(root, "calculate_summary", saved["revision"])

    assert report["status"] == "failed"
    expected = "重复执行结果不一致" if branch == "repeated" else "输出与预期不一致"
    assert expected in report["cases"][0]["reason"]


def test_provider_stub_distinguishes_boolean_from_number(tmp_path: Path):
    stub = tmp_path / "deterministic_provider.py"
    stub.write_bytes(deterministic_verification._provider_stub([  # pylint: disable=protected-access
        {"operation": "count_search", "arguments": {"filter_dict": {"flag": False}}, "result": {"count": 1}},
    ]))
    namespace = {}
    exec(compile(stub.read_bytes(), str(stub), "exec"), namespace)  # pylint: disable=exec-used
    client = namespace["DeterministicProviderClient"]()

    assert client.execute("count_search", {"filter_dict": {"flag": False}}) == {"count": 1}
    with pytest.raises(namespace["DeterministicProviderCallError"]):
        client.execute("count_search", {"filter_dict": {"flag": 0}})


def test_verification_rejects_non_standard_json_output(
    manager: WorkspaceManager, monkeypatch: pytest.MonkeyPatch,
):
    pid, root = _project(manager)
    saved = deterministic_contract.save_contracts(manager, pid, [_item()], "")

    def run_in_gateway(_bundle, entry, _cases):
        if entry.endswith("_parser.py"):
            return [{
                **_sandbox_result(None), "exit_code": 0, "stdout_is_json": True,
                "stdout": '{"matched":true,"arguments":{"amount":NaN},"issues":[]}',
            }]
        raise AssertionError("Parser 输出非严格 JSON 时不应执行 Handler")

    monkeypatch.setattr(deterministic_verification._sandbox, "run_in_gateway", run_in_gateway)
    _source_revision, report = deterministic_verification.verify(root, "calculate_summary", saved["revision"])

    assert report["status"] == "failed"
    assert "不是严格 JSON" in report["cases"][0]["reason"]


def test_verification_rejects_numeric_overflow_in_parser_output(
    manager: WorkspaceManager, monkeypatch: pytest.MonkeyPatch,
):
    pid, root = _project(manager)
    saved = deterministic_contract.save_contracts(manager, pid, [_item()], "")

    def run_in_gateway(_bundle, entry, _cases):
        if entry.endswith("_parser.py"):
            return [{
                **_sandbox_result(None), "exit_code": 0, "stdout_is_json": True,
                "stdout": '{"matched":true,"arguments":{"amount":1e309},"issues":[]}',
            }]
        raise AssertionError("numeric overflow must not reach the Handler")

    monkeypatch.setattr(deterministic_verification._sandbox, "run_in_gateway", run_in_gateway)
    _source_revision, report = deterministic_verification.verify(root, "calculate_summary", saved["revision"])

    assert report["status"] == "failed"
    assert report["cases"][0]["stage"] == "parser"
    assert "不是严格 JSON" in report["cases"][0]["reason"]


def test_verification_enforces_json_schema_formats(
    manager: WorkspaceManager, monkeypatch: pytest.MonkeyPatch,
):
    pid, root = _project(manager)
    item = _item()
    item["input_schema"] = json.dumps({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["amount"],
        "properties": {"amount": {"type": "string", "format": "date"}},
    })
    item["test_vectors"] = [{
        **TEST_VECTORS[0],
        "known_arguments": {"amount": "not-a-date"},
        "expected_arguments": {"amount": "not-a-date"},
        "expected_output": {"value": 12.5},
    }]
    saved = deterministic_contract.save_contracts(manager, pid, [item], "")

    def run_in_gateway(_bundle, entry, _cases):
        if entry.endswith("_parser.py"):
            return [_sandbox_result({
                "matched": True, "arguments": {"amount": "not-a-date"}, "issues": [],
            })]
        raise AssertionError("输入 format 无效时不应执行 Handler")

    monkeypatch.setattr(deterministic_verification._sandbox, "run_in_gateway", run_in_gateway)
    _source_revision, report = deterministic_verification.verify(root, "calculate_summary", saved["revision"])

    assert report["status"] == "failed"
    assert report["cases"][0]["stage"] == "input_schema"


def test_verification_checks_explicit_argument_that_resolves_parser_issue(
    manager: WorkspaceManager, monkeypatch: pytest.MonkeyPatch,
):
    pid, root = _project(manager)
    item = _item()
    item["test_vectors"] = [{
        **TEST_VECTORS[0],
        "known_arguments": {"amount": "not-a-number"},
        "expected_arguments": {},
        "expected_issues": [{"field": "amount", "message": "synthetic missing amount"}],
        "expected_output": {"value": 12.5},
    }]
    saved = deterministic_contract.save_contracts(manager, pid, [item], "")

    def run_in_gateway(_bundle, entry, _cases):
        if entry.endswith("_parser.py"):
            return [_sandbox_result({
                "matched": True,
                "arguments": {},
                "issues": [{"field": "amount", "message": "synthetic missing amount"}],
            })]
        raise AssertionError("invalid explicit argument must not reach the Handler")

    monkeypatch.setattr(deterministic_verification._sandbox, "run_in_gateway", run_in_gateway)
    _source_revision, report = deterministic_verification.verify(root, "calculate_summary", saved["revision"])

    assert report["status"] == "failed"
    assert report["cases"][0]["stage"] == "input_schema"


def test_verification_runs_handler_after_explicit_argument_resolves_parser_issue(
    manager: WorkspaceManager, monkeypatch: pytest.MonkeyPatch,
):
    pid, root = _project(manager)
    item = _item()
    item["test_vectors"] = [{
        **TEST_VECTORS[0],
        "expected_arguments": {},
        "expected_issues": [{"field": "amount", "message": "synthetic missing amount"}],
    }]
    saved = deterministic_contract.save_contracts(manager, pid, [item], "")
    entries = []

    def run_in_gateway(_bundle, entry, _cases):
        entries.append(entry)
        if entry.endswith("_parser.py"):
            return [_sandbox_result({
                "matched": True,
                "arguments": {},
                "issues": [{"field": "amount", "message": "synthetic missing amount"}],
            })]
        return [_sandbox_result({"value": 12.5}), _sandbox_result({"value": 12.5})]

    monkeypatch.setattr(deterministic_verification._sandbox, "run_in_gateway", run_in_gateway)
    _source_revision, report = deterministic_verification.verify(root, "calculate_summary", saved["revision"])

    assert report["status"] == "passed"
    assert report["cases"][0]["stage"] == "handler"
    assert entries == ["calculate_summary_parser.py", "calculate_summary.py"]


def test_verification_never_falls_back_to_local_execution(
    manager: WorkspaceManager, monkeypatch: pytest.MonkeyPatch,
):
    pid, root = _project(manager)
    saved = deterministic_contract.save_contracts(manager, pid, [_item()], "")

    def unavailable(*_args, **_kwargs):
        raise deterministic_verification._sandbox.SandboxUnavailable("synthetic unavailable")

    monkeypatch.setattr(deterministic_verification._sandbox, "run_in_gateway", unavailable)
    with pytest.raises(Exception) as exc:
        deterministic_verification.verify(root, "calculate_summary", saved["revision"])

    assert getattr(exc.value, "code", "") == "deterministic_verification_unavailable"


def test_verification_route_does_not_block_event_loop(
    manager: WorkspaceManager, monkeypatch: pytest.MonkeyPatch,
):
    from app.routers import deterministic as deterministic_router

    pid, root = _project(manager)
    saved = deterministic_contract.save_contracts(manager, pid, [_item()], "")

    async def json_body(_request):
        return {"base_revision": saved["revision"]}

    def verify(_root, _key, _revision):
        time.sleep(0.15)
        return "source-revision", {
            "status": "passed", "runtime": "jiuwenbox", "passed": 1, "failed": 0, "cases": [],
        }

    monkeypatch.setattr(deterministic_router, "_root", lambda *_args, **_kwargs: root)
    monkeypatch.setattr(deterministic_router.deps, "json_body", json_body)
    monkeypatch.setattr(deterministic_router.deps, "username", lambda _request: "operator_a")
    monkeypatch.setattr(deterministic_router.deterministic_verification, "verify", verify)
    monkeypatch.setattr(
        deterministic_router.deterministic_contract,
        "write_verification_report",
        lambda *_args, **_kwargs: {"report": {"status": "passed"}},
    )

    async def scenario():
        started = time.monotonic()
        task = asyncio.create_task(
            deterministic_router.verify_contract(pid, "calculate_summary", SimpleNamespace()),
        )
        await asyncio.sleep(0.02)
        event_loop_delay = time.monotonic() - started
        await task
        return event_loop_delay

    assert asyncio.run(scenario()) < 0.1


def test_old_verification_result_cannot_overwrite_changed_contract(manager: WorkspaceManager):
    pid, root = _project(manager)
    deterministic_contract.save_contracts(manager, pid, [_item()], "")
    item = deterministic_contract.check_at(root).contracts[0]
    old_source_revision = deterministic_contract.source_revision(root, item)
    handler = root / "ontology" / "scripts" / "calculate_summary.py"
    handler.write_text(handler.read_text(encoding="utf-8") + "\n# concurrent edit\n", encoding="utf-8")

    with pytest.raises(Exception) as exc:
        deterministic_contract.write_verification_report(
            manager, pid, item.key, old_source_revision,
            {"status": "passed", "runtime": "jiuwenbox", "passed": 1, "failed": 0, "cases": []},
        )

    assert getattr(exc.value, "code", "") == "deterministic_contract_changed"


def test_delete_contract_removes_files_and_keeps_workspace_clean(manager: WorkspaceManager):
    pid, root = _project(manager)
    saved = deterministic_contract.save_contracts(manager, pid, [_item()], "")

    deleted = deterministic_contract.save_contracts(manager, pid, [], saved["revision"])

    assert deleted["operations"] == []
    assert not (root / deterministic_contract.CONTRACT_DIR / "calculate_summary").exists()
    assert _git(root, "status", "--porcelain") == ""


def test_contract_reports_unknown_managed_file_and_save_cleans_it(manager: WorkspaceManager):
    pid, root = _project(manager)
    saved = deterministic_contract.save_contracts(manager, pid, [_item()], "")
    contract_dir = root / deterministic_contract.CONTRACT_DIR / "calculate_summary"
    (contract_dir / "unexpected.txt").write_text("synthetic", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "test: synthetic legacy contract file")

    view = deterministic_contract.contract_view(root)
    assert any("不支持的文件" in finding["message"] for finding in view["findings"])

    repaired = deterministic_contract.save_contracts(manager, pid, [_item()], view["revision"])
    assert repaired["findings"] == []
    assert not (contract_dir / "unexpected.txt").exists()
    assert _git(root, "status", "--porcelain") == ""


def test_disabled_contract_is_still_validated_and_not_published(manager: WorkspaceManager, tmp_path: Path):
    pid, _root = _project(manager)
    item = {**_item(), "enabled": False}
    deterministic_contract.save_contracts(manager, pid, [item], "")

    archive = tmp_path / "disabled.zip"
    publish_service.export_package_zip(manager, pid, archive)
    with zipfile.ZipFile(archive) as package:
        names = set(package.namelist())
        operation = package.read(f"{pid}/operations/计算汇总.md").decode("utf-8")

    assert f"{pid}/scripts/calculate_summary_parser.py" not in names
    assert 'type: "deterministic"' not in operation


def test_builtin_deterministic_plugin_can_be_staged_and_installed(tmp_path: Path):
    source = Path(__file__).parents[2] / "assets" / "packs" / "deterministic-execution"
    staged = tmp_path / "staged"

    manifest = builtin.stage(source, staged)
    repository = PackRepository(tmp_path / "repository")
    repository.install(staged, origin="builtin")

    assert manifest.name == "deterministic-execution"
    assert repository.verify(manifest.name, manifest.version) == []
    _manifest, report = checks.run(staged, official=True)
    assert not [item for item in report if item["result"] == "fail"], report


def test_plain_package_bytes_do_not_change(manager: WorkspaceManager, tmp_path: Path):
    pid, _root = _project(manager)
    before = tmp_path / "before.zip"
    after = tmp_path / "after.zip"

    publish_service.export_package_zip(manager, pid, before)
    publish_service.export_package_zip(manager, pid, after)

    assert before.read_bytes() == after.read_bytes()
    with zipfile.ZipFile(after) as package:
        descriptor = package.read(f"{pid}/scripts/calculate_summary.md").decode("utf-8")
    assert "data_access:" not in descriptor


def test_published_package_import_restores_deterministic_contract_as_unverified(
    manager: WorkspaceManager, tmp_path: Path,
):
    pid, root = _project(manager)
    (root / "ontology" / "object" / "合成记录.md").write_text(
        "# 合成记录\n\n用于验证知识包回导。\n", encoding="utf-8",
    )
    deterministic_contract.save_contracts(manager, pid, [_item("workspace_provider")], "")
    _mark_verified(manager, pid, root)
    archive = tmp_path / "published.zip"
    publish_service.export_package_zip(manager, pid, archive)

    imported = manager.import_package_zip("确定性回导", archive)
    imported_root = manager.project_root(imported["id"])
    view = deterministic_contract.contract_view(imported_root)

    assert len(view["operations"]) == 1
    restored = view["operations"][0]
    assert restored["operation"] == "计算汇总"
    assert restored["handler"] == "calculate_summary"
    assert restored["data_access"] == "workspace_provider"
    assert restored["parser_source"] == PARSER
    assert restored["test_vectors"] == []
    assert restored["verification"] == {"status": "missing", "current": False}
    assert not (imported_root / "ontology" / "scripts" / "calculate_summary_parser.py").exists()
    status = deterministic_contract.release_status_at(imported_root)
    assert any("测试向量必须是" in item["message"] for item in status["findings"])

    restored.pop("verification")
    restored["test_vectors"] = TEST_VECTORS
    saved = deterministic_contract.save_contracts(
        manager,
        imported["id"],
        [restored],
        view["revision"],
    )
    assert saved["operations"][0]["test_vectors"] == TEST_VECTORS


@pytest.mark.parametrize("missing_suffix", [
    "scripts/calculate_summary_parser.py",
    "schemas/calculate_summary.input.json",
    "schemas/calculate_summary.output.json",
])
def test_import_preview_rejects_incomplete_deterministic_package(
    manager: WorkspaceManager, tmp_path: Path, missing_suffix: str,
):
    pid, root = _project(manager)
    (root / "ontology" / "object" / "合成记录.md").write_text("# 合成记录\n", encoding="utf-8")
    deterministic_contract.save_contracts(manager, pid, [_item()], "")
    _mark_verified(manager, pid, root)
    source = publish_service.export_package_zip(manager, pid, tmp_path / "complete.zip")

    archive = _rewrite_package(
        source,
        tmp_path / f"missing-{Path(missing_suffix).name}.zip",
        lambda members: members.pop(f"{pid}/{missing_suffix}"),
    )
    summary = manager.import_summary(archive)

    assert summary["deterministic_operations"] == 0
    assert summary["deterministic_problems"]
    assert any("发布契约不完整" in message for message in summary["blockers"])
    before = len(manager.list_projects())
    with pytest.raises(WorkspaceError, match="发布契约不完整"):
        manager.import_package_zip("不完整确定性包", archive)
    assert len(manager.list_projects()) == before


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("invalid_key", "确定性标识不合法"),
        ("invalid_data_access", "data_access 只能是"),
        ("invalid_utf8", "必须是 UTF-8 文本"),
        ("escaped_path", "引用路径不合法"),
        ("prebuilt_lock", "只能由 Consumer 安装时生成"),
    ],
)
def test_import_preview_rejects_invalid_deterministic_package(
    manager: WorkspaceManager, tmp_path: Path, mutation: str, message: str,
):
    pid, root = _project(manager)
    (root / "ontology" / "object" / "合成记录.md").write_text("# 合成记录\n", encoding="utf-8")
    deterministic_contract.save_contracts(manager, pid, [_item()], "")
    _mark_verified(manager, pid, root)
    source = publish_service.export_package_zip(manager, pid, tmp_path / "valid.zip")

    def mutate(members):
        operation_path = f"{pid}/operations/计算汇总.md"
        descriptor_path = f"{pid}/scripts/calculate_summary.md"
        if mutation == "invalid_key":
            body = members[operation_path].decode("utf-8").replace(
                "scripts/calculate_summary_parser.py", "scripts/Invalid-Key_parser.py",
            )
            members[operation_path] = body.encode("utf-8")
            members[f"{pid}/scripts/Invalid-Key_parser.py"] = members.pop(
                f"{pid}/scripts/calculate_summary_parser.py"
            )
        elif mutation == "invalid_data_access":
            body = members[descriptor_path].decode("utf-8").replace(
                "data_access: none", "data_access: external_database",
            )
            members[descriptor_path] = body.encode("utf-8")
        elif mutation == "invalid_utf8":
            members[f"{pid}/schemas/calculate_summary.input.json"] = b"\xff\xfe"
        elif mutation == "prebuilt_lock":
            members[f"{pid}/.deterministic-lock.json"] = b"{}\n"
        else:
            body = members[operation_path].decode("utf-8").replace(
                "scripts/calculate_summary_parser.py", "scripts/../calculate_summary_parser.py",
            )
            members[operation_path] = body.encode("utf-8")

    archive = _rewrite_package(source, tmp_path / f"{mutation}.zip", mutate)
    summary = manager.import_summary(archive)

    assert any(message in blocker for blocker in summary["blockers"])
    assert summary["deterministic_problems"]


def test_import_preview_rejects_schema_v02_deterministic_package(
    manager: WorkspaceManager, tmp_path: Path,
):
    """Consumer 当前不为 schema 0.2 包生成确定性安装锁，建模侧必须提前拦截。"""
    pid, root = _project(manager)
    (root / "ontology" / "object" / "合成记录.md").write_text("# 合成记录\n", encoding="utf-8")
    deterministic_contract.save_contracts(manager, pid, [_item()], "")
    _mark_verified(manager, pid, root)
    source = publish_service.export_package_zip(manager, pid, tmp_path / "v01.zip")

    def mutate(members):
        manifest_path = f"{pid}/manifest.yaml"
        manifest = members[manifest_path].decode("utf-8").replace(
            "schema_version: '0.1'", "schema_version: '0.2'",
        )
        members[manifest_path] = manifest.encode("utf-8")

    archive = _rewrite_package(source, tmp_path / "v02-deterministic.zip", mutate)
    summary = manager.import_summary(archive)

    assert summary["package_schema_version"] == "0.2"
    assert summary["deterministic_operations"] == 1
    assert any("只支持 schema 0.1" in blocker for blocker in summary["blockers"])
    before = len(manager.list_projects())
    with pytest.raises(WorkspaceError, match="只支持 schema 0.1"):
        manager.import_package_zip("schema 0.2 确定性包", archive)
    assert len(manager.list_projects()) == before


def test_documented_deterministic_handler_scans_and_preserves_cli_errors(manager):
    """Exercise the actual generation template, including CLI error recovery."""
    command = Path(__file__).resolve().parents[2] / "assets/packs/deterministic-execution/commands/ontology-deterministic-generate.md"
    source = command.read_text(encoding="utf-8").split("```python\n", 1)[1].split("```", 1)[0]
    _pid, root = _project(manager)
    handler = root / "ontology/scripts/calculate_summary.py"
    header = HANDLER.split("import argparse", 1)[0]
    handler.write_text(header + source, encoding="utf-8")
    candidate = deterministic_contract.candidate_scan(root)["candidates"][0]
    assert candidate["can_create"], candidate["blockers"]
    for args, expected in [(["--amount", "12.5"], {"value": 12.5}),
                           ([], None), (["--amount", "bad"], None),
                           (["--amount", "2", "--unknown", "3"], None)]:
        result = subprocess.run([sys.executable, str(handler), *args], capture_output=True, text=True, check=False)
        assert result.returncode == 0
        assert not result.stderr
        payload = json.loads(result.stdout)
        if expected is not None:
            assert payload == expected
        else:
            assert payload["error"]["code"] == "INVALID_INPUT"
    handler.write_text(header + source.replace('return {"value": amount}', 'raise RuntimeError("private diagnostic")'), encoding="utf-8")
    result = subprocess.run([sys.executable, str(handler), "--amount", "2"], capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert not result.stdout
    assert result.stderr.startswith("ONTO_PUBLIC_ERROR:")
    assert "private diagnostic" not in result.stderr
