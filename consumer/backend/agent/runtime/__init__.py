"""Shared contracts for the consumer/openJiuwen async runtime boundary."""

from .async_utils import (
    call_async_or_thread,
    find_group_exception,
    run_sync_compat,
)
from .adapter import PublicEventAdapter
from .contracts import (
    AsyncAgentRuntime,
    PublicEventSink,
    RunScope,
    RunSnapshot,
    RunStatus,
    RunView,
    RuntimeErrorInfo,
    RuntimeEventSink,
    RuntimeHistoryMessage,
    RuntimeInvocationContext,
    RuntimeRecoveryAction,
    RuntimeResult,
)
from .events import PublicEvent, PublicEventDraft, RuntimeEvent
from .factory import components_factory
from .jiuwen import JiuwenAsyncAgentRuntime
from .jiuwen_events import JiuwenRuntimeEventBridge, RuntimeEventBufferFull
from .lifecycle import (
    JiuwenRunnerLifecycle,
    JiuwenRunnerLifecycleSnapshot,
    JiuwenRuntimeLifecycleError,
    get_jiuwen_runner_lifecycle,
    jiuwen_runtime_lifespan,
)
from .manager import AsyncRunManager
from .offload import (
    BoundedOffloadPool,
    OffloadPoolClosed,
    OffloadPoolSaturated,
)
from .service import AsyncExecutionService
from .store import InMemoryRunRepository
from .supervisor import RunTaskSupervisor
from .config import NativeAsyncRuntimeConfig
from .runtime import RuntimeComponents, RuntimeContainer, RuntimeState

__all__ = [
    "AsyncAgentRuntime",
    "AsyncExecutionService",
    "AsyncRunManager",
    "BoundedOffloadPool",
    "call_async_or_thread",
    "components_factory",
    "InMemoryRunRepository",
    "JiuwenRunnerLifecycle",
    "JiuwenRunnerLifecycleSnapshot",
    "JiuwenRuntimeLifecycleError",
    "JiuwenAsyncAgentRuntime",
    "JiuwenRuntimeEventBridge",
    "PublicEvent",
    "PublicEventAdapter",
    "PublicEventDraft",
    "PublicEventSink",
    "OffloadPoolClosed",
    "OffloadPoolSaturated",
    "RunScope",
    "RunSnapshot",
    "RunStatus",
    "RunView",
    "RuntimeErrorInfo",
    "RuntimeEventBufferFull",
    "RuntimeEvent",
    "RuntimeEventSink",
    "RuntimeHistoryMessage",
    "RuntimeInvocationContext",
    "RuntimeRecoveryAction",
    "RuntimeResult",
    "RunTaskSupervisor",
    "NativeAsyncRuntimeConfig",
    "RuntimeComponents",
    "RuntimeContainer",
    "RuntimeState",
    "get_jiuwen_runner_lifecycle",
    "find_group_exception",
    "jiuwen_runtime_lifespan",
    "run_sync_compat",
]
