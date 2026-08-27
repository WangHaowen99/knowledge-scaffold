from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from integration import solver_fact_providers as fact_provider_module
from integration.solver_fact_providers import (
    PackageEvaluationFactProvider,
    PackageEvaluationError,
    PackageRouteEvaluationRunner,
)
from agent.tools.business_data.script_env import SCRIPT_BUSINESS_DATA_CONNECTION_ENV
from solver_coordination.contracts import NormalizedInput
from solver_coordination.errors import (
    KnowledgePackageSolverContractIncompatibleError,
)
from solver_coordination.fact_snapshot import FactSnapshotInvalidError


def _capability() -> dict[str, Any]:
    return {
        "package_id": "demo-refinery-ontology",
        "package_revision": "9.4.2",
        "decision_model": {
            "profile_id": "ordered_binary_route.v1",
            "fact_source": {
                "kind": "external_snapshot",
                "fail_closed": True,
                "evaluator_path": "scripts/synthetic_route_evaluator.py",
            },
            "required_facts": ["route_metrics"],
        },
    }


def _normalized_input(*, month: str, item_count: int, first_quantity: str) -> NormalizedInput:
    origins = (
        "SYNTHETIC_FEED_A",
        "SYNTHETIC_FEED_B",
        "SYNTHETIC_FEED_C",
        "SYNTHETIC_FEED_D",
    )
    return NormalizedInput(
        operation_key="optimize_synthetic_route",
        values={
            "plan_month": month,
            "diesel_unit_no": "2#",
            "items": [
                {
                    "item_order": index,
                    "item_id": "batch-{}".format(index),
                    "crude_origin": origins[(index - 1) % len(origins)],
                    "quantity_wan_tons": first_quantity if index == 1 else "1.000",
                    "process_days": "1.00",
                    "quantity": first_quantity if index == 1 else "1.000",
                    "duration": "1天",
                }
                for index in range(1, item_count + 1)
            ],
        },
    )


class _EvaluationRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        normalized_input = kwargs["normalized_input"]
        candidates = kwargs["candidates"]
        score = Decimal(normalized_input["items"][0]["quantity_wan_tons"])
        route_context = {
            "schema_version": "refinery_route_context.v1",
            "profile_id": "refinery_route.v1",
            "metric_scale": 100,
            "rounding": "ROUND_HALF_EVEN",
            "plan_month": normalized_input["plan_month"],
            "production_condition": "NO_JET",
            "items": [
                {
                    "item_id": item["item_id"],
                    "item_order": item["item_order"],
                    "source_id": "SOURCE_SYNTHETIC",
                    "input_tons": str(Decimal(item["quantity_wan_tons"]) * 10000),
                    "process_days": item["process_days"],
                }
                for item in normalized_input["items"]
            ],
            "model": {},
            "tables": {},
        }
        return {
            "route_metrics": {
                "metric_scale": 100,
                "rounding": "ROUND_HALF_EVEN",
                "candidates": {
                    candidate["candidate_id"]: {
                        "eligible": True,
                        "primary_score": str(score),
                        "safety_margin": "1.00",
                    }
                    for candidate in candidates
                },
            },
            "refinery_route_context": route_context,
            "as_of": "2099-04-05T00:00:00+00:00",
            "source_evidence": (
                {"kind": "package_business_evaluation", "ref": "fake-evaluator"},
            ),
            "provider_binding_revision": "package-evaluator:test",
        }


def _provider(tmp_path: Path, runner: _EvaluationRunner) -> PackageEvaluationFactProvider:
    package_root = tmp_path / "synthetic-ontology-9.4.2"
    script_root = package_root / "scripts"
    script_root.mkdir(parents=True)
    (script_root / "synthetic_route_evaluator.py").write_text(
        "# synthetic evaluator placeholder\n",
        encoding="utf-8",
    )

    def resolve_ontology(package_id: str, *, version: str, workspace_id: str):
        assert package_id == "demo-refinery-ontology"
        assert version == "9.4.2"
        assert workspace_id == "workspace-solver-demo"
        return SimpleNamespace(version=version, ontology_dir=str(package_root)), None

    def resolve_business_data(*, workspace_id: str, ontology_id: str):
        assert workspace_id == "workspace-solver-demo"
        assert ontology_id == "demo-refinery-ontology"
        return SimpleNamespace(
            provider="neo4j",
            config={
                "uri": "bolt://graph.example.test:7687",
                "workspace_property": "d2o_workspace_id",
                "workspace_value": "workspace_demo",
                "ontology_property": "d2o_ontology_id",
            },
        )

    return PackageEvaluationFactProvider(
        ontology_resolver=resolve_ontology,
        business_data_resolver=resolve_business_data,
        evaluation_runner=runner,
    )


