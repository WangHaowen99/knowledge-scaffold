from __future__ import annotations

from internal_access import internal
import importlib
import json
import sys
import threading
import types
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from integration import solver_coordination_runtime as runtime_module
from integration.solver_coordination_runtime import (
    BoundedIndependentVerifier,
    PlatformFactResolver,
    _load_or_create_secret,
    build_solver_coordination_service,
)
from solver_coordination.contracts import NormalizedInput, SolverResult
from solver_coordination.errors import VerificationDivergenceError
from solver_coordination.fact_snapshot import FactSnapshotInvalidError
from solver_coordination.job_store import SolverJobStore


def _capability(*, objectives=()):
    return {
        "problem_family": "binary_ordered_route.v1",
        "decision_model": {
            "decision_space": {"choices": ["OPTION_0", "OPTION_1"]},
            "constraints": [],
            "objectives": list(objectives),
        },
    }


def _result(*, objective_values=None, assignment=None):
    return SolverResult(
        problem_id="sha256:" + "a" * 64,
        status="feasible",
        assignment={"choice": "OPTION_0"} if assignment is None else assignment,
        objective_values=objective_values or {},
        diagnostics=(),
        engine={"name": "synthetic-engine", "version": "1"},
    )


def test_runtime_secret_is_exact_binary_and_reused(tmp_path, monkeypatch):
    expected = b"a" * 8 + b"\n" + b"b" * 23
    monkeypatch.setattr(
        "integration.solver_coordination_runtime.secrets.token_bytes",
        lambda size: expected if size == 32 else b"",
    )
    path = tmp_path / "runtime" / "idempotency.key"

    assert _load_or_create_secret(path) == expected
    assert _load_or_create_secret(path) == expected
    assert path.read_bytes() == expected


def test_runtime_service_assembly_uses_durable_store(tmp_path):
    service = build_solver_coordination_service(runtime_root=tmp_path)

    assert service.store.database_path.is_file()
    assert service.job_orchestrator.backend.capabilities() == {
        "protocol_versions": ["ortools.solver.v1"],
        "transport": "http-json",
        "connector": {
            "name": "ortools",
            "build": service.job_orchestrator.backend.capabilities()["connector"]["build"],
        },
    }


def test_runtime_assembly_injects_one_profile_registry_and_ortools_connector(tmp_path):
    service = build_solver_coordination_service(runtime_root=tmp_path)
    profiles = service.fact_resolver.profile_registry
    candidate_adapter = service.compiler.registry.get("candidate_selection.v1")

    assert service.compiler.registry.families() == (
        "binary_ordered_route.v1",
        "candidate_selection.v1",
    )
    assert candidate_adapter.profile_registry is profiles
    assert service.verifier.verifier.profile_registry is profiles
    assert service.presenter.profile_registry is profiles
    assert profiles.profile_ids() == ("ordered_binary_route.v1",)

    backend = service.job_orchestrator.backend
    assert backend.__class__.__name__ == "OrToolsConnector"
    assert backend.transport.__class__.__name__ == "HttpSolverTransport"
    assert backend.capabilities()["protocol_versions"] == ["ortools.solver.v1"]


