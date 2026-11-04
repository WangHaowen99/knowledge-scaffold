"""F2 第 2、5 条：工作过程按时间顺序分段，子代理的每一步进对话流（session_host/timeline）。

助手的话一律进正文、派出的子任务单独成卡（不进工作过程组），与旧版对话页一致。"""

from __future__ import annotations

from app.services.session_host import timeline


def run(turn: timeline.Turn, events: list[dict]) -> list[dict]:
    """引擎事件 → 宿主记录（编上 seq）→ 对话流事件，与会话宿主写历史、投影的顺序一致。"""
    envelopes = []
    seq = 0
    for event in events:
        for record, transient in timeline.ingest(turn, event):
            record = {**record, "ts": 1_700_000_000.0 + seq}
            if not transient:
                record["seq"] = seq
                seq += 1
            envelope = timeline.project(record, "p1", "c1")
            if envelope:
                envelopes.append(envelope)
    return envelopes


# 成段的说明（带清单）：留在正文，前后的步骤分在两段
BODY = "资料有两份：\n\n- a.md\n- b.md\n\n下面逐份读。"


def tool(tool_use_id: str, name: str, **data) -> dict:
    return {"kind": "tool_use", "tool": name, "tool_use_id": tool_use_id, "input": data}


def done(tool_use_id: str, error: bool = False) -> dict:
    return {"type": "tool_results", "blocks": [{"kind": "tool_result", "tool_use_id": tool_use_id, "is_error": error}]}


def test_steps_after_assistant_text_start_a_new_segment_below_it():
    turn = timeline.Turn.new("资料分析")
    envelopes = run(turn, [
        {"type": "assistant_message", "blocks": [tool("t1", "Read", file_path="resource/a.md")]},
        done("t1"),
        {"type": "assistant_message", "blocks": [{"kind": "text", "text": BODY},
                                                 tool("t2", "Read", file_path="resource/b.md")]},
        done("t2"),
    ])
    kinds = [(item["type"], item["data"].get("group_id")) for item in envelopes if item["type"] != "tool.activity"]
    first = kinds[0][1]
    text_at = next(index for index, (kind, _) in enumerate(kinds) if kind == "message.assistant")
    # 第一段在这段话之前收起（已完成、折叠）；后面的步骤另起一段，排在这段话后面
    before = [item for item in envelopes[:[e["type"] for e in envelopes].index("message.assistant")]
              if item["type"].startswith("progress")]
    assert before[-1]["data"]["group_id"] == first
    assert before[-1]["data"]["state"] == "done" and before[-1]["data"]["collapsed"] is True
    assert [step["text"] for step in before[-1]["data"]["steps"]] == ["读取资料「a」"]
    after = [group for kind, group in kinds[text_at:] if kind.startswith("progress")]
    assert after and all(group != first for group in after)
    second = [item for item in envelopes if item["type"].startswith("progress") and item["data"]["group_id"] == after[0]]
    assert second[0]["type"] == "progress"  # 新一段的第一条是 progress，不是 progress.updated
    assert [step["text"] for step in second[-1]["data"]["steps"]] == ["读取资料「b」"]
    assert second[-1]["data"]["state"] == "running"


def test_text_without_steps_before_it_does_not_add_an_empty_segment():
    turn = timeline.Turn.new()
    envelopes = run(turn, [
        {"type": "assistant_message", "blocks": [{"kind": "text", "text": "先说一句"}]},
        {"type": "assistant_message", "blocks": [{"kind": "text", "text": "再说一句"}, tool("t1", "Bash", description="列出资料")]},
    ])
    groups = {item["data"]["group_id"] for item in envelopes if item["type"].startswith("progress")}
    assert len(groups) == 1
    assert all(item["data"]["steps"] for item in envelopes if item["type"].startswith("progress"))


