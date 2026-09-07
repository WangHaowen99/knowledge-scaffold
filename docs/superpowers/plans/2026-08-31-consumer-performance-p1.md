# Consumer Platform Performance P1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make session listing and run admission bounded by page size and immutable generations instead of historical event volume.

**Architecture:** Maintain a rebuildable per-user/workspace session summary index while preserving chat and execution logs as facts. Build one request-scoped preflight snapshot from P0 generations and cache only immutable package-revision products in a bounded single-flight LRU.

**Tech Stack:** Python 3.12, FastAPI, atomic JSON, standard-library base64/OrderedDict/RLock, React and TypeScript SDK.

---

### Task 1: Session Summary Index Core

**Files:**
- Create: `consumer/backend/persistence/session_summary_index.py`
- Test: `consumer/backend/tests/test_session_summary_index.py`

- [ ] **Step 1: Write failing index tests**

Cover atomic writes, schema-version rebuild, corrupt-index quarantine, stable `(pinned, updated_at, session_id)` ordering, opaque cursor validation, no duplicate/omitted records at a fixed index revision, and user/workspace isolation.
Assert `PERF_SESSION_SUMMARY_INDEX_ENABLED=false` preserves the legacy full-list response path.

- [ ] **Step 2: Verify RED**

Run: `cd consumer/backend && .\venv\Scripts\python.exe -m pytest tests/test_session_summary_index.py -q`

- [ ] **Step 3: Implement index types and persistence**

Create frozen `SessionSummary` and `SessionPage`; store `schema_version`, `index_revision`, and records. Use URL-safe base64 JSON cursors containing sort tuple plus revision, same-directory temp write and `os.replace`, and explicit degraded/rebuild state.

- [ ] **Step 4: Verify GREEN**

Run: `cd consumer/backend && .\venv\Scripts\python.exe -m pytest tests/test_session_summary_index.py -q`

- [ ] **Step 5: Checkpoint**

Run: `git diff --check -- consumer/backend/persistence/session_summary_index.py consumer/backend/tests/test_session_summary_index.py`

### Task 2: Project Chat and Execution Writes into the Index

**Files:**
- Modify: `consumer/backend/persistence/chat_store.py`
- Modify: `consumer/backend/persistence/execution_store.py`
- Modify: `consumer/backend/workspace/user_store_manager.py`
- Test: `consumer/backend/tests/test_chat_store.py`
- Test: `consumer/backend/tests/test_execution_store_interactions.py`
- Test: `consumer/backend/tests/test_user_store_manager.py`

- [ ] **Step 1: Write failing projection tests**

Test create/title/pin/message/metadata/interaction/active-run/terminal/compact/delete projections, shared index injection into both stores, original-write success despite index failure, and no index rewrite for each token event.

- [ ] **Step 2: Verify RED**

Run: `cd consumer/backend && .\venv\Scripts\python.exe -m pytest tests/test_chat_store.py tests/test_execution_store_interactions.py tests/test_user_store_manager.py -k summary -q`

- [ ] **Step 3: Implement optional index injection**

Keep direct `ChatStore` and `ExecutionStore` construction compatible. `UserStoreManager` creates one index per `(safe_user, workspace)` and injects it into both stores. Project only after durable writes; on projection failure mark the index degraded and schedule rebuild.

- [ ] **Step 4: Verify GREEN**

Run: `cd consumer/backend && .\venv\Scripts\python.exe -m pytest tests/test_chat_store.py tests/test_execution_store_interactions.py tests/test_user_store_manager.py -q`

- [ ] **Step 5: Checkpoint**

Run: `git diff --check -- consumer/backend/persistence consumer/backend/workspace`

### Task 3: Paginated Session API, Bounded Recovery, and Indexed Prune

**Files:**
- Modify: `consumer/backend/api/routes/sessions.py`
- Modify: `consumer/backend/app.py`
- Modify: `consumer/backend/agent/core/orchestrator.py`
- Test: `consumer/backend/tests/test_session_routes.py`
- Test: `consumer/backend/tests/test_session_summary_index.py`

- [ ] **Step 1: Write failing API and complexity tests**

Cover `limit=1..100`, bad cursors, legacy full-list compatibility, `next_cursor/has_more`, recovery restricted to current-page active/pending sessions, indexed prune excluding pinned/active sessions, and 1000-session first page without event-log traversal.

- [ ] **Step 2: Verify RED**

Run: `cd consumer/backend && .\venv\Scripts\python.exe -m pytest tests/test_session_routes.py tests/test_session_summary_index.py -k 'pagination or bounded or prune' -q`

- [ ] **Step 3: Implement bounded request paths**

Move full recovery to a lifespan background batch with item/time budgets. Remove synchronous full prune from create; use one manager-owned indexed prune entry point. Preserve old `sessions` response and add page metadata.

- [ ] **Step 4: Verify GREEN**

Run: `cd consumer/backend && .\venv\Scripts\python.exe -m pytest tests/test_session_routes.py tests/test_user_store_manager.py -q`

- [ ] **Step 5: Checkpoint**

