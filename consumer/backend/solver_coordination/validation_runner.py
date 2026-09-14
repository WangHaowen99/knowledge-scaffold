# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Execute one strict, frozen-snapshot validation operation batch."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping

from workspace.workspaces import get_workspace_project_root

from .canonical import canonical_json_bytes
from .contracts import CandidateSet
from .errors import VerificationDivergenceError, VerificationRuntimeFailure
from .operation_runtime import OperationInvocation, OperationRuntime
from .operation_validation import ResolvedValidationContext
from .validation import CandidateValidationResult, CandidateValidationStatus
from .validation_artifacts import ValidationEvidenceStore


class OntologyValidationOperationRunner:
    """Run a package-bound helper once and map its result to trusted records."""

    _RESPONSE_FIELDS = frozenset({"schema_version", "results"})
    _RESULT_FIELDS = frozenset({"candidate_id", "status", "error_code", "error_stage"})

    def __init__(
        self,
        *,
        operation_runtime: OperationRuntime | None = None,
        execute_operation: Callable[..., str] | None = None,
        evidence_store_factory: Callable[[str], ValidationEvidenceStore] = ValidationEvidenceStore,
        timeout_seconds: int | None = None,
    ) -> None:
        if operation_runtime is not None and execute_operation is not None:
            raise ValueError("configure operation_runtime or execute_operation, not both")
        self.operation_runtime = operation_runtime
        self.execute_operation = execute_operation
        self.evidence_store_factory = evidence_store_factory
        self.timeout_seconds = timeout_seconds or int(os.getenv("SOLVER_VALIDATION_TIMEOUT_SECONDS", "300"))

    def validate(
        self,
        *,
        candidate_set: CandidateSet,
        context: ResolvedValidationContext,
    ) -> tuple[CandidateValidationResult, ...]:
        request_payload = {
            "schema_version": "solver_coordination.validation_request.v1",
            "candidate_set": candidate_set.to_dict(),
            "fact_snapshot": {
                "ref": context.fact_snapshot_ref,
                "digest": context.fact_snapshot_digest,
            },
        }
        if context.fact_snapshot_payload is not None:
            request_payload["fact_snapshot"]["payload"] = context.fact_snapshot_payload
        try:
            if self.execute_operation is not None:
                raw_output = self.execute_operation(
                    ontology_id=context.knowledge_package_id,
                    ontology_version=context.knowledge_package_revision,
                    operation_key=context.validation_operation,
                    request_payload=request_payload,
                    base_path=get_workspace_project_root(context.workspace_id),
                    workspace_id=context.workspace_id,
                    run_id=context.validation_run_id,
                    timeout_seconds=self.timeout_seconds,
                    data_access="frozen_snapshot",
                    context=context,
                )
            elif self.operation_runtime is not None:
                if context.validation_script_path is None:
                    raise VerificationRuntimeFailure(
                        "resolved validation script path is unavailable"
                    )
                raw_output = self.operation_runtime.execute(
                    OperationInvocation(
                        ontology_id=context.knowledge_package_id,
                        ontology_version=context.knowledge_package_revision,
                        operation_key=context.validation_operation,
                        arguments={
                            "request_json": canonical_json_bytes(request_payload).decode(
                                "utf-8"
                            )
                        },
                        base_path=get_workspace_project_root(context.workspace_id),
                        workspace_id=context.workspace_id,
                        run_id=context.validation_run_id,
                        timeout_seconds=self.timeout_seconds,
                        data_access="frozen_snapshot",
                        expected_script_path=context.validation_script_path,
                        expected_script_digest=context.validation_script_digest,
                    )
                )
            else:
                raise VerificationRuntimeFailure(
                    "validation operation runtime is not configured"
                )
        except VerificationRuntimeFailure:
            raise
        except Exception as exc:
            raise VerificationRuntimeFailure("validation operation invocation failed") from exc

        response = self._parse_response(raw_output)
        results_payload = response["results"]
        expected_ids = tuple(candidate.candidate_id for candidate in candidate_set.candidates)
        actual_ids = tuple(item.get("candidate_id") for item in results_payload)
        if actual_ids != expected_ids:
            raise VerificationDivergenceError(
                "validation results do not match candidate order"
            )

        evidence_payload = {
            "request": request_payload,
            "response": response,
        }
        try:
            evidence_ref = self.evidence_store_factory(context.workspace_id).put(
                evidence_payload,
                owner_id=context.owner_id,
                source_run_id=context.validation_run_id,
            )
        except Exception as exc:
            raise VerificationRuntimeFailure("validation evidence persistence failed") from exc

        mapped: list[CandidateValidationResult] = []
        for candidate, item in zip(candidate_set.candidates, results_payload):
            status = CandidateValidationStatus(item["status"])
            error_code = str(item.get("error_code") or "").strip()
            error_stage = str(item.get("error_stage") or "").strip()
            mapped.append(
                CandidateValidationResult(
                    candidate_id=candidate.candidate_id,
                    candidate_digest=candidate.candidate_digest,
                    status=status,
                    knowledge_package_id=candidate_set.knowledge_package_id,
                    knowledge_package_revision=candidate_set.knowledge_package_revision,
                    fact_snapshot_digest=context.fact_snapshot_digest,
                    rules_revision=context.rules_revision,
                    evidence_ref=evidence_ref,
                    error_code=error_code,
                    error_stage=error_stage,
                )
            )
        return tuple(mapped)

    @classmethod
    def _parse_response(cls, raw_output: object) -> dict[str, Any]:
        if not isinstance(raw_output, str):
            raise VerificationRuntimeFailure("validation operation returned non-text output")
        try:
            parsed = json.loads(raw_output)
        except (TypeError, ValueError) as exc:
            raise VerificationRuntimeFailure("validation operation returned invalid JSON") from exc
        if not isinstance(parsed, Mapping) or set(parsed) != cls._RESPONSE_FIELDS:
            raise VerificationRuntimeFailure("validation operation response envelope is invalid")
        if parsed.get("schema_version") != "solver_coordination.validation_operation.v1":
            raise VerificationRuntimeFailure("validation operation response schema is invalid")
        results = parsed.get("results")
        if not isinstance(results, list) or not results:
            raise VerificationRuntimeFailure("validation operation results are invalid")
        normalized: list[dict[str, Any]] = []
        for item in results:
            if not isinstance(item, Mapping) or not set(item).issubset(cls._RESULT_FIELDS):
                raise VerificationRuntimeFailure("validation result fields are invalid")
            candidate_id = item.get("candidate_id")
            status = item.get("status")
            if not isinstance(candidate_id, str) or not candidate_id.strip():
                raise VerificationRuntimeFailure("validation result candidate is invalid")
            if status not in {status.value for status in CandidateValidationStatus}:
                raise VerificationRuntimeFailure("validation result status is invalid")
            if status == CandidateValidationStatus.VALIDATION_ERROR.value:
                if not isinstance(item.get("error_code"), str) or not str(item.get("error_code")).strip():
                    raise VerificationRuntimeFailure("validation error code is required")
                if not isinstance(item.get("error_stage"), str) or not str(item.get("error_stage")).strip():
                    raise VerificationRuntimeFailure("validation error stage is required")
            elif "error_code" in item or "error_stage" in item:
                raise VerificationRuntimeFailure("validation error fields are not allowed")
            normalized.append(dict(item))
        return {"schema_version": parsed["schema_version"], "results": normalized}
