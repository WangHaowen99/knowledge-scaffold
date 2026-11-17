"""自主构建接到别处的地方（契约 P-01、P-06、P-13、V-06、CS-04、BP-03、O-01、O-15、O-21、O-35、/metrics、SY-17）：

- 项目卡片带运行进度、落地页是运行对话；运维总览、会话池标出自主构建；
- 同时运行数上限：超出的排队，前一个结束后接着开始；
- 维护模式：在跑的做完这一段就暂停（F8 起只有一段「全部」，暂停在校验之前）；
- 运行中改引擎、回退版本、删项目、手动确认这一段的修改都 409；
- 蓝图卡不再有确认按钮（F8），BP-03 `next=L4` 仍返回开始前预览（只有一段「全部」）；
- 删项目清掉运行记录；启动自检报告有「自主构建」一项；/metrics 有运行数与复核改判比例。

测试数据全部为合成内容。
"""

from __future__ import annotations

from app import config
from app.governance import ops_state
from app.kernel.changesets import service as changesets
from app.services.autobuild import store
from app.services.flow import blueprint as blueprint_service
from app.services.packs import contracts
from tests.autobuild_helpers import (DRAFT, AutoEngine, ab, gate, run_of, start, wait_state,  # noqa: F401
                                     wait_until)
from tests.test_changesets import main_write

ONE = {"scope": {"until_node": "core.operation"}}


def test_cards_ops_views_and_guards_while_running(ab, monkeypatch):
    from app.routers import ops as ops_routes
    from app.services import agent_sessions

    client, workspaces, project_id, root = ab
    monkeypatch.setattr(config, "OPS_ON_MAIN", True)
    monkeypatch.setattr(ops_routes, "manager", agent_sessions.manager)
    release = gate("/ontology-extract-object:all")
    run = start(client, project_id, **ONE)
    wait_until(lambda: store.get(project_id, run["run_id"])["cursor"].get("exec_id"))
    card = next(item for item in client.get("/api/v1/projects").json()["items"] if item["id"] == project_id)
    assert card["status"] == ["active", "autobuild"] and card["landing_page"] == "l4-run"
    assert card["autobuild"]["run_id"] == run["run_id"] and card["autobuild"]["slice_total"] == 1
    assert client.get("/api/v1/projects", params={"status": "autobuild"}).json()["counts"]["autobuild"] == 1
    assert client.get(f"/api/v1/projects/{project_id}/build-mode").json()["autobuild_running"] is True
    overview = client.get("/api/v1/ops/overview").json()
    assert overview["stats"]["autobuild"] == {"running": 1, "limit": 2}
    assert overview["autobuild_runs"][0]["run_no"] == 1 and overview["autobuild_runs"][0]["state"] == "running"
    pool = client.get("/api/v1/ops/sessions").json()["items"]
    assert any(item["autobuild"] for item in pool)
    # 运行中：回退版本、删项目、手动确认这一段都 409
    head = changesets.current(workspaces, project_id)
    assert client.post(f"/api/v1/projects/{project_id}/versions/revert", json={"commit": "HEAD"}).json()["code"] == \
        "autobuild_running"
    assert client.request("DELETE", f"/api/v1/projects/{project_id}",
                          json={"confirm_name": "自主构建"}).json()["code"] == "autobuild_running"
    assert client.post(f"/api/v1/projects/{project_id}/changesets/{head['id']}/merge", json={}).json()["code"] == \
        "autobuild_running"
    assert client.post(f"/api/v1/projects/{project_id}/archive", json={"archived": True}).json()["code"] == \
        "autobuild_running"
    preview = client.post(f"/api/v1/projects/{project_id}/autobuild/runs/preview", json={})
    assert preview.status_code == 409 and preview.json()["code"] == "autobuild_running"
    release.set()
    wait_state(client, project_id, run["run_id"], "completed")
    card = next(item for item in client.get("/api/v1/projects").json()["items"] if item["id"] == project_id)
    assert card["status"] == ["active"] and card["autobuild"] is None  # 没有要复核的
    today = client.get("/api/v1/ops/overview").json()["autobuild_today"]
    assert today["completed"] == 1 and today["avg_duration_ms"] is not None
    from app.governance import telemetry

    metrics = telemetry.prometheus_text()
    assert "onto_autobuild_running 0" in metrics and 'onto_autobuild_runs_total{' in metrics


