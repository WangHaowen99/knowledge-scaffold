"""普通用户装自己的插件包（ADR-0079、ADR-0080，契约 PL-22～PL-26）：只归上传者、能带的内容和管理员一样、不进全局。

用户名、包名均为合成数据。
"""

from __future__ import annotations

import io
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.main import app
from app.services import auth
from app.services.autobuild import views
from app.services.flow import view
from app.services.packs import builtin, extensions, governance, lockfile, uploads
from app.services.packs import service as pack_service
from app.services.workspace import WorkspaceManager
from tests.pack_helpers import make_assets, make_node_pack, make_pack

OWNER, OTHER, ADMIN = "operator_a", "operator_b", "operator_admin"
TOKENS = {
    "owner-token": {"sub": OWNER, "role": "user"},
    "other-token": {"sub": OTHER, "role": "user"},
    "admin-token": {"sub": ADMIN, "role": "admin"},
}
AS_OWNER = {"Authorization": "Bearer owner-token"}
AS_OTHER = {"Authorization": "Bearer other-token"}
AS_ADMIN = {"Authorization": "Bearer admin-token"}
MINE = "/api/v1/me/pack-uploads"
ADMIN_UPLOADS = "/api/v1/admin/packs/uploads"


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    builtin.clear_cache()
    manager = WorkspaceManager(tmp_path / "data", make_assets(tmp_path))
    monkeypatch.setattr(deps, "workspaces", manager)
    monkeypatch.setattr(auth, "ENABLED", True)
    monkeypatch.setattr(auth, "verify_access_token", lambda token: TOKENS.get(token))
    yield TestClient(app), manager, tmp_path
    builtin.clear_cache()


@pytest.fixture()
def real_flow_env(tmp_path: Path, monkeypatch):
    """用镜像里的真实资产：节点要组装进主流程。"""
    builtin.clear_cache()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    manager = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", manager)
    monkeypatch.setattr(auth, "ENABLED", True)
    monkeypatch.setattr(auth, "verify_access_token", lambda token: TOKENS.get(token))
    yield TestClient(app), manager, tmp_path
    builtin.clear_cache()


def zipped(directory: Path) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(directory).as_posix())
    return buffer.getvalue()


def post_zip(client, path, data, headers):
    return client.post(path, files={"file": ("pack.zip", data, "application/zip")}, headers=headers)


def upload(client, tmp_path, name, version="0.1.0", headers=None, path=MINE, **kw) -> dict:
    headers = AS_OWNER if headers is None else headers
    accepted = post_zip(client, path, zipped(make_pack(tmp_path / "src" / version, name, version, **kw)), headers)
    assert accepted.status_code == 202, accepted.text
    return client.get(f"{path}/{accepted.json()['id']}", headers=headers).json()


def install(client, tmp_path, name, version="0.1.0", headers=None, **kw) -> dict:
    headers = AS_OWNER if headers is None else headers
    detail = upload(client, tmp_path, name, version, headers, **kw)
    assert detail["state"] == "passed", detail["checks"]
    done = client.post(f"{MINE}/{detail['id']}/install", json={}, headers=headers)
    assert done.status_code == 200, done.text
    assert done.json()["state"] == "installed"
    return done.json()


def project(client, headers=None, **body) -> str:
    headers = AS_OWNER if headers is None else headers
    created = client.post("/api/v1/projects", json={"name": "合成项目", "goal": "合成场景", **body}, headers=headers)
    assert created.status_code in (200, 201), created.text
    return created.json()["id"]


def catalog_names(client, headers) -> set[str]:
    return {item["name"] for item in client.get("/api/v1/packs", headers=headers).json()["items"]}


def test_owner_installs_and_enables_only_for_themselves(env):
    client, manager, tmp_path = env
    install(client, tmp_path, "my-notes")
    assert "my-notes" in catalog_names(client, AS_OWNER)
    assert "my-notes" not in catalog_names(client, AS_OTHER)
    card = next(item for item in client.get("/api/v1/packs", headers=AS_OWNER).json()["items"] if item["name"] == "my-notes")
    assert card["publisher"]["kind"] == "private"

    assert client.get("/api/v1/packs/my-notes", headers=AS_OWNER).status_code == 200
    assert client.get("/api/v1/packs/my-notes", headers=AS_OTHER).status_code == 404
    assert client.get("/api/v1/packs/my-notes", headers=AS_ADMIN).status_code == 404

    mine = project(client)
    assert client.post(f"/api/v1/projects/{mine}/packs", json={"name": "my-notes"}, headers=AS_OWNER).status_code == 201
    entry = lockfile.read(manager.project_root(mine)).get("my-notes")
    assert entry is not None and entry.enabled

    theirs = project(client, AS_OTHER)
    denied = client.post(f"/api/v1/projects/{theirs}/packs", json={"name": "my-notes"}, headers=AS_OTHER)
    assert denied.status_code == 404 and denied.json()["code"] == "pack_not_found"
    assert lockfile.read(manager.project_root(theirs)).get("my-notes") is None


