# Solver Code Check Compliance Design

## Context

The initial static-analysis pass reported 184 findings. The first remediation
fixed 52 findings and documented 132 findings for review. The delivery target is
now stricter: the branch must have no unresolved code-check findings. Broad rule
suppression is not acceptable.

## Goals

- Eliminate all ordinary code-quality findings through source refactoring.
- Preserve public HTTP, MCP, Connector, TCK, and solver behavior.
- Preserve fail-closed validation and durable error mappings.
- Keep unavoidable security-hotspot review exact to one call site, with a
  concrete control and test; never disable a rule globally.
- Add a local regression gate for every finding shape that can be represented
  with Python AST or text inspection.

## Approaches Considered

### A. Broad suppression

Keep the current implementation and approve all context-dependent findings.
This has the smallest runtime risk but leaves too many unresolved review items.

### B. Mechanical scanner evasion

Rename APIs or replace flagged syntax without improving structure. This can
reduce finding counts, but produces misleading code and is rejected.

### C. Semantic refactoring with narrow hotspot review

Express complex validation as named predicates and guard helpers, expand complex
comprehensions into explicit projections, make key invariants type-visible, and
introduce explicit stdout/import/process boundaries. This is the selected
approach because it satisfies the check while retaining understandable behavior.

## Design

### Local compliance gate

Extend the static-quality test module with checks for the exact production
surfaces in the workbook:

- no filtered or multi-generator comprehensions at reported locations;
- no control predicate exceeding the accepted boolean-expression budget;
- no production `print`, assigned lambda, named expression, or `sys.path`
  mutation;
- no redundant or shadowed exception handlers;
- no direct rethrow where the same result can be expressed by normal control
  flow or the shared HTTP error response;
- no unchecked mapping subscript at the reported trust boundaries;
- explicit checks for read-only file opening, service-process isolation, and the
  stateless bearer-only Flask boundary.

The gate must fail against the current implementation before production code is
changed.

### Maintainability refactoring

Complex conditions will become named validation predicates, small helper
functions, or sequential guard clauses. Complex comprehensions will become
explicit loops with stable ordering and no new mutation outside the local
projection. Fixed-key dictionaries will become named locals, typed records, or
validated lookups so missing keys cannot be converted silently to `None`.

### Protocol and import boundaries

Machine-readable command output will use a dedicated stdout writer that emits
exactly the existing bytes and flush behavior without general-purpose logging.
Test import setup will move to pytest configuration. Dynamic evaluator loading
will use an isolated import boundary without mutating the platform process's
`sys.path`.

### Security hotspots

The solver HTTP service will enforce bearer-only, cookie-independent requests at
the application boundary and will carry a regression test proving cookies cannot
provide ambient authority. Evaluator execution will retain an argument-vector,
no-shell, timed, minimized-environment process boundary with post-execution
output-size validation. If the scanner
still classifies either call as a review-only hotspot, the review will be scoped
to that exact call and cite these controls; the implementation will not weaken
isolation merely to hide the API from static analysis.

The current `capture_output=True` implementation does not impose a hard
in-flight stdout memory limit. Documentation and review evidence must not claim
otherwise; this is a residual resource risk separate from command-injection
safety.

### Error compatibility

Existing stable response mappings remain authoritative. In particular,
workspace lookup failures remain the shared 404 response, storage failures remain
503, invalid input remains 400, and verifier divergence remains fail closed.

## Verification

1. Run the new compliance tests red against the current source.
2. Refactor one finding family at a time and run the focused tests green.
3. Run the complete backend solver, MCP, Connector, TCK, contract, and OR-Tools
   suites.
4. Build backend and solver images, recreate only affected services, and verify
   all health endpoints.
5. Re-run the external code check. Any remaining item must be fixed or reviewed
   at its exact call site before the PR is considered ready.
