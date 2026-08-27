# OR-Tools Solver Service

This directory contains the independently deployable `ortools.solver.v1`
Solver Service. It owns the CP-SAT engine, finite-TTL job store, process
isolation, and the HTTP API described by `contracts/solver_services/ortools`.
It does not import Consumer runtime code or platform databases.

The public API is limited to `GET /health` and `/api/v1/*`; legacy `/v1/*`
routes are intentionally absent. Service authentication uses a bearer token
for `/api/v1/*` and is configured only through `ORTOOLS_SOLVER_*` variables.
State defaults to `/var/lib/ortools-solver`.

For a local synthetic run, set `ORTOOLS_SOLVER_ALLOW_UNAUTHENTICATED=1`.
Production deployments should provide `ORTOOLS_SOLVER_SERVICE_TOKEN_FILE` (or
the direct `ORTOOLS_SOLVER_SERVICE_TOKEN`) through a secret injector. The
service enforces `max_search_workers=1`; `max_concurrent_jobs` controls job
capacity independently.

Build from the repository root:

```bash
./solvers/ortools/build_image.sh v1.0.0 amd
```

The default image name is `solver-ortools-service:<version>`. The Compose
service and private-network DNS name are `ortools-solver`; Consumer outbound
configuration uses only `SOLVER_SERVICE_*` variables.

The first-release deployment uses private-network HTTP on port 8080 and does
not ship or start an Nginx/TLS sidecar. Cross-host TLS is a later deployment
enhancement, outside this first-release image and Compose overlay.

The Consumer release build does not bundle this independent service image.
Build and publish `solver-ortools-service` separately with the command above,
then deploy it with the solver overlay.

Run the focused checks:

```powershell
$env:PYTHONPATH='.'
python -m pytest -q contracts/solver_services/ortools/tests solvers/ortools/tests/test_config.py solvers/ortools/tests/test_store.py
```
