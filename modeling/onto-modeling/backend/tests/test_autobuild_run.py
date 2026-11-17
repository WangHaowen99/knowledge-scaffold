"""自主构建运行器（详设 8.5、13 章 P3 验收；方案 B12～B14；契约 AB-03～AB-09、AB-18、AB-19、EP-14～EP-17、4.7 卡片）。

- 一个全量模式的项目（F8 起不分段，只有一段「全部」）一路跑完：一个变更集、自动合入（来源 autobuild）、
  显示「自主完成」，台账与报告进 git；蓝图是草案也能开始（F8 起不用确认）；
- 运行中确认卡、决策卡由策略立即作答并记台账；
- 运行中用户在别的对话发消息 409，在运行对话里是补充说明（不打断，下一步带给助手）；
- 暂停（这一步做完）、停在安全点（这一段做完、校验还没开始）、转为陪伴；
- 用量只记不拦，运行一直做到完；配额用完暂停在这一步，续跑接着做（F6）；
- 结束后确认：运行条收起，构建方式回到陪伴建模（AB-19）；
- 重启后按执行记录续跑。

假引擎按消息现做（tests/autobuild_helpers.py），等待一律按条件。测试数据全部为合成内容。
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.governance.quota import QuotaExceeded
from app.main import app
from app.services import agent_sessions, notifications
from app.services.agent_sessions import SessionManager
from app.services.autobuild import store
from app.services.autobuild import runner as runner_module
from app.services.autobuild.runner import runner
from app.routers import sessions as session_routes
from tests.autobuild_helpers import (AutoEngine, ab, ask, gate, history, project_events, run_of, start,  # noqa: F401
                                     wait_state, wait_until)
from tests.test_changesets import git, trailers

SLICES_ONLY = {"scope": {"until_node": "core.operation"}}
UNTIL_VALIDATE = {"scope": {"until_node": "core.validate"}}


def test_full_project_runs_to_the_end_with_policy_answers(ab):
    client, workspaces, project_id, root = ab
    answers: list = []
    # 蓝图是草案、没确认：F8 起自主构建不要求先确认（原来 409 blueprint_not_confirmed）
    assert json.loads((root / "research" / "blueprint.json").read_text(encoding="utf-8"))["state"] == "draft"
    AutoEngine.hooks["/ontology-extract-object:all"] = [ask([
        {"question": "促销备货要不要单列", "header": "置信度 0.9 · 依据 file:resource/合成规则.docx#p:7.5",
         "options": [{"label": "不单列（推荐）"}, {"label": "单列一张表"}]},
        {"question": "检查项目含不含温度记录", "header": "置信度 0.6",
         "options": [{"label": "包含（推荐）"}, {"label": "不包含（保守）"}]}], into=answers)]
    AutoEngine.hooks["/ontology-extract-operation:all"] = [ask([], into=answers, tool="Write")]
    run = start(client, project_id, **SLICES_ONLY)
    assert run["state"] in ("queued", "running") and run["run_no"] == 1 and run["slices"]["total"] == 1
    conversation_id = run["conversation_id"]
    done = wait_state(client, project_id, run["run_id"], "completed")
    # 一段「全部」自主完成：一个主线提交，来源 autobuild、带变更集编号
    assert done["slices"] == {"done": 1, "total": 1} and done["report_available"] is True
    subjects = git(root, "log", "--format=%s", "-3").splitlines()
    assert subjects[0] == "autobuild: 自主构建运行 #1 报告"
    assert subjects[1].startswith("autobuild: 自主构建运行 #1「全部」")
    assert trailers(root, "HEAD~1")["Onto-Source"] == "autobuild"
    flow = client.get(f"/api/v1/projects/{project_id}/flow").json()
    for node_id in ("core.object", "core.operation"):
        model = next(node for node in flow["nodes"] if node["id"] == node_id)
        assert model["display_state"] == "auto" and model["flags"]["review_source"] == "autobuild"
    # 新开上下文、注入说明与台账摘要；全量模式命令不带分段，一次写完全部条目
    assert [text for text in AutoEngine.sent if text.startswith("/ontology-extract")] == [
        "/ontology-extract-object", "/ontology-extract-operation"]
    assert all((root / "ontology" / "object" / f"对象{n}.md").is_file() for n in range(1, 6))
    assert all(hint.startswith("自主构建 · 运行 #1") for hint in AutoEngine.hints)
    later = [hint for hint in AutoEngine.hints if "促销备货要不要单列" in hint]
    assert later and "促销备货要不要单列 → 不单列" in later[0] and "（假设）" in later[0]
    # 策略作答：达到阈值取推荐项；低于阈值取保守项并记为假设；工具确认放行
    assert answers[0]["updated_input"]["answers"] == {"促销备货要不要单列": "不单列（推荐）",
                                                      "检查项目含不含温度记录": "不包含（保守）"}
    assert answers[1] == {"behavior": "allow", "updated_input": None}
    ledger = client.get(f"/api/v1/projects/{project_id}/autobuild/runs/{run['run_id']}/ledger").json()
    assert ledger["counts"] == {"total": 2, "high_confidence": 1, "below_threshold": 1, "assumptions": 1}
    first = next(item for item in ledger["items"] if item["question"] == "促销备货要不要单列")
    assert first["chosen"] == "不单列" and first["confidence"] == 0.9 and first["assumption"] is False
    assert first["anchors"] == [{"n": 1, "anchor": "file:resource/合成规则.docx#p:7.5", "label": "合成规则 §7.5"}]
    assert first["slice"] == {"id": "all", "name": "全部"} and first["review_state"] == "pending"
    assert [option["recommended"] for option in first["options"]] == [True, False]
    # 台账与报告进 git
    saved = json.loads((root / "reports" / "autobuild" / "1" / "ledger.json").read_text(encoding="utf-8"))
    assert len(saved["ledger"]) == 2 and (root / "reports" / "autobuild" / "1" / "report.json").is_file()
    # 运行在自己的对话里：开始卡、一段进展块（自动决定嵌在时间线里）、报告卡；不出「这一轮改了什么」卡
    conversations = client.get(f"/api/v1/projects/{project_id}/conversations").json()["items"]
    mine = next(item for item in conversations if item["id"] == conversation_id)
    assert mine["kind"] == "autobuild" and mine["run_id"] == run["run_id"] and mine["title"] == "自主构建 · 运行 #1"
    events = history(client, project_id, conversation_id)
    cards = [item["data"] for item in events if item["type"] == "card"]
    kinds = [card["card_type"] for card in cards]
    assert kinds[0] == "autobuild_start" and kinds.count("slice_progress") == 1 and kinds[-1] == "run_report"
    assert "round_changes" not in kinds and not any(item["type"] == "message.user" for item in events)
    progress = next(card for card in cards if card["card_type"] == "slice_progress")
    assert progress["slice"]["id"] == "all" and progress["state"] == "done" and progress["version_label"]
    assert len(progress["decision_ids"]) == 2
    assert [item["type"] for item in progress["timeline"] if "type" in item] == ["auto_decision", "auto_decision"]
    report_card = cards[-1]
    assert report_card["review_count"] == 2 and report_card["metrics"]["slices"] == {"done": 1, "total": 1, "partial": []}
    suggestions = [item["data"] for item in events if item["type"] == "suggestions.updated"][-1]
    assert suggestions["primary"]["label"] == "去复核"
    # 项目流：运行条、安全点、结束
    assert {event["data"]["next"] for event in project_events(project_id, "autobuild.safe_point")} == {"continue"}
    finished = wait_until(lambda: project_events(project_id, "autobuild.finished"))
    assert finished[-1]["data"]["state"] == "completed"
    state = client.get(f"/api/v1/projects/{project_id}/conversations/{conversation_id}").json()
    assert state["autobuild_run_id"] == run["run_id"]
    report = client.get(f"/api/v1/projects/{project_id}/autobuild/runs/{run['run_id']}/report").json()
    assert [row["result"] for row in report["slices"]] == ["done"] and all(row["revertible"] for row in report["slices"])
    assert report["metrics"]["produced"] == {"objects": 5, "operations": 5, "rules": 0}
    listing = client.get(f"/api/v1/projects/{project_id}/autobuild/runs").json()["items"]
    assert [item["run_id"] for item in listing] == [run["run_id"]]
    # 版本列表：自主构建合入的版本带运行与段（V-01 Version.run）
    versions = client.get(f"/api/v1/projects/{project_id}/versions", params={"source": "autobuild"}).json()["items"]
    runs = [item["run"] for item in versions if item["run"]]
    assert [item["slice_id"] for item in runs] == ["all"]
    assert next(item for item in versions if (item["run"] or {}).get("slice_id") == "all")["needs_review"] == 2


def test_messages_during_a_run_are_409_elsewhere_and_a_note_in_the_run_conversation(ab):
    client, _workspaces, project_id, _root = ab
    release = gate("/ontology-extract-object:all")
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


def test_start_takes_over_a_conversation_waiting_for_an_answer(ab):
    """别的对话停在问题卡上：不带 take_over 时 409 并说清是在等回答；带上就撤回问题、打断那一轮，再开始。"""
    client, _workspaces, project_id, _root = ab
    AutoEngine.hooks["你好"] = [ask([{"question": "已有的操作怎么处理", "options": [{"label": "保留（推荐）"}]}])]
    default = client.get(f"/api/v1/projects/{project_id}/conversations").json()["items"][0]["id"]
    assert client.post(f"/api/v1/projects/{project_id}/conversations/{default}/messages",
                       json={"text": "你好"}).status_code == 202

    def asked():
        return [item for item in history(client, project_id, default) if item["type"] == "question.asked"]
    question = wait_until(asked)[0]["data"]
    base = f"/api/v1/projects/{project_id}/autobuild"
    client.put(f"{base}/policy", json=SLICES_ONLY)
    preview = client.post(f"{base}/runs/preview", json={}).json()
    assert preview["busy"] == {"conversation_id": default, "waiting": True}
    refused = client.post(f"{base}/runs", json={"preview_token": preview["preview_token"]})
    assert refused.status_code == 409 and refused.json()["code"] == "session_busy"
    assert refused.json()["details"] == {"running_conversation_id": default, "waiting": True}
    assert "等你回答" in refused.json()["message"]
    started = client.post(f"{base}/runs", json={"preview_token": preview["preview_token"], "take_over": True})
    assert started.status_code == 201, started.text
    withdrawn = [item["data"] for item in history(client, project_id, default) if item["type"] == "question.withdrawn"]
    assert withdrawn == [{"question_id": question["question_id"], "reason": runner_module.TAKE_OVER_REASON}]
    wait_state(client, project_id, started.json()["run_id"], "completed")


def test_take_over_hands_the_question_and_the_unfinished_step_to_the_run(ab, monkeypatch):
    """接管时的交接（实测：撤回的决策卡没去向、被打断的那一步留成「没做完」的续跑项、打断记录写「已保存到」旧版本）。

    陪伴对话里对象提取写完对象、停在问题卡上，从这里开始自主构建：这一轮的对象先合入，对象一步开始时已经做完；
    - 撤回的问题按「拿不准时」的做法定下来，记进台账（标明是开始时没回答的、待复核），并带给运行的每一步；
    - 被打断的执行交给运行收尾：不进待处理的续跑，陪伴对话的建议条不推「继续」，对话里留一行交给了哪次运行；
    - 打断记录写「交给自主构建」，不带已保存的版本。"""
    import asyncio
    import threading

    from tests.autobuild_helpers import OBJECTS
    from tests.test_changesets import obj

    client, _workspaces, project_id, root = ab
    held = threading.Event()

    def write_objects(ctx):
        from pathlib import Path

        for name in OBJECTS:
            path = Path(ctx.cwd) / "ontology" / "object" / f"{name}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(obj(name, "合成对象"), encoding="utf-8")

    async def hold(ctx):
        await asyncio.to_thread(held.wait, 30)

    monkeypatch.setattr(AutoEngine, "interrupt", stopping_interrupt(held))

    AutoEngine.hooks["/ontology-extract-object:all"] = [write_objects, lock_phase("confirm"), ask([
        {"question": "补货明细要不要单独建对象", "header": "置信度 0.6",
         "options": [{"label": "单独建（推荐）"}, {"label": "并入补货单（保守）"}]}]), hold]
    default = client.get(f"/api/v1/projects/{project_id}/conversations").json()["items"][0]["id"]
    assert client.post(f"/api/v1/projects/{project_id}/conversations/{default}/messages",
                       json={"text": "/ontology-extract-object"}).status_code == 202
    wait_until(lambda: [item for item in history(client, project_id, default) if item["type"] == "question.asked"])
    base = f"/api/v1/projects/{project_id}/autobuild"
    client.put(f"{base}/policy", json=SLICES_ONLY)
    preview = client.post(f"{base}/runs/preview", json={}).json()
    started = client.post(f"{base}/runs", json={"preview_token": preview["preview_token"], "take_over": True})
    assert started.status_code == 201, started.text
    run = wait_state(client, project_id, started.json()["run_id"], "completed")
    # 撤回的问题：低于阈值，取保守项记为假设、待复核；运行的每一步都带着它
    ledger = client.get(f"{base}/runs/{run['run_id']}/ledger").json()["items"]
    handed = [item for item in ledger if item["question"] == "补货明细要不要单独建对象"]
    assert len(handed) == 1 and handed[0]["chosen"] == "并入补货单" and handed[0]["assumption"] is True
    assert handed[0]["review_state"] == "pending" and handed[0]["slice"] == {"id": "all", "name": "全部"}
    assert handed[0]["reason_text"].startswith("开始自主构建时没回答的问题")
    run_hints = [hint for hint in AutoEngine.hints if hint.startswith("自主构建 · 运行 #1")]
    assert run_hints and all("补货明细要不要单独建对象 → 并入补货单（假设）" in hint for hint in run_hints)
    # 对象一步开始时已经做完（这一轮先合入了），运行只做操作；被打断的执行交给运行收尾
    assert [text for text in AutoEngine.sent if text.startswith("/ontology-extract")] == [
        "/ontology-extract-object", "/ontology-extract-operation"]
    records = [record for record in exec_records_of(project_id) if record.get("conversation_id") == default]
    assert len(records) == 1 and records[0]["state"] == "abandoned"
    assert records[0]["handed_over"] == {"run_id": run["run_id"], "run_no": 1, "taken_over": True}
    inbox_items = client.get(f"/api/v1/projects/{project_id}/inbox").json()["items"]
    assert not [item for item in inbox_items if item["kind"] == "resumable"]
    suggestions = client.get(f"/api/v1/projects/{project_id}/flow/suggestions",
                             params={"conversation_id": default}).json()
    actions = [item["action"] for item in [suggestions["primary"], *suggestions["secondary"]] if item]
    assert not [action for action in actions if action.get("type") in ("resume_exec", "abandon_exec")]
    events = history(client, project_id, default)
    interrupted = [item["data"] for item in events if item["type"] == "session.interrupted"]
    assert interrupted[-1]["by"] == "autobuild" and interrupted[-1]["saved"] is None
    assert interrupted[-1]["position_label"] == "开始了自主构建，「提取对象」交给它接着做"
    notices = [item["data"]["text"] for item in events if item["type"] == "notice"]
    assert notices == ["「提取对象」交给自主构建 · 运行 #1，这里不用再继续"]
    # 切回陪伴对话：注入里带交接说明（运行从哪个版本开始、增删改了哪些条目、以主线为准）；运行自己的对话、
    # 运行之后才建的对话不带
    from app.services.session_host import injection

    def injected(conversation_id):
        return injection.build_context(root, user_text="现在几个操作", session_start=True, last_injected=None,
                                       conversation_id=conversation_id).text

    note = injected(default)
    assert "交接：这条对话之后，自主构建运行 #1（从 " in note and "新增 操作1、操作2、操作3、操作4、操作5" in note
    assert "以现在的主线为准" in note and "对象1" not in note  # 对象在运行开始前就合入了，不是运行改的
    assert "交接" not in injected(run["conversation_id"])
    later = client.post(f"/api/v1/projects/{project_id}/conversations", json={}).json()["id"]
    assert "交接" not in injected(later)


def test_rules_written_along_with_operations_need_review(ab):
    """操作提取一起抽的规则、指标也记成自主完成、待复核。

    规则一步不在运行计划里（F8 随操作一起抽），原来按「内容在、没有记录」记成人确认过（实测：运行写的 13 条规则在流程上
    是已确认）；指标节点同理（实测：运行合入后指标节点是已确认）。"""
    from pathlib import Path

    client, _workspaces, project_id, root = ab

    def write_rule(ctx):
        for rel, text in (("ontology/rule/规则1.md", "# 规则1\n\n## 规则描述\n\n合成规则\n"),
                          ("ontology/metric/指标1.md", "# 指标1\n\n## 指标定义\n\n合成指标\n")):
            path = Path(ctx.cwd) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    AutoEngine.hooks["/ontology-extract-operation:all"] = [write_rule]
    run = start(client, project_id, **SLICES_ONLY)
    wait_state(client, project_id, run["run_id"], "completed")
    flow = client.get(f"/api/v1/projects/{project_id}/flow").json()
    # 规则是提取操作的产出：随提取操作一起自主完成、待复核
    operation = next(node for node in flow["nodes"] if node["id"] == "core.operation")
    assert operation["display_state"] == "auto" and operation["flags"]["review_source"] == "autobuild"
    assert (root / "ontology" / "rule" / "规则1.md").is_file() and flow["slices"] == []
    # 指标节点（旁支）也是这一轮顺带写的：自主完成、待复核，不是人确认过
    metric = next(node for node in flow["nodes"] if node["id"] == "core.metric")
    assert metric["display_state"] == "auto" and metric["flags"]["review_source"] == "autobuild"


def test_only_steps_the_run_owns_are_handed_over():
    """开始时别的对话里没做完的执行，哪些交给运行收尾。

    运行计划里有的交给运行；不在计划里的，停在等回答（活干完了）、这一步也做完了的收尾；停在干活中途的（条目写了一半也算
    「做完」）、运行范围外又没做完的留着，照常能续跑。"""
    run = {"units": [{"id": "all", "kind": "slice", "steps": [{"id": "operation"}]},
                     {"id": "core.validate", "kind": "node", "steps": [{"id": "core.validate"}]}]}
    rows = {"all": {"object": "confirmed", "operation": "ready", "rule": "not_ready"}}
    runner_class = runner_module.AutobuildRunner

    def step(name, phase=None, state="interrupted"):
        return {"slice": {"id": "all", "name": "全部"}, "step": name, "node": {"id": "core.object"},
                "state": state, "command_name": "extract-object", "phase": {"raw": phase}}

    assert runner_class._in_run(run, step("operation")) is True
    assert runner_class._in_run(run, step("object")) is False
    assert runner_class._in_run(run, {"slice": None, "step": None, "node": {"id": "core.validate"}}) is True
    assert runner_class._done_at_start(step("object"), rows) is True
    assert runner_class._done_at_start(step("rule"), rows) is False
    assert runner_class._done_at_start({"slice": None, "step": None, "node": {"id": "core.metric"}}, rows) is None
    flow = {"nodes": [], "commands": {}}
    assert runner_module._stopped_for_reply(step("object", "confirm"), flow) is True
    assert runner_module._stopped_for_reply(step("object", "objects"), flow) is False
    assert runner_module._stopped_for_reply(step("object", None, state="waiting"), flow) is True


def test_tail_redoes_a_node_taken_over_mid_work(tmp_path, monkeypatch):
    """被接管时停在干活中途的主线节点（校验写了一半的报告也满足完成判定）：已确认也排进运行，提示从哪停的。"""
    from app.services.autobuild import plan
    from app.services.flow import view

    flow_doc = {"nodes": [{"id": "core.object"}, {"id": "core.operation"},
                          {"id": "core.validate", "title": "校验", "entry": {"command": "/ontology-validate"}}],
                "commands": {"extract-object": {"node": "core.object"}, "extract-operation": {"node": "core.operation"}}}
    monkeypatch.setattr(view, "build", lambda root: {"nodes": [{"id": "core.object", "state": "confirmed"},
                                                               {"id": "core.operation", "state": "confirmed"},
                                                               {"id": "core.validate", "state": "confirmed"}]})
    policy = {"scope": {"until_node": "core.validate"}}
    assert plan._tail(tmp_path, flow_doc, policy) == []
    units = plan._tail(tmp_path, flow_doc, policy, {"core.validate": {"step": "core.validate", "phase": "检查"}})
    assert [unit["id"] for unit in units] == ["core.validate"] and units[0]["steps"][0]["resume_from"] == "检查"


def test_preview_warns_about_unanswered_blueprint_questions(ab):
    """蓝图里没回答的问题留在待处理、运行不代答（契约 BP-03）：开始前预览说清楚（实测：没提，跑完还在待处理里）。"""
    from app.services.flow import blueprint as blueprint_service

    client, workspaces, project_id, _root = ab
    base = f"/api/v1/projects/{project_id}/autobuild"
    client.put(f"{base}/policy", json=SLICES_ONLY)
    assert client.post(f"{base}/runs/preview", json={}).json()["warnings"] == []
    blueprint_service.add_question(workspaces, project_id, "补货金额按含税还是不含税", actor="employee_0001")
    warnings = client.post(f"{base}/runs/preview", json={}).json()["warnings"]
    assert warnings == ["建模蓝图里有 1 个问题没回答，运行不会替你答，会留在待处理里"]


def test_take_over_mid_work_lets_the_run_finish_that_step(ab, monkeypatch):
    """对象提取写到一半（「写入对象」阶段，不是在等回答）时接管，运行接着做完这一步。

    写了一半的对象合入后按「条目在」算这一步做完了，运行原来直接跳过对象（实测）。现在运行照做，提示里说清从哪停的、
    已保存的不要重做；做完记自主完成。"""
    import asyncio
    import threading

    from tests.autobuild_helpers import OBJECTS
    from tests.test_changesets import obj

    client, _workspaces, project_id, _root = ab
    held = threading.Event()

    def write_two(ctx):
        from pathlib import Path

        for name in OBJECTS[:2]:
            path = Path(ctx.cwd) / "ontology" / "object" / f"{name}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(obj(name, "合成对象"), encoding="utf-8")

    async def hold(ctx):
        await asyncio.to_thread(held.wait, 30)

    AutoEngine.hooks["/ontology-extract-object:all"] = [lock_phase("objects"), write_two, hold]
    monkeypatch.setattr(AutoEngine, "interrupt", stopping_interrupt(held))
    default = client.get(f"/api/v1/projects/{project_id}/conversations").json()["items"][0]["id"]
    assert client.post(f"/api/v1/projects/{project_id}/conversations/{default}/messages",
                       json={"text": "/ontology-extract-object"}).status_code == 202
    wait_until(lambda: "/ontology-extract-object" in AutoEngine.sent)
    base = f"/api/v1/projects/{project_id}/autobuild"
    client.put(f"{base}/policy", json=SLICES_ONLY)
    preview = client.post(f"{base}/runs/preview", json={}).json()
    assert preview["busy"] == {"conversation_id": default, "waiting": False}
    started = client.post(f"{base}/runs", json={"preview_token": preview["preview_token"], "take_over": True})
    assert started.status_code == 201, started.text
    run = wait_state(client, project_id, started.json()["run_id"], "completed")
    assert [text for text in AutoEngine.sent if text.startswith("/ontology-extract")] == [
        "/ontology-extract-object", "/ontology-extract-object", "/ontology-extract-operation"]
    first = next(hint for hint in AutoEngine.hints if hint.startswith("自主构建 · 运行 #1"))
    assert "「对象提取」是从陪伴建模接过来的、没做完的一步（停在「写入对象」）" in first
    flow = client.get(f"/api/v1/projects/{project_id}/flow").json()
    states = {node["id"]: node["display_state"] for node in flow["nodes"]}
    assert states["core.object"] == "auto" and states["core.operation"] == "auto"
    records = [record for record in exec_records_of(project_id) if record.get("conversation_id") == default]
    assert [(record["state"], record["phase"]["raw"]) for record in records] == [("abandoned", "objects")]
    assert run_of(client, project_id, run["run_id"])["matrix"][0]["cells"]["object"] == "auto"


def lock_phase(phase: str, command: str = "extract-object"):
    """假引擎把命令锁切到某个阶段（工具箱的命令等你回答之前切到 confirm）。"""
    def action(ctx):
        from pathlib import Path

        path = Path(ctx.cwd) / ".ontology-state" / f"lock-{command}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"command": command, "phase": phase}), encoding="utf-8")
    return action


def stopping_interrupt(held):
    """和真引擎一样的打断：这一轮剩下的不再做，马上以「已打断」结束（假引擎原来只在队尾补一个结果）。"""
    from tests.autobuild_helpers import result

    async def interrupt(self):
        while not self.queue.empty():
            self.queue.get_nowait()
        await self.queue.put(result(subtype="interrupted"))
        held.set()
    return interrupt


def exec_records_of(project_id: str) -> list[dict]:
    from app.services.session_host import exec_records

    return exec_records.all_records(project_id)


def test_pause_then_stop_at_safe_point_then_convert(ab):
    client, _workspaces, project_id, root = ab
    release = gate("/ontology-extract-object:all")
    run = start(client, project_id, **UNTIL_VALIDATE)
    base = f"/api/v1/projects/{project_id}/autobuild/runs/{run['run_id']}"
    wait_until(lambda: run_of(client, project_id, run["run_id"])["current"])
    pausing = client.post(f"{base}/pause").json()
    assert pausing["state"] == "pausing"
    release.set()
    # 这一步（对象）做完就停：这一段还没合入，变更集开着、写入范围锁在这一段
    paused = wait_state(client, project_id, run["run_id"], "paused")
    assert paused["matrix"][0]["cells"]["object"] == "auto" and paused["matrix"][0]["cells"]["operation"] == "queued"
    scope = client.get(f"/api/v1/projects/{project_id}/scope").json()
    assert scope["state"] == "autobuild_slice" and scope["run_id"] == run["run_id"]
    assert client.post(f"{base}/pause").json()["code"] == "autobuild_not_running"
    # 续跑：接着做这一段剩下的步骤（不新开上下文）；做操作时叫停，这一段做完停在安全点，校验不再开始
    release2 = gate("/ontology-extract-operation:all")
    assert client.post(f"{base}/resume").json()["state"] in ("queued", "running")
    wait_until(lambda: (run_of(client, project_id, run["run_id"])["current"] or {}).get("step_label") == "操作提取"
               or "/ontology-extract-operation" in AutoEngine.sent)
    assert client.post(f"{base}/stop-at-safe-point").json()["state"] == "stopping"
    release2.set()
    stopped = wait_state(client, project_id, run["run_id"], "stopped")
    assert stopped["slices"] == {"done": 1, "total": 1}
    assert [row["result"] for row in stopped["matrix"]] == ["完成"]
    assert "/ontology-validate" not in AutoEngine.sent
    assert [event["data"]["next"] for event in project_events(project_id, "autobuild.safe_point")] == ["stopped"]
    assert client.get(f"/api/v1/projects/{project_id}/scope").json()["state"] == "none"
    # 转为陪伴：第二次运行只剩校验，做完当前这一步就转，项目默认构建方式不变
    release3 = gate("/ontology-validate")
    second = start(client, project_id, **UNTIL_VALIDATE)
    assert second["run_no"] == 2 and second["slices"]["total"] == 0
    wait_until(lambda: run_of(client, project_id, second["run_id"])["current"])
    converting = client.post(f"/api/v1/projects/{project_id}/autobuild/runs/{second['run_id']}/convert-to-l2").json()
    assert converting["state"] == "converting"
    release3.set()
    converted = wait_state(client, project_id, second["run_id"], "converted")
    assert converted["state"] == "converted" and "/ontology-validate" in AutoEngine.sent
    assert client.get(f"/api/v1/projects/{project_id}/build-mode").json() == {
        "build_mode": "L2", "autobuild_running": False, "run_id": None}
    # 转为陪伴之后这条对话照常能聊
    assert client.post(f"/api/v1/projects/{project_id}/conversations/{second['conversation_id']}/messages",
                       json={"text": "接着做下一步"}).status_code == 202


def test_usage_is_recorded_but_never_ends_the_run(ab):
    client, _workspaces, project_id, _root = ab
    # 每轮 125 token（对象、操作两轮），老客户端带的 200 token 预算忽略：用量照记，运行一直做到完
    run = start(client, project_id, scope={"until_node": "core.operation"}, budget={"max_tokens": 200})
    done = wait_state(client, project_id, run["run_id"], "completed")
    assert done["slices"] == {"done": 1, "total": 1} and done["tokens"]["used"] > 200
    assert set(done["tokens"]) == {"used"} and set(done["cost"]) == {"used"} and "budget" not in done["policy"]
    assert not project_events(project_id, "autobuild.budget_warning")
    report = client.get(f"/api/v1/projects/{project_id}/autobuild/runs/{run['run_id']}/report").json()
    assert not {"budget", "budget_duration_ms"} & set(report["metrics"])


def test_quota_exhausted_pauses_the_run_and_resume_finishes_it(ab, monkeypatch):
    client, _workspaces, project_id, root = ab
    manager = agent_sessions.manager
    original = manager.send
    blocked = {"on": True}

    async def send(pid, root, text, *args, **kwargs):
        if blocked["on"] and text.startswith("/ontology-extract-operation"):
            raise QuotaExceeded("daily_tokens", 1000, 1000, {"kind": "user", "id": "employee_0001"})
        return await original(pid, root, text, *args, **kwargs)

    monkeypatch.setattr(manager, "send", send)
    run = start(client, project_id, **SLICES_ONLY)
    # 对象那一步做完、操作那一步发不出去：暂停在这一步，这一段还没做完
    paused = wait_state(client, project_id, run["run_id"], "paused")
    assert paused["pause_reason"] == "quota" and paused["slices"]["done"] == 0
    assert paused["matrix"][0]["cells"]["object"] == "auto"
    assert not store.get(project_id, run["run_id"]).get("failure")  # 配额用完不算出错
    notices = [item["title"] for item in notifications.list_notifications("employee_0001")
               if item["kind"] == "autobuild_stopped"]
    assert notices == ["自主构建已暂停"]
    blocked["on"] = False
    resumed = client.post(f"/api/v1/projects/{project_id}/autobuild/runs/{run['run_id']}/resume").json()
    assert resumed["pause_reason"] is None
    done = wait_state(client, project_id, run["run_id"], "completed")
    assert done["slices"] == {"done": 1, "total": 1}
    assert (root / "ontology" / "operation" / "操作5.md").is_file()


def test_stop_while_quota_is_exhausted_ends_the_run(ab, monkeypatch):
    client, _workspaces, project_id, _root = ab
    manager = agent_sessions.manager
    original = manager.send

    async def send(pid, root, text, *args, **kwargs):
        if text.startswith("/ontology-extract-operation"):
            raise QuotaExceeded("daily_tokens", 1000, 1000, {"kind": "user", "id": "employee_0001"})
        return await original(pid, root, text, *args, **kwargs)

    monkeypatch.setattr(manager, "send", send)
    run = start(client, project_id, **SLICES_ONLY)
    wait_state(client, project_id, run["run_id"], "paused")
    # 配额还没补：停在安全点要做完这一段也做不完，直接按停下结束（已做的对象照样合入），不再暂停回去
    client.post(f"/api/v1/projects/{project_id}/autobuild/runs/{run['run_id']}/stop-at-safe-point")
    stopped = wait_state(client, project_id, run["run_id"], "stopped")
    assert stopped["matrix"][0]["cells"]["operation"] != "auto"


def test_acknowledge_ended_run_returns_to_companion_mode(ab):
    client, _workspaces, project_id, _root = ab
    base = f"/api/v1/projects/{project_id}"
    assert client.put(f"{base}/build-mode", json={"build_mode": "L4"}).status_code == 200
    release = gate("/ontology-extract-object:all")
    run = start(client, project_id, **SLICES_ONLY)
    ack = f"{base}/autobuild/runs/{run['run_id']}/acknowledge"
    early = client.post(ack)
    assert early.status_code == 409 and early.json()["code"] == "autobuild_not_running"
    release.set()
    done = wait_state(client, project_id, run["run_id"], "completed")
    assert done["acknowledged"] is False
    acked = client.post(ack)
    assert acked.status_code == 200 and acked.json()["acknowledged"] is True
    assert client.get(f"{base}/build-mode").json()["build_mode"] == "L2"
    updated = project_events(project_id, "project.updated")[-1]["data"]
    assert updated["fields"] == ["build_mode"] and updated["meta"]["build_mode"] == "L2"
    assert client.post(ack).status_code == 200  # 再确认一次照样成功
    assert run_of(client, project_id, run["run_id"])["acknowledged"] is True


def test_restart_resumes_from_the_checkpoint(ab, monkeypatch):
    client, workspaces, project_id, _root = ab
    release = gate("/ontology-extract-operation:all")
    run = start(client, project_id, **SLICES_ONLY)
    # 操作那一步发出去了、引擎正在跑
    wait_until(lambda: (store.get(project_id, run["run_id"])["cursor"].get("step") == "operation"
                        and store.get(project_id, run["run_id"])["cursor"].get("unit") == "all"
                        and store.get(project_id, run["run_id"])["cursor"].get("exec_id")))
    # 停机：运行停在检查点（操作那一步还在跑）
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
        assert done["recovered_from_checkpoint"] == 1 and done["slices"] == {"done": 1, "total": 1}
        report = again.get(f"/api/v1/projects/{project_id}/autobuild/runs/{run['run_id']}/report").json()
        assert report["recovered_restarts"] == 1
        # 操作那一步按执行记录续跑（续跑指令带原命令）
        assert any(text.startswith("/ontology-extract-operation 继续上次没做完") for text in AutoEngine.sent)
        # 启动自检报告（O-35）记了这一项
        from app.services import startup_report

        item = next(entry for entry in startup_report.latest()["items"] if entry["id"] == "autobuild_runs")
        assert item["tone"] == "fix" and "1 次启动后自动续跑" in item["detail"]
