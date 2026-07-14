"""Sliding window and optional tool pruning for LLM context.

Applied only to the message list passed to the LLM; state and persistence
keep the full message history.
"""

import copy
from typing import Any, Dict, List, Optional, Set


def apply_sliding_window(
    messages: List[Dict[str, Any]],
    max_rounds: int,
    prune_tools: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Apply sliding window (keep last N user-rounds) and optionally prune
    specified tool calls/responses in history rounds only.

    - System messages are always kept at the front and not counted as rounds.
    - A "round" starts at a message with role "user" and runs until the next "user".
    - Pruning only affects messages before the last user (history rounds);
      the current round is left intact for in-progress reasoning.
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
    after_window = system_block + windowed

    # Optional tool pruning only in history rounds
    prune_tools_set = prune_tools or set()
    if not prune_tools_set:
        return after_window

    # Current round = from last "user" to end; history = before that last user
    last_user_idx_in_windowed = max(i for i, m in enumerate(windowed) if m.get("role") == "user")
    history_after_system = windowed[:last_user_idx_in_windowed]
    current_round = windowed[last_user_idx_in_windowed:]

    # First pass: collect tool_call_ids to remove (in history, tool name in prune_tools)
    ids_to_remove: Set[str] = set()
    for m in history_after_system:
        if m.get("role") != "tool":
            continue
        name = m.get("name")
        if name in prune_tools_set:
            tid = m.get("tool_call_id")
            if tid:
                ids_to_remove.add(tid)

    # Second pass: rebuild history with pruned tool messages and filtered assistant tool_calls
    result: List[Dict[str, Any]] = list(system_block)
    for m in history_after_system:
        role = m.get("role")
        if role == "tool":
            if m.get("tool_call_id") in ids_to_remove:
                continue
            result.append(m)
            continue
        if role == "assistant" and m.get("tool_calls"):
            tool_calls = m.get("tool_calls", [])
            kept = [tc for tc in tool_calls if tc.get("id") not in ids_to_remove]
            content = m.get("content") or ""
            has_content = bool(content if isinstance(content, str) else content)
            if not kept and not has_content:
                continue
            new_msg = copy.deepcopy(m)
            if kept:
                new_msg["tool_calls"] = kept
            else:
                new_msg.pop("tool_calls", None)
            result.append(new_msg)
            continue
        result.append(m)
    result.extend(current_round)
    return result
