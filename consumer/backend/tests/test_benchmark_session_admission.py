import pytest
from scripts import benchmark_session_admission as benchmark


def _report(*, create=1.0, first_page=1.0, preflight=1.0, detail_reads=50):
    return {
        "thresholds_ms": {
            "create_session_p95": 500.0,
            "first_page_p95": 300.0,
            "create_run_preflight_p95": 500.0,
        },
        "metrics": {
            "create_session": {"warm_p95_ms": create},
            "first_page_sessions": {"warm_p95_ms": first_page},
            "create_run_preflight": {"warm_p95_ms": preflight},
        },
        "counters": {
            "first_page_detail_reads_max": detail_reads,
            "page_limit": 50,
            "preflight_package_loads": 1,
            "preflight_requests": 6,
            "preflight_profile_loads": 6,
            "preflight_fact_checks": 6,
            "preflight_manifest_reads": 6,
            "preflight_root_payload_loads": 1,
            "preflight_source_signature_traversals": 3,
            "manager_create_calls": 6,
            "admitted_turns": 6,
        },
    }


def test_admission_gate_uses_strict_thresholds_and_bounded_reads():
    assert benchmark.evaluate_thresholds(_report()) == []
    violations = benchmark.evaluate_thresholds(
        _report(create=500.0, first_page=300.0, preflight=500.0, detail_reads=51)
    )
    codes = {item["code"] for item in violations}

    assert "p95_exceeded" in codes
    assert "first_page_detail_read_limit_exceeded" in codes

    repeated_traversal = _report()
    repeated_traversal["counters"]["preflight_source_signature_traversals"] = 4
    traversal_codes = {item["code"] for item in benchmark.evaluate_thresholds(repeated_traversal)}
    assert "preflight_source_signature_traversal_repeated" in traversal_codes

    missing_paths = _report()
    missing_paths["counters"].update(
        {
            "preflight_fact_checks": 0,
            "preflight_manifest_reads": 0,
            "preflight_root_payload_loads": 0,
            "manager_create_calls": 0,
            "admitted_turns": 0,
        }
    )
    path_codes = {item["code"] for item in benchmark.evaluate_thresholds(missing_paths)}
    assert path_codes == {
        "dynamic_fact_check_skipped",
        "ontology_manifest_check_skipped",
        "ontology_root_payload_not_loaded_once",
        "canonical_manager_admission_skipped",
        "durable_turn_admission_skipped",
    }


def test_admission_benchmark_exercises_production_entrypoints(monkeypatch):
    from agent.runtime import preflight as preflight_module
    from agent.runtime.manager import AsyncRunManager
    from api.routes import sessions as session_routes
    from ontology import ontologies as ontologies_module
    from ontology import ontology_catalog
    from persistence.chat_store import ChatStore
    from persistence.execution_store import ExecutionStore

    calls = {
        "create": 0,
        "list": 0,
        "preflight": 0,
        "manager": 0,
        "durable_turn": 0,
        "manifest": 0,
        "root_payload": 0,
    }
    original_create = ChatStore.create_session
    original_list = session_routes.list_sessions
    original_preflight = preflight_module.build_run_preflight_snapshot
    original_manager_create = AsyncRunManager.create_run
    original_durable_turn = ExecutionStore.create_turn_for_run
    original_manifest = ontologies_module.get_runtime_ontology_manifest
    original_root_payload = ontology_catalog.build_root_payload

    def create(self, *args, **kwargs):
        calls["create"] += 1
        return original_create(self, *args, **kwargs)

    async def list_sessions(*args, **kwargs):
        calls["list"] += 1
        return await original_list(*args, **kwargs)

    def build_preflight(*args, **kwargs):
        calls["preflight"] += 1
        return original_preflight(*args, **kwargs)

    async def manager_create(self, *args, **kwargs):
        calls["manager"] += 1
        return await original_manager_create(self, *args, **kwargs)

    def create_durable_turn(self, *args, **kwargs):
        calls["durable_turn"] += 1
        return original_durable_turn(self, *args, **kwargs)

    def load_manifest(*args, **kwargs):
        calls["manifest"] += 1
        return original_manifest(*args, **kwargs)

    def build_root_payload(*args, **kwargs):
        calls["root_payload"] += 1
        return original_root_payload(*args, **kwargs)

    monkeypatch.setattr(ChatStore, "create_session", create)
    monkeypatch.setattr(session_routes, "list_sessions", list_sessions)
    monkeypatch.setattr(
        preflight_module,
        "build_run_preflight_snapshot",
        build_preflight,
    )
    monkeypatch.setattr(AsyncRunManager, "create_run", manager_create)
    monkeypatch.setattr(
        ExecutionStore,
        "create_turn_for_run",
        create_durable_turn,
    )
    monkeypatch.setattr(
        ontologies_module,
        "get_runtime_ontology_manifest",
        load_manifest,
    )
    monkeypatch.setattr(
        ontology_catalog,
        "build_root_payload",
        build_root_payload,
    )

    report = benchmark.run_benchmark(samples=5, session_count=80, page_limit=50)

    assert report["passed"] is True
    assert calls == {
        "create": 5,
        "list": 6,
        "preflight": 6,
        "manager": 6,
        "durable_turn": 6,
        "manifest": 6,
        "root_payload": 1,
    }
    assert report["counters"]["first_page_detail_reads_max"] <= 50
    assert report["counters"]["preflight_package_loads"] == 1
    assert report["counters"]["preflight_profile_loads"] == 6
    assert report["counters"]["preflight_fact_checks"] == 6
    assert report["counters"]["preflight_manifest_reads"] == 6
    assert report["counters"]["preflight_root_payload_loads"] == 1
    assert report["counters"]["preflight_source_signature_traversals"] <= 3
    assert report["counters"]["manager_create_calls"] == 6
    assert report["counters"]["admitted_turns"] == 6


@pytest.mark.parametrize(
    "target",
    [
        "agent.runtime.manager.AsyncRunManager.create_run",
        "persistence.execution_store.ExecutionStore.create_turn_for_run",
        "ontology.ontologies.get_runtime_ontology_manifest",
        "ontology.ontology_catalog.build_root_payload",
    ],
)
def test_admission_benchmark_fails_when_a_production_path_fails(
    monkeypatch,
    target,
):
    def fail(*_args, **_kwargs):
        raise AssertionError("production admission path was exercised")

    monkeypatch.setattr(target, fail)

    with pytest.raises(AssertionError, match="production admission path was exercised"):
        benchmark.run_benchmark(samples=5, session_count=80, page_limit=50)
