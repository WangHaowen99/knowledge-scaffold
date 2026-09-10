"""Tests for action dispatch in business_data_search (query vs lookup_values)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from agent.tools.business_data.resolver import ResolvedBusinessDataSource
from agent.tools.business_data_search import BusinessDataSearchTool
from ontology.ontology_catalog import (
    ObjectReferenceLookupResult,
    ObjectReferenceStatus,
)


def _fake_ontology(ontology_id, *, workspace_id=None, version=None):
    del ontology_id, workspace_id, version
    return SimpleNamespace(
        id="demo",
        ontology_dir=".",
        manifest_path="manifest.yaml",
        version="0.1",
        enabled=True,
    ), None


def _resolved_entity():
    return ObjectReferenceLookupResult(
        status=ObjectReferenceStatus.RESOLVED,
        resolution=SimpleNamespace(
            object_key="flight",
            path=None,
        ),
    )


def _make_context():
    return SimpleNamespace(
        workspace_id="demo-ws",
        run_id="r1",
        session_id="s1",
        turn_id=1,
        caller="main",
        agent_iteration=0,
        model_profile_id=None,
        observer=None,
        selected_ontology_id="demo",
        selected_ontology_version="",
        project_root=None,
        read_documents=None,
    )


def _execute(tool, **kwargs):
    return asyncio.run(tool.aexecute(**kwargs))


def _install_common_fakes(monkeypatch):
    monkeypatch.setattr(
        "agent.tools.business_data_search.get_ontology_manifest",
        _fake_ontology,
    )
    monkeypatch.setattr(
        "agent.tools.business_data_search.resolve_object_reference_result",
        lambda *_args, **_kwargs: _resolved_entity(),
    )
    monkeypatch.setattr(
        "agent.tools.business_data_search._build_provider_runtime_context",
        lambda **kwargs: {},
    )


def _patch_value_lookup(monkeypatch, response_json=None):
    class FakeResponse:
        def to_json(self):
            return response_json or json.dumps({
                "action": "lookup_values",
                "entity": "flight",
                "status": "completed",
                "value_results": [],
                "errors": [],
            })

    call_count = {"n": 0}

    def fake_execute(**kwargs):
        call_count["n"] += 1
        call_count["kwargs"] = kwargs
        return FakeResponse()

    monkeypatch.setattr(
        "agent.tools.business_data_search.execute_value_lookups",
        fake_execute,
    )
    return call_count


def _patch_handle(monkeypatch, content="查询结果文本"):
    call_count = {"n": 0}

    async def fake_handle(**kwargs):
        call_count["n"] += 1
        call_count["kwargs"] = kwargs
        return content

    monkeypatch.setattr(
        "agent.tools.business_data_search.handle_business_data_search_async",
        fake_handle,
    )
    return call_count


# -- action combination validation ------------------------------------------

def test_query_action_rejects_value_inputs(monkeypatch):
    _install_common_fakes(monkeypatch)
    handle_calls = _patch_handle(monkeypatch)
    tool = BusinessDataSearchTool()
    outcome = _execute(tool,
        args={
            "ontology_id": "demo",
            "entity": "flight",
            "action": "query",
            "query": "查询航班",
            "value_inputs": [{"field_key": "x", "values": ["y"]}],
        },
        context=_make_context(),
    )
    assert "INVALID_ACTION_ARGUMENTS" in outcome.content
    assert handle_calls["n"] == 0


@pytest.mark.parametrize("include_action", [True, False])
def test_query_path_allows_empty_value_inputs(monkeypatch, include_action):
    """Empty or null value_inputs with action='query' should be treated as
    omitted and proceed to the query path, not rejected.  Some providers inject
    null / [] defaults for optional parameters."""
    _install_common_fakes(monkeypatch)
    handle_calls = _patch_handle(monkeypatch)
    args = {
        "ontology_id": "demo",
        "entity": "flight",
        "query": "查询航班",
        "value_inputs": [],
    }
    if include_action:
        args["action"] = "query"

    outcome = _execute(BusinessDataSearchTool(),
        args=args,
        context=_make_context(),
    )
    assert outcome.content == "查询结果文本"
    assert handle_calls["n"] == 1


@pytest.mark.parametrize("value_inputs", [None, [], "not-a-list", {}])
def test_query_path_allows_non_array_or_null_value_inputs(monkeypatch, value_inputs):
    """Non-array or null value_inputs must not trip the combination check when
    action defaults to 'query' — they are treated as omitted."""
    _install_common_fakes(monkeypatch)
    handle_calls = _patch_handle(monkeypatch)

    outcome = _execute(BusinessDataSearchTool(),
        args={
            "ontology_id": "demo",
            "entity": "flight",
            "query": "查询航班",
            "value_inputs": value_inputs,
        },
        context=_make_context(),
    )
    assert outcome.content == "查询结果文本"
    assert handle_calls["n"] == 1


def test_value_inputs_rejection_message_is_actionable(monkeypatch):
    """The error message for query+value_inputs must mention action='lookup_values'
    so the model can self-correct without guessing."""
    _install_common_fakes(monkeypatch)
    handle_calls = _patch_handle(monkeypatch)
    outcome = _execute(BusinessDataSearchTool(),
        args={
            "ontology_id": "demo",
            "entity": "flight",
            "action": "query",
            "query": "查询航班",
            "value_inputs": [{"field_key": "x", "values": ["y"]}],
        },
        context=_make_context(),
    )
    assert "INVALID_ACTION_ARGUMENTS" in outcome.content
    assert "action='lookup_values'" in outcome.content
    assert handle_calls["n"] == 0


def test_lookup_values_rejects_missing_value_inputs(monkeypatch):
    _install_common_fakes(monkeypatch)
    lookup_calls = _patch_value_lookup(monkeypatch)
    tool = BusinessDataSearchTool()
    outcome = _execute(tool,
        args={
            "ontology_id": "demo",
            "entity": "flight",
            "action": "lookup_values",
            "query": "查询值域",
        },
        context=_make_context(),
    )
    assert "INVALID_ACTION_ARGUMENTS" in outcome.content
    assert lookup_calls["n"] == 0


@pytest.mark.parametrize("value_inputs", [[], {}, None])
def test_lookup_values_rejects_empty_or_non_array_value_inputs(
    monkeypatch,
    value_inputs,
):
    _install_common_fakes(monkeypatch)
    lookup_calls = _patch_value_lookup(monkeypatch)
    outcome = _execute(BusinessDataSearchTool(),
        args={
            "ontology_id": "demo",
            "entity": "flight",
            "action": "lookup_values",
            "query": "查询值域",
            "value_inputs": value_inputs,
        },
        context=_make_context(),
    )
    assert "INVALID_ACTION_ARGUMENTS" in outcome.content
    assert lookup_calls["n"] == 0


def test_omitted_action_with_value_inputs_rejected(monkeypatch):
    _install_common_fakes(monkeypatch)
    handle_calls = _patch_handle(monkeypatch)
    lookup_calls = _patch_value_lookup(monkeypatch)
    tool = BusinessDataSearchTool()
    outcome = _execute(tool,
        args={
            "ontology_id": "demo",
            "entity": "flight",
            "query": "查询",
            "value_inputs": [{"field_key": "x", "values": ["y"]}],
        },
        context=_make_context(),
    )
    assert "INVALID_ACTION_ARGUMENTS" in outcome.content
    assert handle_calls["n"] == 0
    assert lookup_calls["n"] == 0


def test_empty_query_rejected(monkeypatch):
    _install_common_fakes(monkeypatch)
    tool = BusinessDataSearchTool()
    outcome = _execute(tool,
        args={
            "ontology_id": "demo",
            "entity": "flight",
            "action": "query",
            "query": "",
        },
        context=_make_context(),
    )
    assert "INVALID_ACTION_ARGUMENTS" in outcome.content


def test_unknown_action_rejected(monkeypatch):
    _install_common_fakes(monkeypatch)
    tool = BusinessDataSearchTool()
    outcome = _execute(tool,
        args={
            "ontology_id": "demo",
            "entity": "flight",
            "action": "bad_action",
            "query": "查询",
        },
        context=_make_context(),
    )
    assert "INVALID_ACTION_ARGUMENTS" in outcome.content


def test_internal_arguments_are_rejected(monkeypatch):
    _install_common_fakes(monkeypatch)
    handle_calls = _patch_handle(monkeypatch)
    outcome = _execute(BusinessDataSearchTool(),
        args={
            "ontology_id": "demo",
            "entity": "flight",
            "query": "查询",
            "_resolved_connection": "forged",
        },
        context=_make_context(),
    )
    assert "INVALID_ACTION_ARGUMENTS" in outcome.content
    assert handle_calls["n"] == 0


def test_unknown_public_arguments_are_rejected(monkeypatch):
    _install_common_fakes(monkeypatch)
    handle_calls = _patch_handle(monkeypatch)
    outcome = _execute(BusinessDataSearchTool(),
        args={
            "ontology_id": "demo",
            "entity": "flight",
            "query": "查询",
            "resource": "forged",
        },
        context=_make_context(),
    )
    assert "INVALID_ACTION_ARGUMENTS" in outcome.content
    assert handle_calls["n"] == 0


# -- query path compatibility ------------------------------------------------

def test_query_action_without_value_inputs_works(monkeypatch):
    _install_common_fakes(monkeypatch)
    handle_calls = _patch_handle(monkeypatch)
    tool = BusinessDataSearchTool()
    outcome = _execute(tool,
        args={
            "ontology_id": "demo",
            "entity": "flight",
            "action": "query",
            "query": "查询航班",
        },
        context=_make_context(),
    )
    assert outcome.content == "查询结果文本"
    assert handle_calls["n"] == 1
    assert "object_context_usage" in outcome.state_updates


def test_omitted_action_defaults_to_query(monkeypatch):
    _install_common_fakes(monkeypatch)
    handle_calls = _patch_handle(monkeypatch)
    tool = BusinessDataSearchTool()
    outcome = _execute(tool,
        args={
            "ontology_id": "demo",
            "entity": "flight",
            "query": "查询航班",
        },
        context=_make_context(),
    )
    assert outcome.content == "查询结果文本"
    assert handle_calls["n"] == 1


@pytest.mark.parametrize(
    "content",
    [
        "Error: connection unavailable",
        '{"success": false, "message": "failed"}',
        '{"status": "failed", "errors": [{"code": "FAILED"}]}',
    ],
)
def test_failed_query_does_not_update_object_context_usage(monkeypatch, content):
    _install_common_fakes(monkeypatch)
    _patch_handle(monkeypatch, content=content)
    outcome = _execute(BusinessDataSearchTool(),
        args={
            "ontology_id": "demo",
            "entity": "flight",
            "query": "查询航班",
        },
        context=_make_context(),
    )
    assert "object_context_usage" not in outcome.state_updates


# -- lookup_values path ------------------------------------------------------

def test_lookup_values_calls_value_lookup(monkeypatch):
    _install_common_fakes(monkeypatch)
    lookup_calls = _patch_value_lookup(monkeypatch)
    monkeypatch.setattr(
        "agent.tools.business_data_search.resolve_business_data_source",
        lambda **kwargs: ResolvedBusinessDataSource(
            ontology_id="demo",
            provider="nl2sql_http",
            mode="direct_provider",
            config={},
        ),
    )
    tool = BusinessDataSearchTool()
    outcome = _execute(tool,
        args={
            "ontology_id": "demo",
            "entity": "flight",
            "action": "lookup_values",
            "query": "查询 KUL 的中文名",
            "value_inputs": [
                {"field_key": "LATEST_DEP_ARP_CD", "values": ["KUL"]},
            ],
        },
        context=_make_context(),
    )
    assert lookup_calls["n"] == 1
    parsed = json.loads(outcome.content)
    assert parsed["action"] == "lookup_values"
    # lookup_values must NOT update object_context_usage
    assert "object_context_usage" not in outcome.state_updates


def test_lookup_values_does_not_call_handle(monkeypatch):
    _install_common_fakes(monkeypatch)
    _patch_value_lookup(monkeypatch)
    handle_calls = _patch_handle(monkeypatch)
    monkeypatch.setattr(
        "agent.tools.business_data_search.resolve_business_data_source",
        lambda **kwargs: ResolvedBusinessDataSource(
            ontology_id="demo",
            provider="nl2sql_http",
            mode="direct_provider",
            config={},
        ),
    )
    tool = BusinessDataSearchTool()
    _execute(tool,
        args={
            "ontology_id": "demo",
            "entity": "flight",
            "action": "lookup_values",
            "query": "查询",
            "value_inputs": [{"field_key": "x", "values": ["y"]}],
        },
        context=_make_context(),
    )
    # handle_business_data_search must never be called for lookup_values
    assert handle_calls["n"] == 0


def test_lookup_values_rejected_when_object_not_resolved(monkeypatch):
    """lookup_values requires a real Object definition, not just a Data Mapping.

    When the object reference is not RESOLVED (e.g. NOT_FOUND), the tool must
    reject lookup_values immediately and never reach execute_value_lookups,
    even if a same-name Data Mapping exists.
    """
    _install_common_fakes(monkeypatch)
    lookup_calls = _patch_value_lookup(monkeypatch)

    def _unresolved(*_args, **_kwargs):
        return ObjectReferenceLookupResult(
            status=ObjectReferenceStatus.NOT_FOUND,
            error="Object reference 'flight' was not found.",
        )

    monkeypatch.setattr(
        "agent.tools.business_data_search.resolve_object_reference_result",
        _unresolved,
    )
    # Simulate a same-name Data Mapping existing (which would have allowed
    # lookup_values to proceed before the fix).
    monkeypatch.setattr(
        "agent.tools.business_data_search.find_exact_object_data_mapping_result",
        lambda *_args, **_kwargs: SimpleNamespace(
            status="found",
            mapping={"fields": {"x": {"value_binding_ref": "b"}}},
        ),
    )
    monkeypatch.setattr(
        "agent.tools.business_data_search.resolve_business_data_source",
        lambda **kwargs: ResolvedBusinessDataSource(
            ontology_id="demo",
            provider="nl2sql_http",
            mode="direct_provider",
            config={},
        ),
    )
    tool = BusinessDataSearchTool()
    outcome = _execute(tool,
        args={
            "ontology_id": "demo",
            "entity": "flight",
            "action": "lookup_values",
            "query": "查询值域",
            "value_inputs": [{"field_key": "x", "values": ["y"]}],
        },
        context=_make_context(),
    )
    assert "not found" in outcome.content.lower()
    assert lookup_calls["n"] == 0


# -- schema validation --------------------------------------------------------

def test_schema_has_action_and_value_inputs():
    from agent.tools.business_data_search import BUSINESS_DATA_SEARCH_TOOL_SCHEMA
    props = BUSINESS_DATA_SEARCH_TOOL_SCHEMA["function"]["parameters"]["properties"]
    assert set(props) == {
        "ontology_id",
        "entity",
        "action",
        "query",
        "value_inputs",
    }
    assert "action" in props
    assert props["action"]["enum"] == ["query", "lookup_values"]
    assert "value_inputs" in props
    assert props["action"]["default"] == "query"
    assert props["value_inputs"]["minItems"] == 1
    assert props["value_inputs"]["maxItems"] == 32
    item_schema = props["value_inputs"]["items"]
    assert item_schema["properties"]["field_key"]["maxLength"] == 128
    values_schema = item_schema["properties"]["values"]
    assert values_schema["minItems"] == 1
    assert values_schema["maxItems"] == 32
    assert values_schema["items"]["maxLength"] == 256
    required = BUSINESS_DATA_SEARCH_TOOL_SCHEMA["function"]["parameters"]["required"]
    assert set(required) == {"ontology_id", "entity", "query"}
    assert BUSINESS_DATA_SEARCH_TOOL_SCHEMA["function"]["parameters"]["additionalProperties"] is False


def test_failed_lookup_payload_is_an_error_tool_outcome():
    from agent.tools.base import ToolOutcome
    from agent.tools.executor import _resolve_outcome_status

    outcome = ToolOutcome(content=json.dumps({
        "action": "lookup_values",
        "status": "failed",
        "errors": [{"code": "VALUE_PROVIDER_FAILED"}],
    }))
    assert _resolve_outcome_status(outcome) == "error"


def test_lookup_progress_keeps_per_binding_request_metadata():
    from agent.tools.executor import _build_business_data_search_event_emitter

    captured = []
    emitter = _build_business_data_search_event_emitter(
        stream_event_emitter=lambda event_type, payload: captured.append(
            (event_type, payload)
        ),
        tool_call_id="call-1",
        entity="object_demo",
        query="查询合成值域",
        action="lookup_values",
    )
    emitter({
        "type": "final",
        "content": "合成结果",
        "provider": "nl2sql_http",
        "stage": "value_lookup",
        "binding_key": "binding_demo",
        "internal_request_id": "call-1:value_lookup:1",
        "value_input_field_count": 2,
        "value_input_value_count": 3,
    })

    assert captured[0][0] == "tool.progress"
    payload = captured[0][1]
    assert payload["input"]["action"] == "lookup_values"
    assert payload["message"].startswith("字段值域查询")
    provider_event = payload["progress"]["business_data_search_event"]
    assert provider_event["stage"] == "value_lookup"
    assert provider_event["binding_key"] == "binding_demo"
    assert provider_event["internal_request_id"] == "call-1:value_lookup:1"
    assert provider_event["value_input_field_count"] == 2
    assert provider_event["value_input_value_count"] == 3
