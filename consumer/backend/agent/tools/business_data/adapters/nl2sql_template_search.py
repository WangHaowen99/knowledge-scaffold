"""NL2SQL Template ontology search provider with declarative matcher support."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional, Tuple

from ontology.ontologies import get_ontology_version_dir
from agent.tools.business_data.adapters.base import BusinessDataSearchEvent
from shared_capabilities.business_data import EffectiveConnection, NL2SQLProvider
from utils.logging import get_logger

# Matcher protocol type: (query) -> (sql | None, metadata | None)
MatcherFn = Callable[[str], Tuple[Optional[str], Optional[Dict[str, Any]]]]

logger = get_logger("business_data.nl2sql_template")

_LEGACY_MATCHER_BLOCKED_ALERT = (
    "UNTRUSTED_MATCHER_BLOCKED: legacy ontology matcher was ignored because "
    "knowledge-package Python is not imported into the backend process. "
    "ontology_id=%s matcher_path=%s remediation=migrate to sql_matcher.json "
    "or query_templates.json"
)


class _DefaultMatcher:
    """Built-in exact-match matcher that loads scripts/query_templates.json and does key lookup."""

    def __init__(
        self,
        workspace_root: str,
        ontology_id: str,
    ) -> None:
        self._templates = self._load_templates(workspace_root, ontology_id)

    def __call__(self, query: str) -> tuple[str | None, dict | None]:
        template_data = self._templates.get(query)
        if not template_data:
            return None, None

        if isinstance(template_data, str):
            return template_data, {"from_template": query}

        sql = str(template_data.get("sql") or "")
        if not sql:
            return None, None
        metadata: dict[str, Any] = {"from_template": query}
        dedupe_key = template_data.get("dedupe_key")
        if dedupe_key:
            metadata["dedupe_key"] = dedupe_key
        return sql, metadata

    @staticmethod
    def _load_templates(
        workspace_root: str,
        ontology_id: str,
    ) -> dict[str, Any]:
        template_file = _resolve_template_path(workspace_root, ontology_id)
        if template_file is None or not template_file.exists():
            return {}
        try:
            with template_file.open("r", encoding="utf-8") as f:
                payload = json.load(f)
            if isinstance(payload, dict):
                return {str(k): v for k, v in payload.items() if k and v}
            return {}
        except (json.JSONDecodeError, OSError):
            return {}


class _DeclarativeMatcher:
    """Matcher backed by scripts/sql_matcher.json."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._rules = self._load_rules(path)

    def __call__(self, query: str) -> tuple[str | None, dict | None]:
        for rule in self._rules:
            try:
                sql, metadata = _match_rule(rule, query)
            except Exception as exc:
                logger.warning(
                    "Invalid declarative matcher rule skipped: matcher_path=%s rule_index=%s error=%s",
                    str(self._path),
                    rule.get("rule_index", "unknown"),
                    exc,
                )
                continue
            if sql is not None:
                return sql, metadata
        return None, None

    @staticmethod
    def _load_rules(path: Path) -> list[dict[str, Any]]:
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, dict):
            raise ValueError("sql_matcher.json must contain a top-level object")

        version = payload.get("version", 1)
        if type(version) is not int:
            raise ValueError("sql_matcher.json version must be an integer")
        if version != 1:
            raise ValueError("sql_matcher.json only supports version=1")

        rules = payload.get("rules")
        if not isinstance(rules, list) or not rules:
            raise ValueError("sql_matcher.json must define a non-empty rules list")
        normalized_rules: list[dict[str, Any]] = []
        for index, rule in enumerate(rules):
            try:
                normalized_rules.append(_normalize_rule(rule, index=index))
            except ValueError as exc:
                logger.warning(
                    "Invalid declarative matcher rule ignored: matcher_path=%s rule_index=%s error=%s",
                    str(path),
                    index,
                    exc,
                )
        return normalized_rules


def _resolve_ontology_version_dir(
    workspace_root: str,
    ontology_id: str,
) -> Optional[Path]:
    if not workspace_root or not ontology_id:
        return None

    ontology_root = (Path(workspace_root) / ontology_id).resolve()
    if not ontology_root.is_dir():
        return None

    try:
        version_dir = get_ontology_version_dir(ontology_root, create=False)
    except ValueError:
        version_dir = None
    return version_dir or ontology_root


def _resolve_template_path(
    workspace_root: str,
    ontology_id: str,
) -> Optional[Path]:
    version_dir = _resolve_ontology_version_dir(workspace_root, ontology_id)
    if version_dir is None:
        return None
    return (version_dir / "scripts" / "query_templates.json").resolve()


