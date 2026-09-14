from __future__ import annotations

import json
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from operator import getitem
from pathlib import Path
from types import SimpleNamespace

import pytest
from ontology.view_cache import (
    OntologyViewCache,
    get_ontology_mutation_revision,
    get_package_tree_cached,
    invalidate_ontology_views,
)
from starlette.requests import Request


def test_package_tree_cache_reuses_build_and_returns_defensive_copies(monkeypatch):
    import ontology.view_cache as view_cache_module

    cache = OntologyViewCache(max_entries=8)
    monkeypatch.setattr(view_cache_module, "_view_cache", cache)
    calls = 0

    def loader():
        nonlocal calls
        calls += 1
        return [{"name": "objects", "children": [{"name": "demo.md"}]}]

    first = get_package_tree_cached(
        workspace_id="workspace-cache-a",
        ontology_id="ontology-a",
        version="1.0.0",
        loader=loader,
    )
    first[0]["children"][0]["name"] = "mutated.md"
    second = get_package_tree_cached(
        workspace_id="workspace-cache-a",
        ontology_id="ontology-a",
        version="1.0.0",
        loader=loader,
    )

    assert calls == 1
    assert second == [{"name": "objects", "children": [{"name": "demo.md"}]}]


def test_package_tree_cache_keys_include_workspace_ontology_version_and_revision(
    monkeypatch,
):
    import ontology.view_cache as view_cache_module

    cache = OntologyViewCache(max_entries=8)
    monkeypatch.setattr(view_cache_module, "_view_cache", cache)
    calls: list[tuple[str, str, str]] = []

    def read(workspace_id: str, ontology_id: str, version: str):
        def loader():
            calls.append((workspace_id, ontology_id, version))
            return [{"name": "{}:{}:{}".format(workspace_id, ontology_id, version)}]

        return get_package_tree_cached(
            workspace_id=workspace_id,
            ontology_id=ontology_id,
            version=version,
            loader=loader,
        )

    read("workspace-cache-a", "ontology-a", "1.0.0")
    read("workspace-cache-b", "ontology-a", "1.0.0")
    read("workspace-cache-a", "ontology-b", "1.0.0")
    read("workspace-cache-a", "ontology-a", "2.0.0")
    before_revision = get_ontology_mutation_revision(
        "workspace-cache-a",
        "ontology-a",
    )
    after_revision = invalidate_ontology_views(
        workspace_id="workspace-cache-a",
        ontology_id="ontology-a",
    )
    read("workspace-cache-a", "ontology-a", "1.0.0")

    assert len(calls) == 5
    assert after_revision > before_revision


def test_package_tree_cache_single_flight_shares_one_build(monkeypatch):
    import ontology.view_cache as view_cache_module

    cache = OntologyViewCache(max_entries=8)
    monkeypatch.setattr(view_cache_module, "_view_cache", cache)
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def loader():
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(timeout=2.0)
        return [{"name": "shared"}]

    def read():
        return get_package_tree_cached(
            workspace_id="workspace-single-flight",
            ontology_id="ontology-single-flight",
            version="1.0.0",
            loader=loader,
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(read) for _ in range(4)]
        assert entered.wait(timeout=1.0)
        release.set()
        results = [future.result(timeout=2.0) for future in futures]

    assert calls == 1
    assert results == [[{"name": "shared"}]] * 4


def test_package_tree_cache_rebuilds_after_throttled_external_signature_change(
    monkeypatch,
):
    import ontology.view_cache as view_cache_module

    now = [10.0]
    signature = ["signature-a"]
    calls = 0
    cache = OntologyViewCache(
        max_entries=8,
        check_interval_seconds=1.5,
        clock=lambda: now[0],
    )
    monkeypatch.setattr(view_cache_module, "_view_cache", cache)

    def loader():
        nonlocal calls
        calls += 1
        return [{"name": "tree-{}".format(calls)}]

    def read():
        return get_package_tree_cached(
            workspace_id="workspace-external-drift",
            ontology_id="ontology-external-drift",
            version="1.0.0",
            loader=loader,
            signature_loader=lambda: signature[0],
        )

    assert read() == [{"name": "tree-1"}]
    signature[0] = "signature-b"
    now[0] += 1.4
    assert read() == [{"name": "tree-1"}]
    now[0] += 0.2
    assert read() == [{"name": "tree-2"}]
    assert calls == 2


