"""I1：Studio 新建条目、删除条目（契约 ST-09、ST-10）。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.kernel.changesets import service as changesets
from app.main import app
from app.services import auth, streams
from app.services.workspace import WorkspaceManager
from tests import proc


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    workspaces = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", workspaces)
    monkeypatch.setattr(auth, "ENABLED", False)
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    project_id = workspaces.create_project("条目合成", owner="employee_0001")["id"]
    root = workspaces.project_root(project_id)
    streams.reset_all()
    with TestClient(app) as client:
        yield client, project_id, root
    streams.reset_all()


def last_message(root: Path) -> str:
    return proc.run(["git", "log", "-1", "--format=%B"], cwd=root, capture_output=True, text=True).stdout


def test_create_entry_from_template(api):
    client, pid, root = api
    base = f"/api/v1/projects/{pid}/studio/entries"
    res = client.post(base, json={"kind": "object", "name": "门店等级"})
    assert res.status_code == 201
    entry = res.json()
    assert entry["id"] == "object:门店等级" and entry["name"] == "门店等级" and entry["path"] == "ontology/object/门店等级.md"
    text = (root / "ontology/object/门店等级.md").read_text(encoding="utf-8")
    assert text.startswith("# 对象描述") and "# 对象属性清单" in text
    # 只有章节骨架：没有给助手看的填写指引，属性清单留表头
    assert "{待填写" not in text and "\n>" not in text and "| 中文名称 | 英文名称 |" in text
    for title in ("# 对象别名", "# 英文名称", "# 数据来源", "# 查询边界", "# 关联对象"):
        assert f"\n{title}\n" in text
    assert "Onto-Source: studio" in last_message(root)
    # 同名：不管类型，[[链接]] 按名字认
    dup = client.post(base, json={"kind": "rule", "name": "门店等级"})
    assert dup.status_code == 409 and dup.json()["code"] == "revision_conflict" and dup.json()["details"]["kind"] == "name"
    assert client.post(base, json={"kind": "bogus", "name": "x"}).json()["code"] == "invalid_argument"
    assert client.post(base, json={"kind": "rule", "name": ""}).json()["code"] == "invalid_argument"
    assert client.post(base, json={"kind": "rule", "name": "a/b"}).json()["code"] == "invalid_argument"


def test_create_with_key_keeps_display_name(api):
    client, pid, root = api
    base = f"/api/v1/projects/{pid}/studio/entries"
    entry = client.post(base, json={"kind": "rule", "name": "陈列上限", "key": "display_cap", "from_template": False}).json()
    assert entry["id"] == "rule:display_cap" and entry["name"] == "陈列上限"
    text = (root / "ontology/rule/display_cap.md").read_text(encoding="utf-8")
    assert text.startswith('---\nreport_name: "陈列上限"\n---\n') and "{待填写" not in text and "# 规则描述" in text


def test_create_flowchart_needs_diagram_pack(api):
    client, pid, _root = api
    base = f"/api/v1/projects/{pid}/studio/entries"
    res = client.post(base, json={"kind": "flowchart", "name": "超量处理流程"})
    assert res.status_code == 201 and res.json()["type"] == "diagram.flowchart" and res.json()["key"]
    again = client.post(base, json={"kind": "flowchart", "name": "超量处理流程"})
    assert again.status_code == 409 and again.json()["details"]["kind"] == "name"
    assert client.post(f"/api/v1/projects/{pid}/packs/diagram/disable", json={}).status_code == 200
    off = client.post(base, json={"kind": "flowchart", "name": "另一个流程"})
    assert off.status_code == 409 and off.json()["code"] == "pack_not_enabled" and off.json()["details"]["pack"] == "diagram"


def test_create_respects_write_scope(api, monkeypatch):
    client, pid, _root = api
    lock = {**changesets.empty_scope(), "state": "assistant_running", "paths": ["ontology/object/*"], "entries": []}
    monkeypatch.setattr(changesets, "scope_lock", lambda manager, project_id: lock)
    res = client.post(f"/api/v1/projects/{pid}/studio/entries", json={"kind": "object", "name": "锁内对象"})
    assert res.status_code == 423 and res.json()["code"] == "write_scope_locked"


def test_delete_entry(api):
    client, pid, root = api
    base = f"/api/v1/projects/{pid}/studio/entries"
    client.post(base, json={"kind": "object", "name": "门店"})
    rule = client.post(base, json={"kind": "rule", "name": "门店规则", "from_template": False}).json()
    (root / "ontology/rule/门店规则.md").write_text("# 规则描述\n\n约束 [[门店]]\n", encoding="utf-8")
    proc.run(["git", "-c", "core.hooksPath=/dev/null", "commit", "-qam", "web: 引用"], cwd=root, check=True)
    blocked = client.request("DELETE", f"{base}/object:门店", json={})
    assert blocked.status_code == 409 and blocked.json()["code"] == "entry_referenced"
    assert blocked.json()["details"]["references"][0]["id"] == "rule:门店规则"
    stale = client.request("DELETE", f"{base}/{rule['id']}", json={"base_revision": "stale"})
    assert stale.status_code == 409 and stale.json()["code"] == "revision_conflict"
    done = client.request("DELETE", f"{base}/{rule['id']}", json={})
    assert done.status_code == 200 and done.json()["version_label"].startswith("v0.")
    assert not (root / "ontology/rule/门店规则.md").exists()
    assert client.request("DELETE", f"{base}/{rule['id']}", json={}).status_code == 404
    flow = client.post(base, json={"kind": "flowchart", "name": "门店流程"}).json()
    assert client.request("DELETE", f"{base}/flowchart:{flow['key']}", json={}).status_code == 200


def test_entries_carry_updated_at_and_sort_by_it(api):
    """ST-03 最后更新时间：已提交的取提交时间，未提交的取文件修改时间；sort=updated 新的在前。"""
    import os
    import time

    client, pid, root = api
    base = f"/api/v1/projects/{pid}/studio/entries"
    assert client.post(base, json={"kind": "rule", "name": "合成规则甲"}).status_code == 201
    loose = root / "ontology/rule/合成规则乙.md"
    loose.write_text("# 规则描述\n\n未提交的合成规则\n", encoding="utf-8")
    future = int(time.time()) + 3600
    os.utime(loose, (future, future))

    rows = {row["id"]: row for row in client.get(base).json()["items"]}
    assert rows["rule:合成规则甲"]["updated_at"]
    assert rows["rule:合成规则乙"]["updated_at"] == deps.iso(future)

    ordered = [row["id"] for row in client.get(base, params={"sort": "updated"}).json()["items"]]
    assert ordered.index("rule:合成规则乙") < ordered.index("rule:合成规则甲")
