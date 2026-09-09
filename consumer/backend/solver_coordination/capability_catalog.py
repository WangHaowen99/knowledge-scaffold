"""Immutable, public-safe catalog of ontology solver capabilities."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from ontology.ontologies import (
    SUPPORTED_ONTOLOGY_SCHEMA_VERSIONS,
    validate_no_runtime_binding_data,
)
from solver_coordination.canonical import json_schema_digest, sha256_digest


class SolverCapabilityCatalogError(ValueError):
    """Raised when a declared solver contract is unsafe or inconsistent."""


@dataclass(frozen=True, slots=True)
class SolverOperationCapability:
    package_id: str
    package_revision: str
    operation_key: str
    decision_model_id: str
    problem_family: str
    input_schema: Mapping[str, Any]
    result_schema: Mapping[str, Any]
    contract_digest: str
    validator_revision: str
    enabled: bool
    package_digest: str
    operation_digest: str
    decision_model_digest: str
    input_schema_digest: str
    result_schema_digest: str
    engine_capabilities: tuple[str, ...] = ()


SolverCapability = SolverOperationCapability


@dataclass(frozen=True, slots=True)
class _CapabilityDraft:
    operation_key: str
    decision_model_id: str
    problem_family: str
    input_schema: Mapping[str, Any]
    result_schema: Mapping[str, Any]
    validator_revision: str
    operation_digest: str
    decision_model_digest: str
    input_schema_digest: str
    result_schema_digest: str
    engine_capabilities: tuple[str, ...]
    package_basis: Mapping[str, Any]
    contract_basis: Mapping[str, Any]


def load_solver_capability_catalog(
    package_root: Path,
    manifest: Mapping[str, Any],
    *,
    package_revision: str | None = None,
) -> tuple[SolverOperationCapability, ...]:
    """Load enabled solver operations from an immutable package revision."""

    _validate_declarative_contract(manifest)

    schema_version = str(manifest.get("schema_version") or "").strip()
    if schema_version not in SUPPORTED_ONTOLOGY_SCHEMA_VERSIONS:
        raise SolverCapabilityCatalogError(
            "Unsupported ontology schema version: {}".format(schema_version or "missing")
        )
    if schema_version == "0.1":
        return ()
    if not _is_enabled(manifest.get("enabled", True)):
        return ()

    root = Path(package_root).resolve(strict=True)
    package_id = str(manifest.get("id") or "").strip()
    revision = str(package_revision or manifest.get("version") or "").strip()
    if not package_id or not revision:
        raise SolverCapabilityCatalogError("Solver package identity is incomplete")

    capabilities = manifest.get("capabilities")
    if not isinstance(capabilities, Mapping):
        return ()
    if not _is_enabled(capabilities.get("enabled", True)):
        return ()
    raw_models = capabilities.get("decision_models")
    if raw_models is None:
        return ()
    if not isinstance(raw_models, list):
        raise SolverCapabilityCatalogError("capabilities.decision_models must be a list")

    models: dict[str, Mapping[str, Any]] = {}
    for raw_model in raw_models:
        if not isinstance(raw_model, Mapping):
            continue
        _validate_declarative_contract(raw_model)
        if not _is_enabled(raw_model.get("enabled", True)):
            continue
        model_id = str(raw_model.get("id") or "").strip()
        if not model_id:
            continue
        if model_id in models:
            raise SolverCapabilityCatalogError(
                "Duplicate decision model id: {}".format(model_id)
            )
        models[model_id] = raw_model

    drafts: list[_CapabilityDraft] = []
    operation_keys: set[str] = set()
    operations_dir = root / "operations"
    if not operations_dir.is_dir():
        return ()
    nested_operations = []
    for path in operations_dir.rglob("*.md"):
        if path.parent != operations_dir:
            nested_operations.append(path)
    if nested_operations:
        raise SolverCapabilityCatalogError(
            "Operation Markdown files must use a flat operations directory"
        )

    for operation_path in sorted(operations_dir.glob("*.md")):
        if operation_path.name.lower() == "index.md":
            continue
        operation_path = _resolve_declared_file(
            root,
            operation_path.relative_to(root),
            "operation",
        )
        frontmatter = _read_operation_frontmatter(operation_path)
        if frontmatter is None:
            continue
        _validate_declarative_contract(frontmatter)
        if not _is_enabled(frontmatter.get("enabled", True)):
            continue
        execution = frontmatter.get("execution")
        if not isinstance(execution, Mapping):
            continue
        if str(execution.get("mode") or "").strip() != "solver_workflow":
            continue
        if not _is_enabled(execution.get("enabled", True)):
            continue
        if str(frontmatter.get("resource") or "").strip() != "operation":
            continue
        operation_key = str(frontmatter.get("key") or "").strip()
        model_id = str(
            execution.get("decision_model")
            or execution.get("decision_model_id")
            or frontmatter.get("decision_model")
            or frontmatter.get("decision_model_id")
            or ""
        ).strip()
        if not operation_key or not model_id or model_id not in models:
            continue
        if operation_key in operation_keys:
            raise SolverCapabilityCatalogError(
                "Duplicate solver operation key: {}".format(operation_key)
            )

        model = models[model_id]
        entry_path = _resolve_declared_file(root, model.get("entry"), "decision model")
        input_path = _resolve_declared_file(root, model.get("input_schema"), "input schema")
        result_path = _resolve_declared_file(root, model.get("result_schema"), "result schema")
        presentation = model.get("presentation")

        model_document = _read_yaml_mapping(entry_path, "decision model")
        _validate_declarative_contract(model_document)
        document_id = str(model_document.get("id") or "").strip()
        if document_id and document_id != model_id:
            raise SolverCapabilityCatalogError(
                "Decision model id does not match its declaration: {}".format(model_id)
            )
        input_schema = _read_json_mapping(input_path, "input schema")
        result_schema = _read_json_mapping(result_path, "result schema")
        _validate_declarative_contract(input_schema)
        _validate_declarative_contract(result_schema)
        engine_requirements = model.get("engine_requirements")
        if not isinstance(engine_requirements, Mapping):
            continue
        problem_family = str(engine_requirements.get("problem_family") or "").strip()
        if not problem_family:
            continue
        raw_engine_capabilities = engine_requirements.get("capabilities") or []
        if not isinstance(raw_engine_capabilities, list):
            raise SolverCapabilityCatalogError("Engine capabilities must be a list")
        normalized_capabilities: set[str] = set()
        for item in raw_engine_capabilities:
            value = str(item or "").strip()
            if value:
                normalized_capabilities.add(value)
        engine_capabilities = tuple(sorted(normalized_capabilities))

        operation_digest = _file_digest(operation_path)
        try:
            decision_model_digest = sha256_digest(model_document)
        except ValueError as exc:
            raise SolverCapabilityCatalogError(
                "Decision model is not canonical"
            ) from exc
        input_schema_digest = json_schema_digest(input_schema)
        result_schema_digest = json_schema_digest(result_schema)
        validator_revision = str(
            execution.get("validator_revision")
            or model.get("validator_revision")
            or ""
        ).strip()
        policy = execution.get("policy") or model.get("policy") or {}
        if not isinstance(policy, Mapping):
            raise SolverCapabilityCatalogError("Solver policy must be a mapping")
        _validate_declarative_contract(policy)
        try:
            policy_digest = sha256_digest(policy)
        except ValueError as exc:
            raise SolverCapabilityCatalogError("Solver policy is not canonical") from exc
        presentation_digest = ""
        if presentation:
            presentation_path = _resolve_declared_file(root, presentation, "presentation")
            presentation_document = _read_yaml_mapping(presentation_path, "presentation")
            _validate_declarative_contract(presentation_document)
            presentation_digest = _file_digest(presentation_path)

        package_basis = {
            "decision_model_id": model_id,
            "decision_model_digest": decision_model_digest,
            "input_schema_digest": input_schema_digest,
            "result_schema_digest": result_schema_digest,
            "presentation_digest": presentation_digest,
            "problem_family": problem_family,
            "validator_revision": validator_revision,
            "engine_capabilities": engine_capabilities,
            "policy_digest": policy_digest,
        }
        contract_basis = {
            **package_basis,
            "operation_key": operation_key,
            "operation_digest": operation_digest,
        }
        drafts.append(
            _CapabilityDraft(
                operation_key=operation_key,
                decision_model_id=model_id,
                problem_family=problem_family,
                input_schema=input_schema,
                result_schema=result_schema,
                validator_revision=validator_revision,
                operation_digest=operation_digest,
                decision_model_digest=decision_model_digest,
                input_schema_digest=input_schema_digest,
                result_schema_digest=result_schema_digest,
                engine_capabilities=engine_capabilities,
                package_basis=package_basis,
                contract_basis=contract_basis,
            )
        )
        operation_keys.add(operation_key)

    if not drafts:
        return ()

    package_contracts = set()
    for draft in drafts:
        package_contracts.add(sha256_digest(draft.package_basis))
    normalized_package_contracts = sorted(package_contracts)
    package_digest = sha256_digest(
        {
            "package_id": package_id,
            "package_revision": revision,
            "schema_version": schema_version,
            "solver_contracts": normalized_package_contracts,
        }
    )
    records = []
    for draft in drafts:
        records.append(
            SolverOperationCapability(
                package_id=package_id,
                package_revision=revision,
                operation_key=draft.operation_key,
                decision_model_id=draft.decision_model_id,
                problem_family=draft.problem_family,
                input_schema=_freeze(draft.input_schema),
                result_schema=_freeze(draft.result_schema),
                contract_digest=sha256_digest(
                    {"package_digest": package_digest, **draft.contract_basis}
                ),
                validator_revision=draft.validator_revision,
                enabled=True,
                package_digest=package_digest,
                operation_digest=draft.operation_digest,
                decision_model_digest=draft.decision_model_digest,
                input_schema_digest=draft.input_schema_digest,
                result_schema_digest=draft.result_schema_digest,
                engine_capabilities=draft.engine_capabilities,
            )
        )
    return tuple(records)


def public_solver_catalog(
    records: tuple[SolverOperationCapability, ...],
) -> list[dict[str, Any]]:
    """Serialize only solver contract metadata safe for model-facing payloads."""

    return [
        {
            "package_id": record.package_id,
            "package_revision": record.package_revision,
            "operation_key": record.operation_key,
            "decision_model_id": record.decision_model_id,
            "problem_family": record.problem_family,
            "input_schema": _thaw(record.input_schema),
            "result_schema": _thaw(record.result_schema),
            "contract_digest": record.contract_digest,
            "validator_revision": record.validator_revision,
            "enabled": record.enabled,
            "package_digest": record.package_digest,
            "operation_digest": record.operation_digest,
            "decision_model_digest": record.decision_model_digest,
            "input_schema_digest": record.input_schema_digest,
            "result_schema_digest": record.result_schema_digest,
            "engine_capabilities": list(record.engine_capabilities),
        }
        for record in records
    ]


def public_solver_summary(
    records: tuple[SolverOperationCapability, ...] | list[SolverOperationCapability],
) -> list[dict[str, Any]]:
    """Project solver capabilities for the progressive root context.

    The root ontology context is model-facing and is intentionally a discovery
    surface, not a ToolCard schema transport.  Keep this projection limited to
    routing and display metadata; callers that need input/result schemas must
    use :func:`load_public_solver_capabilities` (the private, package-bound
    loader) instead.
    """

    return [
        {
            "package_id": record.package_id,
            "package_revision": record.package_revision,
            "operation_key": record.operation_key,
            "decision_model_id": record.decision_model_id,
            "problem_family": record.problem_family,
            "validator_revision": record.validator_revision,
            "enabled": record.enabled,
            "contract_digest": record.contract_digest,
            "engine_capabilities": list(record.engine_capabilities),
            "load_policy": "progressive",
            "next_action": {
                "resource": "operation",
                "key": record.operation_key,
            },
        }
        for record in records
    ]


def load_public_solver_capabilities(
    package_root: Path,
    manifest: Mapping[str, Any],
    *,
    package_revision: str | None = None,
) -> list[dict[str, Any]]:
    """Load the complete, package-bound capability projection for execution.

    Despite the historical ``public`` name, this function is not used for the
    model-facing root context.  It is the explicit full-contract loading path
    for platform code that must build a strict Solver ToolCard.  The returned
    records include the validated input/result schemas, while still excluding
    runtime binding data through the catalog loader's existing checks.
    """

    records = load_solver_capability_catalog(
        package_root,
        manifest,
        package_revision=package_revision,
    )
    return public_solver_catalog(records)


def load_solver_summary_catalog(
    package_root: Path,
    manifest: Mapping[str, Any],
    *,
    package_revision: str | None = None,
) -> list[dict[str, Any]]:
    """Load only the progressive solver summary from package declarations.

    This intentionally delegates validation and digest construction to the
    canonical catalog loader for now, then discards schema documents before
    returning.  The public contract is therefore stable while package-level
    validation remains identical to the full execution path; a future lazy
    reader can replace the internal implementation without changing callers.
    """

    records = load_solver_capability_catalog(
        package_root,
        manifest,
        package_revision=package_revision,
    )
    return public_solver_summary(records)


def load_full_solver_capability_catalog(
    package_root: Path,
    manifest: Mapping[str, Any],
    *,
    package_revision: str | None = None,
) -> list[dict[str, Any]]:
    """Compatibility alias for callers that prefer an explicit full name."""

    return load_public_solver_capabilities(
        package_root,
        manifest,
        package_revision=package_revision,
    )


def _read_operation_frontmatter(path: Path) -> Mapping[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        closing = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
        parsed = yaml.safe_load("\n".join(lines[1:closing])) or {}
    except (StopIteration, yaml.YAMLError):
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _resolve_declared_file(root: Path, value: Any, label: str) -> Path:
    raw = str(value or "").strip()
    relative = Path(raw)
    if not raw or relative.is_absolute() or ".." in relative.parts:
        raise SolverCapabilityCatalogError(
            "{} path must be contained in the package".format(label.capitalize())
        )
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise SolverCapabilityCatalogError(
                "{} path must be contained in the package".format(label.capitalize())
            )
    try:
        resolved = (root / relative).resolve(strict=True)
    except OSError as exc:
        raise SolverCapabilityCatalogError("{} file is missing".format(label.capitalize())) from exc
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise SolverCapabilityCatalogError(
            "{} path must be contained in the package".format(label.capitalize())
        )
    return resolved


def _read_yaml_mapping(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise SolverCapabilityCatalogError("{} is malformed".format(label.capitalize())) from exc
    if not isinstance(value, Mapping):
        raise SolverCapabilityCatalogError("{} must be a mapping".format(label.capitalize()))
    return value


def _read_json_mapping(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SolverCapabilityCatalogError("{} is malformed".format(label.capitalize())) from exc
    if not isinstance(value, Mapping):
        raise SolverCapabilityCatalogError("{} must be a mapping".format(label.capitalize()))
    return value


def _file_digest(path: Path) -> str:
    return sha256_digest({"sha256": hashlib.sha256(path.read_bytes()).hexdigest()})


def _validate_declarative_contract(value: Any) -> None:
    try:
        validate_no_runtime_binding_data(value)
    except ValueError as exc:
        raise SolverCapabilityCatalogError(str(exc)) from exc


def _is_enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    return str(value or "").strip().lower() in {"true", "1", "yes", "on", "enabled"}


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value