def test_jiuwen_subagent_card_follows_the_dispatch_message_and_takes_no_step():
    """内测引擎先报子代理开始、后报带这段话与派发调用的消息：子任务卡排在这段话后面，工作过程里不加「派出子任务」。"""
    turn = timeline.Turn.new()
    sub = {"task_id": "call-9", "tool_use_id": "call-9", "description": "子任务", "agent_type": "general-purpose"}
    envelopes = run(turn, [
        {"type": "assistant_message", "blocks": [tool("t1", "Bash", description="列出资料")]},
        done("t1"),
        {"type": "subagent_started", **sub},
        {"type": "assistant_message", "blocks": [{"kind": "text", "text": BODY}, tool("call-9", "Task")]},
        {"type": "subagent_activity", **sub, "step_id": "s-a", "kind": "tool", "tool": "Read",
         "input": {"file_path": "resource/临期与报损管理办法.md"}},
        {"type": "subagent_activity", **sub, "step_id": "s-b", "kind": "tool", "tool": "Bash",
         "input": {"command": "python3 x.py", "description": "读取表格结构"}},
        {"type": "subagent_activity", **sub, "step_id": "s-a", "kind": "tool", "tool": "Read", "status": "ok"},
        {"type": "subagent_activity", **sub, "step_id": "s-b", "kind": "tool", "tool": "Bash", "status": "error"},
        {"type": "subagent_activity", **sub, "step_id": "call-9-say-3", "kind": "text", "text": "两份都读完了\n要点如下"},
        {"type": "subagent_finished", **sub, "status": "completed", "usage": {"duration_ms": 4000}},
        done("call-9"),
        {"type": "assistant_message", "blocks": [tool("t2", "Read", file_path="resource/b.md")]},
    ])
    texts = [item["data"]["text_md"] for item in envelopes if item["type"] == "message.assistant"]
    assert texts == [BODY]
    groups = [item["data"] for item in envelopes if item["type"].startswith("progress")]
    assert all("派出子任务" not in step["text"] for group in groups for step in group["steps"])
    order = [item["type"] for item in envelopes]
    first_sub = order.index("subagent.updated")
    assert order.index("message.assistant") < first_sub
    # 子任务之后的步骤另起一段，排在子任务卡后面
    later = [item["data"] for item in envelopes[first_sub:] if item["type"].startswith("progress")]
    assert later and later[0]["group_id"] != groups[0]["group_id"]
    assert [step["text"] for step in later[-1]["steps"]] == ["读取资料「b」"]
    subs = [item["data"] for item in envelopes if item["type"] == "subagent.updated"]
    assert subs[0]["status"] == "started" and subs[0]["step"] is None
    steps = [item["step"] for item in subs if item["step"]]
    assert steps[0] == {"id": "s-a", "kind": "tool", "text": "读取资料「临期与报损管理办法」", "state": "running"}
    assert steps[1]["text"] == "读取表格结构" and steps[1]["state"] == "running"
    # 收尾的事件不带参数，照样带着开始时的说法，不退成「读取资料」「运行脚本」
    assert steps[2] == {"id": "s-a", "kind": "tool", "text": "读取资料「临期与报损管理办法」", "state": "done"}
    assert steps[3]["text"] == "读取表格结构" and steps[3]["state"] == "failed"
    assert steps[4] == {"id": "call-9-say-3", "kind": "text", "text": "两份都读完了\n要点如下", "state": "done"}
    assert [item["progress_text"] for item in subs[1:6]] == [
        "读取资料「临期与报损管理办法」", "读取表格结构", "读取资料「临期与报损管理办法」", "读取表格结构", "两份都读完了"]
    assert subs[-1]["status"] == "done"


def test_subagent_reported_after_its_dispatch_call_is_sent_at_once():
    """稳定引擎：派发调用先到、子代理开始后到；只派子任务的一条消息不出空的工作过程组。"""
    turn = timeline.Turn.new()
    envelopes = run(turn, [
        {"type": "assistant_message", "blocks": [tool("tu-2", "Task", description="语义检查")]},
        {"type": "subagent_started", "task_id": "task-1", "tool_use_id": "tu-2", "description": "语义检查"},
        {"type": "subagent_finished", "task_id": "task-1", "tool_use_id": "tu-2", "status": "completed",
         "summary": "没有问题"},
        done("tu-2"),
    ])
    assert not [item for item in envelopes if item["type"].startswith("progress")]
    subs = [item["data"] for item in envelopes if item["type"] == "subagent.updated"]
    assert [item["status"] for item in subs] == ["started", "done"] and subs[-1]["summary"] == "没有问题"
    assert subs[0]["title"] == "语义检查"


def test_legacy_subagent_records_project_with_unknown_step_text():
    """F2 之前写进历史的记录没有步骤说法：收尾的那条不给说法，前端保留开始时的。"""
    records = [
        {"type": "subagent_activity", "task_id": "c1", "tool_use_id": "c1", "description": "子任务", "step_id": "s1",
         "kind": "tool", "tool": "Read", "input": {"file_path": "resource/a.md"}, "group_id": "g-1", "seq": 3},
        {"type": "subagent_activity", "task_id": "c1", "tool_use_id": "c1", "description": "子任务", "step_id": "s1",
         "kind": "tool", "tool": "Read", "status": "ok", "group_id": "g-1", "seq": 4},
    ]
    start, end = (timeline.project(record, "p1", "c1")["data"] for record in records)
    assert start["step"] == {"id": "s1", "kind": "tool", "text": "读取资料「a」", "state": "running"}
    assert end["step"] == {"id": "s1", "kind": "tool", "text": None, "state": "done"}
    assert end["progress_text"] == ""


