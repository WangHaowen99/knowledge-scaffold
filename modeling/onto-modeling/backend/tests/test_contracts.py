"""三份契约接口（API 契约 3.20、3.21，ADR-0020）：发布记录、包清单与按版本下载，求解验证候选与回执，
外部反馈，以及服务令牌鉴权。

经 HTTP 调真实路由；工作区、令牌、用户名与反馈内容全部为合成数据。
"""

import hashlib
import io
import json
import shutil
import zipfile
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app import config
from app.main import app
from app.routers import projects
from app.services import auth, feedback_inbox, release_store, service_tokens, solver_validation
from app.services.workspace import WorkspaceError, WorkspaceManager, _git
from tests.test_release_publish import commit_all, rich_project
from tests.test_solver_contract_check import _with_contract

OWNER, OTHER = "operator_a", "operator_b"
TOKENS = {"owner-token": {"sub": OWNER, "role": "user"}, "other-token": {"sub": OTHER, "role": "user"}}
OWNER_HEADERS = {"Authorization": "Bearer owner-token"}


@pytest.fixture()
def manager(tmp_path: Path, monkeypatch) -> WorkspaceManager:
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    assets = tmp_path / "assets"
    (assets / "workspace-toolkit" / "templates").mkdir(parents=True)
    (assets / "workspace-rules.md").write_text("# rules", encoding="utf-8")
    (assets / "resource-template" / "others").mkdir(parents=True)
    instance = WorkspaceManager(tmp_path / "data", assets)
    monkeypatch.setattr(projects, "workspaces", instance)
    return instance


@pytest.fixture()
def client(manager, monkeypatch) -> TestClient:
    monkeypatch.setattr(auth, "ENABLED", True)
    monkeypatch.setattr(auth, "verify_access_token", lambda token: TOKENS.get(token))
    return TestClient(app)


def token(*scopes: str, project_ids: list[str] | None = None) -> dict:
    body = {"name": "合成服务", "scopes": list(scopes)}
    if project_ids is not None:
        body["project_ids"] = project_ids
    return {"Authorization": f"Bearer {service_tokens.issue(body)['token']}"}


def owned_project(manager, name: str = "发布回归") -> tuple[str, Path]:
    pid, root = rich_project(manager, name)
    manager.set_owner(pid, OWNER)
    return pid, root


