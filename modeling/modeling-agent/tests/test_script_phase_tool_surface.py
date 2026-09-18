from modeling_agent.tools import (
    BUSINESS_PHASE_EDGES,
    DOMAIN_CHANGE_CONTROL_TOOLS,
    DOMAIN_PHASE_TOOLS,
    DOMAIN_READ_TOOLS,
    EVALUATION_PHASE_TOOLS,
    FUSION_PHASE_TOOLS,
    OBJECT_RELATION_REVIEW_TOOLS,
    SCRIPT_REPAIR_TOOLS,
    core_tool_specs_for_phase,
    phase_capability_host,
    phase_capability_hosts,
    phase_capability_specs,
)

CONTROLLED_CAPABILITY_TOOLS = (
    DOMAIN_CHANGE_CONTROL_TOOLS
    | OBJECT_RELATION_REVIEW_TOOLS
    | SCRIPT_REPAIR_TOOLS
    | FUSION_PHASE_TOOLS
    | EVALUATION_PHASE_TOOLS
)


def test_validation_phase_can_read_operation_snapshot_without_domain_write_tools() -> None:
    validation = {spec.name for spec in core_tool_specs_for_phase("validation")}

    assert {
        "list_domain_resources", "get_domain_resource", "define_script_contract",
        "get_script_edit_context", "stage_script_patch", "run_script_test",
    } <= validation
    assert DOMAIN_READ_TOOLS <= validation
    assert not (DOMAIN_PHASE_TOOLS - DOMAIN_READ_TOOLS) & validation


def test_evaluation_phase_keeps_domain_read_tools_for_failure_analysis() -> None:
    evaluation = {spec.name for spec in core_tool_specs_for_phase("evaluation")}

    assert DOMAIN_READ_TOOLS <= evaluation
    assert {"run_evaluation", "get_evaluation_results"} <= evaluation
    # 只读放开，写入仍然留在各自阶段。
    assert not (DOMAIN_PHASE_TOOLS - DOMAIN_READ_TOOLS) & evaluation
    assert not SCRIPT_REPAIR_TOOLS & evaluation


def test_evaluation_phase_answers_repair_writes_with_a_phase_redirect() -> None:
    hosts = {
        spec.name: host_phase
        for spec, host_phase in phase_capability_specs("evaluation")
    }

    # 评测失败后的修复回合落在评测阶段上，两条受控写入链路都必须有确定性出口，
    # 否则模型只会撞上未注册工具，再把阶段边界讲成平台缺少写入能力。
    assert SCRIPT_REPAIR_TOOLS <= set(hosts)
    assert DOMAIN_CHANGE_CONTROL_TOOLS <= set(hosts)
    assert hosts["stage_script_patch"] == "validation"
    assert hosts["stage_change_set"] == "knowledge"


def test_knowledge_and_validation_phases_redirect_only_the_missing_chain() -> None:
    knowledge = {spec.name for spec, _ in phase_capability_specs("knowledge")}
    validation = {spec.name for spec, _ in phase_capability_specs("validation")}

    # 每个阶段只为「本阶段没有、但确实由别的阶段承载」的受控能力注册重定向：
    # 手上有的链路不能被影子工具挡住，手上没有的也不能静默消失。
    assert SCRIPT_REPAIR_TOOLS <= knowledge
    assert not DOMAIN_CHANGE_CONTROL_TOOLS & knowledge
    assert DOMAIN_CHANGE_CONTROL_TOOLS <= validation
    assert not SCRIPT_REPAIR_TOOLS & validation
    assert knowledge <= CONTROLLED_CAPABILITY_TOOLS
    assert validation <= CONTROLLED_CAPABILITY_TOOLS


def test_every_repair_phase_redirects_the_controlled_chains_it_lacks() -> None:
    # 修复回合可能落在建模主链路的任一后段阶段上。任何一个阶段漏掉一条链路，
    # 模型在那里就会重新撞上未注册工具，再把阶段边界讲成平台缺少写入能力。
    for phase in ("knowledge", "increment", "validation", "evaluation", "release"):
        available = {spec.name for spec in core_tool_specs_for_phase(phase)}
        redirected = {spec.name for spec, _ in phase_capability_specs(phase)}
        missing = {
            name for name in CONTROLLED_CAPABILITY_TOOLS
            if name not in available and phase not in phase_capability_hosts(name)
        }
        assert missing <= redirected, phase


def test_phase_redirects_never_shadow_an_available_tool() -> None:
    for phase in ("intake", "goal", "process", "knowledge", "increment", "validation", "evaluation", "release"):
        available = {spec.name for spec in core_tool_specs_for_phase(phase)}
        redirected = {spec.name for spec, _ in phase_capability_specs(phase)}
        assert not available & redirected, phase


def test_recovery_required_tools_keep_the_real_tool_instead_of_a_redirect() -> None:
    required = frozenset({"stage_script_patch"})

    available = {
        spec.name for spec in core_tool_specs_for_phase("evaluation", required)
    }
    redirected = {
        spec.name for spec, _ in phase_capability_specs("evaluation", required)
    }

    assert "stage_script_patch" in available
    assert "stage_script_patch" not in redirected


def test_phase_capability_host_only_covers_controlled_write_chains() -> None:
    assert phase_capability_host("stage_script_patch") == "validation"
    assert phase_capability_host("create_change_plan") == "knowledge"
    assert phase_capability_host("write_file") == ""
    assert phase_capability_hosts("write_file") == ()


def test_object_relation_review_keeps_a_phase_exit_where_it_is_not_carried() -> None:
    # 关系审阅在 apply=true 时会写工作版本，属于受控写入。确定性验证和评测阶段按只读
    # 保留它，发布阶段不承载：那里必须给出承载阶段，否则又只剩「工具不存在」这一个解释。
    for phase in ("knowledge", "increment", "validation", "evaluation"):
        available = {spec.name for spec in core_tool_specs_for_phase(phase)}
        assert OBJECT_RELATION_REVIEW_TOOLS <= available, phase

    release = {spec.name for spec in core_tool_specs_for_phase("release")}
    redirected = {
        spec.name: host_phase for spec, host_phase in phase_capability_specs("release")
    }

    assert not OBJECT_RELATION_REVIEW_TOOLS & release
    assert OBJECT_RELATION_REVIEW_TOOLS <= set(redirected)
    # 承载阶段要是从发布阶段一跳走得通的那个，否则重定向给出的指引本身就是死路。
    assert redirected["review_object_relations"] == "knowledge"
    assert redirected["review_object_relations"] in BUSINESS_PHASE_EDGES["release"]


def test_phase_capability_host_prefers_a_phase_the_current_one_can_reach() -> None:
    # 承载阶段有多个时要挑当前阶段一跳走得通的那个，否则重定向给出的指引本身就是死路。
    assert "increment" in phase_capability_hosts("stage_change_set")
    assert phase_capability_host("stage_change_set", "validation") == "knowledge"
    assert phase_capability_host("run_evaluation", "increment") in BUSINESS_PHASE_EDGES["increment"]
    assert phase_capability_host("run_evaluation", "knowledge") == "validation"
    # 一个都不可达时退回第一个承载阶段，由重定向去解释走不通。
    assert phase_capability_host("stage_script_patch", "goal") == "validation"
