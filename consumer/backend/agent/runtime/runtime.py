"""ASGI-lifespan composition root for the B asynchronous runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import Enum
import inspect
import threading
from typing import Any

from agent.runtime.config import NativeAsyncRuntimeConfig
from agent.runtime.lifecycle import (
    JiuwenRunnerLifecycle,
    get_jiuwen_runner_lifecycle,
)
from agent.runtime.offload import BoundedOffloadPool
from agent.runtime.supervisor import RunTaskSupervisor


class RuntimeState(str, Enum):
    """Process runtime states exposed to readiness and diagnostics."""

    CREATED = "CREATED"
    STARTING = "STARTING"
    ACCEPTING = "ACCEPTING"
    DRAINING = "DRAINING"
    STOPPED = "STOPPED"


@dataclass(frozen=True)
class RuntimeComponents:
    """B-side execution objects assembled by the existing bootstrap path."""

    runtime: Any = None
    service: Any = None
    manager: Any = None
    event_hub: Any = None

    @classmethod
    def from_manager(cls, manager: Any) -> "RuntimeComponents":
        service = getattr(manager, "execution_service", None)
        return cls(
            runtime=getattr(service, "runtime", None),
            service=service,
            manager=manager,
            event_hub=getattr(manager, "event_hub", None)
            or getattr(manager, "event_bus", None),
        )


@dataclass(frozen=True)
class _OwnedResource:
    name: str
    close: Callable[..., Any]


ComponentsFactory = Callable[
    ["RuntimeContainer"],
    RuntimeComponents | Awaitable[RuntimeComponents],
]
PoolFactory = Callable[..., Any]


def _required_positional_parameters(function: Callable[..., Any]) -> int:
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return 0
    return sum(
        parameter.default is inspect.Parameter.empty
        and parameter.kind
        in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        }
        for parameter in signature.parameters.values()
    )


def _accepts_deadline(function: Callable[..., Any]) -> bool:
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return False
    return "deadline" in signature.parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


def _process_close_method(resource: Any) -> Callable[..., Any] | None:
    if resource is None:
        return None
    for name in ("shutdown", "aclose", "close"):
        method = getattr(resource, name, None)
        if not callable(method):
            continue
        # B's current EventBus.close(run_id) closes one run lane. It is not a
        # process lifecycle hook and must never receive a shutdown deadline as
        # a run id.
        if _required_positional_parameters(method) == 0:
            return method
    return None


def _consume_background_result(task: asyncio.Future[Any]) -> None:
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        pass


class RuntimeContainer:
    """Own all process resources for one and only one ASGI event loop."""

    def __init__(
        self,
        *,
        config: NativeAsyncRuntimeConfig | None = None,
        runner_lifecycle: JiuwenRunnerLifecycle | None = None,
        components: RuntimeComponents | None = None,
        components_factory: ComponentsFactory | None = None,
        runtime: Any = None,
        service: Any = None,
        manager: Any = None,
        event_hub: Any = None,
        supervisor: RunTaskSupervisor | None = None,
        stop_admission: Callable[..., Any] | None = None,
        http_client: Any = None,
        http_client_factory: Callable[[], Any] | None = None,
        pool_factory: PoolFactory = BoundedOffloadPool,
    ) -> None:
        if components is not None and any(
            value is not None
            for value in (runtime, service, manager, event_hub)
        ):
            raise ValueError(
                "components cannot be combined with individual runtime components"
            )
        if http_client is not None and http_client_factory is not None:
            raise ValueError(
                "http_client and http_client_factory are mutually exclusive"
            )

        selected = components or RuntimeComponents(
            runtime=runtime,
            service=service,
            manager=manager,
            event_hub=event_hub,
        )
        if selected.manager is not None:
            inferred = RuntimeComponents.from_manager(selected.manager)
            selected = RuntimeComponents(
                runtime=selected.runtime or inferred.runtime,
                service=selected.service or inferred.service,
                manager=selected.manager,
                event_hub=selected.event_hub or inferred.event_hub,
            )

        self.config = config or NativeAsyncRuntimeConfig.from_env()
        self.runner_lifecycle = (
            runner_lifecycle or get_jiuwen_runner_lifecycle()
        )
        self.runtime = selected.runtime
        self.service = selected.service
        self.manager = selected.manager
        self.event_hub = selected.event_hub
        self._components_factory = components_factory
        self._components_resolved = components_factory is None

        manager_supervisor = getattr(self.manager, "supervisor", None)
        if (
            supervisor is not None
            and manager_supervisor is not None
            and supervisor is not manager_supervisor
        ):
            raise ValueError("manager and container must share one supervisor")
        self.supervisor = supervisor or manager_supervisor or RunTaskSupervisor()

        self.short_io: Any = None
        self.external_io: Any = None
        self.tool_io: Any = None
        self.http_client = http_client
        self._http_client_factory = http_client_factory
        self._pool_factory = pool_factory
        self._stop_admission = stop_admission

        self._state_guard = threading.RLock()
        self._state = RuntimeState.CREATED
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._transition_lock: asyncio.Lock | None = None
        self._start_count = 0
        self._close_count = 0
        self._close_result: bool | None = None
        self._last_shutdown_deadline: float | None = None
        self._shutdown_failures: list[str] = []
        self._unfinished_close_tasks: set[asyncio.Task[Any]] = set()

    @property
    def state(self) -> RuntimeState:
        with self._state_guard:
            return self._state

    @property
    def is_ready(self) -> bool:
        return self.state is RuntimeState.ACCEPTING

    @property
    def ready(self) -> bool:
        return self.is_ready

    @property
    def owner_loop(self) -> asyncio.AbstractEventLoop | None:
        with self._state_guard:
            return self._owner_loop

    def readiness(self) -> dict[str, Any]:
        state = self.state
        return {
            "ready": state is RuntimeState.ACCEPTING,
            "state": state.value,
        }

    def metrics(self) -> dict[str, Any]:
        supervisor_metrics = {
            "active_runs": len(self.supervisor.active_run_ids),
            "active_derived": self.supervisor.active_derived_count,
        }
        event_hub_metrics = self._component_metrics(self.event_hub)
        return {
            "state": self.state.value,
            "ready": self.is_ready,
            "owner_loop_id": (
                id(self.owner_loop) if self.owner_loop is not None else None
            ),
            "start_count": self._start_count,
            "close_count": self._close_count,
            "close_complete": self._close_result,
            "shutdown_deadline": self._last_shutdown_deadline,
            "shutdown_failures": tuple(self._shutdown_failures),
            "unfinished_close_tasks": sum(
                not task.done() for task in self._unfinished_close_tasks
            ),
            "supervisor": supervisor_metrics,
            "event_hub": event_hub_metrics,
            "short_io": self._component_metrics(self.short_io),
            "external_io": self._component_metrics(self.external_io),
            "tool_io": self._component_metrics(self.tool_io),
        }

    async def start(self) -> bool:
        """Start once on the caller's ASGI event loop."""
        loop = asyncio.get_running_loop()
        transition_lock = self._bind_owner_loop(loop)
        async with transition_lock:
            state = self.state
            if state is RuntimeState.ACCEPTING:
                return False
            if state is RuntimeState.STOPPED:
                raise RuntimeError("runtime container is stopped")
            if state is not RuntimeState.CREATED:
                raise RuntimeError(f"runtime container cannot start from {state.value}")

            self._set_state(RuntimeState.STARTING)
            started: list[_OwnedResource] = []
            try:
                runner_resource = _OwnedResource(
                    "runner",
                    self._stop_runner,
                )
                started.append(runner_resource)
                await self.runner_lifecycle.start(mode="async")

                # The supervisor exists before the manager is assembled. Keep
                # it in the partial-start rollback stack unless the manager is
                # already known to own that exact instance.
                if getattr(self.manager, "supervisor", None) is not self.supervisor:
                    supervisor_close = _process_close_method(self.supervisor)
                    if supervisor_close is not None:
                        started.append(
                            _OwnedResource("supervisor", supervisor_close)
                        )

                self.short_io = self._new_pool(
                    name="short-io",
                    max_workers=self.config.short_io_workers,
                    max_pending=self.config.short_io_max_pending,
                    capacity_wait_seconds=(
                        self.config.short_io_capacity_wait_seconds
                    ),
                    operation_timeout_seconds=(
                        self.config.short_io_operation_timeout_seconds
                    ),
                )
                started.append(self._pool_resource("short_io", self.short_io))
                self.external_io = self._new_pool(
                    name="external-io",
                    max_workers=self.config.external_io_workers,
                    max_pending=self.config.external_io_max_pending,
                    capacity_wait_seconds=(
                        self.config.external_io_capacity_wait_seconds
                    ),
                    operation_timeout_seconds=(
                        self.config.external_io_operation_timeout_seconds
                    ),
                )
                started.append(
                    self._pool_resource("external_io", self.external_io)
                )
                self.tool_io = self._new_pool(
                    name="tool-io",
                    max_workers=self.config.tool_io_workers,
                    max_pending=self.config.tool_io_max_pending,
                    capacity_wait_seconds=(
                        self.config.tool_io_capacity_wait_seconds
                    ),
                    operation_timeout_seconds=(
                        self.config.tool_io_operation_timeout_seconds
                    ),
                )
                started.append(self._pool_resource("tool_io", self.tool_io))

                await self._resolve_components()
                manager_close = _process_close_method(self.manager)
                if manager_close is not None:
                    started.append(_OwnedResource("manager", manager_close))

                event_hub_close = _process_close_method(self.event_hub)
                if event_hub_close is not None:
                    started.append(
                        _OwnedResource("event_hub", event_hub_close)
                    )
                await self._start_optional(self.event_hub)

                if self.http_client is None and self._http_client_factory is not None:
                    self.http_client = await self._await_if_needed(
                        self._http_client_factory()
                    )
                client_close = _process_close_method(self.http_client)
                if client_close is not None:
                    started.append(_OwnedResource("http_client", client_close))

                self._start_count += 1
                self._set_state(RuntimeState.ACCEPTING)
                return True
            except BaseException as start_error:
                deadline = loop.time() + self.config.shutdown_total_deadline_seconds
                self._last_shutdown_deadline = deadline
                self._shutdown_failures = [
                    f"start:{type(start_error).__name__}"
                ]
                cleanup_complete = await self._close_resources(
                    reversed(started),
                    deadline=deadline,
                )
                self._close_result = cleanup_complete
                self.http_client = None
                self._clear_offload_binding()
                self._set_state(RuntimeState.STOPPED)
                raise

    async def close(self, *, deadline: float | None = None) -> bool:
        """Drain all resources under one absolute monotonic deadline."""
        if self.state is RuntimeState.STOPPED:
            return self._close_result is not False

        loop = asyncio.get_running_loop()
        transition_lock = self._bind_owner_loop(loop)
        async with transition_lock:
            if self.state is RuntimeState.STOPPED:
                return self._close_result is not False

            absolute_deadline = (
                loop.time() + self.config.shutdown_total_deadline_seconds
                if deadline is None
                else float(deadline)
            )
            if absolute_deadline < 0:
                raise ValueError("deadline must be an absolute monotonic time")
            self._last_shutdown_deadline = absolute_deadline
            self._close_count += 1
            self._shutdown_failures = []
            self._set_state(RuntimeState.DRAINING)

            resources: list[_OwnedResource] = []
            admission_stop = self._admission_stop_method()
            if admission_stop is not None:
                resources.append(_OwnedResource("admission", admission_stop))

            manager_close = _process_close_method(self.manager)
            if manager_close is not None:
                resources.append(_OwnedResource("manager", manager_close))

            manager_supervisor = getattr(self.manager, "supervisor", None)
            if manager_supervisor is not self.supervisor:
                supervisor_close = _process_close_method(self.supervisor)
                if supervisor_close is not None:
                    resources.append(
                        _OwnedResource("supervisor", supervisor_close)
                    )

            event_hub_close = _process_close_method(self.event_hub)
            if event_hub_close is not None:
                resources.append(_OwnedResource("event_hub", event_hub_close))

            client_close = _process_close_method(self.http_client)
            if client_close is not None:
                resources.append(_OwnedResource("http_client", client_close))

            for name, pool in (
                ("tool_io", self.tool_io),
                ("external_io", self.external_io),
                ("short_io", self.short_io),
            ):
                close = _process_close_method(pool)
                if close is not None:
                    resources.append(_OwnedResource(name, close))

            resources.append(
                _OwnedResource("runner", self._stop_runner)
            )
            self._close_result = await self._close_resources(
                resources,
                deadline=absolute_deadline,
            )
            self.http_client = None
            self._clear_offload_binding()
            self._set_state(RuntimeState.STOPPED)
            return self._close_result

    def _bind_owner_loop(
        self,
        loop: asyncio.AbstractEventLoop,
    ) -> asyncio.Lock:
        with self._state_guard:
            if self._owner_loop is None:
                self._owner_loop = loop
                self._transition_lock = asyncio.Lock()
            elif self._owner_loop is not loop:
                raise RuntimeError(
                    "runtime container is bound to a different event loop"
                )
            transition_lock = self._transition_lock
            if transition_lock is None:
                raise RuntimeError("runtime transition lock is unavailable")
            return transition_lock

    def _set_state(self, state: RuntimeState) -> None:
        with self._state_guard:
            self._state = state

    def _new_pool(self, **kwargs: Any) -> Any:
        return self._pool_factory(**kwargs)

    @staticmethod
    def _pool_resource(name: str, pool: Any) -> _OwnedResource:
        close = _process_close_method(pool)
        if close is None:
            raise TypeError(f"{name} pool must expose close(deadline=...)")
        return _OwnedResource(name, close)

    async def _resolve_components(self) -> None:
        if self._components_resolved:
            return
        factory = self._components_factory
        if factory is None:
            self._components_resolved = True
            return
        components = await self._await_if_needed(factory(self))
        if not isinstance(components, RuntimeComponents):
            raise TypeError("components_factory must return RuntimeComponents")
        if components.manager is not None:
            inferred = RuntimeComponents.from_manager(components.manager)
            components = RuntimeComponents(
                runtime=components.runtime or inferred.runtime,
                service=components.service or inferred.service,
                manager=components.manager,
                event_hub=components.event_hub or inferred.event_hub,
            )
        manager_supervisor = getattr(components.manager, "supervisor", None)
        if manager_supervisor is not None and manager_supervisor is not self.supervisor:
            if self.supervisor.active_run_ids or self.supervisor.active_derived_count:
                raise ValueError("manager and container must share one supervisor")
            self.supervisor = manager_supervisor
        self.runtime = components.runtime
        self.service = components.service
        self.manager = components.manager
        if components.event_hub is not None:
            self.event_hub = components.event_hub
        self._components_resolved = True

    async def _start_optional(self, resource: Any) -> None:
        start = getattr(resource, "start", None)
        if not callable(start) or _required_positional_parameters(start) != 0:
            return
        await self._await_if_needed(start())

    async def _stop_runner(self) -> bool:
        # JiuwenRunnerLifecycle.stop() returns False when it is already
        # stopped. For container shutdown that is still a fully closed state.
        await self.runner_lifecycle.stop()
        return True

    def _admission_stop_method(self) -> Callable[..., Any] | None:
        if self._stop_admission is not None:
            return self._stop_admission
        for name in ("stop_accepting", "begin_draining", "stop_admission"):
            method = getattr(self.manager, name, None)
            if callable(method) and _required_positional_parameters(method) == 0:
                return method
        return None

    async def _close_resources(
        self,
        resources: Any,
        *,
        deadline: float,
    ) -> bool:
        complete = True
        closed_objects: set[tuple[int, int]] = set()
        for resource in resources:
            identity = (
                id(getattr(resource.close, "__self__", resource.close)),
                id(getattr(resource.close, "__func__", resource.close)),
            )
            if identity in closed_objects:
                continue
            closed_objects.add(identity)
            resource_complete = await self._close_one(
                resource,
                deadline=deadline,
            )
            complete = resource_complete and complete
        return complete

    async def _close_one(
        self,
        resource: _OwnedResource,
        *,
        deadline: float,
    ) -> bool:
        loop = asyncio.get_running_loop()
        remaining = max(0.0, deadline - loop.time())
        if remaining <= 0:
            self._shutdown_failures.append(
                f"{resource.name}:deadline_exceeded"
            )
            return False
        try:
            result = (
                resource.close(deadline=deadline)
                if _accepts_deadline(resource.close)
                else resource.close()
            )
            if not inspect.isawaitable(result):
                if result is False:
                    self._shutdown_failures.append(
                        f"{resource.name}:incomplete"
                    )
                    return False
                return True

            task = asyncio.ensure_future(result)
            done, pending = await asyncio.wait({task}, timeout=remaining)
            if pending:
                task.cancel()
                task.add_done_callback(_consume_background_result)
                self._unfinished_close_tasks.add(task)
                task.add_done_callback(self._unfinished_close_tasks.discard)
                self._shutdown_failures.append(
                    f"{resource.name}:deadline_exceeded"
                )
                return False
            if task.cancelled():
                self._shutdown_failures.append(
                    f"{resource.name}:CancelledError"
                )
                return False
            resolved = task.result()
            if resolved is False:
                self._shutdown_failures.append(
                    f"{resource.name}:incomplete"
                )
                return False
            return True
        except Exception as exc:
            self._shutdown_failures.append(
                f"{resource.name}:{type(exc).__name__}"
            )
            return False

    @staticmethod
    async def _await_if_needed(value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    @staticmethod
    def _clear_offload_binding() -> None:
        from agent.runtime.async_utils import clear_bounded_offload_pools

        clear_bounded_offload_pools()

    @staticmethod
    def _component_metrics(component: Any) -> Mapping[str, Any] | None:
        if component is None:
            return None
        metrics = getattr(component, "metrics", None)
        if not callable(metrics):
            return None
        try:
            value = metrics()
        except Exception:
            return {"available": False}
        if inspect.isawaitable(value):
            return {"available": False}
        if isinstance(value, Mapping):
            return dict(value)
        return {"value": value}
