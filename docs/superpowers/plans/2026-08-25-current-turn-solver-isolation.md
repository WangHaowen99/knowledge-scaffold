# Current-Turn Solver Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure evaluation-only turns neither expose nor inherit solver execution while preserving explicit and implicit solver routing.

**Architecture:** The orchestrator derives a per-turn solver suppression flag from the latest request. Tool registries consume that flag to omit dynamic Solver ToolCards, while the Jiuwen context builder removes historical raw solver tool messages and adds a current-turn provenance instruction. Existing required-solver routing remains fail-closed.

**Tech Stack:** Python 3.11, pytest, Flask agent runtime, OpenJiuwen ReAct adapter, Docker Compose.

---

Delivery commits are intentionally omitted from this plan because repository policy requires a separate explicit user request before `git commit` or `git push`.

### Task 1: Derive and bind per-turn solver suppression

**Files:**
- Modify: `consumer/backend/agent/core/shared/solver_route_guard.py`
- Test: `consumer/backend/tests/test_solver_route_guard.py`

- [x] **Step 1: Write failing routing-state tests**

Add tests proving that a synthetic evaluation request returns `True` from `should_suppress_solver_tools`, an explicit solve-and-evaluate request returns `False`, and `bind_solver_route_requirement` stores suppression only when no requirement exists.

```python
assert should_suppress_solver_tools("【合成测试数据】请统计资源负荷是否过载") is True
assert should_suppress_solver_tools("【合成测试数据】请求解候选方案并统计结果") is False
assert solver_tools_suppressed_from_state({"user_context": context}) is True
```

- [x] **Step 2: Run the focused test and verify RED**

Run: `$env:PYTHONPATH='consumer/backend'; python -m pytest consumer/backend/tests/test_solver_route_guard.py -q`

Expected: FAIL because the suppression API and context key do not exist.

- [x] **Step 3: Implement the minimal routing-state API**

Add `SUPPRESS_SOLVER_TOOLS_CONTEXT_KEY`, `should_suppress_solver_tools`, and `solver_tools_suppressed_from_state`. Extend `bind_solver_route_requirement` with a keyword-only `suppress_solver_tools=False`; a concrete requirement always wins and clears suppression.

- [x] **Step 4: Run the focused test and verify GREEN**

Run the command from Step 2.

Expected: all route guard tests pass.

### Task 2: Hide dynamic Solver ToolCards from both runtime adapters

**Files:**
- Modify: `consumer/backend/agent/tools/registry.py`
- Modify: `consumer/backend/agent/core/jiuwen/tools.py`
- Modify: `consumer/backend/agent/core/jiuwen_engine.py`
- Test: `consumer/backend/tests/test_tool_registry.py`
- Test: `consumer/backend/tests/test_execute_script_dynamic_schema.py`

- [x] **Step 1: Write failing registry and engine tests**

Patch the synthetic solver catalog loader to return one solver spec. Assert that `include_solver=False` omits it from both `get_tools_schema` and `get_jiuwen_tool_infos`, while the default still includes it. Add an engine-state assertion for `_suppress_solver_tools=True`.

```python
names = [row["function"]["name"] for row in get_tools_schema("main", include_solver=False)]
assert "solve_synthetic_route_abcd1234" not in names
```

- [x] **Step 2: Run tests and verify RED**

Run: `$env:PYTHONPATH='consumer/backend'; python -m pytest consumer/backend/tests/test_tool_registry.py consumer/backend/tests/test_execute_script_dynamic_schema.py -q`

Expected: FAIL because `include_solver` is unsupported and suppressed state is ignored.

- [x] **Step 3: Implement tool visibility**

Add an `include_solver: bool = True` keyword to `get_tool_specs_for_caller`, `get_tools_schema`, and `get_jiuwen_tool_infos`. Skip dynamic solver catalog loading when false. In both Jiuwen schema paths, pass `include_solver=not solver_tools_suppressed_from_state(state)`.