def test_package_tree_cache_retries_when_source_changes_during_build(monkeypatch):
    import ontology.view_cache as view_cache_module

    cache = OntologyViewCache(max_entries=8, check_interval_seconds=60)
    monkeypatch.setattr(view_cache_module, "_view_cache", cache)
    signature = ["signature-1"]
    loader_calls = 0

    def loader():
        nonlocal loader_calls
        loader_calls += 1
        if loader_calls == 1:
            signature[0] = "signature-2"
            return [{"name": "mixed-tree-must-not-publish"}]
        return [{"name": "stable-tree"}]

    def read():
        return get_package_tree_cached(
            workspace_id="workspace-build-race",
            ontology_id="ontology-build-race",
            version="1.0.0",
            loader=loader,
            signature_loader=lambda: signature[0],
        )

    assert read() == [{"name": "stable-tree"}]
    assert read() == [{"name": "stable-tree"}]
    assert loader_calls == 2


def test_package_tree_cache_fails_after_bounded_continuous_build_drift(monkeypatch):
    import ontology.view_cache as view_cache_module

    cache = OntologyViewCache(max_entries=8, check_interval_seconds=60)
    monkeypatch.setattr(view_cache_module, "_view_cache", cache)
    signature = [0]
    loader_calls = 0

    def loader():
        nonlocal loader_calls
        loader_calls += 1
        signature[0] += 1
        return [{"name": f"unstable-{loader_calls}"}]

    with pytest.raises(
        view_cache_module.OntologyPackageRevisionError,
        match="changed during package tree build",
    ):
        get_package_tree_cached(
            workspace_id="workspace-continuous-drift",
            ontology_id="ontology-continuous-drift",
            version="1.0.0",
            loader=loader,
            signature_loader=lambda: signature[0],
        )

    assert 1 < loader_calls <= 5


def test_revision_source_observer_bumps_once_after_throttled_external_change():
    now = [10.0]
    signature = ["revision-a"]
    signature_reads = []
    cache = OntologyViewCache(
        max_entries=8,
        check_interval_seconds=1.5,
        clock=lambda: now[0],
    )

    def observe():
        return cache.observe_revision_source(
            workspace_id="workspace-revision",
            ontology_id="ontology-revision",
            signature_loader=lambda: signature_reads.append(signature[0]) or signature[0],
        )

    assert observe() == 0
    signature[0] = "revision-b"
    now[0] += 1.4
    assert observe() == 0
    now[0] += 0.2
    assert observe() == 1
    assert observe() == 1
    assert signature_reads == ["revision-a", "revision-b"]


def test_disabled_package_tree_cache_calls_validated_loader_on_every_read(
    monkeypatch,
):
    import ontology.view_cache as view_cache_module

    monkeypatch.setenv("PERF_ONTOLOGY_VIEW_CACHE_ENABLED", "false")
    monkeypatch.setattr(view_cache_module, "_view_cache", OntologyViewCache(max_entries=8))
    calls = 0

    def loader():
        nonlocal calls
        calls += 1
        return [{"name": "tree-{}".format(calls)}]

    first = get_package_tree_cached(
        workspace_id="workspace-disabled-cache",
        ontology_id="ontology-disabled-cache",
        version="1.0.0",
        loader=loader,
    )
    second = get_package_tree_cached(
        workspace_id="workspace-disabled-cache",
        ontology_id="ontology-disabled-cache",
        version="1.0.0",
        loader=loader,
    )

    assert first == [{"name": "tree-1"}]
    assert second == [{"name": "tree-2"}]
    assert calls == 2


def test_ontology_list_route_does_not_unconditionally_refresh_registry(monkeypatch):
    from api.routes import ontologies as routes

    refresh_calls = 0

    class Registry:
        ontologies = {}

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def refresh(self):
            nonlocal refresh_calls
            refresh_calls += 1

    monkeypatch.setattr(routes, "workspace_scope", lambda _workspace_id: nullcontext())
    monkeypatch.setattr(
        routes,
        "get_ontology_registry",
        lambda *, workspace_id: Registry(),
    )
    monkeypatch.setattr(
        routes,
        "get_business_checkpoint_capabilities",
        lambda *_args, **_kwargs: {},
    )

    response = routes.list_ontologies(workspace_id="workspace-list-cache")

    assert response.status_code == 200
    assert refresh_calls == 0


