"""Tests for unified tool registry execution."""

from __future__ import annotations

import json
import inspect
import logging
import threading
import time
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from agentic.config import AgentConfig
from agentic.exceptions import WorkflowCancelledError
from agentic.evolution import ProposalBuffer
from agentic.memory.filesystem_manager import FileSystemMemoryManager
from agentic.services.llm import configure_llm_router, reset_llm_router_for_tests
from agentic.tools.base import ToolCaller, ToolContext, ToolInvocation, ToolOutcome
from agentic.tools.memory import wait_for_memory_tasks
from agentic.tools.registry import (
    TOOL_SPECS,
    _ARGUMENT_PARSE_ERROR_KEY,
    execute_tool_batch,
    get_regular_tools_schema,
    get_tools_schema,
    is_tool_available,
)
from agentic.tools.executor import _execute_single_task
import agentic.utils.logging as logging_utils
from agentic.utils.read_docs import build_read_document_record, merge_read_documents_registry
from agentic.utils.streaming import stream_writer_context
from agentic.utils.ontologies import OntologyRegistry
from agentic.utils.skills import SkillRegistry


def _build_tool_call(name: str, arguments: dict, call_id: str = "call_1") -> ToolInvocation:
    return ToolInvocation(
        name=name,
        args=arguments,
        tool_call_id=call_id,
    )


class _FakeMemoryManager:
    def __init__(self):
        self.stored = []

    def store_memory(self, **kwargs):
        self.stored.append(dict(kwargs))
        return SimpleNamespace(
            to_dict=lambda: {
                "stored": True,
                "memory_type": kwargs["memory_type"],
                "scope": kwargs["scope"],
            }
        )

    def recall_memory(self, **kwargs):
        del kwargs
        return []


class _FakeRouterLLM:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def call(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        index = len(self.calls) - 1
        payload = self.payloads[min(index, len(self.payloads) - 1)]
        if callable(payload):
            payload = payload(messages, kwargs, self)
        return SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))


class _BlockingRouterLLM:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = []

    def call(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        self.started.set()
        self.release.wait(timeout=5)
        if len(self.calls) == 1:
            payload = {
                "destination": "ordinary_memory",
                "reason": "test",
            }
        else:
            payload = {
                "content": "blocked",
                "memory_type": "preference",
                "reason": "test",
            }
        return SimpleNamespace(
            content=json.dumps(
                payload,
                ensure_ascii=False,
            )
        )


class _FakeLLMRouter:
    def __init__(self, llm):
        self.llm = llm
        self.agent_config = AgentConfig.from_env()

    def call(self, *, purpose, messages, profile_id=None, **kwargs):
        del purpose, profile_id
        return self.llm.call(messages, **kwargs)

    def get_config(self, purpose, profile_id=None):
        del purpose, profile_id
        return SimpleNamespace(
            model="fake-model",
            base_url="https://fake.example/v1",
            temperature=0.0,
            max_tokens=512,
            timeout=30,
        )


@pytest.fixture(autouse=True)
def _reset_router():
    reset_llm_router_for_tests()
    yield
    reset_llm_router_for_tests()


def _make_mock_tool(
    name: str,
    handler,
    *,
    concurrency_safe: bool = False,
    schema: dict | None = None,
    category: str = "regular",
):
    """Minimal BaseTool implementation for test injection."""
    from typing import Any, Dict, Optional, Set

    _schema = schema or {
        "type": "function",
        "function": {"name": name, "description": name, "parameters": {"type": "object", "properties": {}}},
    }

    signature = inspect.signature(handler)

    @dataclass(frozen=True)
    class _MockTool:
        name: str = field(default_factory=lambda: name)
        schema: Dict[str, Any] = field(default_factory=lambda: _schema)
        allowed_callers: Set[ToolCaller] = field(default_factory=lambda: {"main"})
        concurrency_safe: bool = field(default_factory=lambda: concurrency_safe)
        max_parallelism: Optional[int] = None
        category: str = field(default_factory=lambda: category)

        def execute(self, *, args, context, tool_call_id=None, event_emitter=None) -> ToolOutcome:
            call_kwargs = dict(args)
            optional_kwargs = {
                "context": context,
                "tool_call_id": tool_call_id,
                "event_emitter": event_emitter,
            }
            if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()):
                call_kwargs.update(optional_kwargs)
                return ToolOutcome(content=handler(**call_kwargs))

            for key, value in optional_kwargs.items():
                if key in signature.parameters:
                    call_kwargs[key] = value
            return ToolOutcome(content=handler(**call_kwargs))

    return _MockTool()


def _make_context(**overrides) -> ToolContext:
    llm = overrides.pop("llm", None)
    if llm is not None:
        configure_llm_router(_FakeLLMRouter(llm))
    base = ToolContext(
        memory_manager=None,
        messages=[],
        run_id="run-tools",
        session_id="sess-tools",
        turn_id=1,
        ov_session_id="ov-tools",
        agent_config=AgentConfig.from_env(),
        caller="main",
        event_emitter=None,
        parent_tool_call_id=None,
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def _write_scripted_ontology(tmp_path, *, operation_key="流程A", script_name="hello.py", script_body='print("hello from script")\n'):
    ontology_dir = tmp_path / "ontologies" / "demo"
    script_path = ontology_dir / "scripts" / script_name
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(script_body, encoding="utf-8")
    (ontology_dir / "manifest.yaml").write_text(
        "\n".join(
            [
                "kind: ontology",
                "id: demo",
                "name: Demo",
                "description: Demo ontology",
                "version: 1.0.0",
                "language: zh-CN",
            ]
        ),
        encoding="utf-8",
    )
    (ontology_dir / "scripts-list.md").write_text(
        "- `{}` -> `scripts/{}`\n".format(operation_key, script_name),
        encoding="utf-8",
    )
    return ontology_dir


def _write_v01_ontology_package(tmp_path):
    ontology_dir = tmp_path / "ontologies" / "demo-v01"
    (ontology_dir / "operations").mkdir(parents=True)
    (ontology_dir / "objects").mkdir(parents=True)
    (ontology_dir / "rules").mkdir(parents=True)
    (ontology_dir / "metrics").mkdir(parents=True)
    (ontology_dir / "scripts").mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        "\n".join(
            [
                "kind: ontology-package",
                "schema_version: 0.1",
                "id: demo-v01",
                "name: Demo v0.1",
                "description: Demo ontology package",
                "version: 0.1.0",
                "language: zh-CN",
            ]
        ),
        encoding="utf-8",
    )
    (ontology_dir / "operations" / "index.md").write_text(
        (
            "---\nresource: operation_index\n---\n\n"
            "| operation key | 标题 | 输入 |\n"
            "| --- | --- | --- |\n"
            "| 分析影响 | 影响分析 | eco_id |\n"
        ),
        encoding="utf-8",
    )
    (ontology_dir / "operations" / "impact.md").write_text(
        (
            "---\n"
            "resource: operation\n"
            "key: 分析影响\n"
            "canonical_key: impact_analysis\n"
            "title: 影响分析\n"
            "uses:\n"
            "  objects:\n"
            "    - 物料\n"
            "  rules:\n"
            "    - 生效规则\n"
            "  metrics:\n"
            "    - 处理成本\n"
            "---\n\n"
            "# 影响分析\n"
        ),
        encoding="utf-8",
    )
    (ontology_dir / "objects" / "index.md").write_text(
        (
            "| object key | 标题 | 说明 |\n"
            "| --- | --- | --- |\n"
            "| 物料 | TargetItem | 制造物料 |\n"
        ),
        encoding="utf-8",
    )
    (ontology_dir / "objects" / "target_item.md").write_text(
        (
            "---\n"
            "resource: object\n"
            "key: 物料\n"
            "canonical_key: target_item\n"
            "aliases:\n"
            "  - TargetItem\n"
            "---\n\n"
            "# 物料\n字段定义\n"
        ),
        encoding="utf-8",
    )
    (ontology_dir / "rules" / "index.md").write_text(
        (
            "| rule key | 标题 | 说明 |\n"
            "| --- | --- | --- |\n"
            "| 生效规则 | 变更生效 | 判断生效批次 |\n"
        ),
        encoding="utf-8",
    )
    (ontology_dir / "rules" / "effectivity.md").write_text(
        "---\nresource: rule\nkey: 生效规则\ncanonical_key: effectivity_rule\n---\n\n# 生效规则\n",
        encoding="utf-8",
    )
    (ontology_dir / "metrics" / "index.md").write_text(
        (
            "| metric key | 标题 | 说明 |\n"
            "| --- | --- | --- |\n"
            "| 处理成本 | 库存处理成本 | 数量乘单价 |\n"
        ),
        encoding="utf-8",
    )
    (ontology_dir / "metrics" / "handling_cost.md").write_text(
        "---\nresource: metric\nkey: 处理成本\ncanonical_key: handling_cost\n---\n\n# 处理成本\n",
        encoding="utf-8",
    )
    (ontology_dir / "scripts" / "index.md").write_text(
        (
            "| script key | 绑定 operation | 入口 |\n"
            "| --- | --- | --- |\n"
            "| 影响脚本 | 分析影响 | impact.py |\n"
        ),
        encoding="utf-8",
    )
    (ontology_dir / "scripts" / "impact_script.md").write_text(
        (
            "---\n"
            "resource: script\n"
            "key: 影响脚本\n"
            "canonical_key: impact_script\n"
            "bound_operation: 分析影响\n"
            "entrypoint: impact.py\n"
            "---\n\n"
            "| 参数 key | CLI 参数 | 类型 | 必填 | 说明 |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| eco_id | --eco-id | string | 是 | CHANGE_ORDER编号 |\n"
        ),
        encoding="utf-8",
    )
    (ontology_dir / "scripts" / "impact.py").write_text(
        (
            "import json\n"
            "import sys\n"
            "print(json.dumps(sys.argv[1:], ensure_ascii=False))\n"
        ),
        encoding="utf-8",
    )
    return ontology_dir


def _patch_test_ontology_registry(monkeypatch, tmp_path):
    registry = OntologyRegistry(ontology_dirs=[tmp_path / "ontologies"])
    registry.discover_ontologies()
    monkeypatch.setattr("agentic.utils.ontology_catalog.get_ontology_registry", lambda: registry)
    return registry


def _write_demo_skill(tmp_path):
    skill_dir = tmp_path / "skills" / "demo-skill"
    (skill_dir / "references" / "scripts").mkdir(parents=True)
    (skill_dir / "agents").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            "name: demo-skill\n"
            "description: Demo skill guidance\n"
            "allowed-tools: ontology_context, skill\n"
            "---\n\n"
            "# Demo Skill\n"
            "Use this skill carefully.\n"
        ),
        encoding="utf-8",
    )
    (skill_dir / "references" / "patterns.md").write_text("# Patterns\nReference details.\n", encoding="utf-8")
    (skill_dir / "references" / "scripts" / "helper.py").write_text("print('helper')\n", encoding="utf-8")
    (skill_dir / "agents" / "openai.yaml").write_text("model: test\n", encoding="utf-8")
    return skill_dir


def _patch_test_skill_registry(monkeypatch, tmp_path):
    registry = SkillRegistry(skill_dirs=[tmp_path / "skills"])
    registry.discover_skills()
    monkeypatch.setattr("agentic.tools.skill.get_skill_registry", lambda: registry)
    monkeypatch.setattr("agentic.tools.registry.get_skill_registry", lambda: registry)
    return registry


