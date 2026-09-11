"""Flask adapter for the standalone ``ortools.solver.v1`` service."""

from __future__ import annotations

import hmac
import re
import uuid
from typing import Any, Mapping

from flask import Flask, Response, jsonify, request
from flask.sessions import NullSession, SessionInterface

from contracts.solver_services.ortools.validation import (
    ContractValidationError,
    canonical_json_bytes,
    parse_json_bytes,
    validate_document,
    validate_traceparent,
)

from .config import OrToolsSolverConfig
from .service import (
    CapacityUnavailable,
    IdempotencyConflict,
    LimitExceeded,
    OrToolsSolverService,
)
from .store import AttemptRecord

_IDEMPOTENCY_RE = re.compile(r"^[\x21-\x7e]{16,256}$")
_PREFER_RE = re.compile(r"^wait=([0-9]{1,8})$")


class BearerOnlySessionInterface(SessionInterface):
    """Disable cookie-backed sessions for the bearer-only solver API."""

    def open_session(self, app: Flask, request_obj: Any) -> NullSession:
        return NullSession()

    def save_session(
        self,
        app: Flask,
        session: Any,
        response: Response,
    ) -> None:
        return None


class BearerOnlyJsonApp(Flask):
    """Flask application whose API authority comes only from a bearer header."""

    session_interface = BearerOnlySessionInterface()


def _error(*, status: int, code: str, category: str, message: str, retryable: bool):
    payload = {
        "error": {
            "code": code,
            "category": category,
            "message": message,
            "retryable": retryable,
            "diagnostic_ref": "diag_" + uuid.uuid4().hex,
        }
    }
    return jsonify(payload), status


def _job(record: AttemptRecord) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "api_version": "ortools.solver.v1",
        "job_id": record.job_id,
        "request_id": record.request_id,
        "problem_type": record.problem_type,
        "problem_version": record.problem_version,
        "status": record.status,
    }
    if record.status in {"failed", "attempt_lost"}:
        payload["failure"] = {
            "code": record.error_code or "SOLVER_EXECUTION_FAILED",
            "category": "execution",
            "retryable": False,
            "diagnostic_ref": "diag_" + record.job_id.removeprefix("job_"),
        }
    return payload


def _authorized(config: OrToolsSolverConfig) -> bool:
    expected = config.service_token
    if expected is None:
        return True
    supplied = request.headers.get("Authorization", "")
    return supplied.startswith("Bearer ") and hmac.compare_digest(supplied[7:], expected)


def _wait_preference(config: OrToolsSolverConfig) -> int:
    value = request.headers.get("Prefer")
    if value is None:
        return 0
    match = _PREFER_RE.fullmatch(value)
    if match is None:
        raise ContractValidationError("Prefer header is invalid")
    return min(int(match.group(1)), config.prefer_wait_max_ms)


def _strict_submit(config: OrToolsSolverConfig) -> Mapping[str, Any]:
    if request.mimetype != "application/json":
        raise ContractValidationError("request must use application/json")
    raw = request.get_data(cache=False, as_text=False)
    if len(raw) > config.max_payload_bytes:
        raise LimitExceeded("request payload exceeds solver hard limit")
    document = parse_json_bytes(raw)
    if not isinstance(document, Mapping):
        raise ContractValidationError("submit request must be an object")
    canonical_json_bytes(document)
    validate_document("submit-request", document)
    return document


