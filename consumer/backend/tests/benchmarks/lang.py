# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Benchmark language helpers.

This module keeps the benchmark-local ``.env`` lookup contract, but delegates
language detection and translation to production code so model calls go through
the shared lightweight LLM router.
"""

from __future__ import annotations

import os
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _SCRIPT_DIR.parents[1]
_DOTENV_PATH = _SCRIPT_DIR / ".env"
_env_cache: dict[str, str | None] | None = None


def _load_dotenv() -> dict[str, str | None]:
    global _env_cache
    if _env_cache is not None:
        return _env_cache

    env_map: dict[str, str | None] = {}
    if _DOTENV_PATH.exists():
        with _DOTENV_PATH.open("r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                env_map[key.strip()] = value.strip().strip("'\"")

    _env_cache = env_map
    return _env_cache


def _apply_dotenv_to_environment() -> None:
    for key, value in _load_dotenv().items():
        if value is not None:
            os.environ.setdefault(key, value)
    if os.environ.get("LLM_MODEL_NAME") and not os.environ.get("LLM_MODEL"):
        os.environ["LLM_MODEL"] = os.environ["LLM_MODEL_NAME"]


_apply_dotenv_to_environment()

from utils.lang import (  # noqa: E402
    _LANG_DISPLAY_NAMES,
    SUPPORTED_LANGUAGES,
    detect_language,
    get_lang_display_name,
    get_thinking_phrase,
    get_tool_call_phrase,
    translate_text,
)


def get_env(key: str, default: str = "") -> str:
    return os.environ.get(key) or _load_dotenv().get(key) or default


__all__ = [
    "SUPPORTED_LANGUAGES",
    "_LANG_DISPLAY_NAMES",
    "detect_language",
    "get_env",
    "get_lang_display_name",
    "get_thinking_phrase",
    "get_tool_call_phrase",
    "translate_text",
]