def test_talk_between_tool_calls_is_printed_in_the_body():
    """工具调用之间的话（「先做前置检查」）照常进正文，前面做完的步骤收成一段，与旧版对话页一致。"""
    turn = timeline.Turn.new("资料分析")
    envelopes = run(turn, [
        {"type": "stream_delta", "kind": "text", "text": "先做前置检查。"},
        {"type": "assistant_message", "blocks": [{"kind": "thinking", "text": "想一想"},
                                                 {"kind": "text", "text": "先做前置检查。"},
                                                 tool("t1", "Bash", description="检查命令锁")]},
        done("t1"),
        {"type": "assistant_message", "blocks": [{"kind": "text", "text": "再读资料。"},
                                                 tool("t2", "Read", file_path="resource/a.md")]},
        done("t2"),
    ])
    texts = [item["data"]["text_md"] for item in envelopes if item["type"] == "message.assistant"]
    assert texts == ["先做前置检查。", "再读资料。"]
    groups = [item["data"] for item in envelopes if item["type"].startswith("progress")]
    assert all(step["state"] != "info" for group in groups for step in group["steps"])
    assert len({group["group_id"] for group in groups}) == 2
    # 流式片段和整条消息是同一个消息号，前端原位收尾
    delta = next(item for item in envelopes if item["type"] == "message.delta")
    first = next(item for item in envelopes if item["type"] == "message.assistant")
    assert delta["data"]["message_id"] == first["data"]["message_id"]


def test_legacy_narration_record_is_not_projected():
    """旧版收进工作过程的短话：那一步已经在组里，回放时不再出正文。"""
    record = {"type": "assistant_message", "narration": True, "seq": 2,
              "blocks": [{"kind": "text", "text": "先做前置检查。"}]}
    assert timeline.project(record, "p1", "c1") is None


def test_subagent_text_step_drops_markdown_marks():
    """真实模型：子代理最后的回答以「## 资料要点汇总」开头，工作过程里不显示 Markdown 记号。"""
    record = {"type": "subagent_activity", "task_id": "c1", "tool_use_id": "c1", "description": "子任务", "step_id": "c1-say",
              "kind": "text", "text": "## 资料要点汇总\n\n---\n- **门店补货作业规范**：`docx`，规定补货流程", "group_id": "g-1", "seq": 5}
    data = timeline.project(record, "p1", "c1")["data"]
    assert data["step"]["text"] == "资料要点汇总\n门店补货作业规范：docx，规定补货流程"
    assert data["progress_text"] == "资料要点汇总"


# ---- F6：子任务卡的名称、任务说明、思考中与稳定引擎子代理的步骤 ----


def test_subagent_title_falls_back_to_brief_first_line_then_type():
    assert timeline.subagent_title("语义检查", "# 你是检查代理\n...", "general-purpose") == "语义检查"
    line = "检查三个合成操作的字段、对象引用与规则引用是否一致，逐条给出问题清单、所在步骤和修改建议"
    assert len(line) > 40
    assert timeline.subagent_title("", f"\n## {line}\n第二行") == line[:40] + "…"
    assert timeline.subagent_title("", "- 列出问题\n第二行") == "列出问题"
    assert timeline.subagent_title("", "", "rule-check-agent") == "rule-check-agent"
    assert timeline.subagent_title("", None, "general-purpose") == "子任务"


def test_thinking_is_transient_and_keeps_the_card_title():
    turn = timeline.Turn.new()
    sub = {"task_id": "call-9", "tool_use_id": "call-9", "description": "", "agent_type": "general-purpose"}
    records = []
    for event in [
        {"type": "subagent_started", **sub, "brief": "检查合成操作\n细节……"},
        {"type": "subagent_activity", **sub, "step_id": "s-a", "kind": "tool", "tool": "Read",
         "input": {"file_path": "resource/a.md"}},
        {"type": "subagent_activity", **sub, "step_id": "call-9-thinking", "kind": "thinking",
         "since": 1_700_000_000.0, "elapsed_ms": 15000},
    ]:
        records += timeline.ingest(turn, event)
    started, step, thinking = records
    assert started[1] is False and step[1] is False
    assert thinking[1] is True  # 瞬时：只刷新界面，不进历史
    envelopes = [timeline.project({**record, "ts": 1_700_000_001.0}, "p1", "c1") for record, _ in records]
    first, second, third = (item["data"] for item in envelopes)
    assert first["brief"] == "检查合成操作\n细节……" and second["brief"] is None
    assert {first["title"], second["title"], third["title"]} == {"检查合成操作"}
    assert second["step_count"] == 1 and third["step_count"] == 1
    assert third["thinking"] == {"since": timeline.iso(1_700_000_000.0), "elapsed_ms": 15000}
    assert third["step"] is None and third["progress_text"] == "" and first["thinking"] is None