def _write_package_validation_fixture(root: Path) -> None:
    fixture_root = root / "tests" / "solver"
    fixture_root.mkdir(parents=True)
    golden = {
        "fixture_version": "solver-fixture.v2",
        "fixture_scope": "test_only",
        "case_id": "synthetic-route-golden",
        "input": {
            "plan_month": "2099-03",
            "diesel_unit_no": "2#",
            "items": [
                {
                    "item_order": 1,
                    "item_id": "BATCH_001",
                    "crude_origin": "SYNTHETIC_FEED_A",
                    "quantity_wan_tons": "3.125",
                    "process_days": "1.25",
                },
                {
                    "item_order": 2,
                    "item_id": "BATCH_002",
                    "crude_origin": "SYNTHETIC_FEED_B",
                    "quantity_wan_tons": "8.250",
                    "process_days": "2.5",
                },
            ],
        },
        "route_metric_fixture": "route-metrics.json",
    }
    metrics = {
        "fixture_version": "route-metrics-fixture.v1",
        "fixture_scope": "test_only",
        "case_id": "synthetic-route-golden",
        "facts": [
            {
                "name": "route_metrics",
                "value": {
                    "metric_scale": 100,
                    "rounding": "ROUND_HALF_EVEN",
                    "capacity_limit": "100.00",
                    "items": {
                        "BATCH_001": {
                            "OPTION_0": {"primary_score": "10", "load": "1", "safety": "10"},
                            "OPTION_1": {"primary_score": "1", "load": "1", "safety": "10"},
                        },
                        "BATCH_002": {
                            "OPTION_0": {"primary_score": "1", "load": "1", "safety": "10"},
                            "OPTION_1": {"primary_score": "10", "load": "1", "safety": "10"},
                        },
                    },
                },
            }
        ],
        "bindings": {"solver_binding_revision": "fixture-placeholder"},
        "as_of": "2099-03-01T00:00:00Z",
        "source_evidence": [{"kind": "synthetic_test_fixture", "ref": "route-metrics.json"}],
    }
    (fixture_root / "golden-route-case.json").write_text(json.dumps(golden, ensure_ascii=False), encoding="utf-8")
    (fixture_root / "route-metrics.json").write_text(json.dumps(metrics, ensure_ascii=False), encoding="utf-8")


def test_explicit_package_validation_provider_remaps_fixture_to_current_item_ids(tmp_path, monkeypatch):
    factory = getattr(runtime_module, "build_ordered_route_fact_provider", None)
    assert callable(factory), "runtime must expose an explicit route fact provider factory"

    package_root = tmp_path / "synthetic-ontology-9.4.2"
    _write_package_validation_fixture(package_root)
    monkeypatch.setenv("SOLVER_ROUTE_FACT_PROVIDER", "package_validation_fixture")
    monkeypatch.setenv("SOLVER_ROUTE_VALIDATION_WORKSPACE", "workspace-solver-demo")

    def resolve_ontology(package_id, *, version, workspace_id):
        assert package_id == "demo-refinery-ontology"
        assert version == "9.4.2"
        assert workspace_id == "workspace-solver-demo"
        return SimpleNamespace(version=version, ontology_dir=str(package_root)), None

    provider = factory(ontology_resolver=resolve_ontology)
    normalized_input = NormalizedInput(
        operation_key="optimize_synthetic_route",
        values={
            "plan_month": "2099-03",
            "diesel_unit_no": "2#",
            "items": [
                {
                    "item_order": 1,
                    "item_id": "batch-1",
                    "crude_origin": "SYNTHETIC_FEED_A",
                    "quantity_wan_tons": "3.125",
                    "process_days": "1.25",
                    "quantity": "3.125",
                    "duration": "1.25天",
                },
                {
                    "item_order": 2,
                    "item_id": "batch-2",
                    "crude_origin": "SYNTHETIC_FEED_B",
                    "quantity_wan_tons": "8.250",
                    "process_days": "2.5",
                    "quantity": "8.250",
                    "duration": "2.5天",
                },
            ],
        },
    )
    capability = {
        "package_id": "demo-refinery-ontology",
        "package_revision": "9.4.2",
        "decision_model": {
            "profile_id": "ordered_binary_route.v1",
            "fact_source": {"kind": "external_snapshot", "fail_closed": True},
            "required_facts": ["route_metrics"],
        },
    }

    resolved = provider.resolve_facts(
        capability=capability,
        normalized_input=normalized_input,
        required_facts=("route_metrics",),
        binding_revision_id="binding-test",
        workspace_id="workspace-solver-demo",
        package_id="demo-refinery-ontology",
        package_revision="9.4.2",
    )

    route_items = resolved["facts"][0]["value"]["items"]
    assert tuple(route_items) == ("batch-1", "batch-2")
    assert route_items["batch-1"]["OPTION_0"]["primary_score"] == "10"
    assert route_items["batch-2"]["OPTION_1"]["primary_score"] == "10"
    assert resolved["bindings"] == {
        "provider_binding_revision": "package-validation-fixture:synthetic-route-golden",
        "solver_binding_revision": "binding-test",
    }
    assert resolved["source_evidence"] == (
        {
            "kind": "synthetic_test_fixture",
            "ref": "package-validation-fixture:synthetic-route-golden",
        },
    )

    with pytest.raises(FactSnapshotInvalidError, match="validation workspace"):
        provider.resolve_facts(
            capability=capability,
            normalized_input=normalized_input,
            required_facts=("route_metrics",),
            binding_revision_id="binding-test",
            workspace_id="production-workspace",
            package_id="demo-refinery-ontology",
            package_revision="9.4.2",
        )


