# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Sliding window and optional tool pruning for LLM context.

Applied only to the message list passed to the LLM; state and persistence
keep the full message history.
"""

from typing import Any, Dict, List


def apply_sliding_window(
    messages: List[Dict[str, Any]],
    max_rounds: int,
) -> List[Dict[str, Any]]:
    """
    Apply sliding window (keep last N user-rounds).

    - System messages are always kept at the front and not counted as rounds.
    - A "round" starts at a message with role "user" and runs until the next "user".
    """
    if not messages:
        return []
    if max_rounds < 1:
        max_rounds = 1

    # Split leading system block from the rest
    system_block: List[Dict[str, Any]] = []
    for m in messages:
        if m.get("role") == "system":
            system_block.append(m)
        else:
            break
    rest = messages[len(system_block):]
    if not rest:
        return list(messages)

    # Find round boundaries (each round starts at a "user" message)
    user_indices = [i for i, m in enumerate(rest) if m.get("role") == "user"]
    if not user_indices:
        return list(messages)

    # Sliding window: keep only the last max_rounds rounds
    if len(user_indices) <= max_rounds:
        windowed = list(rest)
    else:
        start_idx = user_indices[-max_rounds]
        windowed = list(rest[start_idx:])
    return system_block + windowed
