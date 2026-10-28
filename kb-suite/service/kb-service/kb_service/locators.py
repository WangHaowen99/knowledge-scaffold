"""Map chunk ranges to source locators (after WeKnora internal/sourceloc)."""

from __future__ import annotations

import bisect
from typing import Sequence

from .parsing import SourceBlock

_GROUP_KEYS = ("type", "page", "slide", "sheet", "section")
MAX_LOCATORS = 8


def _merge(into: dict, loc: dict) -> None:
    bbox_a, bbox_b = into.get("bbox"), loc.get("bbox")
    if isinstance(bbox_a, list) and isinstance(bbox_b, list) and len(bbox_a) == len(bbox_b) == 4:
        into["bbox"] = [
            min(bbox_a[0], bbox_b[0]),
            min(bbox_a[1], bbox_b[1]),
            max(bbox_a[2], bbox_b[2]),
            max(bbox_a[3], bbox_b[3]),
        ]
    for low, high in (("row_start", "row_end"), ("start", "end"), ("start_ms", "end_ms"), ("block", "block_end")):
        if low in loc:
            into[low] = min(into.get(low, loc[low]), loc[low])
        if high in loc:
            into[high] = max(into.get(high, loc[high]), loc[high])


class LocatorIndex:
    def __init__(self, blocks: Sequence[SourceBlock]) -> None:
        self.blocks = sorted(blocks, key=lambda b: b.start)
        self.starts = [b.start for b in self.blocks]

    def for_range(self, start: int, end: int) -> list[dict]:
        if not self.blocks:
            return []
        hi = bisect.bisect_left(self.starts, end)
        merged: dict[tuple, dict] = {}
        order: list[tuple] = []
        for block in self.blocks[:hi]:
            if block.end <= start:
                continue
            loc = {k: v for k, v in block.locator.items() if k != "quote"}
            key = tuple(loc.get(k) for k in _GROUP_KEYS)
            if key in merged:
                _merge(merged[key], loc)
            else:
                merged[key] = dict(loc)
                order.append(key)
        return [merged[k] for k in order[:MAX_LOCATORS]]