def test_claude_subagent_messages_become_steps_of_its_card():
    """稳定引擎：子代理自己的消息带派发调用 ID，工具调用和说的话记成子任务卡的步骤，任务说明取 Task 调用的 prompt。"""
    turn = timeline.Turn.new()
    envelopes = run(turn, [
        {"type": "assistant_message", "blocks": [tool("tu-2", "Task", description="语义检查",
                                                      prompt="检查三个操作的字段\n逐条列出问题", subagent_type="x")]},
        {"type": "subagent_started", "task_id": "task-1", "tool_use_id": "tu-2", "description": "语义检查",
         "agent_type": "x"},
        {"type": "assistant_message", "subagent": True, "parent_tool_use_id": "tu-2",
         "blocks": [{"kind": "thinking", "text": "想一想"}, {"kind": "text", "text": "先读对象"},
                    tool("s-1", "Read", file_path="ontology/object/合成对象.md", content="不进历史" * 50)]},
        done("s-1"),
        {"type": "assistant_message", "subagent": True, "parent_tool_use_id": "tu-2",
         "blocks": [tool("s-2", "Grep", pattern="合成", path="ontology/")]},
        done("s-2", error=True),
        {"type": "subagent_finished", "task_id": "task-1", "tool_use_id": "tu-2", "status": "completed",
         "summary": "发现 2 个问题"},
        done("tu-2"),
    ])
    subs = [item["data"] for item in envelopes if item["type"] == "subagent.updated"]
    assert subs[0]["brief"] == "检查三个操作的字段\n逐条列出问题" and subs[0]["title"] == "语义检查"
    steps = [item["step"] for item in subs if item["step"]]
    assert [(step["kind"], step["state"]) for step in steps] == [
        ("text", "done"), ("tool", "running"), ("tool", "done"), ("tool", "running"), ("tool", "failed")]
    assert steps[1]["text"] == steps[2]["text"] and "合成对象" in steps[1]["text"]
    assert subs[-1]["status"] == "done" and subs[-1]["step_count"] == 2 and subs[-1]["summary"] == "发现 2 个问题"
    # 子代理的工具调用不进主工作过程，也不把参数原文写进历史
    assert not [item for item in envelopes if item["type"].startswith("progress")]
    assert "不进历史" not in str([item for item in envelopes if item["type"] == "subagent.updated"])


def test_claude_subagent_steps_are_capped():
    turn = timeline.Turn.new()
    events = [{"type": "assistant_message", "blocks": [tool("tu-2", "Task", description="长任务")]},
              {"type": "subagent_started", "task_id": "task-1", "tool_use_id": "tu-2", "description": "长任务"}]
    for index in range(timeline.SUBAGENT_STEPS_MAX + 5):
        events.append({"type": "assistant_message", "parent_tool_use_id": "tu-2",
                       "blocks": [tool(f"s-{index}", "Read", file_path=f"resource/{index}.md")]})
    subs = [item["data"] for item in run(turn, events) if item["type"] == "subagent.updated"]
    steps = [item["step"] for item in subs if item["step"]]
    assert len(steps) == timeline.SUBAGENT_STEPS_MAX + 1
    assert steps[-1]["kind"] == "text" and "省略" in steps[-1]["text"]


def test_history_keeps_one_subagent_card_with_its_latest_state():
    turn = timeline.Turn.new()
    sub = {"task_id": "call-9", "tool_use_id": "call-9", "description": "规则检查", "agent_type": "general-purpose"}
    envelopes = run(turn, [
        {"type": "subagent_started", **sub, "brief": "任务说明"},
        {"type": "assistant_message", "blocks": [tool("call-9", "Task")]},
        {"type": "subagent_activity", **sub, "step_id": "s-a", "kind": "tool", "tool": "Read",
         "input": {"file_path": "resource/a.md"}},
        {"type": "subagent_activity", **sub, "step_id": "s-a", "kind": "tool", "tool": "Read", "status": "ok"},
        {"type": "subagent_finished", **sub, "status": "completed"},
    ])
    collapsed = timeline.collapse(envelopes)
    subs = [item for item in collapsed if item["type"] == "subagent.updated"]
    assert len(subs) == 1
    first = next(item for item in envelopes if item["type"] == "subagent.updated")
    assert subs[0]["id"] == first["id"]  # 放在第一次出现的位置
    data = subs[0]["data"]
    assert data["status"] == "done" and data["brief"] == "任务说明" and data["step_count"] == 1
    assert data["progress_text"] == "读取资料「a」"
