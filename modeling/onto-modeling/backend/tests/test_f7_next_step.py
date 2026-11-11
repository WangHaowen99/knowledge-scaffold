"""F7 下一步建议（详设 6.10）：规则表从上往下判、命中第一条就停；能显示就能执行；跟着会话状态重算并推送。

- 第 1～4 条（运行中、等你回答、这一轮出错、刚被打断）走真实会话（假引擎），主推、次选都真的点一次接口；
- 第 5～10 条用装配好的流程直接喂 flow_state.suggest，出来的命令再经 S-20 发一次，全部 2xx；
- 打断之后能力升级，「继续「X」」按新版重新执行，不再 409；
- 看到按钮到点下去之间情况变了：409 suggestion_stale，带回重算后的建议。
测试数据全部为合成内容。
"""
from __future__ import annotations

import asyncio

from app.services.agent_sessions import _busy
from app.services.flow import model, next_step
from app.services.session_host import exec_records
from tests.test_commands_api import api, wait  # noqa: F401 - 夹具
from tests.test_session_host import FakeEngine, lock, result

P = "/api/v1/projects"


def fl02(client, project_id: str) -> dict:
    response = client.get(f"{P}/{project_id}/flow/suggestions", params={"conversation_id": "default"})
    assert response.status_code == 200, response.text
    return response.json()


def items(sug: dict) -> list[dict]:
    return [item for item in [sug.get("primary"), *(sug.get("secondary") or [])] if item]


def click(client, project_id: str, item: dict):
    """照界面那样把建议条上的一项点下去（请求体带 from_suggestion）；纯界面动作（跳页、弹窗、滚到卡片）返回 None。"""
    action = item["action"]
    kind = action["type"]
    flag = {"from_suggestion": True, "conversation_id": "default"}
    if kind == "send" and action.get("command"):
        return client.post(f"{P}/{project_id}/commands", json={
            "command": action["command"], "args": action.get("args") or {}, "text": action.get("text") or "",
            "origin": {"page": "wb-l2", "label": "下一步建议"}, **flag})
    if kind == "send":
        return client.post(f"{P}/{project_id}/conversations/default/messages", json={"text": action["text"], **flag})
    if kind == "resume_exec":
        return client.post(f"{P}/{project_id}/execs/{action['exec_id']}/resume", json=flag)
    if kind == "abandon_exec":
        return client.post(f"{P}/{project_id}/execs/{action['exec_id']}/abandon", json=flag)
    if kind == "retry":
        return client.post(f"{P}/{project_id}/conversations/default/retry", json=flag)
    assert kind in ("navigate", "confirm_blueprint", "focus_question"), kind
    return None


def idle(manager, project_id: str):
    """会话真正空下来：不在跑、不在连、没有积压的消息。"""
    return wait(lambda: (session := manager.get(project_id)) is not None and session.state == "idle"
                and not _busy(session) and not session.pending_prompts and session)


def pushed(manager, project_id: str) -> list[dict]:
    return [event["payload"] for event in manager.get_history(project_id, "default") if event.get("type") == "suggestions"]


def interrupted_exec(client, manager, project_id: str) -> dict:
    """发一条命令，跑到一半打断：执行记为中断。"""
    FakeEngine.scripts = [[lock("extract-operation", "operation"),
                           {"type": "assistant_message", "blocks": [{"kind": "tool_use", "tool": "Bash", "tool_use_id": "tu-1",
                                                                     "input": {"command": "python3 x.py"}}]}]]
    response = client.post(f"{P}/{project_id}/commands", json={"command": "/ontology-extract-operation",
                                                                "origin": {"page": "wb-l2", "label": "全部命令"}})
    assert response.status_code == 202, response.text
    wait(lambda: (session := manager.get(project_id)) is not None and session.exec_id and session.state == "running")
    assert client.post(f"{P}/{project_id}/conversations/default/interrupt", json={}).status_code == 200
    idle(manager, project_id)
    return wait(lambda: next(iter(exec_records.query(project_id, ["interrupted"])), None))


# ---- 第 1 条：运行中不显示 ----


