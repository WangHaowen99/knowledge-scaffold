from modeling_agent.visible_text import (
    VisibleReasoningSanitizer,
    VisibleTextSanitizer,
    claims_platform_capability_gap,
    sanitize_visible_text,
)


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


def test_reasoning_sanitizer_removes_private_line_split_across_chunks() -> None:
    sanitizer = VisibleReasoningSanitizer()
    parts = [
        sanitizer.feed("先分析资料结构。\nsystem pro"),
        sanitizer.feed("mpt 与 api_key=top-secret 不展示。\narguments={\"path\":\"rules/demo.md\"}\n再核对字段。"),
        sanitizer.finish(),
    ]
    visible = "".join(parts)

    assert visible == "先分析资料结构。\n（内部配置已隐藏）\n（内部配置已隐藏）\n再核对字段。"
    assert "top-secret" not in visible
    assert "rules/demo.md" not in visible


def test_reasoning_sanitizer_hides_platform_capability_claims() -> None:
    sanitizer = VisibleReasoningSanitizer()
    visible = sanitizer.feed("工具集中没有该读取能力，请确认平台是否开放。") + sanitizer.finish()

    assert visible == "（内部配置已隐藏）"


def test_visible_text_hides_internal_http_and_https_links() -> None:
    http_link = "http" + "://localhost:5100/api/projects"
    https_link = "https" + "://example.com/api/projects"
    text = f"请查看 {http_link} 和 {https_link}。"

    visible = sanitize_visible_text(text)

    assert visible == "请查看 内部链接 和 内部链接。"


def test_visible_text_translates_ambiguous_tool_name_in_chinese_context() -> None:
    visible = sanitize_visible_text("请 focus 当前对象。")

    assert visible == "请 定位内容 当前对象。"
    assert sanitize_visible_text("keep focus unchanged.") == "keep focus unchanged."


def test_reasoning_sanitizer_hides_the_wording_from_the_stalled_repair_session() -> None:
    sanitizer = VisibleReasoningSanitizer()
    visible = "".join([
        sanitizer.feed("写入能力经二次确认仍未注册。"),
        sanitizer.feed("累计 6 次写入尝试均返回能力不可用，属于平台侧环境边界。"),
        sanitizer.feed("先读取当前操作的脚本索引。"),
        sanitizer.finish(),
    ])

    assert "未注册" not in visible
    assert "能力不可用" not in visible
    assert "平台侧环境边界" not in visible
    assert "先读取当前操作的脚本索引。" in visible


def test_capability_claim_gate_covers_the_wording_from_the_stalled_repair_session() -> None:
    assert claims_platform_capability_gap("写入能力经二次确认仍未注册，需等待平台开放写入能力后继续。")
    assert claims_platform_capability_gap("累计 6 次写入尝试均返回“能力不可用”。")
    assert claims_platform_capability_gap("这属于平台侧环境边界，不是内容问题。")
    assert claims_platform_capability_gap("工具集中没有该读取能力，请确认平台是否开放。")
    assert not claims_platform_capability_gap("修复内容已经写入候选，请在确认卡里核对差异。")


def test_capability_claim_gate_leaves_ordinary_business_wording_alone() -> None:
    # 「未注册」「不可用」「缺失」本身就是建模对象的正常业务表述。裸短语匹配会把
    # 业务正文误判成平台能力声明，整轮被强制纠偏，可见推理还被整段隐藏。
    assert not claims_platform_capability_gap("该企业尚未注册，按草稿状态处理。")
    assert not claims_platform_capability_gap("该流程存在能力缺失，需要补充岗位培训。")
    assert not claims_platform_capability_gap("客户要求对外开放写入接口，需在目标里登记。")
    assert not claims_platform_capability_gap("这批物料仍未注册到主数据，先走临时编码。")
    assert not claims_platform_capability_gap("该设备当前不可用，排产时要跳过。")


def test_capability_claim_gate_still_catches_the_claim_in_business_wording() -> None:
    assert claims_platform_capability_gap("受控写入能力仍未注册，无法生成候选。")
    assert claims_platform_capability_gap("落盘未开放，本轮修改无法提交。")