Run: `git diff --check -- consumer/backend/api/routes/sessions.py consumer/backend/app.py consumer/backend/agent/core/orchestrator.py`

### Task 4: Frontend and SDK Pagination Compatibility

**Files:**
- Modify: `consumer/frontend/src/features/chat/hooks/useChatSessions.js`
- Modify: `consumer/frontend/src/features/chat/lib/sessionState.test.js`
- Modify: `consumer/sdk/typescript-sdk/src/types/agentic.ts`
- Modify: `consumer/sdk/typescript-sdk/src/protocols/agentic/sessions.ts`
- Test: `consumer/sdk/typescript-sdk` existing tests or add `sessions.test.ts`

- [ ] **Step 1: Write failing client tests**

Assert the frontend requests `limit=50`, preserves current selection, stores cursor, and merges the next page without duplicates. Assert SDK `list()` remains compatible and `listPage()` exposes page metadata.

- [ ] **Step 2: Verify RED**

Run: `cd consumer/frontend && npm test`

- [ ] **Step 3: Implement pagination adapters**

Add page normalization at transport boundaries; do not leak cursor mechanics into conversation rendering.

- [ ] **Step 4: Verify GREEN**

Run: `cd consumer/frontend && npm test && npm run build`

- [ ] **Step 5: Checkpoint**

Run: `git diff --check -- consumer/frontend consumer/sdk/typescript-sdk`

### Task 5: Immutable Preflight Product LRU

**Files:**
- Create: `consumer/backend/agent/runtime/preflight_cache.py`
- Test: `consumer/backend/tests/test_preflight_cache.py`

- [ ] **Step 1: Write failing LRU tests**

Test same-key single-flight, any system/workspace/ontology generation change causing a miss, immutable return values, exception non-caching, bounded eviction, and concurrent callers receiving one load.
Assert `PERF_RUN_PREFLIGHT_CACHE_ENABLED=false` executes the immutable loader for each request while retaining request-scoped snapshot reuse.

- [ ] **Step 2: Verify RED**

Run: `cd consumer/backend && .\venv\Scripts\python.exe -m pytest tests/test_preflight_cache.py -q`

- [ ] **Step 3: Implement the bounded cache**

Key exactly `(workspace_id, workspace_config_generation, system_config_generation, ontology_id, ontology_version, mutation_revision)`. Freeze mappings/lists recursively; do not cache ACL, idempotency, busy state, user input, facts, or results.

- [ ] **Step 4: Verify GREEN**

Run: `cd consumer/backend && .\venv\Scripts\python.exe -m pytest tests/test_preflight_cache.py -q`

- [ ] **Step 5: Checkpoint**

Run: `git diff --check -- consumer/backend/agent/runtime/preflight_cache.py consumer/backend/tests/test_preflight_cache.py`

### Task 6: RunPreflightSnapshot Contract and Runtime Integration

**Files:**
- Create: `consumer/backend/agent/runtime/preflight.py`
- Modify: `consumer/backend/agent/runtime/contracts.py`
- Modify: `consumer/backend/agent/runtime/service.py`
- Modify: `consumer/backend/api/routes/workflows.py`
- Test: `consumer/backend/tests/test_async_runtime_contracts.py`
- Test: `consumer/backend/tests/test_canonical_run_api.py`
- Test: `consumer/backend/tests/test_async_run_manager.py`
- Create: `consumer/backend/scripts/benchmark_session_admission.py`

- [ ] **Step 1: Write failing snapshot reuse tests**

Assert a request resolves model/config/manifest/operation catalog once, the accepted run freezes exact package revision, protocols share the same runtime path, generation change misses the cache, and dynamic ACL/idempotency/busy/fact checks still execute every request.

- [ ] **Step 2: Verify RED**

Run: `cd consumer/backend && .\venv\Scripts\python.exe -m pytest tests/test_async_runtime_contracts.py tests/test_canonical_run_api.py -k preflight -q`

- [ ] **Step 3: Implement frozen contract and builder**

Add frozen `RunPreflightSnapshot` and an excluded `preflight_snapshot` field on `RuntimeInvocationContext`. Build it after authentication/authorization but before durable admission; reuse immutable products in `_prepare_sync_impl` and keep all dynamic checks outside it. Admission failures must still write a stable terminal state.

- [ ] **Step 4: Verify GREEN and protocol regressions**

Run: `cd consumer/backend && .\venv\Scripts\python.exe -m pytest tests/test_async_runtime_contracts.py tests/test_canonical_run_api.py tests/test_async_run_manager.py tests/test_mcp_ontology_binding.py tests/test_orchestrator_ontology_version_stamp.py -q`

- [ ] **Step 5: P1 acceptance checkpoint**

Measure synthetic create-session, first-page list, and create-run preflight counters; verify no permission or integrity check was removed. `benchmark_session_admission.py` must emit JSON and fail when first-page session-list P95 is at least 300 ms, create-session P95 is at least 500 ms, create-run admission P95 is at least 500 ms, or the first page reads more than 50 session details.
