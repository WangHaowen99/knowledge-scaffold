# Consumer Platform Performance P0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate repeated metadata I/O and duplicate frontend requests while preserving authentication, authorization, ontology, and model-reference behavior.

**Architecture:** Publish immutable, generation-based snapshots after authoritative writes; use explicit invalidation plus throttled drift checks for out-of-process changes. The frontend owns one workspace-scoped ontology resource and calls one fail-closed model-reference inventory endpoint.

**Tech Stack:** Python 3.12, FastAPI, standard-library locks/dataclasses/OrderedDict, React 19, Node test runner.

---

### Task 1: Atomic Config Snapshot

**Files:**
- Create: `consumer/backend/config/config_snapshot.py`
- Modify: `consumer/backend/config/system_config.py`
- Test: `consumer/backend/tests/test_config_snapshot.py`
- Test: `consumer/backend/tests/test_system_config_settings.py`

- [ ] **Step 1: Write failing snapshot tests**

Cover one load/decrypt per generation, defensive copies, a 1.5 second throttled source-signature check, failed writes that do not advance generation, and successful update/import writes that do.
Also assert `PERF_CONFIG_SNAPSHOT_ENABLED=false` executes the original authoritative load path without changing public output.

```python
def test_snapshot_reuses_one_validated_load_until_generation_changes(snapshot_manager):
    first = snapshot_manager.load()
    second = snapshot_manager.load()
    assert first == second
    assert snapshot_manager.load_count == 1

def test_snapshot_returns_defensive_copy(snapshot_manager):
    first = snapshot_manager.load()
    first["agent"]["context_max_rounds"] = 999
    assert snapshot_manager.load()["agent"]["context_max_rounds"] != 999
```

- [ ] **Step 2: Verify RED**

Run: `cd consumer/backend && .\venv\Scripts\python.exe -m pytest tests/test_config_snapshot.py -q`

Expected: FAIL because `ConfigSnapshotManager` and generation APIs do not exist.

- [ ] **Step 3: Implement immutable publication and atomic writes**

Implement `ConfigSourceSignature`, `ConfigSnapshot`, and `ConfigSnapshotManager` with an `RLock`, monotonic generation, a frozen process environment, throttled file signature checks, stale-last-good behavior for non-security drift failures, and deep-copy reads. Replace direct `CONFIG_PATH.write_text` with same-directory temporary write, file flush/fsync, and `os.replace`; publish only after replacement succeeds.

Public functions in `system_config.py`:

```python
def get_system_config_generation() -> int: ...
def invalidate_system_config_snapshot() -> int: ...
def load_system_config() -> dict[str, dict[str, Any]]: ...
```

- [ ] **Step 4: Verify GREEN and regressions**

Run: `cd consumer/backend && .\venv\Scripts\python.exe -m pytest tests/test_config_snapshot.py tests/test_system_config_api.py tests/test_system_config_migrations.py -q`

Expected: PASS. Existing Windows-only POSIX path assertions remain documented baseline exceptions and are not weakened.

- [ ] **Step 5: Checkpoint**

Run: `git diff --check -- consumer/backend/config consumer/backend/tests/test_config_snapshot.py`

### Task 2: AppConfig Generation Memoization

**Files:**
- Modify: `consumer/backend/config/settings.py`
- Test: `consumer/backend/tests/test_agent_runtime_config.py`
- Test: `consumer/backend/tests/test_config_snapshot.py`

- [ ] **Step 1: Write failing memoization tests**

Assert repeated `AppConfig.from_env()` calls at one generation reuse one parsed immutable source, returned instances cannot corrupt later reads, and a generation change rebuilds exactly once.

- [ ] **Step 2: Verify RED**

Run: `cd consumer/backend && .\venv\Scripts\python.exe -m pytest tests/test_config_snapshot.py -k app_config -q`

- [ ] **Step 3: Implement generation-keyed memoization**

Use a private lock and `(generation, AppConfig)` cache. Keep request-specific or secret-bearing public projections outside the cache. Add a storage-only accessor that reads only the storage section when resolving runtime paths.

- [ ] **Step 4: Verify GREEN**

Run: `cd consumer/backend && .\venv\Scripts\python.exe -m pytest tests/test_config_snapshot.py tests/test_agent_runtime_config.py -q`

- [ ] **Step 5: Checkpoint**

Run: `git diff --check -- consumer/backend/config/settings.py consumer/backend/tests`

### Task 3: Workspace Catalog Snapshot

**Files:**
- Create: `consumer/backend/workspace/catalog_snapshot.py`
- Modify: `consumer/backend/workspace/workspaces.py`
- Modify: `consumer/backend/api/auth/user_deletion.py`
- Test: `consumer/backend/tests/test_workspace_catalog_snapshot.py`
- Test: `consumer/backend/tests/test_workspace_authz.py`

