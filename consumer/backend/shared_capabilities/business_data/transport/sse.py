# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Provider-neutral SSE data frame decoding."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any

from ..errors import ProviderProtocolError


def iter_sse_data(lines: Iterable[str | bytes | None]) -> Iterator[Any]:
    data_lines: list[str] = []
    for raw_line in lines:
        if raw_line is None:
            continue
        line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
        line = line.rstrip("\r")
        if line == "":
            payload = _decode(data_lines)
            data_lines = []
            if payload is _DONE:
                return
            if payload is not None:
                yield payload
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator and field == "data":
            data_lines.append(value.lstrip(" "))
    payload = _decode(data_lines)
    if payload is not None and payload is not _DONE:
        yield payload


_DONE = object()


def _decode(data_lines: list[str]) -> Any:
    if not data_lines:
        return None
    text = "\n".join(data_lines).strip()
    if not text:
        return None
    if text == "[DONE]":
        return _DONE
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text
    if not isinstance(payload, (dict, str)):
        raise ProviderProtocolError("invalid SSE payload type")
    return payload