def _patch_empty_skill_registry(monkeypatch, tmp_path):
    registry = SkillRegistry(skill_dirs=[tmp_path / "skills"])
    registry.discover_skills()
    monkeypatch.setattr("agentic.tools.skill.get_skill_registry", lambda: registry)
    monkeypatch.setattr("agentic.tools.registry.get_skill_registry", lambda: registry)
    return registry


def test_registry_loads_ontology_context_root_and_object(tmp_path, monkeypatch):
    ontology_dir = _write_scripted_ontology(tmp_path)
    (ontology_dir / "operations-list.md").write_text(
        "| 名称 | 标签 | 业务描述 |\n| --- | --- | --- |\n| 流程A | 工具流程 | demo |\n",
        encoding="utf-8",
    )
    (ontology_dir / "objects-list.md").write_text(
        "| 中文名称 | 别名 | 业务定义 |\n| --- | --- | --- |\n| TargetItem | 物料 | demo object |\n",
        encoding="utf-8",
    )
    (ontology_dir / "objects").mkdir(exist_ok=True)
    (ontology_dir / "objects" / "TargetItem.md").write_text("# TargetItem\n字段", encoding="utf-8")
    _patch_test_ontology_registry(monkeypatch, tmp_path)

    root_result = execute_tool_batch(
        [_build_tool_call("ontology_context", {"ontology_id": "demo", "resource": "root"})],
        _make_context(project_root=str(tmp_path)),
    )
    root_content = root_result.messages[0]["content"]
    assert "id: demo" in root_content
    assert "ontology: Demo - Demo ontology" in root_content
    assert (
        "operations_catalog:\n"
        "columns: 名称 | 标签 | 业务描述\n"
        "- 流程A | 工具流程 | demo"
    ) in root_content
    assert (
        "objects_catalog:\n"
        "columns: 中文名称 | 别名 | 业务定义\n"
        "- TargetItem | 物料 | demo object"
    ) in root_content
    assert (
        "script_catalog:\n"
        "parameter format: name=description required|optional type; type[] means pass an array value.\n"
        "- 流程A (parameters: none)"
    ) in root_content
    assert "assets_catalog" not in root_content

    object_result = execute_tool_batch(
        [_build_tool_call("ontology_context", {"ontology_id": "demo", "resource": "object", "key": "TargetItem"})],
        _make_context(project_root=str(tmp_path)),
    )
    assert object_result.messages[0]["content"] == "object_key: TargetItem\n\ncontent:\n# TargetItem\n字段"
    assert object_result.state_updates["read_document_records"][0]["doc_type"] == "object_doc"

    unsupported_resource_result = execute_tool_batch(
        [_build_tool_call("ontology_context", {"ontology_id": "demo", "resource": "asset", "key": "rules/limits.ini"})],
        _make_context(project_root=str(tmp_path)),
    )
    assert "Unsupported ontology_context resource 'asset'" in unsupported_resource_result.messages[0]["content"]


def test_registry_loads_v01_ontology_package_resources(tmp_path, monkeypatch):
    _write_v01_ontology_package(tmp_path)
    _patch_test_ontology_registry(monkeypatch, tmp_path)

    root_result = execute_tool_batch(
        [_build_tool_call("ontology_context", {"ontology_id": "demo-v01", "resource": "root"})],
        _make_context(project_root=str(tmp_path)),
    )
    root_content = root_result.messages[0]["content"]
    assert "id: demo-v01" in root_content
    assert (
        "operations_catalog:\n"
        "columns: operation key | 标题 | 输入\n"
        "- 分析影响 | 影响分析 | eco_id"
    ) in root_content
    assert (
        "rules_catalog:\n"
        "columns: rule key | 标题 | 说明\n"
        "- 生效规则 | 变更生效 | 判断生效批次"
    ) in root_content
    assert (
        "metrics_catalog:\n"
        "columns: metric key | 标题 | 说明\n"
        "- 处理成本 | 库存处理成本 | 数量乘单价"
    ) in root_content
    assert "- 分析影响 - 影响脚本 (parameters: eco_id=CHANGE_ORDER编号 required string)" in root_content

    operation_result = execute_tool_batch(
        [
            _build_tool_call(
                "ontology_context",
                {"ontology_id": "demo-v01", "resource": "operation", "key": "impact_analysis"},
            )
        ],
        _make_context(project_root=str(tmp_path)),
    )
    assert "operation_key: impact_analysis" in operation_result.messages[0]["content"]
    assert "key: 分析影响" in operation_result.messages[0]["content"]

    object_result = execute_tool_batch(
        [_build_tool_call("ontology_context", {"ontology_id": "demo-v01", "resource": "object", "key": "TargetItem"})],
        _make_context(project_root=str(tmp_path)),
    )
    assert "object_key: TargetItem" in object_result.messages[0]["content"]
    assert "# 物料" in object_result.messages[0]["content"]

    rule_result = execute_tool_batch(
        [
            _build_tool_call(
                "ontology_context",
                {"ontology_id": "demo-v01", "resource": "rule", "key": "effectivity_rule"},
            )
        ],
        _make_context(project_root=str(tmp_path)),
    )
    assert "rule_key: effectivity_rule" in rule_result.messages[0]["content"]
    assert "# 生效规则" in rule_result.messages[0]["content"]

    metric_result = execute_tool_batch(
        [_build_tool_call("ontology_context", {"ontology_id": "demo-v01", "resource": "metric", "key": "处理成本"})],
        _make_context(project_root=str(tmp_path)),
    )
    assert "metric_key: 处理成本" in metric_result.messages[0]["content"]
    assert "# 处理成本" in metric_result.messages[0]["content"]

    script_doc_result = execute_tool_batch(
        [_build_tool_call("ontology_context", {"ontology_id": "demo-v01", "resource": "script", "key": "影响脚本"})],
        _make_context(project_root=str(tmp_path)),
    )
    assert "script_key: 影响脚本" in script_doc_result.messages[0]["content"]
    assert "CLI 参数" in script_doc_result.messages[0]["content"]

    script_result = execute_tool_batch(
        [
            _build_tool_call(
                "execute_script",
                {"ontology_id": "demo-v01", "operation_key": "分析影响", "arguments": {"eco_id": "CHANGE_ORDER-001"}},
            )
        ],
        _make_context(project_root=str(tmp_path)),
    )
    assert json.loads(script_result.messages[0]["content"]) == ["--eco-id", "CHANGE_ORDER-001"]


def test_execute_script_uses_active_ontology_version(tmp_path, monkeypatch):
    ontology_dir = tmp_path / "ontologies" / "demo"
    version1 = ontology_dir / "versions" / "1.0.0"
    version2 = ontology_dir / "versions" / "2.0.0"
    (version1 / "scripts").mkdir(parents=True)
    (version2 / "scripts").mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        "\n".join(
            [
                "kind: ontology",
                "id: demo",
                "name: Demo",
                "description: Demo ontology",
                "version: 2.0.0",
                "active_version: 2.0.0",
                "versions:",
                "  - 1.0.0",
                "  - 2.0.0",
                "language: zh-CN",
            ]
        ),
        encoding="utf-8",
    )
    (version1 / "scripts-list.md").write_text("- `流程A` -> `scripts/hello.py`\n", encoding="utf-8")
    (version1 / "scripts" / "hello.py").write_text('print("v1")\n', encoding="utf-8")
    (version2 / "scripts-list.md").write_text("- `流程A` -> `scripts/hello.py`\n", encoding="utf-8")
    (version2 / "scripts" / "hello.py").write_text('print("v2")\n', encoding="utf-8")
    _patch_test_ontology_registry(monkeypatch, tmp_path)

    result = execute_tool_batch(
        [_build_tool_call("execute_script", {"ontology_id": "demo", "operation_key": "流程A", "arguments": {}})],
        _make_context(project_root=str(tmp_path)),
    )

    assert result.messages[0]["name"] == "execute_script"
    assert "v2" in result.messages[0]["content"]
    assert "v1" not in result.messages[0]["content"]


def test_ontology_context_appends_near_point_recall_for_operation(tmp_path, monkeypatch):
    ontology_dir = _write_scripted_ontology(tmp_path)
    (ontology_dir / "operations").mkdir(exist_ok=True)
    (ontology_dir / "operations" / "流程A.md").write_text("# 流程A\n执行步骤", encoding="utf-8")
    _patch_test_ontology_registry(monkeypatch, tmp_path)

    buffer = ProposalBuffer(str(tmp_path / "operations-evolution"))
    aggregate = buffer.append(
        ontology_id="demo",
        operation_key="流程A",
        kind="procedure",
        content="先校验物料编号是否为空，再查询供应商。",
        source="active_chat",
    )
    monkeypatch.setattr("agentic.evolution.near_point_recall.get_proposal_buffer", lambda: buffer)

    result = execute_tool_batch(
        [_build_tool_call("ontology_context", {"ontology_id": "demo", "resource": "operation", "key": "流程A"})],
        _make_context(project_root=str(tmp_path)),
    )

    content = result.messages[0]["content"]
    assert "## 历史经验" in content
    assert "operation evolution:" not in content
    assert "成功模式" in content
    assert "成功模式 (procedure)" not in content
    assert "先校验物料编号是否为空" in content
    assert result.state_updates["evolution_recall_records"] == [
        {
            "workspace_id": None,
            "ontology_id": "demo",
            "operation_key": "流程A",
            "entity_key": None,
            "fingerprints": [aggregate.fingerprint],
        }
    ]


def test_ontology_context_does_not_inject_ontology_level_procedure(tmp_path, monkeypatch):
    ontology_dir = _write_scripted_ontology(tmp_path)
    (ontology_dir / "operations").mkdir(exist_ok=True)
    (ontology_dir / "operations" / "流程A.md").write_text("# 流程A\n执行步骤", encoding="utf-8")
    _patch_test_ontology_registry(monkeypatch, tmp_path)

    buffer = ProposalBuffer(str(tmp_path / "operations-evolution"))
    buffer.append(
        ontology_id="demo",
        operation_key=None,
        kind="procedure",
        content="执行本体内流程时先确认输入对象完整。",
        source="active_chat",
    )
    operation_aggregate = buffer.append(
        ontology_id="demo",
        operation_key="流程A",
        kind="procedure",
        content="流程A先校验物料编号。",
        source="active_chat",
    )
    monkeypatch.setattr("agentic.evolution.near_point_recall.get_proposal_buffer", lambda: buffer)

    result = execute_tool_batch(
        [_build_tool_call("ontology_context", {"ontology_id": "demo", "resource": "operation", "key": "流程A"})],
        _make_context(project_root=str(tmp_path)),
    )

    content = result.messages[0]["content"]
    assert "执行本体内流程时先确认输入对象完整" not in content
    assert "流程A先校验物料编号" in content
    assert result.state_updates["evolution_recall_records"][0]["fingerprints"] == [
        operation_aggregate.fingerprint,
    ]