def test_ontology_tree_route_reuses_cached_validated_tree(monkeypatch, tmp_path):
    import ontology.view_cache as view_cache_module
    from api.routes import ontologies as routes

    ontology_dir = tmp_path / "ontology-a"
    version_dir = ontology_dir / "versions" / "1.0.0"
    version_dir.mkdir(parents=True)
    calls = 0

    def validated_loader(_root, *, trusted_root):
        nonlocal calls
        assert _root == version_dir
        assert trusted_root == ontology_dir
        calls += 1
        return [{"name": "objects", "type": "directory", "children": []}]

    monkeypatch.setattr(view_cache_module, "_view_cache", OntologyViewCache(max_entries=8))
    monkeypatch.setattr(routes, "workspace_scope", lambda _workspace_id: nullcontext())
    monkeypatch.setattr(routes, "_resolve_ontology_dir", lambda _ontology_id: ontology_dir)
    monkeypatch.setattr(
        routes,
        "_resolve_package_browser_version_snapshot",
        lambda _ontology_dir, _version: (_version or "1.0.0", version_dir),
    )
    monkeypatch.setattr(routes, "build_package_tree", validated_loader)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/agentic/ontologies/ontology-a/tree",
            "headers": [],
            "query_string": b"version=1.0.0",
        }
    )

    first = routes.get_ontology_tree(
        "ontology-a",
        request,
        workspace_id="workspace-tree-cache",
    )
    second = routes.get_ontology_tree(
        "ontology-a",
        request,
        workspace_id="workspace-tree-cache",
    )

    assert first.status_code == second.status_code == 200
    assert calls == 1


def test_ontology_tree_route_rebuilds_after_external_package_change(
    monkeypatch,
    tmp_path,
):
    import ontology.view_cache as view_cache_module
    from api.routes import ontologies as routes

    ontology_dir = tmp_path / "ontology-external"
    version_dir = ontology_dir / "versions" / "1.0.0"
    source_file = version_dir / "objects" / "demo.md"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("first", encoding="utf-8")
    calls = 0

    def validated_loader(_root, *, trusted_root):
        nonlocal calls
        assert _root == version_dir
        assert trusted_root == ontology_dir
        calls += 1
        return [{"name": source_file.read_text(encoding="utf-8")}]

    monkeypatch.setattr(
        view_cache_module,
        "_view_cache",
        OntologyViewCache(max_entries=8, check_interval_seconds=0),
    )
    monkeypatch.setattr(routes, "workspace_scope", lambda _workspace_id: nullcontext())
    monkeypatch.setattr(routes, "_resolve_ontology_dir", lambda _ontology_id: ontology_dir)
    monkeypatch.setattr(
        routes,
        "_resolve_package_browser_version_snapshot",
        lambda _ontology_dir, _version: (_version or "1.0.0", version_dir),
    )
    monkeypatch.setattr(routes, "build_package_tree", validated_loader)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/agentic/ontologies/ontology-external/tree",
            "headers": [],
            "query_string": b"version=1.0.0",
        }
    )

    routes.get_ontology_tree(
        "ontology-external",
        request,
        workspace_id="workspace-external-route",
    )
    source_file.write_text("second-version", encoding="utf-8")
    routes.get_ontology_tree(
        "ontology-external",
        request,
        workspace_id="workspace-external-route",
    )

    assert calls == 2


def test_ontology_tree_route_reports_unstable_package_as_retryable(monkeypatch, tmp_path):
    from api.routes import ontologies as routes
    from ontology.view_cache import OntologyPackageRevisionError

    ontology_dir = tmp_path / "ontology-unstable"
    version_dir = ontology_dir / "versions" / "1.0.0"
    version_dir.mkdir(parents=True)
    monkeypatch.setattr(routes, "workspace_scope", lambda _workspace_id: nullcontext())
    monkeypatch.setattr(routes, "_resolve_ontology_dir", lambda _ontology_id: ontology_dir)
    monkeypatch.setattr(
        routes,
        "_resolve_package_browser_version_snapshot",
        lambda _ontology_dir, _version: ("1.0.0", version_dir),
    )
    monkeypatch.setattr(
        routes,
        "get_package_tree_cached",
        lambda **_kwargs: (_ for _ in ()).throw(OntologyPackageRevisionError("synthetic continuous drift")),
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/agentic/ontologies/ontology-unstable/tree",
            "headers": [],
            "query_string": b"version=1.0.0",
        }
    )

    response = routes.get_ontology_tree(
        "ontology-unstable",
        request,
        workspace_id="workspace-unstable-route",
    )

    assert response.status_code == 503
    assert json.loads(response.body) == {
        "error": "Ontology package tree is changing; retry the request",
        "code": "ONTOLOGY_PACKAGE_REVISION_UNSTABLE",
        "retriable": True,
    }


