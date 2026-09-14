# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Problem-family registration without concrete-family imports."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


class ProblemFamilyRegistryError(ValueError):
    """Base error for problem-family registration and dispatch."""


class ProblemFamilyNotFound(ProblemFamilyRegistryError):
    pass


class ProblemFamilyAlreadyRegistered(ProblemFamilyRegistryError):
    pass


class ProblemFamilyRegistry:
    """Resolve adapters by stable family key and dispatch generic operations."""

    def __init__(self, adapters: Iterable[Any] = ()) -> None:
        self._adapters: dict[str, Any] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: Any, *, family_key: str | None = None) -> None:
        key = family_key or self._family_key(adapter)
        if key in self._adapters:
            raise ProblemFamilyAlreadyRegistered(
                "problem family is already registered: {}".format(key)
            )
        self._adapters[key] = adapter

    def get(self, family_key: str) -> Any:
        key = str(family_key or "").strip()
        try:
            return self._adapters[key]
        except KeyError as exc:
            raise ProblemFamilyNotFound(
                "problem family is not registered: {}".format(key or "missing")
            ) from exc

    resolve = get

    def families(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def compile_problem(self, family_key: str, **kwargs: Any) -> Any:
        adapter = self.get(family_key)
        compiler = getattr(adapter, "compile_problem", None) or getattr(
            adapter, "compile", None
        )
        if not callable(compiler):
            raise ProblemFamilyRegistryError(
                "problem family does not support compilation: {}".format(family_key)
            )
        return compiler(**kwargs)

    def enumerate(self, family_key: str, **kwargs: Any) -> Any:
        adapter = self.get(family_key)
        enumerator = getattr(adapter, "enumerate", None) or getattr(
            adapter, "generate_candidates", None
        )
        if not callable(enumerator):
            raise ProblemFamilyRegistryError(
                "problem family does not support enumeration: {}".format(family_key)
            )
        return enumerator(**kwargs)

    @staticmethod
    def _family_key(adapter: Any) -> str:
        for attribute in ("family_key", "problem_family", "family"):
            value = getattr(adapter, attribute, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        raise ProblemFamilyRegistryError("problem family adapter has no family key")


ProblemFamilyRegistryProtocol = ProblemFamilyRegistry
