"""Endpoint-only conformance runner for the public solver Connector contract."""

from __future__ import annotations

import json
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from contracts.solver.validation import (
    ContractValidationError,
    canonical_json_bytes,
    parse_json_bytes,
    validate_document,
)

_CONTRACT_ROOT = Path(__file__).resolve().parents[1]
_MAX_RESPONSE_BYTES = 1024 * 1024
_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "attempt_lost"}
_EXPECTED_CHECKS = (
    "health",
    "capabilities",
    "missing_idempotency_key",
    "unknown_field",
    "unsupported_family_version",
    "idempotent_submit",
    "idempotency_conflict",
    "job_state_machine",
    "result_contract",
    "binary_ordered_route_golden",
    "idempotent_cancel",
    "sensitive_echo",
)
_FAULT_CHECKS = (
    "fault_429",
    "fault_502",
    "fault_503",
    "fault_timeout",
    "fault_execution_failed",
    "fault_attempt_lost",
    "fault_restart",
    "fault_late_result",
    "verifier_acceptance",
)


class TckFailure(RuntimeError):
    """Raised when a Connector violates a public wire-contract requirement."""


@dataclass(frozen=True)
class FaultScenario:
    """Optional out-of-band controls for a fault probe.

    The control callbacks are deliberately outside ``solver.job.v1``.  A
    provider may implement them with a test-only admin API, process restart,
    network proxy, or an in-process harness.  The TCK itself still talks to
    the Connector only through the six public HTTP/JSON endpoints.
    """

    before_submit: Callable[[Mapping[str, Any]], None] | None = None
    after_submit: Callable[[Mapping[str, Any]], None] | None = None
    after_failure: Callable[[], None] | None = None
    after_cancel: Callable[[Mapping[str, Any]], None] | None = None
    cleanup: Callable[[], None] | None = None


class FaultScenarioDriver(Protocol):
    """Provide optional, out-of-band fault scenarios for Connector admission."""

    def scenario(self, name: str, request: Mapping[str, Any]) -> FaultScenario | None:
        return ...


ResultVerifier = Callable[..., bool | None]


