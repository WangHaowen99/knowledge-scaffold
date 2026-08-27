# Solver Coordination Static-Analysis Disposition

## Scope

This disposition covers all 184 findings in the supplied static-analysis
workbook, detail sheet rows 2 through 185. Workbook row numbers are used below
because source line numbers move as fixes are applied.

| Disposition | Count | Workbook rows |
| --- | ---: | --- |
| Fixed in code | 183 | 2-3, 5-185 |
| Security hotspot reviewed | 1 | 4 |
| Suppression requested | 0 | None |
| Total | 184 | 2-185 |

## Code Fixes

| Finding or rule | Count | Workbook rows | Resolution |
| --- | ---: | --- | --- |
| Hardcoded access key | 1 | 2 | Replaced the quoted recovery-worker return annotation with a normal postponed annotation. No credential literal exists. |
| CSRF disabled | 1 | 3 | Disabled cookie-backed Flask sessions with `BearerOnlySessionInterface`; cookie-only requests are rejected and successful bearer requests emit no session cookie. |
| G.FIO.01 | 1 | 5 | Added an explicit restrictive mode to the controlled read-only `os.open` call. |
| G.LOG.02 | 8 | 6-13 | Replaced general `print` calls with dedicated newline-delimited stdout protocol writers. |
| G.ERR.07 | 8 | 14-21 | Replaced silent exception suppression with fixed diagnostic logging or explicit fallback behavior. |
| G.TES.01 | 5 | 22-26 | Replaced production assertions with explicit domain/runtime failures. |
| G.FIO.04 | 6 | 27-32 | Centralized temporary-file descriptor ownership and cleanup in `utils.atomic_files.staged_bytes`. |
| G.ERR.10 | 1 | 33 | Ordered exception handlers from specific storage errors to parent value errors. |
| G.FNM.04 | 2 | 34-35 | Typed compiler and verifier ports and added runtime `ProblemIR` / `VerificationResult` narrowing. |
| G.CTL.03 | 66 | 36-101 | Replaced four-or-more-term predicates with sequential guards, named state checks, or small helpers while preserving short-circuit order. |
| G.OPR.04 | 3 | 102-104 | Replaced assignment expressions with explicit locals and loops. |
| G.ERR.13 | 13 | 105-117 | Removed direct catch-and-rethrow handlers while preserving domain-exception propagation and shared HTTP mappings. |
| G.ERR.09 | 14 | 118-131 | Removed child exception types already covered by a parent in the same handler. |
| G.FIO.05 | 1 | 132 | Replaced Windows UNC path concatenation with `ntpath.join`. |
| G.EXP.04 | 43 | 133-175 | Expanded reported comprehensions and generators into ordered explicit loops. |
| G.PSL.03 | 2 | 176-177 | Removed runtime/test `sys.path` mutation; package resolution now uses the isolated child environment and pytest configuration. |
| G.EXP.03 | 1 | 178 | Replaced the assigned lambda clock default with named `_utc_now`. |
| G.TYP.07 | 7 | 179-185 | Replaced weak fixed-key dictionary recovery with typed count locals, direct tuple iteration, and validated fact payload lookup. |

## Reviewed Security Hotspot

Workbook row 4 is the single remaining scanner-defined hotspot:
`PackageRouteEvaluationRunner` starts the trusted package evaluator with
`subprocess.run`.

The call is retained because the separate process is the evaluator isolation
boundary. It uses a fixed argument vector beginning with `sys.executable`, an
explicit `shell=False`, business input only through JSON stdin, a validated
evaluator path, a minimized allowlisted environment, a timeout, and
`check=False` with explicit return-code handling. Tests prove command-like
request text never enters argv and parent-process secrets do not enter the child
environment.

Residual risk: `capture_output=True` captures output before the post-process
size check. The configured maximum rejects oversized completed output, but it
is not a hard in-flight stdout memory limit. This does not create command or
shell injection, but it remains a resource-boundary consideration.

## Proactive Rescan Hardening

An independent AST pass over the affected files also found source shapes that
were not rows in the original workbook but could be reported by a fresh scan.
They were fixed before delivery:

| Shape | Count | Resolution |
| --- | ---: | --- |
| Direct bare rethrow | 13 | Preserved domain propagation through explicit exception classification. |
| Silent `except: pass` | 14 | Added fixed, data-free diagnostics or explicit fallback state. |
| Production `assert` | 2 | Added stable request guards and error responses. |

The local AST gate covers these shapes as well as boolean complexity,
comprehension owners, exception hierarchy/order, protocol output, import-path
mutation, weak mapping access, Flask session isolation, and subprocess controls.
The external code-check service should still be rerun on the final PR revision;
no ordinary finding is approved for suppression.
