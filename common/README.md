# common

Shared foundational code for top-level applications in this repository.

Keep this package dependency-light:

- Allowed: logging, shared errors, small config/type helpers, and other reusable infrastructure.
- Avoid: consumer business workflows, app-specific routes, database access, model/provider clients, and UI concerns.
- Direction: applications may import `common`, but `common` must not import application packages such as `consumer.backend.agentic`.

Current modules:

- `common.logging`: shared logging context, formatters, dynamic log-level refresh, and date-partitioned rotating file handlers.
