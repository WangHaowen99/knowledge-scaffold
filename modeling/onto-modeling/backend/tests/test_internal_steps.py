"""工作过程不露内部动作（端到端验证）。

命令锁、进度这类平台内部步骤不进工作过程；模型写的英文描述不直接给用户看，
按脚本或子代理给中文说法。
"""

from __future__ import annotations

from app.services.session_host import timeline


def _bash(command: str, description: str) -> dict:
    return {"command": command, "description": description}


def test_internal_steps_are_recognised():
    assert timeline.internal_step("Bash", _bash("python3 .claude/scripts/state_helper.py acquire research start",
                                                "Acquire research start lock"))
    assert timeline.internal_step("Bash", _bash("cat .ontology-state/progress.json", "Read progress"))
    assert timeline.internal_step("Read", {"file_path": ".claude/agents/research-agent.md"})
    assert timeline.internal_step("Read", {"file_path": "/data/ws/p1/.claude/commands/ontology-validate.md"})
    assert not timeline.internal_step("Read", {"file_path": "research/资料分析.md"})
    assert not timeline.internal_step("Bash", _bash("python3 .claude/scripts/ontology_checker.py", "Run checker"))


def test_step_text_says_scripts_in_chinese_and_drops_english_descriptions():
    assert timeline.step_text("Bash", _bash("python3 .claude/scripts/research_checker.py research/x.md",
                                            "Run research checker")) == "核验资料分析报告"
    assert timeline.step_text("Bash", _bash("python3 .claude/scripts/knowledge_helper.py load --scope global",
                                            "Load global business knowledge")) == "加载业务知识"
    assert timeline.step_text("Bash", _bash("git diff --stat", "Show candidate diff")) == "运行脚本"
    assert timeline.step_text("Bash", _bash("python3 x.py", "抽取操作")) == "抽取操作"
    assert timeline.step_text("Task", {"description": "Conduct domain research"}) == "派出子任务"
    assert timeline.step_text("Task", {"description": "语义检查"}) == "派出子任务「语义检查」"


def test_turn_leaves_internal_steps_out_of_the_work_process():
    turn = timeline.Turn.new("资料分析")
    blocks = [
        {"kind": "tool_use", "tool": "Bash", "tool_use_id": "t1",
         "input": _bash("python3 .claude/scripts/state_helper.py check-lock research", "Check research lock")},
        {"kind": "tool_use", "tool": "Read", "tool_use_id": "t2", "input": {"file_path": ".claude/agents/research-agent.md"}},
        {"kind": "tool_use", "tool": "Read", "tool_use_id": "t3", "input": {"file_path": "research/资料分析.md"}},
    ]
    timeline.ingest(turn, {"type": "assistant_message", "blocks": blocks})
    records = timeline.ingest(turn, {"type": "tool_results", "blocks": [
        {"kind": "tool_result", "tool_use_id": key, "is_error": False} for key in ("t1", "t2", "t3")]})
    assert [step["text"] for step in turn.steps] == ["读取资料分析报告"]
    assert turn.steps[0]["state"] == "done"
    assert any(record.get("type") == "progress" for record, _ in records)


def test_subagent_internal_steps_and_english_titles():
    turn = timeline.Turn.new("资料分析")
    brief = "你是一名领域资料分析 agent。请完整阅读 .claude/agents/research-agent.md，严格按其中的规则执行。"
    timeline.ingest(turn, {"type": "assistant_message", "blocks": [
        {"kind": "tool_use", "tool": "Task", "tool_use_id": "d1",
         "input": {"description": "Conduct domain research", "prompt": brief}}]})
    started = timeline.ingest(turn, {"type": "subagent_started", "task_id": "s1", "tool_use_id": "d1",
                                     "description": "Conduct domain research"})
    assert started[0][0]["title"] == "资料分析"
    hidden = timeline.ingest(turn, {"type": "subagent_activity", "task_id": "s1", "tool_use_id": "d1", "step_id": "a",
                                    "kind": "tool", "tool": "Bash",
                                    "input": _bash("python3 .claude/scripts/state_helper.py acquire research analyze",
                                                   "Acquire lock")})
    done = timeline.ingest(turn, {"type": "subagent_activity", "task_id": "s1", "tool_use_id": "d1", "step_id": "a",
                                  "kind": "tool", "status": "ok"})
    shown = timeline.ingest(turn, {"type": "subagent_activity", "task_id": "s1", "tool_use_id": "d1", "step_id": "b",
                                   "kind": "tool", "tool": "Bash",
                                   "input": _bash("python3 .claude/scripts/source_extract.py --sources", "List sources")})
    assert hidden == [] and done == []
    assert shown[0][0]["step_text"] == "读取资料" and shown[0][0]["step_count"] == 1


