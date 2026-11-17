"""服务令牌接口（API 契约 3.20、3.21，ADR-0020、ADR-0098、ADR-0092）：发布记录、包清单与按版本下载、
服务令牌鉴权，以及退役的 X-01、求解验证候选包、X-04、X-05 不再提供。
发布走 conftest 的内存 Hub。

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
from app.services import publish as publish_service
from app.main import app
from app.api import deps
from app.services import auth, hub_packages, release_store, service_tokens
from app.services.workspace import WorkspaceError, WorkspaceManager, _git
from tests.test_release_publish import commit_all, rich_project

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
    monkeypatch.setattr(deps, "workspaces", instance)
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


def _publish(manager, pid, *args, **kwargs):
    """以所有者的登录身份发布到内存 Hub。"""
    return publish_service.publish_package(manager, pid, *args, token="owner-token", subject="scope_owner", **kwargs)


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
    missing = client.get(release_url(pid, "0.1.1"))
    assert missing.status_code == 401 and missing.json()["code"] == "unauthenticated"
    # 项目不在限定范围
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
    first = _publish(manager, pid, tmp_path / "first.zip", author=OWNER)
    (root / "ontology" / "rule" / "新规则.md").write_text("# 新规则\n", encoding="utf-8")
    commit_all(root, "test: second release")
    second = _publish(manager, pid, tmp_path / "second.zip", author=OWNER)

    listed = client.get(f"/api/v1/projects/{pid}/releases", headers=OWNER_HEADERS).json()
    assert [item["version"] for item in listed["items"]] == [second["version"], first["version"]]
    assert listed["next_cursor"] is None
    headers = token("releases.read")
    record = client.get(release_url(pid, first["version"]), headers=headers).json()
    # ReleaseRecord（契约 7.14）
    assert record["commit"] == first["commit"] and record["by"] == OWNER
    assert record["format"] == "ontology" and record["format_label"] == "普通知识包"
    assert record["gates"] == {"all_passed": True, "degraded": False, "unbound_objects": []}
    assert record["at"].endswith("Z") and len(record["sha256"]) == 64 and record["manifest_attached"] is True
    assert record["version_label"] == "v0." + _git(root, "rev-list", "--count", "--first-parent", first["commit"]).strip()
    assert record["download_url"] == release_url(pid, first["version"], "/download")

    response = client.get(record["download_url"], headers=headers)
    assert response.status_code == 200
    assert response.content == (tmp_path / "first.zip").read_bytes()
    assert response.headers["X-Package-Commit"] == first["commit"]
    assert response.headers["X-Package-Format"] == "ontology"
    assert response.headers["X-Package-Variant"] == "standard"
    manifest = yaml.safe_load(zip_files(response.content)[f"{first['ontology_id']}/manifest.yaml"])
    assert manifest["version"] == first["version"] and manifest["status"] == "released"
    assert record["hub"]["release_id"] == first["release_id"]

    pulled = client.get(release_url(pid, first["version"]), headers=headers).json()
    [pull] = pulled["pulls"]  # 服务令牌拉取记一次
    assert pull["at"].endswith("Z") and pull["by_token"].startswith("service:") and pull["variant"] == "standard"

    missing = client.get(release_url(pid, "0.1.999", "/download"), headers=headers)
    assert missing.status_code == 404 and missing.json()["code"] == "release_not_found"
    assert client.get(release_url(pid, "latest", "/download"), headers=headers).status_code == 400


def test_manifest_lists_package_contents(client, manager, tmp_path):
    pid, _ = owned_project(manager)
    record = _publish(manager, pid, tmp_path / "pkg.zip")
    body = client.get(release_url(pid, record["version"], "/manifest"), headers=token("releases.read")).json()
    package = hub_packages.ontology_id(pid)
    assert body["manifest"]["version"] == record["version"] and body["manifest"]["id"] == package
    assert body["sha256"] == hashlib.sha256((tmp_path / "pkg.zip").read_bytes()).hexdigest()
    assert f"{package}/manifest.yaml" in {item["path"] for item in body["files"]} and body["truncated"] is False


def test_graph_variant_is_rendered_once_from_the_tag(client, manager, tmp_path):
    pid, root = owned_project(manager)
    record = _publish(manager, pid, tmp_path / "pkg.zip")
    (root / "ontology" / "rule" / "之后.md").write_text("# 之后\n", encoding="utf-8")
    commit_all(root, "test: after release")  # 变体按标签提交渲染，不读之后的提交

    url = release_url(pid, record["version"], "/download")
    first = client.get(url, params={"variant": "graph"}, headers=token("releases.read"))
    assert first.status_code == 200 and first.headers["X-Package-Variant"] == "graph"
    files = zip_files(first.content)
    mappings = json.loads(files[f"{record['ontology_id']}/mappings/object-data-mappings.json"])
    assert mappings["objects"]["订单"]["resource"] == "订单"
    assert not any(name.endswith("之后.md") for name in files)
    again = client.get(url, params={"variant": "graph"}, headers=token("releases.read"))
    assert again.content == first.content
    assert client.get(url, params={"variant": "neo4j"}, headers=token("releases.read")).status_code == 400


def test_owner_can_read_releases_with_login_token(client, manager, tmp_path):
    pid, _ = owned_project(manager)
    record = _publish(manager, pid, tmp_path / "pkg.zip")
    assert client.get(release_url(pid, record["version"]), headers=OWNER_HEADERS).status_code == 200
    download = client.get(release_url(pid, record["version"], "/download"), params={"token": "owner-token"})
    assert download.status_code == 200  # 下载链接可用 ?token=
    assert client.get(release_url(pid, record["version"]), headers=OWNER_HEADERS).json()["pulls"] == []
    other = client.get(release_url(pid, record["version"]), headers={"Authorization": "Bearer other-token"})
    assert other.status_code == 403
    assert client.get(release_url(pid, record["version"])).status_code == 401


def test_same_version_keeps_first_stored_bytes(manager, tmp_path):
    """同一请求重放（同一 Hub 版本）只认第一次留存的字节。"""
    pid, _ = owned_project(manager)
    record = _publish(manager, pid, tmp_path / "a.zip", request_id="request_a")
    stored, _ = release_store.get(manager.data_dir, pid, record["version"], record["commit"])
    stored.write_bytes(b"synthetic-first-bytes")  # 模拟渲染器升级前留存的字节
    meta = stored.with_suffix(".json")
    data = json.loads(meta.read_text(encoding="utf-8"))
    data["sha256"] = hashlib.sha256(b"synthetic-first-bytes").hexdigest()
    meta.write_text(json.dumps(data), encoding="utf-8")

    again = _publish(manager, pid, tmp_path / "b.zip", request_id="request_a")
    assert again["version"] == record["version"]
    assert (tmp_path / "b.zip").read_bytes() == b"synthetic-first-bytes"  # 同一版本只认第一次的字节
    path, _ = publish_service.release_package(manager, pid, record["version"])
    assert path.read_bytes() == b"synthetic-first-bytes"


def test_missing_stored_package_is_rerendered_once(client, manager, tmp_path):
    pid, _ = owned_project(manager)
    record = _publish(manager, pid, tmp_path / "pkg.zip")
    shutil.rmtree(release_store.root_dir(manager.data_dir))

    assert client.get(release_url(pid, record["version"]), headers=token("releases.read")).json()["manifest_attached"] is False
    response = client.get(release_url(pid, record["version"], "/download"), headers=token("releases.read"))
    assert response.status_code == 200
    assert response.content == (tmp_path / "pkg.zip").read_bytes()  # 同一提交、同一渲染器：字节一致
    _, stored = release_store.get(manager.data_dir, pid, record["version"], record["commit"])
    assert stored["backfilled"] is True and stored["release_id"] == record["release_id"]


def test_missing_stored_package_is_fetched_from_hub_with_login(client, manager, tmp_path, synthetic_hub):
    """所有者带登录令牌下载时，留存缺失的版本从 Hub 下回缓存，字节与 Hub 上的相同。"""
    pid, _ = owned_project(manager)
    record = _publish(manager, pid, tmp_path / "pkg.zip")
    shutil.rmtree(release_store.root_dir(manager.data_dir))
    response = client.get(release_url(pid, record["version"], "/download"), headers=OWNER_HEADERS)
    assert response.status_code == 200
    assert response.content == (synthetic_hub.root / record["release_id"]).read_bytes()
    _, stored = release_store.get(manager.data_dir, pid, record["version"], record["commit"])
    assert "backfilled" not in stored and stored["package_digest"] == record["package_digest"]


def test_retired_contract_routes_are_gone(client, manager):
    """X-01、求解验证候选包、X-04、SV-04 随 ADR-0098 退役：正式版本到 Hub 取，求解验证在评测服务里做。"""
    pid, _ = owned_project(manager)
    service = token("releases.read")
    assert client.get("/api/v1/contract/releases", headers=service).status_code in (401, 404)
    assert client.get(f"/api/v1/projects/{pid}/solver/candidate", headers=service).status_code in (401, 404)
    assert client.post(f"/api/v1/projects/{pid}/solver/receipts", headers=service, json={}).status_code in (401, 404)
    assert client.get(f"/api/v1/projects/{pid}/solver/receipts", headers=OWNER_HEADERS).status_code == 404
    with pytest.raises(service_tokens.ServiceTokenError):
        service_tokens.issue({"name": "合成服务", "scopes": ["receipts.write"]})


def test_feedback_contract_is_retired(client, manager):
    """X-05 随 ADR-0092 退役：没有调用方；服务令牌、登录令牌都写不进来。"""
    pid, root = owned_project(manager)
    url = f"/api/v1/projects/{pid}/feedback"
    assert client.post(url, headers=token("releases.read"), json={"feedback_id": "round-001"}).status_code == 401
    assert client.post(url, headers=OWNER_HEADERS, json={"feedback_id": "round-001"}).status_code in (404, 405)
    assert not (root / "feedback").exists()
    with pytest.raises(service_tokens.ServiceTokenError):
        service_tokens.issue({"name": "合成服务", "scopes": ["feedback.write"]})