def test_ontology_context_appends_schema_recall_for_object_with_recent_operation(tmp_path, monkeypatch):
    ontology_dir = _write_scripted_ontology(tmp_path)
    (ontology_dir / "operations").mkdir(exist_ok=True)
    (ontology_dir / "operations" / "流程A.md").write_text("# 流程A\n执行步骤", encoding="utf-8")
    (ontology_dir / "objects").mkdir(exist_ok=True)
    (ontology_dir / "objects" / "TargetItem.md").write_text("# TargetItem\n字段", encoding="utf-8")
    _patch_test_ontology_registry(monkeypatch, tmp_path)

    buffer = ProposalBuffer(str(tmp_path / "operations-evolution"))
    aggregate = buffer.append(
        ontology_id="demo",
        operation_key="流程A",
        kind="schema",
        entity_key="TargetItem",
        content="TargetItem 的物料编码字段是 TargetItem编码。",
        source="active_chat",
    )
    monkeypatch.setattr("agentic.evolution.near_point_recall.get_proposal_buffer", lambda: buffer)
    read_documents = merge_read_documents_registry(
        None,
        [
            build_read_document_record(
                file_path="ontologies/demo/operations/流程A.md",
                content="# 流程A\n执行步骤",
                source="ontology_context",
                turn_id=1,
                iteration=1,
            )
        ],
    )

    result = execute_tool_batch(
        [_build_tool_call("ontology_context", {"ontology_id": "demo", "resource": "object", "key": "TargetItem"})],
        _make_context(project_root=str(tmp_path), read_documents=read_documents),
    )

    content = result.messages[0]["content"]
    assert "实体字段修正" in content
    assert "实体 schema 修正 (schema)" not in content
    assert "TargetItem 的物料编码字段是 TargetItem编码" in content
    assert result.state_updates["evolution_recall_records"][0]["fingerprints"] == [aggregate.fingerprint]


def test_ontology_context_appends_object_level_schema_without_recent_operation(tmp_path, monkeypatch):
    ontology_dir = _write_scripted_ontology(tmp_path)
    (ontology_dir / "objects").mkdir(exist_ok=True)
    (ontology_dir / "objects" / "TargetItem.md").write_text("# TargetItem\n字段", encoding="utf-8")
    _patch_test_ontology_registry(monkeypatch, tmp_path)

    buffer = ProposalBuffer(str(tmp_path / "operations-evolution"))
    aggregate = buffer.append(
        ontology_id="demo",
        operation_key=None,
        kind="schema",
        entity_key="TargetItem",
        content="TargetItem 的物料编码字段是 TargetItem编码。",
        source="active_chat",
    )
    monkeypatch.setattr("agentic.evolution.near_point_recall.get_proposal_buffer", lambda: buffer)

    result = execute_tool_batch(
        [_build_tool_call("ontology_context", {"ontology_id": "demo", "resource": "object", "key": "TargetItem"})],
        _make_context(project_root=str(tmp_path)),
    )

    content = result.messages[0]["content"]
    assert "## 历史经验" in content
    assert "operation evolution:" not in content
    assert "实体字段修正" in content
    assert "实体 schema 修正 (schema)" not in content
    assert "TargetItem 的物料编码字段是 TargetItem编码" in content
    assert result.state_updates["evolution_recall_records"] == [
        {
            "workspace_id": None,
            "ontology_id": "demo",
            "operation_key": None,
            "entity_key": "TargetItem",
            "fingerprints": [aggregate.fingerprint],
        }
    ]


def test_registry_loads_skill_root_and_supporting_files(tmp_path, monkeypatch):
    _write_demo_skill(tmp_path)
    _patch_test_skill_registry(monkeypatch, tmp_path)

    root_result = execute_tool_batch(
        [_build_tool_call("skill", {"skill_name": "demo-skill", "resource": "root"})],
        _make_context(project_root=str(tmp_path)),
    )
    root_payload = json.loads(root_result.messages[0]["content"])
    assert root_result.messages[0]["name"] == "skill"
    assert root_payload["skill"]["name"] == "demo-skill"
    assert "# Demo Skill" in root_payload["content"]
    assert root_payload["reference_catalog"][0]["key"] == "patterns.md"
    assert root_payload["script_catalog"][0]["key"] == "helper.py"
    assert root_payload["agent_catalog"][0]["key"] == "openai.yaml"
    assert root_result.state_updates["read_document_records"][0]["path"] == "skills/demo-skill/SKILL.md"
    assert root_result.state_updates["read_document_records"][0]["source"] == "skill"

    reference_result = execute_tool_batch(
        [_build_tool_call("skill", {"skill_name": "demo-skill", "resource": "reference", "key": "patterns.md"})],
        _make_context(project_root=str(tmp_path)),
    )
    assert "\"resource\": \"reference\"" in reference_result.messages[0]["content"]
    assert "Reference details" in reference_result.messages[0]["content"]

    script_result = execute_tool_batch(
        [_build_tool_call("skill", {"skill_name": "demo-skill", "resource": "script", "key": "helper.py"})],
        _make_context(project_root=str(tmp_path)),
    )
    assert "\"resource\": \"script\"" in script_result.messages[0]["content"]
    assert "print('helper')" in script_result.messages[0]["content"]


def test_single_tool_interval_starts_at_actual_execution(monkeypatch):
    ticks = iter([2000, 2015])
    monkeypatch.setattr("agentic.tools.executor._now_monotonic_ms", lambda: next(ticks))

    task = {
        "index": 0,
        "name": "mock_tool",
        "args": {},
        "tool_call_id": "call_mock",
        "spec": _make_mock_tool("mock_tool", lambda: "ok"),
        "started_at_ms": 1000,
    }

    output = _execute_single_task(task, _make_context(), None)

    assert output["tool_interval_ms"] == {
        "started_at_ms": 2000,
        "finished_at_ms": 2015,
        "elapsed_ms": 15,
    }


def test_registry_executes_script_from_ontology_operation(tmp_path, monkeypatch):
    _write_scripted_ontology(tmp_path)
    _patch_test_ontology_registry(monkeypatch, tmp_path)

    result = execute_tool_batch(
        [_build_tool_call("execute_script", {"ontology_id": "demo", "operation_key": "流程A", "arguments": {}})],
        _make_context(project_root=str(tmp_path)),
    )

    assert result.messages[0]["name"] == "execute_script"
    assert "hello from script" in result.messages[0]["content"]


def test_execute_script_supports_structured_parameters(tmp_path, monkeypatch):
    _write_scripted_ontology(
        tmp_path,
        script_body=(
            "import json\n"
            "import sys\n"
            "print(json.dumps(sys.argv[1:], ensure_ascii=False))\n"
        ),
    )
    scripts_list = tmp_path / "ontologies" / "demo" / "scripts-list.md"
    scripts_list.write_text(
        (
            "- `流程A` -> `scripts/hello.py`\n"
            "  `parameters`: `[{\"name\":\"target_item_code\",\"flag\":\"--target_item-code\","
            "\"required\":true,\"type\":\"string\",\"description\":\"TargetItem编码\"},"
            "{\"name\":\"equipment_model\",\"flag\":\"--equipment-model\",\"required\":false,"
            "\"type\":\"string\",\"description\":\"设备型号\"},"
            "{\"name\":\"batch_codes\",\"flag\":\"--batch-code\",\"required\":true,"
            "\"type\":\"string\",\"multiple\":true,\"description\":\"设备批次\"}]`\n"
        ),
        encoding="utf-8",
    )
    _patch_test_ontology_registry(monkeypatch, tmp_path)

    root_result = execute_tool_batch(
        [_build_tool_call("ontology_context", {"ontology_id": "demo", "resource": "root"})],
        _make_context(project_root=str(tmp_path)),
    )
    root_content = root_result.messages[0]["content"]
    assert "parameter format: name=description required|optional type; type[] means pass an array value." in root_content
    assert (
        "- 流程A (parameters: "
        "target_item_code=TargetItem编码 required string; "
        "equipment_model=设备型号 optional string; "
        "batch_codes=设备批次 required string[])"
    ) in root_content

    result = execute_tool_batch(
        [
            _build_tool_call(
                "execute_script",
                {
                    "ontology_id": "demo",
                    "operation_key": "流程A",
                    "arguments": {
                        "target_item_code": "MP-001",
                        "batch_codes": ["F001", "F002"],
                    },
                },
            )
        ],
        _make_context(project_root=str(tmp_path)),
    )

    assert json.loads(result.messages[0]["content"]) == [
        "--target_item-code",
        "MP-001",
        "--batch-code",
        "F001",
        "--batch-code",
        "F002",
    ]

    result_with_optional = execute_tool_batch(
        [
            _build_tool_call(
                "execute_script",
                {
                    "ontology_id": "demo",
                    "operation_key": "流程A",
                    "arguments": {
                        "target_item_code": "MP-001",
                        "equipment_model": "EQ-100",
                        "batch_codes": ["F001"],
                    },
                },
            )
        ],
        _make_context(project_root=str(tmp_path)),
    )

    assert json.loads(result_with_optional.messages[0]["content"]) == [
        "--target_item-code",
        "MP-001",
        "--equipment-model",
        "EQ-100",
        "--batch-code",
        "F001",
    ]


def test_execute_script_validates_structured_parameters(tmp_path, monkeypatch):
    _write_scripted_ontology(tmp_path)
    scripts_list = tmp_path / "ontologies" / "demo" / "scripts-list.md"
    scripts_list.write_text(
        (
            "- `流程A` -> `scripts/hello.py`\n"
            "  `parameters`: `[{\"name\":\"quantity\",\"flag\":\"--quantity\","
            "\"required\":true,\"type\":\"integer\",\"description\":\"数量\"},"
            "{\"name\":\"batch_codes\",\"flag\":\"--batch-code\",\"required\":false,"
            "\"type\":\"string\",\"multiple\":true,\"description\":\"设备批次\"}]`\n"
        ),
        encoding="utf-8",
    )
    _patch_test_ontology_registry(monkeypatch, tmp_path)

    missing_result = execute_tool_batch(
        [_build_tool_call("execute_script", {"ontology_id": "demo", "operation_key": "流程A", "arguments": {}})],
        _make_context(project_root=str(tmp_path)),
    )
    assert "Missing required script argument(s)" in missing_result.messages[0]["content"]
    assert "数量" in missing_result.messages[0]["content"]

    invalid_result = execute_tool_batch(
        [
            _build_tool_call(
                "execute_script",
                {
                    "ontology_id": "demo",
                    "operation_key": "流程A",
                    "arguments": {"quantity": "abc", "unknown": "value"},
                },
            )
        ],
        _make_context(project_root=str(tmp_path)),
    )
    assert "Invalid script argument(s)" in invalid_result.messages[0]["content"]
    assert "quantity: expected integer" in invalid_result.messages[0]["content"]
    assert "Unknown argument 'unknown'" in invalid_result.messages[0]["content"]


def test_execute_script_rejects_arguments_when_no_parameters_declared(tmp_path, monkeypatch):
    _write_scripted_ontology(tmp_path)
    _patch_test_ontology_registry(monkeypatch, tmp_path)

    result = execute_tool_batch(
        [
            _build_tool_call(
                "execute_script",
                {
                    "ontology_id": "demo",
                    "operation_key": "流程A",
                    "arguments": {"unexpected": "value"},
                },
            )
        ],
        _make_context(project_root=str(tmp_path)),
    )

    assert "Invalid script argument(s)" in result.messages[0]["content"]
    assert "declares no parameters" in result.messages[0]["content"]


