from modeling_agent.visible_text import VisibleTextSanitizer, sanitize_visible_text


def test_visible_text_hides_runtime_implementation_but_keeps_business_values() -> None:
    text = (
        "评测轮次 rd_64bea577-467 已完成，基于修订 588743e，"
        "request_release 后进入 evaluation:running；不要显示 get_project_state、agent_project_workflow_state "
        "或 discard_rejected_candidate_changes，"
        "当前内部阶段是 increment:idle，"
        "knowledge_extractor 不应向 consumer 暴露，"
        "或 session:pj_demo:run:123、mv_shadow_pj_796c0f3e_156。"
        "结果 5/5，版本 v1.0.0，业务编号 PIC11401。"
    )

    visible = sanitize_visible_text(text)

    assert "rd_64bea577-467" not in visible
    assert "588743e" not in visible
    assert "request_release" not in visible
    assert "evaluation:running" not in visible
    assert "get_project_state" not in visible
    assert "agent_project_workflow_state" not in visible
    assert "discard_rejected_candidate_changes" not in visible
    assert "increment:idle" not in visible
    assert "knowledge_extractor" not in visible
    assert "consumer" not in visible
    assert "session:pj_demo:run:123" not in visible
    assert "mv_shadow_pj_796c0f3e_156" not in visible
    assert "本次评测" in visible
    assert "当前版本" in visible
    assert "发布确认" in visible
    assert "放弃已退回的修改" in visible
    assert "正式评测" in visible
    assert "增量建模" in visible
    assert "知识分析" in visible
    assert "下游应用" in visible
    assert "5/5" in visible
    assert "v1.0.0" in visible
    assert "PIC11401" in visible


def test_streaming_sanitizer_handles_internal_token_split_across_chunks() -> None:
    sanitizer = VisibleTextSanitizer()

    parts = [sanitizer.feed(part) for part in ["结果：rd_64", "bea577-467，修订 ", "5887", "43e。"]]
    parts.append(sanitizer.finish())
    visible = "".join(parts)

    assert visible == "结果：本次评测，修订 当前版本。"
    assert parts[0] == "结果："