def test_ontology_registry_summary_is_scoped_by_workspace_generation(
    monkeypatch,
    tmp_path,
):
    import ontology.ontologies as ontologies_module

    generation = [10]
    roots = {
        "workspace-registry-a": tmp_path / "workspace-a" / "ontologies",
        "workspace-registry-b": tmp_path / "workspace-b" / "ontologies",
    }
    for root in roots.values():
        root.mkdir(parents=True)

    monkeypatch.setattr(ontologies_module, "_global_registry", None)
    monkeypatch.setattr(ontologies_module, "_registries", {})
    monkeypatch.setattr(
        ontologies_module,
        "get_workspace_ontologies_root",
        lambda workspace_id: getitem(roots, workspace_id),
    )
    monkeypatch.setattr(
        ontologies_module,
        "get_workspace_catalog_generation",
        lambda: generation[0],
        raising=False,
    )

    registry_a_generation_10 = ontologies_module.get_ontology_registry(workspace_id="workspace-registry-a")
    registry_b_generation_10 = ontologies_module.get_ontology_registry(workspace_id="workspace-registry-b")
    assert ontologies_module.get_ontology_registry(workspace_id="workspace-registry-a") is registry_a_generation_10

    generation[0] = 11
    registry_a_generation_11 = ontologies_module.get_ontology_registry(workspace_id="workspace-registry-a")
    registry_b_generation_11 = ontologies_module.get_ontology_registry(workspace_id="workspace-registry-b")

    assert registry_a_generation_11 is not registry_a_generation_10
    assert registry_b_generation_11 is not registry_b_generation_10
    assert registry_a_generation_11 is not registry_b_generation_11


def test_package_tree_cache_is_bounded_lru(monkeypatch):
    import ontology.view_cache as view_cache_module

    cache = OntologyViewCache(max_entries=2)
    monkeypatch.setattr(view_cache_module, "_view_cache", cache)
    calls: dict[str, int] = {}

    def read(ontology_id: str):
        def loader():
            calls[ontology_id] = calls.get(ontology_id, 0) + 1
            return [{"name": ontology_id}]

        return get_package_tree_cached(
            workspace_id="workspace-lru",
            ontology_id=ontology_id,
            version="1.0.0",
            loader=loader,
        )

    read("ontology-a")
    read("ontology-b")
    read("ontology-a")
    read("ontology-c")
    read("ontology-b")

    assert calls == {"ontology-a": 1, "ontology-b": 2, "ontology-c": 1}


def test_workspace_invalidation_evicts_all_ontology_trees(monkeypatch):
    import ontology.view_cache as view_cache_module

    cache = OntologyViewCache(max_entries=8)
    monkeypatch.setattr(view_cache_module, "_view_cache", cache)
    calls: dict[tuple[str, str], int] = {}

    def read(workspace_id: str, ontology_id: str):
        def loader():
            key = (workspace_id, ontology_id)
            calls[key] = calls.get(key, 0) + 1
            return [{"name": "{}:{}".format(*key)}]

        return get_package_tree_cached(
            workspace_id=workspace_id,
            ontology_id=ontology_id,
            version="1.0.0",
            loader=loader,
        )

    read("workspace-wide", "ontology-a")
    read("workspace-wide", "ontology-b")
    read("workspace-other", "ontology-a")
    invalidate_ontology_views(workspace_id="workspace-wide")
    read("workspace-wide", "ontology-a")
    read("workspace-wide", "ontology-b")
    read("workspace-other", "ontology-a")

    assert calls == {
        ("workspace-wide", "ontology-a"): 2,
        ("workspace-wide", "ontology-b"): 2,
        ("workspace-other", "ontology-a"): 1,
    }


def test_initial_signature_failure_still_uses_validated_tree_loader(monkeypatch):
    import ontology.view_cache as view_cache_module
    from ontology.package_browser import PackageBrowserError

    cache = OntologyViewCache(max_entries=8)
    monkeypatch.setattr(view_cache_module, "_view_cache", cache)
    loader_called = False

    def loader():
        nonlocal loader_called
        loader_called = True
        raise PackageBrowserError("not_found", "Package directory not found")

    def failed_signature():
        raise OSError("package changed during resolution")

    with pytest.raises(PackageBrowserError, match="Package directory not found"):
        get_package_tree_cached(
            workspace_id="workspace-signature-error",
            ontology_id="ontology-signature-error",
            version="1.0.0",
            loader=loader,
            signature_loader=failed_signature,
        )

    assert loader_called is True


