# Solver Code Check Compliance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove every ordinary code-check finding from the solver-coordination PR and leave only exact, evidence-backed review state for scanner-defined security hotspots.

**Architecture:** A local AST/text compliance gate first reproduces the reported finding shapes. Production code is then refactored by ownership area while behavior tests protect API mappings, deterministic ordering, stdout protocols, evaluator isolation, and durable storage.

**Tech Stack:** Python 3.11, pytest, Python `ast`, Flask, multiprocessing/subprocess isolation, OR-Tools, Docker Compose.

---

### Task 1: Local Code-Check Gate

**Files:**
- Modify: `consumer/backend/tests/test_solver_static_quality.py`
- Test: `consumer/backend/tests/test_solver_static_quality.py`

- [x] Add AST helpers that count flattened boolean terms and resolve the enclosing function or method.

```python
def _boolean_term_count(node: ast.expr) -> int:
    if isinstance(node, ast.BoolOp):
        return sum(_boolean_term_count(value) for value in node.values)
    return 1
```

- [x] Add failing checks for control predicates with four or more boolean terms across the workbook's production target files.
- [x] Add failing checks for the workbook's filtered or multi-generator comprehension owners.
- [x] Add failing checks for production `print`, `sys.path` mutation, direct rethrow, unchecked target mapping subscripts, and the two assignment-from-no-return call sites.
- [x] Run:

```powershell
$env:PYTHONPATH='consumer/backend;.'
python -m pytest -q consumer/backend/tests/test_solver_static_quality.py
```

Expected: failures enumerate the current unresolved finding families, including at least 66 control predicates and 42 comprehension findings.

### Task 2: Agent, API, Workflow, and Ontology Projections

**Files:**
- Modify: `consumer/backend/agent/core/shared/solver_route_guard.py`
- Modify: `consumer/backend/agent/core/orchestrator.py`
- Modify: `consumer/backend/agent/tools/registry.py`
- Modify: `consumer/backend/agent/tools/solver.py`
- Modify: `consumer/backend/api/routes/ontologies.py`
- Modify: `consumer/backend/api/routes/solver_coordination.py`
- Modify: `consumer/backend/api/routes/workflows.py`
- Modify: `consumer/backend/ontology/ontology_catalog.py`
- Modify: `consumer/backend/ontology/package_import.py`
- Modify: `consumer/backend/persistence/execution_store.py`
- Test: corresponding `consumer/backend/tests/test_*` modules

- [x] Replace four-or-more-term conditions with named predicates or sequential guards. Preserve the original return/raise body exactly.

```python
invalid_identity = not isinstance(identity, str) or not identity.strip()
invalid_revision = not isinstance(revision, str) or not revision.strip()
if invalid_identity or invalid_revision:
    raise ValueError("invalid solver identity")
```

- [x] Expand only scanner-reported complex projections into ordered explicit loops.

```python
items = []
for event in events:
    if isinstance(event, dict) and event.get("turn_id") == turn_id:
        items.append(event)
```

- [x] Replace the remaining workspace direct rethrow in the route helper with normal propagation, and map retry failures through the shared workspace response instead of rethrowing inside a broad handler.
- [x] Run the route, Agent, workflow, ontology, and execution-store test modules plus the static gate.

Expected: API status/payload assertions remain unchanged and this file group disappears from the local finding list.

### Task 3: Integration and Controlled-Validation Boundaries

**Files:**
- Modify: `consumer/backend/integration/solver_coordination_runtime.py`
- Modify: `consumer/backend/integration/solver_fact_providers.py`
- Modify: `consumer/backend/integration/solver_route_fact_worker.py`
- Modify: `consumer/backend/scripts/run_controlled_solver_validation.py`
- Test: `consumer/backend/tests/test_solver_runtime_assembly.py`
- Test: `consumer/backend/tests/test_solver_production_fact_provider.py`
- Test: `consumer/backend/tests/test_solver_route_fact_worker.py`
- Test: `consumer/backend/tests/test_controlled_solver_validation.py`

- [x] Remove the quoted recovery-worker return annotation so it cannot be classified as a hardcoded credential.
- [x] Pass an explicit `0o600` mode to the read-only `os.open` call, documenting that it is ignored without `O_CREAT` but makes the security intent explicit.
- [x] Introduce `_write_stdout_line` / `_write_json_result` helpers based on `sys.stdout.write` and flush; preserve exact newline-delimited worker and CLI output.
- [x] Remove `sys.path.insert` from evaluator loading. Supply the trusted evaluator root through the isolated child import boundary and test sibling-module imports.
- [x] Keep the evaluator process argument-vector, no-shell, minimized environment, output-size validation, and timeout. Add a focused test proving request text cannot alter the executable or argument vector.
- [x] Refactor reported conditions and projections, then run the four focused test modules and static gate.