def test_concurrency_limit_queues_and_maintenance_pauses_at_the_safe_point(ab):
    client, workspaces, project_id, _root = ab
    second = workspaces.create_project("自主构建二", owner="employee_0001")["id"]
    from tests.autobuild_helpers import blueprint

    blueprint(workspaces.project_root(second))
    assert client.put("/api/v1/admin/autobuild-limits", json={"max_concurrent_runs": 1}).status_code == 200
    release = gate("/ontology-extract-object:all")
    first = start(client, project_id, **ONE)
    wait_until(lambda: store.get(project_id, first["run_id"])["state"] == "running")
    preview = client.post(f"/api/v1/projects/{second}/autobuild/runs/preview", json={"policy_overrides": ONE}).json()
    assert preview["queued"] is True and preview["queue_position"] == 1
    queued = client.post(f"/api/v1/projects/{second}/autobuild/runs", json={"preview_token": preview["preview_token"]})
    assert queued.json()["state"] == "queued"
    release.set()
    wait_state(client, project_id, first["run_id"], "completed")
    wait_state(client, second, queued.json()["run_id"], "completed")
    # 维护模式：在跑的做完这一段就暂停（O-21 autobuild=pause_at_safe_point）；第三个项目做「全部」与校验
    assert client.put("/api/v1/admin/autobuild-limits", json={"max_concurrent_runs": 2}).status_code == 200
    third = workspaces.create_project("自主构建三", owner="employee_0001")["id"]
    blueprint(workspaces.project_root(third))
    release2 = gate("/ontology-extract-object:all")
    run = start(client, third, scope={"until_node": "core.validate"})
    wait_until(lambda: store.get(third, run["run_id"])["cursor"].get("exec_id"))
    ops_state.enter_maintenance("reject_new_sessions", "合成维护", "ops")
    from app.services.autobuild.runner import runner

    assert runner.pause_all_at_safe_point() == 1
    release2.set()
    paused = wait_state(client, third, run["run_id"], "paused")
    assert paused["slices"] == {"done": 1, "total": 1} and "/ontology-validate" not in AutoEngine.sent
    ops_state.exit_maintenance("ops")
    resumed = client.post(f"/api/v1/projects/{third}/autobuild/runs/{run['run_id']}/resume").json()
    assert resumed["state"] in ("queued", "running")
    wait_state(client, third, run["run_id"], "completed")
    assert "/ontology-validate" in AutoEngine.sent


def test_blueprint_l4_preview_and_project_delete_purges_runs(ab):
    client, workspaces, project_id, root = ab
    module = contracts.default().load("blueprint")
    other = workspaces.create_project("蓝图预览", owner="employee_0001")["id"]
    other_root = workspaces.project_root(other)
    main_write(other_root, "research/blueprint.json", module.dumps(module.from_draft(DRAFT)), "research: 合成蓝图草案")
    # F8：蓝图不用确认，卡片上没有确认按钮；BP-03 给 F8 之前的界面与脚本仍可调
    assert blueprint_service.confirm_options("draft") == [] and blueprint_service.confirm_options("confirmed") == []
    base = f"/api/v1/projects/{other}/blueprint"
    revision = client.get(base).json()["revision"]
    confirmed = client.post(f"{base}/confirm", json={"base_revision": revision, "next": "L4"})
    assert confirmed.status_code == 200, confirmed.text
    preview = confirmed.json()["autobuild_preview"]
    assert preview["preview_token"].startswith("pv-") and [item["id"] for item in preview["slices"]] == ["all"]
    assert all(item["included"] for item in preview["slices"]) and "production" not in preview
    # 删项目：运行记录一起清掉
    run = start(client, project_id, **ONE)
    wait_state(client, project_id, run["run_id"], "completed")
    assert store.runs(project_id)
    deleted = client.request("DELETE", f"/api/v1/projects/{project_id}", json={"confirm_name": "自主构建"})
    assert deleted.status_code == 202
    assert store.runs(project_id) == [] and not store.project_dir(project_id).exists()