def test_changed_tree_never_masks_trusted_loader_validation_failure(monkeypatch):
    import ontology.view_cache as view_cache_module
    from ontology.package_browser import PackageBrowserError

    signature = ["safe"]
    unsafe = [False]
    cache = OntologyViewCache(max_entries=8, check_interval_seconds=0)
    monkeypatch.setattr(view_cache_module, "_view_cache", cache)

    def loader():
        if unsafe[0]:
            raise PackageBrowserError("invalid_path", "Symbolic links are not allowed")
        return [{"name": "safe-tree"}]

    def read():
        return get_package_tree_cached(
            workspace_id="workspace-loader-validation",
            ontology_id="ontology-loader-validation",
            version="1.0.0",
            loader=loader,
            signature_loader=lambda: signature[0],
        )

    assert read() == [{"name": "safe-tree"}]
    signature[0] = "unsafe"
    unsafe[0] = True

    with pytest.raises(PackageBrowserError, match="Symbolic links are not allowed"):
        read()


def test_warm_signature_failure_runs_trusted_loader_and_propagates_security_error(
    monkeypatch,
):
    import ontology.view_cache as view_cache_module
    from ontology.package_browser import PackageBrowserError

    cache = OntologyViewCache(max_entries=8, check_interval_seconds=0)
    monkeypatch.setattr(view_cache_module, "_view_cache", cache)
    signature_fails = False
    loader_rejects = False
    loader_calls = 0

    def signature_loader():
        if signature_fails:
            raise OSError("package cannot be inspected")
        return "safe"

    def loader():
        nonlocal loader_calls
        loader_calls += 1
        if loader_rejects:
            raise PackageBrowserError("invalid_path", "Symbolic links are not allowed")
        return [{"name": "safe-tree"}]

    assert get_package_tree_cached(
        workspace_id="workspace-warm-signature-error",
        ontology_id="ontology-warm-signature-error",
        version="1.0.0",
        loader=loader,
        signature_loader=signature_loader,
    ) == [{"name": "safe-tree"}]

    signature_fails = True
    loader_rejects = True
    with pytest.raises(PackageBrowserError, match="Symbolic links are not allowed"):
        get_package_tree_cached(
            workspace_id="workspace-warm-signature-error",
            ontology_id="ontology-warm-signature-error",
            version="1.0.0",
            loader=loader,
            signature_loader=signature_loader,
        )

    assert loader_calls == 2


def test_tree_route_uses_one_manifest_snapshot_for_cache_version_and_directory(
    monkeypatch,
    tmp_path,
):
    import ontology.view_cache as view_cache_module
    from api.routes import ontologies as routes

    ontology_dir = tmp_path / "ontology-race"
    version_one = ontology_dir / "versions" / "1.0.0"
    version_two = ontology_dir / "versions" / "2.0.0"
    version_one.mkdir(parents=True)
    version_two.mkdir(parents=True)
    (version_one / "old.md").write_text("old", encoding="utf-8")
    (version_two / "new.md").write_text("new", encoding="utf-8")
    (ontology_dir / "manifest.yaml").write_text(
        "schema_version: 0.1\nversion: 1.0.0\nactive_version: 1.0.0\nversions:\n  - 1.0.0\n  - 2.0.0\n",
        encoding="utf-8",
    )
    manifests = (
        {"version": "1.0.0", "active_version": "1.0.0"},
        {"version": "2.0.0", "active_version": "2.0.0"},
    )
    manifest_reads = 0

    def load_manifest(_ontology_dir):
        nonlocal manifest_reads
        manifest = manifests[min(manifest_reads, len(manifests) - 1)]
        manifest_reads += 1
        return dict(manifest)

    monkeypatch.setattr(
        view_cache_module,
        "_view_cache",
        OntologyViewCache(max_entries=8, check_interval_seconds=60),
    )
    monkeypatch.setattr(routes, "workspace_scope", lambda _workspace_id: nullcontext())
    monkeypatch.setattr(routes, "_resolve_ontology_dir", lambda _ontology_id: ontology_dir)
    monkeypatch.setattr(routes, "load_ontology_manifest_data", load_manifest)

    first_request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/agentic/ontologies/ontology-race/tree",
            "headers": [],
            "query_string": b"",
        }
    )
    second_request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/agentic/ontologies/ontology-race/tree",
            "headers": [],
            "query_string": b"version=2.0.0",
        }
    )

    first = routes.get_ontology_tree(
        "ontology-race",
        first_request,
        workspace_id="workspace-version-race",
    )
    second = routes.get_ontology_tree(
        "ontology-race",
        second_request,
        workspace_id="workspace-version-race",
    )

    assert first.status_code == 200, first.body
    assert second.status_code == 200, second.body
    first_entries = json.loads(first.body)["entries"]
    second_entries = json.loads(second.body)["entries"]
    assert [entry["name"] for entry in first_entries] == ["old.md"]
    assert [entry["name"] for entry in second_entries] == ["new.md"]
    assert manifest_reads == 2


