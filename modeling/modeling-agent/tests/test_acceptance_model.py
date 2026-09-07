from __future__ import annotations

import json
from typing import Any

from openjiuwen.core.foundation.tool.schema import ToolInfo

from modeling_agent.acceptance_model import plan_acceptance_turn


TOOL_NAMES = {
    "get_project_state", "list_documents", "read_documents_fully", "list_tables", "describe_table",
    "list_domain_resources", "get_domain_resource", "create_change_plan", "get_change_plan",
    "stage_change_set", "get_change_set_receipt", "validate_change_set", "request_confirmation",
    "sync_schema_objects", "get_schema_semantics", "stage_schema_semantic_changes",
    "confirm_schema_semantics", "suggest_object_relations", "patch_object_source_binding",
    "define_script_contract", "get_script_contract",
    "get_script_edit_context", "stage_script_patch", "run_script_test", "ask",
    "discard_rejected_candidate_changes",
}
TOOLS = [ToolInfo(name=name, description=name, parameters={"type": "object"}) for name in TOOL_NAMES]


def completed(
    messages: list[dict[str, Any]],
    name: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> None:
    call_id = f"call_{len(messages)}"
    messages.append({
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
        }],
    })
    messages.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps(result, ensure_ascii=False)})


def state_ready(messages: list[dict[str, Any]]) -> None:
    completed(messages, "get_project_state", {}, {"phase": "increment", "display": {"text": "当前阶段"}})


def test_first_turn_uses_openjiuwen_state_tool() -> None:
    turn = plan_acceptance_turn([{"role": "user", "content": "修改折扣资格规则说明"}], TOOLS, "SCN-CLOSED")
    assert turn.name == "get_project_state"
    assert turn.arguments == {}


def test_ambiguous_request_becomes_structured_decision_card() -> None:
    messages = [{"role": "user", "content": "修改同名测试指标，但目标不够明确。"}]
    state_ready(messages)
    turn = plan_acceptance_turn(messages, TOOLS, "SCN-AMBIGUOUS")
    assert turn.name == "ask"
    assert turn.arguments is not None
    assert turn.arguments["kind"] == "business_decision"
    assert len(turn.arguments["options"]) == 2


def test_single_resource_edit_uses_core_revision_hash_and_task_id() -> None:
    messages = [{"role": "user", "content": "只把折扣资格规则的说明改成“合成新说明”，其他内容不变。"}]
    state_ready(messages)
    completed(messages, "list_domain_resources", {"kind": "rule"}, {
        "working_revision": "rev-synthetic-1",
        "items": [{"resource_id": "rule:discount_eligibility", "resource_hash": "sha256:list"}],
    })
    completed(messages, "get_domain_resource", {"resource_id": "rule:discount_eligibility"}, {
        "working_revision": "rev-synthetic-1",
        "resource": {"id": "rule:discount_eligibility", "resource_hash": "sha256:resource"},
    })
    completed(messages, "create_change_plan", {
        "goal": "goal", "mode": "expert_edit", "expected_revision": "rev-synthetic-1", "tasks": [],
    }, {"plan": {
        "id": "cp_synthetic", "mode": "expert_edit", "approval_status": "approved",
        "expected_revision": "rev-synthetic-1",
        "tasks": [{"id": "task-rule", "resource_kind": "rule", "target_key": "discount_eligibility"}],
    }})
    turn = plan_acceptance_turn(messages, TOOLS, "SCN-CLOSED")
    assert turn.name == "stage_change_set"
    assert turn.arguments is not None
    assert turn.arguments["expected_revision"] == "rev-synthetic-1"
    action = turn.arguments["actions"][0]
    assert action["task_id"] == "task-rule"
    assert action["arguments"]["expected_resource_hash"] == "sha256:resource"
    assert action["arguments"]["actions"] == [{"op": "set_description", "value": "合成新说明"}]


