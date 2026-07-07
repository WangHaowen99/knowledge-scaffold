"""MindsDB client helpers for agent streaming and SQL queries."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Sequence

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..business_data.script_env import load_script_business_data_connection

ACTION_THOUGHT_MARKER = "```\nThought:"
logger = logging.getLogger(__name__)


class MindsDBError(RuntimeError):
    """Raised when MindsDB operations fail."""


def _normalize_server_url(url: Optional[str]) -> Optional[str]:
    text = str(url or "").strip()
    if not text:
        return None
    text = text.rstrip("/")
    if text.endswith("/chat"):
        text = text[: -len("/chat")].rstrip("/")
    return text or None


@dataclass
class MindsDBConfig:
    """Configuration for the MindsDB HTTP client.

    Keep field defaults out of the dataclass itself. Construction helpers apply
    defaults from either an injected script connection or an explicit config.
    """

    url: Optional[str]
    login: Optional[str]
    password: Optional[str]
    default_database: Optional[str]
    default_agent_name: str
    project_name: str
    requires_auth: bool
    timeout_seconds: int
    max_retries: int

    @classmethod
    def from_env(cls) -> "MindsDBConfig":
        script_connection = load_script_business_data_connection()
        if script_connection:
            provider = str(script_connection.get("provider") or "").strip().lower()
            if provider != "mindsdb":
                raise MindsDBError(
                    "当前本体绑定的数据连接 provider 是 '{}'，但脚本需要 MindsDB".format(
                        provider or "未配置"
                    )
                )
            config = dict(script_connection.get("config") or {})
            return cls(
                url=_normalize_server_url(config.get("url")),
                login=config.get("login") or "mindsdb",
                password=config.get("password") or "",
                default_database=config.get("default_database") or None,
                default_agent_name=(
                    config.get("default_agent_name")
                    or config.get("agent_name")
                    or "my_agent"
                ),
                project_name=config.get("project_name") or "mindsdb",
                requires_auth=bool(config.get("requires_auth", False)),
                timeout_seconds=int(config.get("timeout_seconds") or 600),
                max_retries=int(config.get("max_retries") or 3),
            )

        raise MindsDBError(
            "business data connection is not configured; bind the ontology to a workspace data connection"
        )


def _config_from_dict(config: Optional[dict | MindsDBConfig]) -> MindsDBConfig:
    if config is None:
        return MindsDBConfig.from_env()
    if isinstance(config, MindsDBConfig):
        return config

    ontology_search = config.get("ontology_search", {}) if isinstance(config, dict) else {}
    if isinstance(ontology_search, dict) and isinstance(ontology_search.get("mindsdb"), dict):
        mindsdb = ontology_search.get("mindsdb", {})
        query = ontology_search
    else:
        mindsdb = config.get("mindsdb", {}) if isinstance(config, dict) else {}
        query = config.get("query", {}) if isinstance(config, dict) else {}

    host = mindsdb.get("host") or mindsdb.get("url")
    return MindsDBConfig(
        url=_normalize_server_url(host),
        login=mindsdb.get("login") or "mindsdb",
        password=mindsdb.get("password") or "",
        default_database=mindsdb.get("default_database") or None,
        default_agent_name=(
            mindsdb.get("default_agent_name")
            or mindsdb.get("agent_name")
            or "my_agent"
        ),
        project_name=mindsdb.get("project_name") or "mindsdb",
        requires_auth=bool(mindsdb.get("requires_auth", False)),
        timeout_seconds=int(
            mindsdb.get("timeout_seconds", query.get("timeout_seconds", 600))
        ),
        max_retries=int(mindsdb.get("max_retries", query.get("max_retries", 3))),
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


class MindsDBClient:
    """Requests-based MindsDB client for agent completion streaming."""

    def __init__(self, config: Optional[dict | MindsDBConfig] = None):
        self.config = _config_from_dict(config)
        self.base_url = self.config.url or ""
        self.username = self.config.login or "mindsdb"
        self.password = self.config.password or ""
        self.agent_name = self.config.default_agent_name
        self.project_name = self.config.project_name
        self.requires_auth = self.config.requires_auth
        self.timeout = self.config.timeout_seconds
        self._auth_token: Optional[str] = None
        self._session = _create_session(self.config.max_retries)

    def _get_headers(self, with_auth: bool = False) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if with_auth:
            if not self._auth_token:
                if not self.base_url:
                    raise MindsDBError("MindsDB URL is not configured")
                try:
                    response = self._session.post(
                        "{}/api/login".format(self.base_url),
                        json={"username": self.username, "password": self.password},
                        timeout=10,
                    )
                except requests.exceptions.RequestException as exc:
                    raise MindsDBError("Failed to authenticate to MindsDB: {}".format(exc)) from exc

                if response.status_code == 200:
                    data = response.json()
                    self._auth_token = data.get("token") or data.get("access_token")

            if self._auth_token:
                headers["Authorization"] = "Bearer {}".format(self._auth_token)
        return headers

    def agent_completion_stream(
        self,
        question: str,
        agent_name: Optional[str] = None,
    ) -> Iterator[Any]:
        normalized_question = str(question or "").strip()
        if not normalized_question:
            raise MindsDBError("Question cannot be empty")
        if not self.base_url:
            raise MindsDBError("MindsDB URL is not configured")

        agent = str(agent_name or self.agent_name or "my_agent").strip() or "my_agent"
        url = "{}/api/projects/{}/agents/{}/completions/stream".format(
            self.base_url,
            self.project_name,
            agent,
        )
        payload = {"messages": [{"question": normalized_question, "answer": ""}]}
        logger.info(
            "MindsDB agent completion request: project=%s agent=%s url=%s question_chars=%d",
            self.project_name,
            agent,
            url,
            len(normalized_question),
        )

        try:
            response = self._session.post(
                url,
                headers=self._get_headers(with_auth=self.requires_auth),
                json=payload,
                timeout=self.timeout,
                stream=True,
            )

            if not self.requires_auth and response.status_code == 401:
                response.close()
                response = self._session.post(
                    url,
                    headers=self._get_headers(with_auth=True),
                    json=payload,
                    timeout=self.timeout,
                    stream=True,
                )
        except requests.exceptions.RequestException as exc:
            raise MindsDBError("Request failed: {}".format(exc)) from exc

        if response.status_code != 200:
            try:
                body = response.text
            except Exception:
                body = ""
            response.close()
            raise MindsDBError("Query failed: {} - {}".format(response.status_code, body))

        with response:
            yield from _iter_sse_events(response)

    def query_agent_stream(self, question: str) -> Iterator[Dict[str, str]]:
        for raw_chunk in self.agent_completion_stream(question):
            for event in _parse_mindsdb_chunk_to_events(raw_chunk):
                yield event

    def query_sql(self, query: str) -> Any:
        normalized_query = str(query or "").strip()
        if not normalized_query:
            raise MindsDBError("Query cannot be empty")
        if not self.base_url:
            raise MindsDBError("MindsDB URL is not configured")

        url = "{}/api/sql/query".format(self.base_url)
        payload = {
            "query": normalized_query,
            "context": {
                "db": self.config.default_database or "mindsdb",
                "show_secrets": False,
            },
        }

        try:
            response = self._session.post(
                url,
                headers=self._get_headers(with_auth=self.requires_auth),
                json=payload,
                timeout=self.timeout,
            )

            if not self.requires_auth and response.status_code == 401:
                response.close()
                response = self._session.post(
                    url,
                    headers=self._get_headers(with_auth=True),
                    json=payload,
                    timeout=self.timeout,
                )
        except requests.exceptions.RequestException as exc:
            raise MindsDBError("Request failed: {}".format(exc)) from exc

        if response.status_code != 200:
            try:
                body = response.text
            except Exception:
                body = ""
            response.close()
            raise MindsDBError("Query failed: {} - {}".format(response.status_code, body))

        try:
            return response.json()
        except ValueError as exc:
            raise MindsDBError("Invalid MindsDB SQL response JSON") from exc

    def query(self, question: str, agent_name: Optional[str] = None) -> Dict[str, Any]:
        try:
            final_content: Optional[str] = None
            for event in self.query_agent_stream(question):
                if str(event.get("type") or "").strip().lower() == "final":
                    final_content = _extract_final_content(str(event.get("content") or ""))
            return {
                "success": True,
                "data": {"answer": final_content or ""},
                "message": "Query successful",
            }
        except MindsDBError as exc:
            return {"success": False, "data": None, "message": str(exc)}

    def close(self) -> None:
        if self._session:
            self._session.close()

def _parse_mindsdb_chunk_to_events(raw_chunk: Any) -> List[Dict[str, str]]:
    chunk = _coerce_chunk_dict(raw_chunk)
    if not chunk:
        return []

    chunk_type = str(chunk.get("type") or "").strip().lower()

    if chunk_type == "start":
        content = str(chunk.get("prompt") or "").strip() or _collect_messages_text(chunk.get("messages"))
        return [{"type": "start", "content": content or "开始执行本体探索"}]

    if chunk_type == "end":
        return [{"type": "done", "content": "查询流程结束"}]

    if chunk_type == "sql":
        return []

    if chunk_type == "context":
        context = _collect_chunk_text(chunk)
        if not context or _should_skip_context_chunk(context):
            return []
        return [{"type": "intermediate", "content": context}]

    if chunk_type == "data":
        data = _extract_final_content(_collect_chunk_text(chunk))
        if not data:
            return []
        return [{"type": "final", "content": data}]

    if "output" in chunk:
        output = str(chunk.get("output") or "").strip() or _collect_messages_text(chunk.get("messages"))
        if not output:
            return []
        return [{"type": "final", "content": _extract_final_content(output)}]

    actions = chunk.get("actions")
    if isinstance(actions, list):
        action_texts = _deduplicate_texts(_extract_action_messages(actions))
        return [{"type": "intermediate", "content": text} for text in action_texts]

    steps = chunk.get("steps")
    if isinstance(steps, list):
        step_texts = _extract_step_messages(steps)
        return [{"type": "intermediate", "content": text} for text in step_texts]

    message_text = _collect_chunk_text(chunk)
    if not message_text:
        return []
    return [{"type": "intermediate", "content": message_text}]


def _coerce_chunk_dict(raw_chunk: Any) -> Optional[Dict[str, Any]]:
    if isinstance(raw_chunk, dict):
        return raw_chunk

    if isinstance(raw_chunk, str):
        text = raw_chunk.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"messages": [{"content": text}]}
        if isinstance(parsed, dict):
            return parsed
        return {"messages": [{"content": text}]}

    for attr in ("model_dump", "dict", "to_dict"):
        converter = getattr(raw_chunk, attr, None)
        if callable(converter):
            converted = converter()
            if isinstance(converted, dict):
                return converted

    if hasattr(raw_chunk, "__dict__"):
        converted = {
            key: value
            for key, value in vars(raw_chunk).items()
            if not key.startswith("_")
        }
        if converted:
            return converted

    return None


def _extract_action_messages(actions: Any) -> List[str]:
    if not isinstance(actions, list):
        return []

    messages: List[str] = []
    for action in actions:
        if not isinstance(action, dict):
            continue

        log_text = str(action.get("log") or "").strip()
        if not log_text:
            continue

        marker_index = log_text.find(ACTION_THOUGHT_MARKER)
        if marker_index < 0:
            continue

        prefix = log_text[:marker_index].strip()
        if prefix:
            messages.append(prefix)

    return messages


def _extract_step_messages(steps: Any) -> List[str]:
    if not isinstance(steps, list):
        return []

    messages: List[str] = []
    for step in steps:
        if not isinstance(step, dict):
            continue

        action = step.get("action") if isinstance(step.get("action"), dict) else {}
        tool_name = str(action.get("tool") or "").strip()
        observation = str(step.get("observation") or "").strip()
        if not tool_name or not observation:
            continue

        cleaned_observation = observation.replace("mindsdb.", "")
        messages.append("工具调用: {}".format(tool_name))
        messages.append("结果: {}".format(cleaned_observation))

    return messages


def _collect_messages_text(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""

    texts: List[str] = []
    for item in messages:
        if isinstance(item, str):
            text = item.strip()
            if text:
                texts.append(text)
            continue

        if isinstance(item, dict):
            text = str(item.get("content") or "").strip()
            if text:
                texts.append(text)

    return "\n".join(texts).strip()


def _collect_chunk_text(chunk: Dict[str, Any]) -> str:
    return str(chunk.get("content") or "").strip() or _collect_messages_text(chunk.get("messages"))


def _should_skip_context_chunk(content: str) -> bool:
    normalized = str(content or "").strip().lower()
    if not normalized:
        return True
    return normalized.startswith("executing final sql query:")


def _deduplicate_texts(texts: List[str]) -> List[str]:
    deduplicated: List[str] = []
    seen = set()
    for item in texts:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        deduplicated.append(text)
    return deduplicated


def _extract_final_content(content: str) -> str:
    marker = "## 最终回答:"
    if marker in content:
        return content.split(marker, 1)[1].strip()
    return content.strip()


def _iter_sse_events(response: requests.Response) -> Iterator[Any]:
    data_lines: List[str] = []
    for raw_line in response.iter_lines(decode_unicode=True):
        if raw_line is None:
            continue

        line = raw_line.rstrip("\r")
        if line == "":
            payload = _decode_sse_data_lines(data_lines)
            data_lines = []
            if payload is None:
                continue
            if payload == "__done__":
                return
            yield payload
            continue

        if line.startswith(":"):
            continue

        field, separator, value = line.partition(":")
        if not separator:
            continue
        if field == "data":
            data_lines.append(value.lstrip(" "))

    payload = _decode_sse_data_lines(data_lines)
    if payload and payload != "__done__":
        yield payload


def _decode_sse_data_lines(data_lines: Sequence[str]) -> Optional[Any]:
    if not data_lines:
        return None

    data_str = "\n".join(data_lines).strip()
    if not data_str:
        return None
    if data_str == "[DONE]":
        return "__done__"

    try:
        payload = json.loads(data_str)
    except json.JSONDecodeError:
        return data_str

    if not isinstance(payload, (dict, str)):
        raise MindsDBError("Invalid MindsDB SSE payload type")

    return payload


_client_instance: Optional[MindsDBClient] = None


def _get_client(config: Optional[dict] = None) -> MindsDBClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = MindsDBClient(config=config)
    return _client_instance


def execute_query(question: str, config: Optional[dict] = None, reuse_client: bool = True) -> dict:
    if not question or not question.strip():
        return {"success": False, "message": "查询问题不能为空", "data": None}

    try:
        client = _get_client(config=config) if reuse_client else MindsDBClient(config=config)
        return client.query(question)
    except Exception as exc:
        return {"success": False, "message": "查询执行失败: {}".format(exc), "data": None}


def query_agent_stream(
    question: str,
    config: Optional[dict] = None,
    reuse_client: bool = True,
) -> Iterator[Dict[str, str]]:
    if not question or not question.strip():
        raise MindsDBError("Question cannot be empty")

    client = _get_client(config=config) if reuse_client else MindsDBClient(config=config)
    try:
        for event in client.query_agent_stream(question):
            yield event
    finally:
        if not reuse_client:
            client.close()


def query_sql(
    query: str,
    config: Optional[dict] = None,
    reuse_client: bool = True,
) -> Any:
    client = _get_client(config=config) if reuse_client else MindsDBClient(config=config)
    try:
        return client.query_sql(query)
    finally:
        if not reuse_client:
            client.close()


def query_ontology(question: str, config: Optional[dict] = None, reuse_client: bool = True) -> dict:
    try:
        return execute_query(question, config=config, reuse_client=reuse_client)
    except Exception as exc:
        return {"success": False, "message": "查询失败: {}".format(exc), "data": None}


def test_connection(config: Optional[dict] = None) -> dict:
    client = MindsDBClient(config=config)
    try:
        if not client.base_url:
            return {"success": False, "message": "连接失败: MindsDB URL 未配置"}

        response = client._session.get(
            "{}/api/status".format(client.base_url),
            timeout=5,
        )
        return {
            "success": response.status_code == 200,
            "message": "Connected" if response.status_code == 200 else "Status: {}".format(response.status_code),
        }
    except Exception as exc:
        return {"success": False, "message": "连接失败: {}".format(exc)}
    finally:
        client.close()


def reset_client() -> None:
    global _client_instance
    if _client_instance:
        _client_instance.close()
        _client_instance = None


def main() -> None:
    import sys

    if len(sys.argv) < 2:
        result = {
            "success": False,
            "data": None,
            "message": '缺少查询参数或者参数错误。使用方法: python mindsdb_client.py "<查询问题>"',
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(1)

    question = sys.argv[1]
    result = query_sql(question)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("success"):
        sys.exit(1)


if __name__ == "__main__":
    main()