@dataclass(frozen=True)
class TckReport:
    passed: bool
    checks: tuple[str, ...]
    skipped: tuple[str, ...] = ()
    capability_gaps: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Response:
    status: int
    document: Any
    raw: bytes
    headers: Mapping[str, str]


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class ConnectorTckRunner:
    """Run black-box checks against one HTTP endpoint without platform imports."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: int | float = 5,
        bearer_token: str | None = None,
        fault_driver: FaultScenarioDriver | None = None,
        result_verifier: ResultVerifier | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an HTTP endpoint origin")
        if parsed.username is not None or parsed.password is not None or parsed.query:
            raise ValueError("base_url must be an HTTP endpoint origin")
        if parsed.fragment:
            raise ValueError("base_url must be an HTTP endpoint origin")
        if isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if bearer_token is not None and (not isinstance(bearer_token, str) or len(bearer_token) < 16):
            raise ValueError("bearer_token must contain at least 16 characters")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self._bearer_token = bearer_token
        self._fault_driver = fault_driver
        self._result_verifier = result_verifier
        self._opener = build_opener(_RejectRedirects())

    def run(self) -> TckReport:
        request_ref = "req_" + "A" * 28
        request = self._example("submit-request.json")
        request["request_ref"] = request_ref

        self._check_health()
        limits = self._check_capabilities()
        request["requirements"]["limits"]["workers"] = min(
            request["requirements"]["limits"]["workers"], limits["max_workers"]
        )
        request["requirements"]["limits"]["solve_time_ms"] = min(
            request["requirements"]["limits"]["solve_time_ms"],
            limits["max_solve_time_ms"],
        )
        self._check_rejected_submit(
            request,
            expected_status=400,
            expected_code="IDEMPOTENCY_KEY_REQUIRED",
        )

        strict = deepcopy(request)
        strict["unexpected"] = "synthetic"
        self._check_rejected_submit(
            strict,
            idempotency_key="tck-unknown-field-0001",
            expected_status=400,
            expected_code="CONTRACT_INVALID",
        )

        unsupported = deepcopy(request)
        unsupported["problem"]["family_version"] = "999.0"
        self._check_rejected_submit(
            unsupported,
            idempotency_key="tck-unsupported-family-0001",
            expected_status=400,
            expected_code="CONTRACT_INVALID",
        )

        idempotency_key = "tck-idempotent-submit-0001"
        first = self._request(
            "POST",
            "/v1/solve-jobs",
            document=request,
            idempotency_key=idempotency_key,
        )
        second = self._request(
            "POST",
            "/v1/solve-jobs",
            document=request,
            idempotency_key=idempotency_key,
        )
        first_job = self._job_response(first, expected_status=202)
        second_job = self._job_response(second, expected_status=202)
        if first_job["job_id"] != second_job["job_id"]:
            raise TckFailure("idempotent submit returned a different job")

        changed = deepcopy(request)
        changed["requirements"]["result_quality"] = "any"
        self._check_rejected_submit(
            changed,
            idempotency_key=idempotency_key,
            expected_status=409,
            expected_code="IDEMPOTENCY_CONFLICT",
        )

        terminal = self._poll_terminal(first_job)
        if terminal["status"] != "completed":
            raise TckFailure("golden solve did not complete")
        result = self._check_result(job_id=terminal["job_id"], request_ref=request_ref, request=request)
        self._check_golden(request=request, result=result)

        first_cancel = self._request("DELETE", "/v1/solve-jobs/{}".format(terminal["job_id"]))
        second_cancel = self._request("DELETE", "/v1/solve-jobs/{}".format(terminal["job_id"]))
        cancelled_a = self._job_response(first_cancel, expected_status=200)
        cancelled_b = self._job_response(second_cancel, expected_status=200)
        if cancelled_a != cancelled_b:
            raise TckFailure("idempotent cancel returned different state")

        sensitive = deepcopy(request)
        sensitive["workspace_id"] = "workspace_synthetic_tck_marker"
        rejected = self._request(
            "POST",
            "/v1/solve-jobs",
            document=sensitive,
            idempotency_key="tck-sensitive-echo-0001",
        )
        self._error_response(rejected, expected_status=400)
        forbidden = (b"workspace_synthetic_tck_marker",)
        if any(value in rejected.raw for value in forbidden):
            raise TckFailure("Connector response echoed sensitive request data")

        checks = list(_EXPECTED_CHECKS)
        skipped: list[str] = []
        capability_gaps: list[str] = []
        if self._fault_driver is None:
            skipped.extend(_FAULT_CHECKS[:-1])
            capability_gaps.append("fault_scenario_driver_missing")
        else:
            for name in _FAULT_CHECKS[:-1]:
                scenario = self._scenario(name, request)
                if scenario is None:
                    skipped.append(name)
                    capability_gaps.append(name + "_scenario_unsupported")
                    continue
                self._run_fault_check(name, request, scenario, checks)

        if self._result_verifier is None:
            skipped.append("verifier_acceptance")
            capability_gaps.append("public_verifier_missing")
        else:
            accepted = self._result_verifier(request=request, result=result)
            if accepted is not True:
                raise TckFailure("public verifier rejected the golden result")
            checks.append("verifier_acceptance")

        return TckReport(
            passed=True,
            checks=tuple(checks),
            skipped=tuple(skipped),
            capability_gaps=tuple(capability_gaps),
        )

    @staticmethod
    def _example(name: str) -> dict[str, Any]:
        try:
            value = parse_json_bytes((_CONTRACT_ROOT / "examples" / name).read_bytes())
        except (OSError, ContractValidationError) as exc:
            raise TckFailure("TCK example is unavailable") from exc
        if not isinstance(value, dict):
            raise TckFailure("TCK example is malformed")
        return value

    def _scenario(self, name: str, request: Mapping[str, Any]) -> FaultScenario | None:
        if self._fault_driver is None:
            raise TckFailure("fault scenario driver is unavailable")
        try:
            scenario = self._fault_driver.scenario(name, deepcopy(request))
        except Exception as exc:
            raise TckFailure("fault scenario driver failed") from exc
        if scenario is not None and not isinstance(scenario, FaultScenario):
            raise TckFailure("fault scenario driver returned an invalid scenario")
        return scenario

    def _run_fault_check(
        self,
        name: str,
        request: Mapping[str, Any],
        scenario: FaultScenario,
        checks: list[str],
    ) -> None:
        probe = deepcopy(request)
        probe["request_ref"] = "req_" + "A" * 20 + name.replace("fault_", "").replace("_", "-")
        key = "tck-{}-00000001".format(name.replace("fault_", ""))
        try:
            if scenario.before_submit is not None:
                scenario.before_submit(deepcopy(probe))
            if name == "fault_429":
                response = self._request("POST", "/v1/solve-jobs", document=probe, idempotency_key=key)
                error = self._retryable_error(response, expected_status=429, expected_category="capacity")
                retry_after = response.headers.get("retry-after")
                if retry_after is None or not retry_after.strip():
                    raise TckFailure("429 response omitted Retry-After")
                if error["code"] == "IDEMPOTENCY_CONFLICT":
                    raise TckFailure("capacity failure used a contract error code")
            elif name == "fault_502":
                response = self._request("POST", "/v1/solve-jobs", document=probe, idempotency_key=key)
                self._retryable_error(response, expected_status=502, expected_category="transport")
            elif name == "fault_503":
                response = self._request("POST", "/v1/solve-jobs", document=probe, idempotency_key=key)
                self._retryable_error(response, expected_status=503, expected_category="transport")
            elif name == "fault_timeout":
                try:
                    self._request(
                        "POST",
                        "/v1/solve-jobs",
                        document=probe,
                        idempotency_key=key,
                    )
                except TckFailure as exc:
                    if "unavailable" not in str(exc):
                        raise
                else:
                    raise TckFailure("timeout scenario did not interrupt transport")
            elif name == "fault_execution_failed":
                response = self._request("POST", "/v1/solve-jobs", document=probe, idempotency_key=key)
                job = self._job_response(response, expected_status=202)
                if scenario.after_failure is not None:
                    scenario.after_failure()
                observed = self._poll_terminal(job)
                if observed["status"] != "failed":
                    raise TckFailure("execution fault was not reported as failed")
                unavailable = self._request("GET", "/v1/solve-jobs/{}/result".format(job["job_id"]))
                error = self._error_response(unavailable, expected_status=409)
                if error["category"] != "result" or error["retryable"] is not False:
                    raise TckFailure("failed result error mapping is invalid")
            elif name in {"fault_attempt_lost", "fault_restart"}:
                response = self._request("POST", "/v1/solve-jobs", document=probe, idempotency_key=key)
                job = self._job_response(response, expected_status=202)
                if scenario.after_submit is not None:
                    scenario.after_submit(job)
                observed = self._poll_terminal(job)
                if name == "fault_attempt_lost":
                    if observed["status"] != "attempt_lost":
                        raise TckFailure("lost attempt was not reported explicitly")
                    missing = self._request("GET", "/v1/solve-jobs/{}/result".format(job["job_id"]))
                    error = self._error_response(missing, expected_status=409)
                    if error["category"] != "result" or error["retryable"] is not False:
                        raise TckFailure("attempt_lost result error mapping is invalid")
                elif observed["status"] == "completed":
                    recovered = self._request("GET", "/v1/solve-jobs/{}/result".format(job["job_id"]))
                    self._validate("result", recovered.document)
                    if recovered.document.get("request_ref") != probe["request_ref"]:
                        raise TckFailure("restart recovery changed request identity")
                elif observed["status"] != "attempt_lost":
                    raise TckFailure("restart must recover the attempt or report attempt_lost")
            elif name == "fault_late_result":
                response = self._request("POST", "/v1/solve-jobs", document=probe, idempotency_key=key)
                job = self._job_response(response, expected_status=202)
                if scenario.after_submit is not None:
                    scenario.after_submit(job)
                cancelled = self._job_response(
                    self._request("DELETE", "/v1/solve-jobs/{}".format(job["job_id"])),
                    expected_status=200,
                )
                if cancelled["status"] != "cancelled":
                    raise TckFailure("late-result scenario did not cancel the attempt")
                if scenario.after_cancel is not None:
                    scenario.after_cancel(cancelled)
                observed = self._job_response(
                    self._request("GET", "/v1/solve-jobs/{}".format(job["job_id"])),
                    expected_status=200,
                )
                if observed["status"] != "cancelled":
                    raise TckFailure("late result overwrote the cancelled terminal state")
                unavailable = self._request("GET", "/v1/solve-jobs/{}/result".format(job["job_id"]))
                error = self._error_response(unavailable, expected_status=409)
                if error["category"] != "result" or error["retryable"] is not False:
                    raise TckFailure("cancelled result error mapping is invalid")
            else:
                raise TckFailure("unknown fault scenario")
            checks.append(name)
        finally:
            if scenario.cleanup is not None:
                try:
                    scenario.cleanup()
                except Exception as exc:
                    raise TckFailure("fault scenario cleanup failed") from exc

    def _retryable_error(
        self,
        response: _Response,
        *,
        expected_status: int,
        expected_category: str,
    ) -> Mapping[str, Any]:
        error = self._error_response(response, expected_status=expected_status)
        if error["category"] != expected_category or error["retryable"] is not True:
            raise TckFailure("temporary failure error mapping is invalid")
        return error

    def _check_health(self) -> None:
        response = self._request("GET", "/v1/health", authenticated=False)
        if response.status != 200 or response.document != {"status": "ok"}:
            raise TckFailure("health endpoint is not conformant")

    def _check_capabilities(self) -> Mapping[str, int]:
        response = self._request("GET", "/v1/capabilities")
        if response.status != 200:
            raise TckFailure("capabilities endpoint failed")
        self._validate("capabilities", response.document)
        families = response.document.get("problem_families", [])
        family = None
        for candidate_family in families:
            if not isinstance(candidate_family, Mapping):
                continue
            if candidate_family.get("name") != "binary_ordered_route":
                continue
            if "1.0" in candidate_family.get("versions", []):
                family = candidate_family
                break
        if family is None:
            raise TckFailure("required family capability is absent")
        limits = family.get("limits")
        if not isinstance(limits, Mapping):
            raise TckFailure("family capability limits are absent")
        max_workers = limits.get("max_workers")
        max_solve_time_ms = limits.get("max_solve_time_ms")
        if isinstance(max_workers, bool) or not isinstance(max_workers, int):
            raise TckFailure("family capability limits are invalid")
        if max_workers < 1:
            raise TckFailure("family capability limits are invalid")
        if isinstance(max_solve_time_ms, bool) or not isinstance(max_solve_time_ms, int):
            raise TckFailure("family capability limits are invalid")
        if max_solve_time_ms < 1:
            raise TckFailure("family capability limits are invalid")
        return {
            "max_workers": max_workers,
            "max_solve_time_ms": max_solve_time_ms,
        }

    def _check_rejected_submit(
        self,
        document: Mapping[str, Any],
        *,
        expected_status: int,
        expected_code: str,
        idempotency_key: str | None = None,
    ) -> None:
        response = self._request(
            "POST",
            "/v1/solve-jobs",
            document=document,
            idempotency_key=idempotency_key,
        )
        error = self._error_response(response, expected_status=expected_status)
        if error["code"] != expected_code or error["category"] != "contract":
            raise TckFailure("Connector returned the wrong contract error")
        if error["retryable"] is not False:
            raise TckFailure("contract error must not be retryable")

    def _poll_terminal(self, submitted: Mapping[str, Any]) -> Mapping[str, Any]:
        job_id = submitted["job_id"]
        deadline = time.monotonic() + self.timeout_seconds
        observed = [submitted["status"]]
        current = submitted
        while current["status"] not in _TERMINAL_STATUSES:
            if time.monotonic() >= deadline:
                raise TckFailure("Connector job did not reach a terminal state")
            time.sleep(min(0.05, self.timeout_seconds / 10))
            response = self._request("GET", "/v1/solve-jobs/{}".format(job_id))
            current = self._job_response(response, expected_status=200)
            if current["job_id"] != job_id:
                raise TckFailure("polled job identity changed")
            observed.append(current["status"])
        allowed = {
            "accepted": {"accepted", "running", "completed", "failed", "cancelled", "attempt_lost"},
            "running": {"running", "completed", "failed", "cancelled", "attempt_lost"},
        }
        for before, after in zip(observed, observed[1:]):
            if before in _TERMINAL_STATUSES and after != before:
                raise TckFailure("terminal job state changed")
            if before in allowed and after not in allowed[before]:
                raise TckFailure("Connector job state transition is invalid")
        return current

    def _check_result(
        self,
        *,
        job_id: str,
        request_ref: str,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        response = self._request("GET", "/v1/solve-jobs/{}/result".format(job_id))
        if response.status != 200:
            raise TckFailure("completed result endpoint failed")
        self._validate("result", response.document)
        result = response.document
        problem = request["problem"]
        if (
            result.get("request_ref") != request_ref
            or result.get("problem_family") != problem["problem_family"]
            or result.get("family_version") != problem["family_version"]
        ):
            raise TckFailure("result identity does not match the submitted problem")
        return result

    @staticmethod
    def _check_golden(*, request: Mapping[str, Any], result: Mapping[str, Any]) -> None:
        selection = request["problem"]["payload"]["parameters"]["candidate_selection"]
        candidates = selection["candidates"]
        objectives = request["problem"]["payload"]["objectives"]

        def objective_key(candidate: Mapping[str, Any]) -> tuple[int, ...]:
            values = []
            for objective in objectives:
                metric_value = candidate["metrics"][objective["metric"]]
                values.append(-metric_value if objective["sense"] == "maximize" else metric_value)
            return tuple(values)

        eligible = [candidate for candidate in candidates if candidate["eligible"]]
        if not eligible:
            if result.get("solve_outcome") != "infeasible":
                raise TckFailure("golden infeasibility result diverged")
            return
        expected = min(eligible, key=objective_key)
        expected_assignment = {
            "candidate_id": expected["candidate_id"],
            **expected["assignment"],
        }
        expected_objectives = {
            objective["metric"]: expected["metrics"][objective["metric"]] for objective in objectives
        }
        if (
            result.get("solve_outcome") != "optimal"
            or result.get("assignments") != expected_assignment
            or result.get("objective_values") != expected_objectives
        ):
            raise TckFailure("family golden result diverged")

    def _job_response(self, response: _Response, *, expected_status: int) -> Mapping[str, Any]:
        if response.status != expected_status:
            raise TckFailure("Connector returned an unexpected job HTTP status")
        self._validate("job", response.document)
        return response.document

    def _error_response(self, response: _Response, *, expected_status: int) -> Mapping[str, Any]:
        if response.status != expected_status:
            raise TckFailure("Connector returned an unexpected error HTTP status")
        self._validate("error", response.document)
        return response.document["error"]

    @staticmethod
    def _validate(name: str, document: Any) -> None:
        try:
            validate_document(name, document)
        except ContractValidationError as exc:
            raise TckFailure("Connector returned an invalid contract document") from exc

    def _request(
        self,
        method: str,
        path: str,
        *,
        document: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        authenticated: bool = True,
    ) -> _Response:
        headers = {"Accept": "application/json"}
        if authenticated and self._bearer_token is not None:
            headers["Authorization"] = "Bearer " + self._bearer_token
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        data = None
        if document is not None:
            data = canonical_json_bytes(document)
            headers["Content-Type"] = "application/json"
        request = Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            response = self._opener.open(request, timeout=self.timeout_seconds)
        except HTTPError as exc:
            response = exc
        except OSError as exc:
            raise TckFailure("Connector endpoint is unavailable") from exc
        try:
            content_type = response.headers.get_content_type()
            response_headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
            status = int(response.status)
        except (OSError, ValueError) as exc:
            raise TckFailure("Connector response could not be read") from exc
        finally:
            response.close()
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise TckFailure("Connector response exceeds the TCK limit")
        if content_type != "application/json":
            raise TckFailure("Connector response is not application/json")
        try:
            parsed = parse_json_bytes(raw)
        except ContractValidationError as exc:
            raise TckFailure("Connector response is not strict JSON") from exc
        if not isinstance(parsed, Mapping):
            raise TckFailure("Connector response must be a JSON object")
        return _Response(
            status=status,
            document=parsed,
            raw=raw,
            headers=response_headers,
        )


__all__ = [
    "ConnectorTckRunner",
    "FaultScenario",
    "FaultScenarioDriver",
    "TckFailure",
    "TckReport",
]
