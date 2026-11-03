"""F2 第 2、5 条：工作过程按时间顺序分段，子代理的每一步进对话流（session_host/timeline）。"""

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
    assert [step["text"] for step in before[-1]["data"]["steps"]] == ["读取「a.md」"]
    after = [group for kind, group in kinds[text_at:] if kind.startswith("progress")]
    assert after and all(group != first for group in after)
    second = [item for item in envelopes if item["type"].startswith("progress") and item["data"]["group_id"] == after[0]]
    assert second[0]["type"] == "progress"  # 新一段的第一条是 progress，不是 progress.updated
    assert [step["text"] for step in second[-1]["data"]["steps"]] == ["读取「b.md」"]
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


def test_jiuwen_subagent_hangs_under_its_dispatch_step_without_a_duplicate_step():
    """内测引擎先报子代理开始、后报带这段话与派发调用的消息：子代理挂在这段话后面那一段，不另加一步。"""
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
    ])
    texts = [item["data"]["text_md"] for item in envelopes if item["type"] == "message.assistant"]
    assert texts == [BODY]
    groups = [item["data"] for item in envelopes if item["type"].startswith("progress")]
    last = groups[-1]
    assert [step["text"] for step in last["steps"]] == ["派出子任务"]  # 没有再加一步「子任务「子任务」」
    subs = [item["data"] for item in envelopes if item["type"] == "subagent.updated"]
    assert {item["group_id"] for item in subs} == {last["group_id"]}
    assert subs[0]["status"] == "started" and subs[0]["step"] is None
    order = [item["type"] for item in envelopes]
    assert order.index("message.assistant") < order.index("subagent.updated")
    steps = [item["step"] for item in subs if item["step"]]
    assert steps[0] == {"id": "s-a", "kind": "tool", "text": "读取「临期与报损管理办法.md」", "state": "running"}
    assert steps[1]["text"] == "读取表格结构" and steps[1]["state"] == "running"
    # 收尾的事件不带参数，照样带着开始时的说法，不退成「读取资料」「运行脚本」
    assert steps[2] == {"id": "s-a", "kind": "tool", "text": "读取「临期与报损管理办法.md」", "state": "done"}
    assert steps[3]["text"] == "读取表格结构" and steps[3]["state"] == "failed"
    assert steps[4] == {"id": "call-9-say-3", "kind": "text", "text": "两份都读完了\n要点如下", "state": "done"}
    assert [item["progress_text"] for item in subs[1:6]] == [
        "读取「临期与报损管理办法.md」", "读取表格结构", "读取「临期与报损管理办法.md」", "读取表格结构", "两份都读完了"]
    assert subs[-1]["status"] == "done"


def test_subagent_reported_after_its_dispatch_step_attaches_to_that_step():
    """稳定引擎：派发调用先到、子代理开始后到。"""
    turn = timeline.Turn.new()
    envelopes = run(turn, [
        {"type": "assistant_message", "blocks": [tool("tu-2", "Task", description="语义检查")]},
        {"type": "subagent_started", "task_id": "task-1", "tool_use_id": "tu-2", "description": "语义检查"},
        {"type": "subagent_finished", "task_id": "task-1", "tool_use_id": "tu-2", "status": "completed",
         "summary": "没有问题"},
        done("tu-2"),
    ])
    groups = [item["data"] for item in envelopes if item["type"].startswith("progress")]
    assert [step["text"] for step in groups[-1]["steps"]] == ["派出子任务「语义检查」"]
    subs = [item["data"] for item in envelopes if item["type"] == "subagent.updated"]
    assert [item["status"] for item in subs] == ["started", "done"] and subs[-1]["summary"] == "没有问题"


def test_legacy_subagent_records_project_with_unknown_step_text():
    """F2 之前写进历史的记录没有步骤说法：收尾的那条不给说法，前端保留开始时的。"""
    records = [
        {"type": "subagent_activity", "task_id": "c1", "tool_use_id": "c1", "description": "子任务", "step_id": "s1",
         "kind": "tool", "tool": "Read", "input": {"file_path": "resource/a.md"}, "group_id": "g-1", "seq": 3},
        {"type": "subagent_activity", "task_id": "c1", "tool_use_id": "c1", "description": "子任务", "step_id": "s1",
         "kind": "tool", "tool": "Read", "status": "ok", "group_id": "g-1", "seq": 4},
    ]
    start, end = (timeline.project(record, "p1", "c1")["data"] for record in records)
    assert start["step"] == {"id": "s1", "kind": "tool", "text": "读取「a.md」", "state": "running"}
    assert end["step"] == {"id": "s1", "kind": "tool", "text": None, "state": "done"}
    assert end["progress_text"] == ""


def test_short_talk_between_tool_calls_goes_into_the_work_process():
    """F2 第 5 条（主控补充）：工具调用之间的过程话（「先做前置检查」）收进工作过程，正文只留成段的说明、提问和小结。"""
    turn = timeline.Turn.new("资料分析")
    envelopes = run(turn, [
        {"type": "stream_delta", "kind": "text", "text": "先做前置检查。"},
        {"type": "assistant_message", "blocks": [{"kind": "thinking", "text": "想一想"},
                                                 {"kind": "text", "text": "先做**前置检查**，再获取 `research` 的执行锁。"},
                                                 tool("t1", "Bash", description="检查命令锁")]},
        done("t1"),
        {"type": "assistant_message", "blocks": [{"kind": "text", "text": "资料目录里有：\n\n- a.md\n- b.md"},
                                                 tool("t2", "Read", file_path="resource/a.md")]},
        done("t2"),
        {"type": "assistant_message", "blocks": [{"kind": "text", "text": "需要你定一下口径。"},
                                                 tool("t3", "AskUserQuestion", questions=[])]},
        {"type": "assistant_message", "blocks": [{"kind": "text", "text": "读完了，小结如下：两份资料都讲补货。"}]},
    ])
    texts = [item["data"]["text_md"] for item in envelopes if item["type"] == "message.assistant"]
    # 成段的说明（带清单）、提问前的话、收尾小结留在正文；过程话不进正文
    assert texts == ["资料目录里有：\n\n- a.md\n- b.md", "需要你定一下口径。", "读完了，小结如下：两份资料都讲补货。"]
    first = [item["data"] for item in envelopes if item["type"].startswith("progress")][0]
    steps = [item["data"] for item in envelopes if item["type"].startswith("progress")
             and item["data"]["group_id"] == first["group_id"]][-1]["steps"]
    assert steps[0]["text"] == "先做前置检查，再获取 research 的执行锁。" and steps[0]["state"] == "info"
    assert steps[1]["text"] == "检查命令锁"
    # 下一段话的流式增量换了消息号（过程话那一段的增量不会接到正文上）
    deltas = [item for item in envelopes if item["type"] == "message.delta"]
    assert deltas and all(item["data"]["message_id"] for item in deltas)
    assert all(item["data"]["message_id"] != deltas[0]["data"]["message_id"]
               for item in envelopes if item["type"] == "message.assistant")


def test_narration_record_keeps_text_for_history_replay_but_is_not_projected():
    record = {"type": "assistant_message", "narration": True, "seq": 2,
              "blocks": [{"kind": "text", "text": "先做前置检查。"}]}
    assert timeline.project(record, "p1", "c1") is None
