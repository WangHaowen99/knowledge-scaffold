"""I1：界面文案的后端部分（UI 文案口径 ③ ⑤、证据源本期不下发）与前端新认的字段。

说明槽位不再下发；证据源相关的数据不下发；项目内显示的文字不用「合入」「主线」「提交」「L4」；
FL-01 脚本节点带最近一次结果（刷新后仍在）。
"""

from __future__ import annotations

from app.services.flow import view
from tests.test_node_pack_lifecycle import (  # noqa: F401  （夹具按名字注入）
    LINT_SCRIPT, SCRIPT_NODE, enable_and_apply, env, fake_sandbox, install, kinds_flow)
from tests.test_changesets import main_write, obj


def _script_pack(manager, tmp_path, *, fail: bool = False) -> None:
    node = {**SCRIPT_NODE, "script": {**SCRIPT_NODE["script"], "args": ["--fail"]}} if fail else SCRIPT_NODE
    install(manager, tmp_path, flow=kinds_flow(node, probes={"naming_lint": {"files": ["reports"], "glob": "lint-naming.md"}}),
            files={"commands/naming-check.md": "检查 $ARGUMENTS\n", "scripts/naming_lint.py": LINT_SCRIPT})


def test_script_node_last_run_survives_refresh(env, fake_sandbox):
    client, manager, project_id, tmp_path = env
    root = manager.project_root(project_id)
    main_write(root, "ontology/object/读者.md", obj("读者", "合成读者"))
    _script_pack(manager, tmp_path)
    enable_and_apply(client, manager, project_id)
    nodes = {node["id"]: node for node in view.build(root)["nodes"]}
    assert nodes["naming.lint"]["last_run"] is None  # 还没跑过
    assert "last_run" not in nodes["core.model"]  # 只有脚本节点带
    assert all("note" not in node for node in nodes.values())  # 节点说明不再下发

    started = client.post(f"/api/v1/projects/{project_id}/flow/nodes/naming.lint/start", json={})
    assert started.status_code == 200, started.text
    flow = client.get(f"/api/v1/projects/{project_id}/flow").json()
    run = next(node for node in flow["nodes"] if node["id"] == "naming.lint")["last_run"]
    assert run["state"] == "passed" and run["summary"] == "# 命名格式检查" and run["at"].endswith("Z")
    assert (root / ".onto" / "run" / "script-runs.json").is_file()
    assert "script-runs.json" not in subprocess_ls(root)  # 不进版本


def test_script_node_failure_is_recorded_but_unavailable_sandbox_is_not(env, fake_sandbox, monkeypatch):
    client, manager, project_id, tmp_path = env
    root = manager.project_root(project_id)
    _script_pack(manager, tmp_path, fail=True)
    enable_and_apply(client, manager, project_id)
    failed = client.post(f"/api/v1/projects/{project_id}/flow/nodes/naming.lint/start", json={})
    assert failed.status_code == 422
    run = next(node for node in view.build(root)["nodes"] if node["id"] == "naming.lint")["last_run"]
    assert run["state"] == "failed" and run["summary"] == "退出码 3"
    monkeypatch.setattr(fake_sandbox[0], "gateway_url", lambda: "")
    assert client.post(f"/api/v1/projects/{project_id}/flow/nodes/naming.lint/start", json={}).status_code == 503
    again = next(node for node in view.build(root)["nodes"] if node["id"] == "naming.lint")["last_run"]
    assert again == run  # 沙箱不可用时没跑起来，不覆盖上一次的结果


def subprocess_ls(root) -> str:
    import subprocess

    return subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True).stdout
