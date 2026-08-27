"""Command-line entry point for running the standalone Connector TCK."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from .admission import (
    HttpFaultScenarioDriver,
    StrictAdmissionError,
    assert_admission_report,
)
from .runner import ConnectorTckRunner, TckFailure
from .verifier import verify_solver_result


def _write_payload(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload))
    sys.stdout.write("\n")
    sys.stdout.flush()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="solver-job-tck")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    parser.add_argument("--bearer-token")
    parser.add_argument(
        "--admission",
        action="store_true",
        help="fail release admission when the report has skipped checks or capability gaps",
    )
    parser.add_argument(
        "--fault-control-url",
        help="test-only out-of-band fault control endpoint (required for strict admission)",
    )
    parser.add_argument(
        "--fault-control-token",
        help="test-only fault control bearer token (required with --fault-control-url)",
    )
    args = parser.parse_args(argv)
    if bool(args.fault_control_url) != bool(args.fault_control_token):
        _write_payload(
            {
                "status": "failed",
                "error_code": "tck_admission_configuration_invalid",
            }
        )
        return 1
    try:
        fault_driver = None
        if args.fault_control_url:
            fault_driver = HttpFaultScenarioDriver(
                control_url=args.fault_control_url,
                control_token=args.fault_control_token,
            )
        report = ConnectorTckRunner(
            base_url=args.base_url,
            timeout_seconds=args.timeout_seconds,
            bearer_token=args.bearer_token,
            fault_driver=fault_driver,
            result_verifier=verify_solver_result if args.admission else None,
        ).run()
    except (TckFailure, ValueError):
        _write_payload({"status": "failed", "error_code": "tck_failed"})
        return 1
    payload = {"status": "passed", "checks": list(report.checks)}
    if report.skipped:
        payload["skipped"] = list(report.skipped)
    if report.capability_gaps:
        payload["capability_gaps"] = list(report.capability_gaps)
    if args.admission:
        try:
            assert_admission_report(report)
        except StrictAdmissionError:
            payload["status"] = "failed"
            payload["error_code"] = "tck_admission_failed"
            _write_payload(payload)
            return 1
    _write_payload(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
