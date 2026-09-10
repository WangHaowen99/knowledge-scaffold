"""Connectivity checks for ontology business data sources."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict
from urllib.parse import quote

import requests
from agent.tools.business_data.effective_connection import build_effective_connection
from agent.tools.business_data.providers.graph.timeouts import resolve_graph_connect_timeout_seconds
from agent.tools.business_data.resolver import ResolvedBusinessDataSource
from shared_capabilities.business_data import create_provider_client


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _coerce_string(value: Any) -> str:
    return str(value or "").strip()


def _error_detail(response: requests.Response | None) -> str:
    if response is None:
        return ""
    try:
        body = response.json()
        if isinstance(body, dict):
            return _coerce_string(body.get("message") or body.get("error") or body.get("detail"))
    except Exception:
        pass
    return _coerce_string(getattr(response, "text", ""))


def test_business_data_connection(connection: ResolvedBusinessDataSource) -> Dict[str, Any]:
    if connection.provider in {"mindsdb", "nl2sql_http", "nl2sql_template", "dataagent"}:
        return _test_shared_provider(connection)
    if connection.provider == "neo4j":
        return _test_neo4j(connection)
    if connection.provider == "hugegraph":
        return _test_hugegraph(connection)
    return {
        "success": False,
        "message": "不支持的数据源 Provider: {}".format(connection.provider),
        "tested_at": _utc_now(),
    }


async def test_business_data_connection_async(
    connection: ResolvedBusinessDataSource,
    *,
    runtime: Any,
) -> Dict[str, Any]:
    """Probe an external data source through the owned external-I/O pool."""

    return await runtime.external_io.run(
        test_business_data_connection,
        connection,
    )


def _test_shared_provider(connection: ResolvedBusinessDataSource) -> Dict[str, Any]:
    tested_at = _utc_now()
    provider_name = "nl2sql_http" if connection.provider == "nl2sql_template" else connection.provider
    label = {
        "mindsdb": "MindsDB",
        "nl2sql_http": ("NL2SQL Template" if connection.provider == "nl2sql_template" else "NL2SQL HTTP"),
        "dataagent": "DataAgent REST",
    }.get(provider_name, provider_name)
    try:
        client = create_provider_client(build_effective_connection(connection, provider_override=provider_name))
    except Exception:
        return {
            "success": False,
            "message": "不支持的业务数据源 Provider: {}".format(connection.provider),
            "tested_at": tested_at,
        }
    try:
        probe_timeout = 15 if provider_name == "mindsdb" else 10
        result = client.probe(timeout_seconds=probe_timeout)
    finally:
        client.close()
    return {
        "success": result.success,
        "message": result.message or ("{} 服务连接失败：{}".format(label, result.error or "未知错误")),
        "tested_at": tested_at,
    }


def _test_neo4j(connection: ResolvedBusinessDataSource) -> Dict[str, Any]:
    tested_at = _utc_now()
    config = dict(connection.config or {})
    uri = _coerce_string(config.get("uri"))
    database = _coerce_string(config.get("database")) or "neo4j"
    username = _coerce_string(config.get("username"))
    password = _coerce_string(config.get("password"))
    workspace_property = _coerce_string(config.get("workspace_property")) or "d2o_workspace_id"
    timeout = resolve_graph_connect_timeout_seconds(config)

    if not uri:
        return {"success": False, "message": "Neo4J URI 未配置", "tested_at": tested_at}
    if not workspace_property:
        return {"success": False, "message": "Neo4J workspace_property 未配置", "tested_at": tested_at}

    try:
        from neo4j import GraphDatabase
    except ImportError:
        return {"success": False, "message": "Neo4J Python 驱动未安装", "tested_at": tested_at}

    driver = None
    try:
        driver = GraphDatabase.driver(
            uri,
            auth=(username, password),
            connection_timeout=timeout,
        )
        verify = getattr(driver, "verify_connectivity", None)
        if callable(verify):
            verify()
        with driver.session(database=database) as session:
            session.run("RETURN 1 AS ok").single()
        return {
            "success": True,
            "message": "Neo4J 连接成功，已配置 workspace scope 字段 {}".format(workspace_property),
            "tested_at": tested_at,
        }
    except Exception as exc:
        return {
            "success": False,
            "message": "Neo4J 连接失败：{}".format(_sanitize_secret_text(str(exc), [password])),
            "tested_at": tested_at,
        }
    finally:
        if driver is not None:
            try:
                driver.close()
            except Exception:
                pass


def _test_hugegraph(connection: ResolvedBusinessDataSource) -> Dict[str, Any]:
    tested_at = _utc_now()
    config = dict(connection.config or {})
    url = _coerce_string(config.get("url")).rstrip("/")
    graph = _coerce_string(config.get("graph"))
    graph_space = _coerce_string(config.get("graph_space"))
    username = _coerce_string(config.get("username"))
    password = _coerce_string(config.get("password"))
    token = _coerce_string(config.get("token"))
    workspace_property = _coerce_string(config.get("workspace_property")) or "d2o_workspace_id"
    timeout = resolve_graph_connect_timeout_seconds(config)

    if not url:
        return {"success": False, "message": "HugeGraph 地址未配置", "tested_at": tested_at}
    if not workspace_property:
        return {"success": False, "message": "HugeGraph workspace_property 未配置", "tested_at": tested_at}

    headers = {}
    if token:
        headers["Authorization"] = "Bearer {}".format(token)
    auth = (username, password) if username and password else None
    target = "{}{}".format(url, _hugegraph_graph_path(graph=graph, graph_space=graph_space, suffix="/schema"))

    try:
        response = requests.get(target, headers=headers, auth=auth, timeout=timeout)
        response.raise_for_status()
        return {
            "success": True,
            "message": "HugeGraph 连接成功，已配置 workspace scope 字段 {}".format(workspace_property),
            "tested_at": tested_at,
        }
    except requests.exceptions.HTTPError as exc:
        response = exc.response
        status_code = response.status_code if response is not None else "unknown"
        detail = _sanitize_secret_text(_error_detail(response), [password, token])
        message = "HugeGraph 连接失败（HTTP {}）".format(status_code)
        if detail:
            message = "{}：{}".format(message, detail[:200])
        return {"success": False, "message": message, "tested_at": tested_at}
    except requests.exceptions.RequestException as exc:
        return {
            "success": False,
            "message": "HugeGraph 请求失败：{}".format(_sanitize_secret_text(str(exc), [password, token])),
            "tested_at": tested_at,
        }


def _sanitize_secret_text(text: str, secrets: list[str]) -> str:
    sanitized = str(text or "")
    for secret in secrets:
        secret = _coerce_string(secret)
        if secret:
            sanitized = sanitized.replace(secret, "***")
    return sanitized


def _hugegraph_graph_path(*, graph: str, graph_space: str, suffix: str = "") -> str:
    graph_name = quote(graph or "hugegraph", safe="")
    normalized_suffix = suffix if str(suffix or "").startswith("/") else "/{}".format(suffix) if suffix else ""
    if graph_space:
        return "/graphspaces/{}/graphs/{}{}".format(quote(graph_space, safe=""), graph_name, normalized_suffix)
    return "/graphs/{}{}".format(graph_name, normalized_suffix)