def _resolve_declarative_matcher_path(
    workspace_root: str,
    ontology_id: str,
) -> Optional[Path]:
    version_dir = _resolve_ontology_version_dir(workspace_root, ontology_id)
    if version_dir is None:
        return None
    return (version_dir / "scripts" / "sql_matcher.json").resolve()


def _resolve_legacy_matcher_path(
    workspace_root: str,
    ontology_id: str,
) -> Optional[Path]:
    version_dir = _resolve_ontology_version_dir(workspace_root, ontology_id)
    if version_dir is None:
        return None
    return (version_dir / "scripts" / "sql_matcher.py").resolve()


def _normalize_rule(rule: Any, *, index: int) -> dict[str, Any]:
    if not isinstance(rule, dict):
        raise ValueError("matcher rule {} must be an object".format(index))

    match_payload = rule.get("match")
    if isinstance(match_payload, str):
        match_payload = {"type": "exact", "value": match_payload}
    if not isinstance(match_payload, dict):
        raise ValueError("matcher rule {} must define a match object".format(index))

    match_type = str(match_payload.get("type") or "exact").strip().lower()
    ignore_case_raw = match_payload.get("ignore_case", False)
    if not isinstance(ignore_case_raw, bool):
        raise ValueError("matcher rule {} ignore_case must be a boolean".format(index))
    ignore_case = ignore_case_raw
    match_value_raw = match_payload.get("value")
    if not isinstance(match_value_raw, str):
        raise ValueError("matcher rule {} match value must be a string".format(index))
    match_value = match_value_raw.strip()
    if match_type not in {"exact", "contains"}:
        raise ValueError(
            "matcher rule {} supports only exact and contains match types".format(index)
        )
    if not match_value:
        raise ValueError("matcher rule {} has an empty match value".format(index))

    sql_template = rule.get("sql_template")
    if sql_template is not None:
        raise ValueError(
            "matcher rule {} does not support sql_template; use static sql".format(index)
        )
    sql_raw = rule.get("sql")
    if not isinstance(sql_raw, str):
        raise ValueError("matcher rule {} sql must be a string".format(index))
    sql = sql_raw.strip()
    if not sql:
        raise ValueError("matcher rule {} must define a static sql string".format(index))

    metadata_payload = rule.get("metadata")
    if metadata_payload is None:
        metadata_payload = {}
    if not isinstance(metadata_payload, dict):
        raise ValueError("matcher rule {} metadata must be an object".format(index))
    metadata = {
        str(key): value
        for key, value in metadata_payload.items()
        if str(key or "").strip() and _metadata_value_is_present(value)
    }
    _validate_reserved_metadata_value(metadata.get("from_template"), "from_template", index)
    _validate_reserved_metadata_value(metadata.get("dedupe_key"), "dedupe_key", index)
    from_template_raw = rule.get("from_template")
    if from_template_raw is not None and not isinstance(from_template_raw, str):
        raise ValueError("matcher rule {} from_template must be a string".format(index))
    from_template = str(from_template_raw or "").strip()
    if from_template:
        metadata.setdefault("from_template", from_template)
    dedupe_key_raw = rule.get("dedupe_key")
    if dedupe_key_raw is not None and not isinstance(dedupe_key_raw, str):
        raise ValueError("matcher rule {} dedupe_key must be a string".format(index))
    dedupe_key = str(dedupe_key_raw or "").strip()
    if dedupe_key:
        metadata["dedupe_key"] = dedupe_key

    normalized: dict[str, Any] = {
        "rule_index": index,
        "match_type": match_type,
        "match_value": match_value,
        "ignore_case": ignore_case,
        "sql": sql,
        "metadata": metadata,
    }
    return normalized


def _metadata_value_is_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str) and value == "":
        return False
    return True


def _validate_reserved_metadata_value(value: Any, field_name: str, index: int) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise ValueError(
            "matcher rule {} metadata.{} must be a string".format(index, field_name)
        )


def _match_rule(
    rule: dict[str, Any],
    query: str,
) -> tuple[str | None, dict[str, Any] | None]:
    match_type = str(rule.get("match_type") or "")
    match_value = str(rule.get("match_value") or "")
    ignore_case = bool(rule.get("ignore_case", False))
    metadata = dict(rule.get("metadata") or {})

    lowered_query = query.lower() if ignore_case else query
    lowered_value = match_value.lower() if ignore_case else match_value

    if match_type == "exact":
        if lowered_query != lowered_value:
            return None, None
    elif match_type == "contains":
        if lowered_value not in lowered_query:
            return None, None
    else:
        return None, None

    sql = str(rule.get("sql") or "").strip()
    if not sql:
        return None, None
    return sql, metadata or None


