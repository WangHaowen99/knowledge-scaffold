# Knowledge-package solver contract incompatibility error

## Context

The route-fact worker requires package-owned solver metadata such as
`SOLVER_CONTEXT`. A package revision that predates that evaluator contract
currently fails inside the isolated worker, but each caller replaces the
specific failure with `fact_snapshot_invalid`. Operators therefore cannot
distinguish an incompatible package revision from malformed or incomplete
business facts.

## Decision

Expose the stable error code
`knowledge_package_solver_contract_incompatible` when the selected package
evaluator is missing or violates the required solver-context contract.

- HTTP status: `409 Conflict`
- Retryable: `false`
- Solver phase: `snapshot`
- Public message: `知识包求解器契约版本不兼容，请升级或重新导入知识包。`

The isolated worker returns this machine-readable code without exposing package
paths or implementation details. The evaluation runner validates and preserves
the code, the fact provider raises a typed contract exception, and the solver
coordination service maps that exception to the public solver outcome. Other
evaluator failures and invalid fact data continue to use
`fact_snapshot_invalid`.

## Alternatives

1. Reuse `solver_contract_drift`. Rejected because that code describes a locked
   runtime contract changing after selection, not a package revision that never
   implemented the required evaluator contract.
2. Add a nested reason beneath `fact_snapshot_invalid`. Rejected because current
   Agent and MCP clients dispatch on the top-level stable code and would still
   present the generic snapshot message.

## Verification

Automated tests must prove that:

- a worker loading an evaluator without `SOLVER_CONTEXT` emits the dedicated
  machine code;
- the runner and fact provider preserve that code while unrelated evaluator
  failures remain generic;
- the coordination service returns the dedicated code at the `snapshot` phase;
- the terminal Agent output renders the Chinese remediation message;
- the error registry exposes HTTP 409 and marks the error non-retryable.
