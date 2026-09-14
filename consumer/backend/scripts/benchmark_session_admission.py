"""Machine-readable P1 gate for session listing and run preflight admission."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import json
import logging
import math
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
result_logger = logging.getLogger(f"{__name__}.result")


def _configure_result_logging() -> None:
    result_logger.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    result_logger.addHandler(handler)
    result_logger.setLevel(logging.INFO)
    result_logger.propagate = False


def percentile(values: list[float], percentile_value: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    index = max(0, math.ceil(percentile_value / 100.0 * len(ordered)) - 1)
    return ordered[min(index, len(ordered) - 1)]


def _timed(call: Callable[[], Any]) -> tuple[Any, float]:
    started = time.perf_counter()
    result = call()
    return result, max(0.0, (time.perf_counter() - started) * 1000.0)


def _latency_metric(values: list[float]) -> dict[str, float]:
    return {
        "warm_p50_ms": round(percentile(values, 50), 3),
        "warm_p95_ms": round(percentile(values, 95), 3),
    }


@contextmanager
def _environment(values: dict[str, str]):
    previous = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def evaluate_thresholds(report: dict[str, Any]) -> list[dict[str, Any]]:
    thresholds = report["thresholds_ms"]
    metrics = report["metrics"]
    violations: list[dict[str, Any]] = []
    for metric_name, threshold_name in (
        ("create_session", "create_session_p95"),
        ("first_page_sessions", "first_page_p95"),
        ("create_run_preflight", "create_run_preflight_p95"),
    ):
        actual = metrics[metric_name]["warm_p95_ms"]
        limit = thresholds[threshold_name]
        if actual >= limit:
            violations.append(
                {
                    "code": "p95_exceeded",
                    "metric": metric_name,
                    "actual": actual,
                    "limit": limit,
                }
            )
    counters = report["counters"]
    if counters["first_page_detail_reads_max"] > counters["page_limit"]:
        violations.append(
            {
                "code": "first_page_detail_read_limit_exceeded",
                "metric": "first_page_detail_reads_max",
                "actual": counters["first_page_detail_reads_max"],
                "limit": counters["page_limit"],
            }
        )
    if counters["preflight_package_loads"] > 1:
        violations.append(
            {
                "code": "immutable_preflight_product_reloaded",
                "metric": "preflight_package_loads",
                "actual": counters["preflight_package_loads"],
                "limit": 1,
            }
        )
    # One before/after pair builds the immutable product. Source revision
    # observation is TTL-throttled in production, so longer benchmark windows
    # may legitimately perform one additional check per 25 admissions.
    signature_traversal_limit = counters["preflight_package_loads"] * 2 + max(
        1, math.ceil(counters["preflight_requests"] / 25)
    )
    if counters["preflight_source_signature_traversals"] > signature_traversal_limit:
        violations.append(
            {
                "code": "preflight_source_signature_traversal_repeated",
                "metric": "preflight_source_signature_traversals",
                "actual": counters["preflight_source_signature_traversals"],
                "limit": signature_traversal_limit,
            }
        )
    if counters["preflight_profile_loads"] != counters["preflight_requests"]:
        violations.append(
            {
                "code": "request_local_profile_check_skipped",
                "metric": "preflight_profile_loads",
                "actual": counters["preflight_profile_loads"],
                "limit": counters["preflight_requests"],
            }
        )
    expected_requests = counters["preflight_requests"]
    for counter_name, code in (
        ("preflight_fact_checks", "dynamic_fact_check_skipped"),
        ("preflight_manifest_reads", "ontology_manifest_check_skipped"),
        ("manager_create_calls", "canonical_manager_admission_skipped"),
        ("admitted_turns", "durable_turn_admission_skipped"),
    ):
        actual = counters[counter_name]
        if actual != expected_requests:
            violations.append(
                {
                    "code": code,
                    "metric": counter_name,
                    "actual": actual,
                    "limit": expected_requests,
                }
            )
    if counters["preflight_root_payload_loads"] != 1:
        violations.append(
            {
                "code": "ontology_root_payload_not_loaded_once",
                "metric": "preflight_root_payload_loads",
                "actual": counters["preflight_root_payload_loads"],
                "limit": 1,
            }
        )
    return violations


def _synthetic_summaries(count: int):
    from persistence.session_summary_index import SessionSummary

    return [
        SessionSummary.create(
            session_id=f"synthetic-session-{index:05d}",
            title=f"Synthetic session {index:05d}",
            created_at=f"2026-08-31T00:{index // 60:02d}:{index % 60:02d}+00:00",
            updated_at=f"2026-08-31T00:{index // 60:02d}:{index % 60:02d}+00:00",
            active_run_id=f"synthetic-run-{index:05d}",
            message_count=2,
            metadata={"pinned": False},
            last_run_status="running",
        )
        for index in range(count)
    ]


def _write_synthetic_ontology(root: Path) -> Path:
    ontology_root = root / "ontologies"
    package_root = ontology_root / "synthetic-ontology"
    revision_root = package_root / "versions" / "1.0.0"
    operations_root = revision_root / "operations"
    operations_root.mkdir(parents=True, exist_ok=True)
    (package_root / "manifest.yaml").write_text(
        "kind: ontology-package\n"
        "schema_version: 0.1\n"
        "id: synthetic-ontology\n"
        "name: Synthetic Ontology\n"
        "description: synthetic admission benchmark ontology\n"
        "version: 1.0.0\n"
        "active_version: 1.0.0\n"
        "versions: [1.0.0]\n"
        "language: en-US\n"
        "enabled: true\n",
        encoding="utf-8",
    )
    (revision_root / "index.md").write_text(
        "# Synthetic admission ontology\n",
        encoding="utf-8",
    )
    (operations_root / "index.md").write_text(
        "| operation_key | name | description |\n"
        "| --- | --- | --- |\n"
        "| synthetic_operation | Synthetic operation | Benchmark operation |\n",
        encoding="utf-8",
    )
    return ontology_root


class _AdmissionOnlyTask:
    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def add_done_callback(self, _callback) -> None:
        return None


class _AdmissionOnlySupervisor:
    """Exercise manager admission without executing a background Agent run."""

    def __init__(self) -> None:
        self.started = 0

    def start_run(self, _run_id: str, awaitable):
        self.started += 1
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        return _AdmissionOnlyTask()


def run_benchmark(
    *,
    samples: int = 25,
    session_count: int = 1000,
    page_limit: int = 50,
    create_session_p95_ms: float = 500.0,
    first_page_p95_ms: float = 300.0,
    create_run_preflight_p95_ms: float = 500.0,
) -> dict[str, Any]:
    from agent.core.orchestrator import AgenticOrchestrator
    from agent.runtime.manager import AsyncRunManager
    from agent.runtime.runtime import RuntimeState
    from api.auth.auth import AuthUser
    from api.routes import sessions as session_routes
    from api.routes import workflows
    from persistence.chat_store import ChatStore
    from persistence.execution_store import ExecutionStore
    from persistence.session_summary_index import SessionSummaryIndex
    from ontology import ontologies as ontologies_module
    from ontology import ontology_catalog
    from ontology import view_cache as ontology_view_cache
    from starlette.requests import Request

    samples = max(5, int(samples))
    session_count = max(int(page_limit), int(session_count))
    page_limit = min(100, max(1, int(page_limit)))

    with tempfile.TemporaryDirectory(prefix="consumer-session-benchmark-") as temporary:
        root = Path(temporary)
        ontology_root = _write_synthetic_ontology(root)
        summaries = _synthetic_summaries(session_count)
        summary_index = SessionSummaryIndex(
            root / "session-summary-index.json",
            rebuild_loader=lambda: summaries,
        )
        summary_index.rebuild()
        chat_store = ChatStore(
            str(root / "chat"),
            summary_index=summary_index,
        )
        execution_store = ExecutionStore(
            str(root / "executions"),
            summary_index=summary_index,
        )

        create_latencies = []
        for index in range(samples):
            _session, elapsed = _timed(
                lambda index=index: chat_store.create_session(
                    title=f"Created benchmark session {index}",
                    metadata={"agent_mode": "quick"},
                )
            )
            create_latencies.append(elapsed)

        class _ShortIo:
            @staticmethod
            async def run(function, *args, **kwargs):
                return function(*args, **kwargs)

        class _Manager:
            @staticmethod
            async def list_runs(_scope):
                return []

        runtime = SimpleNamespace(short_io=_ShortIo(), manager=_Manager())
        detail_reads = [0]
        detail_reads_per_call: list[int] = []
        original_get_session = chat_store.get_session

        def tracked_get_session(session_id: str):
            detail_reads[0] += 1
            return original_get_session(session_id)

        chat_store.get_session = tracked_get_session

        def list_first_page() -> Any:
            before = detail_reads[0]
            result = asyncio.run(
                session_routes.list_sessions(
                    request=SimpleNamespace(),
                    current_user=AuthUser(username="synthetic-user", role="user"),
                    workspace_id="synthetic-workspace",
                    limit=page_limit,
                    cursor=None,
                )
            )
            detail_reads_per_call.append(detail_reads[0] - before)
            return result

        with (
            patch.object(
                session_routes,
                "_get_user_stores",
                lambda: (chat_store, execution_store),
            ),
            patch.object(
                session_routes,
                "get_runtime_container",
                lambda _request: runtime,
            ),
        ):
            list_first_page()
            list_latencies = [_timed(list_first_page)[1] for _ in range(samples)]

        package_loads = [0]
        profile_loads = [0]
        preflight_fact_checks = [0]
        manifest_reads = [0]
        root_payload_loads = [0]
        source_signature_traversals = [0]
        manager_create_calls = [0]
        admitted_turns = [0]
        manifest_loader = ontologies_module.get_runtime_ontology_manifest
        root_payload_loader = ontology_catalog.build_root_payload
        source_signature_loader = ontology_view_cache.build_package_revision_source_signature
        durable_turn_create = execution_store.create_turn_for_run

        def load_manifest(*args, **kwargs):
            manifest_reads[0] += 1
            return manifest_loader(*args, **kwargs)

        def load_root_payload(*args, **kwargs):
            root_payload_loads[0] += 1
            package_loads[0] += 1
            return root_payload_loader(*args, **kwargs)

        def load_source_signature(*args, **kwargs):
            source_signature_traversals[0] += 1
            return source_signature_loader(*args, **kwargs)

        def load_profile():
            profile_loads[0] += 1
            return {"id": "synthetic-profile", "provider": "synthetic"}

        def track_durable_turn(*args, **kwargs):
            turn, created = durable_turn_create(*args, **kwargs)
            admitted_turns[0] += int(created)
            return turn, created

        execution_store.create_turn_for_run = track_durable_turn

        chat_store.get_session = original_get_session
        original_get_session_metadata = chat_store.get_session_metadata

        def tracked_fact_session(session_id: str):
            preflight_fact_checks[0] += 1
            return original_get_session_metadata(session_id)

        chat_store.get_session_metadata = tracked_fact_session
        orchestrator = AgenticOrchestrator(
            chat_store=chat_store,
            execution_store=execution_store,
        )

        class _PermitAll:
            @staticmethod
            def try_acquire():
                return True

        orchestrator.run_rate_limiter = _PermitAll()

        supervisor = _AdmissionOnlySupervisor()
        manager = AsyncRunManager(
            execution_service=SimpleNamespace(),
            supervisor=supervisor,
        )
        manager_create = manager.create_run

        async def tracked_manager_create(*args, **kwargs):
            manager_create_calls[0] += 1
            return await manager_create(*args, **kwargs)

        manager.create_run = tracked_manager_create

        admission_runtime = SimpleNamespace(
            short_io=_ShortIo(),
            manager=manager,
            state=RuntimeState.ACCEPTING,
        )
        request_number = [0]

        def admit_run():
            index = request_number[0]
            request_number[0] += 1
            body = json.dumps(
                {
                    "request": "Synthetic admission request",
                    "session_id": f"admission-session-{index:05d}",
                    "agent_mode": "quick",
                    "llm_request_mode": "stream",
                    "arguments": {
                        "selected_ontology_id": "synthetic-ontology",
                        "selected_ontology_version": "1.0.0",
                    },
                }
            ).encode("utf-8")
            delivered = False

            async def receive():
                nonlocal delivered
                if delivered:
                    return {"type": "http.disconnect"}
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}

            request = Request(
                {
                    "type": "http",
                    "http_version": "1.1",
                    "method": "POST",
                    "scheme": "http",
                    "path": "/agentic/runs",
                    "raw_path": b"/agentic/runs",
                    "query_string": b"",
                    "headers": [
                        (b"content-type", b"application/json"),
                        (
                            b"idempotency-key",
                            f"synthetic-admission-{index:05d}".encode("ascii"),
                        ),
                    ],
                    "client": ("127.0.0.1", 1),
                    "server": ("127.0.0.1", 5055),
                },
                receive,
            )
            response = asyncio.run(
                workflows.create_run(
                    request=request,
                    current_user=AuthUser(
                        username="synthetic-user",
                        role="user",
                    ),
                    workspace_id="synthetic-workspace",
                )
            )
            if response.status_code != 202:
                raise RuntimeError(f"synthetic run admission failed: {response.status_code}")
            return response

        with (
            _environment({"PERF_RUN_PREFLIGHT_CACHE_ENABLED": "true"}),
            patch.object(workflows, "_get_runtime", lambda: admission_runtime),
            patch.object(workflows, "_get_orchestrator", lambda: orchestrator),
            patch.object(
                workflows,
                "_get_stores",
                lambda: (chat_store, execution_store),
            ),
            patch.object(
                ontologies_module,
                "get_workspace_ontologies_root",
                lambda _workspace_id=None: ontology_root,
            ),
            patch.object(
                ontologies_module,
                "get_runtime_ontology_manifest",
                load_manifest,
            ),
            patch.object(
                ontology_catalog,
                "build_root_payload",
                load_root_payload,
            ),
            patch.object(
                ontology_view_cache,
                "build_package_revision_source_signature",
                load_source_signature,
            ),
            patch(
                "config.system_config.resolve_effective_llm_profile_config",
                lambda *_args, **_kwargs: load_profile(),
            ),
            patch(
                "config.system_config.get_system_config_generation",
                lambda: 1,
            ),
            patch(
                "workspace.workspaces.get_workspace_config_generation",
                lambda _workspace_id: 1,
            ),
        ):
            admit_run()
            preflight_latencies = [_timed(admit_run)[1] for _ in range(samples)]

        report: dict[str, Any] = {
            "schema_version": 1,
            "synthetic_data": True,
            "parameters": {
                "samples": samples,
                "session_count": session_count,
                "page_limit": page_limit,
            },
            "thresholds_ms": {
                "create_session_p95": float(create_session_p95_ms),
                "first_page_p95": float(first_page_p95_ms),
                "create_run_preflight_p95": float(create_run_preflight_p95_ms),
            },
            "metrics": {
                "create_session": _latency_metric(create_latencies),
                "first_page_sessions": _latency_metric(list_latencies),
                "create_run_preflight": _latency_metric(preflight_latencies),
            },
            "counters": {
                "first_page_detail_reads_max": max(detail_reads_per_call or [0]),
                "page_limit": page_limit,
                "preflight_package_loads": package_loads[0],
                "preflight_requests": samples + 1,
                "preflight_profile_loads": profile_loads[0],
                "preflight_fact_checks": preflight_fact_checks[0],
                "preflight_manifest_reads": manifest_reads[0],
                "preflight_root_payload_loads": root_payload_loads[0],
                "preflight_source_signature_traversals": source_signature_traversals[0],
                "manager_create_calls": manager_create_calls[0],
                "admitted_turns": admitted_turns[0],
            },
        }
        violations = evaluate_thresholds(report)
        report["violations"] = violations
        report["passed"] = not violations
        return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=25)
    parser.add_argument("--session-count", type=int, default=1000)
    parser.add_argument("--page-limit", type=int, default=50)
    parser.add_argument("--create-session-p95-ms", type=float, default=500.0)
    parser.add_argument("--first-page-p95-ms", type=float, default=300.0)
    parser.add_argument("--create-run-preflight-p95-ms", type=float, default=500.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_result_logging()
    args = _parser().parse_args(argv)
    report = run_benchmark(
        samples=args.samples,
        session_count=args.session_count,
        page_limit=args.page_limit,
        create_session_p95_ms=args.create_session_p95_ms,
        first_page_p95_ms=args.first_page_p95_ms,
        create_run_preflight_p95_ms=args.create_run_preflight_p95_ms,
    )
    result_logger.info("%s", json.dumps(report, ensure_ascii=True, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
