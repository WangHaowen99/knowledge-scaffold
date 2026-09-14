"""Unified logging configuration for agentic."""

from __future__ import annotations

import gzip
import json
import logging
import logging.handlers
import os
import re
import shutil
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from operator import getitem
from typing import Any, Optional

LOG_DIR = "logs"
LOG_FILE = "agent.log"
DEFAULT_FORMAT = "%(asctime)s %(levelname)s %(name)s:%(lineno)d [P%(process)d|T%(thread)d] %(message)s"
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LOG_LEVEL_ENV = "LOG_LEVEL"
LOG_STRUCTURED_ENV = "LOG_STRUCTURED"
LOG_CONSOLE_ENV = "LOG_CONSOLE"
LOG_MAX_BYTES_ENV = "LOG_MAX_BYTES"
LOG_BACKUP_COUNT_ENV = "LOG_BACKUP_COUNT"
DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # 5MB
DEFAULT_BACKUP_COUNT = 5
DEFAULT_AGENT_LOG_RETENTION_DAYS = 180
DEFAULT_AGENT_LOG_COMPRESS_OLD = True

_LOGGING_CONFIGURED = False
_CONFIGURED_LOG_LEVEL = logging.INFO
_CURRENT_RUNTIME_LOG_LEVEL: Optional[int] = None
_OPS_CONFIG_SIGNATURE: Optional[tuple[str, bool, int, int, int]] = None
_LOG_LEVEL_FROM_OPS_CONFIG: Optional[int] = None
_LOG_LEVEL_LOCK = threading.RLock()
_LOG_CONTEXT: ContextVar[dict[str, Any]] = ContextVar("agentic_log_context", default={})
_LOG_CONTEXT_FIELDS = (
    "agent_type",
    "agent_id",
    "run_id",
    "session_id",
    "turn_id",
    "task_id",
    "group_id",
    "depth",
    "parent_subagent_id",
    "root_tool_call_id",
    "parent_tool_call_id",
)


def logging_state_for_test() -> dict[str, Any]:
    """Capture mutable module logging state for isolated configuration tests."""
    return {
        "configured": _LOGGING_CONFIGURED,
        "configured_level": _CONFIGURED_LOG_LEVEL,
        "runtime_level": _CURRENT_RUNTIME_LOG_LEVEL,
        "ops_config_signature": _OPS_CONFIG_SIGNATURE,
        "ops_config_level": _LOG_LEVEL_FROM_OPS_CONFIG,
    }


def restore_logging_state_for_test(state: dict[str, Any]) -> None:
    """Restore state captured by :func:`logging_state_for_test`."""
    global _LOGGING_CONFIGURED, _CONFIGURED_LOG_LEVEL
    global _CURRENT_RUNTIME_LOG_LEVEL, _OPS_CONFIG_SIGNATURE
    global _LOG_LEVEL_FROM_OPS_CONFIG
    _LOGGING_CONFIGURED = bool(state["configured"])
    _CONFIGURED_LOG_LEVEL = int(state["configured_level"])
    _CURRENT_RUNTIME_LOG_LEVEL = state["runtime_level"]
    _OPS_CONFIG_SIGNATURE = state["ops_config_signature"]
    _LOG_LEVEL_FROM_OPS_CONFIG = state["ops_config_level"]


def mark_logging_unconfigured_for_test() -> None:
    """Allow an isolated test to configure the root logger from a clean state."""
    global _LOGGING_CONFIGURED
    _LOGGING_CONFIGURED = False

_RESERVED_RECORD_KEYS = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "message",
    "suppress_log_context",
}


def _parse_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _coerce_level(level: Optional[int | str]) -> int:
    if level is None:
        return logging.INFO
    if isinstance(level, int):
        return level
    resolved = getattr(logging, level.upper(), None)
    return resolved if isinstance(resolved, int) else logging.INFO


def _json_default(value: Any) -> str:
    return str(value)


