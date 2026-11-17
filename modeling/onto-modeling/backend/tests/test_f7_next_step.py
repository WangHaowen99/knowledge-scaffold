"""F7 下一步建议（详设 6.10）：规则表从上往下判、命中第一条就停；能显示就能执行；跟着会话状态重算并推送。

- 第 1～4 条（运行中、等你回答、这一轮出错、刚被打断）走真实会话（假引擎），主推、次选都真的点一次接口；
- 按流程的几级（第 2～5 级，合入冲突也在这里）用装配好的流程直接喂 flow_state.suggest，出来的命令再经 S-20 发一次，全部 2xx；
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


def session_where(manager, project_id: str, ready):
    """等会话出现并满足 ready(session)，返回这个会话。"""

    def check():
        session = manager.get(project_id)
        return session if session is not None and ready(session) else None

    return wait(check)


def idle(manager, project_id: str):
    """会话真正空下来：不在跑、不在连、没有积压的消息。"""
    return session_where(manager, project_id,
                         lambda session: session.state == "idle" and not _busy(session) and not session.pending_prompts)


def settled_push(manager, project_id: str):
    """最近一次推送已经不是「运行中」。"""
    found = pushed(manager, project_id)[-1]
    return found if found and found["mode"] != "running" else None


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
    session_where(manager, project_id, lambda session: session.exec_id and session.state == "running")
    assert client.post(f"{P}/{project_id}/conversations/default/interrupt", json={}).status_code == 200
    idle(manager, project_id)
    return wait(lambda: next(iter(exec_records.query(project_id, ["interrupted"])), None))


# ---- 第 1 条：运行中不显示 ----


def test_running_hides_suggestions_and_pushes_on_state_change(api):
    client, manager, project_id, _ = api
    FakeEngine.scripts = [[{"type": "assistant_message", "blocks": [{"kind": "text", "text": "在看资料"}]}]]
    client.post(f"{P}/{project_id}/conversations/default/messages", json={"text": "门店有几类？"})
    session_where(manager, project_id, lambda session: session.state == "running")
    sug = fl02(client, project_id)
    assert sug["mode"] == "running" and sug["primary"] is None and sug["secondary"] == [] and sug["rev"]
    wait(lambda: pushed(manager, project_id) and pushed(manager, project_id)[-1]["mode"] == "running")
    # 一轮结束：推回按流程算的那一份，版本号更大
    running_rev = pushed(manager, project_id)[-1]["rev"]
    session = manager.get(project_id)
    client.portal.call(session.engine.queue.put, result())
    idle(manager, project_id)
    last = wait(lambda: settled_push(manager, project_id))
    assert last["mode"] == "normal" and last["primary"] and last["rev"] > running_rev


# ---- 第 2 条：等你回答 ----


def test_pending_question_offers_answer_and_back_to_running_after_answer(api):
    client, manager, project_id, _ = api
    ask = {"questions": [{"question": "订单按什么口径统计？", "options": [{"label": "按天"}, {"label": "按周"}]}]}

    def raise_question(ctx):
        asyncio.get_running_loop().create_task(ctx.request_permission("AskUserQuestion", ask, None, None))

    FakeEngine.scripts = [[raise_question]]
    client.post(f"{P}/{project_id}/conversations/default/messages", json={"text": "开始"})
    session = session_where(manager, project_id, lambda found: found.pending_permissions)
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
    session_where(manager, project_id, lambda session: session.state == "error")
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
    assert sug["primary"]["label"] == "继续「提取操作」"
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
    assert sug["mode"] == "interrupted" and sug["primary"]["label"] == "继续「提取操作」"
    assert sug["note"].startswith("能力已升级，按新版从头做")
    sent_before = len(FakeEngine.sent)
    response = click(client, project_id, sug["primary"])
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["mode"] == "rerun" and body["replaced"]["id"] == record["id"]
    old = exec_records.get(project_id, record["id"])
    assert old["state"] == "abandoned" and old["replaced_by"] == body["exec"]["id"] != record["id"]
    assert body["exec"]["command_label"] == "提取操作"
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


# ---- 按流程（推荐第 2～5 级）----


def _suggest(root, states: dict, context: dict, check=None) -> dict:
    flow = model.load_for(root)
    module = model.flow_state(root)
    assert getattr(module, "SUGGEST_RULES", 0) >= 9
    return module.suggest(flow, states, context, check)


def _states(root, **overrides) -> dict:
    flow = model.load_for(root)
    out = {node["id"]: {"state": "confirmed", "flags": {}} for node in flow["nodes"]}
    out.update({key.replace("_", "."): {"state": value, "flags": {}} for key, value in overrides.items()})
    return out


def test_flow_rules_in_order(api):
    """推荐五级在项目装配好的流程上（每级的表驱动用例在 test_node_names_and_levels）：命中就停，一次只推一个节点。"""
    _, _, _, root = api
    fresh = _states(root, core_research="ready", core_object="not_ready", core_operation="not_ready",
                    core_validate="not_ready", core_publish="not_ready")
    # 第 3 级：第一个没做完的必需节点（资料分析的几个入口）
    mainline = _suggest(root, fresh, {"blueprint": {"state": "none"}})
    assert mainline["mode"] == "normal" and mainline["primary"]["id"].startswith("core.research#")
    assert mainline["note"] is None
    # 动作的条件 when：还没有资料分析报告时不出「追加资料分析」，有了才出
    no_report = _suggest(root, fresh, {"blueprint": {"state": "none"}, "probes": {"research_report": False}})
    assert [item["label"] for item in items(no_report)] == ["资料分析"]
    with_report = _suggest(root, fresh, {"blueprint": {"state": "none"}, "probes": {"research_report": True}})
    assert [item["label"] for item in items(with_report)] == ["资料分析", "追加资料分析"]
    # 第 1 级：合入冲突，命中就停（不再补主线下一步）
    conflict = _suggest(root, fresh, {"blueprint": {"state": "none"}, "changeset": {"id": "cs-1", "state": "conflict"}})
    assert conflict["primary"]["label"] == "处理冲突" and conflict["secondary"] == []
    # 第 1 级：目标问题没答，和「去回答」同级，命中就停；没有目标问题时照常推下一步
    modeling = _states(root, core_research="confirmed", core_object="ready", core_operation="not_ready")
    goal = _suggest(root, modeling, {"blueprint": {"state": "draft", "goal_pending": True}})
    assert [item["label"] for item in items(goal)] == ["回答目标问题"]
    draft = _suggest(root, modeling, {"blueprint": {"state": "draft"}})
    assert draft["primary"]["label"] == "提取对象"
    assert draft["primary"]["action"] == {"type": "send", "text": "/ontology-extract-object",
                                          "command": "/ontology-extract-object"}
    # 过期的节点：按钮叫节点名，哪类变了写在说明行。可选的（指标）后面的校验还会跑就交给校验，不推（ADR-0090）；
    # 必需节点都做完、没有能接手的核对节点时，在第 4 级推
    upstream = {"stale_upstream": {"core.metric": "对象、操作"}}
    for later in ("ready", "not_ready"):
        deferred = _suggest(root, _states(root, core_metric="stale", core_validate=later),
                            {"blueprint": {"state": "confirmed"}, **upstream})
        assert not any(item["id"].startswith("refresh:") for item in items(deferred))
    stale = _suggest(root, _states(root, core_metric="stale", core_validate="skipped"),
                     {"blueprint": {"state": "confirmed"}, **upstream})
    assert [item["label"] for item in items(stale)] == ["提取规则与指标"]
    assert stale["primary"]["action"] == {"type": "send", "text": "/ontology-extract-rule-metric",
                                          "command": "/ontology-extract-rule-metric"}
    assert stale["note"] == "对象、操作有变化"
    # 必需的节点（提取操作）过期、校验没跑过：第 2 级推它（新增对象可能要补提取操作，校验补不出来）
    required = _suggest(root, _states(root, core_operation="stale", core_validate="ready"),
                        {"blueprint": {"state": "confirmed"}, "stale_upstream": {"core.operation": "对象"}})
    assert (items(required)[0]["label"], required["note"]) == ("提取操作", "对象有变化")
    assert items(required)[0]["action"]["command"] == "/ontology-extract-operation"
    whole = _suggest(root, _states(root, core_validate="stale"), {"stale_upstream": {"core.validate": "对象、操作"}})
    assert (whole["primary"]["label"], whole["note"]) == ("校验", "对象、操作有变化")
    unknown = _suggest(root, _states(root, core_validate="stale"), {})
    assert (unknown["primary"]["label"], unknown["note"]) == ("校验", "前面的内容有变化")  # 老记录说不清哪类变了
    # 不推上游：后面已确认的校验能核对并修复同一批改动（stale_on 盖住、写入范围盖住）。资料变了（校验不看资料）照旧推
    # 提取对象；两个都过期时只推流程里靠前的那个
    checked = _suggest(root, _states(root, core_operation="stale", trace_trace="ready", core_publish="not_ready"),
                       {"stale_upstream": {"core.operation": "对象", "core.metric": "对象、操作"}})
    assert not any(item["id"].startswith("refresh:") for item in items(checked))
    assert checked["primary"]["id"].startswith("trace.trace")
    research = _suggest(root, _states(root, core_object="stale"), {"stale_upstream": {"core.object": "资料"}})
    assert research["primary"]["id"] == "refresh:core.object"
    both = _suggest(root, _states(root, core_operation="stale", core_validate="stale"),
                    {"stale_upstream": {"core.operation": "对象", "core.validate": "对象、操作"}})
    assert [item["id"] for item in items(both)] == ["refresh:core.operation"]
    # 范围限定生效（限定范围的包这一轮进行中）时不推过期的，传播交给那一轮的核对
    confined = _suggest(root, _states(root, core_operation="stale", core_validate="ready", core_publish="not_ready"),
                        {"confine_active": True})
    assert not any(item["id"].startswith("refresh:") for item in items(confined))
    assert confined["primary"]["id"].startswith("core.validate")
    # 命令停下来等你在对话里打字回复：不推别的命令（点了会把命令丢在半路），只留说明；有确认卡时照旧主推去回答
    waiting = _suggest(root, _states(root, core_operation="stale"),
                       {"awaiting_reply": {"id": "ex-1", "label": "校验"}})
    assert waiting["primary"] is None and waiting["secondary"] == []
    assert waiting["note"] == "「校验」在等你回复上面的问题"
    asked = _suggest(root, _states(root, core_operation="stale"),
                     {"awaiting_reply": {"id": "ex-1", "label": "校验"}, "question": {"id": "q1", "kind": "question"}})
    assert asked["mode"] == "waiting_decision"
    # 报告里还有待处理的问题：校验没完成，就是第 3 级的「校验」（不再有写死的「继续校验」）
    report = _suggest(root, _states(root, core_validate="ready", trace_trace="not_ready", core_publish="not_ready"),
                      {"stage": "校验待审阅"})
    assert [item["label"] for item in items(report)] == ["校验"]
    # 提取操作在跑时建议条为空；对象做完、操作还没做时推提取操作（上下文里老的 slice_next 不再起作用）
    running = _suggest(root, _states(root, core_operation="running"), {
        "blueprint": {"state": "confirmed"},
        "slice_next": {"slice_id": "s1", "slice_name": "补货", "step": "operation", "label": "提取操作",
                       "command": "/ontology-extract-operation", "sliced": True}})
    assert running["mode"] == "normal" and running["primary"] is None
    next_operation = _suggest(root, _states(root, core_operation="ready"), {"blueprint": {"state": "confirmed"}})
    assert next_operation["primary"]["label"] == "提取操作"
    # 全部做完：不显示（不再推写死的「查看整体视图」「扫描脚本候选」）
    done = _suggest(root, _states(root), {})
    assert done["primary"] is None and done["secondary"] == []
    # 能执行的检查：选中的节点此刻都不能执行时建议条为空，不越过它推别的
    blocked = _suggest(root, _states(root, core_metric="stale", core_validate="skipped"), {"stage": "校验待审阅"},
                       check=lambda action: action.get("command") != "/ontology-extract-rule-metric")
    assert blocked["primary"] is None


def test_handed_down_matches_rule_7(api):
    """待处理和建议条第 7 条用同一个判定（flow_state.handed_down）。

    校验已确认时它修对象让提取操作、指标过期，不要你刷新；资料变了（提取对象过期）、校验没跑过时要你刷新。
    老调用方传的按段参数不再起作用。"""
    _, _, _, root = api
    flow = model.load_for(root)
    module = model.flow_state(root)
    nodes = flow["nodes"]
    by_id = {node["id"]: node for node in nodes}

    def state_of(**overrides):
        return {node_id: item["state"] for node_id, item in _states(root, **overrides).items()}

    checked = state_of(core_operation="stale", core_metric="stale")
    assert module.handed_down(by_id["core.operation"], nodes, checked)
    assert module.handed_down(by_id["core.metric"], nodes, checked)
    assert not module.handed_down(by_id["core.object"], nodes, state_of(core_object="stale"))
    unchecked = state_of(core_operation="stale", core_metric="stale", core_validate="ready")
    assert not module.handed_down(by_id["core.operation"], nodes, unchecked)
    # 可选的（指标）：后面的校验还会跑就交给它
    assert module.handed_down(by_id["core.metric"], nodes, unchecked)
    old = [{"slice_id": "all", "step": "operation"}]
    assert module.handed_down(by_id["core.operation"], nodes, checked, old)
    assert not module.handed_down(by_id["core.operation"], nodes, unchecked, old)


def test_flow_rule_commands_are_accepted_by_the_api(api):
    """按流程给出的命令按建议条的方式发一次（S-20，带 from_suggestion），都收下（2xx）。"""
    client, manager, project_id, root = api
    fresh = _states(root, core_research="ready", core_object="not_ready", core_operation="not_ready",
                    core_validate="not_ready", core_publish="not_ready")
    produced = [
        _suggest(root, fresh, {"blueprint": {"state": "none"}, "changeset": {"id": "cs-1", "state": "conflict"}}),
        _suggest(root, _states(root, core_research="confirmed", core_object="ready", core_operation="not_ready"),
                 {"blueprint": {"state": "draft", "goal_pending": True}}),
        _suggest(root, _states(root, core_metric="stale", core_validate="skipped"),
                 {"stale_upstream": {"core.metric": "对象、操作"}}),
        _suggest(root, _states(root, core_operation="stale", core_validate="ready"),
                 {"stale_upstream": {"core.operation": "对象"}}),
        _suggest(root, _states(root, core_validate="ready", trace_trace="not_ready", core_publish="not_ready"),
                 {"stage": "校验待审阅"}),
        _suggest(root, fresh, {"blueprint": {"state": "none"}}),
        _suggest(root, _states(root, trace_trace="ready", core_publish="not_ready"), {}),
        _suggest(root, _states(root, diagram_flowchart="ready"), {}),
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
