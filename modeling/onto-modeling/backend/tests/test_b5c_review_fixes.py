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