def test_execute_script_injects_bound_mindsdb_connection(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    _write_scripted_ontology(
        tmp_path,
        script_body=(
            "from agentic.services.mindsdb import MindsDBClient\n"
            "client = MindsDBClient()\n"
            "try:\n"
            "    print(client.base_url)\n"
            "    print(client.config.default_database)\n"
            "finally:\n"
            "    client.close()\n"
        ),
    )
    _patch_test_ontology_registry(monkeypatch, tmp_path)

    from agentic.business_data.bindings_store import set_ontology_data_binding
    from agentic.business_data.connections_store import create_data_connection

    create_data_connection(
        {
            "id": "mindsdb-main",
            "name": "MindsDB Main",
            "provider": "mindsdb",
            "config": {
                "url": "https://mindsdb.example.com/",
                "default_database": "analytics",
                "timeout_seconds": 120,
            },
        },
        workspace_id="demo-ws",
    )
    set_ontology_data_binding(
        ontology_id="demo",
        connection_id="mindsdb-main",
        workspace_id="demo-ws",
    )

    result = execute_tool_batch(
        [_build_tool_call("execute_script", {"ontology_id": "demo", "operation_key": "流程A", "arguments": {}})],
        _make_context(project_root=str(tmp_path), workspace_id="demo-ws"),
    )

    assert result.messages[0]["name"] == "execute_script"
    assert "https://mindsdb.example.com" in result.messages[0]["content"]
    assert "analytics" in result.messages[0]["content"]


def test_execute_script_forces_utf8_child_output(tmp_path, monkeypatch):
    _write_scripted_ontology(
        tmp_path,
        script_body=(
            "import json\n"
            "import os\n"
            "import sys\n"
            "print(json.dumps({\n"
            "    'io_encoding': os.environ.get('PYTHONIOENCODING'),\n"
            "    'utf8_mode': os.environ.get('PYTHONUTF8'),\n"
            "    'stdout_encoding': sys.stdout.encoding,\n"
            "    'result': '成功',\n"
            "}, ensure_ascii=False))\n"
        ),
    )
    _patch_test_ontology_registry(monkeypatch, tmp_path)

    result = execute_tool_batch(
        [_build_tool_call("execute_script", {"ontology_id": "demo", "operation_key": "流程A", "arguments": {}})],
        _make_context(project_root=str(tmp_path)),
    )

    payload = json.loads(result.messages[0]["content"])
    assert payload["io_encoding"] == "utf-8"
    assert payload["utf8_mode"] == "1"
    assert payload["result"] == "成功"
    assert "�" not in result.messages[0]["content"]


def test_registry_emits_subagent_tool_events_via_scoped_event_emitter(tmp_path, monkeypatch):
    captured = []
    _write_scripted_ontology(tmp_path, script_name="child_tool.py", script_body='print("child tool ok")\n')
    _patch_test_ontology_registry(monkeypatch, tmp_path)

    def capture_event(event_type, payload):
        captured.append({"type": event_type, "payload": dict(payload or {})})

    result = execute_tool_batch(
        [_build_tool_call("execute_script", {"ontology_id": "demo", "operation_key": "流程A", "arguments": {}}, call_id="call_child_tool")],
        _make_context(
            project_root=str(tmp_path),
            caller="subagent",
            event_emitter=capture_event,
            subagent_lineage={
                "subagent_id": "sub_test_1",
                "task_id": "task_42",
                "group_id": "group_9",
                "depth": 1,
                "root_tool_call_id": "call_parent",
            },
        ),
    )

    assert result.messages[0]["name"] == "execute_script"
    assert "child tool ok" in result.messages[0]["content"]
    assert [event["type"] for event in captured] == ["tool.started", "tool.finished"]
    assert captured[0]["payload"]["tool_name"] == "execute_script"
    assert captured[0]["payload"]["input"]["operation_key"] == "流程A"
    assert captured[0]["payload"]["subagent_id"] == "sub_test_1"
    assert captured[0]["payload"]["task_id"] == "task_42"
    assert captured[0]["payload"]["group_id"] == "group_9"
    assert captured[1]["payload"]["tool_name"] == "execute_script"
    assert captured[1]["payload"]["subagent_id"] == "sub_test_1"
    assert captured[1]["payload"]["root_tool_call_id"] == "call_parent"



    assert result.terminal is False
    assert result.state_updates == {}


def test_registry_logs_script_execution_lifecycle(tmp_path, monkeypatch, caplog):
    _write_scripted_ontology(tmp_path)
    _patch_test_ontology_registry(monkeypatch, tmp_path)

    with caplog.at_level(logging.INFO, logger="tools.execute_script"):
        result = execute_tool_batch(
            [_build_tool_call("execute_script", {"ontology_id": "demo", "operation_key": "流程A", "arguments": {}}, call_id="call_exec_1")],
            _make_context(project_root=str(tmp_path)),
        )

    assert "hello from script" in result.messages[0]["content"]
    messages = [record.getMessage() for record in caplog.records]

    assert any(
        "execute_script tool invoked: tool_call_id=call_exec_1" in message
        for message in messages
    )
    assert any(
        "execute_script request received" in message
        and "tool_call_id=call_exec_1" in message
        for message in messages
    )
    assert any(
        "Launching python script" in message
        and "tool_call_id=call_exec_1" in message
        and "hello.py" in message
        for message in messages
    )
    assert any(
        "Python script completed" in message
        and "tool_call_id=call_exec_1" in message
        and "return_code=0" in message
        for message in messages
    )


def test_registry_blocks_non_python_script_execution(tmp_path, monkeypatch):
    _write_scripted_ontology(tmp_path, script_name="hello.sh", script_body='echo "hello"\n')
    _patch_test_ontology_registry(monkeypatch, tmp_path)

    result = execute_tool_batch(
        [_build_tool_call("execute_script", {"ontology_id": "demo", "operation_key": "流程A", "arguments": {}})],
        _make_context(project_root=str(tmp_path)),
    )

    assert result.messages[0]["name"] == "execute_script"
    assert "Only .py scripts can be executed" in result.messages[0]["content"]


def test_registry_blocks_unregistered_script_operation(tmp_path, monkeypatch):
    _write_scripted_ontology(tmp_path)
    _patch_test_ontology_registry(monkeypatch, tmp_path)

    result = execute_tool_batch(
        [_build_tool_call("execute_script", {"ontology_id": "demo", "operation_key": "不存在", "arguments": {}})],
        _make_context(project_root=str(tmp_path)),
    )

    assert result.messages[0]["name"] == "execute_script"
    assert "has no registered script implementation" in result.messages[0]["content"]


def test_registry_returns_validation_error_for_missing_required_args():
    result = execute_tool_batch(
        [_build_tool_call("business_data_search", {}, call_id="call_db_1")],
        _make_context(),
    )

    assert result.messages[0]["name"] == "business_data_search"
    assert "missing required argument(s): ontology_id, entity, query" in result.messages[0]["content"]


def test_registry_propagates_log_context_to_parallel_tool_threads(monkeypatch):
    captured_contexts = []

    def capture_context(**kwargs):
        del kwargs
        captured_contexts.append(logging_utils.get_log_context())
        return "ok"

    monkeypatch.setitem(
        TOOL_SPECS,
        "capture_log_context",
        _make_mock_tool(
            "capture_log_context",
            capture_context,
            concurrency_safe=True,
        ),
    )

    with logging_utils.bind_log_context(
        {
            "agent_type": "subagent",
            "agent_id": "sub_parallel_1",
            "task_id": "task-parallel",
        }
    ):
        result = execute_tool_batch(
            [_build_tool_call("capture_log_context", {})],
            _make_context(),
        )

    assert result.messages[0]["content"] == "ok"
    assert captured_contexts == [
        {
            "agent_type": "subagent",
            "agent_id": "sub_parallel_1",
            "task_id": "task-parallel",
        }
    ]


def test_registry_allows_business_data_search_without_matching_object_definition(monkeypatch):
    def fake_search(query, ontology_id=None, entity=None, event_emitter=None, tool_call_id=None):
        del ontology_id, entity, event_emitter, tool_call_id
        return "done:{}".format(query)

    monkeypatch.setitem(
        TOOL_SPECS,
        "business_data_search",
        _make_mock_tool(
            "business_data_search",
            fake_search,
            schema=TOOL_SPECS["business_data_search"].schema,
            category=TOOL_SPECS["business_data_search"].category,
        ),
    )

    result = execute_tool_batch(
        [
            _build_tool_call(
                "business_data_search",
                {"ontology_id": "demo", "entity": "TargetItem", "query": "TargetItem where TargetItem编码 = 'A001'"},
                call_id="call_db_without_object",
            )
        ],
        _make_context(
            messages=[
                {
                    "role": "tool",
                    "name": "ontology_context",
                    "tool_call_id": "call_ops",
                    "content": "File: ontologies/demo/operations/demo-op.md\n\n# demo op",
                },
                {
                    "role": "tool",
                    "name": "ontology_context",
                    "tool_call_id": "call_obj_list",
                    "content": "File: ontologies/demo/objects-list.md\n\n- TargetItem",
                },
            ]
        ),
    )

    assert result.messages[0]["name"] == "business_data_search"
    assert result.messages[0]["content"] == "done:TargetItem where TargetItem编码 = 'A001'"


def test_registry_allows_business_data_search_after_matching_object_definition(monkeypatch):
    def fake_search(query, ontology_id=None, entity=None, event_emitter=None, tool_call_id=None):
        del ontology_id, entity, event_emitter, tool_call_id
        return "done:{}".format(query)

    monkeypatch.setitem(
        TOOL_SPECS,
        "business_data_search",
        _make_mock_tool(
            "business_data_search",
            fake_search,
            schema=TOOL_SPECS["business_data_search"].schema,
            category=TOOL_SPECS["business_data_search"].category,
        ),
    )

    result = execute_tool_batch(
        [
            _build_tool_call(
                "business_data_search",
                {"ontology_id": "demo", "entity": "TargetItem", "query": "TargetItem where TargetItem编码 = 'A001'"},
                call_id="call_db_allowed",
            )
        ],
        _make_context(
            read_documents=merge_read_documents_registry(
                None,
                [
                    build_read_document_record(
                        file_path="ontologies/demo/objects/TargetItem.md",
                        content="# TargetItem",
                        source="ontology_context",
                        turn_id=1,
                        iteration=1,
                    )
                ],
            )
        ),
    )

    assert result.messages[0]["content"] == "done:TargetItem where TargetItem编码 = 'A001'"


def test_registry_allows_business_data_search_after_matching_object_definition_for_chinese_query(monkeypatch):
    def fake_search(query, ontology_id=None, entity=None, event_emitter=None, tool_call_id=None):
        del ontology_id, entity, event_emitter, tool_call_id
        return "done:{}".format(query)

    monkeypatch.setitem(
        TOOL_SPECS,
        "business_data_search",
        _make_mock_tool(
            "business_data_search",
            fake_search,
            schema=TOOL_SPECS["business_data_search"].schema,
            category=TOOL_SPECS["business_data_search"].category,
        ),
    )

    query = "CHANGE_ORDER_ID = 'CHANGE_ORDER-001' 的 CHANGE_ORDER受影响对象"
    result = execute_tool_batch(
        [
            _build_tool_call(
                "business_data_search",
                {"ontology_id": "demo", "entity": "CHANGE_ORDER受影响对象", "query": query},
                call_id="call_db_allowed_cn",
            )
        ],
        _make_context(
            messages=[
                {
                    "role": "system",
                    "content": "<Selected-Ontology>",
                }
            ],
            read_documents=merge_read_documents_registry(
                None,
                [
                    build_read_document_record(
                        file_path="ontologies/demo/objects/CHANGE_ORDER受影响对象.md",
                        content="# CHANGE_ORDER受影响对象",
                        source="ontology_context",
                        turn_id=1,
                        iteration=1,
                    )
                ],
            ),
        ),
    )

    assert result.messages[0]["content"] == "done:{}".format(query)


def test_registry_allows_explicit_entity_query_after_matching_object_definition(monkeypatch):
    def fake_search(query, ontology_id=None, entity=None, event_emitter=None, tool_call_id=None):
        del ontology_id, entity, event_emitter, tool_call_id
        return "done:{}".format(query)

    monkeypatch.setitem(
        TOOL_SPECS,
        "business_data_search",
        _make_mock_tool(
            "business_data_search",
            fake_search,
            schema=TOOL_SPECS["business_data_search"].schema,
            category=TOOL_SPECS["business_data_search"].category,
        ),
    )

    query = "查询迷雾中迷雾载荷ID、所属快艇ID、设备名称、健康度、有效范围信息"
    result = execute_tool_batch(
        [
            _build_tool_call(
                "business_data_search",
                {"ontology_id": "demo", "entity": "迷雾", "query": query},
                call_id="call_db_miwu_allowed",
            )
        ],
        _make_context(
            messages=[
                {
                    "role": "tool",
                    "name": "ontology_context",
                    "tool_call_id": "call_obj_list_miwu",
                    "content": "File: ontologies/demo/objects-list.md\n\n- 迷雾\n- 快艇",
                }
            ],
            read_documents=merge_read_documents_registry(
                None,
                [
                    build_read_document_record(
                        file_path="ontologies/demo/objects/迷雾.md",
                        content="# 迷雾",
                        source="ontology_context",
                        turn_id=1,
                        iteration=1,
                    ),
                    build_read_document_record(
                        file_path="ontologies/demo/objects/快艇.md",
                        content="# 快艇",
                        source="ontology_context",
                        turn_id=1,
                        iteration=1,
                    ),
                ],
            ),
        ),
    )

    assert result.messages[0]["content"] == "done:{}".format(query)


def test_registry_allows_explicit_entity_possessive_query_after_matching_object_definition(monkeypatch):
    def fake_search(query, ontology_id=None, entity=None, event_emitter=None, tool_call_id=None):
        del ontology_id, entity, event_emitter, tool_call_id
        return "done:{}".format(query)

    monkeypatch.setitem(
        TOOL_SPECS,
        "business_data_search",
        _make_mock_tool(
            "business_data_search",
            fake_search,
            schema=TOOL_SPECS["business_data_search"].schema,
            category=TOOL_SPECS["business_data_search"].category,
        ),
    )

    query = "查询迷雾的迷雾载荷ID、所属快艇ID、健康度信息"
    result = execute_tool_batch(
        [
            _build_tool_call(
                "business_data_search",
                {"ontology_id": "demo", "entity": "迷雾", "query": query},
                call_id="call_db_miwu_possessive_allowed",
            )
        ],
        _make_context(
            read_documents=merge_read_documents_registry(
                None,
                [
                    build_read_document_record(
                        file_path="ontologies/demo/objects/迷雾.md",
                        content="# 迷雾",
                        source="ontology_context",
                        turn_id=1,
                        iteration=1,
                    ),
                ],
            ),
        ),
    )

    assert result.messages[0]["content"] == "done:{}".format(query)


def test_registry_allows_business_data_search_without_read_documents_registry(monkeypatch):
    def fake_search(query, ontology_id=None, entity=None, event_emitter=None, tool_call_id=None):
        del ontology_id, entity, event_emitter, tool_call_id
        return "done:{}".format(query)

    monkeypatch.setitem(
        TOOL_SPECS,
        "business_data_search",
        _make_mock_tool(
            "business_data_search",
            fake_search,
            schema=TOOL_SPECS["business_data_search"].schema,
            category=TOOL_SPECS["business_data_search"].category,
        ),
    )

    query = "CHANGE_ORDER_ID = 'CHANGE_ORDER-001' 的 CHANGE_ORDER受影响对象"
    result = execute_tool_batch(
        [
            _build_tool_call(
                "business_data_search",
                {"ontology_id": "demo", "entity": "CHANGE_ORDER受影响对象", "query": query},
                call_id="call_db_no_fallback",
            )
        ],
        _make_context(
            messages=[
                {
                    "role": "system",
                    "content": "<Selected-Ontology>",
                },
                {
                    "role": "tool",
                    "name": "ontology_context",
                    "tool_call_id": "call_obj_cn",
                    "content": "File: ontologies/demo/objects/CHANGE_ORDER受影响对象.md\n\n# CHANGE_ORDER受影响对象",
                },
            ],
            read_documents=None,
        ),
    )

    assert result.messages[0]["content"] == "done:{}".format(query)


def test_registry_allows_chinese_business_data_search_without_matching_object_definition(monkeypatch):
    def fake_search(query, ontology_id=None, entity=None, event_emitter=None, tool_call_id=None):
        del ontology_id, entity, event_emitter, tool_call_id
        return "done:{}".format(query)

    monkeypatch.setitem(
        TOOL_SPECS,
        "business_data_search",
        _make_mock_tool(
            "business_data_search",
            fake_search,
            schema=TOOL_SPECS["business_data_search"].schema,
            category=TOOL_SPECS["business_data_search"].category,
        ),
    )

    query = "CHANGE_ORDER_ID = 'CHANGE_ORDER-001' 的 CHANGE_ORDER受影响对象"
    result = execute_tool_batch(
        [
            _build_tool_call(
                "business_data_search",
                {"ontology_id": "demo", "entity": "CHANGE_ORDER受影响对象", "query": query},
                call_id="call_db_blocked_cn",
            )
        ],
        _make_context(
            messages=[
                {
                    "role": "system",
                    "content": "<Selected-Ontology>",
                },
                {
                    "role": "tool",
                    "name": "ontology_context",
                    "tool_call_id": "call_obj_list_cn",
                    "content": "File: ontologies/demo/objects-list.md\n\n- CHANGE_ORDER受影响对象",
                },
            ]
        ),
    )

    assert result.messages[0]["content"] == "done:{}".format(query)


def test_registry_allows_explicit_entity_query_without_objects_list(monkeypatch):
    def fake_search(query, ontology_id=None, entity=None, event_emitter=None, tool_call_id=None):
        del ontology_id, entity, event_emitter, tool_call_id
        return "done:{}".format(query)

    monkeypatch.setitem(
        TOOL_SPECS,
        "business_data_search",
        _make_mock_tool(
            "business_data_search",
            fake_search,
            schema=TOOL_SPECS["business_data_search"].schema,
            category=TOOL_SPECS["business_data_search"].category,
        ),
    )

    query = "查询迷雾的迷雾载荷ID、所属快艇ID、健康度信息"
    result = execute_tool_batch(
        [
            _build_tool_call(
                "business_data_search",
                {"ontology_id": "demo", "entity": "迷雾", "query": query},
                call_id="call_db_miwu_possessive_blocked",
            )
        ],
        _make_context(
            messages=[
                {
                    "role": "system",
                    "content": "<Selected-Ontology>",
                }
            ]
        ),
    )

    assert result.messages[0]["content"] == "done:{}".format(query)


def test_registry_allows_explicit_entity_query_with_objects_list_only(monkeypatch):
    def fake_search(query, ontology_id=None, entity=None, event_emitter=None, tool_call_id=None):
        del ontology_id, entity, event_emitter, tool_call_id
        return "done:{}".format(query)

    monkeypatch.setitem(
        TOOL_SPECS,
        "business_data_search",
        _make_mock_tool(
            "business_data_search",
            fake_search,
            schema=TOOL_SPECS["business_data_search"].schema,
            category=TOOL_SPECS["business_data_search"].category,
        ),
    )

    query = "查询迷雾中迷雾载荷ID、所属快艇ID、设备名称、健康度、有效范围信息"
    result = execute_tool_batch(
        [
            _build_tool_call(
                "business_data_search",
                {"ontology_id": "demo", "entity": "迷雾", "query": query},
                call_id="call_db_miwu_blocked",
            )
        ],
        _make_context(
            messages=[
                {
                    "role": "system",
                    "content": "<Selected-Ontology>",
                },
                {
                    "role": "tool",
                    "name": "ontology_context",
                    "tool_call_id": "call_obj_list_miwu",
                    "content": "File: ontologies/demo/objects-list.md\n\n- 迷雾\n- 快艇",
                },
            ]
        ),
    )

    assert result.messages[0]["content"] == "done:{}".format(query)


def test_registry_emits_business_data_search_progress_events(monkeypatch):
    captured = []

    def fake_search(query, ontology_id=None, entity=None, event_emitter=None, tool_call_id=None):
        del query, ontology_id, entity, tool_call_id
        if event_emitter:
            event_emitter({"type": "start", "content": "start searching"})
            event_emitter({"type": "intermediate", "content": "processing"})
            event_emitter({"type": "final", "content": "done"})
        return "done"

    monkeypatch.setitem(
        TOOL_SPECS,
        "business_data_search",
        _make_mock_tool(
            "business_data_search",
            fake_search,
            schema=TOOL_SPECS["business_data_search"].schema,
            category=TOOL_SPECS["business_data_search"].category,
        ),
    )

    with stream_writer_context(captured.append):
        result = execute_tool_batch(
            [
                _build_tool_call(
                    "business_data_search",
                    {"ontology_id": "demo", "entity": "TargetItem", "query": "TargetItem where TargetItem编码 = 'A001'"},
                    call_id="call_db_1",
                )
            ],
            _make_context(
                read_documents=merge_read_documents_registry(
                    None,
                    [
                        build_read_document_record(
                            file_path="ontologies/demo/objects/TargetItem.md",
                            content="# TargetItem",
                            source="ontology_context",
                            turn_id=1,
                            iteration=1,
                        )
                    ],
                )
            ),
        )

    assert result.messages[0]["content"] == "done"
    event_types = [event["data"]["type"] for event in captured]
    assert event_types == [
        "tool.started",
        "tool.progress",
        "tool.progress",
        "tool.progress",
        "tool.finished",
    ]


def test_memory_tool_routes_user_memory_to_memory_manager():
    memory_manager = _FakeMemoryManager()
    llm = _FakeRouterLLM(
        [
            {
                "destination": "ordinary_memory",
                "reason": "durable response preference",
            },
            {
                "content": "用户偏好：默认使用中文回答。",
                "memory_type": "preference",
                "reason": "durable response preference",
            },
        ]
    )
    result = execute_tool_batch(
        [
            _build_tool_call(
                "memory",
                {
                    "content": "用户偏好：默认使用中文回答。",
                },
            )
        ],
        _make_context(
            llm=llm,
            memory_manager=memory_manager,
        ),
    )

    payload = json.loads(result.messages[0]["content"])
    assert payload["queued"] is True
    assert payload["status"] == "queued"
    wait_for_memory_tasks(timeout=5)
    assert memory_manager.stored[0]["memory_type"] == "preference"
    assert memory_manager.stored[0]["scope"] == "user"
    assert "tags" not in memory_manager.stored[0]
    assert "confidence" not in memory_manager.stored[0]
    assert [call["kwargs"]["request_label"] for call in llm.calls] == [
        "memory_route",
        "memory_extract_ordinary",
    ]
    router_payload = json.loads(llm.calls[0]["messages"][1]["content"])
    assert router_payload == {"content": "用户偏好：默认使用中文回答。"}


def test_memory_tool_replaces_conflicting_ordinary_preference(tmp_path):
    memory_manager = FileSystemMemoryManager(
        user_id="alice",
        workspace_dir=tmp_path / "memory",
    )

    def conflict_decision(messages, _kwargs, _llm):
        payload = json.loads(messages[1]["content"])
        return {
            "decisions": [
                {
                    "source_uri": payload["candidates"][0]["source_uri"],
                    "relation": "conflicts",
                    "reason": "same subject changed from English to Chinese",
                }
            ]
        }

    llm = _FakeRouterLLM(
        [
            {
                "destination": "ordinary_memory",
                "reason": "durable response preference",
            },
            {
                "content": "以后使用英文回答计算处理成本的问题。",
                "memory_type": "preference",
                "reason": "durable response preference",
            },
            {
                "destination": "ordinary_memory",
                "reason": "durable response preference",
            },
            {
                "content": "以后使用中文回答计算处理成本的问题。",
                "memory_type": "preference",
                "reason": "updated durable response preference",
            },
            conflict_decision,
        ]
    )

    execute_tool_batch(
        [_build_tool_call("memory", {"content": "以后使用英文回答计算处理成本的问题。"})],
        _make_context(llm=llm, memory_manager=memory_manager),
    )
    wait_for_memory_tasks(timeout=5)

    execute_tool_batch(
        [_build_tool_call("memory", {"content": "以后改为使用中文回答计算处理成本的问题。"})],
        _make_context(llm=llm, memory_manager=memory_manager),
    )
    wait_for_memory_tasks(timeout=5)

    context = memory_manager.build_memory_context(query="计算处理成本", ov_session_id=None)
    assert "以后使用中文回答计算处理成本的问题" in context
    assert "以后使用英文回答计算处理成本的问题" not in context

    active_hits = memory_manager.recall_memory(query="计算处理成本", memory_type="preference", k=5)
    assert len(active_hits) == 1
    assert active_hits[0].archived is False
    assert "中文回答" in active_hits[0].content

    all_hits = memory_manager.recall_memory(
        query="计算处理成本",
        memory_type="preference",
        k=5,
        include_archived=True,
    )
    assert len(all_hits) == 2
    assert any(hit.archived and "英文回答" in hit.content for hit in all_hits)
    assert [call["kwargs"]["request_label"] for call in llm.calls] == [
        "memory_route",
        "memory_extract_ordinary",
        "memory_route",
        "memory_extract_ordinary",
        "memory_relation_ordinary",
    ]


def test_memory_tool_returns_before_router_completes():
    llm = _BlockingRouterLLM()
    started_at = time.perf_counter()

    result = execute_tool_batch(
        [_build_tool_call("memory", {"content": "记住：后台分类不要阻塞主流程。"})],
        _make_context(llm=llm),
    )

    elapsed = time.perf_counter() - started_at
    payload = json.loads(result.messages[0]["content"])
    assert payload["queued"] is True
    assert elapsed < 1.0
    assert llm.started.wait(timeout=1)
    llm.release.set()
    wait_for_memory_tasks(timeout=5)
    assert [call["kwargs"]["request_label"] for call in llm.calls] == [
        "memory_route",
        "memory_extract_ordinary",
    ]


def test_memory_schema_keeps_model_visible_arguments_minimal():
    schema = TOOL_SPECS["memory"].schema["function"]["parameters"]
    properties = schema["properties"]

    assert schema["required"] == ["content"]
    assert set(properties) == {"content"}
    assert "intent" not in properties
    assert "context_summary" not in properties
    assert "ontology_id" not in properties
    assert "operation_key" not in properties
    assert "entity_key" not in properties
    assert "memory_type" not in properties
    assert "kind" not in properties
    assert "confidence" not in properties


def test_process_memory_request_uses_operation_evolution_buffer(monkeypatch, tmp_path):
    buffer = ProposalBuffer(str(tmp_path / "operations-evolution"))
    monkeypatch.setattr("agentic.tools.memory.get_proposal_buffer", lambda: buffer)
    llm = _FakeRouterLLM(
        [
            {
                "destination": "operation_evolution",
                "reason": "reusable operation procedure",
            },
            {
                "content": "以后执行流程A时，先校验物料编号是否为空，再查询供应商。",
                "kind": "procedure",
                "ontology_id": "demo",
                "operation_key": "流程A",
                "entity_key": None,
                "reason": "reusable operation procedure",
            },
        ]
    )

    result = execute_tool_batch(
        [
            _build_tool_call(
                "memory",
                {
                    "content": "以后执行流程A时，先校验物料编号是否为空，再查询供应商。",
                },
            )
        ],
        _make_context(
            llm=llm,
            workspace_id="ws-1",
            selected_ontology_id="demo",
            read_documents=merge_read_documents_registry(
                None,
                [
                    build_read_document_record(
                        file_path="ontologies/demo/operations/流程A.md",
                        content="---\ndescription: 供应商查询前的物料校验流程\n---\n# 流程A\n",
                        source="ontology_context",
                        turn_id=1,
                    ),
                    build_read_document_record(
                        file_path="ontologies/demo/objects/TargetItem.md",
                        content="---\ndescription: 物料主数据实体\n---\n# TargetItem\n",
                        source="ontology_context",
                        turn_id=1,
                    ),
                    build_read_document_record(
                        file_path="ontologies/demo/manifest.yaml",
                        content="name: demo\n",
                        source="ontology_context",
                        turn_id=1,
                    ),
                ],
            ),
        ),
    )

    content = result.messages[0]["content"]
    payload = json.loads(content)
    assert payload["queued"] is True
    wait_for_memory_tasks(timeout=5)
    op_dir = buffer.op_dir("demo", "流程A", workspace_id="ws-1")
    assert (op_dir / "proposals.jsonl").exists()
    proposal = json.loads((op_dir / "proposals.jsonl").read_text(encoding="utf-8").strip())
    assert proposal["source"] == "active_chat"
    assert proposal["kind"] == "procedure"
    assert proposal["content"] == "以后执行流程A时，先校验物料编号是否为空，再查询供应商。"
    assert [call["kwargs"]["request_label"] for call in llm.calls] == [
        "memory_route",
        "memory_extract_operation",
    ]
    router_payload = json.loads(llm.calls[0]["messages"][1]["content"])
    extractor_input = llm.calls[1]["messages"][1]["content"]
    assert router_payload == {"content": "以后执行流程A时，先校验物料编号是否为空，再查询供应商。"}
    assert "原始记忆内容：以后执行流程A时，先校验物料编号是否为空，再查询供应商。" in extractor_input
    assert "用户选取的本体：demo" in extractor_input
    assert "已读取流程：\n- 名称：流程A；描述：供应商查询前的物料校验流程" in extractor_input
    assert "已读取实体：\n- 名称：TargetItem；描述：物料主数据实体" in extractor_input
    assert "unique_recent_operation_key" not in extractor_input
    assert "unique_recent_entity_key" not in extractor_input
    assert "recent_documents" not in extractor_input
    assert "doc_type" not in extractor_input
    assert "ontologies/demo" not in extractor_input


def test_operation_memory_extractor_skips_compact_key_lines(monkeypatch, tmp_path):
    buffer = ProposalBuffer(str(tmp_path / "operations-evolution"))
    monkeypatch.setattr("agentic.tools.memory.get_proposal_buffer", lambda: buffer)
    llm = _FakeRouterLLM(
        [
            {
                "destination": "operation_evolution",
                "reason": "reusable operation procedure",
            },
            {
                "content": "以后执行流程A时，先校验物料编号。",
                "kind": "procedure",
                "ontology_id": "demo",
                "operation_key": "流程A",
                "entity_key": None,
                "reason": "reusable operation procedure",
            },
        ]
    )

    result = execute_tool_batch(
        [_build_tool_call("memory", {"content": "以后执行流程A时，先校验物料编号。"})],
        _make_context(
            llm=llm,
            workspace_id="ws-1",
            selected_ontology_id="demo",
            read_documents=merge_read_documents_registry(
                None,
                [
                    build_read_document_record(
                        file_path="ontologies/demo/operations/流程A.md",
                        content="operation_key: 流程A\n\ncontent:\n# 流程A\n校验物料编号。",
                        source="ontology_context",
                        turn_id=1,
                    ),
                    build_read_document_record(
                        file_path="ontologies/demo/objects/TargetItem.md",
                        content="object_key: TargetItem\n\ncontent:\n# 物料\n物料主数据。",
                        source="ontology_context",
                        turn_id=1,
                    ),
                ],
            ),
        ),
    )

    assert json.loads(result.messages[0]["content"])["queued"] is True
    wait_for_memory_tasks(timeout=5)
    extractor_input = llm.calls[1]["messages"][1]["content"]
    assert "名称：流程A；描述：校验物料编号。" in extractor_input
    assert "名称：TargetItem；描述：物料" in extractor_input
    assert "operation_key:" not in extractor_input
    assert "object_key:" not in extractor_input


def test_operation_evolution_relation_merges_procedure_duplicate(monkeypatch, tmp_path):
    buffer = ProposalBuffer(str(tmp_path / "operations-evolution"))
    existing = buffer.append(
        workspace_id="ws-1",
        ontology_id="demo",
        operation_key="流程A",
        kind="procedure",
        content="流程A执行前先校验物料编号不能为空。",
        source="active_chat",
    )
    monkeypatch.setattr("agentic.tools.memory.get_proposal_buffer", lambda: buffer)
    llm = _FakeRouterLLM(
        [
            {
                "destination": "operation_evolution",
                "reason": "reusable operation procedure",
            },
            {
                "content": "以后流程A执行前要检查物料编号不能为空。",
                "kind": "procedure",
                "ontology_id": "demo",
                "operation_key": "流程A",
                "entity_key": None,
                "reason": "same procedure",
            },
            {
                "decisions": [
                    {
                        "fingerprint": existing.fingerprint,
                        "relation": "duplicate",
                        "reason": "same pre-check",
                    }
                ]
            },
        ]
    )

    result = execute_tool_batch(
        [_build_tool_call("memory", {"content": "记住：以后流程A执行前要检查物料编号不能为空。"})],
        _make_context(llm=llm, workspace_id="ws-1", selected_ontology_id="demo"),
    )

    assert json.loads(result.messages[0]["content"])["queued"] is True
    wait_for_memory_tasks(timeout=5)
    aggregates = buffer.list_aggregates("demo", "流程A", workspace_id="ws-1", statuses=("pending",))
    assert len(aggregates) == 1
    assert aggregates[0].fingerprint == existing.fingerprint
    assert aggregates[0].occurrences == 2
    assert [call["kwargs"]["request_label"] for call in llm.calls] == [
        "memory_route",
        "memory_extract_operation",
        "memory_relation_operation",
    ]


def test_operation_evolution_relation_replaces_same_scope_schema(monkeypatch, tmp_path):
    buffer = ProposalBuffer(str(tmp_path / "operations-evolution"))
    existing = buffer.append(
        workspace_id="ws-1",
        ontology_id="demo",
        operation_key=None,
        kind="schema",
        entity_key="TargetItem",
        content="TargetItem 的物料编码字段是 code。",
        source="active_chat",
    )
    monkeypatch.setattr("agentic.tools.memory.get_proposal_buffer", lambda: buffer)
    llm = _FakeRouterLLM(
        [
            {
                "destination": "operation_evolution",
                "reason": "entity schema correction",
            },
            {
                "content": "TargetItem 的物料编码字段是 TargetItem编码。",
                "kind": "schema",
                "ontology_id": "demo",
                "operation_key": None,
                "entity_key": "TargetItem",
                "reason": "correct field mapping",
            },
            {
                "decisions": [
                    {
                        "fingerprint": existing.fingerprint,
                        "relation": "conflicts",
                        "reason": "field names disagree",
                    }
                ]
            },
        ]
    )

    result = execute_tool_batch(
        [_build_tool_call("memory", {"content": "记住：TargetItem 的物料编码字段是 TargetItem编码。"})],
        _make_context(llm=llm, workspace_id="ws-1", selected_ontology_id="demo"),
    )

    assert json.loads(result.messages[0]["content"])["queued"] is True
    wait_for_memory_tasks(timeout=5)
    all_schema = buffer.list_aggregates("demo", None, workspace_id="ws-1")
    old = next(agg for agg in all_schema if agg.fingerprint == existing.fingerprint)
    active = buffer.recall(
        "demo",
        workspace_id="ws-1",
        operation_key=None,
        kind="schema",
        entity_key="TargetItem",
        k=5,
    )
    assert old.status == "rejected"
    assert len(active) == 1
    assert active[0].canonical_content == "TargetItem 的物料编码字段是 TargetItem编码。"
    assert active[0].fingerprint != existing.fingerprint
    assert [call["kwargs"]["request_label"] for call in llm.calls] == [
        "memory_route",
        "memory_extract_operation",
        "memory_relation_operation",
    ]


def test_operation_evolution_does_not_compare_schema_across_operations(monkeypatch, tmp_path):
    buffer = ProposalBuffer(str(tmp_path / "operations-evolution"))
    buffer.append(
        workspace_id="ws-1",
        ontology_id="demo",
        operation_key="流程B",
        kind="schema",
        entity_key="TargetItem",
        content="流程B中 TargetItem 的物料编码字段是 code。",
        source="active_chat",
    )
    monkeypatch.setattr("agentic.tools.memory.get_proposal_buffer", lambda: buffer)
    llm = _FakeRouterLLM(
        [
            {
                "destination": "operation_evolution",
                "reason": "operation-scoped schema correction",
            },
            {
                "content": "流程A中 TargetItem 的物料编码字段是 TargetItem编码。",
                "kind": "schema",
                "ontology_id": "demo",
                "operation_key": "流程A",
                "entity_key": "TargetItem",
                "reason": "operation-specific field mapping",
            },
        ]
    )

    result = execute_tool_batch(
        [_build_tool_call("memory", {"content": "记住：流程A中 TargetItem 的物料编码字段是 TargetItem编码。"})],
        _make_context(llm=llm, workspace_id="ws-1", selected_ontology_id="demo"),
    )

    assert json.loads(result.messages[0]["content"])["queued"] is True
    wait_for_memory_tasks(timeout=5)
    assert buffer.list_aggregates("demo", "流程A", workspace_id="ws-1", statuses=("pending",))
    assert [call["kwargs"]["request_label"] for call in llm.calls] == [
        "memory_route",
        "memory_extract_operation",
    ]


def test_process_memory_request_stores_object_level_schema_without_operation(monkeypatch, tmp_path):
    buffer = ProposalBuffer(str(tmp_path / "operations-evolution"))
    monkeypatch.setattr("agentic.tools.memory.get_proposal_buffer", lambda: buffer)
    llm = _FakeRouterLLM(
        [
            {
                "destination": "operation_evolution",
                "reason": "entity schema correction",
            },
            {
                "content": "TargetItem 的物料编码字段是 TargetItem编码。",
                "kind": "schema",
                "ontology_id": "demo",
                "operation_key": None,
                "entity_key": "TargetItem",
                "reason": "general entity schema correction",
            },
        ]
    )

    result = execute_tool_batch(
        [_build_tool_call("memory", {"content": "记住：TargetItem 的物料编码字段是 TargetItem编码。"})],
        _make_context(
            llm=llm,
            workspace_id="ws-1",
            selected_ontology_id="demo",
        ),
    )

    payload = json.loads(result.messages[0]["content"])
    assert payload["queued"] is True
    wait_for_memory_tasks(timeout=5)
    object_schema_dir = buffer.op_dir("demo", None, workspace_id="ws-1")
    proposal = json.loads((object_schema_dir / "proposals.jsonl").read_text(encoding="utf-8").strip())
    assert proposal["operation_key"] is None
    assert proposal["kind"] == "schema"
    assert proposal["entity_key"] == "TargetItem"


def test_operation_memory_without_operation_key_stays_in_evolution(monkeypatch, tmp_path):
    buffer = ProposalBuffer(str(tmp_path / "operations-evolution"))
    monkeypatch.setattr("agentic.tools.memory.get_proposal_buffer", lambda: buffer)
    memory_manager = _FakeMemoryManager()
    llm = _FakeRouterLLM(
        [
            {
                "destination": "operation_evolution",
                "reason": "ontology-level procedure",
            },
            {
                "content": "执行这个本体内流程时，先确认输入对象是否完整。",
                "kind": "procedure",
                "ontology_id": "demo",
                "operation_key": None,
                "entity_key": None,
                "reason": "applies across the ontology",
            },
        ]
    )

    result = execute_tool_batch(
        [_build_tool_call("memory", {"content": "记住：执行这个本体内流程时，先确认输入对象是否完整。"})],
        _make_context(
            llm=llm,
            memory_manager=memory_manager,
            workspace_id="ws-1",
            selected_ontology_id="demo",
        ),
    )

    assert json.loads(result.messages[0]["content"])["queued"] is True
    wait_for_memory_tasks(timeout=5)
    assert memory_manager.stored == []
    ontology_dir = buffer.op_dir("demo", None, workspace_id="ws-1")
    proposal = json.loads((ontology_dir / "proposals.jsonl").read_text(encoding="utf-8").strip())
    assert proposal["operation_key"] is None
    assert proposal["kind"] == "procedure"
    assert proposal["content"] == "执行这个本体内流程时，先确认输入对象是否完整。"


def test_tool_specific_usage_rules_live_in_tool_schemas():
    ontology_schema = TOOL_SPECS["ontology_context"].schema["function"]
    ontology_description = ontology_schema["description"]
    skill_schema = TOOL_SPECS["skill"].schema["function"]
    business_schema = TOOL_SPECS["business_data_search"].schema["function"]
    execute_schema = TOOL_SPECS["execute_script"].schema["function"]
    memory_schema = TOOL_SPECS["memory"].schema["function"]
    spawn_schema = TOOL_SPECS["spawn_sub_agent"].schema["function"]

    ontology_resource_property = ontology_schema["parameters"]["properties"]["resource"]
    ontology_resource_description = ontology_resource_property["description"]
    assert "root returns ontology metadata and catalogs" in ontology_resource_description
    assert "operation returns operation content by key" in ontology_resource_description
    assert "object returns object/entity definition by key" in ontology_resource_description
    assert "asset" not in ontology_resource_property["enum"]
    assert "asset" not in ontology_resource_description
    assert "resource='root'" not in ontology_description
    assert "semantic keys" not in ontology_description
    assert "file paths" not in ontology_description
    assert "Load each needed ontology resource once" in ontology_description

    skill_description = skill_schema["description"]
    assert "materially helps the task" in skill_description
    assert "does not execute scripts" in skill_description
    assert "semantic resource keys" not in skill_description
    assert "instead of file paths" not in skill_description

    business_description = business_schema["description"]
    business_properties = business_schema["parameters"]["properties"]
    assert "concrete domain data" in business_description
    assert "prefer loading the matching object definition with ontology_context(resource='object')" in business_description
    assert "one entity per call" in business_description
    assert "empty result" in business_description
    assert "same retrieval goal" in business_description
    assert "Generic template examples" not in business_description
    assert "objects_catalog, operation content, and root docs alone are not sufficient" not in business_description
    assert "Prefer reading the object via ontology_context(resource='object')" in business_properties["entity"]["description"]
    assert "validation" not in business_properties["entity"]["description"]
    assert "Never guess fields" in business_properties["query"]["description"]
    assert "all values" in business_properties["query"]["description"]

    assert "Use this only when the operation appears" in execute_schema["description"]
    assert "missing parameters" in execute_schema["description"]
    assert "file paths" not in execute_schema["description"]
    assert "script paths" not in execute_schema["parameters"]["properties"]["arguments"]["description"]

    assert "explicit user-provided memory item" in memory_schema["description"]
    assert "appropriate durable subsystem" in memory_schema["description"]
    assert "content" in memory_schema["parameters"]["properties"]
    assert "intent" not in memory_schema["parameters"]["properties"]
    assert "context_summary" not in memory_schema["parameters"]["properties"]

    spawn_description = spawn_schema["description"]
    spawn_tasks_description = spawn_schema["parameters"]["properties"]["tasks"]["description"]
    assert "independent fan-out work" in spawn_description
    assert "logical fan-out" in spawn_description
    assert "dependent or sequential tasks" in spawn_description
    assert "dependency/dependencies fields" not in spawn_description
    assert "dependency/dependencies fields" not in spawn_tasks_description


def test_registry_marks_status_error_payloads_as_tool_failures(monkeypatch):
    captured = []

    monkeypatch.setitem(
        TOOL_SPECS,
        "spawn_sub_agent",
        _make_mock_tool(
            "spawn_sub_agent",
            lambda **kwargs: json.dumps(
                {
                    "group_id": "group_error",
                    "status": "error",
                    "tasks": [],
                    "summary": "depth exceeded",
                },
                ensure_ascii=False,
            ),
            schema=TOOL_SPECS["spawn_sub_agent"].schema,
            category=TOOL_SPECS["spawn_sub_agent"].category,
        ),
    )

    with stream_writer_context(captured.append):
        result = execute_tool_batch(
            [_build_tool_call("spawn_sub_agent", {"tasks": [{"id": "c1", "task": "child"}]})],
            _make_context(),
        )

    assert json.loads(result.messages[0]["content"])["status"] == "error"
    finished_events = [event for event in captured if event["data"]["type"] == "tool.finished"]
    assert len(finished_events) == 1
    assert finished_events[0]["data"]["payload"]["status"] == "error"


def test_registry_reports_invalid_tool_argument_json_before_required_fields():
    malformed_call = ToolInvocation(
        name="spawn_sub_agent",
        args={_ARGUMENT_PARSE_ERROR_KEY: '{"tasks": [id": "cycle_impact"}'},
        tool_call_id="call_bad_json",
    )

    result = execute_tool_batch([malformed_call], _make_context())

    content = result.messages[0]["content"]
    assert "arguments are not valid JSON" in content
    assert "missing required argument" not in content


def test_execute_tool_batch_accepts_internal_tool_invocation(monkeypatch):
    observed = {}

    def handler(skill_id, tool_call_id=None):
        observed["skill_id"] = skill_id
        observed["tool_call_id"] = tool_call_id
        return "ran {}".format(skill_id)

    monkeypatch.setitem(
        TOOL_SPECS,
        "unit_tool",
        _make_mock_tool(
            "unit_tool",
            handler,
            schema={
                "type": "function",
                "function": {
                    "name": "unit_tool",
                    "description": "unit tool",
                    "parameters": {
                        "type": "object",
                        "properties": {"skill_id": {"type": "string"}},
                        "required": ["skill_id"],
                    },
                },
            },
            category="regular",
        ),
    )

    result = execute_tool_batch(
        [
            ToolInvocation(
                name="unit_tool",
                args={"skill_id": "finance"},
                tool_call_id="jiuwen_skill_1",
                index=0,
            )
        ],
        _make_context(),
    )

    assert observed == {"skill_id": "finance", "tool_call_id": "jiuwen_skill_1"}
    assert result.messages == [
        {
            "role": "tool",
            "content": "ran finance",
            "tool_call_id": "jiuwen_skill_1",
            "name": "unit_tool",
        }
    ]


def test_execute_tool_batch_stops_after_terminal_outcome(monkeypatch):
    calls = []

    class _TerminalUnitTool:
        name = "terminal_unit_tool"
        schema = {
            "type": "function",
            "function": {
                "name": "terminal_unit_tool",
                "description": "terminal unit tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        allowed_callers = {"main"}
        concurrency_safe = False
        max_parallelism = 1
        category = "regular"

        def execute(self, *, args, context, tool_call_id=None, event_emitter=None):
            del args, context, event_emitter
            calls.append(("terminal", tool_call_id))
            return ToolOutcome(
                content="awaiting input",
                state_updates={"pending_user_interaction": {"id": "interaction_1"}},
                terminal=True,
            )

    class _LaterUnitTool:
        name = "later_unit_tool"
        schema = {
            "type": "function",
            "function": {
                "name": "later_unit_tool",
                "description": "later unit tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        allowed_callers = {"main"}
        concurrency_safe = False
        max_parallelism = 1
        category = "regular"

        def execute(self, *, args, context, tool_call_id=None, event_emitter=None):
            del args, context, event_emitter
            calls.append(("later", tool_call_id))
            return ToolOutcome(content="should not run")

    monkeypatch.setitem(TOOL_SPECS, "terminal_unit_tool", _TerminalUnitTool())
    monkeypatch.setitem(TOOL_SPECS, "later_unit_tool", _LaterUnitTool())

    result = execute_tool_batch(
        [
            ToolInvocation(name="terminal_unit_tool", args={}, tool_call_id="call_terminal", index=0),
            ToolInvocation(name="later_unit_tool", args={}, tool_call_id="call_later", index=1),
        ],
        _make_context(),
    )

    assert calls == [("terminal", "call_terminal")]
    assert result.terminal is True
    assert result.state_updates == {"pending_user_interaction": {"id": "interaction_1"}}
    assert [message["name"] for message in result.messages] == ["terminal_unit_tool"]


def test_execute_tool_batch_rejects_legacy_openai_style_tool_call_dict():
    legacy_tool_call = {
        "id": "call_legacy",
        "type": "function",
        "function": {
            "name": "spawn_sub_agent",
            "arguments": json.dumps({"tasks": [{"id": "c1", "task": "child"}]}),
        },
    }

    with pytest.raises(TypeError, match="ToolInvocation"):
        execute_tool_batch([legacy_tool_call], _make_context())


def test_registry_propagates_workflow_cancellation_from_tool(monkeypatch):
    def raising_handler(**kwargs):
        del kwargs
        raise WorkflowCancelledError(
            "Workflow cancelled by user",
            details={"run_id": "run-tools"},
        )

    monkeypatch.setitem(
        TOOL_SPECS,
        "spawn_sub_agent",
        _make_mock_tool(
            "spawn_sub_agent",
            raising_handler,
            schema=TOOL_SPECS["spawn_sub_agent"].schema,
            category=TOOL_SPECS["spawn_sub_agent"].category,
        ),
    )

    with pytest.raises(WorkflowCancelledError, match="Workflow cancelled by user"):
        execute_tool_batch(
            [_build_tool_call("spawn_sub_agent", {"tasks": [{"id": "c1", "task": "child"}]})],
            _make_context(),
        )


def test_registry_enforces_tool_whitelist_for_sub_agents(tmp_path, monkeypatch):
    _write_demo_skill(tmp_path)
    _patch_test_skill_registry(monkeypatch, tmp_path)

    subagent_tools = [tool["function"]["name"] for tool in get_tools_schema("subagent")]
    removed_tool = "ask" + "_user"

    assert "read_file" not in subagent_tools
    assert "list_files" not in subagent_tools
    assert "ontology_context" in subagent_tools
    assert "skill" in subagent_tools
    assert "skill_context" not in subagent_tools
    assert "business_data_search" in subagent_tools
    assert "web_search" not in subagent_tools
    assert "web_search" in [
        tool["function"]["name"]
        for tool in get_tools_schema("subagent", include_web_search=True)
    ]
    assert "execute_script" in subagent_tools
    assert "memory" not in subagent_tools
    assert TOOL_SPECS["memory"].allowed_callers == {"main"}
    assert "request_user_interaction" not in subagent_tools
    assert TOOL_SPECS["request_user_interaction"].allowed_callers == {"main"}
    assert "store_memory" not in TOOL_SPECS
    assert "recall_memory" not in TOOL_SPECS
    assert "propose_operation_patch" not in TOOL_SPECS
    assert "spawn_sub_agent" in subagent_tools
    assert removed_tool not in subagent_tools


def test_registry_no_longer_registers_removed_hitl_tool(tmp_path, monkeypatch):
    _patch_empty_skill_registry(monkeypatch, tmp_path)

    removed_tool = "ask" + "_user"

    assert removed_tool not in TOOL_SPECS
    assert is_tool_available(removed_tool) is False
    assert all(
        tool["function"]["name"] != removed_tool
        for tool in get_tools_schema("main")
    )
    assert all(
        tool["function"]["name"] != removed_tool
        for tool in get_tools_schema("subagent")
    )


def test_registry_exposes_request_user_interaction_to_main_only(tmp_path, monkeypatch):
    _patch_empty_skill_registry(monkeypatch, tmp_path)

    main_tools = [tool["function"]["name"] for tool in get_tools_schema("main")]
    subagent_tools = [tool["function"]["name"] for tool in get_tools_schema("subagent")]

    assert "request_user_interaction" in main_tools
    assert "request_user_interaction" not in subagent_tools


def test_registry_exposes_single_subagent_tool_for_main_agent(tmp_path, monkeypatch):
    _patch_empty_skill_registry(monkeypatch, tmp_path)

    main_tools = [tool["function"]["name"] for tool in get_tools_schema("main")]

    assert "spawn_sub_agent" in main_tools
    assert "spawnsubagent" not in main_tools
    assert "evaluate_results" not in main_tools


def test_spawn_sub_agent_schema_contains_invocation_contract_not_plan_sync_guidance():
    schema = TOOL_SPECS["spawn_sub_agent"].schema["function"]
    tasks_schema = schema["parameters"]["properties"]["tasks"]
    task_properties = tasks_schema["items"]["properties"]
    task_id_description = task_properties["id"]["description"]
    task_description = task_properties["task"]["description"]
    context_description = task_properties["context"]["description"]

    assert "active plan task ID" not in task_id_description
    assert "correlate each sub-agent task" in task_id_description
    assert "same logical fan-out in one call" in schema["description"]
    assert "mutually independent" in tasks_schema["description"]
    assert "dependency/dependencies fields" not in tasks_schema["description"]
    assert "concrete goal" in task_description
    assert "required input values" in task_description
    assert "expected output format" in task_description
    assert "ontology_id and operation_key" in context_description
    assert "sub_operation_keys" in context_description


def test_registry_can_hide_memory_tools_for_main_agent(tmp_path, monkeypatch):
    _write_demo_skill(tmp_path)
    _patch_test_skill_registry(monkeypatch, tmp_path)

    main_tools = [
        tool["function"]["name"]
        for tool in get_tools_schema("main", include_memory=False)
    ]

    assert "skill" in main_tools
    assert "skill_context" not in main_tools
    assert "memory" not in main_tools
    assert "store_memory" not in main_tools
    assert "recall_memory" not in main_tools


def test_registry_hides_skill_schema_when_no_skills_are_registered(tmp_path, monkeypatch):
    _patch_empty_skill_registry(monkeypatch, tmp_path)

    main_tools = [tool["function"]["name"] for tool in get_tools_schema("main")]
    subagent_tools = [tool["function"]["name"] for tool in get_tools_schema("subagent")]
    regular_tools = [tool["function"]["name"] for tool in get_regular_tools_schema()]

    assert "skill" not in main_tools
    assert "skill" not in subagent_tools
    assert "skill" not in regular_tools
    assert "skill" in TOOL_SPECS


def test_registry_exposes_skill_schema_when_skills_are_registered(tmp_path, monkeypatch):
    _write_demo_skill(tmp_path)
    _patch_test_skill_registry(monkeypatch, tmp_path)

    main_tools = [tool["function"]["name"] for tool in get_tools_schema("main")]
    subagent_tools = [tool["function"]["name"] for tool in get_tools_schema("subagent")]
    regular_tools = [tool["function"]["name"] for tool in get_regular_tools_schema()]

    assert "skill" in main_tools
    assert "skill" in subagent_tools
    assert "skill" in regular_tools


def test_registry_hides_web_search_until_enabled_for_schema_exposure():
    default_main_tools = [tool["function"]["name"] for tool in get_tools_schema("main")]
    enabled_main_tools = [
        tool["function"]["name"]
        for tool in get_tools_schema("main", include_web_search=True)
    ]

    assert "web_search" in TOOL_SPECS
    assert "web_search" not in default_main_tools
    assert "web_search" in enabled_main_tools


def test_registry_returns_validation_error_for_web_search_missing_query():
    result = execute_tool_batch(
        [_build_tool_call("web_search", {}, call_id="call_web_1")],
        _make_context(),
    )

    assert result.messages[0]["name"] == "web_search"
    assert "missing required argument(s): query" in result.messages[0]["content"]


def test_file_tools_are_not_registered_as_model_tools():
    main_tools = [tool["function"]["name"] for tool in get_tools_schema("main")]
    subagent_tools = [tool["function"]["name"] for tool in get_tools_schema("subagent")]

    assert "read_file" not in TOOL_SPECS
    assert "list_files" not in TOOL_SPECS
    assert is_tool_available("read_file") is False
    assert is_tool_available("list_files") is False
    assert "read_file" not in main_tools
    assert "list_files" not in main_tools
    assert "read_file" not in subagent_tools
    assert "list_files" not in subagent_tools


def test_registry_uses_barriered_parallel_execution(monkeypatch):
    execution_log = []

    def make_handler(name, sleep_for):
        def handler():
            execution_log.append(f"{name}:start")
            time.sleep(sleep_for)
            execution_log.append(f"{name}:end")
            return name

        return handler

    safe_schema = {
        "type": "function",
        "function": {
            "name": "safe_batch_tool",
            "description": "safe",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    unsafe_schema = {
        "type": "function",
        "function": {
            "name": "unsafe_tool",
            "description": "unsafe",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    trailing_schema = {
        "type": "function",
        "function": {
            "name": "safe_trailing_tool",
            "description": "safe",
            "parameters": {"type": "object", "properties": {}},
        },
    }

    monkeypatch.setitem(
        TOOL_SPECS,
        "safe_batch_tool",
        _make_mock_tool("safe_batch_tool", make_handler("safe_batch_tool", 0.02), concurrency_safe=True, schema=safe_schema),
    )
    monkeypatch.setitem(
        TOOL_SPECS,
        "unsafe_tool",
        _make_mock_tool("unsafe_tool", make_handler("unsafe_tool", 0.01), concurrency_safe=False, schema=unsafe_schema),
    )
    monkeypatch.setitem(
        TOOL_SPECS,
        "safe_trailing_tool",
        _make_mock_tool("safe_trailing_tool", make_handler("safe_trailing_tool", 0.01), concurrency_safe=True, schema=trailing_schema),
    )

    result = execute_tool_batch(
        [
            _build_tool_call("safe_batch_tool", {}, call_id="call_1"),
            _build_tool_call("safe_batch_tool", {}, call_id="call_2"),
            _build_tool_call("unsafe_tool", {}, call_id="call_3"),
            _build_tool_call("safe_trailing_tool", {}, call_id="call_4"),
        ],
        _make_context(),
    )

    assert [message["name"] for message in result.messages] == [
        "safe_batch_tool",
        "safe_batch_tool",
        "unsafe_tool",
        "safe_trailing_tool",
    ]
    assert execution_log.index("unsafe_tool:start") > execution_log.index("safe_batch_tool:end")
    assert execution_log.index("safe_trailing_tool:start") > execution_log.index("unsafe_tool:end")