- [x] **Step 4: Run tests and verify GREEN**

Run the command from Step 2.

Expected: registry and dynamic schema suites pass with no solver spec in suppressed state.

### Task 3: Isolate historical solver messages and add provenance policy

**Files:**
- Modify: `consumer/backend/agent/core/shared/solver_route_guard.py`
- Modify: `consumer/backend/agent/core/jiuwen_engine.py`
- Modify: `consumer/backend/agent/prompts/universal.py`
- Test: `consumer/backend/tests/test_jiuwen_react_adapter.py`
- Test: `consumer/backend/tests/test_universal_prompt.py`

- [x] **Step 1: Write failing history and prompt tests**

Create synthetic history containing one complete solver turn followed by a normal script turn. For suppressed state, assert that the complete solver turn is removed while the normal turn remains. Assert the current query includes `<Current-Turn-Tool-Provenance>` and the universal prompt forbids attributing historical tool results to the current request.

- [x] **Step 2: Run tests and verify RED**

Run: `$env:PYTHONPATH='consumer/backend'; python -m pytest consumer/backend/tests/test_jiuwen_react_adapter.py consumer/backend/tests/test_universal_prompt.py -q`

Expected: FAIL because raw solver history is retained and no provenance policy exists.

- [x] **Step 3: Implement history filtering and provenance context**

Add a generated Solver ToolCard name recognizer and a pure `filter_solver_history_messages` helper that removes complete historical turns containing an actual solver call. Apply it in `_build_jiuwen_history_source_messages` only for suppressed state. Append a generic current-request scope block after the selected ontology context and add a universal rule that execution claims must refer to tools called after the latest user message.

- [x] **Step 4: Run tests and verify GREEN**

Run the command from Step 2.

Expected: Jiuwen adapter and prompt suites pass.

### Task 4: Wire suppression through the orchestrator and cover the same-session regression

**Files:**
- Modify: `consumer/backend/agent/core/orchestrator.py`
- Test: `consumer/backend/tests/test_orchestrator_runtime.py`

- [x] **Step 1: Write a failing orchestrator test**

Use a synthetic two-turn session. First persist a solver tool call/result; then issue an evaluation-only request. Capture the second turn initial state and assert `_suppress_solver_tools=True`, no solver requirement, and ordinary script execution remains available.

- [x] **Step 2: Run the test and verify RED**

Run: `$env:PYTHONPATH='consumer/backend'; python -m pytest consumer/backend/tests/test_orchestrator_runtime.py -q`

Expected: FAIL because the orchestrator does not bind suppression.

- [x] **Step 3: Bind the latest-request decision**

Compute suppression from `original_user_request` after resolving the concrete requirement and pass it into `bind_solver_route_requirement`. Do not derive it from stored history or session metadata.

- [x] **Step 4: Run the test and verify GREEN**

Run the command from Step 2.

Expected: orchestrator tests pass.

### Task 5: Full verification and runtime regression

**Files:**
- No new production files.

- [x] **Step 1: Run the backend regression matrix**

Run the route guard, orchestrator, Jiuwen adapter, prompt, registry, MCP solver contract, and ordinary script tests in one pytest invocation.

Expected: zero failures.

- [x] **Step 2: Compile modified Python files**

Run: `python -m py_compile <all modified Python paths>`

Expected: exit code 0.

- [x] **Step 3: Rebuild backend and MCP containers from the corrected source**

Rebuild only the backend image, recreate backend and MCP services, and verify both health endpoints.

- [x] **Step 4: Run the real same-session frontend regression**

In one new synthetic test conversation, execute a solver request followed by an evaluation-only request. Verify the second turn registers and calls only ordinary ontology tools, emits no `solver.result.authoritative`, and does not state that a solver returned the current result.

- [x] **Step 5: Run MCP contract regressions**

Run the MCP solver chat contract and runtime integration suites. Verify explicit solver requests still require an authoritative result and evaluation-only requests cannot expose a solver execution event.