def test_new_run_after_plan_approval_reads_transferred_plan() -> None:
    messages = [{
        "role": "user",
        "content": "我已在页面批准当前建模计划。请从计划第一项继续。",
    }]
    state_ready(messages)
    turn = plan_acceptance_turn(messages, TOOLS, "SCN-GENERATION")
    assert turn.name == "get_change_plan"
    assert turn.arguments == {}


def test_unknown_change_set_result_queries_receipt_before_retry() -> None:
    messages = [{"role": "user", "content": "修改折扣资格规则说明"}]
    state_ready(messages)
    completed(messages, "stage_change_set", {"mutation_id": "acceptance-change-1"}, {
        "error": "CHANGE_SET_RESULT_UNKNOWN",
        "reason": "响应丢失",
        "display": {"text": "响应丢失"},
    })
    turn = plan_acceptance_turn(messages, TOOLS, "SCN-CLOSED")
    assert turn.name == "get_change_set_receipt"
    assert turn.arguments == {"mutation_id": "acceptance-change-1"}


def test_explicit_abandon_after_rejection_discards_working_changes() -> None:
    messages = [{"role": "user", "content": "修改复核通过率说明"}]
    state_ready(messages)
    completed(messages, "request_confirmation", {}, {
        "rejected": True,
        "feedback": "放弃这项修改，保持当前已确认版本。",
    })
    turn = plan_acceptance_turn(messages, TOOLS, "SCN-CLOSED")
    assert turn.name == "discard_rejected_candidate_changes"
    assert turn.arguments == {}

    completed(messages, "discard_rejected_candidate_changes", {}, {
        "discarded": True,
        "display": {"text": "已放弃 2 项退回修改，当前已确认版本保持不变"},
    })
    settled = plan_acceptance_turn(messages, TOOLS, "SCN-CLOSED")
    assert settled.name == ""
    assert "当前已确认版本保持不变" in settled.content


def test_script_flow_starts_from_operation_snapshot() -> None:
    messages = [{"role": "user", "content": "为合成汇总操作生成安全 Python 脚本。"}]
    state_ready(messages)
    turn = plan_acceptance_turn(messages, TOOLS, "SCN-SCRIPT")
    assert turn.name == "list_domain_resources"
    assert turn.arguments == {"kind": "operation", "query": "synthetic_summary"}


def test_explicit_no_script_generation_stays_in_domain_flow() -> None:
    messages = [{
        "role": "user",
        "content": "根据合成资料建立对象、规则、操作和指标，不生成脚本。",
    }]
    state_ready(messages)
    turn = plan_acceptance_turn(messages, TOOLS, "SCN-GENERATION")
    assert turn.name == "list_documents"


def test_generation_uses_real_document_receipt_as_evidence() -> None:
    messages = [{"role": "user", "content": "根据合成资料生成规则。"}]
    state_ready(messages)
    completed(messages, "list_documents", {}, {
        "documents": [{"filename": "synthetic_rules.md", "parseStatus": "parsed"}],
    })
    completed(messages, "read_documents_fully", {}, {"documents": [{"coverage": 100}]})
    completed(messages, "list_domain_resources", {"kind": "rule"}, {
        "working_revision": "rev-generation", "items": [],
    })
    completed(messages, "create_change_plan", {}, {"plan": {
        "id": "cp-generation", "mode": "generation", "approval_status": "approved",
        "expected_revision": "rev-generation",
        "goal": "根据合成资料生成规则。",
        "tasks": [{"id": "task-rule", "resource_kind": "rule", "target_key": "discount_eligibility"}],
    }})
    turn = plan_acceptance_turn(messages, TOOLS, "SCN-GENERATION")
    assert turn.name == "stage_change_set"
    assert turn.arguments is not None
    assert turn.arguments["actions"][0]["arguments"]["evidence"] == [
        "material:synthetic_rules.md#synthetic-acceptance",
    ]


