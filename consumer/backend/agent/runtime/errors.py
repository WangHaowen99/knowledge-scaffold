"""Errors exposed by the consumer asynchronous run platform."""


class AsyncRunError(Exception):
    error_code = "INTERNAL_ERROR"


class RunNotFoundError(AsyncRunError):
    error_code = "RUN_NOT_FOUND"


class RunConflictError(AsyncRunError):
    error_code = "RUN_STATE_CONFLICT"


class SessionBusyError(RunConflictError):
    error_code = "SESSION_BUSY"

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__("session already has an active run")


class InteractionPendingError(RunConflictError):
    error_code = "INTERACTION_PENDING"


class InteractionConflictError(RunConflictError):
    error_code = "RUN_NOT_READY"


class DuplicateInteractionError(InteractionConflictError):
    error_code = "HITL_DUPLICATE"


class IdempotencyConflictError(RunConflictError):
    error_code = "IDEMPOTENCY_CONFLICT"


class EventReplayUnavailableError(AsyncRunError):
    error_code = "EVENT_REPLAY_UNAVAILABLE"
