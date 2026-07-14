"""Provider registry for ontology-scoped business data sources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Type

from agent.tools.business_data.providers.base import OntologySearchProvider
from agent.tools.business_data.providers.dataagent_provider import DataAgentProvider
from agent.tools.business_data.providers.mindsdb_provider import MindsDBOntologySearchProvider
from agent.tools.business_data.providers.nl2sql_http_provider import NL2SQLHttpProvider
from agent.tools.business_data.providers.nl2sql_template import NL2SQLTemplateProvider
from agent.tools.business_data.providers.graph.hugegraph_provider import HugeGraphSearchProvider
from agent.tools.business_data.providers.graph.neo4j_provider import Neo4jGraphSearchProvider


@dataclass(frozen=True)
class ProviderField:
    name: str
    label: str
    type: str = "str"
    required: bool = False
    secret: bool = False
    default: Any = ""


@dataclass(frozen=True)
class BusinessDataProviderSpec:
    name: str
    label: str
    description: str
    factory: Type[OntologySearchProvider]
    fields: List[ProviderField]


PROVIDER_SPECS: Dict[str, BusinessDataProviderSpec] = {
    "nl2sql_http": BusinessDataProviderSpec(
        name="nl2sql_http",
        label="NL2SQL HTTP",
        description="通过 NL2SQL HTTP 查询服务检索本体业务数据。",
        factory=NL2SQLHttpProvider,
        fields=[
            ProviderField("url", "服务地址", required=True),
            ProviderField("token", "访问 Token", secret=True),
            ProviderField("scene", "场景 Scene"),
            ProviderField("timeout_seconds", "超时时间", type="int", default=600),
            ProviderField("max_retries", "最大重试次数", type="int", default=3),
        ],
    ),
    "nl2sql_template": BusinessDataProviderSpec(
        name="nl2sql_template",
        label="NL2SQL Template",
        description="通过预定义模板映射自然语言查询到 SQL，支持 NL2SQL 服务查询本体业务数据。",
        factory=NL2SQLTemplateProvider,
        fields=[
            ProviderField("url", "服务地址", required=True),
            ProviderField("token", "访问 Token", required=True, secret=True),
            ProviderField("scene", "数据源名称", required=True),
            ProviderField("timeout_seconds", "超时时间", type="int", default=600),
            ProviderField("max_retries", "最大重试次数", type="int", default=3),
        ],
    ),
    "mindsdb": BusinessDataProviderSpec(
        name="mindsdb",
        label="MindsDB",
        description="通过 MindsDB Agent 流式接口检索本体业务数据。",
        factory=MindsDBOntologySearchProvider,
        fields=[
            ProviderField("url", "服务地址", required=True),
            ProviderField("login", "登录名", default="mindsdb"),
            ProviderField("password", "密码", secret=True),
            ProviderField("default_database", "默认数据库"),
            ProviderField("default_agent_name", "Agent 名称", default="my_agent"),
            ProviderField("project_name", "Project 名称", default="mindsdb"),
            ProviderField("requires_auth", "需要鉴权", type="bool", default=False),
            ProviderField("timeout_seconds", "超时时间", type="int", default=600),
            ProviderField("max_retries", "最大重试次数", type="int", default=3),
        ],
    ),
    "dataagent": BusinessDataProviderSpec(
        name="dataagent",
        label="DataAgent",
        description="通过 DataAgent A2A REST 服务进行自然语言数据查询与分析。",
        factory=DataAgentProvider,
        fields=[
            ProviderField("url", "DataAgent 服务地址", required=True),
            ProviderField("token", "访问 Token", secret=True),
            ProviderField("timeout_seconds", "超时时间", type="int", default=600),
            ProviderField("max_retries", "最大重试次数", type="int", default=3),
        ],
    ),
    "neo4j": BusinessDataProviderSpec(
        name="neo4j",
        label="Neo4J 图查询",
        description="通过内置图查询 Provider 查询 Neo4J 本体实例数据。",
        factory=Neo4jGraphSearchProvider,
        fields=[
            ProviderField("uri", "Neo4J URI", required=True),
            ProviderField("database", "Database", default="neo4j"),
            ProviderField("username", "用户名", required=True),
            ProviderField("password", "密码", secret=True, required=True),
            ProviderField("timeout_seconds", "超时时间", type="int", default=60),
            ProviderField("max_retries", "最大重试次数", type="int", default=2),
            ProviderField("max_result_rows", "最大返回行数", type="int", default=100),
            ProviderField("max_iterations", "图检索 Agent 最大轮次", type="int", default=4),
            ProviderField("workspace_property", "Workspace 隔离字段", default="d2o_workspace_id"),
            ProviderField("ontology_property", "Ontology 隔离字段", default="d2o_ontology_id"),
        ],
    ),
    "hugegraph": BusinessDataProviderSpec(
        name="hugegraph",
        label="HugeGraph 图查询",
        description="通过内置图查询 Provider 查询 HugeGraph 本体实例数据。",
        factory=HugeGraphSearchProvider,
        fields=[
            ProviderField("url", "HugeGraph 地址", required=True),
            ProviderField("graph", "Graph 名称", default="hugegraph"),
            ProviderField("graph_space", "Graph Space"),
            ProviderField("username", "用户名"),
            ProviderField("password", "密码", secret=True),
            ProviderField("token", "访问 Token", secret=True),
            ProviderField("timeout_seconds", "超时时间", type="int", default=60),
            ProviderField("max_retries", "最大重试次数", type="int", default=2),
            ProviderField("max_result_rows", "最大返回行数", type="int", default=100),
            ProviderField("max_iterations", "图检索 Agent 最大轮次", type="int", default=4),
            ProviderField("workspace_property", "Workspace 隔离字段", default="d2o_workspace_id"),
            ProviderField("ontology_property", "Ontology 隔离字段", default="d2o_ontology_id"),
        ],
    ),
}


class BusinessDataProviderError(ValueError):
    """Raised when a business data provider payload is invalid."""


def normalize_provider_name(provider: Any) -> str:
    return str(provider or "").strip().lower()


def get_provider_spec(provider: Any) -> BusinessDataProviderSpec:
    provider_name = normalize_provider_name(provider)
    if not provider_name:
        raise BusinessDataProviderError("provider 不能为空")
    spec = PROVIDER_SPECS.get(provider_name)
    if spec is None:
        raise BusinessDataProviderError(
            "provider 仅支持 {}".format(" | ".join(sorted(PROVIDER_SPECS)))
        )
    return spec


def _coerce_value(field: ProviderField, value: Any) -> Any:
    if value is None:
        value = field.default
    if field.type == "bool":
        if isinstance(value, bool):
            return value
        normalized = str(value or "").strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off", ""}:
            return False
        raise BusinessDataProviderError("{} 必须是布尔值".format(field.name))
    if field.type == "int":
        if value == "":
            value = field.default
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise BusinessDataProviderError("{} 必须是整数".format(field.name)) from exc
    return str(value or "").strip()


def normalize_provider_config(
    provider: Any,
    config: Dict[str, Any] | None,
    *,
    existing_config: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    spec = get_provider_spec(provider)
    incoming = dict(config or {})
    existing = dict(existing_config or {})
    normalized: Dict[str, Any] = {}
    allowed = {field.name for field in spec.fields}
    unknown = set(incoming) - allowed
    if unknown:
        raise BusinessDataProviderError(
            "{} 不支持字段: {}".format(spec.name, ", ".join(sorted(unknown)))
        )

    for field in spec.fields:
        raw_value = incoming.get(field.name)
        if field.secret and raw_value in {None, ""} and field.name in existing:
            raw_value = existing.get(field.name)
        value = _coerce_value(field, raw_value)
        if field.name == "timeout_seconds" and int(value) <= 0:
            raise BusinessDataProviderError("timeout_seconds 必须大于 0")
        if field.name == "max_retries" and int(value) < 0:
            raise BusinessDataProviderError("max_retries 不能小于 0")
        if field.name in {"max_result_rows", "max_iterations"} and int(value) <= 0:
            raise BusinessDataProviderError("{} 必须大于 0".format(field.name))
        if field.name == "url":
            value = str(value or "").rstrip("/")
        if field.required and not str(value or "").strip():
            raise BusinessDataProviderError("{} 不能为空".format(field.label))
        normalized[field.name] = value

    if spec.name == "mindsdb" and normalized.get("requires_auth") and not normalized.get("password"):
        raise BusinessDataProviderError("MindsDB 密码不能为空")

    return normalized


def publicize_provider_config(provider: Any, config: Dict[str, Any] | None) -> Dict[str, Any]:
    spec = get_provider_spec(provider)
    values = dict(config or {})
    payload: Dict[str, Any] = {}
    for field in spec.fields:
        value = values.get(field.name, field.default)
        if field.secret:
            payload["has_{}".format(field.name)] = bool(value)
        else:
            payload[field.name] = value
    return payload


def list_provider_schemas() -> List[Dict[str, Any]]:
    schemas: List[Dict[str, Any]] = []
    for spec in PROVIDER_SPECS.values():
        schemas.append(
            {
                "name": spec.name,
                "label": spec.label,
                "description": spec.description,
                "fields": [
                    {
                        "name": field.name,
                        "label": field.label,
                        "type": field.type,
                        "required": field.required,
                        "secret": field.secret,
                        "default": field.default,
                    }
                    for field in spec.fields
                ],
            }
        )
    return schemas
