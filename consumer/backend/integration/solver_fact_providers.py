"""Runtime-owned fact providers for solver profiles."""

from __future__ import annotations

import ast
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unicodedata
from typing import Any

from agent.tools.business_data.effective_connection import build_effective_connection
from agent.tools.business_data.resolver import (
    BusinessDataConnectionError,
    resolve_business_data_source,
)
from agent.tools.business_data.script_env import (
    SCRIPT_BUSINESS_DATA_CONNECTION_ENV,
    serialize_script_business_data_connection,
)
from ontology.ontology_catalog import get_ontology_manifest
from solver_coordination.contracts import NormalizedInput
from solver_coordination.errors import (
    KnowledgePackageSolverContractIncompatibleError,
)
from solver_coordination.fact_snapshot import FactSnapshotInvalidError


_VALIDATION_MODE = "package_validation_fixture"
_PRODUCTION_MODE = "package_evaluation"
_VALIDATION_FIXTURE = Path("tests/solver/golden-route-case.json")
_EVALUATOR_REQUIRED_SYMBOLS = frozenset(
    {"BatchPlan", "build_report", "load_tables", "normalize_crude_name"}
)
_MAX_EVALUATOR_SOURCE_BYTES = 2 * 1024 * 1024
_DECIMAL_RE = re.compile(r"^[+]?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_PLAN_MONTH_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
_MAX_EVALUATION_OUTPUT_BYTES = 4 * 1024 * 1024
_CHILD_ENV_ALLOWLIST = frozenset(
    {
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOGNAME",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USER",
        "WINDIR",
    }
)


class PackageEvaluationError(RuntimeError):
    """Raised when the isolated package evaluator cannot produce facts."""

    def __init__(
        self,
        detail: str,
        *,
        code: str = "business_evaluation_failed",
    ) -> None:
        super().__init__(detail)
        allowed_codes = {
            "business_evaluation_failed",
            KnowledgePackageSolverContractIncompatibleError.code,
        }
        self.code = code if code in allowed_codes else "business_evaluation_failed"


def _strict_json_text(raw: str) -> dict[str, Any]:
    if not raw or len(raw.encode("utf-8")) > _MAX_EVALUATION_OUTPUT_BYTES:
        raise PackageEvaluationError("package evaluator output is invalid")

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON number")

    def reject_float(_value: str) -> None:
        raise ValueError("JSON floats are not allowed")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw,
            parse_constant=reject_constant,
            parse_float=reject_float,
            object_pairs_hook=unique_object,
        )
    except ValueError as exc:
        raise PackageEvaluationError("package evaluator output is invalid") from exc
    if not isinstance(value, dict):
        raise PackageEvaluationError("package evaluator output is invalid")
    return value


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FactSnapshotInvalidError("{} is required".format(field_name))
    return value.strip()


def _strict_json_object(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise FactSnapshotInvalidError("validation fixture is unavailable") from exc
    if not raw or len(raw) > 1024 * 1024 or raw.startswith(b"\xef\xbb\xbf"):
        raise FactSnapshotInvalidError("validation fixture is invalid")

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON number")

    def reject_float(_value: str) -> None:
        raise ValueError("JSON floats are not allowed")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            parse_constant=reject_constant,
            parse_float=reject_float,
            object_pairs_hook=unique_object,
        )
    except ValueError as exc:
        raise FactSnapshotInvalidError("validation fixture is invalid") from exc
    if not isinstance(value, dict):
        raise FactSnapshotInvalidError("validation fixture is invalid")
    return value


