"""Application adapters and assembly for the solver-coordination core."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import logging
import os
from pathlib import Path
import secrets
import threading
from typing import Any

import yaml

from ontology.ontology_catalog import get_ontology_manifest
from ontology.ontologies import validate_no_runtime_binding_data
from integration.solver_fact_providers import build_ordered_route_fact_provider
from solver_coordination.bindings import DurableSolverBindingResolver
from solver_coordination.canonical import sha256_digest
from solver_coordination.candidate_results import build_candidate_results
from solver_coordination.capability_catalog import (
    SolverCapabilityCatalogError,
    load_solver_capability_catalog,
)
from solver_coordination.compiler import ProblemIRCompiler
from solver_coordination.connectors import (
    EndpointPolicy,
    HttpSolverTransport,
    OrToolsConnector,
)
from solver_coordination.contracts import (
    MAX_PLAN_ITEMS,
    NormalizedInput,
    Option,
    SolverResult,
    VerificationResult,
)
from solver_coordination.errors import VerificationDivergenceError
from solver_coordination.fact_snapshot import FactSnapshotBuilder, FactSnapshotInvalidError
from solver_coordination.input_binding import SolverInputBinder
from solver_coordination.job_orchestrator import DurableJobOrchestrator
from solver_coordination.job_store import SolverJobStore
from solver_coordination.problem_families.binary_ordered_route import (
    BinaryOrderedRouteAdapter,
)
from solver_coordination.problem_families.candidate_selection import (
    CandidateSelectionAdapter,
)
from solver_coordination.problem_families.registry import ProblemFamilyRegistry
from solver_coordination.presentation import ProfilePresentationService
from solver_coordination.profiles import (
    ProfileFactResolver,
    ProfileInputAdapterService,
    build_default_solver_profile_registry,
)
from solver_coordination.result_projection import AuthoritativeResultProjector
from solver_coordination.service import SolverCoordinationService
from solver_coordination.verification.profile_dispatch import ProfileDispatchVerifier
from solver_coordination.verification.service import (
    ContentAddressedSolverArtifactStore,
    IndependentVerificationService,
)


logger = logging.getLogger(__name__)


class RuntimeSolverCapabilityResolver:
    """Resolve one exact, enabled package revision and load its locked model."""

    def resolve(
        self,
        *,
        workspace_id: str,
        package_id: str,
        package_revision: str,
        operation_key: str,
        **_identity: Any,
    ) -> Mapping[str, Any]:
        ontology, error = get_ontology_manifest(
            package_id,
            version=package_revision,
            workspace_id=workspace_id,
        )
        if error or ontology is None or ontology.version != package_revision:
            raise SolverCapabilityCatalogError("locked solver package was not found")
        root = Path(ontology.ontology_dir).resolve(strict=True)
        manifest_path = root / "manifest.yaml"
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise SolverCapabilityCatalogError("locked solver manifest is unavailable")
        try:
            manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise SolverCapabilityCatalogError("locked solver manifest is malformed") from exc
        if not isinstance(manifest, Mapping):
            raise SolverCapabilityCatalogError("locked solver manifest is malformed")
        records = load_solver_capability_catalog(
            root, manifest, package_revision=package_revision
        )
        matches = []
        for record in records:
            if record.package_id != package_id:
                continue
            if record.package_revision != package_revision:
                continue
            if record.operation_key != operation_key or not record.enabled:
                continue
            matches.append(record)
        if len(matches) != 1:
            raise SolverCapabilityCatalogError(
                "solver operation does not resolve to one locked capability"
            )
        record = matches[0]
        model = self._load_model(root=root, manifest=manifest, record=record)
        return {
            "package_id": record.package_id,
            "package_revision": record.package_revision,
            "operation_key": record.operation_key,
            "decision_model_id": record.decision_model_id,
            "problem_family": record.problem_family,
            "input_schema": self._thaw(record.input_schema),
            "result_schema": self._thaw(record.result_schema),
            "contract_digest": record.contract_digest,
            "validator_revision": record.validator_revision,
            "enabled": record.enabled,
            "package_digest": record.package_digest,
            "operation_digest": record.operation_digest,
            "decision_model_digest": sha256_digest(model),
            "input_schema_digest": record.input_schema_digest,
            "result_schema_digest": record.result_schema_digest,
            "fact_snapshot_schema_digest": record.fact_snapshot_schema_digest,
            "engine_capabilities": list(record.engine_capabilities),
            "decision_model": model,
        }

    @classmethod
    def _load_model(cls, *, root: Path, manifest: Mapping[str, Any], record: Any) -> dict[str, Any]:
        capabilities = manifest.get("capabilities")
        declarations = (
            capabilities.get("decision_models")
            if isinstance(capabilities, Mapping)
            else None
        )
        if not isinstance(declarations, list):
            raise SolverCapabilityCatalogError("decision model declaration is missing")
        matches = []
        for value in declarations:
            if not isinstance(value, Mapping):
                continue
            if str(value.get("id") or "").strip() == record.decision_model_id:
                matches.append(value)
        if len(matches) != 1:
            raise SolverCapabilityCatalogError("decision model identity is ambiguous")
        model_path = cls._contained_file(root, matches[0].get("entry"))
        raw_digest = sha256_digest(
            {"sha256": hashlib.sha256(model_path.read_bytes()).hexdigest()}
        )
        try:
            model = yaml.safe_load(model_path.read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise SolverCapabilityCatalogError("decision model is malformed") from exc
        if not isinstance(model, dict):
            raise SolverCapabilityCatalogError("decision model must be an object")
        validate_no_runtime_binding_data(model)
        canonical_digest = sha256_digest(model)
        if record.decision_model_digest not in {raw_digest, canonical_digest}:
            raise SolverCapabilityCatalogError("decision model digest changed")
        if str(model.get("id") or record.decision_model_id).strip() != record.decision_model_id:
            raise SolverCapabilityCatalogError("decision model id changed")
        return model

    @staticmethod
    def _contained_file(root: Path, value: Any) -> Path:
        raw = str(value or "").strip()
        relative = Path(raw)
        if not raw or relative.is_absolute() or ".." in relative.parts:
            raise SolverCapabilityCatalogError("decision model path is unsafe")
        cursor = root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise SolverCapabilityCatalogError("decision model path is unsafe")
        try:
            resolved = (root / relative).resolve(strict=True)
        except OSError as exc:
            raise SolverCapabilityCatalogError("decision model is missing") from exc
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise SolverCapabilityCatalogError("decision model path is unsafe")
        return resolved

    @classmethod
    def _thaw(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(key): cls._thaw(item) for key, item in value.items()}
        if isinstance(value, tuple):
            return [cls._thaw(item) for item in value]
        return value


class PlatformFactResolver:
    """Fail closed until a package's required facts have a platform provider."""

    def __init__(self, *, binding_revision_id: str | None) -> None:
        if binding_revision_id is not None and not str(binding_revision_id).strip():
            raise ValueError("binding_revision_id must be non-empty or None")
        self.binding_revision_id = (
            str(binding_revision_id).strip()
            if binding_revision_id is not None
            else None
        )

    def resolve(
        self,
        *,
        required_facts: tuple[str, ...],
        normalized_input: NormalizedInput,
        **_identity: Any,
    ) -> Mapping[str, Any]:
        if required_facts:
            raise FactSnapshotInvalidError(
                "required facts have no configured platform snapshot provider",
                details=(
                    {"code": "missing", "facts": list(required_facts)},
                ),
            )
        return {
            "facts": [],
            "bindings": (
                {"solver_binding_revision": self.binding_revision_id}
                if self.binding_revision_id is not None
                else {}
            ),
            "as_of": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "source_evidence": tuple(normalized_input.source_evidence),
        }