def test_route_fact_provider_defaults_to_package_evaluation(monkeypatch):
    factory = getattr(runtime_module, "build_ordered_route_fact_provider", None)
    assert callable(factory), "runtime must expose an explicit route fact provider factory"
    monkeypatch.delenv("SOLVER_ROUTE_FACT_PROVIDER", raising=False)
    monkeypatch.delenv("SOLVER_ROUTE_VALIDATION_WORKSPACE", raising=False)

    provider = factory()

    assert provider.__class__.__name__ == "PackageEvaluationFactProvider"


def test_runtime_service_assembly_initializes_recovery_binding(tmp_path):
    service = build_solver_coordination_service(runtime_root=tmp_path)

    assert service.job_orchestrator.binding_revision_id is None
    assert service.binding_resolver.revision_id == "connector-binding-unconfigured"
    assert (
        service.job_orchestrator.recover_once(
            workspace_id="workspace-empty",
            owner_id="owner-empty",
        )
        == ()
    )


def test_runtime_credential_resolver_uses_only_the_configured_opaque_reference(
    monkeypatch,
):
    monkeypatch.setenv(
        "SOLVER_SERVICE_CREDENTIAL_REF",
        "secret://solver/primary",
    )
    monkeypatch.setenv("SOLVER_SERVICE_TOKEN", "synthetic-token-value")

    resolver = runtime_module.build_solver_connector_credential_resolver()

    assert resolver("secret://solver/primary") == "synthetic-token-value"
    assert resolver("secret://solver/other") is None


def test_runtime_credential_resolver_can_read_a_mounted_secret_file(tmp_path, monkeypatch):
    secret_file = tmp_path / "connector-token"
    secret_file.write_text("synthetic-mounted-token\n", encoding="utf-8")
    monkeypatch.setenv(
        "SOLVER_SERVICE_CREDENTIAL_REF",
        "secret://solver/primary",
    )
    monkeypatch.setenv("SOLVER_SERVICE_TOKEN_FILE", str(secret_file))
    monkeypatch.delenv("SOLVER_SERVICE_TOKEN", raising=False)

    resolver = runtime_module.build_solver_connector_credential_resolver()

    assert resolver("secret://solver/primary") == "synthetic-mounted-token"


def test_runtime_credential_resolver_rejects_multiple_secret_sources(tmp_path, monkeypatch):
    secret_file = tmp_path / "connector-token"
    secret_file.write_text("synthetic-mounted-token\n", encoding="utf-8")
    monkeypatch.setenv("SOLVER_SERVICE_TOKEN", "synthetic-direct-token")
    monkeypatch.setenv("SOLVER_SERVICE_TOKEN_FILE", str(secret_file))

    with pytest.raises(RuntimeError, match="mutually exclusive"):
        runtime_module.build_solver_connector_credential_resolver()


def test_runtime_credential_resolver_fails_closed_without_token(monkeypatch):
    monkeypatch.setenv(
        "SOLVER_SERVICE_CREDENTIAL_REF",
        "secret://solver/primary",
    )
    monkeypatch.delenv("SOLVER_SERVICE_TOKEN", raising=False)

    resolver = runtime_module.build_solver_connector_credential_resolver()

    assert resolver("secret://solver/primary") is None


