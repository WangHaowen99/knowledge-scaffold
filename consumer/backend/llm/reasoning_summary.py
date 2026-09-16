# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Generate concise reasoning-intent summaries for UI display."""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from agent.exceptions import WorkflowCancelledError
from integration.observability import (
    LLMRuntimeTrace,
    RuntimeRequestLabel,
    RuntimeStatus,
)

from utils.logging import get_logger

if TYPE_CHECKING:
    from llm import LLMService


logger = get_logger("utils.reasoning_summary")

_SUMMARY_SYSTEM_PROMPT = (
    "你是一个推理摘要助手。"
    "给定模型的推理片段后，用一句不超过20个字的中文总结“模型接下来准备做什么”。"
    "要求："
    "1) 只输出一句话；"
    "2) 不要解释原因，不要复述细节；"
    "3) 不要使用项目符号、引号或前缀。"
)


def summarize_reasoning_intent(
    llm: "LLMService",
    reasoning_text: str,
    *,
    run_id: Optional[str] = None,
    observer=None,
    session_id: Optional[str] = None,
    turn_id: Optional[int] = None,
    parent_key: Optional[str] = None,
    span_key: Optional[str] = None,
) -> str:
    """
    Summarize a reasoning block into one short sentence for UI headers.

    Returns an empty string on errors to avoid blocking main workflow execution.
    """
    text = (reasoning_text or "").strip()
    if not text:
        return ""

    # Keep prompt bounded to reduce latency/cost.
    clipped = text[:2400]
    messages = [
        {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": "推理内容如下：\n{}\n\n请输出一句话摘要。".format(clipped),
        },
    ]

    runtime_span_key = span_key or "reasoning_summary:{}".format(id(text))
    runtime_trace = LLMRuntimeTrace(
        observer=observer,
        run_id=run_id,
        session_id=session_id,
        turn_id=turn_id,
        span_key=runtime_span_key,
        parent_key=parent_key,
        request_label=RuntimeRequestLabel.REASONING_SUMMARY,
    )
    runtime_trace.started(
        model=getattr(llm, "config", None) and llm.config.model or "unknown",
        metadata={"reasoning_chars": len(text)},
    )

    try:
        response = llm.call(
            messages=messages,
            tools=None,
            temperature=0.2,
            stream=False,
            max_tokens=64,
            enable_reasoning=False,
            run_id=run_id,
            request_label=RuntimeRequestLabel.REASONING_SUMMARY,
        )
        if runtime_trace.enabled:
            clipped_prompt = "\n".join(str(m.get("content", "")) for m in messages)
            runtime_trace.finished(
                usage=getattr(response, "usage", None) or {},
                prompt_text=clipped_prompt,
                completion_text=str(response.content or ""),
            )
    except WorkflowCancelledError:
        runtime_trace.failed(status=RuntimeStatus.CANCELLED)
        return ""
    except Exception:
        runtime_trace.failed(status=RuntimeStatus.ERROR)
        logger.warning("Reasoning summary generation failed", exc_info=True)
        return ""

    summary = str(getattr(response, "content", "") or "").strip()
    if not summary:
        return ""

    summary = " ".join(summary.splitlines()).strip()
    if len(summary) > 80:
        summary = summary[:80].rstrip()
    return summary