class _TestOnlyCompatibilityBindingResolver:
    """Explicit fixture-only binding; never selected by production assembly."""

    binding_revision_id = "test-only-compatibility-binding"

    def __init__(self) -> None:
        self.configuration: Mapping[str, Any] | None = None

    def resolve(self, **_identity: Any) -> Mapping[str, Any]:
        if self.configuration is None:
            raise RuntimeError("test-only compatibility binding is not configured")
        return {
            "binding_revision_id": self.binding_revision_id,
            "configuration": dict(self.configuration),
        }


class _TestOnlyCompatibilityAdapter:
    """Mark the legacy binary fixture explicitly for the test-only worker path."""

    family_key = "synthetic_choice.v1"
    problem_family = family_key

    def __init__(self) -> None:
        self._delegate = BinaryOrderedRouteAdapter()

    def compile_problem(self, **kwargs: Any) -> Mapping[str, Any]:
        compiled = dict(self._delegate.compile_problem(**kwargs))
        parameters = dict(compiled.get("parameters") or {})
        if parameters.get("compatibility_mode") != "test_only_first_domain.v1":
            raise ValueError("test compatibility mode is not explicitly declared")
        compiled["parameters"] = parameters
        return compiled

    def enumerate(self, **kwargs: Any) -> Any:
        return self._delegate.enumerate(**kwargs)


