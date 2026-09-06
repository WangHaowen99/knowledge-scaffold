# Consumer Platform Performance P2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide an opt-in Linux-native storage mode and bounded batched observability without changing default Linux bind-mount behavior.

**Architecture:** Add an explicit Compose overlay and storage-mode-aware backup/restore/migration commands; never infer or switch modes silently. Batch spans and local writes behind bounded queues with low-cardinality health metrics and a five-second shutdown budget.

**Tech Stack:** Docker Compose, POSIX shell, Python 3.12, OpenTelemetry SDK, existing local trace exporter.

---

### Task 1: Optional Named-Volume Compose Mode

**Files:**
- Create: `consumer/deploy/docker-compose.storage-volume.yml`
- Modify: `consumer/docker-compose.yml`
- Modify: `consumer/deploy/docker-compose.release.yml`
- Modify: `consumer/deploy/common.sh`
- Test: `consumer/deploy/tests/test_release_compose.py`

- [ ] **Step 1: Write failing Compose contract tests**

Assert bind mode remains default; volume overlay declares `consumer-runtime-data` and `consumer-auth-data`; backend/MCP are read-write, sandbox is runtime read-only, auth is auth read-write; and invalid `STORAGE_MODE` fails before Compose execution.

- [ ] **Step 2: Verify RED**

Run: `cd consumer && .\backend\venv\Scripts\python.exe -m pytest deploy/tests/test_release_compose.py -q`

- [ ] **Step 3: Implement explicit overlay selection**

Add `STORAGE_MODE=bind|volume` parsing to `common.sh`, compose-file argument construction, stable volume names, and persisted installation mode. Never auto-detect from host OS.

- [ ] **Step 4: Verify GREEN and Compose rendering**

Run both `docker compose config` variants and the test suite. Inspect mount source, target, and read-only flags.

- [ ] **Step 5: Checkpoint**

Run: `git diff --check -- consumer/docker-compose.yml consumer/deploy`

### Task 2: Storage-Aware Backup, Restore, and Migration

**Files:**
- Modify: `consumer/deploy/backup.sh`
- Create: `consumer/deploy/restore.sh`
- Create: `consumer/deploy/migrate_storage.sh`
- Modify: `consumer/deploy/migrate.sh`
- Modify: `consumer/deploy/upgrade.sh`
- Modify: `consumer/deploy/rollback.sh`
- Modify: `consumer/deploy/migrate_persistent_storage.py`
- Test: `consumer/deploy/tests/test_migrate_persistent_storage.py`
- Create: `consumer/deploy/tests/test_storage_scripts.py`

- [ ] **Step 1: Write failing manifest and safety tests**

Use fully synthetic nested/empty files. Assert file count, bytes, normalized relative paths and SHA256 match; corrupt archives fail; restore targets a temporary volume; failed verification never switches; rerun is idempotent; and rollback is code-only unless explicit restore is requested.

- [ ] **Step 2: Verify RED**

Run: `cd consumer && .\backend\venv\Scripts\python.exe -m pytest deploy/tests/test_migrate_persistent_storage.py deploy/tests/test_storage_scripts.py -q`

- [ ] **Step 3: Implement safe mode-aware operations**

For volumes use a pinned project utility image with source mounted read-only and target read-write. Archive plus canonical SHA256 manifest before copying. Stop writers, verify source and target, persist mode/version metadata, and retain the old source as rollback material. Never overwrite the active target in place.

- [ ] **Step 4: Verify GREEN**

Run the synthetic script tests and shell syntax checks. If Docker is available, execute both bind-to-volume and volume-to-bind dry runs.

- [ ] **Step 5: Checkpoint**

Run: `git diff --check -- consumer/deploy`

### Task 3: Bounded Batch Span Processing and Local Batch Writes

**Files:**
- Modify: `consumer/backend/integration/observability/otel.py`
- Modify: `consumer/backend/integration/observability/local_storage.py`
- Modify: `consumer/backend/integration/observability/exporters/local/exporter.py`
- Modify: `consumer/backend/integration/observability/bus.py`
- Modify: `consumer/backend/integration/observability/workspace_router.py`
- Test: `consumer/backend/tests/test_observability_batching.py`

- [ ] **Step 1: Write failing batching tests**

Test queue 2048, batch 256, scheduled delay 300 ms, shutdown deadline 5 seconds, one-lock `append_many`, exporter exception isolation, deterministic dropped counts, and no high-cardinality metric labels.

- [ ] **Step 2: Verify RED**

Run: `cd consumer/backend && .\venv\Scripts\python.exe -m pytest tests/test_observability_batching.py -q`

- [ ] **Step 3: Implement bounded batching**

Replace `SimpleSpanProcessor` with `BatchSpanProcessor` using validated operational overrides. Add `append_many()` and `export_batch()`; make bus/router prefer batch paths and expose accepted/exported/dropped/current-depth/peak-depth/flush-timeout metrics labelled only by exporter and result.

- [ ] **Step 4: Verify GREEN and existing observability tests**

Run: `cd consumer/backend && .\venv\Scripts\python.exe -m pytest tests/test_observability_batching.py tests/test_workspace_observability_config.py tests/test_workspace_observability_routes.py -q`

- [ ] **Step 5: Checkpoint**

Run: `git diff --check -- consumer/backend/integration/observability consumer/backend/tests/test_observability_batching.py`

### Task 4: Shutdown Contract and Operator Documentation

**Files:**
- Modify: `consumer/backend/app.py`
- Modify: `consumer/README.md`
- Modify: `consumer/deploy/README.md`
- Test: `consumer/backend/tests/test_observability_batching.py`

- [ ] **Step 1: Write failing shutdown tests**

Assert shutdown stops admission, drains within five seconds, records timeout/drop count, and never waits indefinitely.

- [ ] **Step 2: Verify RED**

Run: `cd consumer/backend && .\venv\Scripts\python.exe -m pytest tests/test_observability_batching.py -k shutdown -q`

- [ ] **Step 3: Implement lifecycle order and documentation**

Document explicit bind/volume commands, Windows named-volume recommendation, Linux native bind recommendation, backup/restore/migration/rollback sequencing, and the fact that data rollback is separate from code rollback.

- [ ] **Step 4: Verify GREEN and P2 acceptance**

Run backend batching tests, deploy tests, Compose config checks, frontend build, and synthetic migration hash comparison.

- [ ] **Step 5: Final P0-P2 checkpoint**

Run focused backend suites, full frontend lint/test/build, SDK tests, `git diff --check`, branch-name validation, and the repository delivery-security scan before any commit or push is requested.