- [ ] **Step 1: Write failing catalog tests**

Cover warm reads with zero directory/stat/json calls, O(1) get/default/resolve, defensive copies, throttled external drift, and invalidation after create/update/delete/member/default/runtime-config/migration operations. ACL filtering must remain request-local.
Assert `PERF_WORKSPACE_SNAPSHOT_ENABLED=false` falls back to the existing scan path.

- [ ] **Step 2: Verify RED**

Run: `cd consumer/backend && .\venv\Scripts\python.exe -m pytest tests/test_workspace_catalog_snapshot.py -q`

- [ ] **Step 3: Implement catalog publication and invalidation**

Expose:

```python
def get_workspace_catalog_snapshot() -> WorkspaceCatalogSnapshot: ...
def get_workspace_catalog_generation() -> int: ...
def get_workspace_config_generation(workspace_id: str) -> int: ...
def invalidate_workspace_catalog(*, workspace_id: str | None = None, runtime_config: bool = False) -> int: ...
```

Build normalized public metadata once, retain `items_by_id` plus ordered IDs, and update all authoritative write paths only after durable writes succeed.

- [ ] **Step 4: Verify GREEN and authorization regressions**

Run: `cd consumer/backend && .\venv\Scripts\python.exe -m pytest tests/test_workspace_catalog_snapshot.py tests/test_workspace_acl_service.py tests/test_workspace_runtime_config.py -q`

- [ ] **Step 5: Checkpoint**

Run: `git diff --check -- consumer/backend/workspace consumer/backend/api/auth consumer/backend/tests`

### Task 4: Ontology View Revision and Package Tree Cache

**Files:**
- Create: `consumer/backend/ontology/view_cache.py`
- Modify: `consumer/backend/ontology/ontologies.py`
- Modify: `consumer/backend/api/routes/ontologies.py`
- Modify: `consumer/backend/api/routes/operations.py`
- Test: `consumer/backend/tests/test_ontology_view_cache.py`
- Test: `consumer/backend/tests/test_ontology_routes.py`

- [ ] **Step 1: Write failing cache tests**

Assert list GET does not call unconditional `refresh`, one tree build serves repeated reads, cached values cannot be mutated, workspace/version/revision keys do not cross, all package/file/version/status/operation mutations invalidate, and a throttled external tree signature causes rebuild.
Assert `PERF_ONTOLOGY_VIEW_CACHE_ENABLED=false` calls the validated tree loader on every read.

- [ ] **Step 2: Verify RED**

Run: `cd consumer/backend && .\venv\Scripts\python.exe -m pytest tests/test_ontology_view_cache.py -q`

- [ ] **Step 3: Implement revisioned view cache**

Provide:

```python
def get_ontology_mutation_revision(workspace_id: str, ontology_id: str) -> int: ...
def invalidate_ontology_views(*, workspace_id: str, ontology_id: str | None = None) -> int: ...
def get_package_tree_cached(*, workspace_id: str, ontology_id: str, version: str, loader: Callable[[], list[dict]]) -> list[dict]: ...
```

Use `OrderedDict + RLock`, deep-copy on return, bounded entries, single-flight builds, and retain existing trusted-root/symlink validation inside the loader. Remove only the list route's unconditional refresh; retain explicit mutation refresh where required for immediate write responses.

- [ ] **Step 4: Verify GREEN**

Run: `cd consumer/backend && .\venv\Scripts\python.exe -m pytest tests/test_ontology_view_cache.py tests/test_ontology_routes.py tests/test_ontology_mutations.py tests/test_ontology_version_delete_guard.py -q`

- [ ] **Step 5: Checkpoint**

Run: `git diff --check -- consumer/backend/ontology consumer/backend/api/routes consumer/backend/tests`

### Task 5: Server Timing and Low-Cardinality Cache Metrics

**Files:**
- Create: `consumer/backend/integration/observability/performance.py`
- Modify: `consumer/backend/api/middleware.py`
- Test: `consumer/backend/tests/test_performance_observability.py`

- [ ] **Step 1: Write failing timing tests**

Assert `Server-Timing` contains named phase durations, cache counters accept only cache/result labels, and headers/metrics never include workspace, user, run, route parameters, or secret values.

- [ ] **Step 2: Verify RED**

Run: `cd consumer/backend && .\venv\Scripts\python.exe -m pytest tests/test_performance_observability.py -q`

- [ ] **Step 3: Implement request-scoped timing collector**

Use `ContextVar` for phase accumulation and helpers `record_timing(name, duration_ms)` and `record_cache_event(cache_name, result)`. Install middleware after request identity resolution and emit only allow-listed names.

- [ ] **Step 4: Verify GREEN**

Run: `cd consumer/backend && .\venv\Scripts\python.exe -m pytest tests/test_performance_observability.py tests/test_observability_architecture.py -q`

