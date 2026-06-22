"""Connectivity checks for workspace business data connections."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

import requests

from .resolver import ResolvedBusinessDataConnection

_NL2SQL_CONNECTION_PROBE_QUESTION = "连接测试"


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


def _post_mindsdb_sql_probe(
    url: str,
    database: str,
    headers: Dict[str, str],
    timeout: int,
) -> requests.Response:
    return requests.post(
        "{}/api/sql/query".format(url),
        headers=headers,
        json={
            "query": "SELECT 1",
            "context": {"db": database, "show_secrets": False},
        },
        timeout=timeout,
    )


def test_business_data_connection(connection: ResolvedBusinessDataConnection) -> Dict[str, Any]:
    if connection.provider == "mindsdb":
        return _test_mindsdb(connection)
    if connection.provider in ("nl2sql_http", "nl2sql_template"):
        return _test_nl2sql(connection)
    return {
        "success": False,
        "message": "不支持的数据连接 Provider: {}".format(connection.provider),
        "tested_at": _utc_now(),
    }


def _test_mindsdb(connection: ResolvedBusinessDataConnection) -> Dict[str, Any]:
    tested_at = _utc_now()
    config = dict(connection.config or {})
    url = _coerce_string(config.get("url")).rstrip("/")
    if not url:
        return {"success": False, "message": "MindsDB URL 未配置", "tested_at": tested_at}

    timeout = min(int(config.get("timeout_seconds") or 15), 15)
    requires_auth = bool(config.get("requires_auth"))
    login = _coerce_string(config.get("login")) or "mindsdb"
    password = _coerce_string(config.get("password"))
    database = _coerce_string(config.get("default_database")) or "mindsdb"

    try:
        if requires_auth:
            if not password:
                return {"success": False, "message": "已启用鉴权，但密码未配置", "tested_at": tested_at}
            response = requests.post(
                "{}/api/login".format(url),
                json={"username": login, "password": password},
                timeout=timeout,
            )
            response.raise_for_status()
            result = response.json()
            token = _coerce_string(result.get("token") or result.get("access_token"))
            if not token:
                return {
                    "success": False,
                    "message": "MindsDB 登录返回成功，但未返回 token",
                    "tested_at": tested_at,
                }
            response = _post_mindsdb_sql_probe(
                url,
                database,
                {
                    "Content-Type": "application/json",
                    "Authorization": "Bearer {}".format(token),
                },
                timeout,
            )
            response.raise_for_status()
            return {
                "success": True,
                "message": "MindsDB 连接成功，鉴权与查询接口可用",
                "tested_at": tested_at,
            }

        response = _post_mindsdb_sql_probe(url, database, {"Content-Type": "application/json"}, timeout)
        if response.status_code == 401:
            return {
                "success": False,
                "message": "MindsDB 要求鉴权，请启用鉴权并配置用户名密码",
                "tested_at": tested_at,
            }
        response.raise_for_status()
        return {"success": True, "message": "MindsDB 连接成功，匿名访问可用", "tested_at": tested_at}
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


def _test_nl2sql(connection: ResolvedBusinessDataConnection) -> Dict[str, Any]:
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