def test_project_wizard_cannot_reach_someone_elses_pack(env):
    client, manager, tmp_path = env
    install(client, tmp_path, "my-notes")
    theirs = project(client, AS_OTHER, capabilities={"my-notes": True})
    assert lockfile.read(manager.project_root(theirs)).get("my-notes") is None
    mine = project(client, capabilities={"my-notes": True})
    assert lockfile.read(manager.project_root(mine)).get("my-notes") is not None


def test_private_pack_stays_out_of_platform_wide_scans(env):
    client, manager, tmp_path = env
    install(client, tmp_path, "my-notes", default_on=True)
    repository = manager.packs.repository
    assert "my-notes" not in repository.names() and "my-notes" in repository.names(private=True)
    assert "my-notes" not in manager.packs.default_capabilities()
    # 不进配置注册、指标、告警；但管理员要能在插件运维里禁用
    assert "my-notes" not in [item["pack"].split("@")[0] for item in extensions.declared_metrics(manager)]
    assert "my-notes" in {item["name"] for item in governance.ops_items(manager)}


@pytest.mark.parametrize("files", [
    {"commands/my-tool-run.md": "跑\n", "scripts/my_tool.py": "print(1)\n"},
    {"commands/my-tool-run.md": "---\nallowed-tools: Bash\n---\n跑\n"},
    {"commands/my-tool-run.md": "跑\n", "settings.local.json": "{}\n"},
    {"commands/my-tool-run.md": "跑\n", "scripts/broken.py": "def (\n"},  # 语法错，两边都不过
    {"commands/my-tool-run.md": "跑\n", "src/app.js": "alert(1)\n"},  # ui/ 之外有前端代码，两边都不过
])
def test_user_and_admin_uploads_get_the_same_checks(env, files):
    client, _manager, tmp_path = env
    data = zipped(make_pack(tmp_path / "src", "my-tool", "0.1.0", files=files, tenant=True))
    mine = post_zip(client, MINE, data, AS_OWNER).json()["id"]
    admin = client.post(ADMIN_UPLOADS, files={"file": ("pack.zip", data, "application/zip")},
                        data={"tenant_label": "租户 A"}, headers=AS_ADMIN).json()["id"]
    mine = client.get(f"{MINE}/{mine}", headers=AS_OWNER).json()
    admin = client.get(f"{ADMIN_UPLOADS}/{admin}", headers=AS_ADMIN).json()

    def results(detail):
        return {item["id"]: item["result"] for item in detail["checks"] if item["id"] != "private_name"}

    assert results(mine) == results(admin) and mine["state"] == admin["state"]


def test_user_imports_scripts_and_nodes_for_their_own_project(real_flow_env):
    client, manager, tmp_path = real_flow_env
    mine = project(client)  # 建项目时 core 入库，节点包的 requires_flow 要比对它
    pack = make_node_pack(tmp_path / "src", files={
        "commands/naming-check.md": "运行 python3 .claude/scripts/naming_scan.py，写报告 $ARGUMENTS\n",
        "scripts/naming_scan.py": "print('合成')\n",
    })
    accepted = post_zip(client, MINE, zipped(pack), AS_OWNER).json()["id"]
    detail = client.get(f"{MINE}/{accepted}", headers=AS_OWNER).json()
    assert detail["state"] == "passed", [item for item in detail["checks"] if item["result"] == "fail"]
    assert client.post(f"{MINE}/{accepted}/install", json={}, headers=AS_OWNER).status_code == 200

    enabled = client.post(f"/api/v1/projects/{mine}/packs", json={"name": "naming"}, headers=AS_OWNER)
    assert enabled.status_code == 201, enabled.text
    pack_service.prepare_for_message(manager, mine)
    root = manager.project_root(mine)
    assert "naming.check" in {node["id"] for node in view.build(root)["nodes"]}
    assert (root / ".claude" / "scripts" / "naming_scan.py").is_file()

    theirs = project(client, AS_OTHER)
    assert client.post(f"/api/v1/projects/{theirs}/packs", json={"name": "naming"}, headers=AS_OTHER).status_code == 404
    assert "naming.check" not in {node["id"] for node in view.build(manager.project_root(theirs))["nodes"]}