def _normalize_log_context(context: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(context, dict):
        return {}

    normalized: dict[str, Any] = {}
    for key in _LOG_CONTEXT_FIELDS:
        if key not in context:
            continue
        value = context.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        normalized[key] = value
    return normalized


def get_log_context() -> dict[str, Any]:
    """Return the current logging context for the active execution scope."""
    return dict(_LOG_CONTEXT.get() or {})


@contextmanager
def bind_log_context(
    context: Optional[dict[str, Any]] = None,
    **kwargs: Any,
):
    """Bind agent execution metadata to logs emitted in the current scope."""
    merged = get_log_context()
    merged.update(_normalize_log_context(context))
    merged.update(_normalize_log_context(kwargs))
    token = _LOG_CONTEXT.set(merged)
    try:
        yield merged
    finally:
        _LOG_CONTEXT.reset(token)


def _format_agent_context(context: dict[str, Any]) -> str:
    if not context:
        return ""

    parts: list[str] = []
    agent_type = str(context.get("agent_type") or "").strip()
    agent_id = str(context.get("agent_id") or "").strip()
    if agent_type and agent_id and agent_id != agent_type:
        parts.append("agent={}:{}".format(agent_type, agent_id))
    elif agent_type:
        parts.append("agent={}".format(agent_type))
    elif agent_id:
        parts.append("agent_id={}".format(agent_id))

    for key in (
        "run_id",
        "turn_id",
        "task_id",
        "group_id",
        "depth",
        "parent_subagent_id",
        "root_tool_call_id",
        "parent_tool_call_id",
    ):
        value = context.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        parts.append("{}={}".format(key, value))

    return " [{}]".format(" ".join(parts)) if parts else ""


def _enrich_record_with_log_context(record: logging.LogRecord) -> dict[str, Any]:
    if getattr(record, "suppress_log_context", False):
        record.__dict__.setdefault("agent_context", "")
        return {}
    context = get_log_context()
    if context:
        for key, value in context.items():
            record.__dict__.setdefault(key, value)

    record_context = {
        key: record.__dict__.get(key) for key in _LOG_CONTEXT_FIELDS if record.__dict__.get(key) is not None
    }
    record.__dict__.setdefault("agent_context", _format_agent_context(record_context))
    return record_context


def _get_ops_config_signature() -> tuple[str, bool, int, int, int]:
    from config.ops_config import resolve_ops_config_path

    config_path = resolve_ops_config_path()
    try:
        stat = config_path.stat()
    except OSError:
        return str(config_path), False, 0, 0, 0
    return str(config_path), True, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size


def _load_log_level_from_ops_config(*, force_reload: bool = False) -> Optional[int]:
    global _LOG_LEVEL_FROM_OPS_CONFIG, _OPS_CONFIG_SIGNATURE

    current_signature = _get_ops_config_signature()

    if not force_reload and current_signature == _OPS_CONFIG_SIGNATURE:
        return _LOG_LEVEL_FROM_OPS_CONFIG

    previous_value = _LOG_LEVEL_FROM_OPS_CONFIG

    try:
        from config.ops_config import load_ops_config

        raw_level = load_ops_config()["logging"]["level"]
        normalized_level = str(raw_level or "").strip().upper()
        if not normalized_level:
            resolved_level = None
        else:
            resolved_level = getattr(logging, normalized_level, None)
            if not isinstance(resolved_level, int):
                raise ValueError("invalid logging.level: {}".format(raw_level))
    except Exception:
        resolved_level = previous_value

    _OPS_CONFIG_SIGNATURE = current_signature
    _LOG_LEVEL_FROM_OPS_CONFIG = resolved_level
    return resolved_level


def _apply_log_level(level: int) -> None:
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    for handler in root_logger.handlers:
        handler.setLevel(level)


def refresh_log_level(*, force: bool = False) -> int:
    """Refresh the effective log level when ops_config.json changes."""
    global _CURRENT_RUNTIME_LOG_LEVEL

    resolved_level = _load_log_level_from_ops_config(force_reload=force)
    if resolved_level is None:
        resolved_level = _CONFIGURED_LOG_LEVEL
    if not force and resolved_level == _CURRENT_RUNTIME_LOG_LEVEL:
        return resolved_level

    with _LOG_LEVEL_LOCK:
        resolved_level = _load_log_level_from_ops_config(force_reload=force)
        if resolved_level is None:
            resolved_level = _CONFIGURED_LOG_LEVEL
        if force or resolved_level != _CURRENT_RUNTIME_LOG_LEVEL:
            _apply_log_level(resolved_level)
            _CURRENT_RUNTIME_LOG_LEVEL = resolved_level
        return _CURRENT_RUNTIME_LOG_LEVEL


class DynamicLogLevelLogger(logging.getLoggerClass()):
    """Logger that refreshes the effective level before each enabled check."""

    __agentic_dynamic_log_level__ = True

    def isEnabledFor(self, level: int) -> bool:
        refresh_log_level()
        return super().isEnabledFor(level)


if not getattr(logging.getLoggerClass(), "__agentic_dynamic_log_level__", False):
    logging.setLoggerClass(DynamicLogLevelLogger)


class JsonFormatter(logging.Formatter):
    """JSON formatter that includes extra fields for structured logging."""

    def format(self, record: logging.LogRecord) -> str:
        _enrich_record_with_log_context(record)
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, DEFAULT_DATE_FORMAT),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "line": record.lineno,
            "function": record.funcName,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        extras = {k: v for k, v in record.__dict__.items() if k not in _RESERVED_RECORD_KEYS}
        payload.update(extras)
        return json.dumps(payload, default=_json_default, ensure_ascii=False)