class BoundedIndependentVerifier:
    """Independently verify the deliberately small first problem-family slice."""

    def verify(
        self,
        *,
        problem: Any = None,
        capability: Any,
        normalized_input: NormalizedInput,
        fact_snapshot: Any,
        solver_result: SolverResult,
    ) -> VerificationResult:
        del problem, normalized_input, fact_snapshot
        model = self._value(capability, "decision_model")
        if not isinstance(model, Mapping):
            raise VerificationDivergenceError("locked verifier model is missing")
        problem_family = self._value(capability, "problem_family")
        if problem_family == "synthetic_choice.v1":
            parameters = model.get("parameters")
            if (
                not isinstance(parameters, Mapping)
                or parameters.get("compatibility_mode")
                != "test_only_first_domain.v1"
            ):
                raise VerificationDivergenceError(
                    "test compatibility mode is not explicitly declared"
                )
        elif problem_family != "binary_ordered_route.v1":
            raise VerificationDivergenceError(
                "problem family has no independent verifier"
            )
        objectives = model.get("objectives") or []
        if objectives or solver_result.objective_values:
            raise VerificationDivergenceError(
                "objective values have no independent recalculation adapter"
            )
        variable_names, domains = self._domains(model.get("decision_space"))
        assignment = dict(solver_result.assignment)
        if set(assignment) != set(variable_names):
            raise VerificationDivergenceError("solver assignment variables diverged")
        for name in variable_names:
            if assignment[name] not in domains[name]:
                raise VerificationDivergenceError("solver assignment leaves its domain")
        checked_constraints = self._check_constraints(
            model.get("constraints") or (), variable_names, assignment
        )
        return VerificationResult(
            problem_id=solver_result.problem_id,
            status="verified",
            feasibility="feasible",
            optimality="not_proven",
            proof_kind="independent_domain_constraint_check",
            evidence=(
                {
                    "kind": "bounded_independent_recalculation",
                    "checked_variables": len(variable_names),
                    "checked_constraints": checked_constraints,
                },
            ),
        )

    @staticmethod
    def _domains(raw: Any) -> tuple[tuple[str, ...], dict[str, tuple[Any, ...]]]:
        if not isinstance(raw, Mapping):
            raise VerificationDivergenceError("decision space is invalid")
        keys = set(raw)
        if keys == {"choices"}:
            return ("choice",), {"choice": BoundedIndependentVerifier._domain(
                raw["choices"], "decision space choices"
            )}
        if "variables" not in raw:
            raise VerificationDivergenceError(
                "decision space requires choices or non-empty variables"
            )
        variables = raw["variables"]
        if not isinstance(variables, (list, tuple)) or not variables:
            raise VerificationDivergenceError(
                "decision variables must be a non-empty array"
            )
        if len(variables) > MAX_PLAN_ITEMS:
            raise VerificationDivergenceError("decision variables exceed the ordered route limit")
        if all(isinstance(item, str) for item in variables):
            names = BoundedIndependentVerifier._variable_names(variables)
            if keys == {"variables", "option_domain"}:
                domain = BoundedIndependentVerifier._domain(
                    raw["option_domain"], "decision space option_domain"
                )
                return names, {name: domain for name in names}
            if keys == {"variables", "domains"}:
                declared = raw["domains"]
                if not isinstance(declared, Mapping) or set(declared) != set(names):
                    raise VerificationDivergenceError(
                        "decision space domains must match every variable exactly"
                    )
                return names, {
                    name: BoundedIndependentVerifier._domain(
                        declared[name], "decision space domain for {}".format(name)
                    )
                    for name in names
                }
            raise VerificationDivergenceError(
                "decision variables require option_domain or domains"
            )
        if all(isinstance(item, Mapping) for item in variables):
            if keys != {"variables"}:
                raise VerificationDivergenceError(
                    "inline decision variables cannot use shared domains"
                )
            names: list[str] = []
            domains: dict[str, tuple[Any, ...]] = {}
            for item in variables:
                if set(item) != {"name", "domain"}:
                    raise VerificationDivergenceError(
                        "inline decision variable fields are invalid"
                    )
                name = item.get("name")
                if not isinstance(name, str) or not name or name != name.strip():
                    raise VerificationDivergenceError("decision variable name is invalid")
                names.append(name)
                domains[name] = BoundedIndependentVerifier._domain(
                    item.get("domain"), "decision space domain for {}".format(name)
                )
            names_tuple = BoundedIndependentVerifier._variable_names(names)
            return names_tuple, domains
        raise VerificationDivergenceError("decision variables are invalid")

    @staticmethod
    def _variable_names(values: tuple[Any, ...] | list[Any]) -> tuple[str, ...]:
        names = tuple(values)
        if any(
            not isinstance(name, str) or not name or name != name.strip()
            for name in names
        ) or len(set(names)) != len(names):
            raise VerificationDivergenceError("decision variables are invalid")
        return names

    @staticmethod
    def _domain(value: Any, field_name: str) -> tuple[Any, ...]:
        expected = (Option.OPTION_0.value, Option.OPTION_1.value)
        if not isinstance(value, (list, tuple)) or tuple(value) != expected:
            raise VerificationDivergenceError(
                "{} must be OPTION_0 and OPTION_1 in that order".format(field_name)
            )
        return expected

    @staticmethod
    def _check_constraints(
        raw: Any, variable_names: tuple[str, ...], assignment: Mapping[str, Any]
    ) -> int:
        if not isinstance(raw, (list, tuple)):
            raise VerificationDivergenceError("constraints are invalid")
        for constraint in raw:
            if not isinstance(constraint, Mapping) or constraint.get("kind") != "max_transitions":
                raise VerificationDivergenceError(
                    "constraint has no independent recalculation adapter"
                )
            maximum = constraint.get("value")
            if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 0:
                raise VerificationDivergenceError("max_transitions is invalid")
            values = [assignment[name] for name in variable_names]
            transitions = sum(
                values[index] != values[index - 1]
                for index in range(1, len(values))
            )
            if transitions > maximum:
                raise VerificationDivergenceError("solver assignment violates max_transitions")
        return len(raw)

    @staticmethod
    def _value(capability: Any, name: str) -> Any:
        if isinstance(capability, Mapping):
            return capability.get(name)
        return getattr(capability, name, None)