def test_package_tree_signature_ignores_every_invisible_package_entry(
    tmp_path,
    monkeypatch,
):
    from ontology.view_cache import build_package_tree_source_signature

    root = tmp_path / "package"
    root.mkdir()
    (root / "visible.md").write_text("visible", encoding="utf-8")
    invisible_files = (
        root / ".hidden.md",
        root / "datasets" / "data.csv",
        root / "origin_data" / "source.csv",
        root / "__pycache__" / "module.pyc",
    )
    for path in invisible_files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("first", encoding="utf-8")
    symlink_path = root / "linked.md"
    symlink_path.write_bytes(b"")
    original_lstat = Path.lstat

    def fake_lstat(path):
        result = original_lstat(path)
        if path != symlink_path:
            return result
        return SimpleNamespace(
            st_mode=stat.S_IFLNK,
            st_mtime_ns=result.st_mtime_ns,
            st_size=result.st_size,
        )

    monkeypatch.setattr(Path, "lstat", fake_lstat)

    before = build_package_tree_source_signature(root)
    visible_paths = {str(row[1]) for row in before}
    assert "visible.md" in visible_paths
    assert ".hidden.md" not in visible_paths
    assert not any(path.startswith("datasets/") for path in visible_paths)
    assert not any(path.startswith("origin_data/") for path in visible_paths)
    assert not any(path.startswith("__pycache__/") for path in visible_paths)
    assert "linked.md" not in visible_paths

    for path in invisible_files:
        path.write_text("second-version", encoding="utf-8")
    after = build_package_tree_source_signature(root)

    assert after == before


def test_package_tree_signature_tracks_only_visible_tree_shape(tmp_path):
    from ontology.view_cache import build_package_tree_source_signature

    root = tmp_path / "package"
    visible_dir = root / "objects"
    visible_file = visible_dir / "demo.md"
    visible_dir.mkdir(parents=True)
    visible_file.write_text("alpha", encoding="utf-8")
    before = build_package_tree_source_signature(root)

    root_stat = root.stat()
    directory_stat = visible_dir.stat()
    os.utime(
        root,
        ns=(root_stat.st_atime_ns, root_stat.st_mtime_ns + 1_000_000_000),
    )
    os.utime(
        visible_dir,
        ns=(
            directory_stat.st_atime_ns,
            directory_stat.st_mtime_ns + 1_000_000_000,
        ),
    )
    visible_file.write_text("bravo", encoding="utf-8")
    hidden_file = root / ".new-hidden.md"
    hidden_file.write_text("hidden", encoding="utf-8")
    hidden_dir = root / ".new-hidden-dir"
    hidden_dir.mkdir()
    (hidden_dir / "nested.md").write_text("hidden", encoding="utf-8")

    assert build_package_tree_source_signature(root) == before

    hidden_file.unlink()
    (hidden_dir / "nested.md").unlink()
    hidden_dir.rmdir()
    assert build_package_tree_source_signature(root) == before

    visible_file.write_text("longer", encoding="utf-8")
    assert build_package_tree_source_signature(root) != before


def test_package_revision_signature_tracks_same_size_metadata_edits(tmp_path):
    from ontology.view_cache import build_package_revision_source_signature

    root = tmp_path / "package"
    script = root / "scripts" / "run.py"
    runtime_data = root / "datasets" / "live.csv"
    script.parent.mkdir(parents=True)
    runtime_data.parent.mkdir(parents=True)
    script.write_text("alpha", encoding="utf-8")
    runtime_data.write_text("first", encoding="utf-8")
    before = build_package_revision_source_signature(root)

    stat_before = script.stat()
    script.write_text("bravo", encoding="utf-8")
    os.utime(
        script,
        ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns + 1_000_000_000),
    )
    after_script_edit = build_package_revision_source_signature(root)
    assert after_script_edit != before

    runtime_data.write_text("second", encoding="utf-8")
    assert build_package_revision_source_signature(root) == after_script_edit


