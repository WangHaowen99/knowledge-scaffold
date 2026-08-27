from persistence.run_queue import QueuedRunAdmission, RunAdmissionQueue, RunSlotManager


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