def _load_or_create_secret(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0),
            0o600,
        )
    except FileExistsError:
        descriptor = None
    if descriptor is not None:
        try:
            value = secrets.token_bytes(32)
            os.write(descriptor, value)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    try:
        value = path.read_bytes()
    except OSError as exc:
        raise RuntimeError("solver idempotency secret is unavailable") from exc
    if len(value) != 32:
        raise RuntimeError("solver idempotency secret is invalid")
    return value


def recover_solver_coordination_jobs(
    service: SolverCoordinationService,
) -> tuple[dict[str, Any], ...]:
    """Advance every durable Job through verification and ToolResult delivery."""

    outcomes: list[dict[str, Any]] = []
    for workspace_id, owner_id in service.store.list_service_recovery_scopes():
        for job in service.store.list_service_recovery_jobs(
            workspace_id=workspace_id,
            owner_id=owner_id,
        ):
            try:
                outcomes.append(
                    service.recover_job(
                        str(job["job_id"]),
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                    )
                )
            except Exception:
                logger.exception(
                    "Solver recovery tick failed: workspace_id=%s owner_id=%s job_id=%s",
                    workspace_id,
                    owner_id,
                    job.get("job_id"),
                )
    return tuple(outcomes)


class SolverCoordinationRecoveryWorker:
    """Process-local recovery loop for the SQLite/WAL deployment profile."""

    def __init__(
        self,
        service: SolverCoordinationService,
        *,
        interval_seconds: float = 1.0,
    ) -> None:
        if isinstance(interval_seconds, bool) or not isinstance(
            interval_seconds, (int, float)
        ) or interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.service = service
        self.interval_seconds = float(interval_seconds)
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> SolverCoordinationRecoveryWorker:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="solver-coordination-recovery",
                daemon=True,
            )
            self._thread.start()
        return self

    def stop(self, *, timeout_seconds: float = 5.0) -> bool:
        """Request shutdown and report whether the worker actually terminated."""

        if isinstance(timeout_seconds, bool) or not isinstance(
            timeout_seconds, (int, float)
        ) or timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        self._stop_event.set()
        with self._lock:
            thread = self._thread
            if thread is None:
                return True
            if thread is threading.current_thread():
                return False
            thread.join(timeout=float(timeout_seconds))
            terminated = not thread.is_alive()
            if terminated and self._thread is thread:
                self._thread = None
            return terminated

    def tick(self) -> tuple[dict[str, Any], ...]:
        return recover_solver_coordination_jobs(self.service)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("Solver recovery worker tick failed")
            self._stop_event.wait(self.interval_seconds)