Expected: worker protocol snapshots and failure codes are byte-for-byte compatible.

### Task 4: Solver Coordination Core

**Files:**
- Modify: `consumer/backend/solver_coordination/bindings.py`
- Modify: `consumer/backend/solver_coordination/candidate_results.py`
- Modify: `consumer/backend/solver_coordination/compiler.py`
- Modify: `consumer/backend/solver_coordination/connectors/endpoint_policy.py`
- Modify: `consumer/backend/solver_coordination/connectors/http_transport.py`
- Modify: `consumer/backend/solver_coordination/connectors/ortools.py`
- Modify: `consumer/backend/solver_coordination/input_binding.py`
- Modify: `consumer/backend/solver_coordination/job_orchestrator.py`
- Modify: `consumer/backend/solver_coordination/job_store.py`
- Modify: `consumer/backend/solver_coordination/operation_validation.py`
- Modify: `consumer/backend/solver_coordination/presentation.py`
- Modify: `consumer/backend/solver_coordination/profiles/input_adapter.py`
- Modify: `consumer/backend/solver_coordination/profiles/ordered_binary_route.py`
- Modify: `consumer/backend/solver_coordination/service.py`
- Modify: `consumer/backend/solver_coordination/validation_resolver.py`
- Modify: `consumer/backend/solver_coordination/validation_store.py`
- Modify: `consumer/backend/solver_coordination/verification/ordered_binary_route.py`
- Test: all `consumer/backend/tests/test_solver_*.py`

- [x] Refactor every four-or-more-term predicate identified by the local gate into named invariants or guard helpers.
- [x] Expand the reported complex comprehensions while retaining tuple/list type, stable order, and first/last-match behavior.
- [x] Replace fixed-key mapping subscripts with named locals populated and validated in the same block. Do not use `get()` where it would hide an invariant violation.
- [x] Give compiler and verifier ports concrete non-`None` return protocols and validate the returned runtime type.
- [x] Run each affected module's focused tests, then all `test_solver_*.py` tests and the static gate.

Expected: no core finding remains and authoritative result digests/order remain unchanged.

### Task 5: Independent Service, Contracts, and CLI

**Files:**
- Modify: `contracts/solver/tck/__main__.py`
- Modify: `contracts/solver/tck/admission.py`
- Modify: `contracts/solver/tck/runner.py`
- Modify: `contracts/solver_services/ortools/validation.py`
- Modify: `solvers/ortools/app.py`
- Modify: `solvers/ortools/config.py`
- Modify: `solvers/ortools/engine/refinery_route.py`
- Modify: `solvers/ortools/tests/conftest.py`
- Modify: `solvers/ortools/tests/test_nine_item_golden.py`
- Create: `solvers/ortools/pytest.ini`
- Test: `solvers/ortools/tests`
- Test: `contracts/solver/tck/tests`

- [x] Replace TCK `print` calls with one explicit stdout writer and preserve CLI exit codes and JSON formatting.
- [x] Move solver-test import configuration from `sys.path.insert` to `pytest.ini` `pythonpath = ../..`.
- [x] Enforce bearer-only, cookie-independent API requests in the Flask app and add tests for cookie rejection and normal bearer requests.
- [x] Refactor remaining control predicates, comprehensions, and fixed-key route-rate access.
- [x] Run independent service, TCK, and contract tests plus the static gate.

Expected: service behavior and OpenAPI/TCK compatibility stay unchanged.

### Task 6: Final Compliance and Runtime Verification

**Files:**
- Modify: `docs/solver-coordination-static-analysis-disposition.md`

- [x] Replace the prior 52/132 disposition with the final code-fixed count and list only scanner-defined review hotspots, if any.
- [x] Run fresh verification:

```powershell
$env:PYTHONPATH='consumer/backend;.'
python -m pytest -q consumer/backend/tests/test_solver_static_quality.py
python -m pytest -q consumer/backend/tests -k "solver or ortools or controlled_validation"
$env:PYTHONPATH='.;consumer/backend'
python -m pytest -q solvers/ortools/tests contracts/solver/tck/tests contracts/solver/tests contracts/solver_services/ortools/tests
python -m compileall -q consumer/backend contracts solvers/ortools
git diff --check
```

- [x] Rebuild backend and OR-Tools images from the worktree, recreate only backend/MCP/solver containers, and verify every health check is `healthy`.
- [ ] Run the external code check. Resolve every ordinary finding; record an exact-call security review only when the scanner defines the item as a review-only hotspot.

Expected: zero unresolved code-check findings, all task-relevant tests pass, and local services are healthy.