def create_app(config: OrToolsSolverConfig | None = None) -> Flask:
    config = config or OrToolsSolverConfig.from_env()
    service = OrToolsSolverService(config)
    app = BearerOnlyJsonApp(__name__)
    app.config["MAX_CONTENT_LENGTH"] = config.max_payload_bytes
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
    app.config["SESSION_COOKIE_SECURE"] = True
    app.extensions["ortools_solver"] = service

    @app.before_request
    def authenticate() -> tuple[Response, int] | None:
        if not request.path.startswith("/api/v1/"):
            return None
        if not _authorized(config):
            return _error(
                status=401,
                code="AUTHENTICATION_FAILED",
                category="authentication",
                message="Solver service authentication failed",
                retryable=False,
            )
        return None

    @app.get("/health")
    def health() -> Response:
        return jsonify({"status": "ok"})

    @app.get("/api/v1/capabilities")
    def capabilities() -> Response:
        payload = service.capabilities()
        validate_document("capabilities", payload)
        return jsonify(payload)

    @app.post("/api/v1/jobs")
    def submit() -> tuple[Response, int]:
        idempotency_key = request.headers.get("Idempotency-Key", "")
        if _IDEMPOTENCY_RE.fullmatch(idempotency_key) is None:
            return _error(
                status=400,
                code="IDEMPOTENCY_KEY_REQUIRED",
                category="validation",
                message="A valid Idempotency-Key header is required",
                retryable=False,
            )
        try:
            traceparent = request.headers.get("traceparent")
            if traceparent is not None:
                validate_traceparent(traceparent)
            wait_ms = _wait_preference(config)
            document = _strict_submit(config)
            record, _ = service.submit(idempotency_key=idempotency_key, document=document)
        except (ContractValidationError, UnicodeError):
            return _error(
                status=400,
                code="CONTRACT_INVALID",
                category="validation",
                message="Submit request does not satisfy ortools.solver.v1",
                retryable=False,
            )
        except LimitExceeded:
            return _error(
                status=400,
                code="LIMIT_EXCEEDED",
                category="capacity",
                message="Submit request exceeds solver limits",
                retryable=False,
            )
        except IdempotencyConflict:
            return _error(
                status=409,
                code="IDEMPOTENCY_CONFLICT",
                category="validation",
                message="Idempotency key was already used for another request",
                retryable=False,
            )
        except CapacityUnavailable:
            response, status = _error(
                status=429,
                code="CAPACITY_UNAVAILABLE",
                category="capacity",
                message="Solver concurrency limit is reached",
                retryable=True,
            )
            response.headers["Retry-After"] = "1"
            return response, status
        if wait_ms:
            import time

            deadline = time.monotonic() + wait_ms / 1000.0
            while (
                record.status not in {"completed", "failed", "cancelled", "attempt_lost"}
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
                refreshed = service.get(record.job_id)
                if refreshed is None:
                    break
                record = refreshed
        payload = _job(record)
        validate_document("job", payload)
        return jsonify(payload), 202

    @app.get("/api/v1/jobs/<job_id>")
    def get_job(job_id: str):
        record = service.get(job_id)
        if record is None:
            return _error(
                status=404,
                code="JOB_NOT_FOUND",
                category="result",
                message="Job does not exist or has expired",
                retryable=False,
            )
        payload = _job(record)
        validate_document("job", payload)
        return jsonify(payload)

    @app.get("/api/v1/jobs/<job_id>/result")
    def get_result(job_id: str):
        record = service.get(job_id)
        if record is None:
            return _error(
                status=404,
                code="JOB_NOT_FOUND",
                category="result",
                message="Job does not exist or has expired",
                retryable=False,
            )
        if record.status != "completed" or record.result is None:
            return _error(
                status=409,
                code="RESULT_NOT_AVAILABLE",
                category="result",
                message="Solve result is not available",
                retryable=record.status in {"accepted", "running"},
            )
        validate_document("result", record.result)
        return jsonify(record.result)

    @app.delete("/api/v1/jobs/<job_id>")
    def cancel_job(job_id: str):
        record = service.cancel(job_id)
        if record is None:
            return _error(
                status=404,
                code="JOB_NOT_FOUND",
                category="result",
                message="Job does not exist or has expired",
                retryable=False,
            )
        payload = _job(record)
        validate_document("job", payload)
        return jsonify(payload)

    @app.errorhandler(413)
    def payload_too_large(_: BaseException):
        return _error(
            status=413,
            code="PAYLOAD_TOO_LARGE",
            category="capacity",
            message="Request payload exceeds solver limits",
            retryable=False,
        )

    return app


def main() -> None:
    create_app().run(host="0.0.0.0", port=8080, threaded=True)


if __name__ == "__main__":
    main()


__all__ = ["create_app"]
