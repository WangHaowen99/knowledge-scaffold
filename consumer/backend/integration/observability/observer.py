# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Observer protocol and no-op implementation."""

from __future__ import annotations

from typing import Protocol

from integration.observability.events import RuntimeEvent


class RuntimeObserver(Protocol):
    @property
    def enabled(self) -> bool:
        """Whether runtime code should materialize observability payloads."""

    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def emit(self, event: RuntimeEvent) -> bool:
        """Publish ``event`` without blocking the agent execution path."""


class NoopObserver:
    enabled = False

    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def emit(self, event: RuntimeEvent) -> bool:
        del event
        return False

    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def close(self, timeout: float = 0.0) -> None:
        del timeout


NOOP_OBSERVER = NoopObserver()
