"""Assembly of B business adapters into the ASGI-owned runtime container."""

from __future__ import annotations

from typing import Any, Callable

from agent.runtime.async_utils import configure_bounded_offload_pools
from agent.runtime.event_bus import AsyncEventBus
from agent.runtime.jiuwen import JiuwenAsyncAgentRuntime
from agent.runtime.manager import AsyncRunManager
from agent.runtime.runtime import RuntimeComponents, RuntimeContainer
from agent.runtime.service import AsyncExecutionService, OrchestratorSessionAdapter
from agent.runtime.store import InMemoryRunRepository


def components_factory(
    orchestrator_provider: Callable[[], Any],
) -> Callable[[RuntimeContainer], RuntimeComponents]:
    """Create one component graph after the container owns all pools."""

    def build(container: RuntimeContainer) -> RuntimeComponents:
        orchestrator = orchestrator_provider()
        configure_bounded_offload_pools(
            short_io=container.short_io,
            external_io=container.external_io,
            tool_io=container.tool_io,
        )
        bind_supervisor = getattr(
            orchestrator,
            "bind_runtime_supervisor",
            None,
        )
        if callable(bind_supervisor):
            bind_supervisor(container.supervisor)
        session_adapter = OrchestratorSessionAdapter(orchestrator_provider)
        runtime = JiuwenAsyncAgentRuntime(
            agent_config=orchestrator.agent_config,
            memory_manager_provider=session_adapter.memory_manager,
            allowed_lifecycle_modes=("async",),
        )
        service = AsyncExecutionService(
            runtime,
            session_adapter=session_adapter,
        )
        repository = InMemoryRunRepository()
        event_hub = AsyncEventBus(
            repository,
            subscriber_queue_size=container.config.subscriber_queue_size,
            max_subscribers_per_run=(
                container.config.max_subscribers_per_run
            ),
            max_subscribers_total=container.config.max_subscribers_total,
            event_persistor=service.persist_public_event,
        )
        manager = AsyncRunManager(
            service,
            repository=repository,
            event_bus=event_hub,
            supervisor=container.supervisor,
        )
        return RuntimeComponents(
            runtime=runtime,
            service=service,
            manager=manager,
            event_hub=event_hub,
        )

    return build
