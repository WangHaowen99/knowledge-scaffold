from api.routes import workflows
from persistence.run_queue import QueuedRunAdmission, RunAdmissionQueue, RunSlotManager


def _envelope(seq: int) -> dict:
    return {
        "protocol": "agent-stream.v2",
        "event_id": "run-1:{}".format(seq),
        "seq": seq,
        "ts": 1700000000000 + seq,
        "session_id": "session-1",
        "run_id": "run-1",
        "turn_id": 1,
        "type": "token.delta",
        "source": {"node": "test", "component": "unit"},
        "payload": {"channel": "content", "text": str(seq)},
    }


def _handle() -> workflows.AgenticRunHandle:
    return workflows.AgenticRunHandle(
        run_id="run-1",
        user_request="hello",
        user_context={},
        arguments=None,
        session_id="session-1",
        user_id="user-1",
        workspace_id="workspace-1",
        orchestrator=object(),
        slot_manager=object(),
        slot_limit_user_id="user-1",
    )


def test_admission_queue_preserves_per_user_fifo_order():
    slots = RunSlotManager()
    admissions = RunAdmissionQueue(slots)
    admitted = []

    assert admissions.try_acquire("user-1", 1) is True
    assert admissions.enqueue(QueuedRunAdmission(
        run_id="run-1",
        user_id="user-1",
        limit=1,
        on_admit=lambda: admitted.append("run-1"),
    )) == 1
    assert admissions.enqueue(QueuedRunAdmission(
        run_id="run-2",
        user_id="user-1",
        limit=1,
        on_admit=lambda: admitted.append("run-2"),
    )) == 2

    admissions.release("user-1")
    assert admitted == ["run-1"]
    assert admissions.position("run-2") == 1

    admissions.release("user-1")
    assert admitted == ["run-1", "run-2"]
    assert admissions.position("run-2") is None


def test_admission_queue_can_cancel_waiting_run():
    slots = RunSlotManager()
    admissions = RunAdmissionQueue(slots)
    assert admissions.try_acquire("user-1", 1) is True
    admissions.enqueue(QueuedRunAdmission(
        run_id="run-waiting",
        user_id="user-1",
        limit=1,
        on_admit=lambda: None,
    ))

    assert admissions.cancel("run-waiting") is True
    assert admissions.position("run-waiting") is None


def test_run_handle_replays_only_events_after_cursor():
    handle = _handle()
    handle._emit(_envelope(1))  # pylint: disable=protected-access
    handle._emit(_envelope(2))  # pylint: disable=protected-access

    sub_id, _sub_queue, replay, finished = handle.subscribe(after_seq=1)
    try:
        assert [event["seq"] for event in replay] == [2]
        assert finished is False
    finally:
        handle.unsubscribe(sub_id)


def test_slow_subscriber_is_forced_to_reconnect_instead_of_growing_queue(monkeypatch):
    monkeypatch.setattr(workflows, "_SSE_SUBSCRIBER_QUEUE_SIZE", 1)
    handle = _handle()
    _sub_id, sub_queue, replay, finished = handle.subscribe()
    assert replay == []
    assert finished is False

    handle._emit(_envelope(1))  # pylint: disable=protected-access
    handle._emit(_envelope(2))  # pylint: disable=protected-access

    assert sub_queue.maxsize == 1
    assert sub_queue.get_nowait() is workflows._RUN_RECONNECT  # pylint: disable=protected-access
    _sub_id, _sub_queue, replay, _finished = handle.subscribe(after_seq=0)
    assert [event["seq"] for event in replay] == [1, 2]
