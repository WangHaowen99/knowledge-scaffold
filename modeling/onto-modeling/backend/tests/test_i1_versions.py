"""I1：版本详情、某版本的文件、对比、回退预览（契约 V-02～V-05）。"""

import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.main import app
from app.services import auth, streams
from app.services.workspace import WorkspaceManager
from tests import proc


def obj(name: str, desc: str, rows=(("编号", "code"),)) -> str:
    table = "\n".join(f"| {a} | {b} | 合成 | 是 | 字符串 | 无 | 无 |" for a, b in rows)
    return (f"# 对象描述\n\n{desc}\n\n# 对象属性清单\n\n| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |\n"
            f"| --- | --- | --- | --- | --- | --- | --- |\n{table}\n\n# 关联对象\n\n无\n")


def commit(root: Path, rel: str, text: str | None, message: str) -> str:
    path = root / rel
    if text is None:
        path.unlink()
        proc.run(["git", "rm", "-q", "--cached", "--", rel], cwd=root, check=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        proc.run(["git", "add", "--", rel], cwd=root, check=True)
    proc.run(["git", "-c", "core.hooksPath=/dev/null", "commit", "-qm", message], cwd=root, check=True)
    return proc.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True,
                    text=True).stdout.strip()


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    workspaces = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", workspaces)
    monkeypatch.setattr(auth, "ENABLED", False)
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    project_id = workspaces.create_project("版本合成", owner="employee_0001")["id"]
    root = workspaces.project_root(project_id)
    streams.reset_all()
    with TestClient(app) as client:
        yield client, project_id, root
    streams.reset_all()


def test_version_detail_lists_entity_changes(api):
    client, pid, root = api
    first = commit(root, "ontology/object/门店.md", obj("门店", "合成门店"), "web: 门店")
    second = commit(root, "ontology/object/门店.md", obj("门店", "合成门店", (("编号", "code"), ("名称", "name"))),
                    "web: 门店加字段")
    base = f"/api/v1/projects/{pid}/versions"
    detail = client.get(f"{base}/{second}").json()
    assert detail["commit"] == second and detail["run"] is None and detail["current"] is True
    change = detail["changes"][0]
    assert change["target"]["id"] == "object:门店" and change["change"] == "mod" and change["review"] is None
    assert "新增 1 行" in change["detail"]
    added = client.get(f"{base}/{first[:8]}").json()
    assert added["changes"][0]["change"] == "add" and added["current"] is False
    assert client.get(f"{base}/zzzz").json()["code"] == "invalid_commit"
    assert client.get(f"{base}/{'0' * 40}").json()["code"] == "invalid_commit"


def test_version_files_and_zip(api):
    client, pid, root = api
    first = commit(root, "ontology/object/门店.md", obj("门店", "合成门店"), "web: 门店")
    commit(root, "ontology/rule/超量审批.md", "# 规则描述\n\n合成规则\n", "web: 规则")
    base = f"/api/v1/projects/{pid}/versions"
    listed = client.get(f"{base}/{first}/files").json()["items"]
    paths = [item["path"] for item in listed]
    assert "ontology/object/门店.md" in paths and "ontology/rule/超量审批.md" not in paths
    assert all(path.startswith("ontology/") for path in paths) and paths == sorted(paths)
    assert all(item["size"] > 0 for item in listed if item["path"] == "ontology/object/门店.md")
    head = client.get(f"{base}/HEAD/files", params={"prefix": "ontology/rule/"}).json()["items"]
    assert "ontology/rule/超量审批.md" in [item["path"] for item in head]
    assert all(item["path"].startswith("ontology/rule/") for item in head)
    res = client.get(f"{base}/{first}/files", params={"download": 1})
    assert res.status_code == 200 and res.headers["content-type"] == "application/zip"
    assert "attachment" in res.headers["content-disposition"]
    names = zipfile.ZipFile(io.BytesIO(res.content)).namelist()
    assert sorted(names) == paths
    assert client.get(f"{base}/{first}/files", params={"prefix": "../x"}).status_code == 403


def test_version_diff_between_versions_and_worktree(api):
    client, pid, root = api
    first = commit(root, "ontology/object/门店.md", obj("门店", "合成门店"), "web: 门店")
    second = commit(root, "ontology/object/门店.md", obj("门店", "改过的门店"), "web: 改门店")
    base = f"/api/v1/projects/{pid}/versions/diff"
    data = client.get(base, params={"from": first, "to": second, "path": "ontology/object/门店.md"}).json()
    assert data["from"]["commit"] == first and data["to"]["commit"] == second
    assert data["left"] != data["right"] and data["right_revision"] is None
    assert data["hunks"] == [{"left_start": 3, "left_lines": 1, "right_start": 3, "right_lines": 1}]
    current = client.get(base, params={"from": first, "entry": "object:门店"}).json()
    assert current["to"] == {"commit": None, "version_label": None} and current["right_revision"]
    assert current["path"] == "ontology/object/门店.md"
    assert client.get(base, params={"from": first}).json()["code"] == "invalid_argument"
    assert client.get(base, params={"from": first, "path": "ontology/object/没有.md"}).json()["code"] == "file_not_found"
    assert client.get(base, params={"from": first, "path": ".git/config"}).status_code == 403


def test_revert_preview_matches_revert(api):
    client, pid, root = api
    target = commit(root, "ontology/object/门店.md", obj("门店", "合成门店"), "web: 门店")
    commit(root, "ontology/object/门店.md", obj("门店", "改过的门店"), "web: 改门店")
    commit(root, "ontology/object/订单.md", obj("订单", "合成订单"), "web: 订单")
    lock = json.loads((root / ".onto/packs.lock").read_text(encoding="utf-8"))
    core = next(item for item in lock["packs"] if item["name"] == "core")
    core["version"] = "9.9.9"
    commit(root, ".onto/packs.lock", json.dumps(lock, ensure_ascii=False, indent=2), "chore: 合成锁变化")
    base = f"/api/v1/projects/{pid}/versions"
    preview = client.get(f"{base}/revert-preview", params={"commit": target}).json()
    files = {item["path"]: item["change"] for item in preview["files"]}
    assert files == {"ontology/object/门店.md": "restore", "ontology/object/订单.md": "delete"}
    assert len(preview["undone_versions"]) == 3 and preview["target"]["commit"] == target
    head_label = client.get(base).json()["current_version_label"]
    assert preview["next_version_label"] == f"v0.{int(head_label.split('.')[1]) + 1}"
    assert preview["packs_lock_changes"][0]["pack"] == "core" and preview["packs_lock_changes"][0]["from"] == "9.9.9"
    # 带能力的显示名（9.9.9 不在包仓库里时取仓库里别的版本的清单 label）
    assert preview["packs_lock_changes"][0]["label"] and preview["packs_lock_changes"][0]["label"] != "core"
    head = client.get(base).json()["head"]
    assert client.get(f"{base}/revert-preview", params={"commit": head}).json()["code"] == "nothing_to_revert"
    done = client.post(f"{base}/revert", json={"commit": target}).json()
    assert done["version_label"] == preview["next_version_label"]