def test_package_content_revision_detects_same_size_restored_mtime_edit(tmp_path):
    import ontology.view_cache as view_cache_module

    build_content_revision = getattr(
        view_cache_module,
        "build_package_content_revision",
        None,
    )
    assert callable(build_content_revision), "content revision builder is required"

    root = tmp_path / "package"
    operation = root / "operations" / "route.json"
    operation.parent.mkdir(parents=True)
    operation.write_text('{"mode":"alpha"}', encoding="utf-8")
    before = build_content_revision(root)
    source_signature = view_cache_module.build_package_revision_source_signature(root)
    original_stat = operation.stat()

    operation.write_text('{"mode":"bravo"}', encoding="utf-8")
    os.utime(
        operation,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )

    assert operation.stat().st_size == original_stat.st_size
    assert view_cache_module.build_package_revision_source_signature(root) == source_signature
    assert build_content_revision(root) != before


def test_verified_package_staging_is_scoped_filtered_and_cleaned(tmp_path):
    import ontology.view_cache as view_cache_module

    stage_package = getattr(
        view_cache_module,
        "stage_verified_package_revision",
        None,
    )
    assert callable(stage_package), "verified package staging is required"

    project_root = tmp_path / "workspace"
    package_root = project_root / "ontologies" / "synthetic"
    script = package_root / "scripts" / "run.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('admitted')\n", encoding="utf-8")
    (package_root / "datasets").mkdir()
    (package_root / "datasets" / "live.csv").write_text(
        "private-runtime-data",
        encoding="utf-8",
    )
    (package_root / ".private").mkdir()
    (package_root / ".private" / "secret.txt").write_text(
        "hidden",
        encoding="utf-8",
    )
    admitted_revision = view_cache_module.build_package_content_revision(package_root)

    with stage_package(
        source_root=package_root,
        expected_content_revision=admitted_revision,
        staging_root=project_root,
        revision_guard=lambda: True,
    ) as staged_root:
        staged_root = Path(staged_root)
        observed_staged_root = staged_root
        assert staged_root != package_root
        assert staged_root.resolve().is_relative_to(project_root.resolve())
        assert staged_root.name == "ontology"
        assert staged_root.parent.parent == (project_root / ".ontology-run-staging")
        assert (staged_root / "scripts" / "run.py").read_text(encoding="utf-8") == "print('admitted')\n"
        assert not (staged_root / "datasets").exists()
        assert not (staged_root / ".private").exists()
        assert view_cache_module.build_package_content_revision(staged_root) == (admitted_revision)
        replaced_source = package_root.with_name("synthetic-replaced")
        package_root.rename(replaced_source)
        package_root.mkdir()
        (package_root / "replacement.txt").write_text("new", encoding="utf-8")
        assert (staged_root / "scripts" / "run.py").read_text(encoding="utf-8") == "print('admitted')\n"

    assert not observed_staged_root.exists()


def test_package_content_revision_ignores_dynamic_and_arbitrary_hidden_files(
    tmp_path,
):
    from ontology.view_cache import build_package_content_revision

    root = tmp_path / "package"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "run.py").write_text("alpha", encoding="utf-8")
    ignored = (
        root / "datasets" / "live.csv",
        root / "origin_data" / "source.csv",
        root / ".private.json",
    )
    for path in ignored:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("first", encoding="utf-8")
    before = build_package_content_revision(root)

    for path in ignored:
        path.write_text("second", encoding="utf-8")

    assert build_package_content_revision(root) == before
    lock_path = root / ".deterministic-lock.json"
    lock_path.write_text('{"revision":"one"}', encoding="utf-8")
    with_lock = build_package_content_revision(root)
    assert with_lock != before
    lock_path.write_text('{"revision":"two"}', encoding="utf-8")
    assert build_package_content_revision(root) != with_lock


@pytest.mark.parametrize(
    ("unsafe_mode", "message"),
    [(stat.S_IFLNK, "symbolic links"), (stat.S_IFIFO, "special files")],
)
def test_package_content_revision_rejects_unsafe_visible_entries(
    tmp_path,
    monkeypatch,
    unsafe_mode,
    message,
):
    import ontology.view_cache as view_cache_module

    root = tmp_path / "package"
    root.mkdir()
    unsafe_path = root / "unsafe.py"
    unsafe_path.write_text("placeholder", encoding="utf-8")
    original_lstat = Path.lstat

    def fake_lstat(path):
        result = original_lstat(path)
        if path != unsafe_path:
            return result
        return SimpleNamespace(
            st_mode=unsafe_mode,
            st_mtime_ns=result.st_mtime_ns,
            st_size=result.st_size,
        )

    monkeypatch.setattr(Path, "lstat", fake_lstat)

    with pytest.raises(view_cache_module.OntologyPackageRevisionError, match=message):
        view_cache_module.build_package_content_revision(root)


