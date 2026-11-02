"""自主构建运行器（详设 8.5、13 章 P3 验收；方案 B12～B14；契约 AB-03～AB-09、AB-18、EP-14～EP-17、4.7 卡片）。

- 一个 5 片的项目一路跑完：每片一个变更集、自动合入（来源 autobuild）、切片显示「自主完成」，台账与报告进 git；
- 运行中确认卡、决策卡由策略立即作答并记台账；
- 运行中用户在别的对话发消息 409，在运行对话里是补充说明（不打断，下一步带给助手）；
- 暂停（这一步做完）、停在安全点（这一片做完）、转为陪伴；
- 预算 80% 提醒、100% 停在安全点；
- 重启后按执行记录续跑。

假引擎按消息现做（tests/autobuild_helpers.py），等待一律按条件。测试数据全部为合成内容。
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.main import app
from app.services import agent_sessions
from app.services.agent_sessions import SessionManager
from app.services.autobuild import store
from app.services.autobuild.runner import runner
from app.routers import sessions as session_routes
from tests.autobuild_helpers import (AutoEngine, ab, ask, gate, history, project_events, run_of, start,  # noqa: F401
                                     wait_state, wait_until)
from tests.test_changesets import git, trailers

SLICES_ONLY = {"scope": {"until_node": "core.model"}, "budget": {"max_tokens": 500_000}}


def test_five_slice_project_runs_to_the_end_with_policy_answers(ab):
    client, workspaces, project_id, root = ab
    answers: list = []
    AutoEngine.hooks["/ontology-extract-operation:s2"] = [ask([
        {"question": "促销备货要不要单列", "header": "置信度 0.9 · 依据 file:resource/合成规则.docx#p:7.5",
         "options": [{"label": "不单列（推荐）"}, {"label": "单列一张表"}]},
        {"question": "检查项目含不含温度记录", "header": "置信度 0.6",
         "options": [{"label": "包含（推荐）"}, {"label": "不包含（保守）"}]}], into=answers)]
    AutoEngine.hooks["/ontology-extract-object:s3"] = [ask([], into=answers, tool="Write")]
    run = start(client, project_id, **SLICES_ONLY)
    assert run["state"] in ("queued", "running") and run["run_no"] == 1 and run["slices"]["total"] == 5
    conversation_id = run["conversation_id"]
    done = wait_state(client, project_id, run["run_id"], "completed")
    # 5 片都自主完成：每片一个主线提交，来源 autobuild、带变更集编号
    assert done["slices"] == {"done": 5, "total": 5} and done["report_available"] is True
    subjects = git(root, "log", "--format=%s", "-8").splitlines()
    assert sum(1 for line in subjects if line.startswith("autobuild: 自主构建运行 #1「合成片")) == 5
    assert subjects[0] == "autobuild: 自主构建运行 #1 报告"
    assert trailers(root, "HEAD~1")["Onto-Source"] == "autobuild"
    flow = client.get(f"/api/v1/projects/{project_id}/flow").json()
    by_slice = {item["id"]: item for item in flow["slices"]}
    assert all(by_slice[f"s{n}"]["stages"]["object"] == "auto" for n in range(1, 6))
    assert all(by_slice[f"s{n}"]["group"] == "done" for n in range(1, 6))
    model = next(node for node in flow["nodes"] if node["id"] == "core.model")
    assert model["display_state"] == "auto" and model["flags"]["review_source"] == "autobuild"
    # 每片新开上下文、注入片说明与台账摘要；命令带切片
    assert [text for text in AutoEngine.sent if text.startswith("/ontology-extract-object")] == [
        f"/ontology-extract-object slice=s{n}" for n in range(1, 6)]
    assert all(hint.startswith("自主构建 · 运行 #1") for hint in AutoEngine.hints)
    later = [hint for hint in AutoEngine.hints if "「合成片3」" in hint]
    assert later and "促销备货要不要单列 → 不单列" in later[0] and "（假设）" in later[0]
    # 策略作答：达到阈值取推荐项；低于阈值取保守项并记为假设；工具确认放行
    assert answers[0]["updated_input"]["answers"] == {"促销备货要不要单列": "不单列（推荐）",
                                                      "检查项目含不含温度记录": "不包含（保守）"}
    assert answers[1] == {"behavior": "allow", "updated_input": None}
    ledger = client.get(f"/api/v1/projects/{project_id}/autobuild/runs/{run['run_id']}/ledger").json()
    assert ledger["counts"] == {"total": 2, "high_confidence": 1, "below_threshold": 1, "assumptions": 1}
    first = next(item for item in ledger["items"] if item["question"] == "促销备货要不要单列")
    assert first["chosen"] == "不单列" and first["confidence"] == 0.9 and first["assumption"] is False
    assert first["anchors"] == [{"n": 1, "anchor": "file:resource/合成规则.docx#p:7.5", "label": "合成规则.docx §7.5"}]
    assert first["slice"] == {"id": "s2", "name": "合成片2"} and first["review_state"] == "pending"
    assert [option["recommended"] for option in first["options"]] == [True, False]
    # 台账与报告进 git
    saved = json.loads((root / "reports" / "autobuild" / "1" / "ledger.json").read_text(encoding="utf-8"))
    assert len(saved["ledger"]) == 2 and (root / "reports" / "autobuild" / "1" / "report.json").is_file()
    # 运行在自己的对话里：开始卡、5 个切片进展块（自动决定嵌在时间线里）、报告卡；不出「这一轮改了什么」卡
    conversations = client.get(f"/api/v1/projects/{project_id}/conversations").json()["items"]
    mine = next(item for item in conversations if item["id"] == conversation_id)
    assert mine["kind"] == "autobuild" and mine["run_id"] == run["run_id"] and mine["title"] == "自主构建 · 运行 #1"
    events = history(client, project_id, conversation_id)
    cards = [item["data"] for item in events if item["type"] == "card"]
    kinds = [card["card_type"] for card in cards]
    assert kinds[0] == "autobuild_start" and kinds.count("slice_progress") == 5 and kinds[-1] == "run_report"
    assert "round_changes" not in kinds and not any(item["type"] == "message.user" for item in events)
    s2 = next(card for card in cards if card["card_type"] == "slice_progress" and card["slice"]["id"] == "s2")
    assert s2["state"] == "done" and s2["version_label"] and len(s2["decision_ids"]) == 2
    assert [item["type"] for item in s2["timeline"] if "type" in item] == ["auto_decision", "auto_decision"]
    report_card = cards[-1]
    assert report_card["review_count"] == 2 and report_card["metrics"]["slices"] == {"done": 5, "total": 5, "partial": []}
    suggestions = [item["data"] for item in events if item["type"] == "suggestions.updated"][-1]
    assert suggestions["primary"]["label"] == "去复核"
    # 项目流：运行条、安全点、结束
    assert {event["data"]["next"] for event in project_events(project_id, "autobuild.safe_point")} == {"continue"}
    finished = wait_until(lambda: project_events(project_id, "autobuild.finished"))
    assert finished[-1]["data"]["state"] == "completed"
    state = client.get(f"/api/v1/projects/{project_id}/conversations/{conversation_id}").json()
    assert state["autobuild_run_id"] == run["run_id"]
    report = client.get(f"/api/v1/projects/{project_id}/autobuild/runs/{run['run_id']}/report").json()
    assert [row["result"] for row in report["slices"]] == ["done"] * 5 and all(row["revertible"] for row in report["slices"])
    assert report["metrics"]["produced"] == {"objects": 5, "operations": 5, "rules": 0}
    listing = client.get(f"/api/v1/projects/{project_id}/autobuild/runs").json()["items"]
    assert [item["run_id"] for item in listing] == [run["run_id"]]


def test_messages_during_a_run_are_409_elsewhere_and_a_note_in_the_run_conversation(ab):
    client, _workspaces, project_id, _root = ab
    release = gate("/ontology-extract-object:s1")
    run = start(client, project_id, **SLICES_ONLY)
    wait_until(lambda: run_of(client, project_id, run["run_id"])["current"])
    other = client.get(f"/api/v1/projects/{project_id}/conversations").json()["items"]
    elsewhere = next(item["id"] for item in other if item["id"] != run["conversation_id"])
    refused = client.post(f"/api/v1/projects/{project_id}/conversations/{elsewhere}/messages", json={"text": "你好"})
    assert refused.status_code == 409 and refused.json()["code"] == "autobuild_running"
    assert refused.json()["details"] == {"run_id": run["run_id"]}
    command = client.post(f"/api/v1/projects/{project_id}/commands",
                          json={"command": "/ontology-validate", "origin": {"page": "workbench"}})
    assert command.json()["code"] == "autobuild_running"
    for path in ("interrupt", "restart"):
        blocked = client.post(f"/api/v1/projects/{project_id}/conversations/{run['conversation_id']}/{path}")
        assert blocked.json()["code"] == "autobuild_running", path
    assert client.put(f"/api/v1/projects/{project_id}/engine", json={"engine": "claude"}).json()["code"] == "autobuild_running"
    note = client.post(f"/api/v1/projects/{project_id}/conversations/{run['conversation_id']}/messages",
                       json={"text": "门店编号统一用四位"})
    assert note.status_code == 202 and note.json()["delivery"] == "deferred"
    release.set()
    wait_state(client, project_id, run["run_id"], "completed")
    # 补充说明在下一步开始前带给助手
    after = [hint for hint in AutoEngine.hints if "门店编号统一用四位" in hint]
    assert after and "用户补充" in after[0]
    events = history(client, project_id, run["conversation_id"])
    users = [item["data"] for item in events if item["type"] == "message.user"]
    assert users == [{**users[0], "text": "门店编号统一用四位", "delivery": "deferred"}]
    assert client.post(f"/api/v1/projects/{project_id}/conversations/{elsewhere}/messages",
                       json={"text": "你好"}).status_code == 202


def test_pause_then_stop_at_safe_point_then_convert(ab):
    client, _workspaces, project_id, root = ab
    release = gate("/ontology-extract-object:s1")
    run = start(client, project_id, **SLICES_ONLY)
    base = f"/api/v1/projects/{project_id}/autobuild/runs/{run['run_id']}"
    wait_until(lambda: run_of(client, project_id, run["run_id"])["current"])
    pausing = client.post(f"{base}/pause").json()
    assert pausing["state"] == "pausing"
    release.set()
    # 这一步（对象）做完就停：这一片还没合入，变更集开着、写入范围锁在这一片
    paused = wait_state(client, project_id, run["run_id"], "paused")
    assert paused["matrix"][0]["cells"]["object"] == "auto" and paused["matrix"][0]["cells"]["operation"] == "queued"
    scope = client.get(f"/api/v1/projects/{project_id}/scope").json()
    assert scope["state"] == "autobuild_slice" and scope["run_id"] == run["run_id"]
    assert client.post(f"{base}/pause").json()["code"] == "autobuild_not_running"
    # 续跑：接着做这一片剩下的步骤（不新开上下文）；第二片再停在安全点
    release2 = gate("/ontology-extract-object:s2")
    assert client.post(f"{base}/resume").json()["state"] in ("queued", "running")
    wait_until(lambda: (run_of(client, project_id, run["run_id"])["current"] or {}).get("slice_id") == "s2")
    assert client.post(f"{base}/stop-at-safe-point").json()["state"] == "stopping"
    release2.set()
    stopped = wait_state(client, project_id, run["run_id"], "stopped")
    assert stopped["slices"] == {"done": 2, "total": 5}
    assert [row["result"] for row in stopped["matrix"]] == ["完成", "完成", "未开始", "未开始", "未开始"]
    assert [event["data"]["next"] for event in project_events(project_id, "autobuild.safe_point")] == ["continue", "stopped"]
    assert client.get(f"/api/v1/projects/{project_id}/scope").json()["state"] == "none"
    # 转为陪伴：第二次运行做完当前这一片就转，项目默认构建方式不变
    release3 = gate("/ontology-extract-object:s3")
    second = start(client, project_id, **SLICES_ONLY)
    assert second["run_no"] == 2 and second["slices"]["total"] == 3
    wait_until(lambda: run_of(client, project_id, second["run_id"])["current"])
    converting = client.post(f"/api/v1/projects/{project_id}/autobuild/runs/{second['run_id']}/convert-to-l2").json()
    assert converting["state"] == "converting"
    release3.set()
    converted = wait_state(client, project_id, second["run_id"], "converted")
    assert converted["slices"]["done"] == 1
    assert client.get(f"/api/v1/projects/{project_id}/build-mode").json() == {
        "build_mode": "L2", "autobuild_running": False, "run_id": None}
    # 转为陪伴之后这条对话照常能聊
    assert client.post(f"/api/v1/projects/{project_id}/conversations/{second['conversation_id']}/messages",
                       json={"text": "接着做下一片"}).status_code == 202


def test_budget_warns_at_80_and_stops_at_the_safe_point_at_100(ab):
    client, _workspaces, project_id, _root = ab
    # 每轮 125 token；第一片 3 步 = 375，到 400 的 80% 提醒；第二片第一步后过 100%，做完这一片停
    run = start(client, project_id, scope={"until_node": "core.model"},
                budget={"max_tokens": 400, "max_duration_min": 60, "max_cost": {"amount": 50}})
    ended = wait_state(client, project_id, run["run_id"], "budget_exhausted")
    warnings = project_events(project_id, "autobuild.budget_warning")
    assert [event["data"]["dimension"] for event in warnings] == ["tokens"]
    assert warnings[0]["data"]["used_pct"] >= 80
    assert ended["slices"]["done"] == 2 and ended["tokens"]["used"] >= 400
    assert project_events(project_id, "autobuild.safe_point")[-1]["data"]["next"] == "stopped"
    finished = wait_until(lambda: project_events(project_id, "autobuild.finished"))
    assert finished[-1]["data"]["state"] == "budget_exhausted"


def test_restart_resumes_from_the_checkpoint(ab, monkeypatch):
    client, workspaces, project_id, _root = ab
    release = gate("/ontology-extract-operation:s2")
    run = start(client, project_id, **SLICES_ONLY)
    # 第 2 片的操作那一步发出去了、引擎正在跑
    wait_until(lambda: (store.get(project_id, run["run_id"])["cursor"].get("step") == "operation"
                        and store.get(project_id, run["run_id"])["cursor"].get("unit") == "s2"
                        and store.get(project_id, run["run_id"])["cursor"].get("exec_id")))
    # 停机：运行停在检查点（第 2 片的操作那一步还在跑）
    client.__exit__(None, None, None)
    release.set()
    assert store.get(project_id, run["run_id"])["state"] == "running"
    manager = SessionManager()
    monkeypatch.setattr(manager, "_engine_class_for", lambda pid: AutoEngine)
    monkeypatch.setattr(agent_sessions, "manager", manager)
    monkeypatch.setattr(session_routes, "manager", manager)
    runner._tasks.clear()
    runner._admit_lock = None
    with TestClient(app) as again:
        done = wait_state(again, project_id, run["run_id"], "completed")
        assert done["recovered_from_checkpoint"] == 1 and done["slices"] == {"done": 5, "total": 5}
        report = again.get(f"/api/v1/projects/{project_id}/autobuild/runs/{run['run_id']}/report").json()
        assert report["recovered_restarts"] == 1
        # 第 2 片的操作按执行记录续跑（续跑指令带原命令）
        assert any(text.startswith("/ontology-extract-operation 继续上次没做完") for text in AutoEngine.sent)
