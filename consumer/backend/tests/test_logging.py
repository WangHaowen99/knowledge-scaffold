# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Tests for date-partitioned backend logging."""

from internal_access import internal
import gzip
import json
import logging
import logging.handlers
import os
from datetime import date, datetime
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
import utils.logging as logging_utils

DatePartitionedRotatingFileHandler = logging_utils.DatePartitionedRotatingFileHandler
configure_logging = logging_utils.configure_logging


def _record(message: str, created_at: float) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    record.created = created_at
    record.msecs = (created_at - int(created_at)) * 1000
    return record


def _write_ops_config(config_path: Path, level: str | None) -> None:
    document = {"logging": {"level": level}} if level is not None else {}
    config_path.write_text(json.dumps(document), encoding="utf-8")


def _rewrite_ops_config(config_path: Path, content: str) -> None:
    previous_mtime_ns = config_path.stat().st_mtime_ns
    config_path.write_text(content, encoding="utf-8")
    current_mtime_ns = config_path.stat().st_mtime_ns
    if current_mtime_ns == previous_mtime_ns:
        os.utime(config_path, ns=(previous_mtime_ns + 1, previous_mtime_ns + 1))


@pytest.fixture(autouse=True)
def _restore_root_logging(tmp_path, monkeypatch):
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level
    original_configured = internal(logging_utils).LOGGING_CONFIGURED
    original_configured_level = internal(logging_utils).CONFIGURED_LOG_LEVEL
    original_runtime_level = internal(logging_utils).CURRENT_RUNTIME_LOG_LEVEL
    original_config_signature = internal(logging_utils).OPS_CONFIG_SIGNATURE
    original_config_value = internal(logging_utils).LOG_LEVEL_FROM_OPS_CONFIG

    monkeypatch.setenv("OPS_CONFIG_PATH", str(tmp_path / "missing-ops-config.json"))

    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
    internal(logging_utils).LOGGING_CONFIGURED = False

    try:
        yield
    finally:
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
            handler.close()
        for handler in original_handlers:
            root_logger.addHandler(handler)
        root_logger.setLevel(original_level)
        internal(logging_utils).LOGGING_CONFIGURED = original_configured
        internal(logging_utils).CONFIGURED_LOG_LEVEL = original_configured_level
        internal(logging_utils).CURRENT_RUNTIME_LOG_LEVEL = original_runtime_level
        internal(logging_utils).OPS_CONFIG_SIGNATURE = original_config_signature
        internal(logging_utils).LOG_LEVEL_FROM_OPS_CONFIG = original_config_value


def test_date_partitioned_handler_writes_same_day_records_to_one_file(tmp_path):
    handler = DatePartitionedRotatingFileHandler(
        str(tmp_path / "agent.log"),
        max_bytes=0,
        backup_count=5,
    )
    handler.setFormatter(logging.Formatter("%(message)s"))

    handler.emit(_record("first", datetime(2026, 4, 7, 10, 0, 0).timestamp()))
    handler.emit(_record("second", datetime(2026, 4, 7, 22, 0, 0).timestamp()))
    handler.close()

    log_file = tmp_path / "agent-2026-04-07.log"
    assert log_file.exists()
    assert log_file.read_text(encoding="utf-8") == "first\nsecond\n"


def test_date_partitioned_handler_prunes_older_date_groups(tmp_path):
    handler = DatePartitionedRotatingFileHandler(
        str(tmp_path / "agent.log"),
        max_bytes=0,
        backup_count=2,
    )
    handler.setFormatter(logging.Formatter("%(message)s"))

    for day in (5, 6, 7, 8):
        handler.emit(_record("day-{}".format(day), datetime(2026, 4, day, 12, 0, 0).timestamp()))
        if day == 5:
            (tmp_path / "agent-2026-04-05.log.1").write_text("rolled\n", encoding="utf-8")

    handler.close()

    assert not (tmp_path / "agent-2026-04-05.log").exists()
    assert not (tmp_path / "agent-2026-04-05.log.1").exists()
    assert (tmp_path / "agent-2026-04-06.log").exists()
    assert (tmp_path / "agent-2026-04-07.log").exists()
    assert (tmp_path / "agent-2026-04-08.log").exists()


