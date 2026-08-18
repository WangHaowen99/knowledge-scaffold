"""Submit consumer async work to the Runner-owning Jiuwen event loop."""

from __future__ import annotations

from concurrent.futures import Future
from typing import Any, Coroutine, Optional

from agent.runtime.lifecycle import get_legacy_jiuwen_loop_runner


class AsyncServiceLoop:
    def submit(self, coroutine: Coroutine[Any, Any, Any]) -> Future[Any]:
        return get_legacy_jiuwen_loop_runner().submit(coroutine)

    def call(self, coroutine: Coroutine[Any, Any, Any], *, timeout: float = 30.0) -> Any:
        return self.submit(coroutine).result(timeout=timeout)

    def stop(self) -> None:
        get_legacy_jiuwen_loop_runner().shutdown()


_ASYNC_SERVICE_LOOP = AsyncServiceLoop()


def get_async_service_loop() -> AsyncServiceLoop:
    return _ASYNC_SERVICE_LOOP