def test_prompts_and_knowledge_data_pass(env):
    client, _manager, tmp_path = env
    assert upload(client, tmp_path, "my-prompts", files={
        "commands/my-prompts-run.md": "---\ndescription: 跑\nargument-hint: 名字\n---\n跑 $ARGUMENTS，写信到 a@example.com\n",
        "agents/my-prompts-agent.md": "---\nname: my-prompts-agent\ndescription: 子代理\ntools: Read\n---\n读资料\n",
        "templates/out.md": "# 模板\n", "reference/guide.md": "# 指南\n",
    })["state"] == "passed"
    assert upload(client, tmp_path, "my-terms", kind="knowledge", files={"knowledge/terms.md": "# 术语\n"})["state"] == "passed"


def test_users_import_every_kind_an_admin_can(env):
    client, _manager, tmp_path = env
    install(client, tmp_path, "my-terms", kind="knowledge", files={"knowledge/terms.md": "# 术语\n"})
    install(client, tmp_path, "my-exporter", kind="exporter")
    install(client, tmp_path, "my-convert", kind="provider", files={"converter.py": "def convert(data, limits):\n    return ''\n"},
            converter={"entry": "converter.py", "types": [".abc"]})


def test_names_cannot_shadow_public_or_other_users_packs(env):
    client, _manager, tmp_path = env
    # 基础建模能力与别的公开包：新版本进不了
    assert upload(client, tmp_path, "core", version="9.0.0")["state"] == "failed"
    public = upload(client, tmp_path, "team-pack", headers=AS_ADMIN, path=ADMIN_UPLOADS)
    assert client.post(f"{ADMIN_UPLOADS}/{public['id']}/publish", json={"channel": "stable"}, headers=AS_ADMIN).status_code == 200
    takeover = upload(client, tmp_path, "team-pack", version="9.0.0")
    assert takeover["state"] == "failed"
    assert any(item["id"] == "private_name" and item["result"] == "fail" for item in takeover["checks"])

    install(client, tmp_path, "my-notes")
    assert upload(client, tmp_path, "my-notes", version="0.2.0", headers=AS_OTHER)["state"] == "failed"
    # 自己可以发新版本
    install(client, tmp_path, "my-notes", version="0.2.0")


def test_name_race_is_settled_at_install(env):
    client, _manager, tmp_path = env
    late = upload(client, tmp_path, "my-notes", version="0.1.0", headers=AS_OTHER)
    assert late["state"] == "passed"  # 这时候名字还没人占
    install(client, tmp_path, "my-notes", version="0.2.0")
    lost = client.post(f"{MINE}/{late['id']}/install", json={}, headers=AS_OTHER)
    assert lost.status_code == 409 and lost.json()["code"] == "pack_name_taken"


def test_uploads_are_private_to_the_uploader(env):
    client, _manager, tmp_path = env
    detail = upload(client, tmp_path, "my-notes")
    assert detail["scope"] == "private"
    assert [item["id"] for item in client.get(MINE, headers=AS_OWNER).json()["items"]] == [detail["id"]]
    assert client.get(MINE, headers=AS_OTHER).json()["items"] == []
    for call in (client.get(f"{MINE}/{detail['id']}", headers=AS_OTHER),
                 client.get(f"{MINE}/{detail['id']}/report", headers=AS_OTHER),
                 client.post(f"{MINE}/{detail['id']}/install", json={}, headers=AS_OTHER)):
        assert call.status_code == 404
    assert client.get(f"{MINE}/{detail['id']}/report", headers=AS_OWNER).status_code == 200
    # 管理员接口普通用户仍然进不去；管理员看得到普通用户的上传
    assert client.get(ADMIN_UPLOADS, headers=AS_OWNER).status_code == 403
    assert post_zip(client, ADMIN_UPLOADS, b"x", AS_OWNER).status_code == 403
    listed = client.get(ADMIN_UPLOADS, headers=AS_ADMIN).json()["items"]
    assert [(item["id"], item["scope"]) for item in listed] == [(detail["id"], "private")]


def test_admin_can_make_a_private_pack_public(env):
    client, manager, tmp_path = env
    done = install(client, tmp_path, "my-notes")
    published = client.post(f"{ADMIN_UPLOADS}/{done['id']}/publish", json={"channel": "stable"}, headers=AS_ADMIN)
    assert published.status_code == 200, published.text
    assert manager.packs.repository.owner("my-notes") == ""
    assert "my-notes" in catalog_names(client, AS_OTHER)