def test_date_partitioned_handler_prunes_by_retention_days(tmp_path):
    handler = DatePartitionedRotatingFileHandler(
        str(tmp_path / "agent.log"),
        max_bytes=0,
        backup_count=10,
        retention_days=2,
    )
    handler.setFormatter(logging.Formatter("%(message)s"))

    for day in (5, 6, 7, 8):
        handler.emit(_record("day-{}".format(day), datetime(2026, 4, day, 12, 0, 0).timestamp()))
        if day == 5:
            (tmp_path / "agent-2026-04-05.log.1").write_text("rolled\n", encoding="utf-8")

    handler.close()

    assert not (tmp_path / "agent-2026-04-05.log").exists()
    assert not (tmp_path / "agent-2026-04-05.log.1").exists()
    assert not (tmp_path / "agent-2026-04-06.log").exists()
    assert (tmp_path / "agent-2026-04-07.log").exists()
    assert (tmp_path / "agent-2026-04-08.log").exists()


def test_date_partitioned_handler_compresses_old_date_groups(tmp_path):
    handler = DatePartitionedRotatingFileHandler(
        str(tmp_path / "agent.log"),
        max_bytes=0,
        backup_count=10,
        retention_days=180,
        compress_old=True,
    )
    handler.setFormatter(logging.Formatter("%(message)s"))

    handler.emit(_record("old-day", datetime(2026, 4, 7, 12, 0, 0).timestamp()))
    handler.emit(_record("current-day", datetime(2026, 4, 8, 12, 0, 0).timestamp()))
    handler.close()

    assert not (tmp_path / "agent-2026-04-07.log").exists()
    compressed_path = tmp_path / "agent-2026-04-07.log.gz"
    assert compressed_path.exists()
    with gzip.open(compressed_path, "rt", encoding="utf-8") as f:
        assert f.read() == "old-day\n"
    assert (tmp_path / "agent-2026-04-08.log").exists()


def test_configure_logging_uses_date_partitioned_file_handler(tmp_path):
    legacy_handler = logging.handlers.RotatingFileHandler(
        str(tmp_path / "agent.log"),
        encoding="utf-8",
    )
    logging.getLogger().addHandler(legacy_handler)

    configure_logging(
        log_dir=str(tmp_path),
        log_file="agent.log",
        console=False,
        max_bytes=0,
        backup_count=2,
    )

    root_logger = logging.getLogger()
    assert any(isinstance(handler, DatePartitionedRotatingFileHandler) for handler in root_logger.handlers)
    assert not any(
        isinstance(handler, logging.handlers.RotatingFileHandler)
        and getattr(handler, "baseFilename", None) == str(tmp_path / "agent.log")
        for handler in root_logger.handlers
    )

    logger = logging_utils.get_logger("tests.logging")
    logger.info("configured handler works")

    log_file = tmp_path / "agent-{}.log".format(date.today().isoformat())
    assert log_file.exists()
    assert "configured handler works" in log_file.read_text(encoding="utf-8")


def test_configure_logging_defaults_to_180_day_compressed_agent_logs(tmp_path):
    configure_logging(
        log_dir=str(tmp_path),
        log_file="agent.log",
        console=False,
        max_bytes=0,
        backup_count=2,
    )

    handler = next(
        handler for handler in logging.getLogger().handlers if isinstance(handler, DatePartitionedRotatingFileHandler)
    )

    assert handler.retention_days == 180
    assert handler.compress_old is True


