"""DataAgent control-plane schema tests; protocol tests live in shared providers."""

import json

import pytest
from agent.tools.business_data.adapters.dataagent_search import DataAgentSearchAdapter
from agent.tools.business_data.provider_registry import (
    PROVIDER_SPECS,
    get_provider_spec,
    list_provider_schemas,
    normalize_provider_config,
    publicize_provider_config,
)
from backend_synthetic_credentials import API_KEY, synthetic_secret
from shared_capabilities.business_data import AuthSpec, EffectiveConnection


def _connection() -> EffectiveConnection:
    return EffectiveConnection(
        version=1,
        provider="dataagent",
        endpoint="https://dataagent.example.net",
        auth=AuthSpec(type="none"),
        options={"timeout_seconds": 120},
    )


class _Response:
    status_code = 200
    text = ""

    def __init__(self, text: str):
        self._text = text

    def json(self):
        return {
            "task": {
                "status": {
                    "state": "TASK_STATE_COMPLETED",
                    "message": {"parts": [{"text": self._text}]},
                }
            }
        }


def test_dataagent_provider_is_registered_with_expected_schema():
    assert "dataagent" in PROVIDER_SPECS
    spec = get_provider_spec("dataagent")
    assert spec.factory is DataAgentSearchAdapter
    assert {field.name for field in spec.fields} == {
        "url",
        "token",
        "timeout_seconds",
        "max_retries",
    }


def test_dataagent_provider_schema_is_public():
    assert "dataagent" in {schema["name"] for schema in list_provider_schemas()}


def test_dataagent_config_normalization_and_secret_publication():
    normalized = normalize_provider_config("dataagent", {"url": "https://dataagent.example.com/", "token": API_KEY})
    assert normalized == {
        "url": "https://dataagent.example.com",
        "token": API_KEY,
        "timeout_seconds": 600,
        "max_retries": 3,
    }
    public = publicize_provider_config("dataagent", normalized)
    assert public["has_token"] is True
    assert "token" not in public


def test_dataagent_config_rejects_missing_url_and_unknown_fields():
    with pytest.raises(Exception, match="DataAgent 服务地址"):
        normalize_provider_config("dataagent", {"token": API_KEY})
    with pytest.raises(Exception, match="不支持字段"):
        normalize_provider_config("dataagent", {"url": "https://service.example.net", "scene": "foo"})


def test_dataagent_agent_output_preserves_baseline_structured_presentation():
    adapter = DataAgentSearchAdapter(_connection())

    def post_response(*_args, **_kwargs):
        return _Response(
            "{'sql': 'SELECT region FROM sales', 'columns': ['region'], "
            "'rows_preview': [('华东',)], 'confidence': 0.95, 'error': None}"
        )

    adapter._client._session.post = post_response

    events = list(adapter.search_stream(query="查询销售区域", entity="Sales"))
    output = json.loads(events[1]["content"])

    assert output == {
        "success": True,
        "data": {"columns": ["region"], "rows": [["华东"]]},
        "row_count": 1,
        "confidence": 0.95,
        "has_sql": True,
        "message": "查询成功，返回 1 行数据",
    }


def test_dataagent_agent_output_preserves_baseline_literal_failure_presentation():
    adapter = DataAgentSearchAdapter(_connection())

    def post_response(*_args, **_kwargs):
        return _Response(r"{'sql': '\x', 'columns': [invalid], 'confidence': None, 'error': None}")

    adapter._client._session.post = post_response

    events = list(adapter.search_stream(query="查询异常结果", entity="Sales"))
    output = json.loads(events[1]["content"])

    assert output == {
        "success": True,
        "data": {"columns": "[invalid]", "rows": []},
        "row_count": 0,
        "confidence": "None",
        "has_sql": True,
        "message": "查询成功，无数据返回",
    }


def test_dataagent_non_structured_response_preserves_full_provider_text():
    """When DataAgent returns non-structured text, the full text is preserved
    (no 500-char truncation) in the final JSON payload's message field."""
    adapter = DataAgentSearchAdapter(_connection())
    provider_text = "值域结果\n" + ("合成映射行\n" * 120)

    def post_response(*_args, **_kwargs):
        return _Response(provider_text)

    adapter._client._session.post = post_response

    events = list(adapter.search_stream(query="查询合成值域", entity="flight"))

    assert [event["type"] for event in events] == ["start", "final", "done"]
    output = json.loads(events[1]["content"])
    assert output["success"] is True
    assert output["message"] == provider_text


def test_dataagent_non_structured_response_truncates_oversized_text():
    """Free-text answers exceeding the budget are truncated, not passed
    through unbounded."""
    adapter = DataAgentSearchAdapter(_connection())
    oversized = "x" * 20000

    def post_response(*_args, **_kwargs):
        return _Response(oversized)

    adapter._client._session.post = post_response

    events = list(adapter.search_stream(query="查询", entity="flight"))
    output = json.loads(events[1]["content"])

    assert output["success"] is True
    message = output["message"]
    assert len(message) < len(oversized)
    assert message.endswith("...")
    assert len(message) <= 8100
