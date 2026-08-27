# Connector TCK

`ConnectorTckRunner` is an endpoint-only conformance runner for
`solver.job.v1`.  The runner sends only HTTP/JSON requests to the six public
Connector endpoints; it does not import Consumer code, a solver SDK, or an
engine implementation.

The core checks cover health/capabilities, strict schemas, family/version
negotiation, idempotency, the job state machine, result normalization, the
family golden case, cancellation, and sensitive-data isolation.

## Optional fault admission checks

Fault injection is intentionally not part of the public job protocol.  A
deployment can provide a test-only `FaultScenarioDriver` that controls a
network proxy, process supervisor, or other out-of-band harness.  The runner
still observes the Connector through HTTP/JSON only.  Supported scenario
names are:

`fault_429`, `fault_502`, `fault_503`, `fault_timeout`,
`fault_execution_failed`, `fault_attempt_lost`, `fault_restart`, and
`fault_late_result`.

Without a driver, these checks are reported in `TckReport.skipped` and
`TckReport.capability_gaps`; they are never silently reported as passed.  A
driver may return `None` for an unsupported scenario, which records a
scenario-specific capability gap.

The optional `result_verifier` callback receives the submitted request and the
normalized result as JSON mappings.  It must return `True` for the golden
result to be accepted.  A missing verifier is likewise reported as a
capability gap.

`contracts.solver.tck.verifier.verify_solver_result` is the public reference
verifier for the published family golden problem.  It consumes only public
submit/result documents, recomputes the eligible lexicographic winner, and
validates assignment, objective values, bound, and zero gap.  It imports no
Consumer or solver engine implementation.

For a process/deployment admission, use `HttpFaultScenarioDriver` with a
separate test-only control endpoint.  The control endpoint is out of band: it
can arrange faults but the TCK observes health, job state, errors, cancellation,
restart recovery, and results only through the public `solver.job.v1`
HTTP/JSON endpoints.  The allowlisted control commands carry no ProblemIR or
result data.

The CLI includes non-empty `skipped` and `capability_gaps` arrays in its JSON
report. By default it preserves the compatibility behavior of returning exit
code `0` for a completed core report, even when optional checks are skipped.
For release admission, pass `--admission`: the command returns status
`failed`, error code `tck_admission_failed`, and exit code `1` whenever
`report.passed` is false, `skipped` is non-empty, or `capability_gaps` is
non-empty. A complete report returns status `passed` and exit code `0`.

```powershell
python -m contracts.solver.tck `
  --base-url http://127.0.0.1:8080 `
  --admission `
  --fault-control-url http://127.0.0.1:9090/control/faults `
  --fault-control-token $env:SOLVER_CONNECTOR_TCK_CONTROL_TOKEN
```

`--admission` installs the public family verifier.  A control URL and token
install the external fault driver.  Strict admission still fails closed if
either component leaves any check skipped or any capability gap reported.