class ContextFormatter(logging.Formatter):
    """Text formatter that appends agent execution context when available."""

    def format(self, record: logging.LogRecord) -> str:
        _enrich_record_with_log_context(record)
        return super().format(record)


class DatePartitionedRotatingFileHandler(logging.Handler):
    """Write logs into date-partitioned files while keeping size-based rollover."""

    def __init__(
        self,
        base_log_path: str,
        *,
        encoding: str = "utf-8",
        max_bytes: int = 0,
        backup_count: int = 0,
        retention_days: Optional[int] = None,
        compress_old: bool = False,
    ) -> None:
        super().__init__()
        self.base_log_path = os.path.abspath(base_log_path)
        self.baseFilename = self.base_log_path  # pylint: disable=invalid-name
        self.encoding = encoding
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.retention_days = retention_days if retention_days and retention_days > 0 else None
        self.compress_old = bool(compress_old)
        self._current_handler: Optional[logging.handlers.RotatingFileHandler] = None
        self._current_path: Optional[str] = None
        self._file_pattern = self._build_file_pattern()

    def _build_file_pattern(self) -> re.Pattern[str]:
        directory, filename = os.path.split(self.base_log_path)
        stem, ext = os.path.splitext(filename)
        escaped_stem = re.escape(stem)
        escaped_ext = re.escape(ext)
        return re.compile(
            r"^{}-(\d{{4}}-\d{{2}}-\d{{2}}){}(?:\.\d+)?(?:\.gz)?$".format(
                escaped_stem,
                escaped_ext,
            )
        )

    def _build_dated_path(self, date_part: str) -> str:
        directory, filename = os.path.split(self.base_log_path)
        stem, ext = os.path.splitext(filename)
        return os.path.join(directory, "{}-{}{}".format(stem, date_part, ext))

    def _ensure_handler(self, record: logging.LogRecord) -> None:
        date_part = time.strftime("%Y-%m-%d", time.localtime(record.created))
        target_path = self._build_dated_path(date_part)
        if self._current_path == target_path and self._current_handler is not None:
            return

        if self._current_handler is not None:
            self._current_handler.close()

        os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            target_path,
            encoding=self.encoding,
            maxBytes=self.max_bytes,
            backupCount=self.backup_count,
        )
        handler.setLevel(self.level)
        if self.formatter is not None:
            handler.setFormatter(self.formatter)

        self._current_handler = handler
        self._current_path = target_path
        self._prune_old_date_groups(date_part)
        if self.compress_old:
            self._compress_old_date_groups(date_part)

    def _prune_old_date_groups(self, current_date_part: str) -> None:
        if self.backup_count < 0:
            return

        directory, _ = os.path.split(self.base_log_path)
        date_groups: dict[str, list[str]] = {}

        for entry in os.listdir(directory or "."):
            match = self._file_pattern.match(entry)
            if not match:
                continue
            date_groups.setdefault(match.group(1), []).append(os.path.join(directory, entry))

        if self.retention_days is not None:
            try:
                current_date = datetime.strptime(current_date_part, "%Y-%m-%d").date()
                cutoff_date = current_date - timedelta(days=self.retention_days - 1)
                stale_dates = [
                    date_part
                    for date_part in sorted(date_groups)
                    if datetime.strptime(date_part, "%Y-%m-%d").date() < cutoff_date
                ]
            except ValueError:
                stale_dates = []
        else:
            keep_groups = max(self.backup_count + 1, 1)
            stale_dates = sorted(date_groups)[:-keep_groups]
        for stale_date in stale_dates:
            for stale_path in getitem(date_groups, stale_date):
                try:
                    os.remove(stale_path)
                except FileNotFoundError:
                    continue

    def _compress_old_date_groups(self, current_date_part: str) -> None:
        directory, _ = os.path.split(self.base_log_path)
        for entry in os.listdir(directory or "."):
            match = self._file_pattern.match(entry)
            if not match or match.group(1) == current_date_part or entry.endswith(".gz"):
                continue
            source_path = os.path.join(directory, entry)
            target_path = "{}.gz".format(source_path)
            if os.path.abspath(source_path) == os.path.abspath(self._current_path or ""):
                continue
            try:
                if os.path.exists(target_path):
                    os.remove(source_path)
                    continue
                with open(source_path, "rb") as source, gzip.open(target_path, "wb") as target:
                    shutil.copyfileobj(source, target)
                os.remove(source_path)
            except FileNotFoundError:
                continue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._ensure_handler(record)
            if self._current_handler is not None:
                self._current_handler.emit(record)
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        try:
            if self._current_handler is not None:
                self._current_handler.close()
                self._current_handler = None
                self._current_path = None
        finally:
            super().close()

    def setFormatter(self, fmt: Optional[logging.Formatter]) -> None:
        super().setFormatter(fmt)
        if self._current_handler is not None:
            self._current_handler.setFormatter(fmt)

    def setLevel(self, level: int | str) -> None:
        super().setLevel(level)
        if self._current_handler is not None:
            self._current_handler.setLevel(level)


