"""B5c 独立审查查出的缺陷（12 条 + 1 条推断），每条一个修复前会失败的用例（「审查第 N 条」）。

包全部合成（tests/pack_helpers）；脚本节点用本机执行器模拟脚本验证网关（同 test_node_pack_lifecycle）。
"""

from __future__ import annotations

import copy
import subprocess
from pathlib import Path

import pytest
import yaml

from app.kernel.writepath import ChangeTx, Origin
from app.services.packs import checks
from app.services.workspace.errors import WorkspaceError
from tests.pack_helpers import NODE_PACK_FLOW, make_node_pack, make_pack
from tests.test_changesets import main_write, obj
from tests.test_node_pack_lifecycle import (HUMAN_NODE, SCRIPT_NODE, enable_and_apply, env, fake_sandbox,  # noqa: F401
                                            install, kinds_flow, nodes_of)


def failed(items: list[dict]) -> dict[str, str]:
    return {item["id"]: item["detail"] for item in items if item["result"] == "fail"}


def git_status(root: Path) -> None:
    subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, timeout=30)


# ---- 审查第 1 条：脚本、人工节点的输出路径越过 produces / 受保护目录 ----

def fsmonitor_script(marker: Path) -> str:
    config = ('[core]\\n\\trepositoryformatversion = 0\\n\\tbare = false\\n'
              f'\\tfsmonitor = "touch {marker}; false"')
    return f"print('{config}')\n"


def escape_flow(output: str, produces: list[str], kind: str = "script") -> dict:
    flow = copy.deepcopy(NODE_PACK_FLOW)
    flow["probes"]["escape_done"] = {"files": ["reports"], "glob": "escape.md"}
    node = {"id": "naming.lint", "title": "命名格式检查", "kind": kind, "placement": "branch", "after": "core.object",
            "done": ["escape_done"], "produces": produces}
    if kind == "script":
        node["script"] = {"name": "naming_lint.py", "inputs": [], "output": output}
    else:
        node["decision"] = {"title": "会签", "options": [{"id": "pass", "label": "已签署"}, {"id": "fail", "label": "未通过"}],
                            "record": output}
    flow["nodes"].append(node)
    return flow


@pytest.mark.parametrize("output,produces", [
    ("ext/../.git/config", ["ext/../.git/config"]),
    (".git/config", [".git/config"]),
    ("reports/escape/../../.onto/packs.lock", ["reports/escape/*"]),
])
def test_review_1_static_check_normalizes_paths_for_every_pack(tmp_path, env, output, produces):
    """审查第 1 条：静态检查按规范化后的完整路径判受保护目录，输出路径要落在 produces 里——能力包也一样。"""
    _client, manager, _pid, _tmp = env
    flow = escape_flow(output, produces)
    node_pack = make_node_pack(tmp_path / "node", flow=flow, tenant=True,
                               files={"commands/naming-check.md": "检查 $ARGUMENTS\n",
                                      "scripts/naming_lint.py": "print('x')\n"})
    assert failed(checks.run(node_pack, manager.packs.repository, tenant=True)[1])
    files = {"commands/naming-check.md": "检查 $ARGUMENTS\n", "scripts/naming_lint.py": "print('x')\n",
             "flow.yaml": yaml.safe_dump(flow, allow_unicode=True, sort_keys=False)}
    capability = make_pack(tmp_path / "cap", "naming", "0.1.0", files=files, tenant=True,
                           contributes={"flow": "flow.yaml"})
    assert failed(checks.run(capability, manager.packs.repository, tenant=True)[1])
    loose = escape_flow(".git/config", ["reports/escape.md"])
    files["flow.yaml"] = yaml.safe_dump(loose, allow_unicode=True, sort_keys=False)
    capability = make_pack(tmp_path / "cap2", "naming", "0.1.0", files=files, tenant=True,
                           contributes={"flow": "flow.yaml"})
    detail = failed(checks.run(capability, manager.packs.repository, tenant=True)[1])
    assert detail and any("script.output" in text or ".git" in text for text in detail.values())


@pytest.mark.parametrize("output,produces", [
    ("ext/../.git/config", ["ext/../.git/config"]),
    (".git/config", ["reports/escape.md"]),
    ("reports/escape/../../.git/config", ["reports/escape/*"]),
])
def test_review_1_script_node_cannot_write_git_config(env, fake_sandbox, tmp_path, output, produces):
    """审查第 1 条：绕过静态检查入库的包，脚本节点也写不了 .git/config，git status 不会执行包作者的命令。"""
    client, manager, project_id, _tmp = env
    root = manager.project_root(project_id)
    marker = tmp_path / "PWNED"
    files = {"commands/naming-check.md": "检查 $ARGUMENTS\n", "scripts/naming_lint.py": fsmonitor_script(marker),
             "flow.yaml": yaml.safe_dump(escape_flow(output, produces), allow_unicode=True, sort_keys=False)}
    source = make_pack(tmp_path / "src", "naming", "0.1.0", files=files, contributes={"flow": "flow.yaml"})
    manager.packs.repository.install(source, origin="upload", by="admin")
    manager.packs.repository.set_channel("naming", "stable", "0.1.0", by="admin")
    main_write(root, "ontology/object/读者.md", obj("读者", "合成读者"))
    enable_and_apply(client, manager, project_id)
    config_before = (root / ".git" / "config").read_text(encoding="utf-8")
    started = client.post(f"/api/v1/projects/{project_id}/flow/nodes/naming.lint/start", json={})
    assert started.status_code in (403, 409, 422), started.text
    assert (root / ".git" / "config").read_text(encoding="utf-8") == config_before
    git_status(root)
    assert not marker.exists()


