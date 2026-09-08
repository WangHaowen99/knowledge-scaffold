# Consumer Platform Optimization Follow-up Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve the confirmed issues from the PR303 plus 0.26.8.0 integration test and produce a reproducible end-to-end verification report.

**Architecture:** Keep the existing runtime boundaries. Add bounded event-buffer handling and explicit incomplete-result states; normalize API errors at the frontend boundary; replace raw import preview payloads with a summarized result plus opt-in diagnostics; fix generated UTF-8 labels; preserve session index delta recovery and add the missing rebuilding UI state. Business totals remain derived from authoritative tool outputs and are rejected when incompatible.

**Tech Stack:** Python 3.11/FastAPI, React/Vite, OpenJiuwen runtime, existing file-backed stores, Docker Compose, Playwright-compatible browser evidence.

---

### Task 1: Sync the latest release baseline

**Files:**
- Branch state: `origin/0.26.8.0`
- Verification: `git merge-tree`, `git status`, branch diff

- [ ] Fetch `origin/0.26.8.0` and merge it into `perf/consumer-platform-optimization-0.26.8.0`.
- [ ] Confirm the merged tree contains both release `a66c61d3` and PR fix `44e62a86`.
- [ ] Run the focused backend/frontend baseline tests before editing behavior.

### Task 2: Bound event delivery and incomplete-result handling

**Files:**
- Modify: `consumer/backend/integration/observability/bus.py`
- Modify: `consumer/backend/agent/runtime/event_bus.py`
- Modify: `consumer/backend/agent/runtime/jiuwen_events.py`
- Modify: `consumer/backend/api/routes/workflows.py`
- Modify: `consumer/frontend/src/features/chat/ChatWorkspace.jsx`
- Test: `consumer/backend/tests/test_observability_batching.py`
- Test: `consumer/backend/tests/test_async_run_manager.py`
- Test: `consumer/frontend/src/features/chat/ChatWorkspace.test.js`

- [ ] Add a failing regression test that drives three bounded consumers until the queue reaches its limit and asserts terminal error classification plus preserved terminal state.
- [ ] Add bounded event coalescing/backpressure metrics and ensure terminal, tool-result, error, cancel, and recovery events are never silently dropped.
- [ ] Map `event buffer full` to an explicit incomplete-result terminal state and a retry/diagnostic action in the UI.
- [ ] Verify cancellation and reconnect do not replay side-effecting tools.

### Task 3: Validate business summary arithmetic

**Files:**
- Modify: `consumer/backend/agent/core/orchestrator.py`
- Modify: `consumer/backend/agent/runtime/service.py`
- Modify: `consumer/backend/agent/tools/execute_script.py`
- Modify: `consumer/frontend/src/features/chat/ChatWorkspace.jsx`
- Test: `consumer/backend/tests/test_orchestrator_preflight_performance.py`
- Test: `consumer/backend/tests/test_execute_script_preflight_pin.py`
- Test: `consumer/frontend/src/features/chat/ChatWorkspace.test.js`

- [ ] Add a failing test for mismatched batch totals versus monthly totals.
- [ ] Reject or label inconsistent summaries with the source fields, unit, period, and adjustment ledger instead of presenting the monthly gold value as unexplained truth.
- [ ] Clarify daily-inflow versus batch-total tank checks in the structured result and rendered table.
- [ ] Add regression coverage for the known 97.00%/87.60%/99.80% monthly values when inputs are internally consistent.

### Task 4: Normalize authentication errors and import preview UX

**Files:**
- Modify: `consumer/frontend/src/features/auth/*` (actual login component discovered after sync)
- Modify: `consumer/frontend/src/features/knowledge/*` import result component
- Test: matching frontend auth and import tests

- [ ] Add failing UI tests for 401, network, timeout, 429, 503 and unknown 5xx states.
- [ ] Map unknown-user and wrong-password to the same Chinese message without exposing account existence.
- [ ] Replace raw import JSON with summary counts, severity, file path, and expandable escaped diagnostics.
- [ ] Add tests for invalid ZIP, warnings, duplicate version, success and retry states.

### Task 5: Fix generated pagination labels and runtime status wording

**Files:**
- Modify: `consumer/frontend/src/features/chat/hooks/useChatSessions.js`
- Modify: `consumer/frontend/src/features/chat/lib/sessionScope.js`
- Modify: generated/static source identified by failing screenshot and build output
- Test: session pagination and UI copy tests

- [ ] Add a failing test asserting the visible label is `加载更多` in source and production build.
- [ ] Preserve 50→60 pagination, selection, pinning, deduplication and stale-cursor recovery.
- [ ] Distinguish `运行中`, `已停止`, `执行中断，当前结果不完整`, and successful terminal states.

### Task 6: Re-run the full journey and publish evidence

- [ ] Build independent release + branch images without changing the existing running environment.
- [ ] Run frontend journeys for login, workspace isolation, model profile, knowledge package, standard business question, pagination, cancellation, restart recovery and three-client concurrency.
- [ ] Capture screenshots for every claimed result and store request timing samples, deployment proof, and source hashes.
- [ ] Generate `D:\knowledge-scaffold\设计文档\17-0.26.8.0验证报告\2026-09-06-PR303集成验证问题优化建议报告.html` successor with navigation, real screenshots, P50/P95/max/error-rate, defects, limits, and a truthful verdict.
- [ ] Run `git diff --check`, delivery security scan, focused/full tests, commit, push, and record the remote SHA.
