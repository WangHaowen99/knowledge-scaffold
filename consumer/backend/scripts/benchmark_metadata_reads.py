"""Machine-readable performance gate for consumer metadata reads.

The benchmark uses only synthetic temporary data. It exits non-zero when a
warm latency, authoritative traversal, or frontend request-count gate fails.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import tempfile
import time
from typing import Any, Callable
from unittest.mock import patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parents[1]
FRONTEND_ROOT = REPOSITORY_ROOT / "consumer" / "frontend"
FRONTEND_CONTRACT_SCRIPT = FRONTEND_ROOT / "scripts" / "benchmark-ontology-requests.mjs"


@dataclass
class IoCounts:
    open: int = 0
    stat: int = 0
    traversal: int = 0

    def __sub__(self, other: "IoCounts") -> "IoCounts":
        return IoCounts(
            open=self.open - other.open,
            stat=self.stat - other.stat,
            traversal=self.traversal - other.traversal,
        )


class FilesystemProbe:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).absolute()
        self.counts = IoCounts()
        self._stack = ExitStack()

    def _tracked(self, value: object) -> bool:
        try:
            Path(os.fspath(value)).absolute().relative_to(self.root)
            return True
        except (TypeError, ValueError):
            return False

    def __enter__(self) -> "FilesystemProbe":
        original_open = Path.open
        original_stat = Path.stat
        original_lstat = Path.lstat
        original_iterdir = Path.iterdir
        original_os_open = os.open

        def tracked_open(path: Path, *args, **kwargs):
            if self._tracked(path):
                self.counts.open += 1
            return original_open(path, *args, **kwargs)

        def tracked_stat(path: Path, *args, **kwargs):
            if self._tracked(path):
                self.counts.stat += 1
            return original_stat(path, *args, **kwargs)

        def tracked_lstat(path: Path, *args, **kwargs):
            if self._tracked(path):
                self.counts.stat += 1
            return original_lstat(path, *args, **kwargs)

        def tracked_iterdir(path: Path, *args, **kwargs):
            if self._tracked(path):
                self.counts.traversal += 1
            return original_iterdir(path, *args, **kwargs)

        def tracked_os_open(path, *args, **kwargs):
            if self._tracked(path):
                self.counts.open += 1
            return original_os_open(path, *args, **kwargs)

        self._stack.enter_context(patch.object(Path, "open", tracked_open))
        self._stack.enter_context(patch.object(Path, "stat", tracked_stat))
        self._stack.enter_context(patch.object(Path, "lstat", tracked_lstat))
        self._stack.enter_context(patch.object(Path, "iterdir", tracked_iterdir))
        self._stack.enter_context(patch.object(os, "open", tracked_os_open))
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._stack.close()

    def snapshot(self) -> IoCounts:
        return IoCounts(**asdict(self.counts))


def percentile(values: list[float], percentile_value: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    index = max(0, math.ceil((percentile_value / 100.0) * len(ordered)) - 1)
    return ordered[min(index, len(ordered) - 1)]


@contextmanager
def _environment_overrides(values: dict[str, str]):
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


def _timed(call: Callable[[], Any]) -> tuple[Any, float]:
    started = time.perf_counter()
    result = call()
    return result, max(0.0, (time.perf_counter() - started) * 1000.0)


def _exercise(
    cold_call: Callable[[], Any],
    warm_call: Callable[[], Any],
    *,
    samples: int,
    probe: FilesystemProbe,
    authoritative_count: Callable[[], int],
) -> dict[str, Any]:
    before_cold_io = probe.snapshot()
    before_cold_traversals = authoritative_count()
    _, cold_ms = _timed(cold_call)
    cold_io = probe.snapshot() - before_cold_io
    cold_authoritative = authoritative_count() - before_cold_traversals

    before_warm_io = probe.snapshot()
    before_warm_traversals = authoritative_count()
    warm_latencies = [_timed(warm_call)[1] for _ in range(samples)]
    warm_io = probe.snapshot() - before_warm_io
    warm_authoritative = authoritative_count() - before_warm_traversals
    return {
        "cold_ms": round(cold_ms, 3),
        "warm_p50_ms": round(percentile(warm_latencies, 50), 3),
        "warm_p95_ms": round(percentile(warm_latencies, 95), 3),
        "cold_io": asdict(cold_io),
        "warm_io": asdict(warm_io),
        "cold_authoritative_traversals": cold_authoritative,
        "warm_authoritative_traversals": warm_authoritative,
    }


def _write_synthetic_workspaces(runtime_root: Path, count: int) -> None:
    workspaces_root = runtime_root / "workspaces"
    workspaces_root.mkdir(parents=True)
    (runtime_root / "workspace-settings.json").write_text(
        json.dumps({"default_workspace_id": "workspace-000"}),
        encoding="utf-8",
    )
    for index in range(count):
        workspace_id = f"workspace-{index:03d}"
        directory = workspaces_root / workspace_id
        directory.mkdir()
        (directory / "workspace.json").write_text(
            json.dumps(
                {
                    "id": workspace_id,
                    "name": f"Synthetic Workspace {index:03d}",
                    "description": "Synthetic benchmark data",
                    "mcp_endpoint_id": f"{index:016x}",
                    "created_at": "2026-08-31T00:00:00+00:00",
                    "updated_at": "2026-08-31T00:00:00+00:00",
                    "owner": "synthetic-operator",
                    "members": [],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )


def _write_synthetic_ontology(base_dir: Path, file_count: int) -> tuple[Path, Path]:
    ontology_dir = base_dir / "ontologies" / "synthetic-ontology"
    version_dir = ontology_dir / "versions" / "1.0.0"
    objects_dir = version_dir / "objects"
    operations_dir = version_dir / "operations"
    objects_dir.mkdir(parents=True)
    operations_dir.mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        """kind: ontology-package
