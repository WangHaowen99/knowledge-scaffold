"""NL2SQL Template ontology search provider with pluggable matcher support."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional, Tuple

from ontology.ontologies import get_ontology_version_dir
from agent.tools.business_data.adapters.base import BusinessDataSearchEvent
from shared_capabilities.business_data import EffectiveConnection, NL2SQLProvider
from utils.logging import get_logger


logger = get_logger("business_data.nl2sql_template")

# Matcher protocol type: (query) -> (sql | None, metadata | None)
MatcherFn = Callable[[str], Tuple[Optional[str], Optional[Dict[str, Any]]]]


class _DefaultMatcher:
    """Built-in exact-match matcher that loads scripts/query_templates.json and does key lookup."""

    def __init__(
        self,
        workspace_root: str,
        ontology_id: str,
        ontology_version: str = "",
    ) -> None:
        self._templates = self._load_templates(
            workspace_root,
            ontology_id,
            ontology_version,
        )

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

    # -- template loading --

    @staticmethod
    def _load_templates(
        workspace_root: str,
        ontology_id: str,
        ontology_version: str = "",
    ) -> dict[str, Any]:
        template_file = _resolve_template_path(
            workspace_root,
            ontology_id,
            ontology_version,
        )
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


def _resolve_ontology_version_dir(
    workspace_root: str,
    ontology_id: str,
    ontology_version: str = "",
) -> Optional[Path]:
    if not workspace_root or not ontology_id:
        return None

    ontology_root = (Path(workspace_root) / ontology_id).resolve()
    if not ontology_root.is_dir():
        return None

    try:
        version_dir = get_ontology_version_dir(
            ontology_root,
            str(ontology_version or "").strip() or None,
            create=False,
        )
    except ValueError:
        version_dir = None
    return version_dir or ontology_root


def _resolve_template_path(
    workspace_root: str,
    ontology_id: str,
    ontology_version: str = "",
) -> Optional[Path]:
    version_dir = _resolve_ontology_version_dir(
        workspace_root,
        ontology_id,
        ontology_version,
    )
    if version_dir is None:
        return None
    return (version_dir / "scripts" / "query_templates.json").resolve()


class NL2SQLTemplateSearchAdapter:
    """Ontology search provider with pluggable matcher support.

    Matcher discovery:
    - Resolve the active ontology version directory under ``<workspace_root>/<ontology_id>``.
    - If ``<version_dir>/scripts/sql_matcher.py`` exists, load and use it.
    - Otherwise, fall back to the built-in ``_DefaultMatcher`` (exact key lookup).

    The matcher function signature:
        def matcher(query: str) -> tuple[str | None, dict | None]
    Returns (filled_sql, metadata) on match, (None, None) to fall back to NL2SQL.
    """

    name = "nl2sql_template"

    def __init__(
        self,
        connection: EffectiveConnection,
        *,
        workspace_root: str = "",
        workspace_id: str = "",
        ontology_id: str = "",
        ontology_version: str = "",
    ):
        self._client = NL2SQLProvider(connection)
        self.ontology_id = str(ontology_id or "").strip()
        self.ontology_version = str(ontology_version or "").strip()
        self._workspace_root = str(workspace_root or "").strip()
        self.workspace_id = str(workspace_id or "").strip()

        # Resolve matcher: external sql_matcher.py or built-in default
        self._matcher: MatcherFn = self._resolve_matcher() or _DefaultMatcher(
            self._workspace_root,
            self.ontology_id,
            self.ontology_version,
        )

    @property
    def post_callback_for_test(self):
        return None

    @post_callback_for_test.setter
    def post_callback_for_test(self, callback) -> None:
        self._client.set_post_for_test(callback)

    # -- matcher discovery --

    def _resolve_matcher(self) -> Optional[MatcherFn]:
        """Discover and load an external matcher from the ontology directory."""
        if not self._workspace_root or not self.ontology_id:
            return None

        version_dir = _resolve_ontology_version_dir(
            self._workspace_root,
            self.ontology_id,
            self.ontology_version,
        )
        if version_dir is None:
            return None

        matcher_file = version_dir / "scripts" / "sql_matcher.py"
        if not matcher_file.exists():
            return None

        try:
            matcher = self._load_matcher_module(matcher_file)
        except Exception as exc:
            self._log_legacy_matcher_load(
                matcher_file,
                status="failed",
                error=exc,
            )
            raise
        self._log_legacy_matcher_load(matcher_file, status="loaded")
        return matcher

    def _log_legacy_matcher_load(
        self,
        path: Path,
        *,
        status: str,
        error: Exception | None = None,
    ) -> None:
        """Audit backend execution of a legacy Python matcher."""
        error_type = type(error).__name__ if error is not None else ""
        logger.warning(
            (
                "LEGACY_SQL_MATCHER_LOAD: status=%s workspace_id=%s ontology_id=%s "
                "ontology_version=%s matcher_path=%s error=%s"
            ),
            status,
            self.workspace_id,
            self.ontology_id,
            self.ontology_version,
            str(path),
            error_type,
            extra={
                "security_event": "legacy_sql_matcher_load",
                "execution_decision": "backend_dynamic_import",
                "legacy_capability": "sql_matcher.py",
                "provider": self.name,
                "load_status": status,
                "workspace_id": self.workspace_id,
                "ontology_id": self.ontology_id,
                "ontology_version": self.ontology_version,
                "matcher_path": str(path),
                "error_type": error_type,
            },
        )

    @staticmethod
    def _load_matcher_module(path: Path) -> MatcherFn:
        """Dynamically load a matcher module and return its ``matcher`` function."""
        spec = importlib.util.spec_from_file_location("ontology_sql_matcher", path)
        if spec is None or spec.loader is None:
            raise ImportError("Cannot load matcher module from {}".format(path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        matcher_fn = getattr(module, "matcher", None)
        if not callable(matcher_fn):
            raise AttributeError(
                "sql_matcher.py must define a callable 'matcher' function"
            )
        return matcher_fn

    # -- search entry point --

    def search_stream(
        self,
        *,
        query: str,
        entity: str,
    ) -> Iterator[BusinessDataSearchEvent]:
        del entity
        yield {"type": "start", "content": "开始查询本体数据"}

        sql, metadata = self._matcher(query)

        if sql:
            yield {
                "type": "intermediate",
                "content": "模板命中，执行 SQL: {}".format(sql),
                "public_summary": "模板命中，正在执行预定义查询",
                "public_summary_allowed": True,
            }
            result = self._execute_sql(sql)

            if metadata:
                result["from_template"] = metadata.get("from_template")

                # Apply dedupe post-processing if dedupe_key is set
                dedupe_key = metadata.get("dedupe_key")
                if dedupe_key and result.get("success"):
                    result = self.dedupe_result(result, dedupe_key)
        else:
            # No match — fall back to NL2SQL
            yield {
                "type": "intermediate",
                "content": "模板未命中，使用自然语言查询",
                "public_summary": "模板未命中，改用自然语言查询",
                "public_summary_allowed": True,
            }
            result = self._execute_query(query)

        yield {
            "type": "final",
            "content": json.dumps(_format_output(result, show_sql=False), ensure_ascii=False),
        }
        yield {"type": "done", "content": "查询流程结束"}

    # -- dedupe post-processing --

    @staticmethod
    def dedupe_result(
        result: dict,
        dedupe_key: str,
    ) -> dict:
        """Deduplicate rows by dedupe_key, keeping the first occurrence per key value.

        Requires the SQL to have already sorted rows so that the desired record
        appears first within each dedupe_key value (e.g. ORDER BY updateTime DESC).
        """
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

    # -- SQL / NL2SQL execution (unchanged) --

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
        elif result.get("message"):
            # Value-domain lookups return a text answer with no rows; preserve
            # the provider's original message instead of discarding it.
            output["message"] = result["message"]
        else:
            output["message"] = "查询成功，无数据返回"
    else:
        output["message"] = "查询失败: {}".format(result.get("error", "未知错误"))
    return output