def test_running_hides_suggestions_and_pushes_on_state_change(api):
    client, manager, project_id, _ = api
    FakeEngine.scripts = [[{"type": "assistant_message", "blocks": [{"kind": "text", "text": "在看资料"}]}]]
    client.post(f"{P}/{project_id}/conversations/default/messages", json={"text": "门店有几类？"})
    wait(lambda: (session := manager.get(project_id)) is not None and session.state == "running")
    sug = fl02(client, project_id)
    assert sug["mode"] == "running" and sug["primary"] is None and sug["secondary"] == [] and sug["rev"]
    wait(lambda: pushed(manager, project_id) and pushed(manager, project_id)[-1]["mode"] == "running")
    # 一轮结束：推回按流程算的那一份，版本号更大
    running_rev = pushed(manager, project_id)[-1]["rev"]
    session = manager.get(project_id)
    client.portal.call(session.engine.queue.put, result())
    idle(manager, project_id)
    last = wait(lambda: (found := pushed(manager, project_id)[-1]) and found["mode"] != "running" and found)
    assert last["mode"] == "normal" and last["primary"] and last["rev"] > running_rev


# ---- 第 2 条：等你回答 ----


def test_pending_question_offers_answer_and_back_to_running_after_answer(api):
    client, manager, project_id, _ = api
    ask = {"questions": [{"question": "订单按什么口径统计？", "options": [{"label": "按天"}, {"label": "按周"}]}]}

    def raise_question(ctx):
        asyncio.get_running_loop().create_task(ctx.request_permission("AskUserQuestion", ask, None, None))

    FakeEngine.scripts = [[raise_question]]
    client.post(f"{P}/{project_id}/conversations/default/messages", json={"text": "开始"})
    session = wait(lambda: (found := manager.get(project_id)) is not None and found.pending_permissions and found)
    question_id = next(iter(session.pending_permissions.values())).questions[0]["question_id"]
    sug = fl02(client, project_id)
    assert sug["mode"] == "waiting_decision"
    assert sug["primary"]["label"] == "去回答" and not sug["primary"]["disabled"]
    assert sug["primary"]["action"] == {"type": "focus_question", "question_id": question_id}
    assert next_step.blocker(project_id, "default", sug["primary"]["action"]) is None
    wait(lambda: pushed(manager, project_id)[-1]["mode"] == "waiting_decision")
    answered = client.post(f"{P}/{project_id}/conversations/default/questions/{question_id}/answer",
                           json={"option_id": "0.0"})
    assert answered.status_code == 200, answered.text
    wait(lambda: pushed(manager, project_id)[-1]["mode"] == "running")
    assert next_step.blocker(project_id, "default", sug["primary"]["action"]) is not None


# ---- 第 3 条：这一轮出错 ----


def test_failed_turn_offers_retry_that_really_retries(api):
    client, manager, project_id, _ = api
    FakeEngine.scripts = [[{"type": "assistant_message", "blocks": [{"kind": "text", "text": "答"}]},
                           result(subtype="error_during_execution", is_error=True),
                           {"type": "_engine_end", "reason": "error", "error": "模型服务暂时不稳定，请稍后重试"}]]
    client.post(f"{P}/{project_id}/conversations/default/messages", json={"text": "门店有几类？"})
    wait(lambda: (session := manager.get(project_id)) is not None and session.state == "error")
    sug = fl02(client, project_id)
    assert sug["mode"] == "failed" and sug["primary"]["label"] == "重试这一轮"
    assert sug["note"] == "模型服务暂时不稳定，请稍后重试"
    assert pushed(manager, project_id)[-1]["mode"] == "failed"
    response = click(client, project_id, sug["primary"])
    assert response.status_code == 202, response.text


# ---- 第 4 条：刚被打断 ----


def test_interrupted_offers_continue_and_abandon_both_executable(api):
    client, manager, project_id, _ = api
    record = interrupted_exec(client, manager, project_id)
    sug = fl02(client, project_id)
    assert sug["mode"] == "interrupted"
    assert sug["primary"]["label"] == "继续「操作提取」"
    assert sug["primary"]["action"] == {"type": "resume_exec", "exec_id": record["id"]}
    assert [item["label"] for item in sug["secondary"]] == ["放弃这一步"]
    assert sug["note"].startswith("从「写入操作」接着做")
    assert all(item["action"]["type"] != "clear_input" for item in items(sug)), "不再有「换个方向」"
    assert pushed(manager, project_id)[-1]["primary"]["action"] == sug["primary"]["action"]
    response = click(client, project_id, sug["primary"])
    assert response.status_code == 202, response.text
    assert response.json()["mode"] == "resume"
    idle(manager, project_id)

    record = interrupted_exec(client, manager, project_id)
    abandon = fl02(client, project_id)["secondary"][0]
    response = click(client, project_id, abandon)
    assert response.status_code == 200, response.text
    assert exec_records.get(project_id, record["id"])["state"] == "abandoned"
    assert fl02(client, project_id)["mode"] == "normal", "放弃之后回到按流程算"


