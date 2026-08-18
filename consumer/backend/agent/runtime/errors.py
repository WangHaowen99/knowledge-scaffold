"""Errors exposed by the consumer asynchronous run platform."""


class AsyncRunError(Exception):
    error_code = "INTERNAL_ERROR"


class RunNotFoundError(AsyncRunError):
    error_code = "RUN_NOT_FOUND"


class RunConflictError(AsyncRunError):
    error_code = "RUN_STATE_CONFLICT"


class InteractionConflictError(RunConflictError):
    error_code = "RUN_NOT_READY"


class DuplicateInteractionError(InteractionConflictError):
    error_code = "HITL_DUPLICATE"


class IdempotencyConflictError(RunConflictError):
    error_code = "IDEMPOTENCY_CONFLICT"


class EventReplayUnavailableError(AsyncRunError):
    error_code = "EVENT_REPLAY_UNAVAILABLE"
