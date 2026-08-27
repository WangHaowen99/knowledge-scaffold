"""Consumer adapter for the ``ortools.solver.v1`` service DTOs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import logging
import os
import re
from pathlib import Path
from functools import lru_cache
from typing import Any

from solver_coordination.contracts import ProblemIR, SolverResult

from .base import SolverConnector
from .errors import (
    ConnectorContractError,
    ConnectorResultError,
)
from .http_transport import HttpSolverTransport
from .helpers import capability_fingerprint


logger = logging.getLogger(__name__)

API_VERSION = "ortools.solver.v1"
PROBLEM_TYPE = "binary_ordered_route"
PROBLEM_VERSION = "1.0"
_JOB_RE = re.compile(r"^job_[A-Za-z0-9_-]{8,128}$")
_REQUEST_RE = re.compile(r"^req_[A-Za-z0-9_-]{8,96}$")
_BUILD_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_INTERNAL_FAMILY = "binary_ordered_route.v1"
_SUPPORTED_INTERNAL_FAMILIES = {"candidate_selection.v1", _INTERNAL_FAMILY}
DOMAIN_EVALUATION_CAPABILITY = "domain_candidate_evaluation"
ORTOOLS_CONNECTOR_CODE_BUILD = "sha256:" + hashlib.sha256(
    b"consumer-ortools-connector-adapter:ortools.solver.v1"
).hexdigest()


def _text(value: Any, field: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ConnectorContractError(f"connector {field} is invalid")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise ConnectorContractError(f"connector {field} is invalid")
    return value


def _strict_document(name: str, payload: Any) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise ConnectorContractError(f"connector {name} response is invalid")
    try:
        _service_validator().validate_document(name, payload)
    except Exception as exc:
        raise ConnectorContractError(f"connector {name} response is invalid") from exc
    return payload


def _supported_family(families: Any) -> Mapping[str, Any] | None:
    for family in families or ():
        if not isinstance(family, Mapping):
            continue
        if family.get("name") != PROBLEM_TYPE:
            continue
        if family.get("versions") != [PROBLEM_VERSION]:
            continue
        return family
    return None


@lru_cache(maxsize=1)
def _service_validator() -> Any:
    """Load the standalone service contract from source or image layouts."""

    try:
        from contracts.solver_services.ortools import validation as module

        return module
    except ImportError:
        logger.debug(
            "Installed OR-Tools contract module is unavailable; trying artifact paths"
        )
    candidates: list[Path] = []
    configured = os.environ.get("SOLVER_CONTRACT_ROOT")
    if configured:
        candidates.append(Path(configured).expanduser() / "solver_services" / "ortools" / "validation.py")
        candidates.append(Path(configured).expanduser() / "ortools" / "validation.py")
    current = Path(__file__).resolve()
    for parent in current.parents:
        candidates.append(parent / "contracts" / "solver_services" / "ortools" / "validation.py")
    candidates.append(Path("/app/contracts/solver_services/ortools/validation.py"))
    for path in candidates:
        if not path.is_file():
            continue
        spec = importlib.util.spec_from_file_location(
            "solver_service_ortools_contract_validation", path
        )
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    raise RuntimeError("ortools.solver.v1 contract artifact is unavailable")


def _request_id(idempotency_key: str) -> str:
    if not isinstance(idempotency_key, str) or not idempotency_key.startswith("hmac-sha256:"):
        raise ConnectorContractError("idempotency key is invalid")
    return "req_" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:40]


def _connector_build(service_capabilities: Mapping[str, Any]) -> str:
    """Fingerprint adapter code together with the canonical remote descriptor."""

    canonical = json.dumps(
        _thaw(service_capabilities),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(
        ORTOOLS_CONNECTOR_CODE_BUILD.encode("ascii") + b"\0" + canonical
    ).hexdigest()


def _problem_data(problem: ProblemIR) -> dict[str, Any]:
    if not isinstance(problem, ProblemIR) or problem.problem_family not in _SUPPORTED_INTERNAL_FAMILIES:
        raise ConnectorContractError("problem family is not supported by the OR-Tools service")
    return {
        "decision_space": _thaw(problem.decision_space),
        "parameters": _thaw(problem.parameters),
        "constraints": _thaw(problem.constraints),
        "objectives": _thaw(problem.objectives),
        "limits": _thaw(problem.limits),
    }


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw(item) for item in value]
    return value


def _remaining_ms(deadline: datetime) -> int:
    if not isinstance(deadline, datetime) or deadline.tzinfo is None:
        raise ConnectorContractError("deadline must be timezone-aware")
    return max(
        1,
        int(
            (deadline.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()
            * 1000
        ),
    )


class OrToolsConnector:
    """Map platform solver operations to the dedicated OR-Tools API."""

    connector_type = "ortools"

    def __init__(self, *, transport: HttpSolverTransport) -> None:
        if not isinstance(transport, HttpSolverTransport) and not callable(
            getattr(transport, "request", None)
        ):
            raise TypeError("transport must implement request")
        self.transport = transport
        self._job_context: dict[str, dict[str, str]] = {}
        self._binding_by_job: dict[str, Mapping[str, Any]] = {}
        self._capabilities_by_binding: dict[str, Mapping[str, Any]] = {}
        self._validated_capability_bindings: set[str] = set()

    @staticmethod
    def request_id(idempotency_key: str) -> str:
        return _request_id(idempotency_key)

    @staticmethod
    def _binding_key(binding: Mapping[str, Any]) -> str:
        return json.dumps(
            {
                "endpoint": binding.get("endpoint"),
                "capability_fingerprint": binding.get("capability_fingerprint"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _job(payload: Mapping[str, Any], *, expected_request_id: str | None = None) -> dict[str, Any]:
        _strict_document("job", payload)
        if payload["api_version"] != API_VERSION:
            raise ConnectorContractError("connector job api_version is invalid")
        _text(payload["job_id"], "job_id", _JOB_RE)
        _text(payload["request_id"], "request_id", _REQUEST_RE)
        if expected_request_id is not None and payload["request_id"] != expected_request_id:
            raise ConnectorContractError("connector job request_id does not match")
        if payload["problem_type"] != PROBLEM_TYPE or payload["problem_version"] != PROBLEM_VERSION:
            raise ConnectorContractError("connector job problem identity does not match")
        return {"job_id": payload["job_id"], "status": payload["status"]}

    def capabilities(self, *, binding: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        if binding is None:
            return {
                "protocol_versions": [API_VERSION],
                "transport": "http-json",
                "connector": {"name": "ortools", "build": ORTOOLS_CONNECTOR_CODE_BUILD},
            }
        payload = _strict_document(
            "capabilities",
            self.transport.request(
                method="GET", binding=binding, path="/api/v1/capabilities"
            ),
        )
        service = payload["service"]
        engine = payload["engine"]
        families = []
        for family in payload["problem_types"]:
            families.append(
                {
                    "name": family["name"],
                    "versions": list(family["versions"]),
                    "features": list(family["features"]),
                    "result_assurances": list(family["result_assurances"]),
                    "limits": {
                        "max_payload_bytes": family["limits"]["max_payload_bytes"],
                        "max_solve_time_ms": family["limits"]["max_solve_time_ms"],
                        "max_workers": family["limits"]["max_search_workers"],
                    },
                }
            )
        result = {
            "protocol_versions": [API_VERSION],
            "connector": {
                "name": "ortools",
                "version": service["version"],
                "build": _connector_build(payload),
            },
            "service": {
                "name": service["name"],
                "version": service["version"],
                "build": service["build"],
            },
            "engine": {
                "name": engine["name"],
                "version": engine["version"],
            },
            "execution": {
                "async_jobs": payload["execution"]["async_jobs"],
                "cancellation": payload["execution"]["cancellation"],
                "prefer_wait": payload["execution"]["prefer_wait"],
            },
            "problem_families": families,
        }
        self._capabilities_by_binding[self._binding_key(binding)] = result
        return result

    @staticmethod
    def _validate_binding_limits(binding: Mapping[str, Any], capabilities: Mapping[str, Any]) -> None:
        raw = binding.get("neutral_limits")
        if not isinstance(raw, Mapping):
            raise ConnectorContractError("connector binding neutral limits are missing")
        families = capabilities.get("problem_families")
        family = _supported_family(families)
        if not isinstance(family, Mapping) or not isinstance(family.get("limits"), Mapping):
            raise ConnectorContractError("connector capabilities have no supported problem family")
        limits = family["limits"]
        for name in ("max_payload_bytes", "max_solve_time_ms", "max_workers"):
            value = raw.get(name)
            ceiling = limits.get(name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConnectorContractError("binding neutral limits exceed Connector capability")
            if not isinstance(ceiling, int) or value > ceiling:
                raise ConnectorContractError("binding neutral limits exceed Connector capability")

    @staticmethod
    def _family_features(capabilities: Mapping[str, Any]) -> set[str]:
        family = _supported_family(capabilities.get("problem_families", ()))
        if not isinstance(family, Mapping):
            return set()
        return {
            feature
            for feature in family.get("features", ())
            if isinstance(feature, str) and feature
        }

    def assert_binding_capabilities(
        self,
        binding: Mapping[str, Any],
        *,
        required_capabilities: Sequence[str] = (),
    ) -> None:
        live = self.capabilities(binding=binding)
        expected = binding.get("capability_fingerprint")
        if expected is not None:
            if capability_fingerprint(live) != expected:
                raise ValueError("solver capability drift detected")
        self._validate_binding_limits(binding, live)
        missing = set(required_capabilities) - self._family_features(live)
        if missing:
            raise ConnectorContractError(
                "Connector does not support required capabilities: {}".format(
                    ", ".join(sorted(missing))
                )
            )
        self._validated_capability_bindings.add(self._binding_key(binding))

    def _assert_problem_capabilities(
        self, *, binding: Mapping[str, Any], problem: ProblemIR
    ) -> None:
        parameters = problem.parameters
        if not isinstance(parameters, Mapping) or "domain_evaluation" not in parameters:
            return
        key = self._binding_key(binding)
        if key not in self._validated_capability_bindings:
            self.assert_binding_capabilities(
                binding,
                required_capabilities=(DOMAIN_EVALUATION_CAPABILITY,),
            )
            return
        live = self._capabilities_by_binding.get(key, {})
        if DOMAIN_EVALUATION_CAPABILITY not in self._family_features(live):
            raise ConnectorContractError(
                "Connector does not support required capabilities: {}".format(
                    DOMAIN_EVALUATION_CAPABILITY
                )
            )

    def submit(
        self,
        *,
        binding: Mapping[str, Any],
        problem: ProblemIR,
        idempotency_key: str,
        deadline: datetime,
        result_quality: str = "optimal",
        traceparent: str | None = None,
    ) -> Mapping[str, Any]:
        if result_quality not in {"optimal", "feasible", "any"}:
            raise ConnectorContractError("result quality is invalid")
        self._assert_problem_capabilities(binding=binding, problem=problem)
        allowed = binding.get("allowed_families")
        if not isinstance(allowed, Mapping) or PROBLEM_TYPE not in allowed or PROBLEM_VERSION not in allowed[PROBLEM_TYPE]:
            raise ConnectorContractError("problem family is not allowed by the binding")
        request_id = _request_id(idempotency_key)
        raw_limits = binding.get("neutral_limits")
        if not isinstance(raw_limits, Mapping):
            raise ConnectorContractError("connector binding neutral limits are missing")
        payload = {
            "api_version": API_VERSION,
            "request_id": request_id,
            "problem": {
                "type": PROBLEM_TYPE,
                "version": PROBLEM_VERSION,
                "data": _problem_data(problem),
            },
            "options": {
                "time_limit_ms": min(_remaining_ms(deadline), int(raw_limits["max_solve_time_ms"])),
                "search_workers": 1,
            },
        }
        try:
            _service_validator().validate_document("submit-request", payload)
        except Exception as exc:
            raise ConnectorContractError("connector submit request is invalid") from exc
        response = self.transport.request(
            method="POST",
            binding=binding,
            path="/api/v1/jobs",
            json_payload=payload,
            idempotency_key=idempotency_key,
            traceparent=traceparent,
        )
        job = self._job(
            _strict_document("job", response),
            expected_request_id=request_id,
        )
        self._job_context[job["job_id"]] = {
            "request_id": request_id,
            "problem_id": problem.problem_id,
            "problem_type": PROBLEM_TYPE,
            "problem_version": PROBLEM_VERSION,
        }
        self._binding_by_job[job["job_id"]] = dict(binding)
        return job

    def _assert_frozen_binding(self, *, binding: Mapping[str, Any], job_id: str) -> None:
        frozen = self._binding_by_job.get(job_id)
        if frozen is None:
            self._binding_by_job[job_id] = dict(binding)
            return
        if (
            frozen.get("endpoint") != binding.get("endpoint")
            or frozen.get("capability_fingerprint") != binding.get("capability_fingerprint")
        ):
            raise ConnectorContractError("Connector binding changed for an existing job")

    def poll(self, *, binding: Mapping[str, Any], job_id: str) -> Mapping[str, Any]:
        _text(job_id, "job_id", _JOB_RE)
        self._assert_frozen_binding(binding=binding, job_id=job_id)
        context = self._job_context.get(job_id)
        payload = self.transport.request(
            method="GET", binding=binding, path=f"/api/v1/jobs/{job_id}"
        )
        return self._job(_strict_document("job", payload), expected_request_id=context["request_id"] if context else None)

    def cancel(self, *, binding: Mapping[str, Any], job_id: str) -> Mapping[str, Any]:
        _text(job_id, "job_id", _JOB_RE)
        self._assert_frozen_binding(binding=binding, job_id=job_id)
        context = self._job_context.get(job_id)
        payload = self.transport.request(
            method="DELETE", binding=binding, path=f"/api/v1/jobs/{job_id}"
        )
        return self._job(_strict_document("job", payload), expected_request_id=context["request_id"] if context else None)

    def result(self, *, binding: Mapping[str, Any], job_id: str) -> SolverResult:
        _text(job_id, "job_id", _JOB_RE)
        self._assert_frozen_binding(binding=binding, job_id=job_id)
        context = self._job_context.get(job_id)
        if context is None:
            raise ConnectorResultError("connector job context is unavailable")
        payload = self.transport.request(
            method="GET", binding=binding, path=f"/api/v1/jobs/{job_id}/result"
        )
        try:
            payload = dict(_strict_document("result", payload))
            if payload["job_id"] != job_id:
                raise ValueError("result correlation mismatch")
            if payload["request_id"] != context["request_id"]:
                raise ValueError("result correlation mismatch")
            if payload["problem_type"] != context["problem_type"]:
                raise ValueError("result correlation mismatch")
            if payload["problem_version"] != context["problem_version"]:
                raise ValueError("result correlation mismatch")
            engine = payload["engine"]
            if not _BUILD_RE.fullmatch(engine["service_build"]):
                raise ValueError("result service build is invalid")
            return SolverResult(
                problem_id=context["problem_id"],
                status=payload["solve_outcome"],
                assignment=payload["assignments"],
                objective_values=payload["objective_values"],
                diagnostics=(
                    {
                        "kind": "connector_assurance",
                        **dict(payload["assurance"]),
                    },
                ),
                engine={
                    "name": engine["name"],
                    "version": engine["version"],
                    "connector_build": ORTOOLS_CONNECTOR_CODE_BUILD,
                    "service_build": engine["service_build"],
                },
            )
        except Exception as exc:
            if isinstance(exc, ConnectorResultError):
                raise
            raise ConnectorResultError() from exc

    def restore_job_context(
        self, *, job_id: str, idempotency_key: str, problem_id: str
    ) -> None:
        if job_id not in self._job_context:
            self._job_context[job_id] = {
                "request_id": _request_id(idempotency_key),
                "problem_id": problem_id,
                "problem_type": PROBLEM_TYPE,
                "problem_version": PROBLEM_VERSION,
            }


__all__ = [
    "API_VERSION",
    "DOMAIN_EVALUATION_CAPABILITY",
    "ORTOOLS_CONNECTOR_CODE_BUILD",
    "OrToolsConnector",
]