def test_runtime_assembly_injects_environment_credential_resolver(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "SOLVER_SERVICE_CREDENTIAL_REF",
        "secret://solver/primary",
    )
    monkeypatch.setenv("SOLVER_SERVICE_TOKEN", "synthetic-token-value")

    service = build_solver_coordination_service(runtime_root=tmp_path)

    resolver = service.job_orchestrator.backend.transport.credential_resolver
    assert resolver("secret://solver/primary") == "synthetic-token-value"


def test_runtime_assembly_injects_administrator_endpoint_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLVER_SERVICE_ALLOWED_HOSTS", "solver.example.com")
    monkeypatch.setenv("SOLVER_SERVICE_ALLOWED_PORTS", "443,8443")
    monkeypatch.setenv("SOLVER_SERVICE_ALLOW_LOCALHOST", "false")

    service = build_solver_coordination_service(runtime_root=tmp_path)

    policy = service.endpoint_policy
    assert policy.allowed_hosts == ("solver.example.com",)
    assert policy.allowed_ports == (443, 8443)
    assert policy.allow_localhost is False
    assert service.binding_resolver.endpoint_policy is policy
    assert service.job_orchestrator.backend.transport.endpoint_policy is policy


def test_runtime_assembly_scopes_connector_ca_bundle_to_http_transport(tmp_path, monkeypatch):
    ca_file = tmp_path / "connector-ca.pem"
    ca_file.write_text(
        "-----BEGIN CERTIFICATE-----\nsynthetic\n-----END CERTIFICATE-----\n",
        encoding="ascii",
    )
    monkeypatch.setenv("SOLVER_SERVICE_TLS_CA_FILE", str(ca_file))

    service = build_solver_coordination_service(runtime_root=tmp_path)

    assert service.job_orchestrator.backend.transport.tls_ca_bundle == str(ca_file)


def test_runtime_assembly_allows_base_consumer_without_connector_ca(tmp_path, monkeypatch):
    monkeypatch.delenv("SOLVER_SERVICE_TLS_CA_FILE", raising=False)

    service = build_solver_coordination_service(runtime_root=tmp_path)

    assert service.job_orchestrator.backend.transport.tls_ca_bundle is None


def test_runtime_assembly_rejects_malformed_endpoint_policy_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLVER_SERVICE_ALLOWED_PORTS", "443,not-a-port")

    with pytest.raises(ValueError, match="port"):
        build_solver_coordination_service(runtime_root=tmp_path)


def test_runtime_direct_switch_ignores_legacy_connector_environment(monkeypatch):
    monkeypatch.setenv("SOLVER_CONNECTOR_ALLOWED_HOSTS", "legacy.example.com")
    monkeypatch.setenv("SOLVER_CONNECTOR_SERVICE_TOKEN", "legacy-synthetic-token")

    resolver = runtime_module.build_solver_connector_credential_resolver()
    policy = runtime_module.EndpointPolicy.from_env()

    assert resolver("secret://solver/primary") is None
    assert policy.allowed_hosts == ()


