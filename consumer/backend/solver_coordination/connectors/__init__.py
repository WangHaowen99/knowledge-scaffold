"""Consumer-owned transports and service-specific solver connectors."""

from .base import SolverConnector
from .http_transport import HttpSolverTransport
from .ortools import OrToolsConnector, ORTOOLS_CONNECTOR_CODE_BUILD
from .endpoint_policy import EndpointPolicy, EndpointPolicyError
from .helpers import (
    capability_fingerprint,
    idempotency_key,
    validate_traceparent,
)

__all__ = [
    "HttpSolverTransport",
    "OrToolsConnector",
    "ORTOOLS_CONNECTOR_CODE_BUILD",
    "SolverConnector",
    "EndpointPolicy",
    "EndpointPolicyError",
    "capability_fingerprint",
    "idempotency_key",
    "validate_traceparent",
]
