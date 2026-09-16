# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Resolve a frozen validation context from exact run and package artifacts."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

import yaml

from ontology.ontology_catalog import (
    find_script_entry,
    get_ontology_manifest,
    load_script_catalog,
    split_markdown_frontmatter,
)

from .contracts import CandidateSet
from .errors import VerificationRuntimeFailure
from .operation_validation import ResolvedValidationContext, ValidationContextResolver
from .validation_artifacts import FactSnapshotArtifactResolver, ValidationArtifactError


class ValidationOperationResolutionError(ValueError):
    """The package does not expose one trusted validation operation."""


class RunArtifactValidationContextResolver(ValidationContextResolver):
    """Bind a parent run to its exact package revision and helper script."""

    def resolve(
        self,
        *,
        candidate_set: CandidateSet,
        workspace_id: str,
        owner_id: str,
        parent_solve_run_id: str,
        validation_run_id: str,
    ) -> ResolvedValidationContext:
        try:
            snapshot = FactSnapshotArtifactResolver(workspace_id).resolve(
                owner_id=owner_id,
                source_run_id=parent_solve_run_id,
                knowledge_package_id=candidate_set.knowledge_package_id,
                knowledge_package_revision=candidate_set.knowledge_package_revision,
            )
            manifest, error = get_ontology_manifest(
                candidate_set.knowledge_package_id,
                version=candidate_set.knowledge_package_revision,
                workspace_id=workspace_id,
            )
            if error or manifest is None:
                raise ValidationOperationResolutionError(
                    "exact package revision is unavailable"
                )
            package_root = Path(manifest.ontology_dir).resolve()
            manifest_path = Path(manifest.manifest_path).resolve()
            if not manifest_path.is_file() or manifest_path.is_symlink():
                raise ValidationOperationResolutionError(
                    "package revision manifest is unavailable"
                )
            raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
            # v0.1 compatibility packages keep solver capabilities in their
            # revision-local descriptor rather than embedding them in the
            # shared manifest.  The descriptor is still package-owned and is
            # read only from the exact revision selected above.
            if not isinstance(raw.get("capabilities"), Mapping):
                descriptor = package_root / "solver-capabilities.yaml"
                if descriptor.is_file() and not descriptor.is_symlink():
                    descriptor_raw = yaml.safe_load(
                        descriptor.read_text(encoding="utf-8")
                    ) or {}
                    if isinstance(descriptor_raw, Mapping):
                        raw = {**raw, **descriptor_raw}
            capability = self._resolve_capability(raw, candidate_set.problem_family)
            operation_key = self._text(
                capability.get("validation_operation"), "validation_operation"
            )
            validator_revision = self._text(
                capability.get("validator_revision"), "validator_revision"
            )
            rules_revision = self._text(
                capability.get("rules_revision"), "rules_revision"
            )
            if rules_revision != snapshot.rules_revision:
                raise ValidationOperationResolutionError(
                    "snapshot rules revision does not match package capability"
                )
            solve_operation = self._text(capability.get("operation"), "operation")
            self._validate_operation(package_root, operation_key, solve_operation)
            entry = find_script_entry(
                load_script_catalog(package_root), operation_key, bound_only=True
            )
            if entry is None:
                raise ValidationOperationResolutionError(
                    "validation operation has no bound script"
                )
            script_path = Path(str(entry.get("_resolved_script_path") or "")).resolve()
            try:
                script_path.relative_to((package_root / "scripts").resolve())
            except ValueError as exc:
                raise ValidationOperationResolutionError(
                    "validation script is outside package"
                ) from exc
            if not script_path.is_file() or script_path.is_symlink():
                raise ValidationOperationResolutionError(
                    "validation script is unavailable"
                )
            script_digest = "sha256:" + hashlib.sha256(script_path.read_bytes()).hexdigest()
            return ResolvedValidationContext(
                workspace_id=workspace_id,
                owner_id=owner_id,
                parent_solve_run_id=parent_solve_run_id,
                validation_run_id=validation_run_id,
                knowledge_package_id=candidate_set.knowledge_package_id,
                knowledge_package_revision=candidate_set.knowledge_package_revision,
                problem_family=candidate_set.problem_family,
                validation_operation=operation_key,
                validator_revision=validator_revision,
                rules_revision=rules_revision,
                fact_snapshot_ref=snapshot.fact_snapshot_ref,
                fact_snapshot_digest=snapshot.fact_snapshot_digest,
                fact_snapshot_path=snapshot.fact_snapshot_path,
                fact_snapshot_payload=dict(snapshot.fact_payload),
                validation_script_path=script_path,
                validation_script_digest=script_digest,
                ontology_dir=package_root,
            )
        except (ValidationArtifactError, ValidationOperationResolutionError, OSError, yaml.YAMLError) as exc:
            raise VerificationRuntimeFailure(
                "validation context resolution failed"
            ) from exc

    @classmethod
    def _resolve_capability(
        cls, manifest: Mapping[str, object], problem_family: str
    ) -> Mapping[str, object]:
        capabilities = manifest.get("capabilities")
        records = (
            capabilities.get("solver_operations")
            if isinstance(capabilities, Mapping)
            else None
        )
        if not isinstance(records, list):
            raise ValidationOperationResolutionError(
                "solver validation capability is missing"
            )
        matches: list[Mapping[str, object]] = []
        for item in records:
            if not isinstance(item, Mapping):
                continue
            if str(item.get("problem_family") or "").strip() != problem_family:
                continue
            matches.append(item)
        if len(matches) != 1:
            raise ValidationOperationResolutionError(
                "exactly one validation capability is required"
            )
        return matches[0]

    @classmethod
    def _validate_operation(
        cls, package_root: Path, operation_key: str, solve_operation: str
    ) -> None:
        operation_path = package_root / "operations" / (operation_key + ".md")
        if not operation_path.is_file() or operation_path.is_symlink():
            raise ValidationOperationResolutionError(
                "validation operation document is unavailable"
            )
        frontmatter, _body = split_markdown_frontmatter(
            operation_path.read_text(encoding="utf-8")
        )
        execution = frontmatter.get("execution")
        if str(frontmatter.get("resource") or "").strip() != "operation":
            raise ValidationOperationResolutionError(
                "validation operation identity is invalid"
            )
        if str(frontmatter.get("key") or "").strip() != operation_key:
            raise ValidationOperationResolutionError(
                "validation operation identity is invalid"
            )
        if not isinstance(execution, Mapping):
            raise ValidationOperationResolutionError(
                "validation operation identity is invalid"
            )
        if str(execution.get("mode") or "").strip() != "validation_workflow":
            raise ValidationOperationResolutionError(
                "validation operation identity is invalid"
            )
        if str(execution.get("validates_operation") or "").strip() != solve_operation:
            raise ValidationOperationResolutionError(
                "validation operation identity is invalid"
            )

    @staticmethod
    def _text(value: object, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationOperationResolutionError(
                "{} is required".format(field_name)
            )
        return value.strip()