def configure_logging(
    *,
    level: Optional[int | str] = None,
    log_dir: Optional[str] = None,
    log_file: Optional[str] = None,
    structured: Optional[bool] = None,
    console: Optional[bool] = None,
    max_bytes: Optional[int] = None,
    backup_count: Optional[int] = None,
    retention_days: Optional[int] = None,
    compress_old: Optional[bool] = None,
    force: bool = False,
) -> None:
    """Configure root logging handlers and formatting."""
    global _CONFIGURED_LOG_LEVEL, _CURRENT_RUNTIME_LOG_LEVEL, _LOGGING_CONFIGURED
    global _LOG_LEVEL_FROM_OPS_CONFIG, _OPS_CONFIG_SIGNATURE
    if _LOGGING_CONFIGURED and not force:
        return

    _OPS_CONFIG_SIGNATURE = None
    _LOG_LEVEL_FROM_OPS_CONFIG = None

    base_level = level if level is not None else os.getenv(LOG_LEVEL_ENV)
    resolved_level = _coerce_level(base_level)
    _CONFIGURED_LOG_LEVEL = resolved_level
    config_level = _load_log_level_from_ops_config(force_reload=True)
    if config_level is not None:
        resolved_level = config_level
    _CURRENT_RUNTIME_LOG_LEVEL = None
    resolved_structured = structured if structured is not None else _parse_bool(os.getenv(LOG_STRUCTURED_ENV), False)
    resolved_console = console if console is not None else _parse_bool(os.getenv(LOG_CONSOLE_ENV), False)
    resolved_dir = log_dir or os.getenv("LOG_DIR", LOG_DIR)
    resolved_file = log_file or os.getenv("LOG_FILE", LOG_FILE)

    env_max_bytes = os.getenv(LOG_MAX_BYTES_ENV)
    resolved_max_bytes = (
        max_bytes if max_bytes is not None else (int(env_max_bytes) if env_max_bytes else DEFAULT_MAX_BYTES)
    )

    env_backup_count = os.getenv(LOG_BACKUP_COUNT_ENV)
    resolved_backup_count = (
        backup_count
        if backup_count is not None
        else (int(env_backup_count) if env_backup_count else DEFAULT_BACKUP_COUNT)
    )
    default_retention_days, default_compress_old = _load_agent_log_retention_policy()
    resolved_retention_days = retention_days if retention_days is not None else default_retention_days
    resolved_compress_old = compress_old if compress_old is not None else default_compress_old

    os.makedirs(resolved_dir, exist_ok=True)
    log_path = os.path.join(resolved_dir, resolved_file)

    formatter: logging.Formatter
    if resolved_structured:
        formatter = JsonFormatter()
    else:
        formatter = ContextFormatter(
            DEFAULT_FORMAT + "%(agent_context)s",
            datefmt=DEFAULT_DATE_FORMAT,
        )

    root_logger = logging.getLogger()

    if force:
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
            handler.close()

    resolved_log_path = os.path.abspath(log_path)
    matching_file_handlers = []
    for handler in root_logger.handlers:
        matches_base_path = getattr(handler, "base_log_path", None) == resolved_log_path
        matches_filename = getattr(handler, "baseFilename", None) == resolved_log_path
        if matches_base_path or matches_filename:
            matching_file_handlers.append(handler)

    if not any(isinstance(handler, DatePartitionedRotatingFileHandler) for handler in matching_file_handlers):
        for handler in matching_file_handlers:
            root_logger.removeHandler(handler)
            handler.close()

        file_handler = DatePartitionedRotatingFileHandler(
            log_path,
            encoding="utf-8",
            max_bytes=resolved_max_bytes,
            backup_count=resolved_backup_count,
            retention_days=resolved_retention_days,
            compress_old=resolved_compress_old,
        )
        file_handler.setLevel(resolved_level)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    if resolved_console and not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in root_logger.handlers
    ):
        console_handler = logging.StreamHandler()
        console_handler.setLevel(resolved_level)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

    refresh_log_level(force=True)
    _LOGGING_CONFIGURED = True


def _load_agent_log_retention_policy() -> tuple[int, bool]:
    try:
        from config import LimitsConfig

        limits = LimitsConfig.from_env()
        return limits.agent_log_retention_days, limits.agent_log_compress_old
    except Exception:
        return DEFAULT_AGENT_LOG_RETENTION_DAYS, DEFAULT_AGENT_LOG_COMPRESS_OLD


def get_logger(name: str) -> logging.Logger:
    """Return a logger configured with the shared handlers."""
    if not _LOGGING_CONFIGURED:
        configure_logging()
    return logging.getLogger(name)