@pytest.mark.parametrize(
    ("month", "item_count", "candidate_count"),
    [("2099-02", 7, 14), ("2099-03", 9, 18)],
)
def test_package_provider_uses_same_package_for_any_month_and_batch_count(
    tmp_path: Path,
    month: str,
    item_count: int,
    candidate_count: int,
) -> None:
    runner = _EvaluationRunner()
    provider = _provider(tmp_path, runner)
    normalized_input = _normalized_input(
        month=month,
        item_count=item_count,
        first_quantity="2.500",
    )

    resolved = provider.resolve_facts(
        capability=_capability(),
        normalized_input=normalized_input,
        required_facts=("route_metrics",),
        binding_revision_id="binding-test",
        workspace_id="workspace-solver-demo",
        package_id="demo-refinery-ontology",
        package_revision="9.4.2",
    )

    assert [fact["name"] for fact in resolved["facts"]] == [
        "route_metrics",
        "refinery_route_context",
    ]
    assert len(resolved["facts"][0]["value"]["candidates"]) == candidate_count
    assert resolved["facts"][1]["value"]["plan_month"] == month
    assert runner.calls[0]["normalized_input"]["plan_month"] == month
    assert runner.calls[0]["evaluator_path"].name == "synthetic_route_evaluator.py"
    assert runner.calls[0]["graph_scope"] == {
        "workspace_id": "workspace_demo",
        "ontology_id": "demo-refinery-ontology",
    }
    assert resolved["bindings"] == {
        "provider_binding_revision": "package-evaluator:test",
        "solver_binding_revision": "binding-test",
    }


def test_two_different_february_inputs_are_evaluated_independently(tmp_path: Path) -> None:
    runner = _EvaluationRunner()
    provider = _provider(tmp_path, runner)

    results = []
    for quantity in ("2.500", "8.750"):
        results.append(
            provider.resolve_facts(
                capability=_capability(),
                normalized_input=_normalized_input(
                    month="2099-02",
                    item_count=7,
                    first_quantity=quantity,
                ),
                required_facts=("route_metrics",),
                binding_revision_id="binding-test",
                workspace_id="workspace-solver-demo",
                package_id="demo-refinery-ontology",
                package_revision="9.4.2",
            )
        )

    assert len(runner.calls) == 2
    assert runner.calls[0]["normalized_input"] != runner.calls[1]["normalized_input"]
    first = results[0]["facts"][0]["value"]["candidates"]["ALL_0"]
    second = results[1]["facts"][0]["value"]["candidates"]["ALL_0"]
    assert first["primary_score"] == "2.500"
    assert second["primary_score"] == "8.750"


