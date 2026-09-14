"""Explicit white-box access helpers for implementation-level tests.

Tests should prefer public behavior. These helpers keep unavoidable internal
probes in the test tree instead of widening production APIs.
"""

from __future__ import annotations

from typing import Any


class InternalAccess:
    def __init__(self, target: object) -> None:
        vars(self)["target"] = target

    def __getattr__(self, name: str) -> Any:
        return getattr(vars(self)["target"], internal_name(name))

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(vars(self)["target"], internal_name(name), value)

    def __delattr__(self, name: str) -> None:
        delattr(vars(self)["target"], internal_name(name))


def internal(target: object) -> InternalAccess:
    return InternalAccess(target)


def internal_name(name: str) -> str:
    if not isinstance(name, str) or not name or name.startswith("_"):
        raise ValueError("expected a public alias for an implementation attribute")
    return "_" + name