def test_schema_generation_executes_core_injected_sync_task() -> None:
    messages = [{
        "role": "user",
        "content": "按九张数据 Schema Sheet 重新生成全部对象。",
    }]
    state_ready(messages)
    completed(messages, "create_change_plan", {}, {"plan": {
        "id": "cp-schema", "mode": "generation", "approval_status": "approved",
        "expected_revision": "rev-schema", "goal": "按数据 Schema 生成全部对象",
        "tasks": [{
            "id": "task-schema", "resource_kind": "binding", "target_key": "__schema__",
            "allowed_tools": ["sync_schema_objects"], "status": "pending",
        }],
    }})
    turn = plan_acceptance_turn(messages, TOOLS, "SCN-BINDING")
    assert turn.name == "sync_schema_objects"
    assert turn.arguments == {
        "plan_task_id": "task-schema",
        "expected_revision": "rev-schema",
        "mutation_id": "acceptance-schema-sync-1",
        "reason": "按全部合成 Schema Sheet 确定性同步物理对象",
    }


def test_schema_generation_does_not_require_relations_after_semantic_confirmation() -> None:
    messages = [{"role": "user", "content": "按合成数据 Schema 生成全部对象。"}]
    state_ready(messages)
    completed(messages, "create_change_plan", {}, {"plan": {
        "id": "cp-schema-semantics", "mode": "generation", "approval_status": "approved",
        "expected_revision": "rev-schema", "goal": "按数据 Schema 生成全部对象",
        "tasks": [{
            "id": "task-schema", "resource_kind": "binding", "target_key": "__schema__",
            "allowed_tools": ["sync_schema_objects", "stage_schema_semantic_changes", "confirm_schema_semantics"],
            "status": "pending",
        }],
    }})
    completed(messages, "sync_schema_objects", {}, {
        "ok": True, "working_revision": "rev-schema-synced",
    })

    status_turn = plan_acceptance_turn(messages, TOOLS, "SCN-BINDING")
    assert status_turn.name == "get_schema_semantics"

    completed(messages, "get_schema_semantics", status_turn.arguments or {}, {
        "working_revision": "rev-schema-synced",
        "summary": {"pass": True},
        "items": [],
    })
    confirm_turn = plan_acceptance_turn(messages, TOOLS, "SCN-BINDING")
    assert confirm_turn.name == "confirm_schema_semantics"
    assert confirm_turn.arguments == {
        "plan_task_id": "task-schema",
        "expected_revision": "rev-schema-synced",
        "mutation_id": "acceptance-schema-semantics-confirm-1",
        "reason": "确定性复核合成 Schema 业务语义",
    }
    completed(messages, "confirm_schema_semantics", confirm_turn.arguments or {}, {
        "ok": True,
        "working_revision": "rev-schema-synced",
    })
    next_turn = plan_acceptance_turn(messages, TOOLS, "SCN-BINDING")
    assert next_turn.name == "list_domain_resources"
    completed(messages, next_turn.name, next_turn.arguments or {}, {
        "working_revision": "rev-schema-synced",
        "kind": "object",
        "items": [],
    })
    validation_turn = plan_acceptance_turn(messages, TOOLS, "SCN-BINDING")
    assert validation_turn.name == "validate_change_set"


def test_schema_generation_continues_semantics_on_the_same_schema_task() -> None:
    messages = [{"role": "user", "content": "继续补全合成 Schema 业务语义。"}]
    state_ready(messages)
    completed(messages, "create_change_plan", {}, {"plan": {
        "id": "cp-semantic-only", "mode": "generation", "approval_status": "approved",
        "expected_revision": "rev-physical-ready", "goal": "补全 Schema 业务语义",
        "tasks": [{
            "id": "task-schema", "resource_kind": "binding", "target_key": "__schema__",
            "allowed_tools": ["sync_schema_objects", "stage_schema_semantic_changes", "confirm_schema_semantics"],
            "status": "pending",
        }],
    }})

    sync_turn = plan_acceptance_turn(messages, TOOLS, "SCN-BINDING")
    assert sync_turn.name == "sync_schema_objects"
    completed(messages, "sync_schema_objects", sync_turn.arguments or {}, {
        "ok": True, "working_revision": "rev-physical-ready",
    })
    turn = plan_acceptance_turn(messages, TOOLS, "SCN-BINDING")
    assert turn.name == "get_schema_semantics"