def _contained_file(root: Path, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise FactSnapshotInvalidError("validation fixture path is invalid")
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise FactSnapshotInvalidError("validation fixture path is invalid")
    try:
        resolved = cursor.resolve(strict=True)
    except OSError as exc:
        raise FactSnapshotInvalidError("validation fixture is unavailable") from exc
    if not resolved.is_file() or root not in resolved.parents:
        raise FactSnapshotInvalidError("validation fixture path is invalid")
    return resolved


def _canonical_origin(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _required_text(value, "crude_origin"))
    return "".join(character for character in text if not character.isspace())


def _positive_decimal(value: Any, field_name: str) -> Decimal:
    text = _required_text(value, field_name)
    if len(text) > 128 or _DECIMAL_RE.fullmatch(text) is None:
        raise FactSnapshotInvalidError("validation fixture input does not match request")
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise FactSnapshotInvalidError(
            "validation fixture input does not match request"
        ) from exc
    if not parsed.is_finite() or parsed <= 0:
        raise FactSnapshotInvalidError("validation fixture input does not match request")
    return parsed


def _month(value: Any) -> str:
    text = _required_text(value, "plan_month")
    if re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", text) is None:
        raise FactSnapshotInvalidError("validation fixture input does not match request")
    return text[-2:]


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def _ordered_candidate_payloads(values: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    items = values.get("items")
    if not isinstance(items, (list, tuple)) or not 1 <= len(items) <= 64:
        raise FactSnapshotInvalidError("ordered route items are invalid")
    item_ids: list[str] = []
    for expected_order, item in enumerate(items, start=1):
        if not isinstance(item, Mapping) or item.get("item_order") != expected_order:
            raise FactSnapshotInvalidError("ordered route items are invalid")
        item_id = _required_text(item.get("item_id"), "item_id")
        if item_id in item_ids:
            raise FactSnapshotInvalidError("ordered route item ids are invalid")
        item_ids.append(item_id)

    count = len(item_ids)
    assignments: list[tuple[str, tuple[str, ...]]] = [
        ("ALL_0", ("OPTION_0",) * count),
        ("ALL_1", ("OPTION_1",) * count),
    ]
    assignments.extend(
        (
            "OPTION_0_TO_OPTION_1_K{:04d}".format(index),
            ("OPTION_0",) * index + ("OPTION_1",) * (count - index),
        )
        for index in range(1, count)
    )
    assignments.extend(
        (
            "OPTION_1_TO_OPTION_0_K{:04d}".format(index),
            ("OPTION_1",) * index + ("OPTION_0",) * (count - index),
        )
        for index in range(1, count)
    )
    return tuple(
        {
            "candidate_id": candidate_id,
            "assignments": list(options),
        }
        for candidate_id, options in assignments
    )


def _contained_package_script(root: Path, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise FactSnapshotInvalidError("package evaluator path is invalid")
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise FactSnapshotInvalidError("package evaluator path is invalid")
    try:
        resolved = cursor.resolve(strict=True)
    except OSError as exc:
        raise FactSnapshotInvalidError("package evaluator is unavailable") from exc
    if not resolved.is_file() or root not in resolved.parents:
        raise FactSnapshotInvalidError("package evaluator path is invalid")
    return resolved


def _discover_package_evaluator(
    root: Path,
    decision_model: Mapping[str, Any],
) -> Path:
    fact_source = decision_model.get("fact_source")
    if not isinstance(fact_source, Mapping):
        raise FactSnapshotInvalidError("package evaluation fact source is invalid")
    explicit = fact_source.get("evaluator_path")
    if explicit is not None:
        if not isinstance(explicit, str) or not explicit.strip():
            raise FactSnapshotInvalidError("package evaluator path is invalid")
        return _contained_package_script(root, Path(explicit.strip()))

    scripts_dir = root / "scripts"
    if not scripts_dir.is_dir() or scripts_dir.is_symlink():
        raise FactSnapshotInvalidError("package evaluator is unavailable")
    matches: list[Path] = []
    for candidate in sorted(scripts_dir.glob("*.py")):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        try:
            source = candidate.read_bytes()
        except OSError:
            continue
        if not source or len(source) > _MAX_EVALUATOR_SOURCE_BYTES:
            continue
        try:
            module = ast.parse(source.decode("utf-8"), filename=candidate.name)
        except (UnicodeError, SyntaxError):
            continue
        symbols = {
            node.name
            for node in module.body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        }
        if _EVALUATOR_REQUIRED_SYMBOLS.issubset(symbols):
            matches.append(candidate.resolve())
    if len(matches) != 1:
        raise FactSnapshotInvalidError(
            "package must declare or uniquely provide one solver evaluator"
        )
    return matches[0]


@dataclass(frozen=True)
class PackageRouteEvaluationRunner:
    """Run one package evaluator in an isolated child process for all candidates."""

    worker_path: Path = Path(__file__).with_name("solver_route_fact_worker.py")
    timeout_seconds: int = 180

    def __post_init__(self) -> None:
        worker = Path(self.worker_path).resolve()
        if not worker.is_file():
            raise RuntimeError("solver route fact worker is unavailable")
        object.__setattr__(self, "worker_path", worker)
        configured = (os.environ.get("SOLVER_ROUTE_EVALUATION_TIMEOUT_SECONDS") or "").strip()
        timeout = self.timeout_seconds
        if configured:
            try:
                timeout = int(configured)
            except ValueError as exc:
                raise RuntimeError(
                    "SOLVER_ROUTE_EVALUATION_TIMEOUT_SECONDS must be an integer"
                ) from exc
        if isinstance(timeout, bool) or not 1 <= timeout <= 1800:
            raise RuntimeError("solver route evaluation timeout is invalid")
        object.__setattr__(self, "timeout_seconds", timeout)

    def __call__(
        self,
        *,
        evaluator_path: Path,
        normalized_input: Mapping[str, Any],
        candidates: tuple[dict[str, Any], ...],
        business_data_source: Any,
        graph_scope: Mapping[str, str],
        **_identity: Any,
    ) -> Mapping[str, Any]:
        effective = build_effective_connection(business_data_source)
        effective = replace(
            effective,
            options={
                **effective.options,
                "workspace_id": str(graph_scope.get("workspace_id") or "").strip(),
                "ontology_id": str(graph_scope.get("ontology_id") or "").strip(),
            },
        )
        child_env = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in _CHILD_ENV_ALLOWLIST
        }
        child_env[SCRIPT_BUSINESS_DATA_CONNECTION_ENV] = (
            serialize_script_business_data_connection(connection=effective)
        )
        backend_root = Path(__file__).resolve().parents[1]
        python_paths = [str(backend_root), str(evaluator_path.parent)]
        if child_env.get("PYTHONPATH"):
            python_paths.append(child_env["PYTHONPATH"])
        child_env["PYTHONPATH"] = os.pathsep.join(python_paths)
        child_env.setdefault("PYTHONIOENCODING", "utf-8")
        child_env.setdefault("PYTHONUTF8", "1")
        payload = json.dumps(
            {
                "normalized_input": _thaw(normalized_input),
                "candidates": _thaw(candidates),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "integration.solver_route_fact_worker",
                    "--evaluator",
                    str(evaluator_path),
                ],
                input=payload,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=child_env,
                cwd=str(evaluator_path.parent),
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PackageEvaluationError("package evaluator could not be executed") from exc

        result = _strict_json_text(completed.stdout)
        if completed.returncode != 0 or result.get("error"):
            error = result.get("error")
            detail = (
                str(error.get("detail") or "").strip()
                if isinstance(error, Mapping)
                else ""
            )
            if not detail or len(detail) > 512:
                detail = "package evaluator failed"
            code = (
                str(error.get("code") or "").strip()
                if isinstance(error, Mapping)
                else ""
            )
            raise PackageEvaluationError(detail, code=code)
        digest = hashlib.sha256(evaluator_path.read_bytes()).hexdigest()
        return {
            **result,
            "provider_binding_revision": "package-evaluator:sha256:{}".format(
                digest
            ),
        }


@dataclass(frozen=True)
class PackageEvaluationFactProvider:
    """Materialize request-specific route facts from the selected package and data source."""

    ontology_resolver: Callable[..., Any] = get_ontology_manifest
    business_data_resolver: Callable[..., Any] = resolve_business_data_source
    evaluation_runner: Callable[..., Any] = field(
        default_factory=PackageRouteEvaluationRunner
    )

    def __post_init__(self) -> None:
        for value, name in (
            (self.ontology_resolver, "ontology_resolver"),
            (self.business_data_resolver, "business_data_resolver"),
            (self.evaluation_runner, "evaluation_runner"),
        ):
            if not callable(value):
                raise TypeError("{} must be callable".format(name))

    def resolve_facts(
        self,
        *,
        capability: Any,
        normalized_input: NormalizedInput,
        required_facts: Any,
        binding_revision_id: str,
        workspace_id: str,
        package_id: str,
        package_revision: str,
        **_identity: Any,
    ) -> Mapping[str, Any]:
        if not isinstance(normalized_input, NormalizedInput):
            raise FactSnapshotInvalidError("normalized input is invalid")
        required = tuple(required_facts)
        if (
            len(required) != len(set(required))
            or set(required)
            not in ({"route_metrics"}, {"route_metrics", "refinery_route_context"})
        ):
            raise FactSnapshotInvalidError("package evaluation facts are incompatible")
        binding = _required_text(binding_revision_id, "solver binding revision")
        selected_workspace = _required_text(workspace_id, "workspace_id")
        selected_package = _required_text(package_id, "package_id")
        selected_revision = _required_text(package_revision, "package_revision")
        if not isinstance(capability, Mapping):
            raise FactSnapshotInvalidError("solver capability is invalid")
        if capability.get("package_id") != selected_package or capability.get(
            "package_revision"
        ) != selected_revision:
            raise FactSnapshotInvalidError("solver capability identity is invalid")
        model = capability.get("decision_model")
        fact_source = dict(model.get("fact_source") or {}) if isinstance(model, Mapping) else {}
        if not isinstance(model, Mapping):
            raise FactSnapshotInvalidError("package evaluation fact source is invalid")
        if model.get("profile_id") != "ordered_binary_route.v1":
            raise FactSnapshotInvalidError("package evaluation fact source is invalid")
        if (
            fact_source.get("kind") != "external_snapshot"
            or fact_source.get("fail_closed") is not True
        ):
            raise FactSnapshotInvalidError("package evaluation fact source is invalid")
        if not set(fact_source).issubset(
            {"kind", "fail_closed", "evaluator_path"}
        ):
            raise FactSnapshotInvalidError("package evaluation fact source is invalid")

        ontology, error = self.ontology_resolver(
            selected_package,
            version=selected_revision,
            workspace_id=selected_workspace,
        )
        if error or ontology is None or str(ontology.version) != selected_revision:
            raise FactSnapshotInvalidError("package evaluator package is unavailable")
        try:
            root = Path(ontology.ontology_dir).resolve(strict=True)
        except OSError as exc:
            raise FactSnapshotInvalidError("package evaluator package is unavailable") from exc
        if not root.is_dir() or root.is_symlink():
            raise FactSnapshotInvalidError("package evaluator package is invalid")
        evaluator_path = _discover_package_evaluator(root, model)

        try:
            source = self.business_data_resolver(
                workspace_id=selected_workspace,
                ontology_id=selected_package,
            )
        except BusinessDataConnectionError as exc:
            raise FactSnapshotInvalidError(
                "package business data source is unavailable"
            ) from exc
        if str(getattr(source, "provider", "") or "").strip().lower() != "neo4j":
            raise FactSnapshotInvalidError(
                "package evaluator requires a Neo4j business data source"
            )
        source_config = dict(getattr(source, "config", {}) or {})
        graph_scope = {
            "workspace_id": str(
                source_config.get("workspace_value") or selected_workspace
            ).strip(),
            "ontology_id": str(
                source_config.get("ontology_value") or selected_package
            ).strip(),
        }
        candidates = _ordered_candidate_payloads(normalized_input.values)
        try:
            evaluated = self.evaluation_runner(
                evaluator_path=evaluator_path,
                normalized_input=_thaw(normalized_input.values),
                candidates=candidates,
                business_data_source=source,
                graph_scope=graph_scope,
                workspace_id=selected_workspace,
                package_id=selected_package,
                package_revision=selected_revision,
            )
        except Exception as exc:
            if isinstance(exc, KnowledgePackageSolverContractIncompatibleError):
                raise
            if (
                isinstance(exc, PackageEvaluationError)
                and exc.code
                == KnowledgePackageSolverContractIncompatibleError.code
            ):
                raise KnowledgePackageSolverContractIncompatibleError(
                    "selected knowledge package solver contract is incompatible"
                ) from exc
            if isinstance(exc, FactSnapshotInvalidError):
                raise
            raise FactSnapshotInvalidError(
                "package business evaluation failed"
            ) from exc
        if not isinstance(evaluated, Mapping):
            raise FactSnapshotInvalidError("package business evaluation is invalid")
        route_metrics = evaluated.get("route_metrics")
        route_context = evaluated.get("refinery_route_context")
        expected_ids_list = []
        for candidate in candidates:
            expected_ids_list.append(candidate["candidate_id"])
        expected_ids = tuple(expected_ids_list)
        raw_candidates = (
            route_metrics.get("candidates")
            if isinstance(route_metrics, Mapping)
            else None
        )
        if not isinstance(route_metrics, Mapping):
            raise FactSnapshotInvalidError("package route metrics are invalid")
        if set(route_metrics) != {"metric_scale", "rounding", "candidates"}:
            raise FactSnapshotInvalidError("package route metrics are invalid")
        if not isinstance(raw_candidates, Mapping):
            raise FactSnapshotInvalidError("package route metrics are invalid")
        if set(raw_candidates) != set(expected_ids):
            raise FactSnapshotInvalidError("package route metrics are invalid")
        raw_context_items = (
            route_context.get("items") if isinstance(route_context, Mapping) else None
        )
        expected_item_ids_list = []
        for item in normalized_input.values.get("items", ()):
            if isinstance(item, Mapping):
                expected_item_ids_list.append(str(item.get("item_id") or ""))
        expected_item_ids = tuple(expected_item_ids_list)
        if not isinstance(route_context, Mapping):
            raise FactSnapshotInvalidError("package refinery route context is invalid")
        if set(route_context) != {
            "schema_version",
            "profile_id",
            "metric_scale",
            "rounding",
            "plan_month",
            "production_condition",
            "items",
            "model",
            "tables",
        }:
            raise FactSnapshotInvalidError("package refinery route context is invalid")
        if (
            route_context.get("schema_version") != "refinery_route_context.v1"
            or route_context.get("profile_id") != "refinery_route.v1"
            or route_context.get("metric_scale") != 100
        ):
            raise FactSnapshotInvalidError("package refinery route context is invalid")
        if (
            route_context.get("rounding") != "ROUND_HALF_EVEN"
            or route_context.get("plan_month")
            != normalized_input.values.get("plan_month")
            or route_context.get("production_condition") != "NO_JET"
        ):
            raise FactSnapshotInvalidError("package refinery route context is invalid")
        if not isinstance(raw_context_items, (list, tuple)):
            raise FactSnapshotInvalidError("package refinery route context is invalid")
        actual_item_ids_list = []
        for item in raw_context_items:
            if isinstance(item, Mapping):
                actual_item_ids_list.append(str(item.get("item_id") or ""))
        actual_item_ids = tuple(actual_item_ids_list)
        if actual_item_ids != expected_item_ids:
            raise FactSnapshotInvalidError("package refinery route context is invalid")
        if len(raw_context_items) != len(expected_item_ids):
            raise FactSnapshotInvalidError("package refinery route context is invalid")
        if (
            not isinstance(route_context.get("model"), Mapping)
            or not isinstance(route_context.get("tables"), Mapping)
        ):
            raise FactSnapshotInvalidError("package refinery route context is invalid")
        evidence = evaluated.get("source_evidence")
        if not isinstance(evidence, (list, tuple)) or not evidence:
            raise FactSnapshotInvalidError("package evaluation evidence is invalid")
        for item in evidence:
            if not isinstance(item, Mapping):
                raise FactSnapshotInvalidError("package evaluation evidence is invalid")
        source_evidence = []
        for item in evidence:
            source_evidence.append(dict(item))
        as_of = _required_text(evaluated.get("as_of"), "package evaluation as_of")
        provider_revision = _required_text(
            evaluated.get("provider_binding_revision"),
            "provider binding revision",
        )
        return {
            "facts": [
                {"name": "route_metrics", "value": _thaw(route_metrics)},
                {
                    "name": "refinery_route_context",
                    "value": _thaw(route_context),
                },
            ],
            "bindings": {
                "provider_binding_revision": provider_revision,
                "solver_binding_revision": binding,
            },
            "as_of": as_of,
            "source_evidence": tuple(source_evidence),
        }


@dataclass(frozen=True)
class PackageValidationFixtureFactProvider:
    """Load one exact synthetic package fixture for a named validation workspace."""

    allowed_workspace_id: str
    ontology_resolver: Callable[..., Any] = get_ontology_manifest

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "allowed_workspace_id",
            _required_text(self.allowed_workspace_id, "validation workspace"),
        )
        if not callable(self.ontology_resolver):
            raise TypeError("ontology_resolver must be callable")

    def resolve_facts(
        self,
        *,
        capability: Any,
        normalized_input: NormalizedInput,
        required_facts: Any,
        binding_revision_id: str,
        workspace_id: str,
        package_id: str,
        package_revision: str,
        **_identity: Any,
    ) -> Mapping[str, Any]:
        if workspace_id != self.allowed_workspace_id:
            raise FactSnapshotInvalidError("validation workspace is not enabled")
        if not isinstance(normalized_input, NormalizedInput):
            raise FactSnapshotInvalidError("normalized input is invalid")
        required = tuple(required_facts)
        if (
            len(required) != len(set(required))
            or set(required)
            not in ({"route_metrics"}, {"route_metrics", "refinery_route_context"})
        ):
            raise FactSnapshotInvalidError("validation fixture facts are incompatible")
        binding = _required_text(binding_revision_id, "solver binding revision")
        selected_package = _required_text(package_id, "package_id")
        selected_revision = _required_text(package_revision, "package_revision")

        if not isinstance(capability, Mapping):
            raise FactSnapshotInvalidError("solver capability is invalid")
        if capability.get("package_id") != selected_package or capability.get(
            "package_revision"
        ) != selected_revision:
            raise FactSnapshotInvalidError("solver capability identity is invalid")
        model = capability.get("decision_model")
        if not isinstance(model, Mapping) or dict(model.get("fact_source") or {}) != {
            "kind": "external_snapshot",
            "fail_closed": True,
        }:
            raise FactSnapshotInvalidError("validation fixture fact source is invalid")

        ontology, error = self.ontology_resolver(
            selected_package,
            version=selected_revision,
            workspace_id=workspace_id,
        )
        if error or ontology is None or str(ontology.version) != selected_revision:
            raise FactSnapshotInvalidError("validation fixture package is unavailable")
        try:
            root = Path(ontology.ontology_dir).resolve(strict=True)
        except OSError as exc:
            raise FactSnapshotInvalidError("validation fixture package is unavailable") from exc
        if not root.is_dir() or root.is_symlink():
            raise FactSnapshotInvalidError("validation fixture package is invalid")

        golden = _strict_json_object(_contained_file(root, _VALIDATION_FIXTURE))
        if golden.get("fixture_scope") != "test_only":
            raise FactSnapshotInvalidError("validation fixture is not test-only")
        case_id = _required_text(golden.get("case_id"), "validation case_id")
        self._validate_input(golden.get("input"), normalized_input.values)

        metric_name = _required_text(
            golden.get("route_metric_fixture"), "route metric fixture"
        )
        metric_relative = Path("tests/solver") / metric_name
        if Path(metric_name).name != metric_name:
            raise FactSnapshotInvalidError("validation fixture path is invalid")
        metrics = _strict_json_object(_contained_file(root, metric_relative))
        if (
            metrics.get("fixture_scope") != "test_only"
            or metrics.get("case_id") != case_id
        ):
            raise FactSnapshotInvalidError("validation fixture identity is invalid")

        raw_facts = metrics.get("facts")
        matching = []
        context_matching = []
        if isinstance(raw_facts, list):
            for fact in raw_facts:
                if not isinstance(fact, Mapping):
                    continue
                if fact.get("name") == "route_metrics":
                    matching.append(fact)
                if fact.get("name") == "refinery_route_context":
                    context_matching.append(fact)
        if len(matching) != 1 or not isinstance(matching[0].get("value"), Mapping):
            raise FactSnapshotInvalidError("validation route metrics are invalid")
        if "refinery_route_context" in required and (
            len(context_matching) != 1
            or not isinstance(context_matching[0].get("value"), Mapping)
        ):
            raise FactSnapshotInvalidError("validation refinery route context is invalid")
        route_metrics = deepcopy(dict(matching[0]["value"]))
        fixture_items = route_metrics.get("items")
        golden_input = golden.get("input")
        golden_items = golden_input.get("items") if isinstance(golden_input, Mapping) else None
        actual_items = normalized_input.values.get("items")
        if not isinstance(fixture_items, Mapping) or not isinstance(
            golden_items, list
        ) or not isinstance(actual_items, (list, tuple)):
            raise FactSnapshotInvalidError("validation route metrics are invalid")
        remapped: dict[str, Any] = {}
        for expected, actual in zip(golden_items, actual_items):
            fixture_id = _required_text(expected.get("item_id"), "fixture item_id")
            actual_id = _required_text(actual.get("item_id"), "item_id")
            if fixture_id not in fixture_items or actual_id in remapped:
                raise FactSnapshotInvalidError("validation route metrics are invalid")
            remapped[actual_id] = deepcopy(fixture_items[fixture_id])
        expected_fixture_ids = set()
        for item in golden_items:
            expected_fixture_ids.add(
                _required_text(item.get("item_id"), "fixture item_id")
            )
        if set(fixture_items) != expected_fixture_ids:
            raise FactSnapshotInvalidError("validation route metrics are invalid")
        route_metrics["items"] = remapped
        facts = [{"name": "route_metrics", "value": route_metrics}]
        if context_matching:
            route_context = deepcopy(dict(context_matching[0]["value"]))
            context_items = route_context.get("items")
            if not isinstance(context_items, list) or len(context_items) != len(
                actual_items
            ):
                raise FactSnapshotInvalidError(
                    "validation refinery route context is invalid"
                )
            for expected, actual, context_item in zip(
                golden_items, actual_items, context_items
            ):
                if (
                    not isinstance(context_item, dict)
                    or context_item.get("item_id")
                    != _required_text(expected.get("item_id"), "fixture item_id")
                ):
                    raise FactSnapshotInvalidError(
                        "validation refinery route context is invalid"
                    )
                context_item["item_id"] = _required_text(actual.get("item_id"), "item_id")
            route_context["plan_month"] = _required_text(
                normalized_input.values.get("plan_month"), "plan_month"
            )
            facts.append(
                {"name": "refinery_route_context", "value": route_context}
            )
        evidence = {
            "kind": "synthetic_test_fixture",
            "ref": "package-validation-fixture:{}".format(case_id),
        }
        return {
            "facts": facts,
            "bindings": {
                "provider_binding_revision": "package-validation-fixture:{}".format(
                    case_id
                ),
                "solver_binding_revision": binding,
            },
            "as_of": _required_text(metrics.get("as_of"), "validation as_of"),
            "source_evidence": (evidence,),
        }

    @staticmethod
    def _validate_input(expected: Any, actual: Mapping[str, Any]) -> None:
        if not isinstance(expected, Mapping) or not isinstance(actual, Mapping):
            raise FactSnapshotInvalidError("validation fixture input does not match request")
        if expected.get("diesel_unit_no") != actual.get("diesel_unit_no") or _month(
            expected.get("plan_month")
        ) != _month(actual.get("plan_month")):
            raise FactSnapshotInvalidError("validation fixture input does not match request")
        expected_items = expected.get("items")
        actual_items = actual.get("items")
        if not isinstance(expected_items, list) or not isinstance(
            actual_items, (list, tuple)
        ) or len(expected_items) != len(actual_items):
            raise FactSnapshotInvalidError("validation fixture input does not match request")
        for position, (expected_item, actual_item) in enumerate(
            zip(expected_items, actual_items), start=1
        ):
            if not isinstance(expected_item, Mapping) or not isinstance(
                actual_item, Mapping
            ):
                raise FactSnapshotInvalidError(
                    "validation fixture input does not match request"
                )
            if expected_item.get("item_order") != position:
                raise FactSnapshotInvalidError(
                    "validation fixture input does not match request"
                )
            if actual_item.get("item_order") != position:
                raise FactSnapshotInvalidError(
                    "validation fixture input does not match request"
                )
            if _canonical_origin(
                expected_item.get("crude_origin")
            ) != _canonical_origin(actual_item.get("crude_origin")):
                raise FactSnapshotInvalidError(
                    "validation fixture input does not match request"
                )
            if _positive_decimal(
                expected_item.get("quantity_wan_tons"), "quantity_wan_tons"
            ) != _positive_decimal(
                actual_item.get("quantity_wan_tons"), "quantity_wan_tons"
            ):
                raise FactSnapshotInvalidError(
                    "validation fixture input does not match request"
                )
            if _positive_decimal(
                expected_item.get("process_days"), "process_days"
            ) != _positive_decimal(actual_item.get("process_days"), "process_days"):
                raise FactSnapshotInvalidError(
                    "validation fixture input does not match request"
                )


def build_ordered_route_fact_provider(
    *,
    ontology_resolver: Callable[..., Any] | None = None,
    business_data_resolver: Callable[..., Any] | None = None,
    evaluation_runner: Callable[..., Any] | None = None,
) -> Any:
    """Select the production default or an explicit workspace-scoped test provider."""

    mode = (os.environ.get("SOLVER_ROUTE_FACT_PROVIDER") or "").strip()
    if mode in {"", _PRODUCTION_MODE}:
        return PackageEvaluationFactProvider(
            ontology_resolver=ontology_resolver or get_ontology_manifest,
            business_data_resolver=(
                business_data_resolver or resolve_business_data_source
            ),
            evaluation_runner=evaluation_runner or PackageRouteEvaluationRunner(),
        )
    if mode != _VALIDATION_MODE:
        raise RuntimeError("SOLVER_ROUTE_FACT_PROVIDER is invalid")
    workspace_id = (os.environ.get("SOLVER_ROUTE_VALIDATION_WORKSPACE") or "").strip()
    if not workspace_id:
        raise RuntimeError("SOLVER_ROUTE_VALIDATION_WORKSPACE is required")
    return PackageValidationFixtureFactProvider(
        allowed_workspace_id=workspace_id,
        ontology_resolver=ontology_resolver or get_ontology_manifest,
    )


__all__ = [
    "PackageEvaluationFactProvider",
    "PackageRouteEvaluationRunner",
    "PackageValidationFixtureFactProvider",
    "build_ordered_route_fact_provider",
]
