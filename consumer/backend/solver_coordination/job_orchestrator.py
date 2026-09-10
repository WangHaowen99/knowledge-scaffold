"""Durable solver submission, polling, cancellation, and recovery orchestration."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import hashlib
import hmac
import inspect
import logging
import threading
from typing import Any
import uuid

from .canonical import sha256_digest
from .contracts import ProblemIR, SolverResult
from .connectors.errors import (
    ConnectorAuthenticationError,
    ConnectorCapacityError,
    ConnectorContractError,
    ConnectorError,
    ConnectorExecutionError,
    ConnectorResultError,
    ConnectorTransportError,
)
from .job_store import (
    TERMINAL_ATTEMPT_STATUSES,
    TERMINAL_JOB_STATUSES,
    JobStoreConflict,
    JobStoreError,
    LeaseConflict,
    OptimisticLockError,
    SolverJobStore,
    TerminalStateError,
)

logger = logging.getLogger(__name__)


class JobOrchestrationError(RuntimeError):
    pass


class BackendProtocolError(JobOrchestrationError):
    pass


class BackendTransientError(JobOrchestrationError):
    """A backend transport failure that should remain recoverable."""


def _map_connector_exception(exc: BaseException) -> BaseException:
    """Map sanitized generic-client errors to durable orchestration semantics."""

    if isinstance(exc, (ConnectorTransportError, ConnectorCapacityError)):
        return BackendTransientError("solver Connector transport/capacity is retryable")
    if isinstance(exc, ConnectorAuthenticationError):
        return BackendProtocolError("solver Connector authentication failed")
    if isinstance(exc, ConnectorContractError):
        return BackendProtocolError("solver Connector contract was rejected")
    if isinstance(exc, (ConnectorExecutionError, ConnectorResultError)):
        return BackendProtocolError("solver Connector execution/result was rejected")
    if isinstance(exc, ConnectorError):
        return BackendProtocolError("solver Connector request failed")
    return exc


_PRE_SUBMISSION_PHASES = frozenset(("created", "retry", "freeze_inputs"))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise JobOrchestrationError("deadline is invalid") from exc
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise JobOrchestrationError("deadline must be timezone-aware")
    return value.astimezone(timezone.utc)


def _backend_value(payload: Any, name: str) -> Any:
    if isinstance(payload, Mapping):
        return payload.get(name)
    return getattr(payload, name, None)


def _attempt_is_ready_for_submission(
    job: Mapping[str, Any],
    attempt: Mapping[str, Any] | None,
    *,
    expected_attempt_id: str,
) -> bool:
    if attempt is None:
        return False
    if attempt["attempt_id"] != expected_attempt_id:
        return False
    if attempt["backend_job_id"] is not None:
        return False
    if job["status"] in TERMINAL_JOB_STATUSES:
        return False
    if job["phase"] == "freeze_inputs":
        return False
    if job["status"] == "cancelling":
        return False
    return True


class DurableJobOrchestrator:
    """Coordinates a backend exclusively through persisted job state."""

    def __init__(
        self,
        *,
        store: SolverJobStore,
        backend: Any,
        idempotency_secret: bytes,
        binding_revision_id: str | None = None,
        lease_owner: str | None = None,
        lease_seconds: int = 30,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(store, SolverJobStore):
            raise TypeError("store must be a SolverJobStore")
        if not isinstance(idempotency_secret, bytes) or not idempotency_secret:
            raise ValueError("idempotency_secret must be non-empty bytes")
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds < 1:
            raise ValueError("lease_seconds must be a positive integer")
        for method in ("submit", "poll", "cancel", "result"):
            if not callable(getattr(backend, method, None)):
                raise TypeError("backend must implement {}".format(method))
        self.store = store
        self.backend = backend
        self._idempotency_secret = idempotency_secret
        if binding_revision_id is not None and (
            not isinstance(binding_revision_id, str) or not binding_revision_id.strip()
        ):
            raise ValueError("binding_revision_id must be a non-empty string")
        self.binding_revision_id = binding_revision_id.strip() if isinstance(binding_revision_id, str) else None
        self.lease_owner = (lease_owner or "solver_worker_{}".format(uuid.uuid4().hex)).strip()
        if not self.lease_owner:
            raise ValueError("lease_owner is required")
        self.lease_seconds = lease_seconds
        self._clock = clock or _utc_now

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise JobOrchestrationError("clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    def _operation_lease_seconds(self) -> int:
        """Keep a backend boundary alive long enough for one blocking call."""

        return max(self.lease_seconds, 60)

    def _binding_configuration(self, *, job: Mapping[str, Any], workspace_id: str, owner_id: str) -> Mapping[str, Any]:
        record = self.store.get_binding_revision(
            str(job["binding_revision_id"]),
            workspace_id=workspace_id,
            owner_id=owner_id,
        )
        configuration = record.get("configuration") if isinstance(record, Mapping) else None
        if not isinstance(configuration, Mapping):
            raise BackendProtocolError("frozen solver binding revision is unavailable")
        return dict(configuration)

    def _backend_call(
        self,
        method_name: str,
        *,
        binding: Mapping[str, Any],
        **kwargs: Any,
    ) -> Any:
        """Call the neutral backend port with one explicit fixture bridge.

        Production connectors accept ``binding``.  A narrow
        signature check keeps pre-federation synthetic test doubles runnable
        during migration without catching a runtime TypeError and retrying it.
        """

        method = getattr(self.backend, method_name)
        try:
            parameters = inspect.signature(method).parameters
            accepts_var_kwargs = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
            )
            accepts_binding = "binding" in parameters or accepts_var_kwargs
        except (TypeError, ValueError):
            parameters = {}
            accepts_var_kwargs = True
            accepts_binding = True
        if not accepts_var_kwargs:
            kwargs = {name: value for name, value in kwargs.items() if name in parameters}
        # Some fixture backends predate ``result_quality``. A ``**kwargs`` wrapper may still
        # forward unknown keywords to an older strict implementation, so only
        # send this optional field when the actual callable declares it.
        if "result_quality" in kwargs and "result_quality" not in parameters:
            kwargs.pop("result_quality", None)
        # Trace context is likewise optional for narrow synthetic backends;
        # never let a compatibility wrapper receive an unknown keyword.
        if "traceparent" in kwargs and "traceparent" not in parameters:
            kwargs.pop("traceparent", None)
        if accepts_binding:
            kwargs["binding"] = binding
        return method(**kwargs)

    def _submit_backend_with_lease(
        self,
        *,
        problem: ProblemIR,
        idempotency_key: str,
        deadline: datetime,
        job_id: str,
        workspace_id: str,
        owner_id: str,
        lease_owner: str | None,
        fencing_token: int | None,
        binding: Mapping[str, Any],
        result_quality: str = "optimal",
        traceparent: str | None = None,
    ) -> Any:
        """Run submit while the owning worker renews its durable lease."""

        if lease_owner is None or fencing_token is None:
            return self._backend_call(
                "submit",
                binding=binding,
                problem=problem,
                idempotency_key=idempotency_key,
                deadline=deadline,
                result_quality=result_quality,
                traceparent=traceparent,
            )

        result: list[Any] = []
        failure: list[BaseException] = []
        finished = threading.Event()

        def invoke() -> None:
            try:
                result.append(
                    self._backend_call(
                        "submit",
                        binding=binding,
                        problem=problem,
                        idempotency_key=idempotency_key,
                        deadline=deadline,
                        result_quality=result_quality,
                        traceparent=traceparent,
                    )
                )
            except BaseException as exc:  # propagate backend exceptions on the caller thread
                failure.append(exc)
            finally:
                finished.set()

        worker = threading.Thread(
            target=invoke,
            name="solver-submit-{}".format(uuid.uuid4().hex[:8]),
            daemon=True,
        )
        worker.start()
        renewal_failure: list[BaseException] = []
        interval = max(min(self._operation_lease_seconds() / 3, 1.0), 0.05)
        while not finished.wait(interval):
            try:
                self.store.renew_job_lease(
                    job_id,
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    lease_owner=lease_owner,
                    fencing_token=fencing_token,
                    lease_seconds=self._operation_lease_seconds(),
                )
            except BaseException as exc:
                renewal_failure.append(exc)
                break
        worker.join()
        if renewal_failure:
            raise renewal_failure[0]
        if failure:
            raise failure[0]
        if not result:
            raise BackendProtocolError("backend submit returned no result")
        return result[0]

    def _backend_idempotency_key(self, logical_job_key: str, attempt_id: str) -> str:
        message = "{}\0{}".format(logical_job_key, attempt_id).encode("utf-8")
        return "hmac-sha256:" + hmac.new(self._idempotency_secret, message, hashlib.sha256).hexdigest()

    @staticmethod
    def _run_result_quality(job: Mapping[str, Any]) -> str:
        policy = job.get("run_policy")
        quality = policy.get("result_quality", "optimal") if isinstance(policy, Mapping) else "optimal"
        if quality not in {"optimal", "feasible", "any"}:
            raise BackendProtocolError("persisted solver result quality is invalid")
        return str(quality)

    @staticmethod
    def _run_traceparent(job: Mapping[str, Any]) -> str | None:
        policy = job.get("run_policy")
        value = policy.get("traceparent") if isinstance(policy, Mapping) else None
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise BackendProtocolError("persisted solver trace context is invalid")
        return value.strip()

    @staticmethod
    def _problem_payload(problem: ProblemIR | Mapping[str, Any]) -> tuple[dict[str, Any], str]:
        if isinstance(problem, ProblemIR):
            return problem.to_dict(), problem.problem_digest
        if not isinstance(problem, Mapping):
            raise JobOrchestrationError("problem must be ProblemIR or a mapping")
        payload = dict(problem)
        problem_id = payload.get("problem_id")
        if not isinstance(problem_id, str) or not problem_id.strip():
            raise JobOrchestrationError("problem_id is required")
        return payload, problem_id.strip()

    def start_or_resume(
        self,
        *,
        workspace_id: str,
        owner_id: str,
        binding_revision_id: str,
        problem: ProblemIR | Mapping[str, Any],
        run_policy: Mapping[str, Any],
        deadline_at: datetime,
        run_id: str,
        turn_id: str,
        tool_call_id: str,
        card_name: str,
        contract_digest: str,
        attempt_id: str | None = None,
        defer_submit: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(defer_submit, bool):
            raise TypeError("defer_submit must be a boolean")
        problem_payload, problem_digest = self._problem_payload(problem)
        existing_link = self.store.get_run_link(
            workspace_id=workspace_id,
            owner_id=owner_id,
            run_id=run_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
        )
        if existing_link is not None:
            if existing_link["card_name"] != card_name or existing_link["contract_digest"] != contract_digest:
                raise JobStoreConflict("solver contract drifted during run recovery")
            job = self._require_job(existing_link["job_id"], workspace_id=workspace_id, owner_id=owner_id)
            if job["binding_revision_id"] != binding_revision_id:
                raise JobStoreConflict("solver tool call is already linked to a different binding revision")
            if sha256_digest(job["run_policy"]) != sha256_digest(dict(run_policy)):
                raise JobStoreConflict("solver tool call is already linked to a different run policy")
            if _as_utc(job["deadline_at"]) != _as_utc(deadline_at):
                raise JobStoreConflict("solver tool call is already linked to a different deadline")
            if job["problem_digest"] != problem_digest or sha256_digest(job["problem"]) != sha256_digest(
                problem_payload
            ):
                raise JobStoreConflict("solver tool call is already linked to a different problem payload")
            if job["status"] == "cancelling":
                cancelled = self._finish_cancellation(job, workspace_id=workspace_id, owner_id=owner_id)
                cancelled["subscription_id"] = existing_link["subscription_id"]
                return cancelled
            stored_attempt = self._require_attempt(
                existing_link["attempt_id"],
                workspace_id=workspace_id,
                owner_id=owner_id,
            )
            if (
                stored_attempt["backend_job_id"] is None
                and job["status"] not in TERMINAL_JOB_STATUSES
                and not defer_submit
            ):
                job, stored_attempt = self._submit_attempt_with_lease(
                    job,
                    stored_attempt,
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                )
            subscription = self._subscription_outcome(existing_link["subscription_id"], job)
            return self._outcome(job, stored_attempt, subscription)

        preparation_lease_owner = "{}:freeze:{}".format(self.lease_owner, uuid.uuid4().hex) if defer_submit else None
        preparation_lease_seconds = max(self.lease_seconds, 60)
        job, created = self.store.create_or_get_job(
            workspace_id=workspace_id,
            owner_id=owner_id,
            binding_revision_id=binding_revision_id,
            problem_digest=problem_digest,
            problem=problem_payload,
            run_policy=dict(run_policy),
            deadline_at=deadline_at,
            run_id=run_id,
            initial_lease_owner=preparation_lease_owner,
            initial_lease_seconds=preparation_lease_seconds if defer_submit else None,
        )
        stored_attempt = self.store.latest_attempt(job["job_id"], workspace_id=workspace_id, owner_id=owner_id)
        if stored_attempt is None:
            selected_attempt_id = attempt_id or "attempt_{}".format(uuid.uuid4().hex)
            stored_attempt, _ = self.store.create_or_get_initial_attempt(
                job["job_id"],
                workspace_id=workspace_id,
                owner_id=owner_id,
                attempt_id=selected_attempt_id,
                request_payload=problem_payload,
                backend_idempotency_key=self._backend_idempotency_key(job["logical_job_key"], selected_attempt_id),
            )

        subscription_id = "subscription_{}".format(uuid.uuid4().hex)
        link, subscription, _ = self.store.create_subscription_and_link(
            workspace_id=workspace_id,
            owner_id=owner_id,
            run_id=run_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            job_id=job["job_id"],
            attempt_id=stored_attempt["attempt_id"],
            subscription_id=subscription_id,
            card_name=card_name,
            contract_digest=contract_digest,
        )
        if stored_attempt["backend_job_id"] is None and job["status"] not in TERMINAL_JOB_STATUSES and not defer_submit:
            job, stored_attempt = self._submit_attempt_with_lease(
                job,
                stored_attempt,
                workspace_id=workspace_id,
                owner_id=owner_id,
            )
        outcome = self._outcome(job, stored_attempt, subscription, link=link)
        if created and preparation_lease_owner is not None:
            outcome["input_preparation_lease"] = {
                "lease_owner": preparation_lease_owner,
                "fencing_token": job["fencing_token"],
                "lease_seconds": preparation_lease_seconds,
            }
        return outcome

    def retry(
        self,
        job_id: str,
        *,
        workspace_id: str,
        owner_id: str,
        attempt_id: str | None = None,
    ) -> dict[str, Any]:
        job = self._require_job(job_id, workspace_id=workspace_id, owner_id=owner_id)
        selected_attempt_id = attempt_id or "attempt_{}".format(uuid.uuid4().hex)
        attempt, _, superseded_backend_job_id = self.store.prepare_retry_attempt(
            job_id,
            workspace_id=workspace_id,
            owner_id=owner_id,
            attempt_id=selected_attempt_id,
            request_payload=job["problem"],
            backend_idempotency_key=self._backend_idempotency_key(job["logical_job_key"], selected_attempt_id),
        )
        if superseded_backend_job_id is not None:
            self._backend_call(
                "cancel",
                job_id=superseded_backend_job_id,
                binding=self._binding_configuration(job=job, workspace_id=workspace_id, owner_id=owner_id),
            )
        else:
            attempts = self.store.list_attempts(job_id, workspace_id=workspace_id, owner_id=owner_id)
            if len(attempts) >= 2:
                previous = attempts[-2]
                if previous["error_code"] == "superseded_by_retry" and previous["backend_job_id"] is not None:
                    self._backend_call(
                        "cancel",
                        job_id=previous["backend_job_id"],
                        binding=self._binding_configuration(job=job, workspace_id=workspace_id, owner_id=owner_id),
                    )
        job = self._require_job(job_id, workspace_id=workspace_id, owner_id=owner_id)
        job, attempt = self._submit_attempt_with_lease(job, attempt, workspace_id=workspace_id, owner_id=owner_id)
        return self._outcome(job, attempt, None)

    def _submit_attempt(
        self,
        job: Mapping[str, Any],
        attempt: Mapping[str, Any],
        *,
        workspace_id: str,
        owner_id: str,
        fencing_token: int | None = None,
        lease_owner: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        current_job = self._require_job(job["job_id"], workspace_id=workspace_id, owner_id=owner_id)
        if current_job["status"] in TERMINAL_JOB_STATUSES:
            return current_job, dict(attempt)
        if current_job["status"] != "submitting":
            current_job, _ = self._transition_job(
                current_job["job_id"],
                workspace_id=workspace_id,
                owner_id=owner_id,
                status="submitting",
                phase="submit_job",
                event_type="solver.job.submitting",
                fencing_token=fencing_token,
            )
        deadline = _as_utc(current_job["deadline_at"])
        try:
            submitted = self._submit_backend_with_lease(
                problem=self._restore_problem(attempt["request_payload"]),
                idempotency_key=attempt["backend_idempotency_key"],
                deadline=deadline,
                job_id=current_job["job_id"],
                workspace_id=workspace_id,
                owner_id=owner_id,
                lease_owner=lease_owner,
                fencing_token=fencing_token,
                binding=self._binding_configuration(
                    job=current_job,
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                ),
                result_quality=self._run_result_quality(current_job),
                traceparent=self._run_traceparent(current_job),
            )
        except TimeoutError:
            self.store.append_event(
                current_job["job_id"],
                workspace_id=workspace_id,
                owner_id=owner_id,
                attempt_id=attempt["attempt_id"],
                event_type="solver.submit.uncertain",
                payload={"phase": "submit_job", "retryable": True},
            )
            return current_job, self._require_attempt(
                attempt["attempt_id"], workspace_id=workspace_id, owner_id=owner_id
            )
        except Exception as exc:
            mapped = _map_connector_exception(exc)
            if mapped is not exc:
                raise mapped from exc
            raise
        backend_job_id = _backend_value(submitted, "job_id")
        if not isinstance(backend_job_id, str) or not backend_job_id.strip():
            raise BackendProtocolError("backend submit did not return a job_id")
        stored_attempt = self.store.bind_backend_job(
            attempt["attempt_id"],
            workspace_id=workspace_id,
            owner_id=owner_id,
            backend_job_id=backend_job_id,
            fencing_token=fencing_token,
        )
        current_job = self._require_job(current_job["job_id"], workspace_id=workspace_id, owner_id=owner_id)
        if current_job["status"] == "cancelling":
            # A cancellation may have been reserved while submit was in flight.
            # Finish it under the same lease instead of publishing a waiting state.
            self._finish_cancellation(current_job, workspace_id=workspace_id, owner_id=owner_id)
            cancelled_job = self._require_job(current_job["job_id"], workspace_id=workspace_id, owner_id=owner_id)
            cancelled_attempt = self.store.latest_attempt(
                current_job["job_id"], workspace_id=workspace_id, owner_id=owner_id
            )
            return cancelled_job, cancelled_attempt or stored_attempt
        if current_job["status"] != "waiting_solver":
            current_job, _ = self._transition_job(
                current_job["job_id"],
                workspace_id=workspace_id,
                owner_id=owner_id,
                status="waiting_solver",
                phase="solve",
                event_type="solver.job.submitted",
                fencing_token=fencing_token,
            )
        return current_job, stored_attempt

    def _submit_attempt_with_lease(
        self,
        job: Mapping[str, Any],
        attempt: Mapping[str, Any],
        *,
        workspace_id: str,
        owner_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Submit only after acquiring a short-lived, uniquely owned job lease."""

        current_job = self._require_job(job["job_id"], workspace_id=workspace_id, owner_id=owner_id)
        current_attempt = self.store.latest_attempt(current_job["job_id"], workspace_id=workspace_id, owner_id=owner_id)
        if not _attempt_is_ready_for_submission(
            current_job,
            current_attempt,
            expected_attempt_id=attempt["attempt_id"],
        ):
            return current_job, current_attempt

        submission_lease_owner = "{}:submit:{}".format(self.lease_owner, uuid.uuid4().hex)
        claimed: dict[str, Any] | None = None
        try:
            try:
                claimed = self.store.claim_job(
                    current_job["job_id"],
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    lease_owner=submission_lease_owner,
                    lease_seconds=self._operation_lease_seconds(),
                    expected_version=current_job["version"],
                )
            except (LeaseConflict, OptimisticLockError, TerminalStateError):
                latest = self._require_job(
                    current_job["job_id"],
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                )
                return latest, self.store.latest_attempt(latest["job_id"], workspace_id=workspace_id, owner_id=owner_id)

            claimed_attempt = self.store.latest_attempt(claimed["job_id"], workspace_id=workspace_id, owner_id=owner_id)
            if not _attempt_is_ready_for_submission(
                claimed,
                claimed_attempt,
                expected_attempt_id=attempt["attempt_id"],
            ):
                return claimed, claimed_attempt
            try:
                self._submit_attempt(
                    claimed,
                    claimed_attempt,
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    fencing_token=claimed["fencing_token"],
                    lease_owner=submission_lease_owner,
                )
            except LeaseConflict:
                latest = self._require_job(claimed["job_id"], workspace_id=workspace_id, owner_id=owner_id)
                return latest, self.store.latest_attempt(latest["job_id"], workspace_id=workspace_id, owner_id=owner_id)
            except Exception:
                latest = self._require_job(claimed["job_id"], workspace_id=workspace_id, owner_id=owner_id)
                if latest["status"] == "cancelling":
                    return latest, self.store.latest_attempt(
                        latest["job_id"],
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                    )
                raise
        finally:
            if claimed is not None:
                current = self.store.get_job(claimed["job_id"], workspace_id=workspace_id, owner_id=owner_id)
                if (
                    current is not None
                    and current["lease_owner"] == submission_lease_owner
                    and current["fencing_token"] == claimed["fencing_token"]
                ):
                    try:
                        self.store.release_job_lease(
                            claimed["job_id"],
                            workspace_id=workspace_id,
                            owner_id=owner_id,
                            lease_owner=submission_lease_owner,
                            fencing_token=claimed["fencing_token"],
                        )
                    except LeaseConflict:
                        logger.debug("Submission lease was already released or superseded")

        refreshed = self._require_job(current_job["job_id"], workspace_id=workspace_id, owner_id=owner_id)
        return refreshed, self.store.latest_attempt(refreshed["job_id"], workspace_id=workspace_id, owner_id=owner_id)

    def _transition_job(
        self,
        job_id: str,
        *,
        workspace_id: str,
        owner_id: str,
        status: str,
        phase: str,
        event_type: str,
        attempt_id: str | None = None,
        error_code: str | None = None,
        fencing_token: int | None = None,
    ) -> tuple[dict[str, Any], bool]:
        for _ in range(8):
            current = self._require_job(job_id, workspace_id=workspace_id, owner_id=owner_id)
            if current["status"] == status and current["phase"] == phase:
                return current, False
            if current["status"] in TERMINAL_JOB_STATUSES:
                return current, False
            try:
                return (
                    self.store.transition_job_with_event(
                        job_id,
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                        expected_version=current["version"],
                        status=status,
                        phase=phase,
                        error_code=error_code,
                        fencing_token=fencing_token,
                        attempt_id=attempt_id,
                        event_type=event_type,
                        event_payload={"phase": phase, "status": status},
                    )[0],
                    True,
                )
            except OptimisticLockError:
                continue
        raise JobOrchestrationError("solver job changed too frequently to transition")

    @staticmethod
    def _restore_problem(payload: Mapping[str, Any]) -> ProblemIR:
        try:
            return ProblemIR(
                schema_version=payload.get("schema_version", "solver.problem.v1"),
                problem_id=payload["problem_id"],
                problem_family=payload["problem_family"],
                decision_space=payload["decision_space"],
                parameters=payload["parameters"],
                constraints=tuple(payload["constraints"]),
                objectives=tuple(payload["objectives"]),
                limits=payload["limits"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise JobOrchestrationError("persisted ProblemIR is invalid") from exc

    def poll_once(self, job_id: str, *, workspace_id: str, owner_id: str) -> dict[str, Any]:
        job = self._require_job(job_id, workspace_id=workspace_id, owner_id=owner_id)
        if job["status"] in TERMINAL_JOB_STATUSES or job["status"] == "solver_completed":
            return self._outcome(
                job,
                self.store.latest_attempt(job_id, workspace_id=workspace_id, owner_id=owner_id),
                None,
            )
        if job["status"] == "cancelling":
            return self._finish_cancellation(job, workspace_id=workspace_id, owner_id=owner_id)
        if job["phase"] == "freeze_inputs":
            return self._outcome(
                job,
                self.store.latest_attempt(job_id, workspace_id=workspace_id, owner_id=owner_id),
                None,
            )
        poll_lease_owner = "{}:poll:{}".format(self.lease_owner, uuid.uuid4().hex)
        claimed = self.store.claim_job(
            job_id,
            workspace_id=workspace_id,
            owner_id=owner_id,
            lease_owner=poll_lease_owner,
            lease_seconds=self._operation_lease_seconds(),
            expected_version=job["version"],
        )
        try:
            attempt = self.store.latest_attempt(job_id, workspace_id=workspace_id, owner_id=owner_id)
            if attempt is None:
                raise JobOrchestrationError("solver job has no attempt")
            if attempt["backend_job_id"] is None:
                claimed, attempt = self._submit_attempt(
                    claimed,
                    attempt,
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    fencing_token=claimed["fencing_token"],
                    lease_owner=poll_lease_owner,
                )
                if attempt["backend_job_id"] is None:
                    return self._outcome(claimed, attempt, None)
                claimed = self._require_job(job_id, workspace_id=workspace_id, owner_id=owner_id)
            if self._now() >= _as_utc(claimed["deadline_at"]):
                self._backend_call(
                    "cancel",
                    job_id=attempt["backend_job_id"],
                    binding=self._binding_configuration(job=claimed, workspace_id=workspace_id, owner_id=owner_id),
                )
                self.store.update_attempt_status(
                    attempt["attempt_id"],
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    status="timed_out",
                    error_code="timed_out_no_solution",
                    fencing_token=claimed["fencing_token"],
                )
                timed_out, _ = self._transition_job(
                    job_id,
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    status="timed_out",
                    phase="solve",
                    error_code="timed_out_no_solution",
                    fencing_token=claimed["fencing_token"],
                    attempt_id=attempt["attempt_id"],
                    event_type="solver.job.timed_out",
                )
                return self._outcome(
                    timed_out,
                    self._require_attempt(
                        attempt["attempt_id"],
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                    ),
                    None,
                )
            try:
                polled = self._backend_call(
                    "poll",
                    job_id=attempt["backend_job_id"],
                    binding=self._binding_configuration(job=claimed, workspace_id=workspace_id, owner_id=owner_id),
                )
            except Exception as exc:
                mapped = _map_connector_exception(exc)
                if mapped is not exc:
                    raise mapped from exc
                raise BackendTransientError("solver backend poll failed") from exc
            remote_status = _backend_value(polled, "status")
            if not isinstance(remote_status, str):
                raise BackendProtocolError("backend poll did not return a status")
            if remote_status in {"accepted", "running"}:
                self.store.update_attempt_status(
                    attempt["attempt_id"],
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    status="running",
                    fencing_token=claimed["fencing_token"],
                )
                progress = _backend_value(polled, "progress")
                payload = {"phase": "solve", "status": remote_status}
                if isinstance(progress, Mapping):
                    payload["progress"] = dict(progress)
                self.store.append_event(
                    job_id,
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    attempt_id=attempt["attempt_id"],
                    event_type="solver.progress",
                    payload=payload,
                )
                current = self._require_job(job_id, workspace_id=workspace_id, owner_id=owner_id)
                return self._outcome(
                    current,
                    self._require_attempt(
                        attempt["attempt_id"],
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                    ),
                    None,
                )
            if remote_status in {
                "completed",
                "completed_feasible",
                "completed_optimal",
                "completed_infeasible",
            }:
                restore_context = getattr(self.backend, "restore_job_context", None)
                if callable(restore_context):
                    restore_context(
                        job_id=attempt["backend_job_id"],
                        idempotency_key=attempt["backend_idempotency_key"],
                        problem_id=claimed["problem_digest"],
                    )
                solver_result = self._backend_call(
                    "result",
                    job_id=attempt["backend_job_id"],
                    binding=self._binding_configuration(job=claimed, workspace_id=workspace_id, owner_id=owner_id),
                )
                if not isinstance(solver_result, SolverResult):
                    raise BackendProtocolError("backend result is not SolverResult")
                stored_attempt = self.store.record_attempt_result(
                    attempt["attempt_id"],
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    result=solver_result.to_dict(),
                    fencing_token=claimed["fencing_token"],
                )
                completed, _ = self._transition_job(
                    job_id,
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    status="solver_completed",
                    phase="verify",
                    fencing_token=claimed["fencing_token"],
                    attempt_id=attempt["attempt_id"],
                    event_type="solver.backend.completed",
                )
                return self._outcome(completed, stored_attempt, None)
            terminal_map = {
                "timed_out": ("timed_out", "timed_out_no_solution"),
                "failed": ("failed", "solver_unavailable"),
                "cancelled": ("cancelled", "cancelled"),
                "attempt_lost": ("failed", "attempt_lost"),
            }
            if remote_status in terminal_map:
                consumer_status, error_code = terminal_map[remote_status]
                self.store.update_attempt_status(
                    attempt["attempt_id"],
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    status=consumer_status,
                    error_code=error_code,
                    fencing_token=claimed["fencing_token"],
                )
                terminal, _ = self._transition_job(
                    job_id,
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    status=consumer_status,
                    phase="solve",
                    error_code=error_code,
                    fencing_token=claimed["fencing_token"],
                    attempt_id=attempt["attempt_id"],
                    event_type="solver.job.{}".format(consumer_status),
                )
                return self._outcome(
                    terminal,
                    self._require_attempt(
                        attempt["attempt_id"],
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                    ),
                    None,
                )
            raise BackendProtocolError("backend returned an unsupported status")
        except LeaseConflict:
            current = self._require_job(job_id, workspace_id=workspace_id, owner_id=owner_id)
            return self._outcome(
                current,
                self.store.latest_attempt(job_id, workspace_id=workspace_id, owner_id=owner_id),
                None,
            )
        except BackendTransientError:
            current = self._require_job(job_id, workspace_id=workspace_id, owner_id=owner_id)
            if current.get("lease_owner") != poll_lease_owner or current.get("fencing_token") != claimed.get(
                "fencing_token"
            ):
                raise LeaseConflict("solver poll lease is stale")
            current_attempt = self.store.latest_attempt(job_id, workspace_id=workspace_id, owner_id=owner_id)
            try:
                self.store.append_event(
                    job_id,
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    attempt_id=(current_attempt or {}).get("attempt_id"),
                    event_type="solver.progress",
                    payload={
                        "phase": "solve",
                        "status": "retrying",
                        "code": "solver_unavailable",
                    },
                )
            except JobStoreError:
                logger.warning("Failed to persist a solver retry progress event")
            return self._outcome(current, current_attempt, None)
        except Exception:
            current = self._require_job(job_id, workspace_id=workspace_id, owner_id=owner_id)
            if current["status"] == "cancelling":
                return self._finish_cancellation(current, workspace_id=workspace_id, owner_id=owner_id)
            return self._mark_recovery_failed(
                job_id,
                workspace_id=workspace_id,
                owner_id=owner_id,
                expected_version=current["version"],
                fencing_token=claimed["fencing_token"],
                lease_owner=poll_lease_owner,
            )
        finally:
            current = self.store.get_job(job_id, workspace_id=workspace_id, owner_id=owner_id)
            if current is not None and current["lease_owner"] == poll_lease_owner:
                try:
                    self.store.release_job_lease(
                        job_id,
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                        lease_owner=poll_lease_owner,
                        fencing_token=claimed["fencing_token"],
                    )
                except LeaseConflict:
                    logger.debug("Poll lease was already released or superseded")

    def cancel_subscription(
        self,
        subscription_id: str,
        *,
        workspace_id: str,
        owner_id: str,
    ) -> dict[str, Any]:
        cancellation = self.store.cancel_subscription(subscription_id, workspace_id=workspace_id, owner_id=owner_id)
        job = self._require_job(cancellation["job_id"], workspace_id=workspace_id, owner_id=owner_id)
        if cancellation["cancel_reserved"]:
            outcome = self._finish_cancellation(job, workspace_id=workspace_id, owner_id=owner_id)
            job = self._require_job(outcome["job_id"], workspace_id=workspace_id, owner_id=owner_id)
        cancellation["job_status"] = job["status"]
        return cancellation

    def _finish_cancellation(
        self,
        job: Mapping[str, Any],
        *,
        workspace_id: str,
        owner_id: str,
    ) -> dict[str, Any]:
        current = self._require_job(job["job_id"], workspace_id=workspace_id, owner_id=owner_id)
        if current["status"] in TERMINAL_JOB_STATUSES:
            return self._outcome(
                current,
                self.store.latest_attempt(current["job_id"], workspace_id=workspace_id, owner_id=owner_id),
                None,
            )
        if current["status"] != "cancelling":
            raise JobStoreConflict("solver cancellation was not reserved")
        attempt = self.store.latest_attempt(current["job_id"], workspace_id=workspace_id, owner_id=owner_id)
        if current["phase"] in _PRE_SUBMISSION_PHASES and attempt is not None:
            if attempt["backend_job_id"] is None and attempt["status"] not in TERMINAL_ATTEMPT_STATUSES:
                attempt = self.store.update_attempt_status(
                    attempt["attempt_id"],
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    status="cancelled",
                    error_code="cancelled",
                    fencing_token=current["fencing_token"],
                )
        cancellation_lease_owner: str | None = None
        cancellation_fencing_token: int | None = None
        try:
            if (
                attempt is not None
                and attempt["backend_job_id"] is None
                and attempt["status"] not in TERMINAL_ATTEMPT_STATUSES
            ):
                lease_until = current.get("lease_until")
                lease_active = (
                    current.get("lease_owner") is not None
                    and lease_until is not None
                    and _as_utc(lease_until) > self._now()
                )
                if lease_active:
                    # The submit worker still owns the fence. Let it bind the
                    # backend identity and complete cancellation; replaying now
                    # would issue a second submit for the same attempt.
                    return self._outcome(current, attempt, None)

                cancellation_lease_owner = "{}:cancel:{}".format(self.lease_owner, uuid.uuid4().hex)
                try:
                    current = self.store.claim_job(
                        current["job_id"],
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                        lease_owner=cancellation_lease_owner,
                        lease_seconds=self._operation_lease_seconds(),
                        expected_version=current["version"],
                    )
                except (LeaseConflict, OptimisticLockError, TerminalStateError):
                    refreshed = self._require_job(job["job_id"], workspace_id=workspace_id, owner_id=owner_id)
                    return self._outcome(
                        refreshed,
                        self.store.latest_attempt(
                            refreshed["job_id"],
                            workspace_id=workspace_id,
                            owner_id=owner_id,
                        ),
                        None,
                    )
                cancellation_fencing_token = current["fencing_token"]
                attempt = self.store.latest_attempt(current["job_id"], workspace_id=workspace_id, owner_id=owner_id)
                if (
                    attempt is not None
                    and attempt["backend_job_id"] is None
                    and attempt["status"] not in TERMINAL_ATTEMPT_STATUSES
                ):
                    try:
                        submitted = self._submit_backend_with_lease(
                            problem=self._restore_problem(attempt["request_payload"]),
                            idempotency_key=attempt["backend_idempotency_key"],
                            deadline=_as_utc(current["deadline_at"]),
                            job_id=current["job_id"],
                            workspace_id=workspace_id,
                            owner_id=owner_id,
                            lease_owner=cancellation_lease_owner,
                            fencing_token=cancellation_fencing_token,
                            binding=self._binding_configuration(
                                job=current,
                                workspace_id=workspace_id,
                                owner_id=owner_id,
                            ),
                            result_quality=self._run_result_quality(current),
                            traceparent=self._run_traceparent(current),
                        )
                    except TimeoutError:
                        self.store.append_event(
                            current["job_id"],
                            workspace_id=workspace_id,
                            owner_id=owner_id,
                            attempt_id=attempt["attempt_id"],
                            event_type="solver.submit.uncertain",
                            payload={"phase": "submit_job", "retryable": True},
                        )
                        return self._outcome(current, attempt, None)
                    except Exception:
                        try:
                            self.store.append_event(
                                current["job_id"],
                                workspace_id=workspace_id,
                                owner_id=owner_id,
                                attempt_id=attempt["attempt_id"],
                                event_type="solver.submit.uncertain",
                                payload={"phase": "submit_job", "retryable": True},
                            )
                        except Exception:
                            logger.warning(
                                "Failed to persist uncertain solver submission event",
                            )
                        return self._outcome(current, attempt, None)
                    backend_job_id = _backend_value(submitted, "job_id")
                    if not isinstance(backend_job_id, str) or not backend_job_id.strip():
                        raise BackendProtocolError("backend submit replay did not return a job_id during cancellation")
                    attempt = self.store.bind_backend_job(
                        attempt["attempt_id"],
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                        backend_job_id=backend_job_id,
                        fencing_token=cancellation_fencing_token,
                    )

            attempt_fence = (
                cancellation_fencing_token
                if cancellation_fencing_token is not None
                else (current["fencing_token"] if current.get("lease_owner") is not None else None)
            )
            if (
                attempt is not None
                and attempt["backend_job_id"] is not None
                and attempt["status"] not in TERMINAL_ATTEMPT_STATUSES
            ):
                try:
                    self._backend_call(
                        "cancel",
                        job_id=attempt["backend_job_id"],
                        binding=self._binding_configuration(job=current, workspace_id=workspace_id, owner_id=owner_id),
                    )
                except Exception:
                    try:
                        self.store.append_event(
                            current["job_id"],
                            workspace_id=workspace_id,
                            owner_id=owner_id,
                            attempt_id=attempt["attempt_id"],
                            event_type="solver.progress",
                            payload={
                                "phase": "cancel",
                                "status": "retrying",
                                "code": "solver_unavailable",
                            },
                        )
                    except Exception:
                        logger.warning(
                            "Failed to persist solver cancellation retry event",
                        )
                    return self._outcome(current, attempt, None)
                attempt = self.store.update_attempt_status(
                    attempt["attempt_id"],
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    status="cancelled",
                    error_code="cancelled",
                    fencing_token=attempt_fence,
                )
            cancelled = self.store.transition_job_with_event(
                current["job_id"],
                workspace_id=workspace_id,
                owner_id=owner_id,
                expected_version=current["version"],
                fencing_token=current["fencing_token"],
                status="cancelled",
                phase="cancel",
                error_code="cancelled",
                attempt_id=attempt["attempt_id"] if attempt is not None else None,
                event_type="solver.job.cancelled",
                event_payload={"phase": "cancel", "status": "cancelled"},
            )[0]
            return self._outcome(cancelled, attempt, None)
        except (LeaseConflict, OptimisticLockError, TerminalStateError):
            refreshed = self._require_job(current["job_id"], workspace_id=workspace_id, owner_id=owner_id)
            return self._outcome(
                refreshed,
                self.store.latest_attempt(refreshed["job_id"], workspace_id=workspace_id, owner_id=owner_id),
                None,
            )
        finally:
            if cancellation_lease_owner is not None:
                latest = self.store.get_job(current["job_id"], workspace_id=workspace_id, owner_id=owner_id)
                if (
                    latest is not None
                    and latest.get("lease_owner") == cancellation_lease_owner
                    and latest.get("fencing_token") == cancellation_fencing_token
                ):
                    try:
                        self.store.release_job_lease(
                            current["job_id"],
                            workspace_id=workspace_id,
                            owner_id=owner_id,
                            lease_owner=cancellation_lease_owner,
                            fencing_token=cancellation_fencing_token,
                        )
                    except LeaseConflict:
                        logger.debug("Cancellation lease was already released or superseded")

    def recover_once(self, *, workspace_id: str, owner_id: str) -> tuple[dict[str, Any], ...]:
        outcomes: list[dict[str, Any]] = []
        for job in self.store.list_recoverable_jobs(
            workspace_id=workspace_id,
            owner_id=owner_id,
            binding_revision_id=None,
        ):
            try:
                outcomes.append(self.poll_once(job["job_id"], workspace_id=workspace_id, owner_id=owner_id))
            except LeaseConflict:
                continue
            except Exception:
                # A failed worker may only terminalize while holding its original
                # lease and fence. Leave cancellation and racing claims recoverable.
                continue
        return tuple(outcomes)

    def _mark_recovery_failed(
        self,
        job_id: str,
        *,
        workspace_id: str,
        owner_id: str,
        expected_version: int,
        fencing_token: int,
        lease_owner: str,
    ) -> dict[str, Any]:
        attempt = self.store.latest_attempt(job_id, workspace_id=workspace_id, owner_id=owner_id)
        current = self._require_job(job_id, workspace_id=workspace_id, owner_id=owner_id)
        failed, failed_attempt, _ = self.store.fail_job_and_attempt_with_event(
            job_id,
            workspace_id=workspace_id,
            owner_id=owner_id,
            expected_version=expected_version,
            fencing_token=fencing_token,
            lease_owner=lease_owner,
            phase=current["phase"],
            attempt_id=attempt["attempt_id"] if attempt is not None else None,
            error_code="solver_unavailable",
            event_type="solver.job.failed",
            event_payload={"phase": current["phase"], "status": "failed"},
        )
        return self._outcome(failed, failed_attempt, None)

    def _require_job(self, job_id: str, *, workspace_id: str, owner_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id, workspace_id=workspace_id, owner_id=owner_id)
        if job is None:
            raise JobOrchestrationError("solver job was not found")
        return job

    def _require_attempt(self, attempt_id: str, *, workspace_id: str, owner_id: str) -> dict[str, Any]:
        attempt = self.store.get_attempt(attempt_id, workspace_id=workspace_id, owner_id=owner_id)
        if attempt is None:
            raise JobOrchestrationError("solver attempt was not found")
        return attempt

    @staticmethod
    def _subscription_outcome(subscription_id: str, job: Mapping[str, Any]) -> dict[str, Any]:
        return {"subscription_id": subscription_id, "job_id": job["job_id"]}

    @staticmethod
    def _outcome(
        job: Mapping[str, Any],
        attempt: Mapping[str, Any] | None,
        subscription: Mapping[str, Any] | None,
        *,
        link: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = {
            "job_id": job["job_id"],
            "logical_job_key": job["logical_job_key"],
            "status": job["status"],
            "phase": job["phase"],
            "error_code": job.get("error_code"),
        }
        if attempt is not None:
            result.update(
                {
                    "attempt_id": attempt["attempt_id"],
                    "attempt_number": attempt["attempt_number"],
                    "backend_job_id": attempt.get("backend_job_id"),
                }
            )
        if subscription is not None:
            result["subscription_id"] = subscription["subscription_id"]
        if link is not None:
            result["tool_call_id"] = link["tool_call_id"]
        return result


JobOrchestrator = DurableJobOrchestrator