def build_solver_connector_credential_resolver() -> Any:
    """Resolve one solver-service credential from process-local secrets.

    Binding revisions contain only an opaque reference.  The corresponding
    secret value is supplied by the deployment environment and is never put
    into a ProblemIR, knowledge package, durable binding row, or log record.
    A reference mismatch (or an unset secret) deliberately resolves to
    ``None`` so the Consumer Connector fails closed at the authentication
    boundary.
    """

    configured_ref = (os.environ.get("SOLVER_SERVICE_CREDENTIAL_REF") or "").strip()
    configured_token = os.environ.get("SOLVER_SERVICE_TOKEN")
    token_file = os.environ.get("SOLVER_SERVICE_TOKEN_FILE")
    if configured_token is not None and token_file is not None:
        raise RuntimeError("solver service secret sources are mutually exclusive")
    if token_file is not None:
        try:
            raw = Path(token_file).read_bytes()
            if raw.startswith(b"\xef\xbb\xbf"):
                raise ValueError
            text = raw.decode("utf-8")
            configured_token = text[:-1] if text.endswith("\n") else text
            if configured_token.endswith("\r"):
                configured_token = configured_token[:-1]
            if len(configured_token) < 16:
                raise ValueError
            if configured_token != configured_token.strip():
                raise ValueError
            if "\n" in configured_token or "\r" in configured_token:
                raise ValueError
        except (OSError, ValueError) as exc:
            raise RuntimeError("solver service secret file is invalid") from exc

    def resolve(reference: str) -> str | None:
        if not configured_ref or not isinstance(reference, str):
            return None
        if reference != configured_ref or not configured_token:
            return None
        return configured_token

    return resolve


def build_solver_connector_tls_ca_bundle() -> str | None:
    """Return a solver-service CA bundle path without changing global TLS.

    The file is supplied by the optional solver deployment overlay.  The base
    Consumer may start without it; a private-CA Solver Service call remains
    impossible until an administrator enables the overlay and registers the
    corresponding HTTPS Binding.
    """

    configured = (os.environ.get("SOLVER_SERVICE_TLS_CA_FILE") or "").strip()
    if not configured:
        return None
    path = Path(configured)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RuntimeError("solver service TLS CA file cannot be read") from exc
    if not raw or len(raw) > 1024 * 1024:
        raise RuntimeError("solver service TLS CA file is invalid")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise RuntimeError("solver service TLS CA file is invalid")
    if b"-----BEGIN CERTIFICATE-----" not in raw:
        raise RuntimeError("solver service TLS CA file is invalid")
    if b"-----END CERTIFICATE-----" not in raw:
        raise RuntimeError("solver service TLS CA file is invalid")
    return str(path)


