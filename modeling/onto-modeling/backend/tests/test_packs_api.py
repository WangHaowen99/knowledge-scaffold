"""插件接口：上传与审核、启用、停用、升级、回滚、撤销切换、装配状态（契约 3.16；方案 D1～D4）。"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.main import app
from app.services import auth
from app.services.packs import builtin, lockfile
from app.services.workspace import WorkspaceManager
from tests.pack_helpers import make_assets, make_pack
from tests.proc import run as run_cmd


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    builtin.clear_cache()
    manager = WorkspaceManager(tmp_path / "data", make_assets(tmp_path))
    monkeypatch.setattr(deps, "workspaces", manager)
    monkeypatch.setattr(auth, "ENABLED", False)  # 本地模式：同时是所有者与管理员
    client = TestClient(app)
    project_id = client.post("/api/v1/projects", json={"name": "插件", "goal": "合成场景：图书借阅建模"}).json()["id"]
    yield client, manager, project_id, tmp_path
    builtin.clear_cache()


def zipped(directory: Path, prefix: str = "") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                zf.write(path, prefix + path.relative_to(directory).as_posix())
    return buffer.getvalue()


def upload(client: TestClient, data: bytes, *, tenant: str = "", path: str = "/api/v1/admin/packs/uploads"):
    files = {"file": ("pack.zip", data, "application/zip")}
    return client.post(path, files=files, data={"tenant_label": tenant} if tenant else None)


def publish_pack(client: TestClient, tmp_path: Path, name: str, version: str, **kw) -> dict:
    source = make_pack(tmp_path / "src" / version, name, version, **kw)
    accepted = upload(client, zipped(source))
    assert accepted.status_code == 202 and accepted.json()["state"] == "checking"
    upload_id = accepted.json()["id"]
    detail = client.get(f"/api/v1/admin/packs/uploads/{upload_id}").json()
    assert detail["state"] == "passed", detail["checks"]
    published = client.post(f"/api/v1/admin/packs/uploads/{upload_id}/publish", json={"channel": "stable"})
    assert published.status_code == 200, published.text
    return detail


def git_log(root: Path) -> str:
    return run_cmd(["git", "log", "-1", "--format=%s%n%b"], cwd=root, capture_output=True, text=True).stdout


# ---- 上传与审核 ----


def test_upload_check_publish_and_report(env):
    client, manager, _, tmp_path = env
    detail = publish_pack(client, tmp_path, "trace", "0.9.1",
                          files={"commands/trace-run.md": "运行\n", "agents/trace-agent.md": "子代理\n"})
    assert detail["manifest"]["name"] == "trace" and detail["manifest"]["contributes"]["commands"] == 1
    assert [item["text"] for item in detail["timeline"]] == ["上传", "静态检查通过"]
    assert manager.packs.repository.channel("trace", "stable") == "0.9.1"
    report = client.get(f"/api/v1/admin/packs/uploads/{detail['id']}/report")
    assert report.headers["content-type"].startswith("text/markdown") and "静态检查报告" in report.text
    listing = client.get("/api/v1/admin/packs/uploads?state=published").json()
    assert [item["id"] for item in listing["items"]] == [detail["id"]]
    again = client.post(f"/api/v1/admin/packs/uploads/{detail['id']}/publish", json={"channel": "stable"})
    assert again.status_code == 409 and again.json()["code"] == "pack_upload_state"


def test_tenant_pack_with_frontend_code_fails_and_cannot_publish(env):
    client, manager, _, tmp_path = env
    source = make_pack(tmp_path / "src", "dash", "1.0.0", tenant=True,
                       files={"commands/dash-run.md": "运行\n", "web/app.js": "console.log(1)\n"})
    upload_id = upload(client, zipped(source), tenant="租户 A").json()["id"]
    detail = client.get(f"/api/v1/admin/packs/uploads/{upload_id}").json()
    assert detail["state"] == "failed" and detail["tenant_label"] == "租户 A"
    assert next(item for item in detail["checks"] if item["id"] == "frontend")["result"] == "fail"
    blocked = client.post(f"/api/v1/admin/packs/uploads/{upload_id}/publish", json={"channel": "stable"})
    assert blocked.status_code == 422 and blocked.json()["code"] == "static_check_failed"
    assert not manager.packs.repository.versions("dash")  # 失败的上传不进包仓库
    rejected = client.post(f"/api/v1/admin/packs/uploads/{upload_id}/reject", json={"reason": "带前端代码"})
    assert rejected.json()["state"] == "rejected"


def test_upload_rejects_bad_archives(env):
    client, _, _, tmp_path = env
    assert upload(client, b"not-a-zip").json()["code"] == "pack_upload_invalid"
    source = make_pack(tmp_path / "src", "trace", "0.9.1", checksums=False)
    body = upload(client, zipped(source)).json()
    assert body["code"] == "pack_upload_invalid" and body["details"]["reason"] == "checksums_missing"
    evil = io.BytesIO()
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../escape.md", "x")
    assert upload(client, evil.getvalue()).json()["details"]["reason"] == "path_traversal"


def test_upload_with_wrapping_directory_and_tampered_checksum(env):
    client, _, _, tmp_path = env
    source = make_pack(tmp_path / "src", "trace", "0.9.1")
    wrapped = upload(client, zipped(source, prefix="trace-0.9.1/")).json()
    assert client.get(f"/api/v1/admin/packs/uploads/{wrapped['id']}").json()["state"] == "passed"
    (source / "commands" / "trace-run.md").write_text("改过\n", encoding="utf-8")
    tampered = upload(client, zipped(source)).json()
    detail = client.get(f"/api/v1/admin/packs/uploads/{tampered['id']}").json()
    assert detail["state"] == "failed" and not detail["checksums_ok"]


def test_legacy_assets_upload_becomes_core_version_on_stable(env):
    client, manager, project_id, tmp_path = env
    old = manager.packs.repository.channel("core", "stable")
    legacy = tmp_path / "legacy"
    for rel, text in {"commands/ontology.md": "新的入口\n", "agents/research-agent.md": "子代理\n",
                      "scripts/resource_write_guard.sh": "exit 0\n", "settings.json": "{}"}.items():
        (legacy / rel).parent.mkdir(parents=True, exist_ok=True)
        (legacy / rel).write_text(text, encoding="utf-8")
    response = upload(client, zipped(legacy, prefix=".claude/"), path="/api/settings/assets/upload")
    assert response.status_code == 202 and response.headers["deprecation"] == "true"
    new = manager.packs.repository.channel("core", "stable")
    assert new != old and new.startswith("0.1.0+")
    pack = manager.packs.repository.version_dir("core", new)
    # 旧资产包里的守卫与 settings.json 归内核，不进 core；规则从镜像补齐
    assert not (pack / "settings.json").exists() and not (pack / "scripts" / "resource_write_guard.sh").exists()
    assert (pack / "workspace-rules.md").is_file()
    # 跟随 stable 的项目：下一条消息前升级（这里直接走消息前的准备）
    from app.services.packs import service

    service.prepare_for_message(manager, project_id)
    root = manager.project_root(project_id)
    assert lockfile.read(root).get("core").version == new
    assert (root / ".claude" / "commands" / "ontology.md").read_text(encoding="utf-8") == "新的入口\n"


# ---- 按项目启用、升级、回滚、停用 ----


def test_enable_upgrade_rollback_disable_flow(env, monkeypatch):
    client, manager, project_id, tmp_path = env
    root = manager.project_root(project_id)
    publish_pack(client, tmp_path, "trace", "0.9.0")
    from app.services.packs import service

    # 助手在跑：改能力包等下一条消息前切换（空闲时当场切，见下一个用例）
    monkeypatch.setattr(service, "_assistant_idle", lambda project_id: False)

    catalog = client.get(f"/api/v1/packs?project_id={project_id}").json()
    trace = next(item for item in catalog["items"] if item["name"] == "trace")
    assert trace["project_status"] == "available" and catalog["counts"]["all"] == 2
    assert next(item for item in catalog["items"] if item["name"] == "core")["project_status"] == "required"

    compat = client.get(f"/api/v1/packs/trace/versions/0.9.0/compatibility?project_id={project_id}").json()
    assert compat["ok"] and {check["id"] for check in compat["checks"]} == {
        "kernel_api", "engines", "depends", "names", "flow", "page_api"}
    assert compat["effective"] == "next_message"

    enabled = client.post(f"/api/v1/projects/{project_id}/packs", json={"name": "trace"})
    assert enabled.status_code == 201, enabled.text
    body = enabled.json()
    assert body["effective"] == "next_message" and body["pack"]["status"] == "pending"
    assert body["commit"] and body["version_label"].startswith("v0.")
    assert "plugin: 启用 trace 包 0.9.0" in git_log(root) and "Onto-Source: plugin" in git_log(root)
    assert lockfile.read(root).get("trace").version == "0.9.0"
    assembly = client.get(f"/api/v1/projects/{project_id}/assembly").json()
    assert assembly["state"] == "pending" and assembly["pending_changes"] == [
        {"pack": "trace", "from": None, "to": "0.9.0", "kind": "enable"}]
    assert not (root / ".claude" / "commands" / "trace-run.md").exists()  # 下一条消息前才切
    again = client.post(f"/api/v1/projects/{project_id}/packs", json={"name": "trace"})
    assert again.status_code == 409 and again.json()["code"] == "pack_already_enabled"

    # 撤销待生效的切换：锁文件改回去
    cancelled = client.post(f"/api/v1/projects/{project_id}/packs/trace/cancel-pending")
    assert cancelled.status_code == 200 and cancelled.json()["pack"]["status"] == "disabled"
    assert client.get(f"/api/v1/projects/{project_id}/assembly").json()["state"] == "applied"
    nothing = client.post(f"/api/v1/projects/{project_id}/packs/trace/cancel-pending")
    assert nothing.json()["code"] == "no_pending_switch"

    # 重新启用并让它生效（下一条消息前的准备）
    client.post(f"/api/v1/projects/{project_id}/packs", json={"name": "trace"})
    service.prepare_for_message(manager, project_id)
    assert (root / ".claude" / "commands" / "trace-run.md").is_file()
    packs = client.get(f"/api/v1/projects/{project_id}/packs").json()
    assert {item["name"]: item["status"] for item in packs["items"]} == {"core": "required", "trace": "enabled"}

    publish_pack(client, tmp_path, "trace", "0.10.0", files={"commands/trace-run.md": "新版\n"})
    trace = next(item for item in client.get(f"/api/v1/projects/{project_id}/packs").json()["items"] if item["name"] == "trace")
    assert trace["latest_available"] == "0.10.0" and "upgrade" in trace["actions"]
    upgraded = client.post(f"/api/v1/projects/{project_id}/packs/trace/upgrade", json={"to_version": "0.10.0"})
    assert upgraded.status_code == 200 and upgraded.json()["pack"]["pending"]["kind"] == "upgrade"
    service.prepare_for_message(manager, project_id)
    assert (root / ".claude" / "commands" / "trace-run.md").read_text(encoding="utf-8") == "新版\n"

    rolled = client.post(f"/api/v1/projects/{project_id}/packs/trace/rollback", json={"to_version": "0.9.0"})
    assert rolled.json()["pack"]["pending"]["kind"] == "rollback"
    assert lockfile.read(root).get("trace").follow is False  # 回滚后锁定版本，不再跟随通道
    service.prepare_for_message(manager, project_id)
    assert (root / ".claude" / "commands" / "trace-run.md").read_text(encoding="utf-8") == "运行 $ARGUMENTS\n"

    disabled = client.post(f"/api/v1/projects/{project_id}/packs/trace/disable")
    assert disabled.status_code == 200 and lockfile.read(root).get("trace").enabled is False
    core = client.post(f"/api/v1/projects/{project_id}/packs/core/disable")
    assert core.status_code == 409 and core.json()["code"] == "pack_required"
    missing = client.post(f"/api/v1/projects/{project_id}/packs/trace/upgrade", json={"to_version": "0.10.0"})
    assert missing.json()["code"] == "pack_not_enabled"


def test_capability_switch_while_assistant_idle_takes_effect_now(env, monkeypatch):
    """项目里没有助手在跑：启用、升级、停用能力包当场切换装配目录，不再等下一条消息（评审 1.1、2.2）。"""
    client, manager, project_id, tmp_path = env
    root = manager.project_root(project_id)
    publish_pack(client, tmp_path, "trace", "0.9.0")
    from app.services.packs import service

    compat = client.get(f"/api/v1/packs/trace/versions/0.9.0/compatibility?project_id={project_id}").json()
    assert compat["effective"] == "immediate"
    enabled = client.post(f"/api/v1/projects/{project_id}/packs", json={"name": "trace"})
    assert enabled.status_code == 201, enabled.text
    assert enabled.json()["effective"] == "immediate" and enabled.json()["pack"]["status"] == "enabled"
    assert (root / ".claude" / "commands" / "trace-run.md").is_file()
    assembly = client.get(f"/api/v1/projects/{project_id}/assembly").json()
    assert assembly["state"] == "applied" and not assembly["pending_changes"]

    publish_pack(client, tmp_path, "trace", "0.10.0", files={"commands/trace-run.md": "新版\n"})
    upgraded = client.post(f"/api/v1/projects/{project_id}/packs/trace/upgrade", json={"to_version": "0.10.0"})
    assert upgraded.status_code == 200 and upgraded.json()["effective"] == "immediate"
    assert upgraded.json()["pack"]["pending"] is None
    assert (root / ".claude" / "commands" / "trace-run.md").read_text(encoding="utf-8") == "新版\n"

    disabled = client.post(f"/api/v1/projects/{project_id}/packs/trace/disable")
    assert disabled.status_code == 200 and disabled.json()["effective"] == "immediate"
    assert not (root / ".claude" / "commands" / "trace-run.md").exists()

    # 助手在跑时照旧：预告与结果都是下一条消息生效
    monkeypatch.setattr(service, "_assistant_idle", lambda project_id: False)
    compat = client.get(f"/api/v1/packs/trace/versions/0.10.0/compatibility?project_id={project_id}").json()
    assert compat["effective"] == "next_message"
    again = client.post(f"/api/v1/projects/{project_id}/packs", json={"name": "trace"})
    assert again.json()["effective"] == "next_message" and again.json()["pack"]["status"] == "pending"
    assert not (root / ".claude" / "commands" / "trace-run.md").exists()


def test_knowledge_pack_takes_effect_immediately(env):
    client, _, project_id, tmp_path = env
    publish_pack(client, tmp_path, "retail-kb", "1.0.0", kind="knowledge", files={"reference/零售术语.md": "术语\n"})
    enabled = client.post(f"/api/v1/projects/{project_id}/packs", json={"name": "retail-kb"}).json()
    assert enabled["effective"] == "immediate" and enabled["pack"]["status"] == "enabled"
    root = deps.workspaces.project_root(project_id)
    # 行业包这类纯数据包在装配目录里各占一个子目录
    assert (root / ".claude" / "industry" / "retail-kb" / "reference" / "零售术语.md").is_file()


def test_incompatible_pack_leaves_lock_untouched(env):
    client, manager, project_id, tmp_path = env
    root = manager.project_root(project_id)
    # 依赖范围对不上的包过不了静态检查，这里直接放进包仓库模拟「兼容范围后来变了」
    source = make_pack(tmp_path / "bad", "needy", "1.0.0", depends={"core": ">=9.0"}, files={"commands/needy-run.md": "x\n"})
    manager.packs.repository.install(source, origin="upload")
    before = (root / ".onto" / "packs.lock").read_text(encoding="utf-8")
    compat = client.get(f"/api/v1/packs/needy/versions/1.0.0/compatibility?project_id={project_id}").json()
    assert not compat["ok"] and compat["blocked_reason"]
    response = client.post(f"/api/v1/projects/{project_id}/packs", json={"name": "needy"})
    assert response.status_code == 409 and response.json()["code"] == "pack_incompatible"
    assert (root / ".onto" / "packs.lock").read_text(encoding="utf-8") == before


def test_name_conflict_is_reported_by_compatibility(env):
    client, manager, project_id, tmp_path = env
    source = make_pack(tmp_path / "clash", "clash", "1.0.0", files={"commands/ontology.md": "冒名\n"})
    manager.packs.repository.install(source, origin="upload")
    compat = client.get(f"/api/v1/packs/clash/versions/1.0.0/compatibility?project_id={project_id}").json()
    names = next(check for check in compat["checks"] if check["id"] == "names")
    assert not names["ok"] and "commands/ontology.md" in names["detail"]


def test_pack_detail_and_errors(env):
    client, _, project_id, tmp_path = env
    publish_pack(client, tmp_path, "trace", "0.9.0", files={"commands/trace-run.md": "x\n", "agents/trace-agent.md": "y\n"})
    detail = client.get(f"/api/v1/packs/trace?project_id={project_id}").json()
    assert detail["version"] == "0.9.0" and {group["group"] for group in detail["contents"]} == {"命令", "子代理"}
    assert detail["requires"]["kernel_api"] == ">=1.0 <2.0" and detail["changelog"][0]["version"] == "0.9.0"
    assert client.get("/api/v1/packs/nope").json()["code"] == "pack_not_found"
    assert client.get("/api/v1/packs/trace?version=9.9.9").json()["code"] == "pack_version_not_found"
    assert client.post(f"/api/v1/projects/{project_id}/packs", json={"name": "nope"}).json()["code"] == "pack_not_found"


def test_times_are_rfc3339(env):
    import re

    client, _, project_id, tmp_path = env
    publish_pack(client, tmp_path, "trace", "0.9.0")
    rfc = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
    enabled = client.post(f"/api/v1/projects/{project_id}/packs", json={"name": "trace"}).json()
    assert rfc.match(enabled["pack"]["enabled_at"])
    items = client.get(f"/api/v1/projects/{project_id}/packs").json()["items"]
    assert all(item["enabled_at"] is None or rfc.match(item["enabled_at"]) for item in items)
    detail = client.get("/api/v1/packs/trace").json()
    assert rfc.match(detail["released_at"]) and rfc.match(detail["changelog"][0]["date"])


# ---- 锁文件经写入事务提交（方案 C1）----


def _trailer_values(root: Path, key: str) -> list[str]:
    out = run_cmd(["git", "log", "-1", f"--format=%(trailers:key={key},valueonly)"], cwd=root,
                  capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if line.strip()]


def test_lock_commit_goes_through_change_transaction(env):
    """启用包：只提交锁文件，来源 trailer 只有一份（plugin），作者是操作者；agent 的其他改动不带进来。"""
    client, manager, project_id, tmp_path = env
    root = manager.project_root(project_id)
    publish_pack(client, tmp_path, "trace", "0.9.0")
    (root / "research").mkdir(exist_ok=True)
    (root / "research" / "agent-draft.md").write_text("助手还没提交的草稿\n", encoding="utf-8")

    assert client.post(f"/api/v1/projects/{project_id}/packs", json={"name": "trace"}).status_code == 201
    assert _trailer_values(root, "Onto-Source") == ["plugin"]
    assert _trailer_values(root, "Onto-Author") == ["local"]
    changed = run_cmd(["git", "show", "--name-only", "--format=", "HEAD"], cwd=root,
                      capture_output=True, text=True).stdout.split()
    assert changed == [lockfile.LOCK_REL]
    assert "research/agent-draft.md" in run_cmd(["git", "status", "--porcelain"], cwd=root,
                                                capture_output=True, text=True).stdout


def test_lock_file_restored_when_commit_fails(env, monkeypatch):
    client, manager, project_id, tmp_path = env
    root = manager.project_root(project_id)
    publish_pack(client, tmp_path, "trace", "0.9.0")
    before = (root / lockfile.LOCK_REL).read_bytes()
    head = run_cmd(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout

    from app.kernel.writepath import tx as writepath
    from app.services.workspace.errors import WorkspaceError

    def broken(*_args, **_kwargs):
        raise WorkspaceError("git 索引正被其他 git 操作占用（index.lock），请稍后重试", 500)

    monkeypatch.setattr(writepath, "commit_paths", broken)
    response = client.post(f"/api/v1/projects/{project_id}/packs", json={"name": "trace"})
    assert response.status_code == 500
    assert (root / lockfile.LOCK_REL).read_bytes() == before
    assert run_cmd(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout == head
