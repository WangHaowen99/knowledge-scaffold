from __future__ import annotations

import json
from typing import Callable, Iterator, Optional

import requests

from .types import NormalizedStreamEvent, SseEvent


def _safe_json_loads(value: str):
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def iter_sse_events(response: requests.Response) -> Iterator[SseEvent]:
    event_name = "message"
    event_id: Optional[str] = None
    retry: Optional[int] = None
    data_lines: list[str] = []

    def emit_current() -> Optional[SseEvent]:
        nonlocal event_name, event_id, retry, data_lines
        if not data_lines and event_name == "message" and event_id is None and retry is None:
            return None

        data = "\n".join(data_lines)
        message = SseEvent(
            event=event_name or "message",
            data=data,
            json_data=_safe_json_loads(data) if data else None,
            id=event_id,
            retry=retry,
        )
        event_name = "message"
        event_id = None
        retry = None
        data_lines = []
        return message

    for raw_line in response.iter_lines(decode_unicode=True):
        if raw_line is None:
            continue

        line = raw_line.rstrip("\r")
        if line == "":
            current = emit_current()
            if current is not None:
                yield current
            continue

        if line.startswith(":"):
            continue

        field, separator, value = line.partition(":")
        if not separator:
            continue
        value = value.lstrip(" ")

        if field == "event":
            event_name = value or "message"
        elif field == "data":
            data_lines.append(value)
        elif field == "id":
            event_id = value or None
        elif field == "retry":
            try:
                retry = int(value)
            except ValueError:
                retry = None

    trailing = emit_current()
    if trailing is not None:
        yield trailing


class EventStream(Iterator[NormalizedStreamEvent]):
    def __init__(
        self,
        response: requests.Response,
        normalizer: Callable[[SseEvent], NormalizedStreamEvent],
    ) -> None:
        self._response = response
        self._normalizer = normalizer
        self._iterator = iter_sse_events(response)
        self._closed = False

    def __iter__(self) -> "EventStream":
        return self

    def __next__(self) -> NormalizedStreamEvent:
        try:
            event = next(self._iterator)
        except StopIteration:
            self.close()
            raise
        return self._normalizer(event)

    def close(self) -> None:
        if self._closed:
            return
        self._response.close()
        self._closed = True

    def __enter__(self) -> "EventStream":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