def build_solver_coordination_service(
    *,
    runtime_root: str | Path,
    test_only_compatibility: bool = False,
    binding_revision_id: str | None = None,
    credential_resolver: Any | None = None,
) -> SolverCoordinationService:
    """Build the one app-scoped service used by Agent Tool and HTTP adapters.

    ``test_only_compatibility`` is an explicit fixture-only escape hatch for the
    pre-profile synthetic-choice regression.  It is never enabled by production
    callers and is intentionally kept separate from the default OR-Tools path.
    """
    if not isinstance(test_only_compatibility, bool):
        raise TypeError("test_only_compatibility must be boolean")

    raw_max_attempts = (os.environ.get("SOLVER_MAX_ATTEMPTS") or "2").strip()
    try:
        max_attempts = int(raw_max_attempts)
    except ValueError as exc:
        raise ValueError("SOLVER_MAX_ATTEMPTS must be an integer") from exc
    if max_attempts < 1 or max_attempts > 16:
        raise ValueError("SOLVER_MAX_ATTEMPTS must be between 1 and 16")

    root = (Path(runtime_root).expanduser().resolve() / "system" / "solver_coordination")
    root.mkdir(parents=True, exist_ok=True)
    profiles = build_default_solver_profile_registry(
        fact_provider=build_ordered_route_fact_provider()
    )
    store = SolverJobStore(root / "jobs.sqlite3")
    selected_binding_revision = (
        binding_revision_id
        or os.environ.get("SOLVER_BINDING_REVISION_ID")
        or "connector-binding-unconfigured"
    )
    endpoint_policy = EndpointPolicy.from_env()
    binding_resolver = DurableSolverBindingResolver(
        store=store,
        revision_id=selected_binding_revision,
        endpoint_policy=endpoint_policy,
    )
    backend = OrToolsConnector(
        transport=HttpSolverTransport(
            credential_resolver=(
                credential_resolver
                if credential_resolver is not None
                else build_solver_connector_credential_resolver()
            ),
            tls_ca_bundle=build_solver_connector_tls_ca_bundle(),
            endpoint_policy=endpoint_policy,
        )
    )
    orchestrator = DurableJobOrchestrator(
        store=store,
        backend=backend,
        idempotency_secret=_load_or_create_secret(root / "idempotency.key"),
        binding_revision_id=None,
    )
    if test_only_compatibility:
        compatibility_binding_resolver = _TestOnlyCompatibilityBindingResolver()
        return SolverCoordinationService(
            capability_resolver=RuntimeSolverCapabilityResolver(),
            binding_resolver=compatibility_binding_resolver,
            fact_resolver=PlatformFactResolver(
                binding_revision_id=compatibility_binding_resolver.binding_revision_id
            ),
            input_binder=SolverInputBinder(),
            snapshot_builder=FactSnapshotBuilder(),
            compiler=ProblemIRCompiler(
                registry=ProblemFamilyRegistry([_TestOnlyCompatibilityAdapter()])
            ),
            job_orchestrator=orchestrator,
            verifier=IndependentVerificationService(
                verifier=BoundedIndependentVerifier()
            ),
            artifact_store=ContentAddressedSolverArtifactStore(root / "artifacts"),
            result_projector=AuthoritativeResultProjector(),
            endpoint_policy=endpoint_policy,
            max_attempts=max_attempts,
        )

    return SolverCoordinationService(
        capability_resolver=RuntimeSolverCapabilityResolver(),
        binding_resolver=binding_resolver,
        fact_resolver=ProfileFactResolver(
            profile_registry=profiles,
            binding_revision_id=(
                None
                if selected_binding_revision == "connector-binding-unconfigured"
                else selected_binding_revision
            )
        ),
        input_binder=SolverInputBinder(),
        snapshot_builder=FactSnapshotBuilder(),
        compiler=ProblemIRCompiler(
            registry=ProblemFamilyRegistry(
                [
                    BinaryOrderedRouteAdapter(),
                    CandidateSelectionAdapter(profiles),
                ]
            )
        ),
        job_orchestrator=orchestrator,
        verifier=IndependentVerificationService(
            verifier=ProfileDispatchVerifier(profiles)
        ),
        artifact_store=ContentAddressedSolverArtifactStore(root / "artifacts"),
        result_projector=AuthoritativeResultProjector(),
        presenter=ProfilePresentationService(profiles),
        input_adapter=ProfileInputAdapterService(profiles),
        candidate_results_builder=build_candidate_results,
        endpoint_policy=endpoint_policy,
        max_attempts=max_attempts,
    )