def test_runtime_recovery_keeps_workspace_owner_scopes_isolated(tmp_path):
    binding_revision_id = "binding-current"
    store = SolverJobStore(tmp_path / "jobs.sqlite3")
    current_scopes = (
        ("workspace-a", "owner-a"),
        ("workspace-a", "owner-b"),
        ("workspace-b", "owner-a"),
    )
    expected_scopes = (
        *current_scopes,
        ("workspace-other", "owner-other"),
    )
    for index, (workspace_id, owner_id) in enumerate(current_scopes):
        store.register_binding_revision(
            workspace_id=workspace_id,
            owner_id=owner_id,
            binding_revision_id=binding_revision_id,
            configuration={"backend": "synthetic"},
        )
        digest = "sha256:" + str(index + 1) * 64
        store.create_or_get_job(
            workspace_id=workspace_id,
            owner_id=owner_id,
            binding_revision_id=binding_revision_id,
            problem_digest=digest,
            problem={"problem_id": digest},
            run_policy={"scope_index": index},
            deadline_at=datetime(2026, 8, 18, tzinfo=timezone.utc),
            job_id="job-scope-{}".format(index),
        )
    store.register_binding_revision(
        workspace_id="workspace-other",
        owner_id="owner-other",
        binding_revision_id="binding-other",
        configuration={"backend": "other"},
    )
    other_digest = "sha256:" + "f" * 64
    store.create_or_get_job(
        workspace_id="workspace-other",
        owner_id="owner-other",
        binding_revision_id="binding-other",
        problem_digest=other_digest,
        problem={"problem_id": other_digest},
        run_policy={"scope_index": "other"},
        deadline_at=datetime(2026, 8, 18, tzinfo=timezone.utc),
        job_id="job-other-binding",
    )

    class RecordingService:
        def __init__(self):
            self.store = store
            self.job_orchestrator = SimpleNamespace(binding_revision_id=binding_revision_id)
            self.scopes = []

        def recover_job(self, job_id, *, workspace_id, owner_id):
            scope = (workspace_id, owner_id)
            self.scopes.append(scope)
            return {
                "job_id": job_id,
                "workspace_id": workspace_id,
                "owner_id": owner_id,
            }

    service = RecordingService()

    outcomes = runtime_module.recover_solver_coordination_jobs(service)

    assert tuple(service.scopes) == expected_scopes
    assert tuple((outcome["workspace_id"], outcome["owner_id"]) for outcome in outcomes) == expected_scopes


def test_recovery_worker_repeats_ticks_and_stops(monkeypatch):
    calls = []
    repeated = threading.Event()

    def recover(service):
        calls.append(service)
        if len(calls) >= 2:
            repeated.set()
        return ()

    monkeypatch.setattr(runtime_module, "recover_solver_coordination_jobs", recover)
    service = object()
    worker = runtime_module.SolverCoordinationRecoveryWorker(
        service,
        interval_seconds=0.01,
    )

    assert worker.start() is worker
    assert worker.start() is worker
    assert repeated.wait(1.0) is True
    worker.stop(timeout_seconds=1.0)

    assert worker.running is False
    assert len(calls) >= 2


def test_recovery_worker_stop_reports_unterminated_blocked_tick(monkeypatch):
    recovery_entered = threading.Event()
    release_recovery = threading.Event()

    def recover(_service):
        recovery_entered.set()
        release_recovery.wait(timeout=1.0)
        return ()

    monkeypatch.setattr(runtime_module, "recover_solver_coordination_jobs", recover)
    worker = runtime_module.SolverCoordinationRecoveryWorker(
        object(),
        interval_seconds=0.01,
    ).start()

    terminated_after_release = False
    try:
        assert recovery_entered.wait(timeout=1.0) is True
        assert worker.stop(timeout_seconds=0.01) is False
        assert worker.running is True
    finally:
        release_recovery.set()
        terminated_after_release = worker.stop(timeout_seconds=1.0)

    assert terminated_after_release is True
    assert worker.running is False


