"""Shared message projection helpers."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ontology.ontologies import SELECTED_ONTOLOGY_CONTEXT_START

_UTC8 = timezone(timedelta(hours=8))
CURRENT_DATE_REMINDER_START = (
    "<system-reminder>\nAs you answer the user's questions, you can use the following context:"
)
CURRENT_DATE_REMINDER_END = "</system-reminder>"


def current_utc8_date_text() -> str:
    return datetime.now(_UTC8).strftime("%Y/%m/%d")


def build_current_date_reminder() -> str:
    return (
        "{}\n# currentDate\nToday's date is {}.\n\n"
        "      IMPORTANT: this context may or may not be relevant to your tasks. "
        "You should not respond to this context unless it is highly relevant to your task.\n"
        "{}"
    ).format(
        CURRENT_DATE_REMINDER_START,
        current_utc8_date_text(),
        CURRENT_DATE_REMINDER_END,
    )


def append_current_date_reminder_to_latest_user_message(
    messages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    appended = [dict(message) for message in messages]
    for index in range(len(appended) - 1, -1, -1):
        message = appended[index]
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            return appended
        stripped_content = strip_current_date_reminder(content).rstrip()
        appended[index] = {
            **message,
            "content": "{}\n\n{}".format(stripped_content, build_current_date_reminder()),
        }
        return appended
    return appended


def append_current_date_reminder_to_text(text: str) -> str:
    messages = append_current_date_reminder_to_latest_user_message([{"role": "user", "content": str(text or "")}])
    if not messages:
        return str(text or "")
    content = messages[-1].get("content")
    return content if isinstance(content, str) else str(text or "")


def strip_current_date_reminder(content: str) -> str:
    start = content.find(CURRENT_DATE_REMINDER_START)
    if start < 0:
        return content
    end = content.find(CURRENT_DATE_REMINDER_END, start)
    if end < 0:
        return content
    return "{}{}".format(
        content[:start],
        content[end + len(CURRENT_DATE_REMINDER_END) :],
    )


def inject_selected_ontology_context_into_user_message(
    messages: List[Dict[str, Any]],
    selected_ontology_context: Optional[str],
) -> List[Dict[str, Any]]:
    filtered = [
        strip_selected_ontology_context_from_user_message(dict(message))
        for message in messages
        if not (message.get("role") == "system" and is_selected_ontology_context(str(message.get("content") or "")))
    ]
    if not selected_ontology_context:
        return filtered

    injected = [dict(message) for message in filtered]
    for index in range(len(injected) - 1, -1, -1):
        message = injected[index]
        if message.get("role") != "user":
            continue
        injected[index] = {
            **message,
            "content": append_selected_ontology_context_to_user_content(
                message.get("content"),
                selected_ontology_context,
            ),
        }
        return injected

    return [
        *injected,
        {"role": "user", "content": selected_ontology_context},
    ]


def append_selected_ontology_context_to_user_content(
    content: Any,
    selected_ontology_context: str,
) -> Any:
    if isinstance(content, list):
        return [
            *deepcopy(content),
            {
                "type": "text",
                "text": "\n\n{}".format(selected_ontology_context),
            },
        ]
    return "{}\n\n{}".format(str(content or ""), selected_ontology_context).strip()


def strip_selected_ontology_context_from_user_message(message: Dict[str, Any]) -> Dict[str, Any]:
    if message.get("role") != "user":
        return message
    content = message.get("content")
    if not isinstance(content, str):
        return message
    marker_index = content.find(SELECTED_ONTOLOGY_CONTEXT_START)
    if marker_index < 0:
        marker_index = content.find("## Selected Ontology Context (MUST FOLLOW)")
    if marker_index < 0:
        marker_index = content.find("## Ontology Selection Override (MUST FOLLOW)")
    if marker_index < 0:
        return message
    return {**message, "content": content[:marker_index].strip()}


def is_selected_ontology_context(content: str) -> bool:
    stripped = content.strip()
    return (
        stripped.startswith(SELECTED_ONTOLOGY_CONTEXT_START)
        or stripped.startswith("## Selected Ontology Context (MUST FOLLOW)")
        or stripped.startswith("## Ontology Selection Override (MUST FOLLOW)")
    )
