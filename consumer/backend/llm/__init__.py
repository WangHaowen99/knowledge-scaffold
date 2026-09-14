# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""OpenAI-compatible LLM service package."""

from llm.router import (
    LLMPurpose,
    LLMRouter,
    configure_llm_router,
    get_llm_router,
    reset_llm_router_for_tests,
)
from llm.service import LLMResponse, LLMService

__all__ = [
    "LLMPurpose",
    "LLMResponse",
    "LLMRouter",
    "LLMService",
    "configure_llm_router",
    "get_llm_router",
    "reset_llm_router_for_tests",
]
