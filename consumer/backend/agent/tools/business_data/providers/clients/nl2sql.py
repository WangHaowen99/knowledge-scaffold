"""NL2SQL HTTP client helpers for semantic and direct SQL queries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from agent.tools.business_data.script_env import load_script_business_data_connection


class NL2SQLError(RuntimeError):
    """Raised when NL2SQL HTTP operations fail."""


def _normalize_server_url(url: Optional[str]) -> Optional[str]:
    text = str(url or "").strip().rstrip("/")
    return text or None


@dataclass
class NL2SQLConfig:
    """Configuration for the NL2SQL HTTP client."""

    url: Optional[str]
    token: str
    scene: str
    timeout_seconds: int
    max_retries: int

    @classmethod
    def from_env(cls) -> "NL2SQLConfig":
        script_connection = load_script_business_data_connection()
        if script_connection:
            provider = str(script_connection.get("provider") or "").strip().lower()
            if provider != "nl2sql_http":
                raise NL2SQLError(
                    "当前本体配置的业务数据源 provider 是 '{}'，但脚本需要 NL2SQL HTTP".format(
                        provider or "未配置"
                    )
                )
            config = dict(script_connection.get("config") or {})
            return cls(
                url=_normalize_server_url(config.get("url")),
                token=str(config.get("token") or "").strip(),
                scene=str(config.get("scene") or "").strip(),
                timeout_seconds=int(config.get("timeout_seconds") or 600),
                max_retries=int(config.get("max_retries") or 3),
            )

        raise NL2SQLError(
            "ontology business data source is not configured; configure a data source for this ontology"
        )


def _config_from_legacy_dict(config: Optional[dict | NL2SQLConfig]) -> NL2SQLConfig:
    if config is None:
        return NL2SQLConfig.from_env()
    if isinstance(config, NL2SQLConfig):
        return config

    ontology_search = config.get("ontology_search", {}) if isinstance(config, dict) else {}
    provider_config = ontology_search.get("nl2sql_http", {}) if isinstance(ontology_search, dict) else {}
    return NL2SQLConfig(
        url=_normalize_server_url(provider_config.get("url")),
        token=str(provider_config.get("token") or "").strip(),
        scene=str(provider_config.get("scene") or "").strip(),
        timeout_seconds=int(provider_config.get("timeout_seconds") or 600),
        max_retries=int(provider_config.get("max_retries") or 3),
    )


def _create_session(retries: int = 3) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=0.5,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=["GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


class NL2SQLClient:
    """Requests-based client for NL2SQL HTTP query APIs."""

    def __init__(self, config: Optional[dict | NL2SQLConfig] = None):
        self.config = _config_from_legacy_dict(config)
        self.base_url = self.config.url or ""
        self.token = self.config.token
        self.scene = self.config.scene
        self.timeout = self.config.timeout_seconds
        self._session = _create_session(self.config.max_retries)

    def query(self, question: str) -> Dict[str, Any]:
        normalized_question = str(question or "").strip()
        if not normalized_question:
            return {"success": False, "error": "查询问题不能为空", "data": None}
        if not self.base_url:
            return {"success": False, "error": "本体检索未配置：请先配置 NL2SQL HTTP 服务地址", "data": None}

        body: Dict[str, Any] = {"question": normalized_question}
        if self.scene:
            body["scene"] = self.scene
        return self._post_json("/api/v1/query", body)

    def query_sql(self, sql: str) -> Dict[str, Any]:
        normalized_sql = str(sql or "").strip()
        if not normalized_sql:
            return {"success": False, "error": "SQL 不能为空", "data": None}
        if not self.base_url:
            return {"success": False, "error": "本体检索未配置：请先配置 NL2SQL HTTP 服务地址", "data": None}

        body: Dict[str, Any] = {"sql": normalized_sql}
        if self.scene:
            body["scene"] = self.scene
        return self._post_json("/api/v1/sql", body)

    def test_connection(self) -> Dict[str, Any]:
        if not self.base_url:
            return {"success": False, "message": "NL2SQL HTTP URL 未配置"}
        try:
            response = self._session.get(
                "{}/health".format(self.base_url),
                headers=self._headers(),
                timeout=min(self.timeout, 10),
            )
        except requests.exceptions.RequestException as exc:
            return {"success": False, "message": "NL2SQL HTTP 服务请求失败：{}".format(exc)}
        if response.status_code >= 400:
            return {
                "success": False,
                "message": "NL2SQL HTTP 服务连接失败（HTTP {}）：{}".format(
                    response.status_code,
                    _extract_error_detail(response),
                ),
            }
        return {"success": True, "message": "NL2SQL HTTP 服务连接正常"}

    def close(self) -> None:
        self._session.close()

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer {}".format(self.token)
        return headers

    def _post_json(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        try:
            response = self._session.post(
                "{}{}".format(self.base_url, path),
                headers=self._headers(),
                json=body,
                timeout=self.timeout,
            )
        except requests.exceptions.RequestException as exc:
            return {"success": False, "error": "Connection failed: {}".format(exc), "data": None}

        if response.status_code >= 400:
            return {
                "success": False,
                "error": "HTTP {}: {}".format(response.status_code, _extract_error_detail(response)),
                "data": None,
            }

        try:
            payload = response.json()
        except ValueError:
            return {"success": False, "error": "Invalid JSON response", "data": None}

        if isinstance(payload, dict):
            return payload
        return {"success": False, "error": "Invalid response payload", "data": None}


def _extract_error_detail(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        detail = payload.get("detail", payload.get("error", payload.get("message", payload)))
        return str(detail)
    return str(getattr(response, "text", "") or "").strip() or "Unknown error"


_client_instance: Optional[NL2SQLClient] = None


def _get_client(config: Optional[dict] = None) -> NL2SQLClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = NL2SQLClient(config=config)
    return _client_instance


def query(
    question: str,
    config: Optional[dict] = None,
    reuse_client: bool = True,
) -> Dict[str, Any]:
    client = _get_client(config=config) if reuse_client else NL2SQLClient(config=config)
    try:
        return client.query(question)
    finally:
        if not reuse_client:
            client.close()


def query_sql(
    sql: str,
    config: Optional[dict] = None,
    reuse_client: bool = True,
) -> Dict[str, Any]:
    client = _get_client(config=config) if reuse_client else NL2SQLClient(config=config)
    try:
        return client.query_sql(sql)
    finally:
        if not reuse_client:
            client.close()


def reset_client() -> None:
    global _client_instance
    if _client_instance:
        _client_instance.close()
        _client_instance = None