def test_package_tree_signature_ignores_fifo_and_socket_entries(
    tmp_path,
    monkeypatch,
):
    from ontology.view_cache import build_package_tree_source_signature

    root = tmp_path / "package"
    root.mkdir()
    (root / "visible.md").write_text("visible", encoding="utf-8")
    before = build_package_tree_source_signature(root)

    special_modes = {
        root / "events.fifo": stat.S_IFIFO,
        root / "events.socket": stat.S_IFSOCK,
    }
    for path in special_modes:
        path.write_bytes(b"")
    original_lstat = Path.lstat

    def fake_lstat(path):
        result = original_lstat(path)
        special_mode = special_modes.get(path)
        if special_mode is None:
            return result
        return SimpleNamespace(
            st_mode=special_mode,
            st_mtime_ns=result.st_mtime_ns,
            st_size=result.st_size,
        )

    monkeypatch.setattr(Path, "lstat", fake_lstat)

    assert build_package_tree_source_signature(root) == before


def test_deleted_and_recreated_workspace_does_not_reuse_cached_package_tree(
    monkeypatch,
    tmp_path,
):
    import ontology.view_cache as view_cache_module
    import workspace.workspaces as workspaces_module
    from api.routes import ontologies as routes
    from workspace.workspaces import create_workspace, delete_workspace, get_workspace_paths

    workspace_id = "workspace-tree-recreated"
    ontology_id = "shared-ontology"
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    monkeypatch.setattr(
        view_cache_module,
        "_view_cache",
        OntologyViewCache(max_entries=8, check_interval_seconds=60),
    )

    def write_package(marker):
        ontology_dir = get_workspace_paths(workspace_id).ontologies_dir / ontology_id
        version_dir = ontology_dir / "versions" / "1.0.0"
        version_dir.mkdir(parents=True)
        (ontology_dir / "manifest.yaml").write_text(
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: shared-ontology\n"
            "name: Shared Ontology\n"
            "description: Workspace reuse cache regression\n"
            "version: 1.0.0\n"
            "active_version: 1.0.0\n"
            "versions: [1.0.0]\n"
            "language: zh-CN\n",
            encoding="utf-8",
        )
        (version_dir / "{}.md".format(marker)).write_text(
            marker,
            encoding="utf-8",
        )

    def read_tree():
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/agentic/ontologies/shared-ontology/tree",
                "headers": [],
                "query_string": b"version=1.0.0",
            }
        )
        response = routes.get_ontology_tree(
            ontology_id,
            request,
            workspace_id=workspace_id,
        )
        assert response.status_code == 200
        return json.loads(response.body)["entries"]

    create_workspace(
        "Original workspace",
        workspace_id=workspace_id,
        set_as_default=True,
    )
    write_package("old-only")
    assert [entry["name"] for entry in read_tree()] == ["old-only.md"]

    invalidation_entered = threading.Event()
    release_invalidation = threading.Event()
    errors = []
    original_invalidate = view_cache_module.invalidate_ontology_views

    def blocking_invalidate(**scope):
        invalidation_entered.set()
        assert release_invalidation.wait(timeout=5)
        return original_invalidate(**scope)

    monkeypatch.setattr(
        view_cache_module,
        "invalidate_ontology_views",
        blocking_invalidate,
    )

    def delete_original_workspace():
        try:
            delete_workspace(workspace_id, allow_default=True)
        except Exception as exc:  # surfaced in assertions below
            errors.append(exc)

    def recreate_workspace():
        try:
            create_workspace(
                "Recreated workspace",
                workspace_id=workspace_id,
                set_as_default=True,
            )
        except Exception as exc:  # surfaced in assertions below
            errors.append(exc)

    delete_thread = threading.Thread(target=delete_original_workspace)
    recreate_thread = threading.Thread(target=recreate_workspace)
    delete_thread.start()
    assert invalidation_entered.wait(timeout=5)
    metadata_lock_available = workspaces_module._workspace_metadata_lock.acquire(blocking=False)
    if metadata_lock_available:
        workspaces_module._workspace_metadata_lock.release()
    recreate_thread.start()
    release_invalidation.set()
    delete_thread.join(timeout=5)
    recreate_thread.join(timeout=5)

    assert not delete_thread.is_alive()
    assert not recreate_thread.is_alive()
    assert errors == []
    assert metadata_lock_available is False
    write_package("new-only")

    assert [entry["name"] for entry in read_tree()] == ["new-only.md"]
