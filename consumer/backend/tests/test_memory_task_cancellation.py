"""Cancellation boundaries for asynchronous memory capture tasks."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from agent.exceptions import WorkflowCancelledError
from agent.tools import memory as memory_tool
from agent.tools.base import ToolContext
from config import AgentConfig
from llm import configure_llm_router, reset_llm_router_for_tests


class _RouterAdapter:
    def __init__(self, llm):
        self.llm = llm
        self.agent_config = AgentConfig.from_env()

    def call(self, *, purpose, messages, profile_id=None, **kwargs):
        del purpose, profile_id
        return self.llm.call(messages, **kwargs)


class _ScriptedLLM:
    def __init__(self):
        self.calls = []
        self.payloads = [
            {"destination": "ordinary_memory", "reason": "test"},
            {
                "content": "用户偏好：默认使用中文回答。",
                "memory_type": "preference",
                "reason": "test",
            },
        ]

    def call(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        payload = self.payloads[min(len(self.calls) - 1, len(self.payloads) - 1)]
        return SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))


class _BlockingLLM(_ScriptedLLM):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def call(self, messages, **kwargs):
        self.started.set()
        self.release.wait(timeout=5)
        return super().call(messages, **kwargs)


class _CancelOnRelationLLM(_ScriptedLLM):
    def __init__(self, cancel_event: threading.Event, source_uri: str):
        super().__init__()
        self.cancel_event = cancel_event
        self.source_uri = source_uri

    def call(self, messages, **kwargs):
        if len(self.calls) < 2:
            return super().call(messages, **kwargs)
        self.calls.append({"messages": messages, "kwargs": kwargs})
        self.cancel_event.set()
        return SimpleNamespace(
            content=json.dumps(
                {
                    "decisions": [
                        {
                            "source_uri": self.source_uri,
                            "relation": "conflicts",
                            "reason": "test replacement",
                        }
                    ]
                },
                ensure_ascii=False,
            )
        )


class _RecordingMemoryManager:
    def __init__(self, *, block_recall: bool = False):
        self.block_recall = block_recall
        self.recall_started = threading.Event()
        self.release_recall = threading.Event()
        self.stored = []
        self.archived = []

    def recall_memory(self, **kwargs):
        del kwargs
        self.recall_started.set()
        if self.block_recall:
            self.release_recall.wait(timeout=5)
        return []

    def store_memory(self, **kwargs):
        self.stored.append(dict(kwargs))
        return SimpleNamespace(
            to_dict=lambda: {
                "stored": True,
                "memory_type": kwargs["memory_type"],
                "scope": kwargs["scope"],
            }
        )

    def archive_memory(self, **kwargs):
        self.archived.append(dict(kwargs))
        return True


class _CandidateMemoryManager(_RecordingMemoryManager):
    source_uri = "memory://existing-preference"

    def recall_memory(self, **kwargs):
        del kwargs
        self.recall_started.set()
        return [
            SimpleNamespace(
                source_uri=self.source_uri,
                content="用户偏好：默认使用英文回答。",
                memory_type="preference",
                scope="user",
                created_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:00:00Z",
            )
        ]


@pytest.fixture(autouse=True)
def _reset_router():
    reset_llm_router_for_tests()
    yield
    reset_llm_router_for_tests()


def _context(
    *,
    run_id: str,
    cancel_event: threading.Event,
    memory_manager=None,
) -> ToolContext:
    return ToolContext(
        memory_manager=memory_manager,
        messages=[],
        run_id=run_id,
        session_id="session-memory",
        turn_id=1,
        agent_config=AgentConfig.from_env(),
        caller="main",
        workspace_id="workspace-memory",
        cancel_event=cancel_event,
    )


def _task_context(run_id: str) -> memory_tool._MemoryTaskContext:
    return memory_tool._MemoryTaskContext(
        memory_manager=None,
        session_id=None,
        run_id=run_id,
        workspace_id=None,
        selected_ontology_id=None,
        read_documents=None,
        cancel_event=threading.Event(),
    )


def test_memory_task_rejects_pre_cancelled_run_without_queueing():
    llm = _ScriptedLLM()
    configure_llm_router(_RouterAdapter(llm))
    cancel_event = threading.Event()
    cancel_event.set()

    with pytest.raises(WorkflowCancelledError):
        memory_tool.handle_memory(
            "记住：默认使用中文回答。",
            context=_context(
                run_id="run-memory-pre-cancel",
                cancel_event=cancel_event,
                memory_manager=_RecordingMemoryManager(),
            ),
        )

    assert llm.calls == []


def test_queued_memory_task_observes_run_cancellation_before_classification(monkeypatch):
    llm = _ScriptedLLM()
    configure_llm_router(_RouterAdapter(llm))
    manager = _RecordingMemoryManager()
    cancel_event = threading.Event()
    blocker_started = threading.Event()
    release_blocker = threading.Event()

    def block_executor():
        blocker_started.set()
        release_blocker.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=1) as executor:
        monkeypatch.setattr(memory_tool, "_MEMORY_EXECUTOR", executor)
        blocker = executor.submit(block_executor)
        assert blocker_started.wait(timeout=1)
        payload = json.loads(
            memory_tool.handle_memory(
                "记住：默认使用中文回答。",
                context=_context(
                    run_id="run-memory-queued-cancel",
                    cancel_event=cancel_event,
                    memory_manager=manager,
                ),
            )
        )
        assert payload["queued"] is True

        cancel_event.set()
        release_blocker.set()
        blocker.result(timeout=2)
        memory_tool.wait_for_memory_tasks(
            timeout=2,
            run_id="run-memory-queued-cancel",
        )

    assert llm.calls == []
    assert manager.stored == []


def test_running_memory_task_stops_after_inflight_llm_returns():
    llm = _BlockingLLM()
    configure_llm_router(_RouterAdapter(llm))
    manager = _RecordingMemoryManager()
    cancel_event = threading.Event()

    payload = json.loads(
        memory_tool.handle_memory(
            "记住：默认使用中文回答。",
            context=_context(
                run_id="run-memory-llm-cancel",
                cancel_event=cancel_event,
                memory_manager=manager,
            ),
        )
    )
    assert payload["queued"] is True
    assert llm.started.wait(timeout=1)

    cancel_event.set()
    llm.release.set()
    memory_tool.wait_for_memory_tasks(
        timeout=2,
        run_id="run-memory-llm-cancel",
    )

    assert len(llm.calls) == 1
    assert manager.stored == []


def test_running_memory_task_rechecks_cancellation_before_persistence():
    llm = _ScriptedLLM()
    configure_llm_router(_RouterAdapter(llm))
    manager = _RecordingMemoryManager(block_recall=True)
    cancel_event = threading.Event()

    payload = json.loads(
        memory_tool.handle_memory(
            "记住：默认使用中文回答。",
            context=_context(
                run_id="run-memory-store-cancel",
                cancel_event=cancel_event,
                memory_manager=manager,
            ),
        )
    )
    assert payload["queued"] is True
    assert manager.recall_started.wait(timeout=1)

    cancel_event.set()
    manager.release_recall.set()
    memory_tool.wait_for_memory_tasks(
        timeout=2,
        run_id="run-memory-store-cancel",
    )

    assert len(llm.calls) == 2
    assert manager.stored == []
    assert manager.archived == []


def test_memory_task_does_not_archive_or_store_after_relation_llm_cancels():
    cancel_event = threading.Event()
    manager = _CandidateMemoryManager()
    llm = _CancelOnRelationLLM(cancel_event, manager.source_uri)
    configure_llm_router(_RouterAdapter(llm))

    payload = json.loads(
        memory_tool.handle_memory(
            "记住：默认使用中文回答。",
            context=_context(
                run_id="run-memory-relation-cancel",
                cancel_event=cancel_event,
                memory_manager=manager,
            ),
        )
    )
    assert payload["queued"] is True
    memory_tool.wait_for_memory_tasks(
        timeout=2,
        run_id="run-memory-relation-cancel",
    )

    assert len(llm.calls) == 3
    assert cancel_event.is_set()
    assert manager.archived == []
    assert manager.stored == []


def test_memory_task_preserves_normal_async_storage_path():
    llm = _ScriptedLLM()
    configure_llm_router(_RouterAdapter(llm))
    manager = _RecordingMemoryManager()

    payload = json.loads(
        memory_tool.handle_memory(
            "记住：默认使用中文回答。",
            context=_context(
                run_id="run-memory-normal",
                cancel_event=threading.Event(),
                memory_manager=manager,
            ),
        )
    )
    assert payload["queued"] is True
    memory_tool.wait_for_memory_tasks(timeout=2, run_id="run-memory-normal")

    assert len(llm.calls) == 2
    assert len(manager.stored) == 1


def test_memory_task_wait_can_be_scoped_to_one_run(monkeypatch):
    run_b_started = threading.Event()
    release_run_b = threading.Event()
    run_a_finished = threading.Event()

    def process(_content, context):
        if context.run_id == "run-memory-b":
            run_b_started.set()
            release_run_b.wait(timeout=5)
        else:
            run_a_finished.set()
        return {"stored": True}

    with ThreadPoolExecutor(max_workers=2) as executor:
        monkeypatch.setattr(memory_tool, "_MEMORY_EXECUTOR", executor)
        monkeypatch.setattr(memory_tool, "_process_memory_task", process)
        memory_tool._submit_memory_task("b", _task_context("run-memory-b"))
        assert run_b_started.wait(timeout=1)
        memory_tool._submit_memory_task("a", _task_context("run-memory-a"))

        memory_tool.wait_for_memory_tasks(timeout=1, run_id="run-memory-a")
        assert run_a_finished.is_set()
        assert not release_run_b.is_set()

        release_run_b.set()
        memory_tool.wait_for_memory_tasks(timeout=2, run_id="run-memory-b")
