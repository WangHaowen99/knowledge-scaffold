# Knowledge-package Solver Contract Error Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Return a dedicated, actionable error when a selected knowledge-package evaluator does not implement the required solver contract.

**Architecture:** The isolated worker is the first component that can classify the missing package contract, so it emits a stable child-process error code. Each platform layer preserves that classification through typed exceptions until the coordination service creates the public outcome; unrelated evaluation and fact errors retain their existing behavior.

**Tech Stack:** Python 3, dataclasses, subprocess JSON protocol, pytest, existing solver-coordination error registry.

---

### Task 1: Lock the public error contract

**Files:**
- Modify: `consumer/backend/tests/test_solver_errors.py`
- Modify: `consumer/backend/solver_coordination/errors.py`

- [x] **Step 1: Write the failing registry test**

Add assertions that `knowledge_package_solver_contract_incompatible` has HTTP
409, is not retryable, and is carried by
`KnowledgePackageSolverContractIncompatibleError`.

- [x] **Step 2: Run the test and confirm RED**

Run:

```bash
python -m pytest backend/tests/test_solver_errors.py -q
```

Expected: collection or assertion failure because the new exception and registry
entry do not exist.

- [x] **Step 3: Add the minimal public definition**

Add this registry entry and exception:

```python
"knowledge_package_solver_contract_incompatible": ErrorDefinition(
    "knowledge_package_solver_contract_incompatible", 409
)

class KnowledgePackageSolverContractIncompatibleError(SolverApiError):
    code = "knowledge_package_solver_contract_incompatible"
```

- [x] **Step 4: Run the registry test and confirm GREEN**

Run the command from Step 2 and expect all tests in the file to pass.

### Task 2: Preserve the error across worker, Runner, and Provider

**Files:**
- Modify: `consumer/backend/tests/test_solver_route_fact_worker.py`
- Modify: `consumer/backend/tests/test_solver_production_fact_provider.py`
- Modify: `consumer/backend/integration/solver_route_fact_worker.py`
- Modify: `consumer/backend/integration/solver_fact_providers.py`

- [x] **Step 1: Write failing worker and Provider tests**

Extend the real subprocess test using a synthetic evaluator without
`SOLVER_CONTEXT` to assert:

```python
assert error.value.code == "knowledge_package_solver_contract_incompatible"
```

Add a Provider test whose evaluation runner raises that classified
`PackageEvaluationError`; assert that the Provider raises
`KnowledgePackageSolverContractIncompatibleError`. Keep the existing generic
failure test unchanged to prove other failures still become
`FactSnapshotInvalidError`.

- [x] **Step 2: Run both tests and confirm RED**

Run:

```bash
python -m pytest \
  backend/tests/test_solver_route_fact_worker.py \
  backend/tests/test_solver_production_fact_provider.py -q
```

Expected: failures because worker errors and `PackageEvaluationError` do not yet
carry the dedicated code.

- [x] **Step 3: Classify package-contract failures in the worker**

Introduce an internal `PackageSolverContractError(ValueError)`. Raise it for
missing required evaluator symbols and invalid `SOLVER_CONTEXT`. Catch it before
the generic handler in `main()` and return:

```python
{
    "error": {
        "code": "knowledge_package_solver_contract_incompatible",
        "detail": "knowledge package solver contract is incompatible",
    }
}
```

Do not return evaluator paths, missing symbol names, or exception text in this
public classification.

- [x] **Step 4: Preserve the code in Runner and Provider**

Give `PackageEvaluationError` a validated `code` property. The Runner accepts
only the dedicated code from child output and otherwise normalizes to
`business_evaluation_failed`. The Provider maps the dedicated code to
`KnowledgePackageSolverContractIncompatibleError` and keeps its existing generic
fail-closed mapping for every other exception.

- [x] **Step 5: Run both test files and confirm GREEN**

Run the command from Step 2 and expect both files to pass.

### Task 3: Preserve the error through coordination and Agent output

**Files:**
- Modify: `consumer/backend/tests/test_solver_coordination_service.py`
- Create: `consumer/backend/tests/test_solver_terminal.py`
- Modify: `consumer/backend/tests/test_universal_prompt.py`
- Modify: `consumer/backend/solver_coordination/service.py`
- Modify: `consumer/backend/agent/tools/solver_terminal.py`
- Modify: `consumer/backend/agent/prompts/universal.py`

- [x] **Step 1: Write failing coordination and presentation tests**

Inject a fact resolver that raises
`KnowledgePackageSolverContractIncompatibleError` and assert the service raises:

```python
assert error.value.code == "knowledge_package_solver_contract_incompatible"
assert error.value.phase == "snapshot"
assert error.value.retryable is False
```

Project an error outcome through `SolverTerminalOutcomeProjector` and assert the
final output equals:

```text
知识包求解器契约版本不兼容，请升级或重新导入知识包。
```

Extend the universal-prompt test to require explicit administrator/package
remediation for this code and prohibit asking the user for internal facts.

- [x] **Step 2: Run the three tests and confirm RED**

Run:

```bash
python -m pytest \
  backend/tests/test_solver_coordination_service.py \
  backend/tests/test_solver_terminal.py \
  backend/tests/test_universal_prompt.py -q
```

Expected: the service still reports `fact_snapshot_invalid` and terminal output
falls back to the raw code.

- [x] **Step 3: Add the minimal service and Agent mappings**

Catch only `KnowledgePackageSolverContractIncompatibleError` before the generic
fact-resolver handler and raise:

```python
SolverServiceError(
    "knowledge_package_solver_contract_incompatible",
    "snapshot",
    "selected knowledge package does not implement the required solver contract",
)
```

Add the exact Chinese message to `_ERROR_MESSAGES`, and add a prompt rule that
identifies this as a package upgrade/reimport issue rather than missing user
input.

- [x] **Step 4: Run the three test files and confirm GREEN**

Run the command from Step 2 and expect all tests to pass.

### Task 4: Run focused regression verification

**Files:**
- Verify all files modified above.

- [x] **Step 1: Run solver error-path regression tests**

```bash
python -m pytest \
  backend/tests/test_solver_errors.py \
  backend/tests/test_solver_route_fact_worker.py \
  backend/tests/test_solver_production_fact_provider.py \
  backend/tests/test_solver_coordination_service.py \
  backend/tests/test_solver_terminal.py \
  backend/tests/test_universal_prompt.py \
  backend/tests/test_solver_tool_dispatcher.py \
  backend/tests/test_solver_route_guard.py \
  backend/tests/test_mcp_chat_bridge.py -q
```

Expected: all selected tests pass with no warnings introduced by the change.

- [x] **Step 2: Inspect the final diff**

Confirm the diff contains only the dedicated contract-error path, synthetic
tests, and the approved design/plan documents. Do not commit or push until the
user explicitly requests delivery.
