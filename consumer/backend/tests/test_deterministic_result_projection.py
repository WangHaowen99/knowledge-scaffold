# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""User-visible deterministic answers must retain structured business values."""

import json

import pytest

from agent.runtime.service import _deterministic_final_output


@pytest.mark.parametrize("summary_key", ["text", "formatted_result", "message", "result"])
@pytest.mark.parametrize("count", [0, 3, None])
def test_summary_does_not_hide_business_fields(summary_key, count):
    structured = {summary_key: "Synthetic query finished", "count": count}
    envelope = {
        "structured_result": structured,
        "execution": {"type": "deterministic", "authoritative": True},
    }

    projected = _deterministic_final_output(envelope)

    assert json.loads(projected["text"]) == structured
    assert projected["structured_result"] == structured
    assert projected["execution"] == envelope["execution"]
    assert "text" not in envelope


@pytest.mark.parametrize("summary_key", ["text", "formatted_result", "message", "result"])
def test_text_only_result_keeps_readable_answer(summary_key):
    projected = _deterministic_final_output(
        {"structured_result": {summary_key: " Synthetic answer "}}
    )
    assert projected["text"] == "Synthetic answer"
