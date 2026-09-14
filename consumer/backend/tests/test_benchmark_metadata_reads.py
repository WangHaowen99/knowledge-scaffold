from __future__ import annotations

from internal_access import internal
import json
from types import SimpleNamespace

from scripts import benchmark_metadata_reads as benchmark
from scripts.benchmark_metadata_reads import evaluate_thresholds, percentile


def _report(
    *,
    workspace=1.0,
    ontology=1.0,
    tree=1.0,
    traversals=0,
    warm_io_traversals=0,
    lists=1,
    eager=0,
):
    def metric(value):
        return {
            "warm_p95_ms": value,
            "warm_authoritative_traversals": traversals,
            "warm_io": {"open": 0, "stat": 0, "traversal": warm_io_traversals},
        }

    return {
        "thresholds_ms": {
            "workspace_warm_p95": 200.0,
            "ontology_warm_p95": 300.0,
        },
        "metrics": {
            "workspace_list": metric(workspace),
            "ontology_list": metric(ontology),
            "ontology_tree": metric(tree),
        },
        "frontend_contract": {
            "ontology_list_requests": lists,
            "non_knowledge_tree_requests": eager,
        },
    }


def test_percentile_uses_deterministic_nearest_rank():
    assert percentile([5, 1, 4, 2, 3], 50) == 3
    assert percentile([5, 1, 4, 2, 3], 95) == 5
    assert percentile([], 95) == 0


def test_metadata_gate_accepts_values_strictly_below_every_limit():
    assert evaluate_thresholds(_report()) == []


def test_metadata_gate_rejects_equal_thresholds_traversals_and_request_fanout():
    violations = evaluate_thresholds(_report(workspace=200.0, ontology=300.0, traversals=1, lists=2, eager=1))
    codes = [violation["code"] for violation in violations]

    assert "warm_p95_exceeded" in codes
    assert "warm_authoritative_traversal" in codes
    assert "ontology_list_request_fanout" in codes
    assert "eager_tree_request" in codes


def test_metadata_gate_rejects_warm_filesystem_traversal_without_a_rebuild():
    violations = evaluate_thresholds(_report(warm_io_traversals=1))

    assert any(
        violation["code"] == "warm_filesystem_traversal" and violation["metric"] == "workspace_list"
        for violation in violations
    )


def test_frontend_contract_uses_executable_behavior_probe():
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            stdout=json.dumps(
                {
                    "ontology_list_requests": 1,
                    "non_knowledge_tree_requests": 0,
                }
            )
            + "\n",
        )

    assert internal(benchmark).frontend_request_contract(runner=run) == {
        "ontology_list_requests": 1,
        "non_knowledge_tree_requests": 0,
    }
    assert calls[0][0][0] == "node"
    assert calls[0][0][1].endswith("benchmark-ontology-requests.mjs")
    assert calls[0][1]["check"] is True


def test_benchmark_exercises_production_metadata_entrypoints(monkeypatch):
    import ontology.ontologies as ontologies
    import ontology.view_cache as view_cache
    import workspace.workspaces as workspaces

    calls = {"workspaces": 0, "ontology_list": 0, "ontology_tree": 0}
    original_workspaces = workspaces.list_workspaces
    original_registry = ontologies.get_ontology_registry
    original_tree = view_cache.get_package_tree_cached

    def list_workspaces():
        calls["workspaces"] += 1
        return original_workspaces()

    def get_registry(*args, **kwargs):
        calls["ontology_list"] += 1
        return original_registry(*args, **kwargs)

    def get_tree(*args, **kwargs):
        calls["ontology_tree"] += 1
        return original_tree(*args, **kwargs)

    monkeypatch.setattr(workspaces, "list_workspaces", list_workspaces)
    monkeypatch.setattr(ontologies, "get_ontology_registry", get_registry)
    monkeypatch.setattr(view_cache, "get_package_tree_cached", get_tree)

    report = benchmark.run_benchmark(
        samples=5,
        workspace_count=2,
        package_files=4,
    )

    assert report["passed"] is True
    assert calls["workspaces"] >= 6
    assert calls["ontology_list"] >= 6
    assert calls["ontology_tree"] >= 6