schema_version: 0.1
id: synthetic-ontology
name: Synthetic Ontology
description: Synthetic benchmark package
version: 1.0.0
active_version: 1.0.0
versions:
  - 1.0.0
language: zh-CN
enabled: true
""",
        encoding="utf-8",
    )
    for index in range(file_count):
        target = objects_dir if index % 2 == 0 else operations_dir
        (target / f"item-{index:04d}.md").write_text(
            f"# Synthetic item {index}\n",
            encoding="utf-8",
        )
    return ontology_dir, version_dir


def _frontend_request_contract(
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, int]:
    completed = runner(
        ["node", str(FRONTEND_CONTRACT_SCRIPT)],
        cwd=str(FRONTEND_ROOT),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    output_lines = [line for line in str(completed.stdout or "").splitlines() if line.strip()]
    if not output_lines:
        raise RuntimeError("frontend request contract probe returned no JSON")
    payload = json.loads(output_lines[-1])
    result: dict[str, int] = {}
    for field_name in ("ontology_list_requests", "non_knowledge_tree_requests"):
        value = payload.get(field_name) if isinstance(payload, dict) else None
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise RuntimeError(f"frontend request contract field is invalid: {field_name}")
        result[field_name] = value
    return result


def evaluate_thresholds(report: dict[str, Any]) -> list[dict[str, Any]]:
    thresholds = report["thresholds_ms"]
    metrics = report["metrics"]
    violations: list[dict[str, Any]] = []
    for metric_name, threshold_name in (
        ("workspace_list", "workspace_warm_p95"),
        ("ontology_list", "ontology_warm_p95"),
        ("ontology_tree", "ontology_warm_p95"),
    ):
        actual = metrics[metric_name]["warm_p95_ms"]
        limit = thresholds[threshold_name]
        if actual >= limit:
            violations.append(
                {
                    "code": "warm_p95_exceeded",
                    "metric": metric_name,
                    "actual": actual,
                    "limit": limit,
                }
            )
        traversals = metrics[metric_name]["warm_authoritative_traversals"]
        if traversals > 0:
            violations.append(
                {
                    "code": "warm_authoritative_traversal",
                    "metric": metric_name,
                    "actual": traversals,
                    "limit": 0,
                }
            )
        warm_io = metrics[metric_name].get("warm_io") or {}
        filesystem_traversals = int(warm_io.get("traversal") or 0)
        if filesystem_traversals > 0:
            violations.append(
                {
                    "code": "warm_filesystem_traversal",
                    "metric": metric_name,
                    "actual": filesystem_traversals,
                    "limit": 0,
                }
            )
    frontend = report["frontend_contract"]
    if frontend["ontology_list_requests"] > 1:
        violations.append(
            {
                "code": "ontology_list_request_fanout",
                "metric": "ontology_list_requests",
                "actual": frontend["ontology_list_requests"],
                "limit": 1,
            }
        )
    if frontend["non_knowledge_tree_requests"] > 0:
        violations.append(
            {
                "code": "eager_tree_request",
                "metric": "non_knowledge_tree_requests",
                "actual": frontend["non_knowledge_tree_requests"],
                "limit": 0,
            }
        )
    return violations


def run_benchmark(
    *,
    samples: int = 25,
    workspace_count: int = 8,
    package_files: int = 40,
    storage_label: str = "local",
    workspace_warm_p95_ms: float = 200.0,
    ontology_warm_p95_ms: float = 300.0,
) -> dict[str, Any]:
    import ontology.ontologies as ontologies
    import ontology.view_cache as ontology_views
    from ontology.package_browser import build_package_tree
    import workspace.workspaces as workspaces

    samples = max(5, int(samples))
    with tempfile.TemporaryDirectory(prefix="consumer-metadata-benchmark-") as temporary:
        root = Path(temporary)
        runtime_root = root / "runtime"
        ontology_base = root / "package-root"
        _write_synthetic_workspaces(runtime_root, max(1, int(workspace_count)))
        ontology_dir, version_dir = _write_synthetic_ontology(
            ontology_base,
            max(1, int(package_files)),
        )
        workspace_loads = [0]
        original_workspace_loader = workspaces._load_workspace_catalog_data

        def load_workspace_catalog(root_path):
            workspace_loads[0] += 1
            return original_workspace_loader(root_path)

        registry_refreshes = [0]
        original_registry_refresh = ontologies.OntologyRegistry.refresh

        def refresh_registry(registry):
            registry_refreshes[0] += 1
            return original_registry_refresh(registry)

        def read_registry():
            return ontologies.get_ontology_registry(base_dir=ontology_base).ontologies

        tree_loads = [0]

        def load_tree():
            tree_loads[0] += 1
            return build_package_tree(version_dir, trusted_root=ontology_dir)

        def read_tree():
            return ontology_views.get_package_tree_cached(
                workspace_id="workspace-000",
                ontology_id="synthetic-ontology",
                version="1.0.0",
                loader=load_tree,
                signature_loader=lambda: ontology_views.build_package_tree_source_signature(version_dir),
            )

        with (
            _environment_overrides(
                {
                    "PROJECT_ROOT": str(runtime_root),
                    "PERF_WORKSPACE_SNAPSHOT_ENABLED": "true",
                    "PERF_ONTOLOGY_VIEW_CACHE_ENABLED": "true",
                }
            ),
            patch.object(workspaces, "_workspace_catalog_manager", None),
            patch.object(workspaces, "_workspace_catalog_manager_root", None),
            patch.object(workspaces, "_workspace_catalog_last_generation", 0),
            patch.object(
                workspaces,
                "_load_workspace_catalog_data",
                load_workspace_catalog,
            ),
            patch.object(ontologies, "_registries", {}),
            patch.object(ontologies, "_global_registry", None),
            patch.object(ontologies.OntologyRegistry, "refresh", refresh_registry),
            patch.object(
                ontology_views,
                "_view_cache",
                ontology_views.OntologyViewCache(
                    max_entries=8,
                    check_interval_seconds=60.0,
                ),
            ),
            FilesystemProbe(root) as probe,
        ):
            workspace_metrics = _exercise(
                workspaces.list_workspaces,
                workspaces.list_workspaces,
                samples=samples,
                probe=probe,
                authoritative_count=lambda: workspace_loads[0],
            )
            ontology_list_metrics = _exercise(
                read_registry,
                read_registry,
                samples=samples,
                probe=probe,
                authoritative_count=lambda: registry_refreshes[0],
            )
            ontology_tree_metrics = _exercise(
                read_tree,
                read_tree,
                samples=samples,
                probe=probe,
                authoritative_count=lambda: tree_loads[0],
            )

        report = {
            "schema_version": "consumer.metadata_reads.v1",
            "environment": {
                "platform": platform.system().lower(),
                "python": platform.python_version(),
                "storage_label": str(storage_label or "local"),
            },
            "synthetic_fixture": {
                "workspace_count": max(1, int(workspace_count)),
                "package_file_count": max(1, int(package_files)),
                "samples": samples,
            },
            "thresholds_ms": {
                "workspace_warm_p95": float(workspace_warm_p95_ms),
                "ontology_warm_p95": float(ontology_warm_p95_ms),
            },
            "metrics": {
                "workspace_list": workspace_metrics,
                "ontology_list": ontology_list_metrics,
                "ontology_tree": ontology_tree_metrics,
            },
            "frontend_contract": _frontend_request_contract(),
        }
        report["violations"] = evaluate_thresholds(report)
        report["passed"] = not report["violations"]

    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=25)
    parser.add_argument("--workspace-count", type=int, default=8)
    parser.add_argument("--package-files", type=int, default=40)
    parser.add_argument("--storage-label", default="local")
    parser.add_argument("--workspace-warm-p95-ms", type=float, default=200.0)
    parser.add_argument("--ontology-warm-p95-ms", type=float, default=300.0)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = run_benchmark(
        samples=args.samples,
        workspace_count=args.workspace_count,
        package_files=args.package_files,
        storage_label=args.storage_label,
        workspace_warm_p95_ms=args.workspace_warm_p95_ms,
        ontology_warm_p95_ms=args.ontology_warm_p95_ms,
    )
    encoded = json.dumps(report, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