def zip_files(content: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def release_url(pid: str, version: str, suffix: str = "") -> str:
    return f"/api/v1/projects/{pid}/releases/{version}{suffix}"


# ---- 鉴权 ----


def test_service_routes_require_service_token(client, manager):
    pid, _ = owned_project(manager)
    missing = client.get("/api/v1/contract/releases")
    assert missing.status_code == 401 and missing.json()["code"] == "unauthenticated"
    # 登录 token 不能代替服务令牌写回执或反馈
    assert client.post(f"/api/v1/projects/{pid}/solver/receipts", headers=OWNER_HEADERS, json={}).status_code == 401
    assert client.post(f"/api/v1/projects/{pid}/feedback", headers=OWNER_HEADERS, json={}).status_code == 401
    # 权限范围不够、项目不在限定范围
    wrong_scope = client.post(f"/api/v1/projects/{pid}/feedback", headers=token("releases.read"), json={})
    assert wrong_scope.status_code == 403 and wrong_scope.json()["code"] == "scope_forbidden"
    assert wrong_scope.json()["details"]["required_scope"] == "feedback.write"
    other_project = client.get(release_url(pid, "0.1.1"), headers=token("releases.read", project_ids=["p-other"]))
    assert other_project.status_code == 403 and other_project.json()["code"] == "scope_forbidden"


def test_service_token_cannot_use_owner_only_release_list(client, manager):
    pid, _ = owned_project(manager)
    response = client.get(f"/api/v1/projects/{pid}/releases", headers=token("releases.read"))
    assert response.status_code == 401  # PB-04 只给所有者；服务令牌用在别处一律 401
    assert client.get(f"/api/v1/projects/{pid}/releases", headers=OWNER_HEADERS).status_code == 200


def test_unknown_project_is_404(client):
    response = client.get(release_url("p-missing", "0.1.1"), headers=token("releases.read"))
    assert response.status_code == 404


# ---- 包出：发布记录、包清单与按版本下载 ----


def test_release_records_and_download_read_stored_package(client, manager, tmp_path):
    pid, root = owned_project(manager)
    first = manager.publish_package(pid, tmp_path / "first.zip", author=OWNER)
    (root / "ontology" / "rule" / "新规则.md").write_text("# 新规则\n", encoding="utf-8")
    commit_all(root, "test: second release")
    second = manager.publish_package(pid, tmp_path / "second.zip", author=OWNER)

    listed = client.get(f"/api/v1/projects/{pid}/releases", headers=OWNER_HEADERS).json()
    assert [item["version"] for item in listed["items"]] == [second["version"], first["version"]]
    assert listed["next_cursor"] is None and listed["total"] == 2
    headers = token("releases.read")
    record = client.get(release_url(pid, first["version"]), headers=headers).json()
    assert record["commit"] == first["commit"] and record["released_by"] == OWNER
    assert record["format"] == "ontology" and record["degraded"] is False
    assert record["released_at"].endswith("Z") and len(record["package"]["sha256"]) == 64
    assert record["download_url"] == release_url(pid, first["version"], "/download")

    response = client.get(record["download_url"], headers=headers)
    assert response.status_code == 200
    assert response.content == (tmp_path / "first.zip").read_bytes()
    assert response.headers["X-Package-Commit"] == first["commit"]
    assert response.headers["X-Package-Format"] == "ontology"
    assert response.headers["X-Package-Variant"] == "standard"
    manifest = yaml.safe_load(zip_files(response.content)[f"{pid}/manifest.yaml"])
    assert manifest["version"] == first["version"] and manifest["status"] == "released"

    pulled = client.get(release_url(pid, first["version"]), headers=headers).json()
    assert pulled["pulls"]["count"] == 1 and pulled["pulls"]["last_at"].endswith("Z")  # 服务令牌拉取记一次

    missing = client.get(release_url(pid, "0.1.999", "/download"), headers=headers)
    assert missing.status_code == 404 and missing.json()["code"] == "release_not_found"
    assert client.get(release_url(pid, "latest", "/download"), headers=headers).status_code == 400


def test_manifest_lists_package_contents(client, manager, tmp_path):
    pid, _ = owned_project(manager)
    record = manager.publish_package(pid, tmp_path / "pkg.zip")
    body = client.get(release_url(pid, record["version"], "/manifest"), headers=token("releases.read")).json()
    assert body["manifest"]["version"] == record["version"] and body["manifest"]["id"] == pid
    assert body["sha256"] == hashlib.sha256((tmp_path / "pkg.zip").read_bytes()).hexdigest()
    assert f"{pid}/manifest.yaml" in {item["path"] for item in body["files"]} and body["truncated"] is False


def test_graph_variant_is_rendered_once_from_the_tag(client, manager, tmp_path):
    pid, root = owned_project(manager)
    record = manager.publish_package(pid, tmp_path / "pkg.zip")
    (root / "ontology" / "rule" / "之后.md").write_text("# 之后\n", encoding="utf-8")
    commit_all(root, "test: after release")  # 变体按标签提交渲染，不读之后的提交

    url = release_url(pid, record["version"], "/download")
    first = client.get(url, params={"variant": "graph"}, headers=token("releases.read"))
    assert first.status_code == 200 and first.headers["X-Package-Variant"] == "graph"
    files = zip_files(first.content)
    mappings = json.loads(files[f"{pid}/mappings/object-data-mappings.json"])
    assert mappings["objects"]["订单"]["resource"] == "订单"
    assert not any(name.endswith("之后.md") for name in files)
    again = client.get(url, params={"variant": "graph"}, headers=token("releases.read"))
    assert again.content == first.content
    assert client.get(url, params={"variant": "neo4j"}, headers=token("releases.read")).status_code == 400


def test_owner_can_read_releases_with_login_token(client, manager, tmp_path):
    pid, _ = owned_project(manager)
    record = manager.publish_package(pid, tmp_path / "pkg.zip")
    assert client.get(release_url(pid, record["version"]), headers=OWNER_HEADERS).status_code == 200
    download = client.get(release_url(pid, record["version"], "/download"), params={"token": "owner-token"})
    assert download.status_code == 200  # 下载链接可用 ?token=
    assert client.get(release_url(pid, record["version"]), headers=OWNER_HEADERS).json()["pulls"]["count"] == 0
    other = client.get(release_url(pid, record["version"]), headers={"Authorization": "Bearer other-token"})
    assert other.status_code == 403
    assert client.get(release_url(pid, record["version"])).status_code == 401


def test_same_version_keeps_first_stored_bytes(manager, tmp_path):
    pid, _ = owned_project(manager)
    record = manager.publish_package(pid, tmp_path / "a.zip")
    stored, _ = release_store.get(manager.data_dir, pid, record["version"], record["commit"])
    stored.write_bytes(b"synthetic-first-bytes")  # 模拟渲染器升级前留存的字节
    meta = stored.with_suffix(".json")
    data = json.loads(meta.read_text(encoding="utf-8"))
    data["sha256"] = hashlib.sha256(b"synthetic-first-bytes").hexdigest()
    meta.write_text(json.dumps(data), encoding="utf-8")

    again = manager.publish_package(pid, tmp_path / "b.zip")
    assert again["version"] == record["version"]
    assert (tmp_path / "b.zip").read_bytes() == b"synthetic-first-bytes"  # 同一版本只认第一次的字节
    path, _ = manager.release_package(pid, record["version"])
    assert path.read_bytes() == b"synthetic-first-bytes"


def test_missing_stored_package_is_rerendered_once(client, manager, tmp_path):
    pid, _ = owned_project(manager)
    record = manager.publish_package(pid, tmp_path / "pkg.zip")
    shutil.rmtree(release_store.root_dir(manager.data_dir))

    assert client.get(release_url(pid, record["version"]), headers=token("releases.read")).json()["package"] is None
    response = client.get(release_url(pid, record["version"], "/download"), headers=token("releases.read"))
    assert response.status_code == 200
    assert response.content == (tmp_path / "pkg.zip").read_bytes()  # 同一提交、同一渲染器：字节一致
    _, stored = release_store.get(manager.data_dir, pid, record["version"], record["commit"])
    assert stored["backfilled"] is True


def test_contract_release_list_filters_and_pages(client, manager, tmp_path):
    first, _ = owned_project(manager)
    second, second_root = rich_project(manager, "另一个项目")
    manager.publish_package(first, tmp_path / "a.zip")
    manager.publish_package(second, tmp_path / "b.zip")
    (second_root / "ontology" / "rule" / "新规则.md").write_text("# 新规则\n", encoding="utf-8")
    commit_all(second_root, "test: second release")
    manager.publish_package(second, tmp_path / "c.zip")

    everything = client.get("/api/v1/contract/releases", headers=token("releases.read")).json()
    assert {item["project_id"] for item in everything["items"]} == {first, second}
    assert len(everything["items"]) == 3 and everything["next_cursor"] is None
    item = everything["items"][0]
    assert set(item) == {
        "project_id", "package_name", "version", "commit", "format", "released_at", "degraded",
        "manifest_url", "download_url",
    }
    limited = client.get("/api/v1/contract/releases", headers=token("releases.read", project_ids=[second])).json()
    assert {item["project_id"] for item in limited["items"]} == {second}
    assert limited["items"][0]["package_name"] == "另一个项目"

    headers = token("releases.read")
    page = client.get("/api/v1/contract/releases", params={"limit": 2}, headers=headers).json()
    assert len(page["items"]) == 2 and page["next_cursor"]
    rest = client.get(
        "/api/v1/contract/releases", params={"limit": 2, "cursor": page["next_cursor"]}, headers=headers,
    ).json()
    assert len(rest["items"]) == 1 and rest["next_cursor"] is None
    stale = client.get(
        "/api/v1/contract/releases", params={"cursor": page["next_cursor"], "project_id": first}, headers=headers,
    )
    assert stale.status_code == 400 and stale.json()["code"] == "invalid_cursor"
    future = client.get("/api/v1/contract/releases", params={"since": "2999-01-01T00:00:00.000Z"}, headers=headers)
    assert future.json()["items"] == []
    assert client.get("/api/v1/contract/releases", params={"since": "yesterday"}, headers=headers).status_code == 400


# ---- 求解验证候选与回执 ----


def receipt_body(manager, pid: str, revision: str | None = None, **fields) -> dict:
    status = manager.solver_release_status(pid)
    return {
        "revision": revision or status["revision"],
        "solver_keys": ["plan_route"],
        "status": "passed",
        "authoritative": True,
        "contract_digest": status["digests"]["total"],
        "request_digest": "sha256:" + "1" * 64,
        "result_digest": "sha256:" + "2" * 64,
        "completed_at": "2026-11-01T08:00:00.000Z",
        "receipt_id": "run-1",
        **fields,
    }


def test_receipt_unlocks_solver_publish(client, manager, tmp_path):
    pid, _ = _with_contract(manager)
    headers = token("receipts.write")
    with pytest.raises(WorkspaceError):
        manager.publish_package(pid, tmp_path / "blocked.zip", allow_unbound=True)

    candidate = client.get(f"/api/v1/projects/{pid}/solver/candidate", params={"allow_unbound": True}, headers=headers)
    assert candidate.status_code == 200, candidate.text
    revision = candidate.headers["X-Solver-Revision"]
    assert revision == manager.solver_release_status(pid)["revision"]
    assert candidate.headers["X-Solver-Contract-Digest"] == manager.solver_release_status(pid)["digests"]["total"]
    manifest = yaml.safe_load(zip_files(candidate.content)[f"{pid}/manifest.yaml"])
    assert manifest["status"] == "draft" and manifest["version"] == candidate.headers["X-Package-Version"]
    assert _git(manager.project_root(pid), "tag", "--list", "release/*").strip() == ""  # 候选不是发布

    posted = client.post(
        f"/api/v1/projects/{pid}/solver/receipts", headers=headers,
        json=receipt_body(manager, pid, candidate_count=3, message="详情见 http://example.invalid/runs/1",
                          evidence={"package": {"id": pid, "version": manifest["version"]}}),
    )
    assert posted.status_code == 201, posted.text
    receipt = posted.json()
    assert receipt["by"] == "service:合成服务" and receipt["receipt_id"] == "run-1"
    assert receipt["completed_at"] == "2026-11-01T08:00:00.000Z" and receipt["created_at"].endswith("Z")
    assert "example.invalid" not in receipt["message"]
    assert manager.solver_release_status(pid)["gate"] == []

    record = manager.publish_package(pid, tmp_path / "release.zip", allow_unbound=True)
    assert manifest["version"] == record["version"]
    stored = release_store.get(manager.data_dir, pid, record["version"], record["commit"])[1]
    assert stored["format"] == "solver"

    manager.set_owner(pid, OWNER)
    latest = client.get(f"/api/projects/{pid}/package/solver-validations/latest", headers=OWNER_HEADERS).json()
    assert latest["validation"]["id"] == receipt["id"]  # 面板照旧经 latest 读回执


def test_receipt_id_takes_effect_once(client, manager):
    pid, _ = _with_contract(manager)
    headers = token("receipts.write")
    url = f"/api/v1/projects/{pid}/solver/receipts"
    first = client.post(url, headers=headers, json=receipt_body(manager, pid))
    assert first.status_code == 201
    repeat = client.post(url, headers=headers, json=receipt_body(manager, pid, status="failed"))
    assert repeat.status_code == 200 and repeat.json()["id"] == first.json()["id"]
    assert repeat.json()["status"] == "passed"  # 同一幂等键只生效一次
    assert len(solver_validation.list_records(manager.project_root(pid))) == 1


def test_receipt_for_unknown_revision_is_422(client, manager):
    pid, _ = _with_contract(manager)
    url = f"/api/v1/projects/{pid}/solver/receipts"
    unknown = client.post(url, headers=token("receipts.write"), json=receipt_body(manager, pid, revision="sha256:" + "0" * 64))
    assert unknown.status_code == 422 and unknown.json()["code"] == "receipt_revision_unknown"
    assert unknown.json()["details"]["revision"] == "sha256:" + "0" * 64
    digest = client.post(url, headers=token("receipts.write"), json=receipt_body(manager, pid, contract_digest="sha256:other"))
    assert digest.status_code == 400 and "contract_digest" in digest.json()["details"]["fields"]
    keys = client.post(url, headers=token("receipts.write"), json=receipt_body(manager, pid, solver_keys=["other"]))
    assert keys.status_code == 400 and "solver_keys" in keys.json()["details"]["fields"]


def test_failed_or_non_authoritative_receipt_keeps_gate_closed(client, manager):
    pid, _ = _with_contract(manager)
    url = f"/api/v1/projects/{pid}/solver/receipts"
    client.post(url, headers=token("receipts.write"), json=receipt_body(manager, pid, receipt_id="r1", authoritative=False))
    assert manager.solver_release_status(pid)["gate"]
    client.post(url, headers=token("receipts.write"), json=receipt_body(
        manager, pid, receipt_id="r2", status="failed", error_code="solver_tool_not_called",
    ))
    gate = manager.solver_release_status(pid)["gate"]
    assert gate and "未通过" in gate[0]["message"]


@pytest.mark.parametrize("change", [
    {"revision": None},
    {"solver_keys": []},
    {"status": "running"},
    {"authoritative": "yes"},
    {"request_digest": ""},
    {"completed_at": "yesterday"},
    {"completed_at": "2026-11-01T08:00:00"},
    {"candidate_count": -1},
    {"receipt_id": "bad id"},
    {"release_version": "latest"},
    {"evidence": {"blob": "x" * 70000}},
])
def test_invalid_receipts_are_400(client, manager, change):
    pid, _ = _with_contract(manager)
    body = {**receipt_body(manager, pid), **change}
    body = {key: value for key, value in body.items() if value is not None}
    response = client.post(f"/api/v1/projects/{pid}/solver/receipts", headers=token("receipts.write"), json=body)
    assert response.status_code == 400 and response.json()["code"] == "invalid_argument"
    assert response.json()["details"]["fields"]


def test_plain_package_has_no_receipt_target(client, manager):
    pid, _ = owned_project(manager)
    headers = token("receipts.write")
    body = {
        "revision": "sha256:" + "3" * 64, "solver_keys": ["x"], "status": "passed", "authoritative": True,
        "contract_digest": "d", "request_digest": "d", "result_digest": "d",
        "completed_at": "2026-11-01T08:00:00.000Z", "receipt_id": "r1",
    }
    receipt = client.post(f"/api/v1/projects/{pid}/solver/receipts", headers=headers, json=body)
    assert receipt.status_code == 422 and receipt.json()["code"] == "receipt_revision_unknown"
    candidate = client.get(f"/api/v1/projects/{pid}/solver/candidate", headers=headers)
    assert candidate.status_code == 400 and candidate.json()["code"] == "solver_validation_not_required"


def test_receipt_accepts_head_revision_when_workspace_differs(client, manager):
    """工作区有未提交的契约改动时，HEAD 提交的修订（发布读它）仍可回执。"""
    pid, root = _with_contract(manager)
    body = receipt_body(manager, pid)
    evaluator = root / "ontology" / "scripts" / "route_eval.py"
    evaluator.write_text(evaluator.read_text(encoding="utf-8") + "\n# 合成改动\n", encoding="utf-8")
    assert manager.solver_check(pid).revision != body["revision"]
    response = client.post(f"/api/v1/projects/{pid}/solver/receipts", headers=token("receipts.write"), json=body)
    assert response.status_code == 201


# ---- 反馈入 ----


def feedback_body(**fields) -> dict:
    return {
        "feedback_id": "round-001",
        "source": {"service": "合成服务", "run": "run-7"},
        "summary": {"score": 72.5, "total": 2, "fail": 1, "error": 1},
        "items": [
            {"case_id": "c1", "question": "合成问题一？", "expected": "合成答案一", "actual": "合成回答一",
             "loss_reason": "缺少合成概念", "suspected_artifacts": ["ontology/object/订单.md"],
             "tool_failures": ["query · TABLE_NOT_FOUND"]},
            {"case_id": "c2", "question": "合成问题二？", "verdict": "error"},
        ],
        **fields,
    }


def test_feedback_lands_in_workspace_and_commits_only_its_file(client, manager):
    pid, root = owned_project(manager)
    (root / "ontology" / "rule" / "未提交.md").write_text("# 未提交\n", encoding="utf-8")
    response = client.post(f"/api/v1/projects/{pid}/feedback", headers=token("feedback.write"), json=feedback_body())
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["path"].startswith("feedback/") and result["path"].endswith("-round-001.json")
    assert result["item_count"] == 2 and result["verdicts"] == {"fail": 1, "partial": 0, "error": 1}
    assert result["received_at"].endswith("Z") and result["source"] == {"service": "合成服务", "run": "run-7"}

    stored = json.loads((root / result["path"]).read_text(encoding="utf-8"))
    assert stored["format"] == feedback_inbox.FORMAT and stored["received_by"] == "service:合成服务"
    assert stored["items"][0]["tool_failures"] == ["query · TABLE_NOT_FOUND"]
    assert stored["items"][0]["verdict"] == "fail"  # 缺省按失分处理
    changed = _git(root, "-c", "core.quotePath=false", "show", "--name-only", "--pretty=format:", "HEAD").split()
    assert changed == [result["path"]]
    assert "ontology/rule/未提交.md" in _git(root, "-c", "core.quotePath=false", "status", "--porcelain")
    assert feedback_inbox.latest_batch(root) == result["path"]


def test_feedback_id_takes_effect_once(client, manager):
    pid, root = owned_project(manager)
    url = f"/api/v1/projects/{pid}/feedback"
    first = client.post(url, headers=token("feedback.write"), json=feedback_body())
    head = _git(root, "rev-parse", "HEAD")
    repeat = client.post(url, headers=token("feedback.write"), json=feedback_body(summary={"score": 1}))
    assert repeat.status_code == 200 and repeat.json()["path"] == first.json()["path"]
    assert _git(root, "rev-parse", "HEAD") == head
    assert len(list((root / "feedback").glob("*.json"))) == 1


def test_feedback_release_version_must_exist(client, manager, tmp_path):
    pid, _ = owned_project(manager)
    url = f"/api/v1/projects/{pid}/feedback"
    missing = client.post(url, headers=token("feedback.write"), json=feedback_body(release_version="0.1.99"))
    assert missing.status_code == 422 and missing.json()["code"] == "feedback_invalid"
    record = manager.publish_package(pid, tmp_path / "pkg.zip")
    ok = client.post(url, headers=token("feedback.write"), json=feedback_body(release_version=record["version"]))
    assert ok.status_code == 201 and ok.json()["release_version"] == record["version"]


@pytest.mark.parametrize("body", [
    [],
    {"source": {"service": "s"}, "items": [{"case_id": "c", "question": "q"}]},
    {"feedback_id": "f", "items": [{"case_id": "c", "question": "q"}]},
    {"feedback_id": "f", "source": {"service": "s"}, "items": []},
    {"feedback_id": "f", "source": {"service": "s"}, "items": [{"case_id": "c", "question": ""}]},
    {"feedback_id": "f", "source": {"service": "s"}, "items": [{"question": "q"}]},
    {"feedback_id": "f", "source": {"service": "s"}, "items": [{"case_id": "c", "question": "q", "verdict": "pass"}]},
    {"feedback_id": "../escape", "source": {"service": "s"}, "items": [{"case_id": "c", "question": "q"}]},
    {"feedback_id": "f", "source": {"service": "s"}, "release_version": "latest", "items": [{"case_id": "c", "question": "q"}]},
    {"feedback_id": "f", "source": {"service": "s"}, "items": [{"case_id": "c", "question": "q"}] * 201},
])
def test_invalid_feedback_is_422(client, manager, body):
    pid, root = owned_project(manager)
    response = client.post(f"/api/v1/projects/{pid}/feedback", headers=token("feedback.write"), json=body)
    assert response.status_code == 422 and response.json()["code"] == "feedback_invalid"
    assert response.json()["details"]["errors"]
    assert not (root / "feedback").exists()


def test_feedback_body_size_is_capped(client, manager, monkeypatch):
    pid, _ = owned_project(manager)
    monkeypatch.setattr(feedback_inbox, "MAX_BODY_BYTES", 1024)
    response = client.post(
        f"/api/v1/projects/{pid}/feedback", headers=token("feedback.write"),
        json=feedback_body(source={"service": "x" * 2000}),
    )
    assert response.status_code == 413
