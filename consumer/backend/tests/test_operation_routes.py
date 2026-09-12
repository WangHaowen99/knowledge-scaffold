"""Tests for operation-evolution HTTP routes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import quote

from agent.evolution import (
    get_proposal_buffer,
    reset_operation_patcher,
    reset_proposal_buffer,
    resolve_operation_md,
)
from api.routes.operations import operations_router
from api.testing import TestApplication
from auth_test_backend import AuthManager
from backend_synthetic_credentials import AUTH_SIGNING_SECRET, BOOTSTRAP_ADMIN_PASSWORD
from fastapi.testclient import TestClient
from workspace.workspaces import create_workspace, get_workspace_paths


def _build_test_client(tmp_path):
    app = TestApplication()
    app.state.services["TESTING"] = True

    auth_manager = AuthManager(str(tmp_path / "users.json"), AUTH_SIGNING_SECRET)
    user = auth_manager.authenticate("admin", BOOTSTRAP_ADMIN_PASSWORD)
    assert user is not None
    user = auth_manager.change_password("admin", BOOTSTRAP_ADMIN_PASSWORD, "RootStrong123!")

    app.state.services["AUTH_MANAGER"] = auth_manager
    app.include_router(operations_router)

    client = TestClient(app)
    token = auth_manager.issue_token(user)
    headers = {"Authorization": "Bearer {}".format(token)}
    return client, headers


def _create_workspace_with_operation(
    tmp_path,
    monkeypatch,
    *,
    workspace_id: str,
    operation_content: str,
    ontology_id: str = "example",
    versioned: bool = False,
):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Workspace {}".format(workspace_id), workspace_id=workspace_id)
    paths = get_workspace_paths(workspace_id)
    ontology_dir = paths.ontologies_dir / ontology_id
    op_dir = ontology_dir / "versions" / "1.0.0" / "operations" if versioned else ontology_dir / "operations"
    op_dir.mkdir(parents=True, exist_ok=True)
    if versioned:
        (ontology_dir / "manifest.yaml").write_text(
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            f"id: {ontology_id}\n"
            f"name: {ontology_id.title()}\n"
            "description: test\n"
            "version: 1.0.0\n"
            "active_version: 1.0.0\n"
            "versions: [1.0.0]\n"
            "language: zh-CN\n"
            "enabled: true\n",
            encoding="utf-8",
        )
    op_path = op_dir / "OP_A.md"
    op_path.write_text(operation_content, encoding="utf-8")
    return op_path


def _headers_for_workspace(base_headers, workspace_id: str):
    headers = dict(base_headers)
    headers["X-Workspace-Id"] = quote(workspace_id)
    return headers


def _write_staged(
    op_path: Path,
    *,
    original_content: str,
    staged_content: str,
    ontology_id: str = "example",
) -> None:
    staged_path = op_path.with_suffix(op_path.suffix + ".staged")
    meta_path = staged_path.with_suffix(staged_path.suffix + ".meta.json")
    staged_path.write_text(staged_content, encoding="utf-8")
    meta_path.write_text(
        json.dumps(
            {
                "ontology_id": ontology_id,
                "operation_key": "OP_A",
                "fingerprints": ["fp-1"],
                "original_sha": hashlib.sha256(original_content.encode("utf-8")).hexdigest(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_resolve_operation_md_rejects_path_traversal_segments(monkeypatch, tmp_path):
    reset_operation_patcher()
    reset_proposal_buffer()
    operation_path = _create_workspace_with_operation(
        tmp_path,
        monkeypatch,
        workspace_id="safe-workspace",
        operation_content="# OP_A\n",
    )

    assert (
        resolve_operation_md(
            "example",
            "OP_A",
            workspace_id="safe-workspace",
        )
        == operation_path.resolve()
    )
    assert (
        resolve_operation_md(
            "../example",
            "OP_A",
            workspace_id="safe-workspace",
        )
        is None
    )
    assert (
        resolve_operation_md(
            "example",
            "../OP_A",
            workspace_id="safe-workspace",
        )
        is None
    )
    assert (
        resolve_operation_md(
            "example",
            "nested/OP_A",
            workspace_id="safe-workspace",
        )
        is None
    )


def test_apply_staged_rejects_when_operation_changed_after_stage(monkeypatch, tmp_path):
    reset_operation_patcher()
    reset_proposal_buffer()
    client, headers = _build_test_client(tmp_path)
    original = "# OP_A\n\n原始内容"
    op_path = _create_workspace_with_operation(
        tmp_path,
        monkeypatch,
        workspace_id="demo",
        operation_content=original,
    )
    _write_staged(op_path, original_content=original, staged_content="# OP_A\n\nstaged 内容")
    op_path.write_text("# OP_A\n\n人工修改", encoding="utf-8")
    from ontology.view_cache import get_ontology_mutation_revision

    view_revision = get_ontology_mutation_revision("demo", "example")

    response = client.post(
        "/agentic/operations/example/OP_A/staged/apply",
        headers=_headers_for_workspace(headers, "demo"),
    )

    assert response.status_code == 409
    assert "changed since staged" in response.json()["error"]
    assert op_path.read_text(encoding="utf-8") == "# OP_A\n\n人工修改"
    assert get_ontology_mutation_revision("demo", "example") == view_revision


def test_apply_staged_rejects_operation_over_char_limit(monkeypatch, tmp_path):
    reset_operation_patcher()
    reset_proposal_buffer()
    monkeypatch.setenv("ONTOLOGY_OPERATION_MAX_CHARS", "10")
    client, headers = _build_test_client(tmp_path)
    original = "# OP_A\n\nok"
    op_path = _create_workspace_with_operation(
        tmp_path,
        monkeypatch,
        workspace_id="demo",
        operation_content=original,
    )
    _write_staged(op_path, original_content=original, staged_content="# OP_A\n\n" + "x" * 20)

    response = client.post(
        "/agentic/operations/example/OP_A/staged/apply",
        headers=_headers_for_workspace(headers, "demo"),
    )

    assert response.status_code == 400
    assert "单个逻辑长度超过上限" in response.json()["error"]
    assert op_path.read_text(encoding="utf-8") == original


def test_apply_staged_emits_mutation_and_revokes_evidence_pointer(monkeypatch, tmp_path):
    reset_operation_patcher()
    reset_proposal_buffer()
    client, headers = _build_test_client(tmp_path)
    original = "# OP_A\n\n原始内容"
    staged = "# OP_A\n\n已审批的新内容"
    op_path = _create_workspace_with_operation(
        tmp_path,
        monkeypatch,
        workspace_id="demo",
        operation_content=original,
        ontology_id="aviation",
        versioned=True,
    )
    _write_staged(
        op_path,
        original_content=original,
        staged_content=staged,
        ontology_id="aviation",
    )
    from ontology.view_cache import get_ontology_mutation_revision

    view_revision = get_ontology_mutation_revision("demo", "aviation")

    response = client.post(
        "/agentic/operations/aviation/OP_A/staged/apply",
        headers={
            **_headers_for_workspace(headers, "demo"),
            "X-Request-Id": "req-operation-apply",
        },
    )

    assert response.status_code == 200
    assert response.json()["mutation_revision"] == 1
    assert op_path.read_text(encoding="utf-8") == staged
    assert get_ontology_mutation_revision("demo", "aviation") > view_revision

    from ontology.mutations import OntologyMutationCoordinator

    coordinator = OntologyMutationCoordinator(get_workspace_paths("demo").runtime_dir)
    state = coordinator.store.get_state(workspace_id="demo", ontology_id="aviation")
    assert state is not None
    assert state["mutation_revision"] == 1
    outbox = coordinator.store.list_outbox(status="pending")
    assert len(outbox) == 1
    event = json.loads(outbox[0]["payload_json"])
    assert event["mutation_type"] == "ONTOLOGY_FILE_CHANGED"
    assert event["known_file_digests"]["entries"] == {
        "operations/OP_A.md": {
            "sha256": hashlib.sha256(staged.encode("utf-8")).hexdigest(),
            "size_bytes": len(staged.encode("utf-8")),
        }
    }
    assert event["operator"] == {"type": "user", "id": "admin"}
    assert event["request_id"] == "req-operation-apply"
    assert event["trigger"] == "admin"


def test_apply_staged_rejects_in_place_deterministic_operation(monkeypatch, tmp_path):
    reset_operation_patcher()
    reset_proposal_buffer()
    client, headers = _build_test_client(tmp_path)
    original = (
        "---\n"
        "resource: operation\n"
        "key: OP_A\n"
        "execution:\n"
        "  type: deterministic\n"
        "  input_schema: schemas/input.json\n"
        "  output_schema: schemas/output.json\n"
        "---\n\n# OP_A\n"
    )
    op_path = _create_workspace_with_operation(
        tmp_path,
        monkeypatch,
        workspace_id="demo",
        operation_content=original,
    )
    (op_path.parent.parent / ".deterministic-lock.json").write_text(
        "{}",
        encoding="utf-8",
    )
    _write_staged(op_path, original_content=original, staged_content=original + "staged")

    response = client.post(
        "/agentic/operations/example/OP_A/staged/apply",
        headers=_headers_for_workspace(headers, "demo"),
    )

    assert response.status_code == 409
    assert response.json()["error"] == "deterministic_operation_immutable"
    assert response.json()["error_code"] == "deterministic_operation_immutable"
    assert op_path.read_text(encoding="utf-8") == original


def test_apply_staged_rejects_legacy_operation_in_mixed_locked_version(
    monkeypatch,
    tmp_path,
):
    reset_operation_patcher()
    reset_proposal_buffer()
    client, headers = _build_test_client(tmp_path)
    original = "# OP_A\n\n旧 Operation，与确定性 Operation 同版本"
    op_path = _create_workspace_with_operation(
        tmp_path,
        monkeypatch,
        workspace_id="demo",
        operation_content=original,
    )
    (op_path.parent.parent / ".deterministic-lock.json").write_text(
        "{}",
        encoding="utf-8",
    )
    _write_staged(op_path, original_content=original, staged_content=original + "\nstaged")

    response = client.post(
        "/agentic/operations/example/OP_A/staged/apply",
        headers=_headers_for_workspace(headers, "demo"),
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "deterministic_operation_immutable"
    assert op_path.read_text(encoding="utf-8") == original


def test_staged_routes_resolve_operation_inside_request_workspace(monkeypatch, tmp_path):
    reset_operation_patcher()
    reset_proposal_buffer()
    client, headers = _build_test_client(tmp_path)
    op_a = _create_workspace_with_operation(
        tmp_path,
        monkeypatch,
        workspace_id="ws-a",
        operation_content="# OP_A\n\nworkspace A",
    )
    op_b = _create_workspace_with_operation(
        tmp_path,
        monkeypatch,
        workspace_id="ws-b",
        operation_content="# OP_A\n\nworkspace B",
    )
    _write_staged(op_b, original_content="# OP_A\n\nworkspace B", staged_content="# OP_A\n\nstaged B")
    assert not op_a.with_suffix(op_a.suffix + ".staged").exists()

    response = client.get(
        "/agentic/operations/example/OP_A/staged",
        headers=_headers_for_workspace(headers, "ws-b"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["original"] == "# OP_A\n\nworkspace B"
    assert body["staged"] == "# OP_A\n\nstaged B"


def test_operation_proposal_index_scopes_to_request_workspace(monkeypatch, tmp_path):
    reset_operation_patcher()
    reset_proposal_buffer()
    client, headers = _build_test_client(tmp_path)
    _create_workspace_with_operation(
        tmp_path,
        monkeypatch,
        workspace_id="ws-a",
        operation_content="# OP_A\n\nworkspace A",
    )
    _create_workspace_with_operation(
        tmp_path,
        monkeypatch,
        workspace_id="ws-b",
        operation_content="# OP_A\n\nworkspace B",
    )
    buf = get_proposal_buffer()
    buf.append(
        workspace_id="ws-a",
        ontology_id="example",
        operation_key="OP_A",
        kind="procedure",
        content="优先读取工单状态再计算完成率",
        source="active_chat",
    )
    buf.append(
        workspace_id="ws-b",
        ontology_id="example",
        operation_key="OP_A",
        kind="pitfall",
        content="缺少库存记录时不能默认通过",
        source="passive",
    )

    response = client.get(
        "/agentic/operations/proposals?status=all",
        headers=_headers_for_workspace(headers, "ws-a"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["aggregate_count"] == 1
    item = body["items"][0]
    assert item["workspace_id"] == "ws-a"
    assert item["ontology_id"] == "example"
    assert item["operation_key"] == "OP_A"
    assert item["aggregate_count"] == 1
    assert item["status_counts"] == {"pending": 1}
    assert item["kind_counts"] == {"procedure": 1}
    assert item["source_counts"] == {"active_chat": 1}
    assert item["occurrences"] == 1
    assert item["top_content"] == "优先读取工单状态再计算完成率"


def test_list_proposals_can_include_raw_records(monkeypatch, tmp_path):
    reset_operation_patcher()
    reset_proposal_buffer()
    client, headers = _build_test_client(tmp_path)
    _create_workspace_with_operation(
        tmp_path,
        monkeypatch,
        workspace_id="demo",
        operation_content="# OP_A\n\nworkspace",
    )
    buf = get_proposal_buffer()
    agg = buf.append(
        workspace_id="demo",
        ontology_id="example",
        operation_key="OP_A",
        kind="schema",
        entity_key="工单",
        content="工单状态字段应使用 status_code",
        source="active_chat",
        run_id="run-1",
        user_id="admin",
    )

    response = client.get(
        "/agentic/operations/example/OP_A/proposals?status=all&include_records=1",
        headers=_headers_for_workspace(headers, "demo"),
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["fingerprint"] for item in body["proposals"]] == [agg.fingerprint]
    assert len(body["proposal_records"]) == 1
    record = body["proposal_records"][0]
    assert record["fingerprint"] == agg.fingerprint
    assert record["content"] == "工单状态字段应使用 status_code"
    assert record["source"] == "active_chat"
    assert record["run_id"] == "run-1"


def test_export_operation_proposals_includes_workspace_records(monkeypatch, tmp_path):
    reset_operation_patcher()
    reset_proposal_buffer()
    client, headers = _build_test_client(tmp_path)
    _create_workspace_with_operation(
        tmp_path,
        monkeypatch,
        workspace_id="ws-a",
        operation_content="# OP_A\n\nworkspace A",
    )
    _create_workspace_with_operation(
        tmp_path,
        monkeypatch,
        workspace_id="ws-b",
        operation_content="# OP_A\n\nworkspace B",
    )
    buf = get_proposal_buffer()
    agg = buf.append(
        workspace_id="ws-a",
        ontology_id="example",
        operation_key="OP_A",
        kind="procedure",
        content="先读取 operation 再执行脚本",
        source="active_chat",
        run_id="run-export",
        user_id="admin",
    )
    buf.append(
        workspace_id="ws-b",
        ontology_id="example",
        operation_key="OP_A",
        kind="pitfall",
        content="另一个 workspace 的 proposal 不应导出",
        source="passive",
    )

    response = client.get(
        "/agentic/operations/proposals/export?status=all",
        headers=_headers_for_workspace(headers, "ws-a"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == "operation-evolution-proposals.v1"
    assert body["workspace_id"] == "ws-a"
    assert body["operation_count"] == 1
    assert body["aggregate_count"] == 1
    assert body["record_count"] == 1
    exported = body["operations"][0]
    assert exported["summary"]["operation_key"] == "OP_A"
    assert [item["fingerprint"] for item in exported["proposals"]] == [agg.fingerprint]
    assert exported["proposal_records"][0]["run_id"] == "run-export"
