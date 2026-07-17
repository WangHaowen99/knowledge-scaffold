"""Connectivity checks for ontology business data sources."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict
from urllib.parse import quote

import requests

from agent.tools.business_data.resolver import ResolvedBusinessDataSource

_NL2SQL_CONNECTION_PROBE_QUESTION = "连接测试"
_DATAAGENT_AGENT_CARD_PATH = "/.well-known/agent-card.json"
_DATAAGENT_LEGACY_AGENT_CARD_PATH = "/.well-known/agent.json"


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


def _get_mindsdb_agents(
    url: str,
    project_name: str,
    headers: Dict[str, str],
    timeout: int,
) -> requests.Response:
    return requests.get(
        "{}/api/projects/{}/agents".format(url, quote(project_name, safe="")),
        headers=headers,
        timeout=timeout,
    )


def _mindsdb_agent_names(response: requests.Response) -> list[str] | None:
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return None
    rows = payload
    if isinstance(payload, dict):
        if "agents" in payload:
            rows = payload.get("agents")
        elif "data" in payload:
            rows = payload.get("data")
        else:
            rows = None
    if not isinstance(rows, list):
        return None

    names: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        normalized = {str(key).strip().lower(): value for key, value in row.items()}
        name = _coerce_string(normalized.get("name") or normalized.get("agent_name"))
        if name:
            names.append(name)
    return names


def _validate_mindsdb_agent(
    response: requests.Response,
    *,
    project_name: str,
    agent_name: str,
    tested_at: str,
) -> Dict[str, Any] | None:
    names = _mindsdb_agent_names(response)
    if names is None:
        return {
            "success": False,
            "message": "MindsDB Agent 列表响应格式无效",
            "tested_at": tested_at,
        }
    if agent_name in names:
        return None
    return {
        "success": False,
        "message": "MindsDB 连接成功，但 Project '{}' 中不存在 Agent '{}'".format(
            project_name,
            agent_name,
        ),
        "tested_at": tested_at,
    }


def test_business_data_connection(connection: ResolvedBusinessDataSource) -> Dict[str, Any]:
    if connection.provider == "mindsdb":
        return _test_mindsdb(connection)
    if connection.provider in ("nl2sql_http", "nl2sql_template"):
        return _test_nl2sql(connection)
    if connection.provider == "dataagent":
        return _test_dataagent(connection)
    if connection.provider == "neo4j":
        return _test_neo4j(connection)
    if connection.provider == "hugegraph":
        return _test_hugegraph(connection)
    return {
        "success": False,
        "message": "不支持的数据源 Provider: {}".format(connection.provider),
        "tested_at": _utc_now(),
    }


def _test_mindsdb(connection: ResolvedBusinessDataSource) -> Dict[str, Any]:
    tested_at = _utc_now()
    config = dict(connection.config or {})
    url = _coerce_string(config.get("url")).rstrip("/")
    if not url:
        return {"success": False, "message": "MindsDB URL 未配置", "tested_at": tested_at}

    timeout = min(int(config.get("timeout_seconds") or 15), 15)
    requires_auth = bool(config.get("requires_auth"))
    login = _coerce_string(config.get("login")) or "mindsdb"
    password = _coerce_string(config.get("password"))
    project_name = _coerce_string(config.get("project_name")) or "mindsdb"
    agent_name = _coerce_string(
        config.get("default_agent_name") or config.get("agent_name")
    ) or "my_agent"

    try:
        headers = {"Content-Type": "application/json"}
        if requires_auth:
            if not password:
                return {"success": False, "message": "已启用鉴权，但密码未配置", "tested_at": tested_at}
            response = requests.post(
                "{}/api/login".format(url),
                json={"username": login, "password": password},
                timeout=timeout,
            )
            response.raise_for_status()
            try:
                result = response.json()
            except (TypeError, ValueError):
                result = None
            if not isinstance(result, dict):
                return {
                    "success": False,
                    "message": "MindsDB 登录响应格式无效",
                    "tested_at": tested_at,
                }
            token = _coerce_string(result.get("token") or result.get("access_token"))
            if not token:
                return {
                    "success": False,
                    "message": "MindsDB 登录返回成功，但未返回 token",
                    "tested_at": tested_at,
                }
            headers["Authorization"] = "Bearer {}".format(token)

        response = _get_mindsdb_agents(url, project_name, headers, timeout)
        if response.status_code == 401:
            return {
                "success": False,
                "message": "MindsDB 要求鉴权，请启用鉴权并配置用户名密码",
                "tested_at": tested_at,
            }
        if response.status_code == 404:
            return {
                "success": False,
                "message": "MindsDB Project '{}' 不存在或 Agent 列表接口不可用".format(project_name),
                "tested_at": tested_at,
            }
        response.raise_for_status()
        invalid_agent = _validate_mindsdb_agent(
            response,
            project_name=project_name,
            agent_name=agent_name,
            tested_at=tested_at,
        )
        if invalid_agent is not None:
            return invalid_agent
        message = (
            "MindsDB 连接成功，鉴权与 Agent 配置可用"
            if requires_auth
            else "MindsDB 连接成功，匿名访问与 Agent 配置可用"
        )
        return {"success": True, "message": message, "tested_at": tested_at}
    except requests.exceptions.HTTPError as exc:
        response = exc.response
        status_code = response.status_code if response is not None else "unknown"
        detail = _error_detail(response)
        message = "MindsDB 连接失败（HTTP {}）".format(status_code)
        if detail:
            message = "{}：{}".format(message, detail[:200])
        return {"success": False, "message": message, "tested_at": tested_at}
    except requests.exceptions.RequestException as exc:
        return {"success": False, "message": "MindsDB 请求失败：{}".format(str(exc)), "tested_at": tested_at}


def _test_nl2sql(connection: ResolvedBusinessDataSource) -> Dict[str, Any]:
    tested_at = _utc_now()
    config = dict(connection.config or {})
    url = _coerce_string(config.get("url")).rstrip("/")
    label = "NL2SQL Template" if connection.provider == "nl2sql_template" else "NL2SQL HTTP"
    if not url:
        return {"success": False, "message": "{} URL 未配置".format(label), "tested_at": tested_at}

    timeout = min(int(config.get("timeout_seconds") or 10), 10)
    token = _coerce_string(config.get("token"))
    scene = _coerce_string(config.get("scene"))
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer {}".format(token)
    body: Dict[str, Any] = {"question": _NL2SQL_CONNECTION_PROBE_QUESTION}
    if scene:
        body["scene"] = scene

    try:
        response = requests.post(
            "{}/api/v1/query".format(url),
            headers=headers,
            json=body,
            timeout=timeout,
        )
        response.raise_for_status()
        return {"success": True, "message": "{} 服务连接正常".format(label), "tested_at": tested_at}
    except requests.exceptions.HTTPError as exc:
        response = exc.response
        status_code = response.status_code if response is not None else "unknown"
        detail = _error_detail(response)
        message = "{} 服务连接失败（HTTP {}）".format(label, status_code)
        if detail:
            message = "{}：{}".format(message, detail[:200])
        return {"success": False, "message": message, "tested_at": tested_at}
    except requests.exceptions.RequestException as exc:
        return {"success": False, "message": "{} 服务请求失败：{}".format(label, str(exc)), "tested_at": tested_at}


def _test_dataagent(connection: ResolvedBusinessDataSource) -> Dict[str, Any]:
    tested_at = _utc_now()
    config = dict(connection.config or {})
    url = _coerce_string(config.get("url")).rstrip("/")
    if not url:
        return {"success": False, "message": "DataAgent 服务地址未配置", "tested_at": tested_at}

    timeout = min(int(config.get("timeout_seconds") or 10), 10)
    token = _coerce_string(config.get("token"))
    headers: Dict[str, str] = {}
    if token:
        headers["Authorization"] = "Bearer {}".format(token)

    try:
        response = requests.get(
            "{}{}".format(url, _DATAAGENT_AGENT_CARD_PATH),
            headers=headers,
            timeout=timeout,
        )
        if response.status_code == 404:
            response = requests.get(
                "{}{}".format(url, _DATAAGENT_LEGACY_AGENT_CARD_PATH),
                headers=headers,
                timeout=timeout,
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not _coerce_string(payload.get("name")):
            return {"success": False, "message": "DataAgent AgentCard 响应格式无效", "tested_at": tested_at}
        return {"success": True, "message": "DataAgent REST 服务连接正常", "tested_at": tested_at}
    except requests.exceptions.HTTPError as exc:
        response = exc.response
        status_code = response.status_code if response is not None else "unknown"
        detail = _sanitize_secret_text(_error_detail(response), [token])
        message = "DataAgent REST 连接失败（HTTP {}）".format(status_code)
        if detail:
            message = "{}：{}".format(message, detail[:200])
        return {"success": False, "message": message, "tested_at": tested_at}
    except requests.exceptions.RequestException as exc:
        return {
            "success": False,
            "message": "DataAgent REST 请求失败：{}".format(_sanitize_secret_text(str(exc), [token])),
            "tested_at": tested_at,
        }
    except ValueError:
        return {"success": False, "message": "DataAgent AgentCard 返回非 JSON 响应", "tested_at": tested_at}


def _test_neo4j(connection: ResolvedBusinessDataSource) -> Dict[str, Any]:
    tested_at = _utc_now()
    config = dict(connection.config or {})
    uri = _coerce_string(config.get("uri"))
    database = _coerce_string(config.get("database")) or "neo4j"
    username = _coerce_string(config.get("username"))
    password = _coerce_string(config.get("password"))
    workspace_property = _coerce_string(config.get("workspace_property")) or "d2o_workspace_id"
    timeout = min(int(config.get("timeout_seconds") or 15), 15)

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
    timeout = min(int(config.get("timeout_seconds") or 15), 15)

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
