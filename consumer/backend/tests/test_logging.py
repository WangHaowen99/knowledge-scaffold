"""Tests for date-partitioned backend logging."""

from datetime import date, datetime
import gzip
import logging
import logging.handlers
from pathlib import Path
import sys

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import utils.logging as logging_utils

DatePartitionedRotatingFileHandler = logging_utils.DatePartitionedRotatingFileHandler
configure_logging = logging_utils.configure_logging
refresh_log_level = logging_utils.refresh_log_level


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


def _write_log_config(config_path: Path, level: str | None) -> None:
    lines = [
        '"""Test logging config."""',
        "",
    ]
    if level is not None:
        lines.append('LOG_LEVEL = "{}"'.format(level))
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture(autouse=True)
def _restore_root_logging():
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level
    original_configured = logging_utils._LOGGING_CONFIGURED
    original_configured_level = logging_utils._CONFIGURED_LOG_LEVEL
    original_runtime_level = logging_utils._CURRENT_RUNTIME_LOG_LEVEL
    original_config_path = logging_utils._LOG_LEVEL_CONFIG_PATH
    original_config_mtime = logging_utils._LOG_LEVEL_CONFIG_MTIME
    original_config_value = logging_utils._LOG_LEVEL_FROM_CONFIG

    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
    logging_utils._LOGGING_CONFIGURED = False

    try:
        yield
    finally:
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
            handler.close()
        for handler in original_handlers:
            root_logger.addHandler(handler)
        root_logger.setLevel(original_level)
        logging_utils._LOGGING_CONFIGURED = original_configured
        logging_utils._CONFIGURED_LOG_LEVEL = original_configured_level
        logging_utils._CURRENT_RUNTIME_LOG_LEVEL = original_runtime_level
        logging_utils._LOG_LEVEL_CONFIG_PATH = original_config_path
        logging_utils._LOG_LEVEL_CONFIG_MTIME = original_config_mtime
        logging_utils._LOG_LEVEL_FROM_CONFIG = original_config_value


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
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, DatePartitionedRotatingFileHandler)
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


def test_runtime_log_level_reloads_from_config_file(tmp_path):
    config_path = tmp_path / "server.py"
    _write_log_config(config_path, "INFO")

    configure_logging(
        level="INFO",
        log_dir=str(tmp_path),
        log_file="agent.log",
        level_config_path=str(config_path),
        console=False,
        max_bytes=0,
        backup_count=2,
    )

    logger = logging_utils.get_logger("tests.dynamic")
    log_file = tmp_path / "agent-{}.log".format(date.today().isoformat())

    assert not logger.isEnabledFor(logging.DEBUG)
    logger.debug("hidden-before-change")

    _write_log_config(config_path, "DEBUG")
    refresh_log_level(force=True)
    assert logger.isEnabledFor(logging.DEBUG)
    logger.debug("visible-after-change")

    _write_log_config(config_path, "INFO")
    refresh_log_level(force=True)
    assert not logger.isEnabledFor(logging.DEBUG)
    logger.debug("hidden-after-reset")

    content = log_file.read_text(encoding="utf-8")
    assert "hidden-before-change" not in content
    assert "visible-after-change" in content
    assert "hidden-after-reset" not in content


def test_refresh_log_level_preserves_explicit_default_when_config_has_no_override(tmp_path):
    config_path = tmp_path / "server.py"
    _write_log_config(config_path, None)

    configure_logging(
        level="WARNING",
        log_dir=str(tmp_path),
        log_file="agent.log",
        level_config_path=str(config_path),
        console=False,
        max_bytes=0,
        backup_count=2,
    )

    logger = logging_utils.get_logger("tests.dynamic-default")
    assert not logger.isEnabledFor(logging.INFO)

    _write_log_config(config_path, "DEBUG")
    refresh_log_level(force=True)
    assert logger.isEnabledFor(logging.DEBUG)

    _write_log_config(config_path, None)
    refresh_log_level(force=True)
    assert not logger.isEnabledFor(logging.INFO)
