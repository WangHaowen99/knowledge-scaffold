# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Production assembly for operation-backed solver validation."""

from __future__ import annotations

from .operation_validation import OperationValidationRunService
from .validation_artifacts import ValidationEvidenceStore
from .validation_resolver import RunArtifactValidationContextResolver
from .validation_runner import OntologyValidationOperationRunner
from .operation_runtime import OperationRuntime


def build_operation_validation_service(
    *, operation_runtime: OperationRuntime | None = None
) -> OperationValidationRunService:
    """Build a stateless service whose request inputs are resolved at run time."""
    return OperationValidationRunService(
        context_resolver=RunArtifactValidationContextResolver(),
        batch_validator=OntologyValidationOperationRunner(
            operation_runtime=operation_runtime,
            evidence_store_factory=ValidationEvidenceStore,
        ),
    )