def test_package_provider_fails_closed_when_business_evaluation_fails(tmp_path: Path) -> None:
    runner = _EvaluationRunner()
    provider = _provider(tmp_path, runner)

    def fail(**_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("DATABASE_URL=synthetic-secret-that-must-not-leak")

    object.__setattr__(provider, "evaluation_runner", fail)

    with pytest.raises(
        FactSnapshotInvalidError,
        match=r"^package business evaluation failed$",
    ) as error:
        provider.resolve_facts(
            capability=_capability(),
            normalized_input=_normalized_input(
                month="2099-02",
                item_count=7,
                first_quantity="2.500",
            ),
            required_facts=("route_metrics",),
            binding_revision_id="binding-test",
            workspace_id="workspace-solver-demo",
            package_id="demo-refinery-ontology",
            package_revision="9.4.2",
        )
    assert "synthetic-secret" not in str(error.value)


def test_package_provider_preserves_an_incompatible_package_contract(tmp_path: Path) -> None:
    runner = _EvaluationRunner()
    provider = _provider(tmp_path, runner)

    def fail(**_kwargs: Any) -> dict[str, Any]:
        raise PackageEvaluationError(
            "knowledge package solver contract is incompatible",
            code="knowledge_package_solver_contract_incompatible",
        )

    object.__setattr__(provider, "evaluation_runner", fail)

    with pytest.raises(KnowledgePackageSolverContractIncompatibleError):
        provider.resolve_facts(
            capability=_capability(),
            normalized_input=_normalized_input(
                month="2099-02",
                item_count=7,
                first_quantity="2.500",
            ),
            required_facts=("route_metrics",),
            binding_revision_id="binding-test",
            workspace_id="workspace-solver-demo",
            package_id="demo-refinery-ontology",
            package_revision="9.4.2",
        )


def test_package_evaluator_child_receives_only_scoped_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    worker = tmp_path / "worker.py"
    evaluator = tmp_path / "evaluator.py"
    worker.write_text("# worker\n", encoding="utf-8")
    evaluator.write_text("# evaluator\n", encoding="utf-8")
    captured: dict[str, Any] = {}

    def run(*args: Any, **kwargs: Any):
        captured["args"] = args
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout='{"route_metrics":{}}')

    monkeypatch.setattr(fact_provider_module.subprocess, "run", run)
    monkeypatch.setenv("LLM_API_KEY", "synthetic-secret-that-must-not-leak")
    monkeypatch.setenv("AUTH_SIGNING_SECRET", "synthetic-auth-secret")
    runner = PackageRouteEvaluationRunner(worker_path=worker)

    command_text = '"; Remove-Item -Recurse C:\\synthetic-do-not-run; #'
    runner(
        evaluator_path=evaluator,
        normalized_input={"items": [{"item_id": command_text}]},
        candidates=(),
        business_data_source=SimpleNamespace(
            provider="neo4j",
            config={"uri": "bolt://graph.example.test:7687"},
        ),
        graph_scope={"workspace_id": "workspace_demo", "ontology_id": "ontology_demo"},
    )

    child_env = captured["env"]
    assert "LLM_API_KEY" not in child_env
    assert "AUTH_SIGNING_SECRET" not in child_env
    assert SCRIPT_BUSINESS_DATA_CONNECTION_ENV in child_env
    assert "PYTHONPATH" in child_env
    argv = captured["args"][0]
    assert argv == [
        sys.executable,
        "-m",
        "integration.solver_route_fact_worker",
        "--evaluator",
        str(evaluator),
    ]
    assert all(command_text not in argument for argument in argv)
    child_input = json.loads(captured["input"])
    assert child_input["normalized_input"]["items"][0]["item_id"] == command_text


def test_package_evaluator_child_loads_a_sibling_helper_module(tmp_path: Path) -> None:
    evaluator = tmp_path / "evaluator.py"
    helper = tmp_path / "evaluator_helper.py"
    marker = tmp_path / "helper-loaded.marker"
    helper.write_text(
        "from pathlib import Path\n"
        "Path(__file__).with_name('helper-loaded.marker').write_text('loaded', encoding='utf-8')\n"
        "class BatchPlan:\n"
        "    pass\n",
        encoding="utf-8",
    )
    evaluator.write_text(
        "from evaluator_helper import BatchPlan\n"
        "def build_report(*args, **kwargs):\n"
        "    return {}\n"
        "def load_tables():\n"
        "    return {}\n"
        "def normalize_crude_name(value):\n"
        "    return value\n",
        encoding="utf-8",
    )
    runner = PackageRouteEvaluationRunner(timeout_seconds=30)

    with pytest.raises(PackageEvaluationError) as error:
        runner(
            evaluator_path=evaluator,
            normalized_input={},
            candidates=(),
            business_data_source=SimpleNamespace(
                provider="neo4j",
                config={"uri": "bolt://graph.example.test:7687"},
            ),
            graph_scope={
                "workspace_id": "workspace_demo",
                "ontology_id": "ontology_demo",
            },
        )

    assert error.value.code == "knowledge_package_solver_contract_incompatible"
    assert marker.read_text(encoding="utf-8") == "loaded"
