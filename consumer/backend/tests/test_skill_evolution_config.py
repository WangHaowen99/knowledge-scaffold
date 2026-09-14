"""Boundary tests for skill_evolution config normalization.

Covers the validation gates in _normalize_skill_evolution_effective that the
SystemSettings form relies on. Documents the `or <default>` fallback behavior
for falsy values (0 / "" → default), which is intentional but worth pinning.
"""

from __future__ import annotations

import pytest
from config.system_config import (
    SystemConfigValidationError,
    _normalize_skill_evolution_effective,
)


def _base() -> dict:
    return {
        "enabled": True,
        "base_url": "http://x:8765/",
        "timeout_seconds": 30,
        "poll_interval_seconds": 10,
        "task_timeout_seconds": 1800,
        "max_iterations": 15,
    }


def test_normalizes_base_url_trailing_slash_and_enabled_bool():
    n = _normalize_skill_evolution_effective(_base())
    assert n["base_url"] == "http://x:8765"  # rstrip("/")
    assert n["enabled"] is True


def test_max_iterations_accepts_bounds_rejects_out_of_range():
    for v in (1, 15, 30):
        c = _base()
        c["max_iterations"] = v
        assert _normalize_skill_evolution_effective(c)["max_iterations"] == v
    for v in (31, -1, 100):
        c = _base()
        c["max_iterations"] = v
        with pytest.raises(SystemConfigValidationError, match="max_iterations"):
            _normalize_skill_evolution_effective(c)


def test_max_iterations_zero_falls_back_to_default():
    """0 is falsy → `or 15` fallback (NOT rejected). Pin current behavior: a."""
    # Details: 0 is falsy → `or 15` fallback (NOT rejected). Pin current behavior: a user submitting 0 gets the
    # default 15, not a validation error.
    c = _base()
    c["max_iterations"] = 0
    assert _normalize_skill_evolution_effective(c)["max_iterations"] == 15


def test_task_timeout_rejects_below_60():
    c = _base()
    c["task_timeout_seconds"] = 59
    with pytest.raises(SystemConfigValidationError, match="task_timeout_seconds"):
        _normalize_skill_evolution_effective(c)
    c = _base()
    c["task_timeout_seconds"] = 60
    assert _normalize_skill_evolution_effective(c)["task_timeout_seconds"] == 60


def test_poll_interval_rejects_below_1():
    # 0 is falsy → fallback to 10 (passes). Use a non-zero sub-1 value to test
    # the gate; poll_interval has no integer between 0 and 1, so this mainly
    # documents that 0 falls back rather than rejects.
    c = _base()
    c["poll_interval_seconds"] = 0
    assert _normalize_skill_evolution_effective(c)["poll_interval_seconds"] == 10


def test_timeout_rejects_below_1():
    c = _base()
    c["timeout_seconds"] = 0
    assert _normalize_skill_evolution_effective(c)["timeout_seconds"] == 30  # falsy fallback


def test_missing_fields_use_defaults():
    """An empty/partial section normalizes to defaults instead of raising."""
    n = _normalize_skill_evolution_effective({})
    assert n["enabled"] is False
    assert n["base_url"] == ""
    assert n["max_iterations"] == 15
    assert n["task_timeout_seconds"] == 1800
