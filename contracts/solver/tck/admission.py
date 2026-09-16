# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Strict TCK admission helpers and an out-of-band HTTP fault driver.

The public TCK data plane is deliberately limited to ``solver.job.v1``
HTTP/JSON endpoints.  ``HttpFaultScenarioDriver`` talks to a separate,
administrator-controlled test endpoint only to arrange a fault; it never
reads job state or result data from that endpoint.
"""

from __future__ import annotations

from copy import deepcopy
import json
from typing import Any, Mapping
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from contracts.solver.validation import ContractValidationError, parse_json_bytes

from .runner import FaultScenario, TckFailure, TckReport


_FAULTS = frozenset(
    {
        "fault_429",
        "fault_502",
        "fault_503",
        "fault_timeout",
        "fault_execution_failed",
        "fault_attempt_lost",
        "fault_restart",
        "fault_late_result",
    }
)
_STATUSES = frozenset({"failed", "attempt_lost", "running"})


class StrictAdmissionError(RuntimeError):
    """Raised when a TCK report is incomplete for release admission."""


def assert_admission_report(report: TckReport) -> None:
    """Fail closed unless every requested TCK capability was exercised."""

    if not isinstance(report, TckReport):
        raise StrictAdmissionError("TCK report is invalid")
    if report.passed is not True:
        raise StrictAdmissionError("TCK core checks did not pass")
    if report.skipped:
        raise StrictAdmissionError("TCK admission has skipped checks")
    if report.capability_gaps:
        raise StrictAdmissionError("TCK admission has capability gaps")


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class HttpFaultScenarioDriver:
    """Drive a test-only Connector control plane over HTTP/JSON.

    The control URL must be a separate origin/path from the public Connector
    endpoint.  Commands are a tiny allowlisted protocol and responses are
    parsed strictly, so an accidental HTML/admin response cannot be mistaken
    for a successful injection.
    """

    def __init__(
        self,
        *,
        control_url: str,
        control_token: str,
        timeout_seconds: float = 3.0,
    ) -> None:
        parsed = urlsplit(control_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("control_url must be an HTTP endpoint")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("control_url must be an HTTP endpoint")
        if parsed.query or parsed.fragment:
            raise ValueError("control_url must be an HTTP endpoint")
        if not isinstance(control_token, str) or len(control_token) < 16:
            raise ValueError("control_token must contain at least 16 characters")
        if isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.control_url = control_url.rstrip("/")
        self.control_token = control_token
        self.timeout_seconds = float(timeout_seconds)
        self._opener = build_opener(_RejectRedirects())

    def scenario(
        self, name: str, request: Mapping[str, Any]
    ) -> FaultScenario | None:
        if name not in _FAULTS:
            return None
        # Keep a copy only to ensure callbacks never mutate the runner's
        # request; the control endpoint receives only the scenario label.
        _ = deepcopy(request)

        def before_submit(_payload: Mapping[str, Any]) -> None:
            self._command({"action": "set_fault", "scenario": name})

        def after_submit(job: Mapping[str, Any]) -> None:
            job_id = job.get("job_id")
            if not isinstance(job_id, str) or not job_id:
                raise TckFailure("fault control received an invalid job identity")
            if name == "fault_attempt_lost":
                self._command(
                    {"action": "transition", "job_id": job_id, "status": "attempt_lost"}
                )
            elif name == "fault_restart":
                self._command({"action": "restart"})

        def after_failure() -> None:
            self._command({"action": "transition_latest", "status": "failed"})

        def after_cancel(job: Mapping[str, Any]) -> None:
            if name == "fault_late_result":
                job_id = job.get("job_id")
                if not isinstance(job_id, str) or not job_id:
                    raise TckFailure("fault control received an invalid cancelled job")
                self._command({"action": "late_result", "job_id": job_id})

        def cleanup() -> None:
            self._command({"action": "clear"})

        return FaultScenario(
            before_submit=before_submit,
            after_submit=after_submit,
            after_failure=after_failure,
            after_cancel=after_cancel,
            cleanup=cleanup,
        )

    def _command(self, command: Mapping[str, Any]) -> Mapping[str, Any]:
        data = json.dumps(
            dict(command), ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        request = Request(
            self.control_url,
            data=data,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.control_token,
            },
        )
        try:
            response = self._opener.open(request, timeout=self.timeout_seconds)
        except OSError as exc:
            raise TckFailure("fault control endpoint is unavailable") from exc
        try:
            if response.headers.get_content_type() != "application/json":
                raise TckFailure("fault control response is not application/json")
            raw = response.read(64 * 1024 + 1)
            if len(raw) > 64 * 1024:
                raise TckFailure("fault control response is too large")
            status = int(response.status)
        except (OSError, ValueError) as exc:
            raise TckFailure("fault control response could not be read") from exc
        finally:
            response.close()
        if status != 200:
            raise TckFailure("fault control command was rejected")
        try:
            document = parse_json_bytes(raw)
        except ContractValidationError as exc:
            raise TckFailure("fault control response is not strict JSON") from exc
        if not isinstance(document, Mapping) or document.get("status") != "ok":
            raise TckFailure("fault control response is invalid")
        return document


__all__ = [
    "HttpFaultScenarioDriver",
    "StrictAdmissionError",
    "assert_admission_report",
]