def test_review_1_human_node_record_cannot_escape(env, tmp_path):
    client, manager, project_id, _tmp = env
    root = manager.project_root(project_id)
    flow = escape_flow("reports/escape/../../.onto/flow.lock", ["reports/escape/*"], kind="human")
    source = make_pack(tmp_path / "src", "naming", "0.1.0", contributes={"flow": "flow.yaml"},
                       files={"commands/naming-check.md": "检查 $ARGUMENTS\n",
                              "flow.yaml": yaml.safe_dump(flow, allow_unicode=True, sort_keys=False)})
    manager.packs.repository.install(source, origin="upload", by="admin")
    manager.packs.repository.set_channel("naming", "stable", "0.1.0", by="admin")
    main_write(root, "ontology/object/读者.md", obj("读者", "合成读者"))
    enable_and_apply(client, manager, project_id)
    started = client.post(f"/api/v1/projects/{project_id}/flow/nodes/naming.lint/start", json={})
    assert started.status_code in (403, 409, 422), started.text


@pytest.mark.parametrize("rel", [".git/config", ".git/hooks/pre-commit", "ontology/../.git/config"])
def test_review_1_change_tx_refuses_git_dir(env, rel):
    """审查第 1 条：写入事务的路径解析一律拒绝 .git/ 下的路径（所有写入入口的最后一道兜底）。"""
    _client, manager, project_id, _tmp = env
    root = manager.project_root(project_id)
    before = (root / ".git" / "config").read_text(encoding="utf-8")
    with pytest.raises(WorkspaceError) as exc:
        with ChangeTx(manager, project_id, Origin("web", "local")) as tx:
            tx.write(rel, "[core]\n\tfsmonitor = \"false\"\n")
            tx.commit("web: 越界")
    assert exc.value.status == 403
    assert (root / ".git" / "config").read_text(encoding="utf-8") == before


# ---- 审查第 2 条：发布门禁的 ref_exists 在只导出产物目录的临时树里永远找不到本体条目 ----

@pytest.fixture()
def checklist_env(tmp_path: Path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import config
    from app.api import deps
    from app.kernel import event_port
    from app.main import app
    from app.services import auth
    from app.services.packs import builtin
    from app.services.packs import service as pack_service
    from app.services.workspace import WorkspaceManager
    from tests.pack_helpers import make_assets
    from tests.test_extensions_artifacts import CHECKLIST_CONTRIBUTES, _checklist_files

    builtin.clear_cache()
    event_port.clear()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    manager = WorkspaceManager(tmp_path / "data", make_assets(tmp_path))
    monkeypatch.setattr(deps, "workspaces", manager)
    monkeypatch.setattr(auth, "ENABLED", False)
    client = TestClient(app)
    project_id = client.post("/api/v1/projects", json={"name": "引用门禁", "goal": "合成"}).json()["id"]
    gates = [{"id": "ref-ok", "label": "关联操作要存在", "artifact": "checklist.item", "on": ["publish"],
              "rules": [{"field": "operation", "ref_exists": True}]}]
    source = make_pack(tmp_path / "src", "checklist", "1.0.0", files=_checklist_files(),
                       contributes={**CHECKLIST_CONTRIBUTES, "gates": gates})
    manager.packs.repository.install(source, origin="upload", by="admin")
    manager.packs.repository.set_channel("checklist", "stable", "1.0.0", by="admin")
    pack_service.enable(manager, project_id, "checklist")
    pack_service.prepare_for_message(manager, project_id)
    yield client, manager, project_id
    builtin.clear_cache()


def test_review_2_publish_ref_gate_sees_ontology_entries(checklist_env):
    from tests.test_pack_extensions import item_md

    client, manager, project_id = checklist_env

    def save(rel: str, text: str) -> None:
        with ChangeTx(manager, project_id, Origin("web", "local")) as tx:
            tx.write(rel, text)
            tx.commit(f"web: {rel}")

    def gate() -> dict:
        body = client.get(f"/api/v1/projects/{project_id}/publish/check").json()
        return next(item for item in body["gates"] if item["id"] == "checklist.ref-ok")

    save("ontology/operation/借书.md", "# 借书\n\n合成操作\n")
    save("ext/checklist/items/借阅检查.md", item_md("借阅检查", "high").replace("result: pass", "result: pass\noperation: 借书"))
    assert gate()["status"] == "pass"
    save("ext/checklist/items/还书检查.md", item_md("还书检查", "high").replace("result: pass", "result: pass\noperation: 还书"))
    missing = gate()
    assert missing["status"] == "fail" and "引用的条目不存在" in missing["summary"]