def test_continue_after_upgrade_reruns_with_new_version(api):
    client, manager, project_id, root = api
    record = interrupted_exec(client, manager, project_id)
    # 模拟部署升级：原记录里的节点版本和现在的对不上
    exec_records.update(project_id, record["id"], node={**record["node"], "version": "core@0.0.1+old"})
    assert exec_records.not_resumable_reason(exec_records.get(project_id, record["id"]), root,
                                             model.load_for(root)) == "node_upgraded"
    sug = fl02(client, project_id)
    assert sug["mode"] == "interrupted" and sug["primary"]["label"] == "继续「操作提取」"
    assert sug["note"].startswith("能力已升级，按新版从头做")
    sent_before = len(FakeEngine.sent)
    response = click(client, project_id, sug["primary"])
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["mode"] == "rerun" and body["replaced"]["id"] == record["id"]
    old = exec_records.get(project_id, record["id"])
    assert old["state"] == "abandoned" and old["replaced_by"] == body["exec"]["id"] != record["id"]
    assert body["exec"]["command_label"] == "操作提取"
    prompt = FakeEngine.sent[sent_before]
    assert prompt.startswith("/ontology-extract-operation") and "已经保存的修改保留，不要重做" in prompt
    idle(manager, project_id)
    # 做完的那份不能再继续（不是从建议条来的：原来的错误码）
    again = client.post(f"{P}/{project_id}/execs/{record['id']}/resume", json={})
    assert again.status_code == 409 and again.json()["code"] == "exec_not_resumable"


def test_stale_click_returns_fresh_suggestions(api):
    client, manager, project_id, _ = api
    record = interrupted_exec(client, manager, project_id)
    sug = fl02(client, project_id)
    # 另一处（待处理）先放弃了这一步，建议条上的「继续」已经过时
    assert client.post(f"{P}/{project_id}/execs/{record['id']}/abandon", json={}).status_code == 200
    response = click(client, project_id, sug["primary"])
    assert response.status_code == 409
    error = response.json()
    assert error["code"] == "suggestion_stale" and error["details"]["reason"]
    assert error["details"]["suggestions"]["mode"] == "normal"


# ---- 第 5～10 条：按流程 ----


def _suggest(root, states: dict, context: dict, check=None) -> dict:
    flow = model.load_for(root)
    module = model.flow_state(root)
    assert getattr(module, "SUGGEST_RULES", 0) >= 5
    return module.suggest(flow, states, context, check)


# 建模的下一步（F8 起只有「全部」一段，不带分段）
FULL_NEXT = {"slice_id": "all", "slice_name": "全部", "step": "object", "label": "提取对象",
             "command": "/ontology-extract-object", "sliced": False}


def _states(root, **overrides) -> dict:
    flow = model.load_for(root)
    out = {node["id"]: {"state": "confirmed", "flags": {}} for node in flow["nodes"]}
    out.update({key.replace("_", "."): {"state": value, "flags": {}} for key, value in overrides.items()})
    return out


