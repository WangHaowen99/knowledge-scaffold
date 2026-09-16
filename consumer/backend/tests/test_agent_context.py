# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from agent.context import apply_sliding_window


def test_sliding_window_preserves_tool_chain_in_retained_rounds():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "first"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "name": "read_file"}],
        },
        {
            "role": "tool",
            "name": "read_file",
            "tool_call_id": "call-1",
            "content": "result",
        },
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "second"},
    ]

    assert apply_sliding_window(messages, max_rounds=2) == messages