def _log_blocked_legacy_matcher(path: Path, ontology_id: str) -> None:
    logger.warning(
        _LEGACY_MATCHER_BLOCKED_ALERT,
        ontology_id,
        str(path),
        extra={
            "security_event": "untrusted_matcher_blocked",
            "ontology_id": ontology_id,
            "matcher_path": str(path),
        },
    )


class NL2SQLTemplateSearchAdapter:
    """Ontology search provider with declarative matcher support."""

    name = "nl2sql_template"

    def __init__(
        self,
        connection: EffectiveConnection,
        *,
        workspace_root: str = "",
        ontology_id: str = "",
    ):
        self._client = NL2SQLProvider(connection)
        self._session = self._client._session
        self.ontology_id = str(ontology_id or "").strip()
        self._workspace_root = str(workspace_root or "").strip()
        self._declarative_matcher = self._resolve_matcher()
        self._default_matcher = _DefaultMatcher(
            self._workspace_root,
            self.ontology_id,
        )

    def _resolve_matcher(self) -> Optional[MatcherFn]:
        """Discover a declarative matcher from the ontology directory."""
        if not self._workspace_root or not self.ontology_id:
            return None

        legacy_matcher = _resolve_legacy_matcher_path(
            self._workspace_root,
            self.ontology_id,
        )
        if legacy_matcher is not None and legacy_matcher.exists():
            _log_blocked_legacy_matcher(legacy_matcher, self.ontology_id)

        matcher_file = _resolve_declarative_matcher_path(
            self._workspace_root,
            self.ontology_id,
        )
        if matcher_file is None or not matcher_file.exists():
            return None

        try:
            return _DeclarativeMatcher(matcher_file)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "Invalid declarative matcher ignored: ontology_id=%s matcher_path=%s error=%s",
                self.ontology_id,
                str(matcher_file),
                exc,
            )
            return None

    def search_stream(
        self,
        *,
        query: str,
        entity: str,
    ) -> Iterator[BusinessDataSearchEvent]:
        del entity
        yield {"type": "start", "content": "开始查询本体数据"}

        sql = None
        metadata = None
        if self._declarative_matcher is not None:
            sql, metadata = self._declarative_matcher(query)
        if not sql:
            sql, metadata = self._default_matcher(query)

        if sql:
            yield {"type": "intermediate", "content": "模板命中，执行 SQL: {}".format(sql)}
            result = self._execute_sql(sql)

            if metadata:
                result["from_template"] = metadata.get("from_template")
                dedupe_key = metadata.get("dedupe_key")
                if dedupe_key and result.get("success"):
                    result = self._dedupe_result(result, dedupe_key)
        else:
            yield {"type": "intermediate", "content": "模板未命中，使用自然语言查询"}
            result = self._execute_query(query)

        yield {
            "type": "final",
            "content": json.dumps(_format_output(result, show_sql=False), ensure_ascii=False),
        }
        yield {"type": "done", "content": "查询流程结束"}

    @staticmethod
    def _dedupe_result(
        result: dict,
        dedupe_key: str,
    ) -> dict:
        """Deduplicate rows by dedupe_key, keeping the first occurrence per key value."""
        data = result.get("data")
        if not data or not isinstance(data, list):
            return result

        seen: set[str] = set()
        deduped: list[dict] = []
        for row in data:
            if not isinstance(row, dict):
                continue
            key = str(row.get(dedupe_key, ""))
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(row)

        result["data"] = deduped
        return result

    def _execute_query(self, question: str) -> Dict[str, Any]:
        return _result_to_legacy_dict(self._client.query(question))

    def _execute_sql(self, sql: str) -> Dict[str, Any]:
        return _result_to_legacy_dict(self._client.query_sql(sql))

    def close(self) -> None:
        self._client.close()


def _result_to_legacy_dict(result) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "success": result.success,
        "data": result.data,
        **dict(result.metadata),
    }
    if result.success:
        payload["message"] = result.message
    else:
        payload["error"] = result.error
    return payload


def _format_output(result: dict, show_sql: bool = False) -> dict:
    output = {
        "success": result.get("success", False),
        "data": result.get("data"),
    }
    if show_sql and "sql" in result:
        output["sql"] = result["sql"]

    if result.get("from_template"):
        output["from_template"] = result["from_template"]

    if result.get("success"):
        row_count = result.get("row_count", 0)
        if row_count > 0:
            output["message"] = "查询成功，返回 {} 行数据".format(row_count)
        else:
            output["message"] = "查询成功，无数据返回"
    else:
        output["message"] = "查询失败: {}".format(result.get("error", "未知错误"))
    return output