def test_flow_rules_in_order(api):
    _, _, _, root = api
    fresh = _states(root, core_research="ready", core_model="not_ready", core_validate="not_ready",
                    core_publish="not_ready")
    # 9：主线下一个必经步骤（资料分析的几个入口）
    mainline = _suggest(root, fresh, {"blueprint": {"state": "none"}})
    assert mainline["mode"] == "normal" and mainline["primary"]["id"].startswith("core.research#")
    assert mainline["note"] is None
    # 动作的条件 when：还没有资料分析报告时不出「追加分析」，有了才出
    no_report = _suggest(root, fresh, {"blueprint": {"state": "none"}, "probes": {"research_report": False}})
    assert [item["label"] for item in items(no_report)] == ["资料分析"]
    with_report = _suggest(root, fresh, {"blueprint": {"state": "none"}, "probes": {"research_report": True}})
    assert [item["label"] for item in items(with_report)] == ["资料分析", "追加分析"]
    # 5：合入冲突，其余按 6～10 补齐
    conflict = _suggest(root, fresh, {"blueprint": {"state": "none"}, "changeset": {"id": "cs-1", "state": "conflict"}})
    assert conflict["primary"]["label"] == "处理冲突"
    assert [item["id"] for item in conflict["secondary"]] == [item["id"] for item in items(mainline)][:2]
    # 6：目标问题没答：主推去回答，后面跟主线下一步（F8：蓝图草案不用确认，不拦着往下建）
    goal = _suggest(root, _states(root, core_research="confirmed", core_model="not_ready"),
                    {"blueprint": {"state": "draft", "goal_pending": True}, "slice_next": FULL_NEXT})
    assert [item["label"] for item in items(goal)][:2] == ["回答目标问题", "提取对象"]
    draft = _suggest(root, _states(root, core_research="confirmed", core_model="not_ready"),
                     {"blueprint": {"state": "draft"}, "slice_next": FULL_NEXT})
    assert draft["primary"]["label"] == "提取对象"
    # 7：过期的步骤，按钮写清哪类变了、刷新哪一段哪一步（不再另起说明行）
    stale_step = {"slice_id": "s1", "slice_name": "补货", "step": "rule", "step_label": "规则", "label": "提取规则",
                  "command": "/ontology-extract-rule-metric", "sliced": True, "upstream": "对象、操作"}
    second = {**stale_step, "slice_id": "s2", "slice_name": "退货"}
    # 规则是可选的补抽：后面的校验还会跑（跑过、能跑、建模没做完还不能跑）就交给校验，不推补抽（端到端实测：规则与指标
    # 提取补了回链，「规则」过期后建议条推的正是刚跑完的同一条命令）；没有能接手的核对节点时照旧推刷新
    for later in ("ready", "not_ready"):
        deferred = _suggest(root, _states(root, core_model="stale", core_validate=later),
                            {"blueprint": {"state": "confirmed"}, "slice_stale": [stale_step, second]})
        assert not any(item["id"].startswith("refresh:") for item in items(deferred))
    stale = _suggest(root, _states(root, core_model="stale", core_validate="skipped"),
                     {"blueprint": {"state": "confirmed"}, "slice_stale": [stale_step, second]})
    assert [item["label"] for item in items(stale)] == ["对象、操作有变化，刷新「补货 · 规则」", "对象、操作有变化，刷新「退货 · 规则」"]
    assert stale["primary"]["action"] == {"type": "send", "text": "", "command": "/ontology-extract-rule-metric",
                                          "args": {"slice": "s1"}}
    assert stale["note"] is None
    # 必经的步骤（操作）过期、校验没跑过：照旧推刷新（新增对象可能要补提取操作，校验补不出来）
    op_step = {**stale_step, "step": "operation", "step_label": "操作", "label": "提取操作",
               "command": "/ontology-extract-operation", "upstream": "对象"}
    required = _suggest(root, _states(root, core_model="stale", core_validate="ready"),
                        {"blueprint": {"state": "confirmed"}, "slice_stale": [op_step]})
    assert items(required)[0]["label"] == "对象有变化，刷新「补货 · 操作」"
    whole = _suggest(root, _states(root, core_validate="stale"), {"stale_upstream": {"core.validate": "对象、操作"}})
    assert whole["primary"]["label"] == "对象、操作有变化，刷新校验" and whole["note"] is None
    unknown = _suggest(root, _states(root, core_validate="stale"), {})
    assert unknown["primary"]["label"] == "刷新校验"  # 说不清哪类变了（老记录）时只写刷新什么
    # 7 不推上游：后面已确认的校验能核对并修复同一批改动（stale_on 盖住、写入范围盖住）。校验的修复改了对象、校验已确认 →
    # 不推重新提取操作，主线往下走；资料变了（校验不看资料）照旧推刷新建模；校验也过期（改动在校验之后）时两个都推
    op_step = {"slice_id": "all", "slice_name": "全部", "step": "operation", "step_label": "操作",
               "command": "/ontology-extract-operation", "sliced": False, "upstream": "对象"}
    checked = _suggest(root, _states(root, core_model="stale", trace_trace="ready", core_publish="not_ready"),
                       {"slice_stale": [op_step], "stale_upstream": {"core.metric": "对象、操作"}})
    assert not any(item["id"].startswith("refresh:") for item in items(checked))
    assert checked["primary"]["id"].startswith("trace.trace")
    obj_step = {**op_step, "step": "object", "step_label": "对象", "command": "/ontology-extract-object", "upstream": "资料"}
    research = _suggest(root, _states(root, core_model="stale"), {"slice_stale": [obj_step]})
    assert research["primary"]["id"] == "refresh:core.model:all:object"
    both = _suggest(root, _states(root, core_model="stale", core_validate="stale"),
                    {"slice_stale": [op_step], "stale_upstream": {"core.validate": "对象、操作"}})
    assert [item["id"] for item in items(both)] == ["refresh:core.model:all:operation", "refresh:core.validate"]
    # 7 不出：增量构建一轮进行中（范围限定生效），传播交给那一轮的核对，不推整段重新提取（会写范围外的条目）
    confined = _suggest(root, _states(root, core_model="stale", core_validate="ready", core_publish="not_ready"),
                        {"slice_stale": [stale_step], "confine_active": True})
    assert not any(item["id"].startswith("refresh:") for item in items(confined))
    assert confined["primary"]["id"].startswith("core.validate")
    # 命令停下来等你在对话里打字回复：不推别的命令（点了会把命令丢在半路），只留说明；有确认卡时照旧主推去回答
    waiting = _suggest(root, _states(root, core_model="stale"),
                       {"slice_stale": [stale_step], "awaiting_reply": {"id": "ex-1", "label": "校验"}})
    assert waiting["primary"] is None and waiting["secondary"] == []
    assert waiting["note"] == "「校验」在等你回复上面的问题"
    asked = _suggest(root, _states(root, core_model="stale"),
                     {"awaiting_reply": {"id": "ex-1", "label": "校验"}, "question": {"id": "q1", "kind": "question"}})
    assert asked["mode"] == "waiting_decision"
    # 8：报告里还有待处理的问题
    report = _suggest(root, _states(root), {"stage": "校验待审阅"})
    assert report["primary"]["label"] == "继续校验"
    # 9（按段）：带段名
    sliced = _suggest(root, _states(root, core_model="running"), {
        "blueprint": {"state": "confirmed"},
        "slice_next": {"slice_id": "s1", "slice_name": "补货", "step": "operation", "label": "提取操作",
                       "command": "/ontology-extract-operation", "sliced": True}})
    sliced_model = _suggest(root, _states(root, core_model="not_ready"), {
        "blueprint": {"state": "confirmed"},
        "slice_next": {"slice_id": "s1", "slice_name": "补货", "step": "operation", "label": "提取操作",
                       "command": "/ontology-extract-operation", "sliced": True}})
    assert sliced["mode"] == "normal"
    assert sliced_model["primary"]["label"] == "提取操作「补货」"
    # 10：必经都做完
    done = _suggest(root, _states(root), {})
    assert done["primary"]["label"] == "查看整体视图"
    # 能执行的检查：整条规则都不能执行时往下一条判
    blocked = _suggest(root, _states(root, core_model="stale"), {"slice_stale": [stale_step], "stage": "校验待审阅"},
                       check=lambda action: action.get("command") != "/ontology-extract-rule-metric")
    assert blocked["primary"]["label"] == "继续校验"