def test_configure_logging_appends_agent_context_to_plain_logs(tmp_path):
    configure_logging(
        log_dir=str(tmp_path),
        log_file="agent.log",
        console=False,
        max_bytes=0,
        backup_count=2,
    )

    logger = logging_utils.get_logger("tests.context")
    with logging_utils.bind_log_context(
        {
            "agent_type": "subagent",
            "agent_id": "sub_123",
            "run_id": "run-ctx",
            "task_id": "task-ctx",
            "depth": 2,
        }
    ):
        logger.info("context aware log")

    log_file = tmp_path / "agent-{}.log".format(date.today().isoformat())
    content = log_file.read_text(encoding="utf-8")

    assert "context aware log" in content
    assert "agent=subagent:sub_123" in content
    assert "run_id=run-ctx" in content
    assert "task_id=task-ctx" in content


def test_runtime_log_level_reloads_from_ops_config_on_next_log_check(tmp_path, monkeypatch):
    config_path = tmp_path / "ops_config.json"
    _write_ops_config(config_path, "INFO")
    monkeypatch.setenv("OPS_CONFIG_PATH", str(config_path))

    configure_logging(
        level="INFO",
        log_dir=str(tmp_path),
        log_file="agent.log",
        console=False,
        max_bytes=0,
        backup_count=2,
    )

    logger = logging_utils.get_logger("tests.dynamic")
    log_file = tmp_path / "agent-{}.log".format(date.today().isoformat())

    assert not logger.isEnabledFor(logging.DEBUG)
    logger.debug("hidden-before-change")

    _rewrite_ops_config(config_path, '{"logging":{"level":"DEBUG"}}')
    assert logger.isEnabledFor(logging.DEBUG)
    logger.debug("visible-after-change")

    _rewrite_ops_config(config_path, '{"logging":{"level":"INFO"}}')
    assert not logger.isEnabledFor(logging.DEBUG)
    logger.debug("hidden-after-reset")

    content = log_file.read_text(encoding="utf-8")
    assert "hidden-before-change" not in content
    assert "visible-after-change" in content
    assert "hidden-after-reset" not in content


def test_refresh_log_level_preserves_explicit_default_when_ops_config_has_no_override(tmp_path, monkeypatch):
    config_path = tmp_path / "ops_config.json"
    _write_ops_config(config_path, None)
    monkeypatch.setenv("OPS_CONFIG_PATH", str(config_path))

    configure_logging(
        level="WARNING",
        log_dir=str(tmp_path),
        log_file="agent.log",
        console=False,
        max_bytes=0,
        backup_count=2,
    )

    logger = logging_utils.get_logger("tests.dynamic-default")
    assert not logger.isEnabledFor(logging.INFO)

    _rewrite_ops_config(config_path, '{"logging":{"level":"DEBUG"}}')
    assert logger.isEnabledFor(logging.DEBUG)

    _rewrite_ops_config(config_path, "{}")
    assert not logger.isEnabledFor(logging.INFO)


def test_runtime_log_level_keeps_last_good_value_for_invalid_ops_config(tmp_path, monkeypatch):
    config_path = tmp_path / "ops_config.json"
    _write_ops_config(config_path, "DEBUG")
    monkeypatch.setenv("OPS_CONFIG_PATH", str(config_path))

    configure_logging(
        log_dir=str(tmp_path),
        log_file="agent.log",
        console=False,
        max_bytes=0,
        backup_count=2,
    )

    logger = logging_utils.get_logger("tests.dynamic-invalid")
    assert logger.isEnabledFor(logging.DEBUG)

    _rewrite_ops_config(config_path, '{"logging":')
    assert logger.isEnabledFor(logging.DEBUG)

    _rewrite_ops_config(config_path, '{"logging":{"level":"VERBOSE"}}')
    assert logger.isEnabledFor(logging.DEBUG)

    _rewrite_ops_config(config_path, '{"logging":{"level":"INFO"}}')
    assert not logger.isEnabledFor(logging.DEBUG)