def test_subagent_title_keeps_chinese_descriptions():
    assert timeline.subagent_title("语义检查") == "语义检查"
    assert timeline.subagent_title("Check object coverage", "Read .claude/agents/coverage-check-agent.md") == "覆盖检查"
    assert timeline.subagent_title("Do things", "Some english brief") == "子任务"
    assert timeline.subagent_title("", "检查三个操作的口径") == "检查三个操作的口径"


_SEMANTIC_READ = "请先完整阅读 .claude/agents/semantic-check-agent.md，严格按其中的规则与输出格式执行。\n"


def test_semantic_check_parts_get_their_own_titles():
    """全量校验同时派三个语义检查子任务：三张卡片按 PART 取名才分得开，模型写的描述（中英文）都不顶替。"""
    dir_line = "ISSUES_DIR: .ontology-state/semantic/20260101-000000-abcd\n"
    assert timeline.subagent_title("Semantic check", _SEMANTIC_READ + "PART: operation\n" + dir_line) == "语义检查 · 操作"
    assert timeline.subagent_title("语义检查（对象）", _SEMANTIC_READ + "PART: object\n" + dir_line) == "语义检查 · 对象"
    assert timeline.subagent_title("", _SEMANTIC_READ + "PART：cross\n" + dir_line) == "语义检查 · 跨文档"
    assert timeline.subagent_title(None, "PART: object\n", "semantic-check-agent") == "语义检查 · 对象"
    # 不拆的（只查改动、回归）和没写 PART 的照旧
    assert timeline.subagent_title("Semantic check", _SEMANTIC_READ + "PART: all\n" + dir_line) == "语义检查"
    assert timeline.subagent_title("Semantic check", _SEMANTIC_READ + dir_line) == "语义检查"
    assert timeline.subagent_title("语义检查（只查改动）", _SEMANTIC_READ + "PART: all\n") == "语义检查（只查改动）"
    # 别的子代理说明里出现 PART 不算；正文中间提到 PART 取值也不算
    assert timeline.subagent_title("Fix", "请先完整阅读 .claude/agents/semantic-fix-agent.md。\nPART: operation\n") == "语义修复"
    assert timeline.subagent_title("Rule check", "Read .claude/agents/rule-check-agent.md\nPART: object\n") == "规则检查"
    assert timeline.subagent_title("Semantic check", _SEMANTIC_READ + "说明：PART: cross 只在全量时用\n") == "语义检查"


def test_semantic_check_recording_shows_as_progress():
    """语义检查子任务边查边记：记问题、查完一项给用户看（按检查项说）；主命令建目录、看缺项、合并不进工作过程。"""
    run = ".ontology-state/semantic/20260101-000000-abcd"
    script = "python3 .claude/scripts/semantic_issues.py"
    add = f"{script} add --dir {run} --part operation --check 兜底完整性 <<'EOF'\n{{}}\nEOF"
    done = f'{script} done --dir {run} --part operation --check "Data/Operation 内容边界与口径一致"'
    whole = f"{script} done --dir {run} --part all"
    assert not timeline.internal_step("Bash", _bash(add, "Record issues"))
    assert not timeline.internal_step("Bash", _bash(done, "Mark done"))
    assert timeline.step_text("Bash", _bash(add, "Record issues")) == "记录「兜底完整性」的问题"
    assert timeline.step_text("Bash", _bash(done, "Mark done")) == "查完「Data/Operation 内容边界与口径一致」"
    assert timeline.step_text("Bash", _bash(whole, "Mark done")) == "检查做完"
    for sub in ("init --mode full", f"status --dir {run} --parts operation,object,cross", f"merge --dir {run}"):
        assert timeline.internal_step("Bash", _bash(f"{script} {sub}", "x"))


def test_step_duration_left_blank_under_a_second():
    """工具本身多在几十到几百毫秒，时间花在步骤之间的模型思考上：不满 1 秒不写耗时，免得满屏「0 秒」。"""
    assert timeline._duration_text(120) == ""
    assert timeline._duration_text(999) == ""
    assert timeline._duration_text(1000) == "1 秒"
    assert timeline._duration_text(75_000) == "1 分 15 秒"
    assert timeline._duration_text(None) == "进行中"