- [ ] **Step 5: Checkpoint**

Run: `git diff --check -- consumer/backend/integration/observability consumer/backend/api/middleware.py`

### Task 6: Aggregated Model Profile Reference API

**Files:**
- Create: `consumer/backend/config/model_profile_references.py`
- Modify: `consumer/backend/api/routes/system_config.py`
- Test: `consumer/backend/tests/test_model_profile_references.py`
- Test: `consumer/backend/tests/test_system_config_api.py`

- [ ] **Step 1: Write failing aggregation and fail-closed tests**

Test one catalog read, one lightweight runtime-config read per workspace, complete inventory output with generations, and `complete=false` when any workspace cannot be read or parsed.

- [ ] **Step 2: Verify RED**

Run: `cd consumer/backend && .\venv\Scripts\python.exe -m pytest tests/test_model_profile_references.py -q`

- [ ] **Step 3: Implement admin-only endpoint**

Add `GET /api/model-profile-references?profile_id=<id>` returning `{complete, entries, generation}`. Reuse canonical model-reference parsing; do not cache authorization decisions.

- [ ] **Step 4: Verify GREEN**

Run: `cd consumer/backend && .\venv\Scripts\python.exe -m pytest tests/test_model_profile_references.py tests/test_system_config_api.py -q`

- [ ] **Step 5: Checkpoint**

Run: `git diff --check -- consumer/backend/config/model_profile_references.py consumer/backend/api/routes/system_config.py`

### Task 7: Shared Ontology Resource and Lazy Tree Loading

**Files:**
- Create: `consumer/frontend/src/app/providers/ontologyResource.js`
- Create: `consumer/frontend/src/app/providers/ontologyResource.test.js`
- Modify: `consumer/frontend/src/app/AppShell.jsx`
- Modify: `consumer/frontend/src/features/chat/ChatWorkspace.jsx`
- Modify: `consumer/frontend/src/features/knowledge/hooks/useKbBrowser.js`
- Test: `consumer/frontend/src/features/chat/ChatWorkspace.test.js`

- [ ] **Step 1: Write failing request-count tests**

Assert one ontology list request per principal/workspace/revision, chat consumes the shared list, non-knowledge surfaces issue zero tree requests, same-key callers share one promise, scope changes abort the old request, and logout clears principal-scoped entries.

- [ ] **Step 2: Verify RED**

Run: `cd consumer/frontend && node --test src/app/providers/ontologyResource.test.js src/features/chat/ChatWorkspace.test.js`

- [ ] **Step 3: Implement the shared resource**

Use a small module-level map keyed by explicit principal/workspace/resource/revision, store in-flight promises and `AbortController`s, and expose invalidation from authoritative mutations. Change `useKbBrowser({listEnabled, treeEnabled, ontologyResource})`; `AppShell` enables list only for chat/knowledge subscribers and tree only for the knowledge workbench.

- [ ] **Step 4: Verify GREEN and architecture**

Run: `cd consumer/frontend && npm test`

- [ ] **Step 5: Checkpoint**

Run: `git diff --check -- consumer/frontend/src`

### Task 8: Remove Configuration-Center Request Fanout

**Files:**
- Modify: `consumer/frontend/src/features/settings/models/api.js`
- Modify: `consumer/frontend/src/features/settings/models/ModelSettings.jsx`
- Modify: `consumer/frontend/src/features/settings/models/api.test.js`
- Modify: `consumer/frontend/src/features/settings/models/modelConfig.test.js`
- Create: `consumer/backend/scripts/benchmark_metadata_reads.py`

- [ ] **Step 1: Write failing single-request and rename tests**

Assert first paint makes no inventory request, deletion/ID rename invokes one aggregate endpoint, confirmation revalidation compares generation/fingerprint, and incomplete inventory blocks mutation.

- [ ] **Step 2: Verify RED**

Run: `cd consumer/frontend && node --test src/features/settings/models/api.test.js src/features/settings/models/modelConfig.test.js`

- [ ] **Step 3: Implement on-demand inventory**

Replace per-workspace `Promise.all` with the aggregate endpoint, remove initial inventory loading, and apply the same fail-closed path to delete and profile-ID rename.

- [ ] **Step 4: Verify GREEN, lint, and build**

Run: `cd consumer/frontend && npm run lint && npm test && npm run build`

- [ ] **Step 5: P0 acceptance checkpoint**

Run backend P0 tests plus request-count tests and record cold/warm open/stat/traversal counts using synthetic workspaces and packages. `benchmark_metadata_reads.py` must emit machine-readable JSON and fail when workspace warm P95 is at least 200 ms, ontology list/tree warm P95 is at least 300 ms, a warm read performs authoritative traversal, ontology list requests exceed one, or a non-knowledge surface requests a tree.
