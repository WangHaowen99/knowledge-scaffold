# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Black-box Technology Compatibility Kit for ``solver.job.v1`` endpoints."""

from .runner import (
    ConnectorTckRunner,
    FaultScenario,
    FaultScenarioDriver,
    TckFailure,
    TckReport,
)
from .admission import (
    HttpFaultScenarioDriver,
    StrictAdmissionError,
    assert_admission_report,
)
from .verifier import verify_binary_ordered_route_result, verify_solver_result

__all__ = [
    "ConnectorTckRunner",
    "FaultScenario",
    "FaultScenarioDriver",
    "TckFailure",
    "TckReport",
    "HttpFaultScenarioDriver",
    "StrictAdmissionError",
    "assert_admission_report",
    "verify_binary_ordered_route_result",
    "verify_solver_result",
]