def test_create_app_runs_solver_recovery_at_startup(monkeypatch):
    langdetect = types.ModuleType("langdetect")

    class DetectorFactory:
        seed = 0

    class LangDetectException(Exception):
        pass

    def detect_language(_text):
        return "en"

    def detect_languages(_text):
        return []

    langdetect.DetectorFactory = DetectorFactory
    langdetect.LangDetectException = LangDetectException
    langdetect.detect = detect_language
    langdetect.detect_langs = detect_languages
    monkeypatch.setitem(sys.modules, "langdetect", langdetect)
    app_module = importlib.import_module("app")

    solver_service = object()
    runtime = {
        "config": object(),
        "agent_config": object(),
        "llm_router": object(),
        "auth_manager": object(),
        "user_store_manager": object(),
        "solver_validation_service": object(),
        "solver_coordination_service": solver_service,
    }
    workers = []

    class FakeRecoveryWorker:
        def __init__(self, service):
            self.service = service
            self.started = False
            self.timeout_seconds = None
            workers.append(self)

        def start(self):
            self.started = True
            return self

        def stop(self, timeout_seconds=None):
            self.started = False
            self.timeout_seconds = timeout_seconds
            return True

    class FakeRuntimeContainer:
        def __init__(self, *, config, components_factory):
            self.config = config
            self.components_factory = components_factory
            self.started = False

        async def start(self):
            self.started = True

        async def close(self, *, deadline):
            self.started = False
            return True

    async def stop_runtime_warmup(*, deadline):
        return True

    monkeypatch.setattr(app_module, "run_startup_migrations", lambda: None)
    monkeypatch.setattr(
        app_module,
        "recover_workspace_ontology_replacements",
        lambda _root: None,
    )
    monkeypatch.setattr(app_module, "build_runtime_services", lambda: runtime)
    monkeypatch.setattr(
        app_module,
        "initialize_runtime_dependencies",
        lambda *args, **kwargs: SimpleNamespace(
            operation_patcher=None,
            reflection_worker=None,
        ),
    )
    monkeypatch.setattr(app_module, "RuntimeContainer", FakeRuntimeContainer)
    monkeypatch.setattr(
        app_module.NativeAsyncRuntimeConfig,
        "from_agent_config",
        lambda _config: SimpleNamespace(shutdown_total_deadline_seconds=30.0),
    )
    monkeypatch.setattr(app_module, "components_factory", lambda _provider: object())
    monkeypatch.setattr(app_module, "set_agent_execution_runtime", lambda _runtime: None)
    monkeypatch.setattr(app_module, "start_runtime_warmup", lambda **kwargs: None)
    monkeypatch.setattr(app_module, "stop_runtime_warmup", stop_runtime_warmup)
    monkeypatch.setattr(app_module, "init_skill_evolution_worker", lambda: None)
    monkeypatch.setattr(app_module, "reset_skill_evolution_singletons", lambda: None)
    monkeypatch.setattr(app_module, "start_platform_observer_if_enabled", lambda: None)
    monkeypatch.setattr(app_module, "shutdown_platform_observer", lambda **kwargs: None)
    monkeypatch.setattr(app_module, "shutdown_observer", lambda **kwargs: None)
    monkeypatch.setattr(app_module, "configure_solver_tool_service", lambda _service: None)
    monkeypatch.setattr(app_module, "SolverCoordinationRecoveryWorker", FakeRecoveryWorker)

    application = app_module.create_app()

    with TestClient(application):
        assert application.state.services["SOLVER_COORDINATION_SERVICE"] is solver_service
        assert workers[0].service is solver_service
        assert workers[0].started is True
        assert application.state.solver_recovery_worker is workers[0]

    assert workers[0].started is False
    assert application.state.solver_recovery_worker is None
    assert application.state.services == {}


def test_concurrent_solver_worker_refresh_keeps_only_latest_worker_running(
    monkeypatch,
):
    from fastapi import FastAPI

    app_module = importlib.import_module("app")
    application = FastAPI()
    first_start_entered = threading.Event()
    release_first_start = threading.Event()
    second_start_entered = threading.Event()
    workers = []

    class FakeRecoveryWorker:
        def __init__(self, service):
            self.service = service
            self.running = False
            self.stop_calls = 0
            workers.append(self)

        def start(self):
            self.running = True
            if self.service == "service-first":
                first_start_entered.set()
                release_first_start.wait(timeout=1)
            elif self.service == "service-second":
                second_start_entered.set()
            return self

        def stop(self, timeout_seconds=None):
            del timeout_seconds
            self.running = False
            self.stop_calls += 1
            return True

    old_worker = FakeRecoveryWorker("service-old")
    old_worker.running = True
    application.state.solver_recovery_worker = old_worker
    monkeypatch.setattr(
        app_module,
        "SolverCoordinationRecoveryWorker",
        FakeRecoveryWorker,
    )

    first_refresh = threading.Thread(
        target=internal(app_module).install_solver_recovery_worker,
        args=(application, "service-first"),
    )
    second_refresh = threading.Thread(
        target=internal(app_module).install_solver_recovery_worker,
        args=(application, "service-second"),
    )
    first_refresh.start()
    assert first_start_entered.wait(timeout=1)
    second_refresh.start()
    second_start_entered.wait(timeout=0.2)
    release_first_start.set()
    first_refresh.join(timeout=1)
    second_refresh.join(timeout=1)

    assert not first_refresh.is_alive()
    assert not second_refresh.is_alive()
    current_worker = application.state.solver_recovery_worker
    assert current_worker.service == "service-second"
    assert [worker for worker in workers if worker.running] == [current_worker]
    assert all(worker.stop_calls == 1 for worker in workers if worker is not current_worker)


