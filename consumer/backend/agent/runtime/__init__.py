"""Shared contracts for the consumer/openJiuwen async runtime boundary."""

from .async_utils import (
    call_async_or_thread,
    find_group_exception,
    run_sync_compat,
    shutdown_sync_runtime_executor,
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
    RuntimeResult,
)
from .events import PublicEvent, PublicEventDraft, RuntimeEvent
from .jiuwen import JiuwenAsyncAgentRuntime
from .jiuwen_events import JiuwenRuntimeEventBridge, RuntimeEventBufferFull
from .lifecycle import (
    JiuwenRunnerLifecycle,
    JiuwenRunnerLifecycleSnapshot,
    JiuwenRuntimeLifecycleError,
    LegacyJiuwenLoopRunner,
    get_jiuwen_runner_lifecycle,
    get_legacy_jiuwen_loop_runner,
    jiuwen_runtime_lifespan,
    shutdown_legacy_jiuwen_runtime,
)
from .manager import AsyncRunManager
from .service import AsyncExecutionService
from .store import InMemoryRunRepository

__all__ = [
    "AsyncAgentRuntime",
    "AsyncExecutionService",
    "AsyncRunManager",
    "call_async_or_thread",
    "InMemoryRunRepository",
    "JiuwenRunnerLifecycle",
    "JiuwenRunnerLifecycleSnapshot",
    "JiuwenRuntimeLifecycleError",
    "JiuwenAsyncAgentRuntime",
    "JiuwenRuntimeEventBridge",
    "LegacyJiuwenLoopRunner",
    "PublicEvent",
    "PublicEventAdapter",
    "PublicEventDraft",
    "PublicEventSink",
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
    "RuntimeResult",
    "get_jiuwen_runner_lifecycle",
    "get_legacy_jiuwen_loop_runner",
    "find_group_exception",
    "jiuwen_runtime_lifespan",
    "run_sync_compat",
    "shutdown_sync_runtime_executor",
    "shutdown_legacy_jiuwen_runtime",
]
