# Package-owned solver facts

## Context

The ordered-route solver originally used a fixed-size validation fixture as its
fact source. That proved the Agent-to-OR-Tools transport, but it made production
requests depend on one month, one batch count, and one exact input payload.
Changing the month or any batch value therefore failed before the solver ran.

The ontology package already contains the business evaluator and the platform
already owns the installed package revision and its configured graph data source.
The external solver service must remain business-neutral.

## Decision

For `ordered_binary_route.v1`, the consumer platform resolves facts by running
the evaluator from the selected, installed knowledge-package revision against
the current request and the package's configured Neo4j source.

For a request containing `N` ordered batches, the platform constructs the full
canonical set of `2N` zero-or-one-transition route candidates. The package
evaluator calculates eligibility, economic score, and safety margin for every
candidate in one isolated process. Those candidate facts are frozen into the
solver snapshot. OR-Tools selects from that snapshot and an independent platform
verifier reconstructs and checks the selected result.

Month, batch count, batch values, and the selected optimum are never loaded from
a validation fixture. The fixture provider remains available only through an
explicit local validation mode.

The decision model may declare `fact_source.evaluator_path`. If it omits that
field, the platform accepts the package only when exactly one flat Python module
under `scripts/` defines the evaluator entry points `BatchPlan`, `build_report`,
`load_tables`, and `normalize_crude_name`.

The evaluator must expose a `SOLVER_CONTEXT` mapping. Customer and plant-specific
identifiers stay in that package-owned mapping; the platform consumes only these
generic roles:

- `option_routes`
- `route_stream_products`
- `atmospheric_product_tanks`
- `special_tanks`
- `unit_capacity_keys`
- `correction_factor_prefix_by_unit`
- `production_condition`

An absent, ambiguous, or invalid evaluator contract fails closed before the
standalone solver service is called.

## Alternatives considered

- Build one knowledge-package revision per month. Rejected because the package
  defines business logic, while monthly plans are request data.
- Copy package formulas into the OR-Tools service. Rejected because this would
  duplicate business semantics and couple the solver service to one package.
- Keep the fixed-size fixture as a fallback. Rejected because a silent fallback
  could return a verified result for facts that do not belong to the request.

## Consequences

- One package revision supports different months, batch counts, and batch values.
- A request with `N` batches produces `2N` candidates without special-case month
  logic.
- Missing package code, graph data, unsupported production conditions, or invalid
  evaluator output fail closed before OR-Tools is called.
- The platform must preserve explicit graph-scope values when the runtime
  workspace identifier differs from the package data's graph scope.
- Production correctness depends on the selected package evaluator and its graph
  data; updating those semantics remains a knowledge-package responsibility.