def test_platform_fact_resolver_fails_closed_without_provider():
    resolver = PlatformFactResolver(binding_revision_id="binding-synthetic")

    with pytest.raises(FactSnapshotInvalidError, match="snapshot provider"):
        resolver.resolve(
            required_facts=("capacity",),
            normalized_input=NormalizedInput(operation_key="solve_synthetic", values={"period": "P0"}),
        )


def test_bounded_verifier_publishes_only_independently_checked_feasibility():
    verification = BoundedIndependentVerifier().verify(
        capability=_capability(),
        normalized_input=NormalizedInput(operation_key="solve_synthetic", values={"period": "P0"}),
        fact_snapshot=MappingProxyType({}),
        solver_result=_result(),
    )

    assert verification.status == "verified"
    assert verification.feasibility == "feasible"
    assert verification.optimality == "not_proven"
    assert verification.proof_kind == "independent_domain_constraint_check"


def test_bounded_verifier_rejects_unrecalculated_objective_claims():
    with pytest.raises(VerificationDivergenceError, match="objective"):
        BoundedIndependentVerifier().verify(
            capability=_capability(objectives=({"name": "score", "direction": "min"},)),
            normalized_input=NormalizedInput(operation_key="solve_synthetic", values={"period": "P0"}),
            fact_snapshot=MappingProxyType({}),
            solver_result=_result(objective_values={"score": "1"}),
        )


@pytest.mark.parametrize(
    "decision_space",
    [
        {},
        {"choices": []},
        {"variables": [], "option_domain": ["OPTION_0", "OPTION_1"]},
    ],
)
def test_bounded_verifier_rejects_empty_decision_spaces_before_publishing_feasibility(
    decision_space,
):
    capability = _capability()
    capability["decision_model"]["decision_space"] = decision_space

    with pytest.raises(VerificationDivergenceError, match="decision space|decision variables|domain"):
        BoundedIndependentVerifier().verify(
            capability=capability,
            normalized_input=NormalizedInput(operation_key="solve_synthetic", values={"period": "P0"}),
            fact_snapshot=MappingProxyType({}),
            solver_result=_result(assignment={}),
        )


@pytest.mark.parametrize(
    ("decision_space", "assignment"),
    [
        ({"choices": ["OPTION_0", "OPTION_0"]}, {"choice": "OPTION_0"}),
        (
            {"choices": ["OPTION_0", "OPTION_1"], "variables": ["choice"]},
            {"choice": "OPTION_0"},
        ),
        (
            {
                "variables": ["choice", "choice"],
                "option_domain": ["OPTION_0", "OPTION_1"],
            },
            {"choice": "OPTION_0"},
        ),
        (
            {
                "variables": ["choice"],
                "domains": {
                    "choice": ["OPTION_0", "OPTION_1"],
                    "unexpected": ["OPTION_0", "OPTION_1"],
                },
            },
            {"choice": "OPTION_0"},
        ),
    ],
)
def test_bounded_verifier_rejects_ambiguous_or_non_binary_decision_space_shape(
    decision_space,
    assignment,
):
    capability = _capability()
    capability["decision_model"]["decision_space"] = decision_space

    with pytest.raises(VerificationDivergenceError, match="decision space|decision variables|domain"):
        BoundedIndependentVerifier().verify(
            capability=capability,
            normalized_input=NormalizedInput(operation_key="solve_synthetic", values={"period": "P0"}),
            fact_snapshot=MappingProxyType({}),
            solver_result=_result(assignment=assignment),
        )