def test_flow_rule_commands_are_accepted_by_the_api(api):
    """第 5～10 条给出的命令按建议条的方式发一次（S-20，带 from_suggestion），都收下（2xx）。"""
    client, manager, project_id, root = api
    stale_step = {"slice_id": "all", "slice_name": "全部", "step": "rule", "step_label": "规则", "label": "提取规则",
                  "command": "/ontology-extract-rule-metric", "sliced": False, "upstream": "对象、操作"}
    fresh = _states(root, core_research="ready", core_model="not_ready", core_validate="not_ready",
                    core_publish="not_ready")
    produced = [
        _suggest(root, fresh, {"blueprint": {"state": "none"}, "changeset": {"id": "cs-1", "state": "conflict"}}),
        _suggest(root, _states(root, core_research="confirmed", core_model="not_ready"),
                 {"blueprint": {"state": "draft", "goal_pending": True}, "slice_next": FULL_NEXT}),
        _suggest(root, _states(root, core_model="stale"), {"slice_stale": [stale_step]}),
        _suggest(root, _states(root), {"stage": "校验待审阅"}),
        _suggest(root, fresh, {"blueprint": {"state": "none"}}),
        _suggest(root, _states(root), {}),
    ]
    check = next_step.allowed
    for sug in produced:
        for item in items(sug):
            assert check(project_id, "default", item["action"], root=root), item
            response = click(client, project_id, item)
            if response is None:
                continue
            assert response.status_code in (200, 202), (item["label"], response.text)
            idle(manager, project_id)


def test_suggestions_hide_commands_that_are_not_installed(api):
    _, _, project_id, root = api
    assert next_step.blocker(project_id, "default", {"type": "send", "command": "/ontology-nonexistent"},
                             root) == "这条命令所属的能力没有启用"