def test_admin_upload_cannot_take_a_private_name(env):
    client, _manager, tmp_path = env
    install(client, tmp_path, "my-notes")
    other = upload(client, tmp_path, "my-notes", version="0.3.0", headers=AS_ADMIN, path=ADMIN_UPLOADS)
    blocked = client.post(f"{ADMIN_UPLOADS}/{other['id']}/publish", json={"channel": "stable"}, headers=AS_ADMIN)
    assert blocked.status_code == 409 and blocked.json()["code"] == "pack_name_taken"


def test_project_keeps_a_locked_pack_after_changing_hands(env):
    client, manager, tmp_path = env
    from app.services.packs import service

    install(client, tmp_path, "my-notes")
    mine = project(client)
    client.post(f"/api/v1/projects/{mine}/packs", json={"name": "my-notes"}, headers=AS_OWNER)
    service.require_visible(manager, "my-notes", OTHER, mine)  # 项目转给别人后不报错
    with pytest.raises(service.PackError):
        service.require_visible(manager, "my-notes", OTHER)


def test_zip_bombs_and_bulk_uploads_are_bounded(env, monkeypatch):
    client, manager, tmp_path = env
    source = make_pack(tmp_path / "src", "big-pack", "0.1.0", files={"commands/big-pack-run.md": "x" * 4096})
    monkeypatch.setattr(uploads, "PRIVATE_MAX_EXTRACTED_BYTES", 1024)
    too_big = post_zip(client, MINE, zipped(source), AS_OWNER)
    assert too_big.status_code == 400 and too_big.json()["details"]["reason"] == "too_large"
    monkeypatch.setattr(uploads, "PRIVATE_MAX_EXTRACTED_BYTES", 1 << 20)
    monkeypatch.setattr(uploads, "PRIVATE_MAX_FILES", 1)
    too_many = post_zip(client, MINE, zipped(source), AS_OWNER)
    assert too_many.status_code == 400 and too_many.json()["details"]["reason"] == "too_large"
    monkeypatch.setattr(uploads, "PRIVATE_MAX_FILES", 2000)
    monkeypatch.setattr(uploads, "PRIVATE_KEEP_UPLOADS", 3)
    for index in range(5):
        assert post_zip(client, MINE, zipped(source), AS_OWNER).status_code == 202
        time.sleep(0.01)
    assert len(client.get(MINE, headers=AS_OWNER).json()["items"]) == 3
    # 单个文件的上限比管理员低
    assert post_zip(client, MINE, b"0" * (uploads.PRIVATE_MAX_EXTRACTED_BYTES + (21 << 20)), AS_OWNER).status_code == 413


def test_version_quota(env, monkeypatch):
    client, _manager, tmp_path = env
    monkeypatch.setattr(uploads, "PRIVATE_MAX_VERSIONS", 1)
    install(client, tmp_path, "my-notes")
    detail = upload(client, tmp_path, "my-notes", version="0.2.0")
    blocked = client.post(f"{MINE}/{detail['id']}/install", json={}, headers=AS_OWNER)
    assert blocked.status_code == 409 and blocked.json()["code"] == "pack_quota_exceeded"


# ---- 运行报告与措辞（F8 对照实验发现的问题） ----


def _run(**extra) -> dict:
    units = [{"id": "all", "name": "全部", "kind": "slice"}, {"id": "core.validate", "name": "校验", "kind": "node"}]
    return {"run_id": "run-1", "run_no": 1, "state": "completed", "units": units, "policy": {"scope": {"mode": "all_pending"}},
            "slices": {"all": {"checks": {"errors": 0, "warnings": 8}}, "core.validate": {"checks": {"errors": 0, "warnings": 0}}},
            "ledger": [], "budget": {}, **extra}


def test_report_counts_what_is_left_at_the_end():
    infos = [views._info(_run(), unit) for unit in ("all", "core.validate")]
    assert views._checks(_run(), infos) == {"errors": 0, "warnings": 8}  # 老运行：各单元相加
    assert views._checks(_run(final_checks={"errors": 1, "warnings": 2}), infos) == {"errors": 1, "warnings": 2}


def test_start_card_does_not_mention_segments_when_not_sliced():
    chips = {chip["label"]: chip["value"] for chip in views.start_card(_run())["chips"]}
    assert chips["范围"] == "未完成的步骤"
    sliced = _run(units=[{"id": f"s{index}", "name": f"段{index}", "kind": "slice"} for index in range(3)])
    assert {chip["label"]: chip["value"] for chip in views.start_card(sliced)["chips"]}["范围"] == "未完成的 3 段"