def test_managed_file_redirect_retries_required_schema_tool() -> None:
    messages = [{
        "role": "user",
        "content": "按九张数据 Schema Sheet 重新生成全部对象。",
    }]
    state_ready(messages)
    arguments = {
        "plan_task_id": "task-schema",
        "expected_revision": "rev-schema",
        "mutation_id": "acceptance-schema-sync-1",
        "reason": "按全部合成 Schema Sheet 确定性同步物理对象",
    }
    completed(messages, "sync_schema_objects", arguments, {
        "error": "MANAGED_FILE_REQUIRES_DOMAIN_TOOL",
        "requiredAction": "redirect_tool",
        "requiredTool": "sync_schema_objects",
        "issueId": "agent-issue-synthetic-schema",
    })

    turn = plan_acceptance_turn(messages, TOOLS, "SCN-BINDING")

    assert turn.name == "sync_schema_objects"
    assert turn.arguments == arguments


def test_generated_script_uses_hashed_business_function_patch() -> None:
    messages = [{"role": "user", "content": "为合成汇总操作生成安全 Python 脚本。"}]
    state_ready(messages)
    completed(messages, "list_domain_resources", {"kind": "operation", "query": "synthetic_summary"}, {
        "working_revision": "rev-script-1",
        "items": [{"resource_id": "operation:synthetic_summary"}],
    })
    completed(messages, "get_domain_resource", {"resource_id": "operation:synthetic_summary"}, {
        "working_revision": "rev-script-1",
        "resource": {
            "id": "operation:synthetic_summary",
            "operation_contract_hash": "sha256:operation",
        },
    })
    completed(messages, "create_change_plan", {}, {"plan": {
        "id": "cp-script",
        "mode": "expert_edit",
        "approval_status": "approved",
        "expected_revision": "rev-script-1",
        "tasks": [{
            "id": "task-script",
            "resource_kind": "script",
            "target_key": "synthetic_summary_script",
        }],
    }})
    completed(messages, "define_script_contract", {}, {
        "script_key": "synthetic_summary_script",
        "contract_hash": "sha256:contract",
    })
    completed(messages, "get_script_edit_context", {"script_key": "synthetic_summary_script"}, {
        "base_source_hash": "sha256:source",
        "contract_hash": "sha256:contract",
        "symbols": [{
            "kind": "function",
            "qualified_name": "run_business_logic",
            "node_hash": "sha256:node",
        }],
    })

    read_turn = plan_acceptance_turn(messages, TOOLS, "SCN-SCRIPT")
    assert read_turn.name == "get_script_edit_context"
    assert read_turn.arguments == {
        "script_key": "synthetic_summary_script",
        "symbols": ["run_business_logic"],
    }

    completed(messages, read_turn.name, read_turn.arguments or {}, {
        "base_source_hash": "sha256:source",
        "contract_hash": "sha256:contract",
        "symbols": [{
            "kind": "function",
            "qualified_name": "run_business_logic",
            "node_hash": "sha256:node",
            "source": "def run_business_logic(inputs):\n    raise NotImplementedError('business logic is not implemented')",
        }],
    })
    patch_turn = plan_acceptance_turn(messages, TOOLS, "SCN-SCRIPT")

    assert patch_turn.name == "stage_script_patch"
    assert patch_turn.arguments is not None
    assert patch_turn.arguments["base_source_hash"] == "sha256:source"
    assert patch_turn.arguments["expected_contract_hash"] == "sha256:contract"
    edit = patch_turn.arguments["edits"][0]
    assert edit["kind"] == "replace_function"
    assert edit["symbol"] == "run_business_logic"
    assert edit["expected_node_hash"] == "sha256:node"
    assert "def run_business_logic(inputs):" in edit["source"]
    assert "argparse" not in edit["source"]
    assert "def main(" not in edit["source"]
